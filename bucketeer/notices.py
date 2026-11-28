"""User notices (CLAUDE.md "User notices"): warn owners before an on-premises bucket is full,
and remind people of old access keys.

Settings key `notices` `{nearFullPercent: 50..100 (default 90), maxKeyAgeDays: 0..3650
(default 180, 0 = no key reminders)}`, admin only:
- `GET /api/admin/settings/notices` -> `{nearFullPercent, maxKeyAgeDays, defaults, limits:
  {field: [lo, hi]}, email: {enabled}, schedule: {time, lastRun: {at, counts, errors}|null,
  nextRun}}` (`nextRun` server local time `YYYY-MM-DDTHH:MM`).
- `PUT /api/admin/settings/notices {nearFullPercent?, maxKeyAgeDays?}` (partial) -> the GET
  shape; 422 `{detail, errors: [{field, message}]}`; audit `admin.settings.notices`
  (target_type `setting`, target `notices`, before/after) only when something changed.

Daily pass (`notice_loop`, a lifespan task, `NOTICES_ON_START`, off in tests; tests call
`notice_tick(app, now)` for one pass and `schedule_tick(app, now)` for the timing): once a day
at `NOTICE_TIME` (07:00 server local time), or at the first check after it when the server was
down then (catch-up); a pass that could not finish (admin client trouble) is retried an hour
later. The day it ran is the settings key `notices_state` (`lastRunDay`, `retryAfter`,
`lastRun`).

1. Near-full buckets. Every on-premises bucket from the admin client's `list_buckets()` whose
   size is at least `nearFullPercent` of its size quota (`near_full`: enabled with maxSize >
   0). Owners that are bucket-key users, the Admin Ops service user, the backup user or a
   suspended account are skipped. The owner (AD mail of the uid's AD names, else the RGW
   email: `billing_report.owner_emails`) gets `Mailer.send_near_full` at most once per
   `NEAR_FULL_EVERY` (7 days) per bucket: `buckets.over_quota_alert_at` of the bucket's
   on-premises row, or for a bucket without a row (made outside Bucketeer) `notices_state`
   `nearFull` {bucket: time}. A failed send is retried the next day; email off = audit only.
   Audit `bucket.near_full_notice` (actor `system`, target_type `bucket`, tenant-qualified
   name; uid, bytes, quotaBytes, percent, threshold, emailed, reason). `GET /api/buckets`
   shows the same test as `nearFull`.
2. Key age. RGW keys carry no creation date, so `key_ages` records one: when Bucketeer
   creates or rotates a key (`key_created`, exact) and, for a key never seen before, the first
   time a key list (`GET /api/credentials`, `GET /api/subusers`: bookkeeping only, nothing a
   person can see changes) or this pass sees it (`first_seen`, so the age is "at least");
   `key_removed` forgets keys Bucketeer deletes, the pass forgets keys that vanished. Cloud keys
   use IAM's CreateDate. Every own key and subuser key of a known person (`known_accounts`;
   bucket-key users, the service and backup users and suspended accounts are skipped), and
   every Active cloud key of a person with a cloud account (while cloud is available), at
   least `maxKeyAgeDays` old is named in one email per person and side (`Mailer.send_key_age`:
   the last 4 characters, the age, how to rotate) at most once per `KEY_REMINDER_EVERY` (30
   days) per key (`key_ages.reminded_at`; a failed send is retried the next day). Audit
   `credentials.key.age_notice` per key (actor `system`, target_type `access_key`; uid or AD
   name, kind, subuser, ageDays, ageExact, limitDays, emailed, reason).
   Key lists (`GET /api/credentials`, `/api/subusers`, `/api/cloud/credentials`) add per key
   `ageDays`, `ageKnownSince` (ISO time the age counts from; null = unknown), `ageExact`
   (false = "at least") and `rotateSoon`, plus `maxKeyAgeDays` (null when off) at the top.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import bucket_quota
from .admin import Admin, Db
from .auth import require_admin
from .billing_report import owner_emails
from .cloud import CloudError
from .croit import CroitClientError, is_backup_uid
from .db import Database
from .rgw_user import is_service_uid, split_uid

log = logging.getLogger("bucketeer.notices")

KEY = "notices"
STATE_KEY = "notices_state"
DEFAULTS = {"nearFullPercent": 90, "maxKeyAgeDays": 180}
LIMITS = {"nearFullPercent": (50, 100), "maxKeyAgeDays": (0, 3650)}
MESSAGES = {
    "nearFullPercent": "Enter a whole number from 50 to 100 (percent of the bucket quota).",
    "maxKeyAgeDays": "Enter a whole number of days from 0 (no reminders) to 3650.",
}
NOTICES_ON_START = True
NOTICE_TIME = (7, 0)  # server local time
FIRST_DELAY = 300.0  # after start: the cloud connection check has run by then
TICK_SECONDS = 60.0
RETRY = timedelta(hours=1)
NEAR_FULL_EVERY = timedelta(days=7)
KEY_REMINDER_EVERY = timedelta(days=30)
DAY = 86400

iso = bucket_quota.iso


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


# -- settings ----------------------------------------------------------------------------


def valid(field: str, value: Any) -> bool:
    lo, hi = LIMITS[field]
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def load_settings(store) -> dict[str, int]:
    """The stored settings over the defaults (a bad stored value falls back). Sync (DB)."""
    raw = store.get(KEY, {})
    raw = raw if isinstance(raw, dict) else {}
    return {k: raw[k] if valid(k, raw.get(k)) else d for k, d in DEFAULTS.items()}


def max_key_age(store) -> int | None:
    """The key age limit in days; None when reminders are off."""
    return load_settings(store)["maxKeyAgeDays"] or None


def _state(db: Database) -> dict[str, Any]:
    s = db.settings.get(STATE_KEY, {})
    return s if isinstance(s, dict) else {}


def _update_state(db: Database, **fields: Any) -> None:
    with db.tx():  # read-modify-write of one settings row
        db.settings.set(STATE_KEY, {**_state(db), **fields}, "system")


# -- buckets -----------------------------------------------------------------------------


def size_limit(quota: dict | None) -> int | None:
    """A bucket quota's size limit in bytes; None without one."""
    q = quota or {}
    size = q.get("maxSize")
    if not q.get("enabled") or isinstance(size, bool) or not isinstance(size, int | float) \
            or size <= 0:
        return None
    return int(size)


