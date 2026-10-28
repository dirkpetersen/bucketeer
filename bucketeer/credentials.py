"""Credentials API: the signed-in user's access keys and the values for the
generated `~/.aws/config` / `~/.aws/credentials` snippets.

- `GET /api/credentials` -> `{profile, region, endpointUrl, addressingStyle,
  keys: [{accessKey}]}`. It never carries a secret.
- `POST /api/credentials/reveal` `{accessKey}` -> `{accessKey, secretKey}`. The
  key must be one of the user's own (fetched from croit and matched exactly);
  anything else is a 404. Each reveal is audited (`credentials.reveal`: actor,
  uid, access key, never the secret) and answered with `Cache-Control: no-store`.
- `POST /api/credentials/keys` -> `{accessKey}`: an additional key (at most
  MAX_KEYS own keys, else 409). The secret is generated server-side and is never
  returned; the user reveals it through `/reveal`.
- `POST /api/credentials/keys/{accessKey}/rotate` -> `{accessKey, oldKeyRemoved}`:
  new key first, confirmed in croit's list, only then the old one is deleted. If the
  delete fails the answer is still 200 with `oldKeyRemoved: false`.
- `DELETE /api/credentials/keys/{accessKey}` -> `{accessKey}`; the last key is
  refused (409). Never the whole-set `PUT .../keys`.
Key mutations hold a per-uid lock and are audited (`credentials.key.*`, no secrets).
- `POST /api/credentials/setup-link` `{os}` -> `{url, command, expiresAt, expiresIn, os}`:
  a one-time link (15 minutes, 10 per user per 15 minutes) for the user's first own key.
  `POST /setup/{token}` (outside `/api`: the token is the only credential, no CSRF header)
  returns a script that installs the key as an AWS profile (`bucketeer/setup_script.py`).
  This is the second path a secret may leave the server by (besides `/reveal`). Only the
  SHA-256 of a token is stored; the token is marked used before the secret is read. Audit
  `credentials.setup_link`, `credentials.setup` and `credentials.setup_refused` never hold
  the token, the URL or the secret; the uvicorn access log has the token redacted.

The uid comes only from the session (`require_active_rgw_user`), so suspended
users get no keys. The browser builds the snippets from this data, so a secret
appears in no response other than the reveal.

Endpoint: `S3_ENDPOINT_URL` when set; otherwise the gateway address in croit's
`.s3cfg` for the user (`host_base`, `use_https`), cached in memory; otherwise
null and the UI tells the user to contact an administrator.
"""

import asyncio
import hashlib
import logging
import re
import secrets
import string
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .admin import croit_http_error
from .config import Settings
from .croit import CroitClient, CroitClientError, CroitError
from .rgw_user import RgwAccount, require_active_rgw_user
from .setup_script import SetupValues, command, error_script, script

log = logging.getLogger("bucketeer.credentials")

ENDPOINT_TTL = 300.0  # seconds a discovered endpoint is remembered
ENDPOINT_MISS_TTL = 30.0  # ... and a failed discovery (retry soon)

_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?\Z")


