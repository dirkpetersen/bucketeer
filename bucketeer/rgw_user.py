"""RGW user resolution: the session's sAMAccountName -> the user's RGW uid.

The uid is decided here, on the server, from the AD username in the signed
session (never from request input, never from an email address). Order:

1. An admin uid mapping for the AD username (case-insensitive) wins. The target
   must already exist (`mapping_target_missing` otherwise: mappings link existing users).
2. Otherwise the candidate is the lowercased sAMAccountName, qualified with
   `S3_TENANT` (`tenant$uid`) when set. An existing RGW user that matches the
   candidate (same tenant exactly, bare uid case-insensitively) is used as it is
   (exact spelling, audit `rgw.user.case_match`); two such users are ambiguous
   and refused rather than guessed. A uid that is the target of an admin mapping
   or a bucket key's RGW user is never given out by this rule (`uid_taken`,
   audit): only the mapping reaches it.
3. Otherwise the user is created in croit (name and email from AD) and gets the
   default user quota: the admin Settings value, else 1 TiB (Q4). Audited.

Statuses (`RgwAccount.status`): `ready`, `suspended` (resolved; callers must
refuse to act), `creation_failed` (`reason`: `email_conflict` (Q5 placeholder:
no retry without email), `invalid_username`, `ambiguous_uid`, `uid_taken`,
`mapping_target_missing`; shown as "contact your administrator") and
`unavailable` (croit trouble; never cached).

Later endpoints take the account from `Depends(require_rgw_user)` (or
`require_active_rgw_user`, which also refuses suspended users) and use only
`account.uid`; the uid is never read from the request.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .auth import SessionUser, require_user
from .croit import CroitClient, CroitClientError, CroitError, CroitUnavailable
from .db import Database
from .policy_settings import load_default_quota, quota_croit_args

log = logging.getLogger("bucketeer.rgw_user")

CACHE_TTL = 300.0  # seconds a resolution is remembered (never "unavailable")
DEFAULT_QUOTA = {"enabled": True, "maxSize": 2**40, "maxObjects": None}  # 1 TiB, Q4
WARM_TIMEOUT = 30.0  # background resolve right after login

_UID = re.compile(r"^[a-z0-9][a-z0-9._@+-]{0,63}\Z")  # same alphabet as validate_rgw_uid

READY, SUSPENDED = "ready", "suspended"
CREATION_FAILED, UNAVAILABLE = "creation_failed", "unavailable"

MESSAGES = {
    "email_conflict": "Your S3 account couldn't be created, contact your administrator.",
    "invalid_username": "Your S3 account couldn't be created, contact your administrator.",
    "ambiguous_uid": "Your S3 account couldn't be created, contact your administrator.",
    "uid_taken": "Your S3 account couldn't be created, contact your administrator.",
    "mapping_target_missing": "Your S3 account couldn't be created, contact your administrator.",
}
UNCACHED_REASONS = {"mapping_target_missing"}  # an admin fixes it in croit; re-check at once
MSG_UNAVAILABLE = "The storage service is temporarily unavailable. Try again shortly."


@dataclass(frozen=True)
class RgwAccount:
    username: str  # AD sAMAccountName from the session
    uid: str | None  # RGW uid (`tenant$uid` with a tenant); None unless resolved
    status: str
    display_name: str = ""
    quota: dict[str, Any] | None = None
    reason: str | None = None
    created: bool = False  # created by this resolution
    source: str = ""  # mapping | existing | case_match | created

    @property
    def resolved(self) -> bool:
        return self.status in (READY, SUSPENDED)

    def public(self) -> dict[str, Any]:
        """For the browser: no keys, and no uid unless it is resolved."""
        message = None
        if self.status == CREATION_FAILED:
            message = MESSAGES.get(self.reason or "", MESSAGES["email_conflict"])
        elif self.status == UNAVAILABLE:
            message = MSG_UNAVAILABLE
        return {
            "uid": self.uid if self.resolved else None,
            "status": self.status,
            "reason": self.reason,
            "message": message,
            "displayName": self.display_name,
            "quota": self.quota,
            "created": self.created,
        }


def quota_summary(q: dict | None) -> dict[str, Any]:
    q = q or {}

    def bound(v: Any) -> int | None:
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
        return int(v) if ok else None

    return {
        "enabled": bool(q.get("enabled")),
        "maxSize": bound(q.get("maxSize")),
        "maxObjects": bound(q.get("maxObjects")),
    }


def split_uid(uid: str) -> tuple[str, str]:
    """`tenant$uid` -> (tenant, uid); no `$` -> ("", uid)."""
    tenant, sep, bare = uid.rpartition("$")
    return (tenant, bare) if sep else ("", uid)


def same_uid(a: str, b: str) -> bool:
    """Tenant compared exactly, the bare uid case-insensitively."""
    (ta, ba), (tb, bb) = split_uid(a), split_uid(b)
    return ta == tb and ba.lower() == bb.lower()


def uid_claimed(db: Database, uid: str) -> bool:
    """Is `uid` somebody's by arrangement: the target of an admin uid mapping, or
    the RGW user of a bucket key (revoked rows too)? Such a uid must never be
    handed to an AD user by the default rule. Sync (DB only)."""
    return any(same_uid(m["rgw_uid"], uid) for m in db.uid_mappings.list()) or any(
        same_uid(k["rgw_uid"], uid) for k in db.bucket_keys.list(include_revoked=True)
    )


def ad_names_for_uid(db: Database, uid: str, tenant: str) -> list[str]:
    """AD usernames whose resolution leads to `uid`: those mapped to it, else
    the default name (the uid's bare part) when the uid is not claimed by a
    mapping or a bucket key, the name has no mapping elsewhere and the tenant
    fits. Sync (DB only); the admin side uses it for bucket-key owners, since
    the owner column holds AD names, not uids."""
    t, bare = split_uid(uid)
    names = [m["ad_username"] for m in db.uid_mappings.list() if same_uid(m["rgw_uid"], uid)]
    free = not uid_claimed(db, uid) and db.uid_mappings.get(bare) is None
    if t == tenant and free:
        names.append(bare)
    return names


class RgwResolver:
    warm_on_login = True  # tests switch it off at class level

    def __init__(
        self,
        croit: CroitClient,
        db: Callable[[], Database],
        tenant: str = "",
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_TTL,
    ):
        self.croit, self._db, self.tenant, self.clock, self.ttl = croit, db, tenant, clock, ttl
        self._cache: dict[str, tuple[float, RgwAccount]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._quiet: dict[tuple[str, str], float] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- cache -------------------------------------------------------------

    def invalidate(self, username: str | None = None, uid: str | None = None) -> None:
        """Forget a user (admin mapping change) or whoever resolved to `uid`
        (admin suspend/unsuspend). No arguments: everything."""
        if username is None and uid is None:
            self._cache.clear()
            return
        for key, (_, acct) in list(self._cache.items()):
            if (username and key == username.lower()) or (
                uid and acct.uid and acct.uid.lower() == uid.lower()
            ):
                del self._cache[key]

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def warm(self, user: SessionUser | Any) -> None:
        """Best-effort resolution after login; login never waits for or fails on it."""
        if not self.warm_on_login:
            return

        async def run() -> None:
            try:
                await asyncio.wait_for(
                    self.resolve(user.username, user.display_name, user.email), WARM_TIMEOUT
                )
            except Exception as e:  # noqa: BLE001 - best effort
                log.warning("post-login RGW resolution for %s failed: %r", user.username, e)

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- resolution --------------------------------------------------------

    async def resolve(self, username: str, display_name: str = "", email: str = "") -> RgwAccount:
        key = username.lower()
        if (hit := self._cached(key)) is not None:
            return hit
        async with self._locks.setdefault(key, asyncio.Lock()):
            if (hit := self._cached(key)) is not None:  # resolved while we waited
                return hit
            try:
                acct = await self._resolve(username, display_name, email)
            except CroitClientError as e:
                log.warning("RGW resolution for %s: croit unavailable (%s)", username, e)
                return RgwAccount(username, None, UNAVAILABLE, display_name)
            if acct.status != UNAVAILABLE and acct.reason not in UNCACHED_REASONS:
                self._cache[key] = (self.clock() + self.ttl, acct)
            return acct

    def _cached(self, key: str) -> RgwAccount | None:
        hit = self._cache.get(key)
        if hit and hit[0] > self.clock():
            return hit[1]
        self._cache.pop(key, None)
        return None

    async def _audit(self, actor: str, action: str, target: str, details: dict) -> None:
        db = self._db()
        try:
            await db.run(db.audit.add, actor, action, "rgw_user", target, details)
        except Exception:
            log.exception("audit write failed for %s", action)

    async def _audit_quiet(self, actor: str, action: str, target: str, details: dict) -> None:
        """For results that are not cached (so they repeat on every request): at most
        one audit row per user and action per `ttl`."""
        key, now = (actor.lower(), action), self.clock()
        if self._quiet.get(key, 0) > now:
            return
        self._quiet[key] = now + self.ttl
        await self._audit(actor, action, target, details)

    async def _get(self, uid: str) -> dict | None:
        return await self.croit.get_user(uid)

    async def _resolve(self, username: str, display_name: str, email: str) -> RgwAccount:
        db = self._db()
        mapping = await db.run(db.uid_mappings.get, username)
        if mapping:
            uid, source = mapping["rgw_uid"], "mapping"
            if await self._get(uid) is None:
                await self._audit_quiet(
                    username, "rgw.user.mapping_target_missing", uid, {"mapped_to": uid}
                )
                return RgwAccount(username, None, CREATION_FAILED, display_name,
                                  reason="mapping_target_missing")
        else:
            bare = username.lower()
            if not _UID.match(bare):
                await self._audit(
                    username, "rgw.user.invalid_username", username,
                    {"reason": "sAMAccountName cannot form a valid RGW uid"},
                )
                return RgwAccount(username, None, CREATION_FAILED, display_name,
                                  reason="invalid_username")
            candidate = f"{self.tenant}${bare}" if self.tenant else bare
            names = await self.croit.list_user_names()
            case_match = False
            if candidate in names:
                uid, source = candidate, "existing"
            else:
                same = [
                    n for n in names
                    if split_uid(n)[0] == self.tenant and split_uid(n)[1].lower() == bare
                ]
                if len(same) > 1:
                    await self._audit(
                        username, "rgw.user.ambiguous_uid", candidate,
                        {"candidates": sorted(same)},
                    )
                    return RgwAccount(username, None, CREATION_FAILED, display_name,
                                      reason="ambiguous_uid")
                if same:
                    uid, source, case_match = same[0], "case_match", True
                else:
                    uid, source = candidate, "derived"
            if await db.run(uid_claimed, db, uid):
                await self._audit(
                    username, "rgw.user.uid_taken", uid,
                    {"candidate": candidate, "reason": "mapping target or bucket-key user"},
                )
                return RgwAccount(username, None, CREATION_FAILED, display_name,
                                  reason="uid_taken")
            if case_match:
                await self._audit(
                    username, "rgw.user.case_match", uid, {"candidate": candidate, "matched": uid}
                )

        user = await self._get(uid)
        created = False
        if user is None:
            user = await self._create(username, uid, display_name, email)
            if isinstance(user, RgwAccount):  # a refusal
                return user
            created = True
            source = "created" if source == "existing" else source
        elif await self._quota_pending(uid):
            await self._apply_default_quota(username, uid, user_created=False)
            user = await self._get(uid) or user
        return RgwAccount(
            username, uid, SUSPENDED if user.get("suspended") else READY,
            display_name or str(user.get("name") or ""),
            quota_summary(user.get("userQuota")), created=created, source=source,
        )

    async def _create(
        self, username: str, uid: str, display_name: str, email: str
    ) -> dict | RgwAccount:
        tenant, bare = split_uid(uid)
        name = display_name or username
        try:
            await self.croit.create_user(bare, name, email or None, tenant or None)
        except CroitUnavailable:
            raise
        except CroitError as e:
            if e.status not in (400, 409, 422):
                raise
            # Lost a race with another creator, or croit refused the request.
            if uid in await self.croit.list_user_names():
                return await self._get(uid) or {"uid": uid}
            if "email" in f"{e.message} {e.details or ''}".lower():
                await self._audit(
                    username, "rgw.user.create_failed", uid,
                    {"reason": "email_conflict", "email": email, "croit_status": e.status},
                )
                return RgwAccount(username, None, CREATION_FAILED, display_name,
                                  reason="email_conflict")
            raise
        await self._apply_default_quota(username, uid, user_created=True)
        return await self._get(uid) or {"uid": uid, "name": name}

    async def _quota_pending(self, uid: str) -> bool:
        """Durable retry: the newest quota-related audit entry for this uid says the
        default quota was not applied (croit failed after the create)."""
        db = self._db()
        row = await db.run(
            db.query_one,
            "SELECT details FROM audit_log WHERE target_type = 'rgw_user' AND target = ? "
            "AND action IN ('rgw.user.create', 'rgw.user.default_quota') "
            "ORDER BY id DESC LIMIT 1",
            (uid,),
        )
        return bool(row) and json.loads(row["details"]).get("quota_applied") is False

    async def _apply_default_quota(self, username: str, uid: str, *, user_created: bool) -> None:
        db = self._db()
        quota = await db.run(load_default_quota, db.settings) or DEFAULT_QUOTA
        try:
            await self.croit.set_user_quota(uid, **quota_croit_args(quota))
        except CroitClientError:
            await self._audit(
                username, "rgw.user.create" if user_created else "rgw.user.default_quota",
                uid, {"quota": quota, "quota_applied": False},
            )
            raise
        await self._audit(
            username, "rgw.user.create" if user_created else "rgw.user.default_quota", uid,
            {"quota": quota, "quota_applied": True},
        )


# -- dependencies and routes ----------------------------------------------------


def get_resolver(request: Request) -> RgwResolver:
    return request.app.state.rgw


Resolver = Annotated[RgwResolver, Depends(get_resolver)]


async def current_account(
    user: Annotated[SessionUser, Depends(require_user)], resolver: Resolver
) -> RgwAccount:
    """The session user's account in whatever state it is in (never raises)."""
    return await resolver.resolve(user.username, user.display_name, user.email)


async def require_rgw_user(
    account: Annotated[RgwAccount, Depends(current_account)],
) -> RgwAccount:
    """For endpoints acting on the user's RGW account. The uid comes only from
    here. `status` can still be `suspended`: use `require_active_rgw_user` to refuse."""
    if account.status == UNAVAILABLE:
        raise HTTPException(503, MSG_UNAVAILABLE)
    if account.status == CREATION_FAILED:
        raise HTTPException(409, account.public()["message"])
    return account


async def require_active_rgw_user(
    account: Annotated[RgwAccount, Depends(require_rgw_user)],
) -> RgwAccount:
    if account.status == SUSPENDED:
        raise HTTPException(403, "Your S3 account is suspended. Contact your administrator.")
    return account


router = APIRouter(tags=["account"])  # mounted under /api in main.create_app


@router.get("/account")
async def account(
    response: Response, acct: Annotated[RgwAccount, Depends(current_account)]
):
    response.headers["Cache-Control"] = "no-store"
    return acct.public()
