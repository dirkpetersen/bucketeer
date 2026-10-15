"""Admin API under /api/admin (mounted in `main.create_app`, so CSRF applies).

Every route depends on `auth.require_admin` (401 anonymous, 403 non-admin). The
admin acts through the croit service identity; the actor in the audit log comes
from the session. Responses never contain S3 keys or secrets.
"""

import asyncio
import csv
import io
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from .auth import SessionUser, require_admin
from .budget import budget_matches, load_budget_rules
from .croit import CroitClient, CroitClientError, CroitError, CroitUnavailable, get_croit
from .db import Database, get_db
from .rgw_user import RgwResolver, ad_names_for_uid, get_resolver

log = logging.getLogger("bucketeer.admin")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

Admin = Annotated[SessionUser, Depends(require_admin)]
Croit = Annotated[CroitClient, Depends(get_croit)]
Db = Annotated[Database, Depends(get_db)]
Rgw = Annotated[RgwResolver, Depends(get_resolver)]


def get_suspend_lock(request: Request) -> asyncio.Lock:
    """The one lock for suspend/unsuspend of RGW users: admin actions, the offboarding
    job and its login unsuspend all hold it (`OffboardingService.lock`)."""
    return request.app.state.offboarding.lock


SuspendLock = Annotated[asyncio.Lock, Depends(get_suspend_lock)]

MAX_QUOTA = 2**62  # sane upper bound, below int64
_UID = re.compile(r"^[^\x00-\x1f\x7f/\\?#%]{1,128}$")  # tenant$uid allowed
_BUCKET = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")  # tenant:name allowed


# -- helpers ---------------------------------------------------------------------


def _uid(value: str) -> str:
    if not _UID.match(value) or value in (".", ".."):
        raise HTTPException(422, "Invalid user id.")
    return value


def _bucket_name(value: str) -> str:
    if not _BUCKET.match(value) or value in (".", ".."):
        raise HTTPException(422, "Invalid bucket name.")
    return value


def croit_http_error(e: CroitClientError) -> HTTPException:
    """A clean HTTP error for a croit failure (croit's own text is already
    scrubbed of the token by the client; no URLs or headers are passed on)."""
    if isinstance(e, CroitUnavailable):
        return HTTPException(503, "The storage backend (croit) is unreachable.")
    if isinstance(e, CroitError):
        if e.status == 404:
            return HTTPException(404, f"Not found: {e.message}")
        if e.status in (400, 409, 422):
            return HTTPException(400, f"croit refused the request: {e.message}")
        if e.status in (401, 403):
            return HTTPException(502, "croit rejected the portal's service credentials.")
    return HTTPException(502, "The storage backend (croit) returned an error.")


def _quota(q: dict | None, *, zero_unlimited: bool = False) -> dict[str, Any]:
    """croit's RgwQuotaResponse as {enabled, maxSize (bytes), maxObjects}; a
    negative bound (unlimited) becomes null. For bucket quotas croit treats a
    bound at or below zero as no bound, so `zero_unlimited` maps 0 to null too."""
    q = q or {}
    low = 1 if zero_unlimited else 0

    def bound(v: Any) -> int | None:
        return int(v) if isinstance(v, (int, float)) and v >= low else None

    return {
        "enabled": bool(q.get("enabled")),
        "maxSize": bound(q.get("maxSize")),
        "maxObjects": bound(q.get("maxObjects")),
    }


def _tenant_of(uid: str) -> str:
    return uid.split("$", 1)[0] if "$" in uid else ""


def _usage(b: dict) -> tuple[int, int]:
    u = b.get("usageSum") or {}
    return int(u.get("numObjects") or 0), int(u.get("size") or 0)