def endpoint_from_s3cfg(text: str) -> str | None:
    """`https?://host_base` from `.s3cfg` text, or None when it has no usable host.
    Comment lines are ignored (the file also mentions the DNS name in one)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, sep, value = line.partition("=")
        if sep:
            values.setdefault(key.strip().lower(), value.strip())
    host = values.get("host_base", "")
    if not _HOST.match(host):
        return None
    https = values.get("use_https", "").lower() in ("true", "yes", "1", "on")
    return f"{'https' if https else 'http'}://{host}"


class EndpointResolver:
    """S3_ENDPOINT_URL, else (croit backend only) the croit `.s3cfg` host (cached in memory)."""

    def __init__(
        self, croit: CroitClient, settings: Settings, clock: Callable[[], float] = time.time
    ):
        self.croit, self.configured, self.clock = croit, settings.s3_endpoint_url, clock
        self._cached: tuple[float, str | None] | None = None  # (expires, endpoint)

    async def resolve(self, uid: str) -> str | None:
        if self.configured:
            return self.configured
        if getattr(self.croit, "backend", "croit") == "rgw":
            return None  # no .s3cfg to ask; Settings requires S3_ENDPOINT_URL with rgw
        now = self.clock()
        if self._cached and self._cached[0] > now:
            return self._cached[1]
        endpoint = None
        try:
            cfg = await self.croit.cfg_file(uid)
            text = cfg.get("fileContents") if isinstance(cfg, dict) else None
            endpoint = endpoint_from_s3cfg(text) if isinstance(text, str) else None
        except CroitClientError as e:
            log.warning("endpoint discovery via the croit .s3cfg failed: %s", type(e).__name__)
        self._cached = (now + (ENDPOINT_TTL if endpoint else ENDPOINT_MISS_TTL), endpoint)
        return endpoint


router = APIRouter(prefix="/credentials", tags=["credentials"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]


class RevealBody(BaseModel):
    accessKey: str = Field(min_length=1, max_length=256)


async def _own_keys(request: Request, uid: str) -> list[dict]:
    try:
        keys = await request.app.state.croit.list_keys(uid)
    except CroitClientError as e:
        raise croit_http_error(e) from None
    # Subuser keys have user "uid:sub": they are never the user's own keys.
    return [k for k in keys or [] if isinstance(k, dict) and k.get("access_key")
            and k.get("user") == uid]


@router.get("")
async def get_credentials(request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    settings: Settings = request.app.state.settings
    keys = await _own_keys(request, acct.uid)
    endpoint = await request.app.state.endpoints.resolve(acct.uid)
    return {
        "profile": settings.aws_profile_name,
        "region": settings.s3_region,
        "endpointUrl": endpoint,
        "addressingStyle": settings.s3_addressing_style,
        "keys": [{"accessKey": k["access_key"]} for k in keys],
    }


@router.post("/reveal")
async def reveal(body: RevealBody, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    key = next((k for k in await _own_keys(request, acct.uid)
                if k["access_key"] == body.accessKey and k.get("secret_key")), None)
    if key is None:
        raise HTTPException(404, "Access key not found.")
    db = request.app.state.db
    # Audit first: if the log cannot be written, the secret is not handed out.
    await db.run(db.audit.add, acct.username, "credentials.reveal", "access_key",
                 key["access_key"], {"uid": acct.uid})
    return {"accessKey": key["access_key"], "secretKey": key["secret_key"]}


# -- key management ---------------------------------------------------------------

MAX_KEYS = 5
_ACCESS_ALPHABET = string.ascii_uppercase + string.digits
_SECRET_ALPHABET = string.ascii_letters + string.digits


def generate_key_pair() -> tuple[str, str]:
    """RGW-style pair: 20 chars of [A-Z0-9] and 40 of [A-Za-z0-9] (no `/` or `+`)."""
    access = "".join(secrets.choice(_ACCESS_ALPHABET) for _ in range(20))
    secret = "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(40))
    return access, secret


class KeyLocks:
    """One asyncio.Lock per uid, so two tabs cannot mutate one user's keys at once."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def __call__(self, uid: str) -> asyncio.Lock:
        return self._locks.setdefault(uid, asyncio.Lock())


def _key_locks(request: Request) -> KeyLocks:
    state = request.app.state
    if not hasattr(state, "key_locks"):
        state.key_locks = KeyLocks()
    return state.key_locks


async def _audit(request: Request, acct: RgwAccount, action: str, target: str, **details):
    db = request.app.state.db
    await db.run(db.audit.add, acct.username, action, "access_key", target,
                 {"uid": acct.uid, **details})


async def _add_new_key(request: Request, uid: str, taken: set[str]) -> str:
    """put_key with a fresh pair; retries when croit says the key belongs to another
    user. Never overwrites: a generated access key already in `taken` is skipped."""
    croit: CroitClient = request.app.state.croit
    for _ in range(5):
        access, secret = generate_key_pair()
        if access in taken:
            continue
        try:
            await croit.put_key(uid, access, secret)
        except CroitError as e:
            if e.status == 409:
                continue
            raise croit_http_error(e) from None
        except CroitClientError as e:
            raise croit_http_error(e) from None
        return access
    raise HTTPException(502, "Could not generate a unique access key. Try again.")


async def _all_access_keys(request: Request, uid: str) -> list[dict]:
    try:
        keys = await request.app.state.croit.list_keys(uid)
    except CroitClientError as e:
        raise croit_http_error(e) from None
    return [k for k in keys or [] if isinstance(k, dict) and k.get("access_key")]


