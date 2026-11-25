"""Bucket quotas in whole TiB and the weekly increase limit (PLAN 2, P6-4).

Every bucket has a quota in whole TiB (at least 1): set when it is created and changed by
its owner (`PUT /api/buckets/{name}/quota`, in `buckets.py`) or an administrator
(`PUT /api/admin/buckets/{bucket}/quota`, any size, in bytes). Billing on premises is on
the quota (`catalog.py`). The account-wide RGW user quota is unlimited instead.

Settings key `bucket_quota` (admin Settings tab, `admin_settings.py`):
`{defaultQuotaTiB (1..MAX_BUCKET_TIB, default 1), maxWeeklyQuotaIncreaseTiB (1..100000,
default 100)}`.

Weekly limit: the sum of a person's quota INCREASES (new minus old, a new bucket counts its
starting quota, a bucket without a quota counts from 0) over all their buckets in the last
7 days (rolling) may not exceed `maxWeeklyQuotaIncreaseTiB`. Decreases are always allowed
and give nothing back. Administrators are not limited. Over the limit -> `LimitExceeded`
(the route answers 409 `{detail, remainingTiB, freesUpAt}`). Changes are recorded in
`quota_changes` (migration v13); callers check and record under the owner's per-uid lock,
so two parallel requests cannot both pass the limit.

`unlimit_user_quotas(app)` is the one-time step that sets the RGW user quota of known
people (`known_accounts`) to unlimited when it still equals the default Bucketeer used to
give new accounts (`policy_settings.effective_default_quota`, 1 TiB unless an admin had
changed it). It runs in the background at startup (`UNLIMIT_ON_START`), retries a few
times when the cluster cannot be reached, records completion in the setting
`user_quota_unlimited` and audits `rgw.user.quota_unlimited` (actor `system`) per account.
Bucket-key users and the Admin Ops service user are never touched.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .croit import CroitClientError
from .policy_settings import SettingsError, effective_default_quota
from .rgw_user import is_key_uid, is_service_uid, quota_summary

log = logging.getLogger("bucketeer.bucket_quota")

KEY = "bucket_quota"
DONE_KEY = "user_quota_unlimited"
TIB = 2**40
MAX_BUCKET_TIB = 100_000  # about 100 PiB per bucket
MAX_WEEKLY_TIB = 100_000
DEFAULTS: dict[str, int] = {"defaultQuotaTiB": 1, "maxWeeklyQuotaIncreaseTiB": 100}
WINDOW = timedelta(days=7)

UNLIMIT_ON_START = True  # tests switch the startup task off and call unlimit_user_quotas()
UNLIMIT_ATTEMPTS = 3
UNLIMIT_RETRY_SECONDS = 200.0


# -- settings ------------------------------------------------------------------------


def load_settings(settings_repo) -> dict[str, int]:
    stored = settings_repo.get(KEY) or {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def is_set(settings_repo) -> bool:
    return settings_repo.get(KEY) is not None


def _whole(v: Any, name: str, top: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= top:
        raise SettingsError(f"{name} must be a whole number of TiB from 1 to {top}.")
    return v


def normalize_settings(raw: Any, current: dict[str, int]) -> dict[str, int]:
    """A partial update `{defaultQuotaTiB?, maxWeeklyQuotaIncreaseTiB?}` applied to
    `current`. Raises SettingsError."""
    if not isinstance(raw, dict) or not raw:
        raise SettingsError("Send defaultQuotaTiB and/or maxWeeklyQuotaIncreaseTiB.")
    if set(raw) - set(DEFAULTS):
        raise SettingsError("Unknown bucket quota field.")
    out = dict(current)
    if "defaultQuotaTiB" in raw:
        out["defaultQuotaTiB"] = _whole(raw["defaultQuotaTiB"], "The default bucket quota",
                                        MAX_BUCKET_TIB)
    if "maxWeeklyQuotaIncreaseTiB" in raw:
        out["maxWeeklyQuotaIncreaseTiB"] = _whole(
            raw["maxWeeklyQuotaIncreaseTiB"], "The weekly limit", MAX_WEEKLY_TIB)
    return out


def legacy_default_tib(raw: dict) -> int:
    """The old `PUT /settings/default-quota` body `{enabled, maxSize (bytes), ...}` (or
    `{quotaTiB}`) as whole TiB. Raises SettingsError."""
    if not isinstance(raw, dict):
        raise SettingsError("The quota must be an object.")
    if "quotaTiB" in raw:
        return _whole(raw["quotaTiB"], "The default bucket quota", MAX_BUCKET_TIB)
    size = raw.get("maxSize")
    if raw.get("enabled") is not True or isinstance(size, bool) or not isinstance(size, int) \
            or size <= 0:
        raise SettingsError("Every bucket needs a quota: give a size in TiB.")
    return _whole(max(1, math.ceil(size / TIB)), "The default bucket quota", MAX_BUCKET_TIB)


# -- quota values ---------------------------------------------------------------------


def quota_tib(q: dict | None) -> int | None:
    """A bucket quota `{enabled, maxSize}` as whole TiB (rounded up); None without a size
    limit."""
    q = q or {}
    size = q.get("maxSize")
    if not q.get("enabled") or isinstance(size, bool) or not isinstance(size, int | float) \
            or size <= 0:
        return None
    return max(1, math.ceil(size / TIB))


def check_tib(v: Any) -> int:
    """A requested bucket quota; raises ValueError with a user-facing message."""
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_BUCKET_TIB:
        raise ValueError(f"The quota must be a whole number of TiB from 1 to "
                         f"{MAX_BUCKET_TIB:,}.")
    return v


async def apply(admin_client, full: str, tib: int) -> None:
    """Set the bucket quota (size only; objects unlimited) through the admin client.
    Raises CroitClientError."""
    await admin_client.set_bucket_quota(full, enabled=True, max_size=tib * TIB, max_objects=-1)


# -- the weekly limit -----------------------------------------------------------------


def iso(dt: datetime) -> str:
    """Same text form as `db.now()`, so stored times compare as strings."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def now_of(app) -> datetime:
    clock = getattr(app.state, "clock", None)
    return datetime.fromtimestamp(clock(), UTC) if clock else datetime.now(UTC)