class QuotaBody(BaseModel):
    """`maxSize` in bytes, `maxObjects` a count; null means unlimited. A quota
    that is enabled needs at least one limit; `enabled=false` switches it off
    and leaves the stored limits as they were."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    maxSize: StrictInt | None = None
    maxObjects: StrictInt | None = None


def _quota_args(body: QuotaBody, *, bucket: bool = False) -> dict[str, Any]:
    low = 1 if bucket else 0
    for name in ("maxSize", "maxObjects"):
        v = getattr(body, name)
        if v is not None and not low <= v <= MAX_QUOTA:
            hint = " To block writes, suspend the owner." if bucket else ""
            raise HTTPException(
                422, f"{name} must be {low} or more (up to {MAX_QUOTA}), or null.{hint}"
            )
    if not body.enabled:
        return {"enabled": False}
    if body.maxSize is None and body.maxObjects is None:
        raise HTTPException(422, "An enabled quota needs a size or an object limit.")
    return {
        "enabled": True,
        "max_size": -1 if body.maxSize is None else body.maxSize,
        "max_objects": -1 if body.maxObjects is None else body.maxObjects,
    }


async def _audit(
    db: Database, admin: SessionUser, action: str, kind: str, target: str, details: dict
):
    await db.run(db.audit.add, admin.username, action, kind, target, details)


def _user_view(u: dict, bucket_count: int, key: dict | None) -> dict[str, Any]:
    uid = str(u["uid"])
    return {
        "uid": uid,
        "name": u.get("name") or "",
        "email": u.get("email") or "",
        "tenant": _tenant_of(uid),
        "suspended": bool(u.get("suspended")),
        "quota": _quota(u.get("userQuota")),
        "bucketQuota": _quota(u.get("bucketQuota")),
        "bucketCount": bucket_count,
        "bucketKey": key
        and {
            "owner": key["owner_ad_username"],
            "bucket": key["bucket_name"],
            "access": key["access"],
        },
    }


def _bucket_view(b: dict, meta: dict | None, matches: bool | None = None) -> dict[str, Any]:
    objects, size = _usage(b)
    return {
        "name": b.get("bucket") or "",
        "tenant": b.get("tenant") or "",
        "owner": b.get("owner") or "",
        "objects": objects,
        "bytes": size,
        "quota": _quota(b.get("bucket_quota"), zero_unlimited=True),
        "budgetNumber": (meta or {}).get("budget_number"),  # null = missing
        # does it match the current admin pattern? null = no number or no pattern
        "budgetMatches": matches,
    }


async def _fetch_user(croit: CroitClient, uid: str) -> dict:
    try:
        user = await croit.get_user(uid)
    except CroitClientError as e:
        raise croit_http_error(e) from e
    if user is None:
        raise HTTPException(404, f"Not found: User {uid} not found")
    return user


async def _fetch_bucket(croit: CroitClient, name: str) -> dict:
    try:
        buckets = await croit.list_buckets()
    except CroitClientError as e:
        raise croit_http_error(e) from e
    for b in buckets:
        if b.get("bucket") == name:
            return b
    raise HTTPException(404, "Bucket not found.")


# -- reads -----------------------------------------------------------------------


@router.get("/users")
async def list_users(_a: Admin, croit: Croit, db: Db):
    try:
        users, buckets = await asyncio.gather(croit.list_users(), croit.list_buckets())
    except CroitClientError as e:
        raise croit_http_error(e) from e
    keys = {k["rgw_uid"]: k for k in await db.run(db.bucket_keys.list)}
    counts: dict[str, int] = {}
    for b in buckets:
        counts[b.get("owner") or ""] = counts.get(b.get("owner") or "", 0) + 1
    out = [_user_view(u, counts.get(u["uid"], 0), keys.get(u["uid"])) for u in users]
    out.sort(key=lambda u: u["uid"].lower())
    return {"users": out}


async def _judged_buckets(croit: CroitClient, db: Database, *, with_users: bool = False):
    """(bucket views, metadata rows by name, croit users by uid or {}); each view
    carries `budgetMatches` against the current admin pattern."""
    try:
        if with_users:
            buckets, users = await asyncio.gather(croit.list_buckets(), croit.list_users())
        else:
            buckets, users = await croit.list_buckets(), []
    except CroitClientError as e:
        raise croit_http_error(e) from e
    meta = {m["name"]: m for m in await db.run(db.buckets.list)}

    pattern = (await db.run(load_budget_rules, db.settings))["pattern"]

    def build():
        rows, seen, timeouts = [], {}, 0
        for b in buckets:
            m = meta.get(b.get("bucket"))
            value = (m or {}).get("budget_number")
            if value not in seen:
                try:
                    seen[value] = None if timeouts >= 3 else budget_matches(value, pattern)
                except TimeoutError:
                    timeouts += 1  # after 3, the rest is unjudged (null)
                    seen[value] = None
            rows.append(_bucket_view(b, m, seen[value]))
        return rows

    out = await asyncio.to_thread(build)
    out.sort(key=lambda b: b["name"].lower())
    return out, meta, {str(u["uid"]): u for u in users}, pattern


@router.get("/buckets")
async def list_buckets(_a: Admin, croit: Croit, db: Db):
    out, _meta, _users, _pattern = await _judged_buckets(croit, db)
    return {"buckets": out}


# -- chargeback export (Q2) ----------------------------------------------------------

CHARGEBACK_COLUMNS = [
    "bucket", "tenant", "owner_uid", "owner_name", "budget_number", "budget_status",
    "objects", "bytes", "size_gib", "quota_bytes", "created_at",
]
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    """One CSV cell as text, neutralizing spreadsheet formulas: a cell starting
    with = + - @ tab or CR gets a leading apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def budget_status(number: str | None, matches: bool | None, pattern: str = "") -> str:
    """missing (no number) | ok | mismatch | unknown (pattern check timed out or
    was cut off). With no pattern set, any number is ok."""
    if not number:
        return "missing"
    if not pattern:
        return "ok"
    return "unknown" if matches is None else "ok" if matches else "mismatch"