def _own(keys: list[dict], uid: str) -> list[dict]:
    return [k for k in keys if k.get("user") == uid]


@router.post("/keys")
async def create_key(request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    async with _key_locks(request)(acct.uid):
        keys = await _all_access_keys(request, acct.uid)
        if len(_own(keys, acct.uid)) >= MAX_KEYS:
            raise HTTPException(409, f"You can have at most {MAX_KEYS} access keys. "
                                     "Delete one you no longer use first.")
        access = await _add_new_key(request, acct.uid, {k["access_key"] for k in keys})
        await _audit(request, acct, "credentials.key.create", access)
    return {"accessKey": access}


@router.post("/keys/{access_key}/rotate")
async def rotate_key(access_key: str, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    croit: CroitClient = request.app.state.croit
    async with _key_locks(request)(acct.uid):
        keys = await _all_access_keys(request, acct.uid)
        own = _own(keys, acct.uid)
        if not any(k["access_key"] == access_key for k in own):
            raise HTTPException(404, "Access key not found.")
        if len(own) > MAX_KEYS:  # at exactly MAX_KEYS a rotation is fine
            raise HTTPException(409, f"You have more than {MAX_KEYS} access keys. "
                                     "Delete the ones you no longer use first.")
        new = await _add_new_key(request, acct.uid, {k["access_key"] for k in keys})
        # Confirm the new key is really there before the old one goes away.
        try:
            after = _own(await _all_access_keys(request, acct.uid), acct.uid)
        except HTTPException as e:
            await _audit(request, acct, "credentials.key.rotate_failed", access_key,
                         newKey=new, old_key_removed=False, confirmed=None)
            raise e from None
        if not any(k["access_key"] == new for k in after):
            await _audit(request, acct, "credentials.key.rotate_failed", access_key,
                         newKey=new, old_key_removed=False, confirmed=False)
            raise HTTPException(502, "The new key could not be confirmed, so the old key "
                                     "was kept. Check your keys and try again.")
        try:
            await croit.delete_key(acct.uid, access_key)
            removed = True
        except CroitClientError as e:
            log.warning("rotate: old key not removed for %s: %s", acct.uid, type(e).__name__)
            removed = False
        await _audit(request, acct, "credentials.key.rotate", access_key,
                     newKey=new, old_key_removed=removed)
    out: dict = {"accessKey": new, "oldKeyRemoved": removed}
    if not removed:
        out["message"] = ("The new key was created, but the old key could not be removed. "
                          "Delete the old key again once the new one works.")
    return out


@router.delete("/keys/{access_key}")
async def delete_key(access_key: str, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    async with _key_locks(request)(acct.uid):
        own = _own(await _all_access_keys(request, acct.uid), acct.uid)
        if not any(k["access_key"] == access_key for k in own):
            raise HTTPException(404, "Access key not found.")
        if len(own) <= 1:
            raise HTTPException(409, "This is your only access key. Rotate it instead "
                                     "of deleting it.")
        try:
            await request.app.state.croit.delete_key(acct.uid, access_key)
        except CroitClientError as e:
            raise croit_http_error(e) from None
        await _audit(request, acct, "credentials.key.delete", access_key)
    return {"accessKey": access_key}


# -- one-time setup command -------------------------------------------------------

SETUP_TTL = 15 * 60  # seconds a setup link works
SETUP_LIMIT = 10  # links per user per SETUP_WINDOW
SETUP_WINDOW = 15 * 60
SETUP_KEEP = 24 * 3600  # rows older than this are deleted when a link is made
_SETUP_PATH = re.compile(r"/setup/[^\s?\"]+")


class RedactSetupToken(logging.Filter):
    """The token is in the request path, so the uvicorn access log must not print it."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact = lambda v: _SETUP_PATH.sub("/setup/[redacted]", v) if isinstance(v, str) else v  # noqa: E731
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) for a in record.args)
        return True


def install_access_log_filter() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSetupToken) for f in lg.filters):
        lg.addFilter(RedactSetupToken())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SetupBody(BaseModel):
    os: Literal["windows", "macos", "linux"]


@router.post("/setup-link")
async def setup_link(body: SetupBody, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    state = request.app.state
    db, now = state.db, state.clock()
    if not await state.endpoints.resolve(acct.uid):
        raise HTTPException(409, "The S3 endpoint is not configured. Ask an administrator.")
    keys = [k for k in await _own_keys(request, acct.uid) if k.get("secret_key")]
    if not keys:
        raise HTTPException(409, "You have no access key yet.")
    recent = await db.run(db.setup_tokens.count_since, acct.username, _iso(now - SETUP_WINDOW))
    if recent >= SETUP_LIMIT:
        raise HTTPException(429, "Too many setup commands. Wait a few minutes and try again.")
    token = secrets.token_urlsafe(32)
    access = keys[0]["access_key"]
    expires = now + SETUP_TTL
    row_id = await db.run(db.setup_tokens.create, _hash(token), acct.username, acct.uid, access,
                          body.os, _iso(now), _iso(expires), _iso(now - SETUP_KEEP))
    await db.run(db.audit.add, acct.username, "credentials.setup_link", "access_key", access,
                 {"uid": acct.uid, "os": body.os, "setupId": row_id, "expiresAt": _iso(expires)})
    url = f"{request.base_url}setup/{token}"
    return {"url": url, "command": command(body.os, url), "expiresAt": _iso(expires),
            "expiresIn": SETUP_TTL, "os": body.os}


setup_router = APIRouter(tags=["setup"])  # mounted at the app root: no session, no CSRF header


def _script_response(text: str) -> PlainTextResponse:
    # Always 200: `curl -f` drops 4xx bodies and `irm` throws on them, so the user would never
    # see the message. The real reason is only in the audit log.
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


@setup_router.post("/setup/{token}")
async def run_setup(token: str, request: Request):
    state = request.app.state
    db, settings = state.db, state.settings
    row = await db.run(db.setup_tokens.by_hash, _hash(token))
    if row is None:
        return _script_response("echo 'Unknown setup link. Make a new one in Bucketeer.'\n")
    os_name, uid, ad = row["os"], row["rgw_uid"], row["ad_username"]
    ip = request.client.host if request.client else "unknown"
    gone = _script_response(error_script(
        os_name,
        "This setup link was already used or has expired. Make a new one in Bucketeer."))
    now = _iso(state.clock())
    if not await db.run(db.setup_tokens.claim, row["id"], now, ip):
        return gone
    info = {"uid": uid, "os": os_name, "setupId": row["id"]}

    async def refuse(reason: str, message: str):
        await db.run(db.audit.add, ad, "credentials.setup_refused", "access_key", row["access_key"],
                     {**info, "reason": reason, "ip": ip})
        return _script_response(error_script(os_name, message))

    try:
        user = await state.croit.get_user(uid)
        keys = [k for k in await state.croit.list_keys(uid) or []
                if isinstance(k, dict) and k.get("access_key") and k.get("user") == uid
                ] if user else []
        endpoint = await state.endpoints.resolve(uid)
    except Exception:  # any admin trouble: refuse with a script, never a JSON error
        return await refuse("admin_unavailable", "Bucketeer could not reach the storage "
                            "service. Make a new setup command in Bucketeer and try again.")
    if not user:
        return await refuse("no_account", "Your S3 account was not found.")
    if user.get("suspended"):
        return await refuse("suspended",
                            "Your S3 account is suspended. Contact your administrator.")
    key = next((k for k in keys
                if k["access_key"] == row["access_key"] and k.get("secret_key")), None)
    if key is None:
        return await refuse("key_gone", "This key was rotated or deleted. Make a new setup "
                            "command in Bucketeer.")
    if not endpoint:
        return await refuse("no_endpoint",
                            "The S3 endpoint is not configured. Ask an administrator.")
    values = SetupValues(settings.aws_profile_name, settings.s3_region, endpoint,
                         settings.s3_addressing_style, key["access_key"], key["secret_key"])
    if not values.valid():
        return await refuse("unsafe_value", "This key cannot be installed automatically.")
    # Audit first: if the log cannot be written, the secret is not handed out.
    await db.run(db.audit.add, ad, "credentials.setup", "access_key", row["access_key"],
                 {**info, "ip": ip})
    return _script_response(script(os_name, values))