@dataclass
class LimitExceeded(Exception):
    requested: int
    remaining: int
    limit: int
    frees_up_at: datetime | None

    def body(self) -> dict[str, Any]:
        when = self.frees_up_at
        if self.requested > self.limit:
            detail = (f"You can add at most {self.limit:,} TiB of quota in 7 days. "
                      f"Ask an administrator for more.")
        else:
            detail = (f"You can add {self.remaining:,} TiB more right now (the limit is "
                      f"{self.limit:,} TiB of quota increases in 7 days).")
            if when is not None:
                detail += (f" You can add {self.requested:,} TiB after "
                           f"{when:%b} {when.day}, {when:%H:%M} UTC.")
        return {"detail": detail, "remainingTiB": self.remaining, "limitTiB": self.limit,
                "requestedTiB": self.requested,
                "freesUpAt": iso(when) if when is not None else None}


def window(rows: list[dict], limit: int, now: datetime) -> dict[str, Any]:
    """Pure: the state of the rolling week from the counted increases (`increases_since`)."""
    start = now - WINDOW
    live = [(_parse(r["ts"]), int(r["increase"])) for r in rows]
    live = sorted((t, n) for t, n in live if t > start)
    used = sum(n for _, n in live)
    nxt = live[0][0] + WINDOW if live else None
    return {"usedTiB": used, "remainingTiB": max(0, limit - used), "limitTiB": limit,
            "live": live, "nextRelease": nxt}


def check(rows: list[dict], limit: int, now: datetime, increase: int) -> None:
    """Pure: raise LimitExceeded when `increase` TiB more would pass the limit."""
    if increase <= 0:
        return
    w = window(rows, limit, now)
    if w["usedTiB"] + increase <= limit:
        return
    frees = None
    if increase <= limit:
        need, released = w["usedTiB"] + increase - limit, 0
        for t, n in w["live"]:
            released += n
            if released >= need:
                frees = t + WINDOW
                break
    raise LimitExceeded(increase, w["remainingTiB"], limit, frees)


def person_of(username: str) -> str:
    return username.lower()