@router.get("/export/chargeback.csv")
async def export_chargeback(admin: Admin, croit: Croit, db: Db):
    """One row per bucket for finance: owner, budget number and usage. UTF-8
    with a BOM, so Excel opens it with the right encoding."""
    views, meta, users, pattern = await _judged_buckets(croit, db, with_users=True)
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(CHARGEBACK_COLUMNS)
    for v in views:
        q = v["quota"]
        quota = q["maxSize"] if q["enabled"] else None
        created = (meta.get(v["name"]) or {}).get("created_at")
        number = v["budgetNumber"]
        w.writerow([csv_safe(c) for c in (
            v["name"], v["tenant"], v["owner"], (users.get(v["owner"]) or {}).get("name") or "",
            number, budget_status(number, v["budgetMatches"], pattern), v["objects"], v["bytes"],
            f"{v['bytes'] / 1024**3:.3f}", quota, created,
        )])
    await _audit(db, admin, "admin.export.chargeback", "report", "chargeback",
                 {"rows": len(views)})
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return Response(
        "\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="bucketeer-chargeback-{day}.csv"',
            "Cache-Control": "no-store",
        },
    )


# -- quotas ----------------------------------------------------------------------


@router.put("/users/{uid}/quota")
async def set_user_quota(uid: str, body: QuotaBody, admin: Admin, croit: Croit, db: Db):
    uid = _uid(uid)
    args = _quota_args(body)
    before = _quota((await _fetch_user(croit, uid)).get("userQuota"))
    try:
        await croit.set_user_quota(uid, **args)
    except CroitClientError as e:
        raise croit_http_error(e) from e
    try:
        after = _quota((await _fetch_user(croit, uid)).get("userQuota"))
    except HTTPException:
        # the write went through; only the read-back failed: still audit the change
        await _audit(db, admin, "admin.user.quota", "rgw_user", uid,
                     {"before": before, "after": None, "requested": args, "readback_failed": True})
        raise
    await _audit(db, admin, "admin.user.quota", "rgw_user", uid,
                 {"before": before, "after": after})
    return {"uid": uid, "quota": after}


@router.put("/buckets/{bucket}/quota")
async def set_bucket_quota(bucket: str, body: QuotaBody, admin: Admin, croit: Croit, db: Db):
    bucket = _bucket_name(bucket)
    args = _quota_args(body, bucket=True)
    before = _quota((await _fetch_bucket(croit, bucket)).get("bucket_quota"), zero_unlimited=True)
    try:
        await croit.set_bucket_quota(bucket, **args)
    except CroitClientError as e:
        raise croit_http_error(e) from e
    try:
        after = _quota((await _fetch_bucket(croit, bucket)).get("bucket_quota"),
                       zero_unlimited=True)
    except HTTPException:
        await _audit(db, admin, "admin.bucket.quota", "bucket", bucket,
                     {"before": before, "after": None, "requested": args,
                      "readback_failed": True})
        raise
    details = {"before": before, "after": after}
    await _audit(db, admin, "admin.bucket.quota", "bucket", bucket, details)
    return {"bucket": bucket, "quota": after}


# -- suspend / unsuspend -----------------------------------------------------------


async def apply_key_suspension(
    croit: CroitClient, db: Database, keys: list[dict], suspend: bool, by: str,
    owner_uid: str,
) -> tuple[list[str], list[str]]:
    """(Un)suspend bucket keys in croit and the table; `by` ('owner', 'direct' or
    'offboarding') is recorded on suspend. Shared by the admin routes and the offboarding
    job. Returns (done uids, failed uids); never raises for a croit failure."""
    done, failed = [], []
    for k in keys:
        try:
            if k["rgw_uid"].lower() != owner_uid.lower():
                try:
                    await croit.update_user(k["rgw_uid"], suspended=suspend)
                except CroitError as e:
                    # A pending/failed row whose user is already gone has nothing to suspend.
                    if not (e.status == 404 and k.get("state") in ("pending", "failed")):
                        raise
            await db.run(db.bucket_keys.set_suspended, k["id"], suspend, by)
            done.append(k["rgw_uid"])
        except CroitClientError as e:
            log.warning("bucket key %s (un)suspend failed: %s", k["rgw_uid"], e)
            failed.append(k["rgw_uid"])
    return done, failed


