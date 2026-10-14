"""Bucket API under /api/buckets: list, create, delete the signed-in user's buckets.

Every route uses `require_active_rgw_user` (uid only from the session; suspended
users get 403) and, being under the parent `/api` router, CSRF applies to mutations.
Names in URLs and responses are the user's own (short) names; the tenant of the
user's uid is added server-side (`tenant:name` in croit and SQLite). A name with `:`
or capitals never matches anything, so a user cannot address another tenant's bucket.

- `GET /api/buckets` -> `{buckets: [{name, tenant, createdAt|null, objects, bytes,
  quota: {enabled, maxSize, maxObjects}, versioning: off|enabled|suspended|null,
  budgetNumber|null, budgetMissing, tagPending}]}`. Only buckets croit lists for the
  user's uid. Read-only; it never changes state (see `reconcile`).
- `GET /api/buckets/options` -> `{naming: {prefix, hasPattern, hint}, placements: [...]
  (empty unless the user may choose), defaultPlacement, defaultBudgetNumber,
  budgetRequired, budgetExample}`.
- `POST /api/buckets {name, budgetNumber, versioning, objectLock, placement}` -> 201
  `{name, budgetNumber, versioning, objectLock, tagSynced, warning?}`. Name: the admin
  naming rules (`policy_settings.validate_bucket_name`); budget number: required unless
  the session user is an admin (`budget.validate_budget_number`); placement: only from
  the admin-allowed list. The bucket is created through croit, the SQLite row written,
  and the `bucketeer-budget` tag set with the USER's own S3 key (`s3client`). If the tag
  call fails the bucket is kept, the row gets `tag_synced = 0`, and the answer carries a
  `warning`; `POST /api/buckets/reconcile` (called by the UI when the list shows
  `tagPending`) retries those. Name taken anywhere in the cluster -> 409.
- `POST /api/buckets/reconcile` -> `{synced: [...], pending: [...]}`: writes the tag for
  the user's rows with `tag_synced = 0`. A POST because it changes state.
- `DELETE /api/buckets/{name}` `{confirmName?}`: ownership is verified in croit's list for
  the user's uid (404 otherwise). An empty bucket is deleted directly. A non-empty one
  answers 409 `{requiresConfirmation: true, objects, bytes}` unless `confirmName` equals
  the bucket name exactly; then croit's confirmation handshake runs
  (`delete_bucket_and_data(user_confirmed=True)`) and all data goes.
- `PUT /api/buckets/{name}/versioning {enabled}` -> `{name, versioning, changed}`. Through
  croit's `PATCH /s3/buckets/{name}` (admin plane, `versioning` ENABLED / SUSPENDED), after
  ownership is verified in croit's list (404 otherwise). off -> enabled, enabled ->
  suspended, suspended -> enabled; versioning can never go back to off. Already in the
  requested state (including `enabled: false` on a never-versioned bucket) -> 200
  `changed: false`, no croit write. Suspending an object-lock bucket -> 409.
  The list also carries `objectLock: true|false|null` for versioned buckets (read with the
  user's own key; null = unknown).
Audited: `bucket.create`, `bucket.delete`, `bucket.tag_sync`, `bucket.versioning`
(before/after).
"""

import asyncio
import logging
import re
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import s3docs
from .admin import croit_http_error
from .auth import SessionUser, require_user
from .budget import BudgetNumberError, validate_budget_number
from .croit import (
    CroitClient,
    CroitClientError,
    CroitError,
    CroitTaskError,
    CroitTaskFailed,
    CroitWarning,
)
from .policy_settings import (
    load_naming,
    load_placement,
    placement_names,
    placement_options,
    suggest_prefix,
    validate_bucket_name,
)
from .rgw_user import RgwAccount, require_active_rgw_user, split_uid

log = logging.getLogger("bucketeer.buckets")

BUDGET_TAG = "bucketeer-budget"
VERSIONING_LOOKUPS = 100  # buckets per list whose versioning state is fetched
_SHORT_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")

