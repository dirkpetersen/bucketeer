"""Cloud (AWS) buckets, P6-10 (PLAN.md section 4).

A person's cloud bucket is `<CLOUD_BUCKET_PREFIX><owner part>-<name>` (`CloudAdmin.bucket_name`;
the owner part is the lowercased AD name with `.`/`_`/`@`/`+` as hyphens, and `backup`/`billing`
(alone or followed by `-`) refused: those are the system buckets). The person types only
`<name>`: 1-63 lowercase letters, digits and hyphens, no dots, starting and ending with a
letter or digit, the full name at most 63 characters. Bucketeer's AWS admin user creates and
configures it; the owner reaches it ONLY through the bucket-policy statement `bucketeer-owner`
naming their IAM user (`CloudAdmin.grant_bucket_owner`), never through a name wildcard or a tag.

Rows live in the `buckets` table with `location = 'cloud'` (migration v18): `quota_tib` (AWS
has no bucket quota; mirrored in the `bucketeer-quota` tag), `storage_class`, the budget
number, `over_quota_alert_at`. The list is read from SQLite, so system buckets never show.

Routes (`/api/cloud/buckets`, `require_active_rgw_user`, cloud available else 503, CSRF on
mutations, `no-store`):
- `GET` -> `{buckets: [{name, fullName, storageClass, product, quotaTiB, monthlyCost (if full),
  bytes|null, sizeAsOf|null, estimatedCost|null, overQuota, createdAt, budgetNumber,
  budgetMissing, tagPending}], region, currency}`; size = CloudWatch `BucketSizeBytes` over
  all storage types (daily, 1-2 days late; cached `SIZE_TTL`), costs from the catalog price
  (Cloud-S3 for Intelligent-Tiering, Archive-S3 for Deep Archive; an estimate: the real AWS
  cost is billed).
- `GET /options` -> `{prefix, maxNameLength, region, storageClasses: [{id, awsClass, product}],
  defaultStorageClass, defaultBudgetNumber, budgetRequired, budgetExample, quota, refused}`.
- `GET /check?name=` -> `{name, fullName, available, status (free|exists|taken|elsewhere|
  invalid|refused), message, suggestions: [{name, fullName}]}`: HeadBucket by the admin user
  (404 free, 200/403 taken, 301 taken in another region); when taken up to 3 suggestions
  (`<name>-2`, `<name>-<year>`, `<name>-<4 random>`), each checked free.
- `POST {name, storageClass: intelligent|deep_archive, quotaTiB?, versioning? (default true),
  noncurrentDays? (default 30, null = none), budgetNumber?}` -> 201. Budget gate and budget
  rules as on premises; only while the person's effective location is cloud (409 otherwise);
  the quota counts against the weekly limit shared with on premises (`bucket_quota`, under the
  same per-uid lock). Steps: `cloud_users.ensure_account` -> CreateBucket (CLOUD_REGION) ->
  Block Public Access, SSE-S3, BucketOwnerEnforced, versioning -> lifecycle `bucketeer-class`
  (day-0 transition to INTELLIGENT_TIERING or DEEP_ARCHIVE), `bucketeer-abort-multipart` (7
  days), `bucketeer-expire-noncurrent` when versioned -> policy `bucketeer-owner` (retried while
  IAM propagates a new user) -> `bucketeer-*` tags (`bucket_facts.build(location='cloud')`) ->
  SQLite row. Any failure after CreateBucket deletes the (empty) bucket again and audits
  `cloud.bucket.create_failed` (step, rolledBack); a CreateBucket whose outcome is unknown is
  audited the same way.
- `PUT /{name}/quota {quotaTiB}` (weekly limit; tag re-synced), `PUT /{name}/budget
  {budgetNumber}` (admins may clear it) -> `{name, ..., changed, tagSynced, warning?}`.
- `DELETE /{name}` `{confirmName?}` (P6-13, owner 2026-11-25): without `confirmName` equal to
  the short name, 409 `{detail, requiresConfirmation, objects, bytes|null, more, sizeAsOf,
  countsMayLag: true, storageClass}` (`contents`: one live ListObjectsV2 page by the admin,
  plus CloudWatch `NumberOfObjects`/`BucketSizeBytes`, a day or two late; an empty first page
  means 0). Confirmed and empty: `revoke_bucket_owner`, DeleteBucket -> 200 `{name}`, its
  shares (`cloud_sharing.py`) marked revoked
  (BucketNotEmpty = old versions remain: statement back, then a job). Holding data: 202
  `{job}`, a background job (`cloud_delete.py`) in `bucket_delete_jobs` with `location
  'cloud'` that empties the bucket with a temporary key of the owner's IAM user; an active job
  answers 409 `{job}`, a failed one is retried. `GET /delete-jobs` -> `{jobs}` (own cloud
  jobs, `bucket` = short name, `fullName`), `POST /delete-jobs/{id}/retry` -> `{job}`.
Audit `cloud.bucket.create|create_failed|quota|budget|delete|delete_started|delete_failed|
delete_retry|over_quota` (target_type `bucket`, the full AWS name).

Over-quota alert: `alert_loop` (lifespan task, `ALERT_ON_START`, off in tests; tests call
`alert_tick(app, now)`) once a day reads every cloud bucket's size; above its quota the OWNER
(AD mail of `created_by`) gets an email at most once per 7 days per bucket (`over_quota_alert_at`;
a failed send retries the next day; email off = audit only), audit `cloud.bucket.over_quota`
(actor `system`). The billing contact is not emailed. The same tick retries tag writes that
failed (`tag_synced = 0`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import bucket_delete, bucket_facts, bucket_quota, catalog, location, s3docs
from .auth import SessionUser, require_user
from .buckets import _user_lock
from .budget import BudgetNumberError, budget_example, validate_budget_number
from .cloud import STORAGE_CLASSES, CloudAdmin, CloudError, CloudUnavailable
from .cloud_costs import is_system_bucket
from .cloud_users import CloudRefused, cloud_http_error, ensure_account, require_cloud
from .db import Database
from .profile import require_default_budget
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.cloud_buckets")

TIB = 2**40
MAX_NAME = 63
ABORT_DAYS = 7
SIZE_TTL = 3600.0
ALERT_EVERY = timedelta(days=7)
ALERT_ON_START = True
ALERT_FIRST_DELAY = 600.0  # after start: the connection check has run by then
ALERT_PERIOD = 86400.0
PRODUCT_OF = {"intelligent": catalog.CLOUD, "deep_archive": catalog.ARCHIVE}
_NAME = re.compile(r"\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_BAD_SUFFIXES = ("-s3alias", "--ol-s3", "--x-s3", "--table-s3")

router = APIRouter(prefix="/cloud/buckets", tags=["cloud"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]
Cloud = Annotated[CloudAdmin, Depends(require_cloud)]


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=256)
    storageClass: Literal["intelligent", "deep_archive"]
    quotaTiB: StrictInt | None = None
    versioning: bool = True
    noncurrentDays: StrictInt | None = 30
    budgetNumber: str | None = Field(default=None, max_length=4096)


class QuotaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quotaTiB: StrictInt


class BudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budgetNumber: str | None = Field(max_length=4096)


# -- names --------------------------------------------------------------------------


def name_problem(name: str, prefix_len: int) -> str:
    """Why `name` can't be a cloud bucket name ('' = fine). `prefix_len` = length of
    `<CLOUD_BUCKET_PREFIX><owner part>-`."""
    room = MAX_NAME - prefix_len
    if not name:
        return "Enter a bucket name."
    if len(name) > room:
        return f"Use at most {room} characters (the full AWS name may have 63)."
    if not _NAME.match(name):
        return ("Use lowercase letters, digits and hyphens (no dots); start and end with a "
                "letter or digit.")
    if name.endswith(_BAD_SUFFIXES):
        return "AWS reserves names ending like this. Choose another ending."
    return ""


def _prefix(cloud: CloudAdmin, username: str) -> str:
    """`<CLOUD_BUCKET_PREFIX><owner part>-`; CloudError 409 `reserved` for system names."""
    return f"{cloud.bucket_prefix}{cloud.owner_part(username)}-"


def _refused(e: CloudError) -> HTTPException:
    return HTTPException(409, e.message)


async def availability(app, cloud: CloudAdmin, full: str) -> str:
    """free | exists | taken | elsewhere; a row in SQLite (any location) counts as exists."""
    db: Database = app.state.db
    if await db.run(db.buckets.get, full) is not None:
        return "exists"
    return await cloud.bucket_status(full)


MESSAGES = {
    "free": "This name is available.",
    "exists": "A bucket with this name already exists.",
    "own": "You already have a bucket with this name.",
    "taken": "This name is taken: AWS bucket names are shared by everyone on AWS.",
    "elsewhere": "This name is taken (the bucket exists in another AWS region).",
}


def _year(app) -> int:
    return bucket_quota.now_of(app).year


async def suggestions(app, cloud: CloudAdmin, prefix: str, name: str) -> list[dict]:
    rand = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
    out: list[dict] = []
    base = name[: max(1, MAX_NAME - len(prefix) - 5)].rstrip("-") or name
    for cand in (f"{base}-2", f"{base}-{_year(app)}", f"{base}-{rand}"):
        if name_problem(cand, len(prefix)) or any(s["name"] == cand for s in out):
            continue
        try:
            if await availability(app, cloud, prefix + cand) == "free":
                out.append({"name": cand, "fullName": prefix + cand})
        except CloudError:
            continue
    return out[:3]


# -- helpers --------------------------------------------------------------------------


async def _audit(app, actor: str, action: str, target: str, **details: Any) -> None:
    db: Database = app.state.db
    await db.run(db.audit.add, actor, action, "bucket", target, details)


async def _catalog(app) -> list[dict]:
    return await app.state.db.run(catalog.load_catalog, app.state.db)


def _product(cat: list[dict], storage_class: str | None) -> dict:
    return catalog.product(cat, PRODUCT_OF.get(storage_class or "", catalog.CLOUD))


def _facts(row: dict, product_name: str, *, owner_email: str | None = None,
           creator: str | None = None) -> dict:
    return bucket_facts.build(
        bucket=row["name"], owner_uid=row["owner_uid"], budget=row.get("budget_number"),
        set_budget=True, owner_email=owner_email, creator=creator or row.get("created_by"),
        created=row.get("created_at"), quota=str(row["quota_tib"]) if row.get("quota_tib")
        else None, product=product_name, location="cloud")


async def write_tags(app, row: dict, *, owner_email: str | None = None) -> None:
    """The managed tags, written by the admin user (it may PutBucketTagging on `<prefix>*`).
    Raises on failure."""
    cloud: CloudAdmin = app.state.cloud
    product = _product(await _catalog(app), row.get("storage_class"))
    facts = _facts(row, product["name"], owner_email=owner_email)
    await asyncio.to_thread(bucket_facts.write, cloud.client("s3"), row["name"], facts)


async def _try_tags(app, row: dict, owner_email: str | None) -> bool:
    db: Database = app.state.db
    try:
        await write_tags(app, row, owner_email=owner_email)
        ok = True
    except Exception as e:  # noqa: BLE001  kept as pending; the daily tick retries
        log.warning("tags of cloud bucket %s not written: %s", row["name"], type(e).__name__)
        ok = False
    try:
        await db.run(db.buckets.set_tag_synced, row["name"], ok)
    except Exception:
        log.exception("could not record the tag state of %s", row["name"])
    return ok


def _lifecycle(storage_class: str, noncurrent_days: int | None):
    rules = [s3docs.storage_class_rule(STORAGE_CLASSES[storage_class]),
             s3docs.abort_multipart_rule(ABORT_DAYS)]
    if noncurrent_days is not None:
        rules.append(s3docs.expire_noncurrent_rule(noncurrent_days))
    return lambda cfg: s3docs.upsert_rules(cfg, rules)


def _sizes(app) -> dict[str, tuple[float, dict | None]]:
    if not hasattr(app.state, "cloud_sizes"):
        app.state.cloud_sizes = {}
    return app.state.cloud_sizes


async def size_of(app, bucket: str, *, fresh: bool = False) -> dict | None:
    """CloudWatch size `{bytes, asOf}` (cached SIZE_TTL; None = nothing yet or unreadable)."""
    cache = _sizes(app)
    hit = cache.get(bucket)
    if hit and not fresh and time.monotonic() - hit[0] < SIZE_TTL:
        return hit[1]
    try:
        value = await app.state.cloud.bucket_size(bucket, now=bucket_quota.now_of(app))
    except CloudError as e:
        log.info("size of cloud bucket %s unreadable: %s", bucket, e.code)
        return hit[1] if hit else None
    cache[bucket] = (time.monotonic(), value)
    return value


async def _own_row(app, cloud: CloudAdmin, acct: RgwAccount, name: str) -> dict:
    db: Database = app.state.db
    try:
        prefix = _prefix(cloud, acct.username)
    except CloudError:
        raise HTTPException(404, "Bucket not found.") from None
    if name_problem(name, len(prefix)):
        raise HTTPException(404, "Bucket not found.")
    row = await db.run(db.buckets.get, prefix + name)
    if row is None or row.get("location") != "cloud" or row["owner_uid"] != acct.uid:
        raise HTTPException(404, "Bucket not found.")
    return row


def _cost(product: dict, tib: float | None) -> float | None:
    if tib is None:
        return None
    return round(tib * float(product["pricePerTiBMonth"]), 2)


def _view(row: dict, product: dict, size: dict | None, short: str) -> dict:
    quota = row.get("quota_tib")
    nbytes = size["bytes"] if size else None
    return {
        "name": short, "fullName": row["name"], "storageClass": row.get("storage_class"),
        "product": catalog.public(product), "quotaTiB": quota,
        "monthlyCost": catalog.monthly_cost(product, quota),
        "bytes": nbytes, "sizeAsOf": size["asOf"] if size else None,
        "estimatedCost": _cost(product, nbytes / TIB) if nbytes is not None else None,
        "overQuota": bool(quota and nbytes is not None and nbytes > quota * TIB),
        "createdAt": row.get("created_at"), "budgetNumber": row.get("budget_number"),
        "budgetMissing": row.get("budget_number") is None,
        "tagPending": not row.get("tag_synced", 1),
    }


def _limit_response(e: bucket_quota.LimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=409, content=e.body())


# -- list / options / check ------------------------------------------------------------


@router.get("")
async def list_cloud_buckets(request: Request, response: Response, acct: Account,
                             cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    s = app.state.settings
    rows = [r for r in await db.run(db.buckets.list_cloud, acct.uid)
            if not is_system_bucket(r["name"], s.cloud_bucket_prefix,
                                    getattr(s, "cloud_billing_bucket", "") or "")]
    cat = await _catalog(app)
    sem = asyncio.Semaphore(4)

    async def one(r: dict) -> dict | None:
        async with sem:
            return await size_of(app, r["name"])

    sizes = await asyncio.gather(*(one(r) for r in rows))
    try:
        prefix = _prefix(cloud, acct.username)
    except CloudError:
        prefix = ""
    out = [_view(r, _product(cat, r.get("storage_class")), sz,
                 r["name"][len(prefix):] if prefix and r["name"].startswith(prefix)
                 else r["name"])
           for r, sz in zip(rows, sizes, strict=True)]
    return {"buckets": out, "region": cloud.region,
            "currency": cat[0]["currency"] if cat else "USD"}


@router.get("/options")
async def options(request: Request, response: Response, acct: Account, user: User,
                  cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    refused = None
    try:
        prefix = _prefix(cloud, acct.username)
    except CloudError as e:
        prefix, refused = "", e.message
    cat = await _catalog(app)
    profile = await db.run(db.profiles.get, acct.username)
    return {
        "prefix": prefix, "maxNameLength": max(0, MAX_NAME - len(prefix)),
        "region": cloud.region, "refused": refused,
        "storageClasses": [{"id": k, "awsClass": STORAGE_CLASSES[k],
                            "product": catalog.public(_product(cat, k))}
                           for k in ("intelligent", "deep_archive")],
        "defaultStorageClass": "intelligent",
        "defaultBudgetNumber": profile["default_budget_number"],
        "budgetRequired": not user.is_admin,
        "budgetExample": await db.run(budget_example, db.settings),
        "quota": await bucket_quota.summary(app, user.username, exempt=user.is_admin),
    }


@router.get("/check")
async def check_name(name: str, request: Request, response: Response, acct: Account,
                     cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    name = name[:128]
    try:
        prefix = _prefix(cloud, acct.username)
    except CloudError as e:
        return {"name": name, "fullName": None, "available": False, "status": "refused",
                "message": e.message, "suggestions": []}
    problem = name_problem(name, len(prefix))
    if problem:
        return {"name": name, "fullName": prefix + name, "available": False,
                "status": "invalid", "message": problem, "suggestions": []}
    full = prefix + name
    try:
        status = await availability(request.app, cloud, full)
        sugg = [] if status == "free" else await suggestions(request.app, cloud, prefix, name)
    except CloudError as e:
        raise cloud_http_error(e) from None
    message = MESSAGES[status]
    if status == "exists":
        row = await request.app.state.db.run(request.app.state.db.buckets.get, full, "cloud")
        if row is not None and row["owner_uid"] == acct.uid:
            message = MESSAGES["own"]  # status stays `exists`
    return {"name": name, "fullName": full, "available": status == "free", "status": status,
            "message": message, "suggestions": sugg}


# -- create -----------------------------------------------------------------------------


async def _rollback(cloud: CloudAdmin, full: str) -> bool:
    try:
        await cloud.delete_bucket(full)
        return True
    except CloudError as e:
        if e.code == "NoSuchBucket":
            return True
        log.warning("rollback: cloud bucket %s not deleted: %s", full, e.code)
        return False


@router.post("", status_code=201, dependencies=[Depends(require_default_budget)])
async def create_cloud_bucket(body: CreateBody, request: Request, response: Response,
                              acct: Account, user: User, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    profile = await db.run(db.profiles.get, acct.username)
    if location.view(app, profile)["location"] != "cloud":
        raise HTTPException(409, "Switch to Cloud to create cloud buckets.")
    try:
        prefix = _prefix(cloud, acct.username)
    except CloudError as e:
        raise _refused(e) from None
    problem = name_problem(body.name, len(prefix))
    if problem:
        raise HTTPException(422, problem)
    full = prefix + body.name
    try:
        budget = await asyncio.to_thread(validate_budget_number, body.budgetNumber, db.settings)
    except BudgetNumberError as e:
        raise HTTPException(422, str(e)) from None
    if budget is None and not user.is_admin:
        raise HTTPException(422, "Enter a budget number for this bucket.")
    if body.quotaTiB is None:
        tib = (await db.run(bucket_quota.load_settings, db.settings))["defaultQuotaTiB"]
    else:
        try:
            tib = bucket_quota.check_tib(body.quotaTiB)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
    noncurrent = body.noncurrentDays if body.versioning else None
    if noncurrent is not None:
        try:
            s3docs.expire_noncurrent_rule(noncurrent)
        except s3docs.S3DocError as e:
            raise HTTPException(422, str(e)) from None
    taken = "This name is taken. Choose another name."

    async with _user_lock(request, acct.uid):  # shared with on prem: one weekly-limit check
        try:
            await bucket_quota.guard(app, user.username, tib, exempt=user.is_admin)
        except bucket_quota.LimitExceeded as e:
            return _limit_response(e)
        try:
            account = await ensure_account(app, acct.username)
        except (CloudRefused, CloudError) as e:
            log.info("cloud bucket create refused for %s: %s", acct.username, e)
            raise cloud_http_error(e) from None
        if await db.run(db.buckets.get, full) is not None:
            raise HTTPException(409, taken)
        try:
            await cloud.create_bucket(full)
        except CloudError as e:
            if e.code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
                raise HTTPException(409, taken) from None
            if e.code == "AccessDenied":  # the admin policy's bucket prefix differs
                log.warning("cloud bucket %s: AWS denied CreateBucket. Bucketeer's AWS policy must "
                            "allow bucket names starting with CLOUD_BUCKET_PREFIX (%s): re-run "
                            "tools/aws-setup with --prefix %s", full, cloud.bucket_prefix,
                            cloud.bucket_prefix)
            if isinstance(e, CloudUnavailable) or e.status >= 500:  # it may exist now
                await _audit(app, user.username, "cloud.bucket.create_failed", full,
                             uid=acct.uid, step="create", error=e.code, rolledBack=None)
            raise cloud_http_error(e) from None

        step = "secure"
        try:
            await cloud.secure_bucket(full, body.versioning)
            step = "lifecycle"
            await asyncio.to_thread(s3docs.update_bucket_lifecycle, cloud.client("s3"), full,
                                    _lifecycle(body.storageClass, noncurrent))
            step = "policy"
            await cloud.grant_bucket_owner(full, await cloud.user_arn(account["iam_user"]))
            step = "tags"
            row = {"name": full, "owner_uid": acct.uid, "budget_number": budget,
                   "created_by": user.username, "quota_tib": tib,
                   "storage_class": body.storageClass,
                   "created_at": datetime.now(UTC).isoformat()}
            await write_tags(app, row, owner_email=user.email)
            step = "record"
            await db.run(db.buckets.create_cloud, full, acct.uid, budget, user.username,
                         body.storageClass, tib)
        except Exception as e:  # noqa: BLE001  any failure: nothing half-made is kept
            code = e.code if isinstance(e, CloudError) else type(e).__name__
            log.warning("cloud bucket %s: %s failed (%s); rolling back", full, step, code)
            rolled = await _rollback(cloud, full)
            await _audit(app, user.username, "cloud.bucket.create_failed", full,
                         uid=acct.uid, step=step, error=code, rolledBack=rolled)
            if rolled:
                raise HTTPException(502, "AWS could not finish setting up the bucket, so it "
                                         "was removed again. Try again later.") from None
            raise HTTPException(502, "AWS could not finish setting up the bucket and it could "
                                     "not be removed. Ask an administrator.") from None
        await bucket_quota.record(app, username=user.username, uid=acct.uid, full=full,
                                  old=None, new=tib, actor=user.username,
                                  counted=not user.is_admin)
        await _audit(app, user.username, "cloud.bucket.create", full, uid=acct.uid,
                     storageClass=body.storageClass, quotaTiB=tib, budget=budget,
                     versioning=body.versioning, noncurrentDays=noncurrent,
                     region=cloud.region, iamUser=account["iam_user"])
    log.info("cloud bucket %s created for %s (%s, %d TiB)", full, acct.username,
             body.storageClass, tib)
    product = _product(await _catalog(app), body.storageClass)
    return {"name": body.name, "fullName": full, "storageClass": body.storageClass,
            "quotaTiB": tib, "monthlyCost": catalog.monthly_cost(product, tib),
            "product": catalog.public(product), "budgetNumber": budget,
            "versioning": "enabled" if body.versioning else "off", "region": cloud.region}


# -- quota / budget ---------------------------------------------------------------------


@router.put("/{name}/quota")
async def set_quota(name: str, body: QuotaBody, request: Request, response: Response,
                    acct: Account, user: User, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    try:
        new = bucket_quota.check_tib(body.quotaTiB)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    async with _user_lock(request, acct.uid):
        row = await _own_row(app, cloud, acct, name)
        product = _product(await _catalog(app), row.get("storage_class"))
        old = row.get("quota_tib")
        if old == new and row.get("tag_synced", 1):
            return {"name": name, "quotaTiB": new, "changed": False,
                    "monthlyCost": catalog.monthly_cost(product, new), "tagSynced": True}
        try:
            await bucket_quota.guard(app, user.username, new - (old or 0),
                                     exempt=user.is_admin)
        except bucket_quota.LimitExceeded as e:
            return _limit_response(e)
        await db.run(db.buckets.set_quota_tib, row["name"], new)
        if old != new:
            await bucket_quota.record(app, username=user.username, uid=acct.uid,
                                      full=row["name"], old=old, new=new, actor=user.username,
                                      counted=not user.is_admin)
        ok = await _try_tags(app, {**row, "quota_tib": new}, user.email)
        if old != new:
            await _audit(app, user.username, "cloud.bucket.quota", row["name"], uid=acct.uid,
                         oldTiB=old, newTiB=new, tagSynced=ok)
    out: dict[str, Any] = {"name": name, "quotaTiB": new, "changed": old != new,
                           "monthlyCost": catalog.monthly_cost(product, new), "tagSynced": ok}
    if not ok:
        out["warning"] = ("The quota was changed, but the bucket's quota tag could not be "
                          "updated yet. It will be retried automatically.")
    return out


@router.put("/{name}/budget")
async def set_budget(name: str, body: BudgetBody, request: Request, response: Response,
                     acct: Account, user: User, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    try:
        budget = await asyncio.to_thread(validate_budget_number, body.budgetNumber, db.settings)
    except BudgetNumberError as e:
        raise HTTPException(422, str(e)) from None
    if budget is None and not user.is_admin:
        raise HTTPException(422, "Enter a budget number for this bucket.")
    async with _user_lock(request, acct.uid):
        row = await _own_row(app, cloud, acct, name)
        old = row.get("budget_number")
        if old == budget and row.get("tag_synced", 1):
            return {"name": name, "budgetNumber": budget, "changed": False, "tagSynced": True}
        await db.run(db.buckets.set_budget_number, row["name"], budget)
        ok = await _try_tags(app, {**row, "budget_number": budget}, user.email)
    if old != budget:
        await _audit(app, user.username, "cloud.bucket.budget", row["name"], uid=acct.uid,
                     old=old, new=budget, tag_synced=ok)
    out: dict[str, Any] = {"name": name, "budgetNumber": budget, "changed": old != budget,
                           "tagSynced": ok}
    if not ok:
        out["warning"] = ("The budget number was saved, but the bucket tag could not be "
                          "updated yet. It will be retried automatically.")
    return out


# -- delete -----------------------------------------------------------------------------


class DeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmName: str | None = Field(default=None, max_length=256)


async def contents(app, cloud: CloudAdmin, full: str) -> dict[str, Any]:
    """What the bucket holds, for the confirmation: one live ListObjectsV2 page (current
    objects only) plus CloudWatch's daily object count and size (a day or two late)."""
    try:
        live, more = await cloud.object_probe(full)
    except CloudError as e:
        if e.code != "NoSuchBucket":
            raise
        live, more = 0, False
    if not live and not more:
        return {"objects": 0, "bytes": 0, "more": False, "sizeAsOf": None}
    now = bucket_quota.now_of(app)
    try:
        counted = await cloud.bucket_metric(full, "NumberOfObjects", "AllStorageTypes",
                                            now - timedelta(days=4), now)
    except CloudError:
        counted = None
    size = await size_of(app, full)
    return {"objects": max(live, int(counted or 0)), "bytes": size["bytes"] if size else None,
            "more": more and not counted, "sizeAsOf": size["asOf"] if size else None}