async def _set_suspended(
    uid: str, suspend: bool, admin: SessionUser, croit: CroitClient, db: Database,
    rgw: RgwResolver, lock: asyncio.Lock,
):
    # Serialized with the offboarding job and the login unsuspend, so none of them can undo
    # another's decision between its read and its write.
    async with lock:
        return await _set_suspended_locked(uid, suspend, admin, croit, db, rgw)


async def _set_suspended_locked(
    uid: str, suspend: bool, admin: SessionUser, croit: CroitClient, db: Database,
    rgw: RgwResolver,
):
    uid = _uid(uid)
    # Bucket-key owners are AD usernames, and the uid is not always one (mapping,
    # tenant prefix): go through the same resolution rules as login.
    owners = await db.run(ad_names_for_uid, db, uid, rgw.tenant)
    owner_set = {o.lower() for o in owners}
    if suspend and admin.username.lower() in owner_set:
        raise HTTPException(409, "You cannot suspend your own account.")
    user = await _fetch_user(croit, uid)
    was = bool(user.get("suspended"))
    try:
        await croit.update_user(uid, suspended=suspend)
    except CroitClientError as e:
        raise croit_http_error(e) from e
    rgw.invalidate(uid=uid)
    # An admin decision ends the offboarding job's claim on this account: a suspend
    # takes it over (never undone by a login), an unsuspend settles it.
    took_over = await db.run(db.known_accounts.clear_offboarding_for_uid, uid)

    # Bucket keys follow their owner (CLAUDE.md "Offboarding"), but only the ones
    # the owner cascade suspended ('owner'); a key suspended on its own ('direct')
    # stays suspended until it is unsuspended directly. Revoked keys never wake.
    # A bucket-key user suspended or unsuspended directly only moves its own row.
    own = await db.run(db.bucket_keys.get_by_uid, uid)
    is_key_user = (
        own is not None
        and own["revoked_at"] is None
        and own["owner_ad_username"].lower() not in owner_set
    )
    live = [
        k
        for owner in owners
        for k in await db.run(db.bucket_keys.list, owner)
    ]
    if is_key_user:
        keys, by = [own], "direct"
        keys = [k for k in keys if suspend or k["suspended_at"]]
    elif suspend:
        keys, by = [k for k in live if not k["suspended_at"]], "owner"
    elif was or took_over:
        # keys the offboarding job suspended wake with an admin unsuspend as well (also
        # when croit already says the user is awake: the keys must not stay stuck)
        keys = [k for k in live if k["suspended_by"] in ("owner", "offboarding")]
        by = "owner"
    else:
        keys, by = [], "owner"  # owner was not suspended: no cascade
    done, failed = await apply_key_suspension(croit, db, keys, suspend, by, uid)
    if took_over and suspend:
        # keys the job suspended now belong to the admin's suspension ('owner' wakes with it)
        for k in live:
            if k["suspended_by"] == "offboarding":
                await db.run(db.bucket_keys.set_suspended, k["id"], True, "owner")
    details: dict[str, Any] = {
        "before": {"suspended": was}, "after": {"suspended": suspend}, "keys": done,
    }
    if took_over:
        details["offboarding_cleared"] = took_over
    if failed:
        details["keys_failed"] = failed
    action = "admin.user.suspend" if suspend else "admin.user.unsuspend"
    await _audit(db, admin, action, "rgw_user", uid, details)
    if failed:
        raise HTTPException(
            502,
            f"{uid} is {'suspended' if suspend else 'unsuspended'}, but these bucket keys failed: "
            + ", ".join(failed),
        )
    return {"uid": uid, "suspended": suspend, "keys": done}


@router.post("/users/{uid}/suspend")
async def suspend_user(uid: str, admin: Admin, croit: Croit, db: Db, rgw: Rgw,
                       lock: SuspendLock):
    return await _set_suspended(uid, True, admin, croit, db, rgw, lock)


@router.post("/users/{uid}/unsuspend")
async def unsuspend_user(uid: str, admin: Admin, croit: Croit, db: Db, rgw: Rgw,
                         lock: SuspendLock):
    return await _set_suspended(uid, False, admin, croit, db, rgw, lock)


