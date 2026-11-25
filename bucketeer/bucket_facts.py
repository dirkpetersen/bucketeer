"""Bucket facts as managed `bucketeer-*` tags (PLAN 1, P6-2).

Every bucket carries its own record as tags, written with the OWNER's own S3 key through
`s3docs.update_bucket_tagging` (never put_bucket_tagging directly):

| key | value |
|---|---|
| `bucketeer-budget` | budget number (removed when an administrator clears it) |
| `bucketeer-bucket` | tenant-qualified bucket name (`tenant:bucket`) |
| `bucketeer-owner` | owner uid; `tenant:uid` with a tenant (`$` is not a tag character) |
| `bucketeer-owner-email` | owner's AD mail, when known |
| `bucketeer-creator` | AD username of the creator, when known |
| `bucketeer-created` | creation date `YYYY-MM-DD` (UTC), when known |
| `bucketeer-quota` | bucket quota in whole TiB (rounded up), `unlimited` without a size limit |
| `bucketeer-product` | product name (`product_name()`; `Tier-3-S3` on premises) |
| `bucketeer-location` | `onprem` (`cloud` later) |

`build()` is pure and takes plain values, so a later cloud path can reuse it with
`location="cloud"`. A fact that is unknown (None) leaves an existing tag as it is; only
the budget number is ever removed. A value outside the S3 tag alphabet
(`[\\w +-=.:/@]`, at most 256 characters) is left out with a log warning. When the 50-tag
limit leaves no room, missing facts are skipped (budget first, then in table order).

Tags are a record, not proof: owners can rewrite them with their own keys, and nothing
that decides access reads them.

Admin (`admin_router`, mounted under `/api/admin`):
- `POST /api/admin/buckets/write-tags` -> `{updated, unchanged, skipped, failed, truncated,
  details: {skipped: [{bucket, owner, reason}], failed: [{bucket, owner}]}}`: writes the
  facts to every bucket the admin client lists (at most `MAX_BULK`), each with its
  owner's own key. Owners that are suspended, unknown, without an S3 key, or bucket-key
  users are skipped. One run at a time (409); safe to repeat (unchanged tag sets are not
  rewritten). Audit `admin.buckets.write_tags` (target_type `bucket`, target `*`).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from . import catalog, s3docs
from .admin import croit_http_error
from .auth import SessionUser, require_admin
from .croit import CroitClientError
from .rgw_user import ad_names_for_uid, is_key_uid, split_uid

log = logging.getLogger("bucketeer.bucket_facts")

BUDGET = "bucketeer-budget"
BUCKET = "bucketeer-bucket"
OWNER = "bucketeer-owner"
OWNER_EMAIL = "bucketeer-owner-email"
CREATOR = "bucketeer-creator"
CREATED = "bucketeer-created"
QUOTA = "bucketeer-quota"
PRODUCT = "bucketeer-product"
LOCATION = "bucketeer-location"
# Write order and priority when the tag limit leaves no room.
KEYS = (BUDGET, BUCKET, OWNER, OWNER_EMAIL, CREATOR, CREATED, QUOTA, PRODUCT, LOCATION)

ONPREM = "onprem"
MAX_TAGS = 50
VALUE_MAX = 256
TIB = 2**40
CHARS = re.compile(r"\A[\w +\-=.:/@]*\Z")
MAX_BULK = 5000  # buckets per admin run
BULK_CONCURRENCY = 4
REPORT_MAX = 200  # skipped / failed entries returned


def product_name(location: str = ONPREM, products: list[dict] | None = None) -> str:
    """The product name of a bucket at `location` from the admin product catalog
    (`catalog.load_catalog`); without one, the default catalog (`Tier-3-S3` on premises)."""
    return catalog.product_for(products or catalog.build_catalog(None), location)["name"]


def tag_value_ok(value: str) -> bool:
    return isinstance(value, str) and len(value) <= VALUE_MAX and bool(CHARS.match(value))


def owner_value(uid: str) -> str:
    tenant, bare = split_uid(uid)
    return f"{tenant}:{bare}" if tenant else bare


def quota_value(q: dict | None) -> str:
    """RGW's bucket quota as whole TiB (rounded up), or `unlimited` without a size limit."""
    q = q or {}
    size = q.get("maxSize")
    if not q.get("enabled") or not isinstance(size, int | float) or size <= 0:
        return "unlimited"
    return str(max(1, math.ceil(size / TIB)))


