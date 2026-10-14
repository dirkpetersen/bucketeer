"""Sessions, login throttle, CSRF guard and the /api login routes.

* Session: a stateless signed cookie (itsdangerous, SESSION_SECRET). Plain
  itsdangerous rather than Starlette's SessionMiddleware because we need one
  cookie with exact flags, no cookie on unrelated requests (static files), and
  server-side expiry from our own injectable clock. Payload: username, display
  name, email, is_admin, issued-at; never a password or S3 key.
* CSRF: every state-changing /api request must carry `X-Requested-With:
  bucketeer` (`csrf_protect`). A cross-site page cannot add a custom header
  without a CORS preflight, and the app serves no CORS headers, so the browser
  blocks it. SameSite=Strict on the cookie is the second layer.
* Throttle: in-memory brute-force limiter (AD locks accounts after repeated bad
  passwords, so we refuse before contacting LDAP).
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadData, URLSafeSerializer

from .config import Settings
from .db import Database, get_db
from .ldapauth import AdAuthenticator, AuthUser, InvalidCredentials, LdapUnavailable

log = logging.getLogger(__name__)

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "bucketeer"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Throttle numbers (deliberately not env settings).
THROTTLE_WINDOW = 900  # seconds
THROTTLE_LOGIN_LIMIT = 10  # attempts per login name per window
THROTTLE_USER_LIMIT = 10  # per resolved account (all login spellings), below AD's lockout threshold
THROTTLE_IP_LIMIT = 20  # per client IP per window; higher so a shared NAT is not locked out
MAX_FIELD_LEN = 512  # login / password length cap
CLOCK_SKEW = 60  # tolerated issued-at in the future, seconds

MSG_INVALID = "Invalid credentials or not authorized."
MSG_UNAVAILABLE = "Login service unavailable. Try again later."
MSG_THROTTLED = "Too many failed login attempts. Try again later."


# -- sessions -----------------------------------------------------------------


@dataclass(frozen=True)
class SessionUser:
    username: str
    display_name: str
    email: str
    is_admin: bool
    issued_at: int

    def public(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "displayName": self.display_name,
            "email": self.email,
            "isAdmin": self.is_admin,
        }


class SessionManager:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.max_age = settings.session_max_age
        self.secure = settings.session_cookie_secure
        self.clock = clock
        self._ser = URLSafeSerializer(
            settings.session_secret.get_secret_value(),
            salt="bucketeer.session.v1",
            signer_kwargs={"digest_method": sha256},
        )
        # __Host- makes the browser insist on Secure, Path=/ and no Domain.
        self.cookie_name = "__Host-bucketeer" if self.secure else "bucketeer"

    def issue(self, user: AuthUser) -> str:
        """A brand-new token (new issued-at) on every login: login rotates."""
        return self._ser.dumps(
            {
                "username": user.username,
                "display_name": user.display_name,
                "email": user.email,
                "is_admin": user.is_admin,
                "issued_at": int(self.clock()),
            }
        )

    def read(self, token: str | None) -> SessionUser | None:
        if not token:
            return None
        try:
            d = self._ser.loads(token)
        except BadData:
            return None
        try:
            ok = (
                isinstance(d, dict)
                and isinstance(d["username"], str)
                and d["username"]
                and isinstance(d["display_name"], str)
                and isinstance(d["email"], str)
                and isinstance(d["is_admin"], bool)
                and type(d["issued_at"]) is int
            )
        except KeyError:
            return None
        if not ok:
            return None
        age = self.clock() - d["issued_at"]
        if age >= self.max_age or age < -CLOCK_SKEW:  # server-side expiry
            return None
        return SessionUser(
            d["username"], d["display_name"], d["email"], d["is_admin"], d["issued_at"]
        )

    def set_cookie(self, response: Response, user: AuthUser) -> None:
        response.set_cookie(
            self.cookie_name,
            self.issue(user),
            max_age=self.max_age,
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="strict",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.cookie_name, path="/", secure=self.secure, httponly=True, samesite="strict"
        )


# -- throttle -------------------------------------------------------------------


class LoginThrottle:
    """Sliding window of login attempts per login name and per client IP.

    An attempt is counted when it starts (so parallel requests cannot all slip
    under the limit) and refunded when it succeeds or LDAP is down. Blocked
    requests are not counted, so they do not extend the block. State is in
    memory: it resets on restart and assumes one process.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        window: int = THROTTLE_WINDOW,
        login_limit: int = THROTTLE_LOGIN_LIMIT,
        ip_limit: int = THROTTLE_IP_LIMIT,
        user_limit: int = THROTTLE_USER_LIMIT,
    ):
        self.clock, self.window = clock, window
        self.limits = {"login": login_limit, "ip": ip_limit, "user": user_limit}
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._reported: set[tuple[str, str]] = set()

    def _live(self, key: tuple[str, str], now: float) -> deque[float]:
        dq = self._hits.get(key)
        if dq is None:
            return deque()
        while dq and dq[0] <= now - self.window:
            dq.popleft()
        if not dq:
            del self._hits[key]
            self._reported.discard(key)
        return dq

    def _clear(self, key: tuple[str, str]) -> None:
        """Forget a key's hits and its 'already reported' flag together."""
        self._hits.pop(key, None)
        self._reported.discard(key)

    def acquire(self, login: str, ip: str) -> tuple[int, bool] | None:
        """None if the attempt may proceed (and is counted). Otherwise
        (retry_after_seconds, first_time_reported)."""
        now = self.clock()
        if len(self._hits) > 5000:  # opportunistic cleanup of stale keys
            for k in list(self._hits):
                self._live(k, now)
        keys = [("login", login), ("ip", ip)]
        blocked = [k for k in keys if len(self._live(k, now)) >= self.limits[k[0]]]
        if blocked:
            retry = max(int(self._hits[k][0] + self.window - now) + 1 for k in blocked)
            first = any(k not in self._reported for k in blocked)
            self._reported.update(blocked)
            return max(retry, 1), first
        for k in keys:
            self._hits.setdefault(k, deque()).append(now)
        return None

    def acquire_user(self, username: str, login: str) -> tuple[int, bool] | None:
        """Count a password check against the *resolved* account, whatever
        spelling (sAMAccountName, mail, UPN) was typed. When blocked, also
        exhaust the typed login's counter, so that spelling answers 429 from
        now on exactly as an unknown name does after its own limit."""
        now = self.clock()
        key = ("user", username)
        dq = self._live(key, now)
        if len(dq) >= self.limits["user"]:
            retry = max(int(dq[0] + self.window - now) + 1, 1)
            first = key not in self._reported
            self._reported.add(key)
            lk = ("login", login)
            ldq = self._hits.setdefault(lk, deque())
            while len(ldq) < self.limits["login"]:
                ldq.append(now)
            return retry, first
        self._hits.setdefault(key, deque()).append(now)
        return None

    def success(self, login: str, ip: str, username: str) -> None:
        """Right password: forget the failures, refund the IP's attempt."""
        self._clear(("login", login))
        self._clear(("user", username))
        self._refund(("ip", ip))

    def refund(self, login: str, ip: str, username: str | None = None) -> None:
        """Not the user's fault (LDAP down): do not count the attempt."""
        self._refund(("login", login))
        self._refund(("ip", ip))
        if username:
            self._refund(("user", username))

    def _refund(self, key: tuple[str, str]) -> None:
        dq = self._hits.get(key)
        if dq:
            dq.pop()
            if not dq:
                self._clear(key)