def _confirmation(row: dict, found: dict) -> JSONResponse:
    held = found["objects"] > 0
    detail = ("This bucket still contains data. Type its name to delete it and everything "
              "in it." if held else "Type the bucket name to delete it.")
    return JSONResponse(status_code=409, content={
        "detail": detail, "requiresConfirmation": True, **found, "countsMayLag": True,
        "storageClass": row.get("storage_class")})


def _job_view(job: dict, prefix: str) -> dict[str, Any]:
    view = bucket_delete.job_view(job)
    full = job["bucket"]
    view["bucket"] = full[len(prefix):] if prefix and full.startswith(prefix) else full
    view["fullName"] = full
    return view


@router.delete("/{name}")
async def delete_cloud_bucket(name: str, request: Request, response: Response, acct: Account,
                              user: User, cloud: Cloud, body: DeleteBody | None = None):
    """Every delete needs the typed name (owner 2026-11-25). Empty: revoke the owner
    statement, DeleteBucket. Holding data: a background job (`cloud_delete.py`)."""
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    async with _user_lock(request, acct.uid):
        row = await _own_row(app, cloud, acct, name)
        full = row["name"]
        prefix = full[: len(full) - len(name)]
        prior = await db.run(db.delete_jobs.latest_for, full)
        if prior and prior["state"] in ("queued", "running"):
            return JSONResponse(status_code=409, content={
                "detail": "This bucket is already being deleted.",
                "job": _job_view(prior, prefix)})
        try:
            found = await contents(app, cloud, full)
        except CloudError as e:
            raise cloud_http_error(e) from None
        if body is None or body.confirmName != name:
            return _confirmation(row, found)
        if found["objects"] == 0:
            done = await _delete_empty(app, cloud, acct, user, row, name)
            if done is not None:
                if prior and prior["state"] != "done":  # an earlier failed job is settled
                    await db.run(db.delete_jobs.finish, prior["id"], "done", None)
                return done
            found["objects"] = 1  # old versions or delete markers remain: the job removes them
        if prior and prior["state"] in ("blocked", "failed") and \
                await db.run(db.delete_jobs.requeue, prior["id"]):
            job = await db.run(db.delete_jobs.get, prior["id"])
            await _audit(app, user.username, "cloud.bucket.delete_retry", full, uid=acct.uid,
                         job=job["id"], confirmed=True)
        else:
            try:
                job = await db.run(db.delete_jobs.create, full, acct.uid, user.username,
                                   found["objects"], found["bytes"], "cloud")
            except sqlite3.IntegrityError:
                raise HTTPException(409, "This bucket is already being deleted.") from None
            await _audit(app, user.username, "cloud.bucket.delete_started", full,
                         uid=acct.uid, job=job["id"], objects=found["objects"],
                         bytes=found["bytes"], storageClass=row.get("storage_class"))
    bucket_delete.start(app, job["id"])
    log.info("cloud bucket %s: delete job %s started by %s", full, job["id"], acct.username)
    return JSONResponse(status_code=202, content={"job": _job_view(job, prefix)})