def build(*, bucket: str, owner_uid: str, budget: str | None = None, set_budget: bool = False,
          owner_email: str | None = None, creator: str | None = None,
          created: str | None = None, quota: str | None = None,
          product: str | None = None, location: str = ONPREM) -> dict[str, str | None]:
    """The managed tags of one bucket as {key: value}; a key missing from the result is left
    as stored, a None value removes it (only the budget, and only with `set_budget`).
    `created` may be an ISO timestamp; its date part is used."""
    raw: dict[str, str | None] = {
        BUCKET: bucket,
        OWNER: owner_value(owner_uid) if owner_uid else None,
        OWNER_EMAIL: owner_email or None,
        CREATOR: creator or None,
        CREATED: (created or "")[:10] or None,
        QUOTA: quota,
        PRODUCT: product or product_name(location),
        LOCATION: location,
    }
    out: dict[str, str | None] = {}
    if set_budget:
        raw = {BUDGET: budget, **raw}
    for key, value in raw.items():
        if value is None:
            if key == BUDGET:
                out[key] = None
            continue
        if not tag_value_ok(value):
            log.warning("tag %s left out for %s: value not allowed in an S3 tag", key, bucket)
            continue
        out[key] = value
    return out


def merge(tags: list[dict] | None, facts: dict[str, str | None]) -> list[dict] | None:
    """The tag set with `facts` applied; every other tag is kept in place."""
    current = s3docs.parse_tags(tags) or []
    gone = {k for k, v in facts.items() if v is None}
    out = [t for t in current if t["Key"] not in gone]
    where = {t["Key"]: i for i, t in enumerate(out)}
    for key in KEYS:
        value = facts.get(key)
        if value is None:
            continue
        if key in where:
            out[where[key]] = {"Key": key, "Value": value}
        elif len(out) < MAX_TAGS:
            out.append({"Key": key, "Value": value})
        else:
            log.warning("tag %s left out: the bucket already has %d tags", key, MAX_TAGS)
    return out or None


def write(s3, bucket: str, facts: dict[str, str | None]) -> bool:
    """Apply the facts to the bucket's tag set (sync, owner's client). True if it changed."""
    changed = []

    def mutate(tags):
        new = merge(tags, facts)
        changed.append(new != (s3docs.parse_tags(tags) or None))
        return new

    s3docs.update_bucket_tagging(s3, bucket, mutate)
    return bool(changed and changed[0])


def _short(full: str) -> str:
    return full.partition(":")[2] or full


async def bucket_row(admin_client, uid: str, full: str) -> dict | None:
    """The admin client's stats row of an own bucket (quota), None if not listed.
    Raises CroitClientError when the admin plane cannot be read."""
    rows = await admin_client.list_user_buckets(uid)
    return next((b for b in rows or [] if isinstance(b, dict) and b.get("bucket") == full
                 and b.get("owner") == uid), None)


_ROW = object()


async def sync(app, uid: str, full: str, *, owner_email: str | None = None,
               creator: str | None = None, stats: dict | None = None,
               lookup: bool = True, budget: Any = _ROW) -> tuple[bool, bool]:
    """Write the facts of one bucket with the owner's own key. Budget, creator and creation
    date come from the SQLite row (`creator` only fills a missing one, and neither is written
    for a row with `creation_known` 0, made after the bucket; `budget` overrides
    the row, e.g. when it could not be written). Returns (ok, changed); ok is False when
    the write failed or the quota could not be read (the caller keeps `tag_synced = 0` and
    reconcile retries). Never raises."""
    db = app.state.db
    complete = True
    try:
        row = await db.run(db.buckets.get, full, "onprem")
        if stats is None and lookup:
            try:
                stats = await bucket_row(app.state.croit, uid, full)
            except CroitClientError as e:
                log.warning("quota of %s unreadable for its tags: %s", full, type(e).__name__)
            if stats is None:
                complete = False
        facts = build(
            bucket=full, owner_uid=uid,
            budget=(row or {}).get("budget_number") if budget is _ROW else budget,
            set_budget=row is not None or budget is not _ROW,
            owner_email=owner_email,
            # a row made after the bucket (`creation_known` 0) leaves both tags as they are
            creator=((row.get("created_by") or creator) if row.get("creation_known", 1)
                     else None) if row else creator,
            created=row.get("created_at") if row and row.get("creation_known", 1) else None,
            quota=quota_value(stats.get("bucket_quota")) if stats is not None else None,
            product=product_name(ONPREM, await db.run(catalog.load_catalog, db)),
        )
        changed = await app.state.s3.call(uid, write, _short(full), facts)
        return complete, bool(changed)
    except HTTPException as e:
        log.warning("Bucketeer tags not written for %s: %s", full, e.detail)
    except Exception as e:  # botocore errors, timeouts: the bucket stays, the tags are retried
        log.warning("Bucketeer tags not written for %s: %s", full, type(e).__name__)
    return False, False