def usage_bytes(bucket: dict) -> int:
    u = bucket.get("usageSum") or {}
    return int(u.get("size") or 0)


def near_full(bucket: dict, percent: int) -> bool:
    """Is the admin listing's bucket at `percent` % of its size quota or more?"""
    limit = size_limit(bucket.get("bucket_quota"))
    return limit is not None and usage_bytes(bucket) * 100 >= percent * limit


# -- key ages ----------------------------------------------------------------------------


def age_fields(since: str | None, exact: bool, now: datetime, limit: int | None) -> dict:
    """`{ageDays, ageKnownSince, ageExact, rotateSoon}` of a key known since `since`."""
    try:
        days = max(0, int((now - _parse(since)).total_seconds() // DAY)) if since else None
    except ValueError:
        days = None
    if days is None:
        return {"ageDays": None, "ageKnownSince": None, "ageExact": False, "rotateSoon": False}
    return {"ageDays": days, "ageKnownSince": since, "ageExact": exact,
            "rotateSoon": bool(limit) and days >= limit}


async def key_ages(app, uid: str, items: list[tuple[str, str]]
                   ) -> tuple[dict[str, dict], int | None]:
    """For a key list answer: `items` = [(access key, 'user'|'subuser')] of `uid` -> (access
    key -> age fields, the limit or None). Keys never seen before are recorded now; trouble
    with that only leaves the age unknown."""
    db: Database = app.state.db
    now = bucket_quota.now_of(app)
    limit = await db.run(max_key_age, db.settings)
    try:
        rows = await db.run(db.key_ages.seen, [(a, uid, kind) for a, kind in items], iso(now))
    except Exception:  # noqa: BLE001  never fail a key list for this
        log.exception("key ages of %s not recorded", uid)
        rows = {}
    return ({a: age_fields((rows.get(a) or {}).get("first_seen"),
                           bool((rows.get(a) or {}).get("created")), now, limit)
             for a, _ in items}, limit)


async def cloud_key_ages(app, keys: list[dict]) -> tuple[list[dict], int | None]:
    """Cloud keys `[{accessKey, status, createdAt}]` with their age fields (IAM's CreateDate
    is exact; an Inactive key is never "rotate soon")."""
    db: Database = app.state.db
    now = bucket_quota.now_of(app)
    limit = await db.run(max_key_age, db.settings)
    out = []
    for k in keys:
        f = age_fields(k.get("createdAt"), True, now, limit)
        f["rotateSoon"] = f["rotateSoon"] and k.get("status", "Active") == "Active"
        out.append({**k, **f})
    return out, limit


async def key_created(app, access_key: str, uid: str, kind: str) -> None:
    """Bucketeer made this key now (own key or subuser key). Never raises."""
    db: Database = app.state.db
    try:
        await db.run(db.key_ages.created, access_key, uid, kind,
                     iso(bucket_quota.now_of(app)))
    except Exception:  # noqa: BLE001  the age just becomes "at least" later
        log.exception("age of the new key of %s not recorded", uid)


async def key_removed(app, *access_keys: str) -> None:
    """Bucketeer deleted these keys. Never raises."""
    db: Database = app.state.db
    try:
        await db.run(db.key_ages.remove, *access_keys)
    except Exception:  # noqa: BLE001  the daily pass forgets vanished keys anyway
        log.exception("ages of deleted keys not forgotten")


# -- the daily pass ----------------------------------------------------------------------


def _norm(uid: str) -> tuple[str, str]:
    tenant, bare = split_uid(uid)
    return tenant, bare.lower()


def _key_uids(db: Database) -> set[tuple[str, str]]:
    """Bucket key users (revoked ones too): never people."""
    return {_norm(k["rgw_uid"]) for k in db.bucket_keys.list(include_revoked=True)}


async def _not_a_person(app, uid: str, key_uids: set) -> bool:
    return (_norm(uid) in key_uids or is_backup_uid(uid)
            or await is_service_uid(app.state.croit, uid))


async def _deliver(app, address: str | None, send: Callable[[str], Awaitable[Any]]
                   ) -> tuple[bool | None, str | None]:
    """(emailed, reason): (None, email_off), (False, no_address|send_failed), (True, None)."""
    if not app.state.mailer.enabled:
        return None, "email_off"
    if not address:
        return False, "no_address"
    return (True, None) if await send(address) else (False, "send_failed")


def _recent(ts: str | None, now: datetime, every: timedelta) -> bool:
    try:
        return bool(ts) and now - _parse(ts) < every
    except ValueError:
        return False


async def _near_full_pass(app, now: datetime, percent: int, counts: dict) -> None:
    db: Database = app.state.db
    croit = app.state.croit
    buckets = await croit.list_buckets()
    key_uids = await db.run(_key_uids, db)
    fallback = dict((await db.run(_state, db)).get("nearFull") or {})
    due: list[tuple[dict, dict | None]] = []
    for b in buckets or []:
        full, owner = (b.get("bucket") or "", b.get("owner") or "") if isinstance(b, dict) \
            else ("", "")
        if not full or not owner:
            continue
        counts["buckets"] += 1
        if not near_full(b, percent):
            continue
        if await _not_a_person(app, owner, key_uids):
            counts["skipped"] += 1
            continue
        counts["nearFull"] += 1
        row = await db.run(db.buckets.get, full, "onprem")
        last = row.get("over_quota_alert_at") if row else fallback.get(full)
        if not _recent(last, now, NEAR_FULL_EVERY):
            due.append((b, row))
    active: list[tuple[dict, dict | None]] = []
    for b, row in due:
        try:
            user = await croit.get_user(b["owner"])
        except CroitClientError as e:
            log.warning("near-full: owner of %s unreadable: %s", b["bucket"], type(e).__name__)
            counts["failed"] += 1
            continue
        if user is None or user.get("suspended"):
            counts["skipped"] += 1
            continue
        active.append((b, row))
    emails = (await owner_emails(app, {b["owner"] for b, _ in active})
              if active and app.state.mailer.enabled else {})
    for b, row in active:
        full, owner, size = b["bucket"], b["owner"], usage_bytes(b)
        limit = size_limit(b.get("bucket_quota")) or 1
        pct = int(size * 100 // limit)
        short = full.partition(":")[2] or full
        sent, reason = await _deliver(app, emails.get(owner), lambda to, short=short, size=size,
                                      limit=limit, pct=pct: app.state.mailer.send_near_full(
                                          to, short, size, limit, pct))
        if reason != "send_failed":  # a failed send is retried tomorrow; else in a week
            if row is not None:
                await db.run(db.buckets.set_alerted, full, iso(now), "onprem")
            else:
                fallback[full] = iso(now)
            counts["bucketNotices"] += 1
        await db.run(db.audit.add, "system", "bucket.near_full_notice", "bucket", full,
                     {"uid": owner, "bytes": size, "quotaBytes": limit, "percent": pct,
                      "threshold": percent, "emailed": sent, "reason": reason})
    kept = {k: v for k, v in fallback.items() if _recent(v, now, NEAR_FULL_EVERY)}
    await db.run(_update_state, db, nearFull=kept)


def _mail_key(k: dict) -> dict:
    return {"last4": k["accessKey"][-4:], "ageDays": k["ageDays"], "exact": k["ageExact"],
            "subuser": k.get("subuser")}


async def _remind(app, now: datetime, limit: int, due: dict[str, list[dict]],
                  emails: dict[str, str | None], cloud: bool, counts: dict) -> None:
    """One email per person (`due` key: uid, or AD name for cloud keys), audit per key."""
    db: Database = app.state.db
    for who, keys in due.items():
        sent, reason = await _deliver(app, emails.get(who), lambda to, keys=keys:
                                      app.state.mailer.send_key_age(
                                          to, [_mail_key(k) for k in keys], limit, cloud))
        for k in keys:
            if reason != "send_failed":
                await db.run(db.key_ages.set_reminded, k["accessKey"], iso(now))
                counts["keyNotices"] += 1
            details = {"kind": k["kind"], "ageDays": k["ageDays"], "ageExact": k["ageExact"],
                       "limitDays": limit, "emailed": sent, "reason": reason}
            details.update({"adUsername": who} if cloud else {"uid": who})
            if k.get("subuser"):
                details["subuser"] = k["subuser"]
            await db.run(db.audit.add, "system", "credentials.key.age_notice", "access_key",
                         k["accessKey"], details)


def _due_key(row: dict | None, access: str, kind: str, now: datetime, limit: int | None,
             counts: dict, **extra: Any) -> dict | None:
    if not row or not limit:
        return None
    f = age_fields(row.get("first_seen"), bool(row.get("created")), now, limit)
    if not f["rotateSoon"]:
        return None
    counts["oldKeys"] += 1
    if _recent(row.get("reminded_at"), now, KEY_REMINDER_EVERY):
        return None
    return {"accessKey": access, "kind": kind, **f, **extra}


async def _key_pass(app, now: datetime, limit: int | None, counts: dict) -> None:
    db: Database = app.state.db
    croit = app.state.croit
    by_uid: dict[str, list[str]] = {}
    for r in await db.run(db.known_accounts.list):
        by_uid.setdefault(r["rgw_uid"], []).append(r["ad_username"])
    key_uids = await db.run(_key_uids, db)
    due: dict[str, list[dict]] = {}
    for uid in sorted(by_uid):
        if await _not_a_person(app, uid, key_uids):
            continue
        try:
            user = await croit.get_user(uid)
            if user is None or user.get("suspended"):
                counts["skipped"] += 1
                continue
            keys = await croit.list_keys(uid)
        except CroitClientError as e:
            log.warning("key ages: keys of %s unreadable: %s", uid, type(e).__name__)
            counts["failed"] += 1
            continue
        items: list[tuple[str, str, str | None]] = []  # (access key, kind, subuser)
        for k in keys or []:
            owner = k.get("user") if isinstance(k, dict) and k.get("access_key") else None
            if owner == uid:
                items.append((k["access_key"], "user", None))
            elif isinstance(owner, str) and owner.startswith(f"{uid}:"):
                items.append((k["access_key"], "subuser", owner[len(uid) + 1:]))
        rows = await db.run(db.key_ages.seen, [(a, uid, kind) for a, kind, _ in items],
                            iso(now))
        await db.run(db.key_ages.prune, uid, ("user", "subuser"), {a for a, _, _ in items})
        counts["keys"] += len(items)
        for access, kind, sub in items:
            k = _due_key(rows.get(access), access, kind, now, limit, counts, subuser=sub)
            if k:
                due.setdefault(uid, []).append(k)
    if due:
        emails = await owner_emails(app, set(due)) if app.state.mailer.enabled else {}
        await _remind(app, now, limit or 0, due, emails, False, counts)


async def _ad_mail(app, name: str) -> str | None:
    try:
        user = await app.state.authenticator.lookup_user(name)
    except Exception:  # noqa: BLE001  left AD, LDAP down: no mail this time
        return None
    return user.email.strip() if user.email and "@" in user.email else None


async def _cloud_key_pass(app, now: datetime, limit: int | None, counts: dict) -> None:
    cloud = getattr(app.state, "cloud", None)
    if cloud is None or not cloud.available:
        return
    db: Database = app.state.db
    due: dict[str, list[dict]] = {}
    for acc in await db.run(db.cloud_accounts.list):
        ad, iam = acc["ad_username"], acc["iam_user"]
        try:
            keys = await cloud.list_access_keys(iam)
        except CloudError as e:
            if e.code != "NoSuchEntity":
                log.warning("key ages: cloud keys of %s unreadable: %s", ad, e.code)
                counts["failed"] += 1
            continue
        active = [k for k in keys if k.get("status") == "Active" and k.get("createdAt")]
        rows = await db.run(db.key_ages.cloud, ad,
                            [(k["accessKey"], k["createdAt"]) for k in active])
        await db.run(db.key_ages.prune, ad, ("cloud",), {k["accessKey"] for k in keys})
        counts["keys"] += len(active)
        for k in active:
            d = _due_key(rows.get(k["accessKey"]), k["accessKey"], "cloud", now, limit, counts)
            if d:
                due.setdefault(ad, []).append(d)
    if due:
        emails = ({ad: await _ad_mail(app, ad) for ad in due} if app.state.mailer.enabled
                  else {})
        await _remind(app, now, limit or 0, due, emails, True, counts)


async def notice_tick(app, now: datetime | None = None) -> dict[str, Any]:
    """One pass: near-full buckets, then key ages (on premises, cloud). A part that fails
    does not stop the others; its name is in `errors`. Returns the counts."""
    db: Database = app.state.db
    now = now or bucket_quota.now_of(app)
    s = await db.run(load_settings, db.settings)
    limit = s["maxKeyAgeDays"] or None
    counts = {"buckets": 0, "nearFull": 0, "bucketNotices": 0, "keys": 0, "oldKeys": 0,
              "keyNotices": 0, "skipped": 0, "failed": 0}
    errors: list[str] = []
    parts = (("buckets", lambda: _near_full_pass(app, now, s["nearFullPercent"], counts)),
             ("keys", lambda: _key_pass(app, now, limit, counts)),
             ("cloudKeys", lambda: _cloud_key_pass(app, now, limit, counts)))
    for name, part in parts:
        try:
            await part()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  the next try is an hour later
            log.warning("user notices: %s failed: %s", name, type(e).__name__)
            errors.append(name)
    await db.run(_update_state, db, lastRun={"at": iso(now), "counts": counts,
                                             "errors": errors})
    return {**counts, "errors": errors}


def due(local: datetime, state: dict, now: datetime) -> bool:
    """Is the daily pass due at server local time `local` (`now` = the same moment, UTC)?"""
    if state.get("lastRunDay") == local.date().isoformat():
        return False
    if _recent_until(state.get("retryAfter"), now):
        return False
    return (local.hour, local.minute) >= NOTICE_TIME


def _recent_until(ts: str | None, now: datetime) -> bool:
    try:
        return bool(ts) and now < _parse(ts)
    except ValueError:
        return False


def next_run(local: datetime, state: dict) -> str:
    """When the pass runs next, server local time `YYYY-MM-DDTHH:MM` (for the admin page)."""
    at = local.replace(hour=NOTICE_TIME[0], minute=NOTICE_TIME[1], second=0, microsecond=0)
    if state.get("lastRunDay") == local.date().isoformat():
        at += timedelta(days=1)
    elif local >= at:
        retry = state.get("retryAfter")
        try:
            at = max(local, _parse(retry).astimezone(local.tzinfo)) if retry else local
        except ValueError:
            at = local
    return at.strftime("%Y-%m-%dT%H:%M")


async def schedule_tick(app, now: datetime | None = None) -> dict[str, Any] | None:
    """One scheduler check: runs the pass when it is due. Returns its counts, else None."""
    db: Database = app.state.db
    now = now or bucket_quota.now_of(app)
    local = now.astimezone()  # server local time
    if not due(local, await db.run(_state, db), now):
        return None
    res = await notice_tick(app, now)
    if res["errors"]:
        await db.run(_update_state, db, retryAfter=iso(now + RETRY))
    else:
        await db.run(_update_state, db, lastRunDay=local.date().isoformat(), retryAfter=None)
    return res


async def notice_loop(app, sleep=asyncio.sleep) -> None:
    """Lifespan task: checks every minute after a short delay. Never raises."""
    await sleep(FIRST_DELAY)
    while True:
        try:
            res = await schedule_tick(app)
            if res is not None:
                log.info("user notices: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  try again at the next check
            log.warning("user notices check failed: %s", type(e).__name__)
        await sleep(TICK_SECONDS)


# -- admin settings ----------------------------------------------------------------------

router = APIRouter(prefix="/admin/settings/notices", tags=["admin-settings"],
                   dependencies=[Depends(require_admin)])


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nearFullPercent: Any = None
    maxKeyAgeDays: Any = None


async def _view(app, db: Database) -> dict[str, Any]:
    state = await db.run(_state, db)
    local = bucket_quota.now_of(app).astimezone()
    return {
        **await db.run(load_settings, db.settings),
        "defaults": dict(DEFAULTS), "limits": {k: list(v) for k, v in LIMITS.items()},
        "email": {"enabled": bool(app.state.mailer.enabled)},
        "schedule": {"time": f"{NOTICE_TIME[0]:02d}:{NOTICE_TIME[1]:02d}",
                     "lastRun": state.get("lastRun"), "nextRun": next_run(local, state)},
    }


@router.get("")
async def get_notices(request: Request, _a: Admin, db: Db):
    return await _view(request.app, db)


@router.put("")
async def put_notices(request: Request, body: SettingsBody, admin: Admin, db: Db):
    given = body.model_fields_set & set(DEFAULTS)
    if not given:
        raise HTTPException(422, "Give nearFullPercent and/or maxKeyAgeDays.")
    errors = [{"field": k, "message": MESSAGES[k]} for k in sorted(given)
              if not valid(k, getattr(body, k))]
    if errors:
        return JSONResponse(status_code=422,
                            content={"detail": errors[0]["message"], "errors": errors})
    before = await db.run(load_settings, db.settings)
    after = {**before, **{k: getattr(body, k) for k in given}}
    if after != before:
        await db.run(db.settings.set, KEY, after, admin.username)
        await db.run(db.audit.add, admin.username, "admin.settings.notices", "setting", KEY,
                     {"before": before, "after": after})
    return await _view(request.app, db)