async def guard(app, username: str, increase: int, *, exempt: bool) -> None:
    """Raise LimitExceeded unless `username` may add `increase` TiB now. Call it under the
    owner's per-uid lock and record the change before releasing it."""
    if exempt or increase <= 0:
        return
    db = app.state.db
    limit = (await db.run(load_settings, db.settings))["maxWeeklyQuotaIncreaseTiB"]
    now = now_of(app)
    rows = await db.run(db.quota_changes.increases_since, person_of(username),
                        iso(now - WINDOW))
    check(rows, limit, now, increase)


async def summary(app, username: str, *, exempt: bool) -> dict[str, Any]:
    """What the create and quota dialogs show: the default, the limit and what is left."""
    db = app.state.db
    st = await db.run(load_settings, db.settings)
    now = now_of(app)
    rows = await db.run(db.quota_changes.increases_since, person_of(username),
                        iso(now - WINDOW))
    w = window(rows, st["maxWeeklyQuotaIncreaseTiB"], now)
    return {"defaultTiB": st["defaultQuotaTiB"], "maxTiB": MAX_BUCKET_TIB,
            "weeklyLimitTiB": None if exempt else st["maxWeeklyQuotaIncreaseTiB"],
            "remainingTiB": None if exempt else w["remainingTiB"],
            "nextReleaseAt": None if exempt or w["nextRelease"] is None
            else iso(w["nextRelease"]),
            "exempt": exempt}


async def record(app, *, username: str | None, uid: str, full: str, old: int | None,
                 new: int | None, actor: str, counted: bool = True) -> None:
    """Record a change in `quota_changes`; never raises (a lost row only shortens the
    week's memory)."""
    db = app.state.db
    try:
        await db.run(lambda: db.quota_changes.add(
            person=person_of(username) if username else None, uid=uid, bucket=full,
            old_tib=old, new_tib=new, actor=actor, counted=counted, ts=iso(now_of(app))))
    except Exception:
        log.exception("could not record the quota change of %s", full)


# -- one-time: unlimited RGW user quota for existing people ---------------------------


def _same_quota(a: dict, b: dict) -> bool:
    a, b = quota_summary(a), quota_summary(b)
    if not a["enabled"] and not b["enabled"]:
        return True
    return a == b


async def unlimit_user_quotas(app) -> dict[str, Any] | None:
    """Set the user quota of every known person whose quota is still the old default to
    unlimited. Returns the counts, or None when it already ran. Idempotent: an account that
    is already unlimited, or has another quota (set by an admin), is left alone. Completion
    is recorded only when every account could be checked."""
    db, croit = app.state.db, app.state.croit
    if await db.run(db.settings.get, DONE_KEY):
        return None
    old = await db.run(effective_default_quota, db.settings)
    uids = sorted({r["rgw_uid"] for r in await db.run(db.known_accounts.list)})
    changed: list[str] = []
    kept = failed = 0
    for uid in uids:
        try:
            if await db.run(is_key_uid, db, uid) or await is_service_uid(croit, uid):
                kept += 1
                continue
            user = await croit.get_user(uid)
            if user is None:
                kept += 1
                continue
            before = quota_summary(user.get("userQuota"))
            if not before["enabled"] or not _same_quota(before, old):
                kept += 1
                continue
            await croit.set_user_quota(uid, enabled=False)
        except CroitClientError as e:
            failed += 1
            log.warning("user quota of %s not made unlimited: %s", uid, type(e).__name__)
            continue
        changed.append(uid)
        await db.run(db.audit.add, "system", "rgw.user.quota_unlimited", "rgw_user", uid,
                     {"before": before, "reason": "bucket quotas replace the user quota"})
    out = {"changed": len(changed), "kept": kept, "failed": failed}
    if not failed:
        await db.run(db.settings.set, DONE_KEY, {**out, "doneAt": iso(now_of(app))}, "system")
        log.info("user quotas made unlimited: %s", out)
    return out


async def unlimit_on_start(app, sleep=asyncio.sleep) -> None:
    """The lifespan task: a few attempts, a pause between them; never raises."""
    for attempt in range(UNLIMIT_ATTEMPTS):
        try:
            res = await unlimit_user_quotas(app)
            if res is None or not res["failed"]:
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  cluster trouble: next start retries
            log.warning("unlimited user quota step failed: %s", type(e).__name__)
        if attempt + 1 < UNLIMIT_ATTEMPTS:
            await sleep(UNLIMIT_RETRY_SECONDS)