router = APIRouter(prefix="/buckets", tags=["buckets"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=256)
    budgetNumber: str | None = Field(default=None, max_length=4096)
    versioning: bool = False
    objectLock: bool = False
    placement: str | None = Field(default=None, max_length=256)


class VersioningBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmName: str | None = Field(default=None, max_length=256)


# -- helpers ---------------------------------------------------------------------


def _user_lock(request: Request, uid: str) -> asyncio.Lock:
    """One lock per uid, so two creates by the same user cannot overwrite each other's row."""
    state = request.app.state
    if not hasattr(state, "bucket_locks"):
        state.bucket_locks = {}
    return state.bucket_locks.setdefault(uid, asyncio.Lock())


def _full(uid: str, name: str) -> str:
    tenant, _ = split_uid(uid)
    return f"{tenant}:{name}" if tenant else name


def _short(full: str) -> str:
    return full.partition(":")[2] or full


def _own_name(name: str) -> str:
    if not _SHORT_NAME.match(name):
        raise HTTPException(404, "Bucket not found.")
    return name


def _usage(b: dict) -> tuple[int, int]:
    u = b.get("usageSum") or {}
    return int(u.get("numObjects") or 0), int(u.get("size") or 0)


def _quota(q: dict | None) -> dict[str, Any]:
    q = q or {}

    def bound(v: Any) -> int | None:
        return int(v) if isinstance(v, int | float) and v > 0 else None

    return {"enabled": bool(q.get("enabled")), "maxSize": bound(q.get("maxSize")),
            "maxObjects": bound(q.get("maxObjects"))}


async def _own_buckets(croit: CroitClient, uid: str) -> list[dict]:
    """The buckets croit says the user owns (the ownership source of truth)."""
    try:
        rows = await croit.list_user_buckets(uid)
    except CroitClientError as e:
        raise croit_http_error(e) from None
    return [b for b in rows or [] if isinstance(b, dict) and b.get("owner") == uid]


async def _audit(request: Request, actor: str, action: str, target: str, **details):
    db = request.app.state.db
    await db.run(db.audit.add, actor, action, "bucket", target, details)


def _write_budget_tag(s3, bucket: str, value: str) -> None:
    s3docs.update_bucket_tagging(
        s3, bucket, lambda tags: s3docs.upsert_tags(tags, [{"Key": BUDGET_TAG, "Value": value}])
    )


async def _try_tag(request: Request, uid: str, short: str, value: str) -> bool:
    try:
        await request.app.state.s3.call(uid, _write_budget_tag, short, value)
        return True
    except HTTPException as e:
        log.warning("budget tag not written for %s: %s", short, e.detail)
    except Exception as e:  # botocore errors, timeouts: the bucket stays, the tag is retried
        log.warning("budget tag not written for %s: %s", short, type(e).__name__)
    return False


def _object_lock_on(s3, bucket: str) -> bool:
    """Whether the bucket has object lock (sync, user's own client)."""
    try:
        cfg = s3.get_object_lock_configuration(Bucket=bucket)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in (
                "ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"):
            return False
        raise
    return (cfg.get("ObjectLockConfiguration") or {}).get("ObjectLockEnabled") == "Enabled"


_STATES = {"OFF": "off", "ENABLED": "enabled", "SUSPENDED": "suspended"}


def _task_failure(e: CroitTaskError, what: str) -> HTTPException:
    """croit's task output is free-form (hosts, commands, other tenants' names): it goes
    to the server log only, never into the response."""
    if isinstance(e, CroitTaskFailed):
        log.warning("croit task %s failed (%s): state=%s status=%s output=%r", e.task_id, what,
                    e.state, e.status_code, e.output_tail.strip()[-500:])
        return HTTPException(502, f"croit could not {what}. Try again or ask an administrator.")
    log.warning("croit task %s did not finish in time (%s)", e.task_id, what)
    return HTTPException(502, f"croit did not finish the task to {what} in time. "
                              "Check the list and try again.")


async def _placement_view(request: Request) -> dict[str, Any]:
    db, croit = request.app.state.db, request.app.state.croit
    rules = await db.run(load_placement, db.settings)
    try:
        resp = await croit.list_placements()
    except CroitClientError:
        # croit's list is unreadable: no choice, but the admin's configured default still applies
        return {"default": rules.get("default"), "choices": [], "userChoice": False}
    return placement_options(rules, (resp or {}).get("defaultPlacementRule"),
                             placement_names((resp or {}).get("placements") or []))


# -- list / options --------------------------------------------------------------


@router.get("")
async def list_buckets(request: Request, acct: Account):
    croit, db = request.app.state.croit, request.app.state.db
    rows = await _own_buckets(croit, acct.uid)
    meta = {m["name"]: m for m in await db.run(db.buckets.list, acct.uid)}
    sem = asyncio.Semaphore(8)

    async def versioning(b: dict) -> str | None:
        async with sem:
            try:
                state = (await croit.get_bucket(b["bucket"], owner=acct.uid)).get("versioning")
            except (CroitClientError, AttributeError):
                return None
        return {"OFF": "off", "ENABLED": "enabled", "SUSPENDED": "suspended"}.get(state)

    async def locked(b: dict, state: str | None) -> bool | None:
        if state not in ("enabled", "suspended"):
            return False  # object lock requires versioning
        async with sem:
            try:
                return await request.app.state.s3.call(
                    acct.uid, _object_lock_on, _short(b["bucket"]))
            except Exception:
                return None

    rows.sort(key=lambda b: str(b.get("bucket", "")).lower())
    states = await asyncio.gather(*(versioning(b) for b in rows[:VERSIONING_LOOKUPS]))
    locks = await asyncio.gather(*(locked(b, s) for b, s in zip(rows, states, strict=False)))
    out = []
    for i, b in enumerate(rows):
        full = b.get("bucket") or ""
        m = meta.get(full)
        objects, size = _usage(b)
        budget = (m or {}).get("budget_number")
        out.append({
            "name": _short(full),
            "tenant": b.get("tenant") or "",
            "createdAt": (m or {}).get("created_at"),
            "objects": objects,
            "bytes": size,
            "quota": _quota(b.get("bucket_quota")),
            "versioning": states[i] if i < len(states) else None,
            "objectLock": locks[i] if i < len(locks) else None,
            "budgetNumber": budget,
            "budgetMissing": budget is None,
            "tagPending": bool(m and budget is not None and not m.get("tag_synced", 1)),
        })
    return {"buckets": out}


@router.get("/options")
async def options(request: Request, acct: Account, user: User):
    db = request.app.state.db
    naming = await db.run(load_naming, db.settings)
    prefix = suggest_prefix(acct.username, naming)
    pattern = bool(naming.get("pattern"))
    hint = "3-63 lowercase letters, digits, dots and hyphens."
    if prefix:
        hint = f"Starts with \"{prefix}\". " + hint
    if pattern:
        hint += " Must also match the format set by your administrators."
    place = await _placement_view(request)
    profile = await db.run(db.profiles.get, acct.username)
    budget_rules = await db.run(db.settings.get, "budget_number", None)
    example = budget_rules.get("example") if isinstance(budget_rules, dict) else None
    return {
        "naming": {"prefix": prefix, "hasPattern": pattern, "hint": hint},
        "placements": place["choices"] if place["userChoice"] else [],
        "defaultPlacement": place["default"],
        "defaultBudgetNumber": profile["default_budget_number"],
        "budgetRequired": not user.is_admin,
        "budgetExample": example if isinstance(example, str) and example else None,
    }


# -- create ----------------------------------------------------------------------


@router.post("", status_code=201)
async def create_bucket(body: CreateBody, request: Request, acct: Account, user: User):
    croit: CroitClient = request.app.state.croit
    db = request.app.state.db
    name = body.name

    rules = await db.run(load_naming, db.settings)
    problems = await asyncio.to_thread(validate_bucket_name, name, acct.username, rules)
    if problems:
        raise HTTPException(422, " ".join(problems))

    try:
        budget = await asyncio.to_thread(validate_budget_number, body.budgetNumber, db.settings)
    except BudgetNumberError as e:
        raise HTTPException(422, str(e)) from None
    if budget is None and not user.is_admin:
        raise HTTPException(422, "Enter a budget number for this bucket.")

    place = await _placement_view(request)
    placement = body.placement or None
    if placement is not None and placement != place["default"] and not (
        place["userChoice"] and placement in place["choices"]
    ):
        raise HTTPException(422, "That placement is not available to you.")
    placement = placement or place["default"]

    full = _full(acct.uid, name)
    taken = ("A bucket with this name already exists. "
             "Bucket names are unique across the whole storage system.")

    async def existing() -> dict | None:
        return next((b for b in await croit.list_buckets() or [] if b.get("bucket") == full), None)

    async def own_unrecorded() -> bool:
        """The existing bucket is the user's own (per croit) and has no SQLite row at all."""
        found = await existing()
        if found is None or found.get("owner") != acct.uid:
            return False
        return await db.run(db.buckets.get, full) is None

    adopted = False
    async with _user_lock(request, acct.uid):
        try:
            found = await existing()
            if found is not None:
                # e.g. an earlier create timed out after croit made the bucket: finish it
                if not await own_unrecorded():
                    if found.get("owner") == acct.uid:
                        raise HTTPException(409, "You already own this bucket.")
                    raise HTTPException(409, taken)
                adopted = True
            else:
                try:
                    task = await croit.create_bucket(
                        name, acct.uid,
                        versioning="ENABLED" if (body.versioning or body.objectLock) else None,
                        object_lock=True if body.objectLock else None,
                        placement=placement,
                    )
                    await croit.wait_for_task(task)
                except CroitError as e:
                    if e.status != 409 or not await own_unrecorded():
                        raise
                    adopted = True  # lost a race with our own earlier request
        except HTTPException:
            raise
        except CroitError as e:
            if e.status == 409:
                raise HTTPException(409, taken) from None
            raise croit_http_error(e) from None
        except CroitTaskError as e:
            raise _task_failure(e, "create the bucket") from None
        except CroitClientError as e:
            raise croit_http_error(e) from None

        recorded = True
        try:
            await db.run(db.buckets.delete, full)  # a leftover row of an earlier bucket
            await db.run(db.buckets.create, full, acct.uid, budget, user.username,
                         budget is None)  # tag_synced stays 0 until the tag is written
        except Exception:
            log.exception("could not record bucket %s in SQLite", full)
            recorded = False
        tag_ok = True
        if budget is not None:
            tag_ok = await _try_tag(request, acct.uid, name, budget)
            if tag_ok and recorded:
                try:
                    await db.run(db.buckets.set_tag_synced, full, True)
                except Exception:
                    log.exception("could not mark the tag of %s as synced", full)
                    tag_ok = False
        try:
            await _audit(request, user.username, "bucket.create", full, uid=acct.uid,
                         budget=budget, versioning=body.versioning or body.objectLock,
                         objectLock=body.objectLock, tagSynced=tag_ok,
                         placement=None if adopted else placement,
                         recorded=recorded, adopted=adopted)
        except Exception:
            log.exception("could not audit the creation of %s", full)
    versioned = body.versioning or body.objectLock
    out: dict[str, Any] = {
        "name": name, "budgetNumber": budget,
        "versioning": "enabled" if versioned else "off",
        "objectLock": body.objectLock, "tagSynced": tag_ok,
    }
    if adopted:
        out["adopted"] = True
        out["note"] = ("This bucket already existed and is yours, so its budget number was "
                       "recorded now. Versioning and object lock were not changed.")
        out["versioning"] = None
        out["objectLock"] = None
    warnings = []
    if not recorded:
        warnings.append("The bucket was created, but Bucketeer could not record it. "
                        "Ask an administrator to check the budget number.")
    elif not tag_ok:
        warnings.append("The bucket was created, but its budget number could not be saved "
                        "as a bucket tag yet. It will be retried automatically.")
    if warnings:
        out["warning"] = warnings[0]
    return out


# -- reconcile -------------------------------------------------------------------


@router.post("/reconcile")
async def reconcile(request: Request, acct: Account, user: User):
    croit, db = request.app.state.croit, request.app.state.db
    existing = {b.get("bucket") for b in await _own_buckets(croit, acct.uid)}
    todo = [m for m in await db.run(db.buckets.list, acct.uid)
            if not m["tag_synced"] and m["name"] in existing]
    synced, pending = [], []
    for m in todo:
        short = _short(m["name"])
        if m["budget_number"] is None or await _try_tag(request, acct.uid, short,
                                                        m["budget_number"]):
            await db.run(db.buckets.set_tag_synced, m["name"], True)
            await _audit(request, user.username, "bucket.tag_sync", m["name"], uid=acct.uid)
            synced.append(short)
        else:
            pending.append(short)
    return {"synced": synced, "pending": pending}


# -- versioning ------------------------------------------------------------------


@router.put("/{name}/versioning")
async def set_versioning(name: str, body: VersioningBody, request: Request, acct: Account,
                         user: User):
    croit: CroitClient = request.app.state.croit
    short = _own_name(name)
    full = _full(acct.uid, short)
    async with _user_lock(request, acct.uid):
        if next((b for b in await _own_buckets(croit, acct.uid)
                 if b.get("bucket") == full), None) is None:
            raise HTTPException(404, "Bucket not found.")
        try:
            before = _STATES.get((await croit.get_bucket(full, owner=acct.uid)).get("versioning"))
        except CroitClientError as e:
            raise croit_http_error(e) from None
        except AttributeError:
            before = None
        if before is None:
            raise HTTPException(502, "Could not read the versioning state of this bucket.")
        target = "enabled" if body.enabled else ("off" if before == "off" else "suspended")
        if target == before:
            return {"name": short, "versioning": before, "changed": False}
        if target == "suspended":
            try:
                locked = await request.app.state.s3.call(acct.uid, _object_lock_on, short)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Could not check whether this bucket has object "
                                         "lock. Try again.") from None
            if locked:
                raise HTTPException(409, "This bucket has object lock, so versioning cannot be "
                                         "paused.")
        try:
            await croit.update_bucket(full, owner=None, versioning=target.upper())
        except CroitClientError as e:
            raise croit_http_error(e) from None
        try:
            after = _STATES.get((await croit.get_bucket(full, owner=acct.uid)).get("versioning"))
        except Exception:
            after = target
        await _audit(request, user.username, "bucket.versioning", full, uid=acct.uid,
                     before=before, after=after)
    return {"name": short, "versioning": after, "changed": True}