async def _delete_empty(app, cloud: CloudAdmin, acct: RgwAccount, user: SessionUser,
                        row: dict, name: str) -> dict | None:
    """Revoke the owner statement and DeleteBucket. None when S3 says it is not empty (old
    versions or delete markers): the statement is back and the caller starts a job."""
    db: Database = app.state.db
    full = row["name"]
    account = await db.run(db.cloud_accounts.get, acct.username)
    try:
        await cloud.revoke_bucket_owner(full)  # no new uploads from here on
        await cloud.delete_bucket(full)
    except CloudError as e:
        if e.code == "BucketNotEmpty":
            if account:
                try:
                    await cloud.grant_bucket_owner(
                        full, await cloud.user_arn(account["iam_user"]))
                except CloudError as g:
                    log.warning("owner statement of %s not restored: %s", full, g.code)
            return None
        if e.code != "NoSuchBucket":
            raise cloud_http_error(e) from None
    await db.run(db.buckets.delete, full, "cloud")
    _sizes(app).pop(full, None)
    await _audit(app, user.username, "cloud.bucket.delete", full, uid=acct.uid,
                 storageClass=row.get("storage_class"), quotaTiB=row.get("quota_tib"),
                 objects=0, dataDeleted=False)
    from .cloud_sharing import revoke_for_deleted_bucket  # cloud_sharing imports this module

    await revoke_for_deleted_bucket(app, user.username, full, acct.uid)
    log.info("cloud bucket %s deleted by %s", full, acct.username)
    return {"name": name}