# -- admin: write the tags of every bucket -------------------------------------------

admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
Admin = Annotated[SessionUser, Depends(require_admin)]


async def _owner_email(app, uid: str) -> str | None:
    """The owner's AD mail through the uid's AD names; best effort, None when unknown."""
    db, auth = app.state.db, getattr(app.state, "authenticator", None)
    if auth is None:
        return None
    try:
        names = await db.run(ad_names_for_uid, db, uid, app.state.rgw.tenant)
    except Exception:
        return None
    for name in names[:2]:
        try:
            person = await auth.lookup_user(name)
        except Exception:
            continue
        if person.email:
            return person.email
    return None


@admin_router.post("/buckets/write-tags")
async def write_all(request: Request, admin: Admin):
    app = request.app
    lock = getattr(app.state, "facts_lock", None)
    if lock is None:
        lock = app.state.facts_lock = asyncio.Lock()
    if lock.locked():
        raise HTTPException(409, "Bucketeer tags are already being written. Try again shortly.")
    async with lock:
        return await _write_all(app, admin.username)


async def _write_all(app, actor: str) -> dict[str, Any]:
    croit, db = app.state.croit, app.state.db
    croit.invalidate_lists()
    try:
        rows = await croit.list_buckets()
    except CroitClientError as e:
        raise croit_http_error(e) from None
    rows = sorted((b for b in rows or [] if isinstance(b, dict) and b.get("bucket")),
                  key=lambda b: str(b["bucket"]))
    truncated = len(rows) > MAX_BULK
    rows = rows[:MAX_BULK]
    by_owner: dict[str, list[dict]] = {}
    skipped: list[dict] = []
    for b in rows:
        owner = b.get("owner") or ""
        if not owner:
            skipped.append({"bucket": b["bucket"], "owner": "", "reason": "no_owner"})
        else:
            by_owner.setdefault(owner, []).append(b)
    failed: list[dict] = []
    counts = {"updated": 0, "unchanged": 0}
    sem = asyncio.Semaphore(BULK_CONCURRENCY)

    async def one_owner(owner: str, buckets: list[dict]) -> None:
        def skip(reason: str) -> None:
            skipped.extend({"bucket": b["bucket"], "owner": owner, "reason": reason}
                           for b in buckets)

        async with sem:
            if await db.run(is_key_uid, db, owner):
                return skip("bucket_key_user")
            try:
                user = await croit.get_user(owner)
            except CroitClientError:
                failed.extend({"bucket": b["bucket"], "owner": owner} for b in buckets)
                return
            if user is None:
                return skip("owner_not_found")
            if user.get("suspended"):
                return skip("owner_suspended")
            try:
                await app.state.s3.client_for(owner)
            except HTTPException as e:
                if e.status_code == 409:
                    return skip("owner_has_no_key")
                failed.extend({"bucket": b["bucket"], "owner": owner} for b in buckets)
                return
            email = await _owner_email(app, owner)
            for b in buckets:
                ok, changed = await sync(app, owner, b["bucket"], owner_email=email, stats=b)
                if not ok:
                    failed.append({"bucket": b["bucket"], "owner": owner})
                    continue
                counts["updated" if changed else "unchanged"] += 1
                try:
                    await db.run(db.buckets.set_tag_synced, b["bucket"], True)
                except Exception:
                    log.exception("could not mark the tags of %s as synced", b["bucket"])

    await asyncio.gather(*(one_owner(o, bs) for o, bs in by_owner.items()))
    out = {
        "updated": counts["updated"], "unchanged": counts["unchanged"],
        "skipped": len(skipped), "failed": len(failed), "truncated": truncated,
        "details": {"skipped": sorted(skipped, key=lambda s: s["bucket"])[:REPORT_MAX],
                    "failed": sorted(failed, key=lambda s: s["bucket"])[:REPORT_MAX]},
    }
    reasons: dict[str, int] = {}
    for s in skipped:
        reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    await db.run(db.audit.add, actor, "admin.buckets.write_tags", "bucket", "*", {
        "updated": out["updated"], "unchanged": out["unchanged"], "skipped": out["skipped"],
        "skipped_reasons": reasons, "failed": out["failed"], "truncated": truncated,
        "failed_buckets": [f["bucket"] for f in failed][:50]})
    return out