# -- delete ----------------------------------------------------------------------


@router.delete("/{name}")
async def delete_bucket(name: str, request: Request, acct: Account, user: User,
                        body: DeleteBody | None = None):
    croit: CroitClient = request.app.state.croit
    db = request.app.state.db
    short = _own_name(name)
    full = _full(acct.uid, short)
    bucket = next((b for b in await _own_buckets(croit, acct.uid)
                   if b.get("bucket") == full), None)
    if bucket is None:
        raise HTTPException(404, "Bucket not found.")
    objects, size = _usage(bucket)
    confirmed = body is not None and body.confirmName == short
    try:
        if confirmed:
            task = await croit.delete_bucket_and_data(full, user_confirmed=True)
        else:
            task = await croit.delete_bucket(full)
        await croit.wait_for_task(task)
    except CroitWarning:
        return JSONResponse(status_code=409, content={
            "detail": "This bucket still contains data. Type its name to delete it and "
                      "everything in it.",
            "requiresConfirmation": True, "objects": objects, "bytes": size,
        })
    except CroitError as e:
        raise croit_http_error(e) from None
    except CroitTaskError as e:
        raise _task_failure(e, "delete the bucket") from None
    except CroitClientError as e:
        raise croit_http_error(e) from None
    await db.run(db.buckets.delete, full)
    await _audit(request, user.username, "bucket.delete", full, uid=acct.uid,
                 objects=objects, bytes=size, dataDeleted=confirmed and objects > 0)
    return {"name": short}