@router.get("/delete-jobs")
async def list_delete_jobs(request: Request, response: Response, acct: Account, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    since = (datetime.now(UTC) - bucket_delete.DONE_VISIBLE).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = await db.run(db.delete_jobs.list_for, acct.uid, since, "cloud")
    try:
        prefix = _prefix(cloud, acct.username)
    except CloudError:
        prefix = ""
    return {"jobs": [_job_view(r, prefix) for r in rows]}


@router.post("/delete-jobs/{job_id}/retry")
async def retry_delete_job(job_id: int, request: Request, response: Response, acct: Account,
                           user: User, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    job = await db.run(db.delete_jobs.get, job_id)
    if job is None or job["owner_uid"] != acct.uid or job.get("location") != "cloud":
        raise HTTPException(404, "Delete job not found.")
    previous = job["state"]
    try:
        requeued = previous in ("blocked", "failed") and \
            await db.run(db.delete_jobs.requeue, job_id)
    except sqlite3.IntegrityError:
        requeued = False
    if not requeued:
        raise HTTPException(409, "This job is not waiting for a retry.")
    job = await db.run(db.delete_jobs.get, job_id)
    await _audit(app, user.username, "cloud.bucket.delete_retry", job["bucket"], uid=acct.uid,
                 job=job_id, previous=previous)
    bucket_delete.start(app, job_id)
    try:
        prefix = _prefix(cloud, acct.username)
    except CloudError:
        prefix = ""
    return {"job": _job_view(job, prefix)}


# -- daily over-quota alert -------------------------------------------------------------


async def _owner_email(app, row: dict) -> str | None:
    name = row.get("created_by")
    if not name:
        return None
    try:
        user = await app.state.authenticator.lookup_user(name)
    except Exception:  # noqa: BLE001  left AD, LDAP down: no mail this time
        return None
    return user.email if user.email and "@" in user.email else None


async def alert_tick(app, now: datetime | None = None) -> dict[str, int]:
    """One pass over every cloud bucket. Returns counts (tests, log)."""
    db: Database = app.state.db
    cloud = getattr(app.state, "cloud", None)
    counts = {"checked": 0, "over": 0, "alerted": 0, "tagsRetried": 0}
    if cloud is None or not cloud.available:
        return counts
    now = now or bucket_quota.now_of(app)
    for row in await db.run(db.buckets.list_cloud):
        if not row.get("tag_synced", 1) and await _try_tags(app, row, None):
            counts["tagsRetried"] += 1
        quota = row.get("quota_tib")
        size = await size_of(app, row["name"], fresh=True)
        counts["checked"] += 1
        if not quota or size is None or size["bytes"] <= quota * TIB:
            continue
        counts["over"] += 1
        last = row.get("over_quota_alert_at")
        if last and now - datetime.fromisoformat(last.replace("Z", "+00:00")) < ALERT_EVERY:
            continue
        sent, reason = None, "email_off"
        if app.state.mailer.enabled:
            email = await _owner_email(app, row)
            if email:
                sent, reason = await app.state.mailer.send_over_quota(
                    email, row["name"], size["bytes"] / TIB, quota, size["asOf"]), None
                if not sent:
                    reason = "send_failed"
            else:
                sent, reason = False, "no_address"
        if reason != "send_failed":  # a failed send is retried tomorrow; else next week
            await db.run(db.buckets.set_alerted, row["name"], bucket_quota.iso(now))
            counts["alerted"] += 1
        await _audit(app, "system", "cloud.bucket.over_quota", row["name"],
                     uid=row["owner_uid"], bytes=size["bytes"], sizeAsOf=size["asOf"],
                     quotaTiB=quota, emailed=sent, reason=reason)
    return counts


async def alert_loop(app, sleep=asyncio.sleep) -> None:
    """Lifespan task: a first pass shortly after start, then daily. Never raises."""
    await sleep(ALERT_FIRST_DELAY)
    while True:
        try:
            res = await alert_tick(app)
            log.info("cloud over-quota check: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("cloud over-quota check failed")
        await sleep(ALERT_PERIOD)