# -- dependencies -----------------------------------------------------------------


def csrf_protect(request: Request) -> None:
    """Require `X-Requested-With: bucketeer` on state-changing methods.

    Every router under /api carries this (see `main.create_app`), so new
    endpoints are protected without opting in.
    """
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "Missing or invalid CSRF header.")


def current_user(request: Request) -> SessionUser | None:
    sm: SessionManager = request.app.state.sessions
    return sm.read(request.cookies.get(sm.cookie_name))


def require_user(request: Request) -> SessionUser:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Not logged in.")
    return user


def require_admin(user: Annotated[SessionUser, Depends(require_user)]) -> SessionUser:
    if not user.is_admin:
        raise HTTPException(403, "Administrator access required.")
    return user


# -- routes -------------------------------------------------------------------------

router = APIRouter(tags=["auth"])  # mounted under /api/ in main.create_app


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _audit(db: Database, actor: str, action: str, details: dict[str, Any]) -> None:
    """Best-effort: a broken audit write must not lock everyone out. Never
    pass passwords here (db.scrub is only a safety net)."""
    try:
        await db.run(db.audit.add, actor, action, "user", actor, details)
    except Exception:
        log.exception("audit write failed for %s", action)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.post("/login")
async def login(request: Request, response: Response, db: Annotated[Database, Depends(get_db)]):
    state = request.app.state
    sessions: SessionManager = state.sessions
    throttle: LoginThrottle = state.throttle
    auth: AdAuthenticator = state.authenticator
    ip = _client_ip(request)
    _no_store(response)

    try:
        body = await request.json()
    except ValueError:
        body = None
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("login"), str)
        or not isinstance(body.get("password"), str)
        or len(body["login"]) > MAX_FIELD_LEN
        or len(body["password"]) > MAX_FIELD_LEN
    ):
        # Hand-rolled instead of a pydantic model: FastAPI's 422 echoes the input.
        raise HTTPException(400, "Expected JSON {login, password}.")
    login_name = body["login"].strip()
    password = body["password"]
    actor = login_name.lower() or "(empty)"

    if not login_name or not password.strip():
        log.info("login refused: empty login or password (ip=%s)", ip)
        raise HTTPException(401, MSG_INVALID)

    blocked = throttle.acquire(actor, ip)
    if blocked:
        retry_after, first = blocked
        log.warning("login throttled: login=%r ip=%s retry_after=%ss", actor, ip, retry_after)
        if first:
            await _audit(db, actor, "login.throttled", {"ip": ip})
        headers = {"Retry-After": str(retry_after), "Cache-Control": "no-store"}
        raise HTTPException(429, MSG_THROTTLED, headers=headers)

    uname_key: str | None = None
    try:
        resolved = await auth.resolve(login_name, password)
        # Count the password check against the account itself: the three login
        # spellings are separate typed-login counters but one AD lockout counter.
        uname_key = resolved.user.username.lower()
        blocked = throttle.acquire_user(uname_key, actor)
        if blocked:
            uname_key = None  # this attempt was not counted
            retry_after, first = blocked
            log.warning("login throttled (account): login=%r ip=%s", actor, ip)
            if first:
                await _audit(db, actor, "login.throttled", {"ip": ip, "scope": "account"})
            headers = {"Retry-After": str(retry_after), "Cache-Control": "no-store"}
            raise HTTPException(429, MSG_THROTTLED, headers=headers)
        user = await auth.verify(resolved, password)
    except InvalidCredentials as e:
        log.info("login failed: login=%r ip=%s reason=%s", actor, ip, e.reason)
        await _audit(db, actor, "login.failure", {"ip": ip, "reason": e.reason})
        raise HTTPException(401, MSG_INVALID) from None
    except LdapUnavailable as e:
        throttle.refund(actor, ip, uname_key)
        log.error("login failed: LDAP unavailable (%s)", e)
        await _audit(db, actor, "login.failure", {"ip": ip, "reason": "ldap_unavailable"})
        raise HTTPException(503, MSG_UNAVAILABLE) from None

    throttle.success(actor, ip, user.username.lower())
    sessions.set_cookie(response, user)
    log.info("login ok: user=%s admin=%s ip=%s", user.username, user.is_admin, ip)
    await _audit(
        db, user.username, "login.success", {"ip": ip, "admin": user.is_admin, "login_as": actor}
    )
    state.rgw.warm(user)  # best effort in the background: login never depends on croit
    return SessionUser(user.username, user.display_name, user.email, user.is_admin, 0).public()


@router.post("/logout")
async def logout(request: Request, response: Response, db: Annotated[Database, Depends(get_db)]):
    user = current_user(request)
    request.app.state.sessions.clear_cookie(response)
    _no_store(response)
    if user:
        await _audit(db, user.username, "logout", {"ip": _client_ip(request)})
    return {"ok": True}


@router.get("/me")
def me(request: Request, response: Response, user: Annotated[SessionUser, Depends(require_user)]):
    _no_store(response)
    profile = request.app.state.db.profiles.get(user.username)
    return {**user.public(), "advancedMode": profile["advanced_mode"]}
