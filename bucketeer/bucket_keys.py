"""Bucket keys: S3 keys that work for one bucket only.

An S3 key belongs to a user, not a bucket, so every bucket key is its own RGW user
(`<owner>+<label>`, tenant-qualified like the owner; `+` cannot occur in an AD name, so a
key uid never equals a human's uid) with a zero user quota (it can
create no data of its own) plus one bucket-policy statement (`bucketeer-key-<id>`, via
`s3docs`) that gives it read or read-write access to that single bucket. Writes into
the bucket count against the bucket owner's quota. Ownership is recorded in SQLite
(`bucket_keys`). Only for the signed-in user's OWN buckets (verified in croit); the
uid and owner always come from the session (`require_active_rgw_user`).

- `GET /api/buckets/{name}/keys` and `GET /api/bucket-keys` -> `{keys: [{id, label,
  access, uid, accessKey, bucket, createdAt, suspended, status}]}` (live keys only,
  never a secret). `status` is `active`, `suspended`, `pending` or `failed`; the last two
  mean a create did not finish cleanly and only revoking (a retried cleanup) is useful.
- `POST /api/buckets/{name}/keys {label, access}` -> 201 with one such entry. `label` is
  1-20 of `[a-z0-9-]` (alphanumeric at both ends), `access` is `read` or `readwrite`.
  Limits (QUESTIONS.md Q9): 10 live keys per user, 5 per bucket, else 409.
- `POST /api/bucket-keys/{id}/reveal` -> `{accessKey, secretKey}`, only for the owner of
  that row (anything else is 404), refused for a suspended key; audited
  `bucket_key.reveal` before answering; `Cache-Control: no-store`.
- `DELETE /api/bucket-keys/{id}` -> `{id, revoked: true}`; idempotent for the owner's own
  row. Removes the policy statement first (the key is dead from then on), then deletes
  the RGW user. If croit refuses the delete (e.g. the key user somehow owns buckets) the
  row stays live and the answer tells the user to contact an administrator; the retry is
  safe. A deleted uid is never reused: the revoked row keeps it reserved (`uid_claimed`).

Create is a saga under a per-user and a per-bucket lock: 1) pending SQLite row, 2) croit
user, 3) zero quota (read back and checked, fail closed), 4) one generated access key
(any key croit made on its own is removed), 5) the policy statement with the OWNER's S3
client, 6) row active. A failure at any step rolls back what was done (statement, RGW
user); if that works the row is `failed` and revoked, if it does not the row stays live
as `failed` so a revoke can finish the cleanup. A crash can leave a live `pending` row
the same way; `cleanup_pending` (background task at startup) rolls such rows back.
Audit: `bucket_key.create|create_failed|reveal|revoke|revoke_failed|cleanup`
(never a secret).

Admin (`admin_router`, `/api/admin/bucket-keys`, see the end of this module):
`GET ?state=live|revoked|all&owner=&bucket=` lists keys of all owners (never a secret,
admins cannot reveal either); `DELETE /{id}` revokes any key, audited
`admin.bucket_key.revoke` with the admin as actor.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import s3docs
from .admin import croit_http_error
from .auth import SessionUser, require_admin
from .credentials import _key_locks, generate_key_pair
from .croit import CroitClient, CroitClientError, CroitError
from .rgw_user import RgwAccount, require_active_rgw_user, same_uid, split_uid

log = logging.getLogger("bucketeer.bucket_keys")

MAX_PER_USER = 10  # Q9 default
MAX_PER_BUCKET = 5
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?\Z")
KEY_BARE = re.compile(r"^[a-z0-9][a-z0-9._-]*\+[a-z0-9-]+\Z")  # owner + label, s3docs-safe
_SHORT_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")

router = APIRouter(tags=["bucket-keys"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=64)
    access: str = Field(max_length=16)


# -- helpers ---------------------------------------------------------------------


def sid_for(key_id: int) -> str:
    return s3docs.PREFIX + "key-" + str(key_id)


def key_uid(owner_uid: str, label: str) -> tuple[str, str]:
    """(full RGW uid, bare uid) of the key user for `label`; 422 when it is not a valid uid."""
    tenant, bare = split_uid(owner_uid)
    kb = f"{bare}+{label}"
    if len(kb) > 64 or not KEY_BARE.match(kb):
        raise HTTPException(422, "A key user name could not be built from your account name "
                                 "and this label. Use a shorter label.")
    return (f"{tenant}${kb}" if tenant else kb), kb


def _full(owner_uid: str, short: str) -> str:
    tenant, _ = split_uid(owner_uid)
    return f"{tenant}:{short}" if tenant else short


def _short(full: str) -> str:
    return full.partition(":")[2] or full


def _bucket_name(name: str) -> str:
    if not _SHORT_NAME.match(name):
        raise HTTPException(404, "Bucket not found.")
    return name


def view(row: dict) -> dict[str, Any]:
    status = "suspended" if row["suspended_at"] else row["state"]
    return {
        "id": row["id"], "label": row["label"], "access": row["access"],
        "uid": row["rgw_uid"], "accessKey": row["access_key"],
        "bucket": _short(row["bucket_name"]), "createdAt": row["created_at"],
        "suspended": bool(row["suspended_at"]), "status": status,
    }


def _bucket_lock(request: Request, full: str) -> asyncio.Lock:
    state = request.app.state
    if not hasattr(state, "bucket_key_locks"):
        state.bucket_key_locks = {}
    return state.bucket_key_locks.setdefault(full, asyncio.Lock())


async def _own_bucket(croit: CroitClient, uid: str, full: str) -> dict:
    try:
        rows = await croit.list_user_buckets(uid)
    except CroitClientError as e:
        raise croit_http_error(e) from None
    for b in rows or []:
        if isinstance(b, dict) and b.get("owner") == uid and b.get("bucket") == full:
            return b
    raise HTTPException(404, "Bucket not found.")


async def _audit(request: Request, actor: str, action: str, target: str, **details):
    db = request.app.state.db
    await db.run(db.audit.add, actor, action, "bucket_key", target, details)


async def _policy(request: Request, owner_uid: str, short: str, mutate) -> None:
    """Change the bucket policy as the OWNER (s3docs: foreign statements preserved)."""
    def run(s3):
        return s3docs.update_bucket_policy(s3, short, mutate)

    await request.app.state.s3.call(owner_uid, run)


def _is_no_bucket(e: Exception) -> bool:
    return isinstance(e, ClientError) and e.response.get("Error", {}).get("Code") in (
        "NoSuchBucket", "404")


def _step_error(e: Exception, what: str) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, CroitClientError):
        return croit_http_error(e)
    if isinstance(e, s3docs.S3DocError):
        return HTTPException(409, f"The bucket policy could not be changed safely: {e}")
    log.warning("bucket key: %s failed: %s", what, type(e).__name__)
    return HTTPException(502, f"Could not {what}. Nothing was left behind; try again.")


async def _verify_zero_quota(croit: CroitClient, uid: str) -> None:
    user = await croit.get_user(uid)
    q = (user or {}).get("userQuota") or {}

    def zero(v: Any) -> bool:
        return isinstance(v, int | float) and not isinstance(v, bool) and v == 0

    if not (q.get("enabled") is True and zero(q.get("maxSize")) and zero(q.get("maxObjects"))):
        raise RuntimeError("zero quota not in effect")


async def _put_single_key(croit: CroitClient, uid: str) -> str:
    """Make exactly one access key for the new user (croit may have made one itself)."""
    existing = [k for k in await croit.list_keys(uid) or [] if isinstance(k, dict)
                and k.get("access_key")]
    taken = {k["access_key"] for k in existing}
    access = ""
    for _ in range(5):
        cand, secret = generate_key_pair()
        if cand in taken:
            continue
        try:
            await croit.put_key(uid, cand, secret)
        except CroitError as e:
            if e.status == 409:
                continue
            raise
        access = cand
        break
    if not access:
        raise RuntimeError("no unique access key")
    after = [k["access_key"] for k in await croit.list_keys(uid) or []
             if isinstance(k, dict) and k.get("user") == uid and k.get("access_key")]
    if access not in after:
        raise RuntimeError("new key not confirmed")
    for other in after:
        if other != access:
            await croit.delete_key(uid, other)
    return access


async def _remove_statement(request: Request, owner_uid: str, short: str, sid: str) -> bool:
    """Remove the key's statement; True also when the bucket (or statement) is gone."""
    try:
        await _policy(request, owner_uid, short, lambda p: s3docs.remove_statements(p, sid))
    except Exception as e:
        if _is_no_bucket(e):
            return True
        raise
    return True


async def _delete_key_user(croit: CroitClient, uid: str) -> None:
    try:
        await croit.delete_user(uid)
    except CroitError as e:
        if e.status != 404:  # already gone is fine
            raise


# -- create ----------------------------------------------------------------------


@router.post("/buckets/{name}/keys", status_code=201)
async def create_key(name: str, body: CreateBody, request: Request, response: Response,
                     acct: Account):
    response.headers["Cache-Control"] = "no-store"
    croit: CroitClient = request.app.state.croit
    db = request.app.state.db
    short = _bucket_name(name)
    full = _full(acct.uid, short)
    if not LABEL.match(body.label):
        raise HTTPException(422, "The label must be 1 to 20 characters: lowercase letters, "
                                 "digits and hyphens, starting and ending with a letter or "
                                 "digit.")
    if body.access not in ("read", "readwrite"):
        raise HTTPException(422, "Choose read-only or read and write access.")
    uid, kb = key_uid(acct.uid, body.label)
    tenant, _ = split_uid(acct.uid)
    try:  # fails before anything is created (e.g. an owner uid s3docs would refuse)
        s3docs.bucket_key_statement(short, kb, tenant, body.access, 0)
    except s3docs.S3DocError:
        raise HTTPException(422, "Your account name cannot be used in a bucket policy, so "
                                 "bucket keys are unavailable for it. Ask an administrator.") \
            from None

    async with _key_locks(request)(acct.uid), _bucket_lock(request, full):
        await _own_bucket(croit, acct.uid, full)
        if await db.run(db.bucket_keys.count_live, acct.username) >= MAX_PER_USER:
            raise HTTPException(409, f"You can have at most {MAX_PER_USER} bucket keys. "
                                     "Revoke one you no longer use first.")
        if await db.run(db.bucket_keys.count_live, acct.username, full) >= MAX_PER_BUCKET:
            raise HTTPException(409, f"A bucket can have at most {MAX_PER_BUCKET} keys. "
                                     "Revoke one you no longer use first.")
        taken = HTTPException(409, "You already have (or had) a key with this label. "
                                   "Labels are never reused; choose another.")
        if any(same_uid(r["rgw_uid"], uid) for r in
               await db.run(db.bucket_keys.list, include_revoked=True)):
            raise taken
        try:
            names = await croit.list_user_names()
        except CroitClientError as e:
            raise croit_http_error(e) from None
        if any(same_uid(n, uid) for n in names or []):
            raise taken

        row = await db.run(db.bucket_keys.create, uid, acct.username, full, body.access,
                           body.label, "pending")
        sid = sid_for(row["id"])
        step, statement_tried = "create the key user", False
        user_state = "maybe"  # none: croit refused (not ours); maybe: unknown; ours: created
        display = f"Bucketeer key {body.label} for {acct.username} ({short})"
        try:
            try:
                await croit.create_user(kb, display, None, tenant or None)
            except CroitError as e:
                if e.status in (400, 409, 422):
                    user_state = "none"  # someone else's user, or nothing was made
                raise
            user_state = "ours"
            step = "limit the key user's quota"
            await croit.set_user_quota(uid, enabled=True, max_size=0, max_objects=0)
            await _verify_zero_quota(croit, uid)
            step = "create the access key"
            access_key = await _put_single_key(croit, uid)
            step = "update the bucket policy"
            statement = s3docs.bucket_key_statement(short, kb, tenant, body.access, row["id"])
            statement_tried = True
            await _policy(request, acct.uid, short,
                          lambda p: s3docs.upsert_statements(p, [statement]))
            step = "record the key"
            await db.run(db.bucket_keys.set_state, row["id"], "active", access_key)
        except Exception as e:
            cleaned = await _rollback(request, acct, row, short, sid, statement_tried,
                                      user_state, display)
            try:
                await _audit(request, acct.username, "bucket_key.create_failed", str(row["id"]),
                             uid=uid, bucket=full, step=step, error=type(e).__name__,
                             cleaned=cleaned)
            except Exception:
                log.exception("could not audit the failed key %s", uid)
            err = _step_error(e, step)
            if not cleaned:
                err = HTTPException(
                    502, f"Could not {step}, and the clean-up did not finish. Revoke the key "
                         "in the list to retry the clean-up, or contact an administrator.")
            raise err from None
        await _audit(request, acct.username, "bucket_key.create", str(row["id"]), uid=uid,
                     bucket=full, access=body.access, label=body.label, access_key=access_key)
        return view(await db.run(db.bucket_keys.get, row["id"]))


async def _rollback(request: Request, acct: RgwAccount, row: dict, short: str, sid: str,
                    statement_tried: bool, user_state: str = "ours", display: str = "") -> bool:
    """Undo a failed create. True when nothing of the key is left (row revoked as failed)."""
    croit, db = request.app.state.croit, request.app.state.db
    clean = True
    if statement_tried:
        try:
            await _remove_statement(request, acct.uid, short, sid)
        except Exception as e:
            log.warning("rollback: statement %s not removed: %s", sid, type(e).__name__)
            clean = False
    try:
        if user_state == "maybe":  # e.g. a timeout: delete only a user that is ours
            user = await croit.get_user(row["rgw_uid"])
            if user is not None and str(user.get("name")) != display:
                user_state = "none"
        if user_state != "none":
            await _delete_key_user(croit, row["rgw_uid"])
    except Exception as e:
        log.warning("rollback: user %s not deleted: %s", row["rgw_uid"], type(e).__name__)
        clean = False
    try:
        await db.run(db.bucket_keys.set_state, row["id"], "failed")
        if clean:
            await db.run(db.bucket_keys.revoke, row["id"])
    except Exception:
        log.exception("rollback: could not update row %s", row["id"])
        clean = False
    return clean


# -- list ------------------------------------------------------------------------


@router.get("/buckets/{name}/keys")
async def list_bucket_keys(name: str, request: Request, acct: Account):
    short = _bucket_name(name)
    full = _full(acct.uid, short)
    await _own_bucket(request.app.state.croit, acct.uid, full)
    db = request.app.state.db
    rows = await db.run(db.bucket_keys.list, acct.username, full)
    return {"keys": [view(r) for r in rows]}


@router.get("/bucket-keys")
async def list_all_keys(request: Request, acct: Account):
    db = request.app.state.db
    return {"keys": [view(r) for r in await db.run(db.bucket_keys.list, acct.username)]}


# -- reveal ----------------------------------------------------------------------


async def _own_row(request: Request, acct: RgwAccount, key_id: int) -> dict:
    db = request.app.state.db
    row = await db.run(db.bucket_keys.get, key_id)
    if row is None or row["owner_ad_username"].lower() != acct.username.lower():
        raise HTTPException(404, "Bucket key not found.")
    return row


@router.post("/bucket-keys/{key_id}/reveal")
async def reveal_key(key_id: int, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    row = await _own_row(request, acct, key_id)
    if row["revoked_at"] is not None:
        raise HTTPException(404, "Bucket key not found.")
    if row["suspended_at"]:
        raise HTTPException(403, "This key is suspended. Contact your administrator.")
    if row["state"] != "active" or not row["access_key"]:
        raise HTTPException(409, "This key is not ready. Revoke it and create a new one.")
    try:
        keys = await request.app.state.croit.list_keys(row["rgw_uid"])
    except CroitClientError as e:
        raise croit_http_error(e) from None
    key = next((k for k in keys or [] if isinstance(k, dict) and k.get("user") == row["rgw_uid"]
                and k.get("access_key") == row["access_key"] and k.get("secret_key")), None)
    if key is None:
        raise HTTPException(404, "The key was not found in the storage system.")
    # Audit first: if the log cannot be written, the secret is not handed out.
    await _audit(request, acct.username, "bucket_key.reveal", str(row["id"]),
                 uid=row["rgw_uid"], bucket=row["bucket_name"], access_key=row["access_key"])
    return {"accessKey": key["access_key"], "secretKey": key["secret_key"]}


# -- revoke ----------------------------------------------------------------------


async def revoke_row(request: Request, actor: str, owner_uid: str, row: dict, *,
                     bucket_gone: bool = False, reason: str = "revoke") -> str | None:
    """Revoke one live key (caller holds the locks). Returns None on success, else a
    message for the user; on failure the row stays live and a retry is safe."""
    croit, db = request.app.state.croit, request.app.state.db
    short = _short(row["bucket_name"])
    problem = None
    try:
        if not bucket_gone:
            await _remove_statement(request, owner_uid, short, sid_for(row["id"]))
    except Exception as e:
        log.warning("revoke: statement of key %s not removed: %s", row["id"], type(e).__name__)
        problem = ("Could not remove the key's access from the bucket. The key was not "
                   "deleted; try again.")
        if isinstance(e, HTTPException):
            problem = str(e.detail)
    if problem is None:
        try:
            await _delete_key_user(croit, row["rgw_uid"])
        except CroitError as e:
            log.warning("revoke: user %s not deleted: %s", row["rgw_uid"], e.status)
            problem = ("The key no longer has access to the bucket, but its user could not be "
                       "deleted (it may own buckets of its own). Contact an administrator.")
        except CroitClientError as e:
            log.warning("revoke: user %s not deleted: %s", row["rgw_uid"], type(e).__name__)
            problem = "The storage system is unavailable. Try again shortly."
    if problem is not None:
        await _audit(request, actor, "bucket_key.revoke_failed", str(row["id"]),
                     uid=row["rgw_uid"], bucket=row["bucket_name"], reason=reason)
        return problem
    await db.run(db.bucket_keys.revoke, row["id"])
    await _audit(request, actor, "bucket_key.revoke", str(row["id"]), uid=row["rgw_uid"],
                 bucket=row["bucket_name"], reason=reason, access_key=row["access_key"])
    return None


@router.delete("/bucket-keys/{key_id}")
async def revoke_key(key_id: int, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    row = await _own_row(request, acct, key_id)
    if row["revoked_at"] is not None:
        return {"id": key_id, "revoked": True}
    async with _key_locks(request)(acct.uid), _bucket_lock(request, row["bucket_name"]):
        db = request.app.state.db
        row = await db.run(db.bucket_keys.get, key_id)  # may have changed while waiting
        if row["revoked_at"] is not None:
            return {"id": key_id, "revoked": True}
        problem = await revoke_row(request, acct.username, acct.uid, row)
    if problem:
        raise HTTPException(502, problem)
    return {"id": key_id, "revoked": True}


async def revoke_for_deleted_bucket(request: Request, acct: RgwAccount, full: str) -> dict:
    """After a bucket was deleted through Bucketeer: revoke its keys (no policy to edit
    any more). Never raises; `{revoked: n, failed: [labels]}`."""
    db = request.app.state.db
    revoked, failed = 0, []
    try:
        async with _key_locks(request)(acct.uid), _bucket_lock(request, full):
            for row in await db.run(db.bucket_keys.list, None, full):
                if await revoke_row(request, acct.username, acct.uid, row, bucket_gone=True,
                                    reason="bucket_deleted"):
                    failed.append(row["label"])
                else:
                    revoked += 1
    except Exception:
        log.exception("could not revoke the keys of deleted bucket %s", full)
        failed.append("?")
    return {"revoked": revoked, "failed": failed}


# -- startup clean-up of crashed creates ---------------------------------------------

CLEANUP_ON_START = True  # tests switch it off (autouse fixture in tests/conftest.py)
CLEANUP_GRACE = 600.0  # seconds a `pending` row must be old before it counts as crashed
CLEANUP_DELAYS = (30.0, 120.0, 450.0)  # waits between the (bounded) tries, ~10 minutes


class _Shim:
    """Just enough of a Request/RgwAccount for the shared helpers outside a request."""

    def __init__(self, app, uid: str = ""):
        self.app, self.uid = app, uid


def _pending_age(row: dict) -> float:
    made = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - made).total_seconds()


def _owner_uid(row: dict) -> str:
    return row["rgw_uid"].rsplit("+", 1)[0]


async def _cleanup_row(app, row: dict) -> str:
    """Roll one stale pending row back. Returns 'done', 'needs_cleanup' or 'retry'."""
    croit, db = app.state.croit, app.state.db
    shim = _Shim(app, _owner_uid(row))
    short = _short(row["bucket_name"])
    try:
        await croit.get_user(row["rgw_uid"])  # croit reachable at all?
        try:
            await app.state.s3.client_for(shim.uid)
        except HTTPException as e:
            if e.status_code >= 500:
                return "retry"
            await db.run(db.bucket_keys.set_state, row["id"], "failed")
            await _audit(shim, "system", "bucket_key.cleanup", str(row["id"]),
                         uid=row["rgw_uid"], bucket=row["bucket_name"], result="needs_cleanup",
                         reason="owner has no usable S3 client")
            return "needs_cleanup"
    except CroitClientError:
        return "retry"
    display = f"Bucketeer key {row['label']} for {row['owner_ad_username']} ({short})"
    clean = await _rollback(shim, shim, row, short, sid_for(row["id"]), True, "maybe", display)
    await _audit(shim, "system", "bucket_key.cleanup", str(row["id"]), uid=row["rgw_uid"],
                 bucket=row["bucket_name"], result="rolled_back" if clean else "needs_cleanup")
    return "done" if clean else "needs_cleanup"


async def cleanup_pending(app, grace: float | None = None,
                          delays: tuple[float, ...] | None = None) -> dict[str, int]:
    """Finish (by rolling back) the `pending` rows a crash left behind. Rows younger than
    `grace`, and rows that are active or revoked, are never touched. Rows whose croit was
    unreachable are retried after each of `delays`, then left until the next start."""
    try:
        return await _cleanup_pending(app, grace, delays)
    except Exception:  # CancelledError is not an Exception: shutdown still works
        log.exception("bucket key clean-up failed")
        return {"rolled_back": 0, "needs_cleanup": 0, "left": -1}


async def _cleanup_pending(app, grace, delays) -> dict[str, int]:
    grace = CLEANUP_GRACE if grace is None else grace
    delays = CLEANUP_DELAYS if delays is None else delays
    db = app.state.db
    result = {"rolled_back": 0, "needs_cleanup": 0, "left": 0}
    todo = [r["id"] for r in await db.run(db.bucket_keys.list) if r["state"] == "pending"]
    for attempt in range(len(delays) + 1):
        retry = []
        for key_id in todo:
            row = await db.run(db.bucket_keys.get, key_id)
            if (row["state"] != "pending" or row["revoked_at"] is not None
                    or _pending_age(row) < grace):
                continue
            try:
                async with _key_locks(_Shim(app))(_owner_uid(row)), \
                        _bucket_lock(_Shim(app), row["bucket_name"]):
                    row = await db.run(db.bucket_keys.get, key_id)
                    if row["state"] != "pending" or row["revoked_at"] is not None:
                        continue
                    outcome = await _cleanup_row(app, row)
            except Exception:
                log.exception("clean-up of pending bucket key %s failed", key_id)
                outcome = "retry"
            if outcome == "retry":
                retry.append(key_id)
            else:
                result["rolled_back" if outcome == "done" else "needs_cleanup"] += 1
        todo = retry
        if not todo or attempt == len(delays):
            break
        await asyncio.sleep(delays[attempt])
    result["left"] = len(todo)
    if todo:
        log.warning("bucket key clean-up gave up on %d pending row(s) until next start",
                    len(todo))
    return result


# -- admin: list and revoke any bucket key ---------------------------------------------

admin_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
Admin = Annotated[SessionUser, Depends(require_admin)]


def admin_view(row: dict) -> dict[str, Any]:
    """The admin list entry: public facts only (never a secret)."""
    return {
        "id": row["id"], "ownerAd": row["owner_ad_username"], "ownerUid": _owner_uid(row),
        "bucket": _short(row["bucket_name"]), "bucketFull": row["bucket_name"],
        "label": row["label"], "access": row["access"], "uid": row["rgw_uid"],
        "accessKey": row["access_key"],
        "state": "revoked" if row["revoked_at"] else row["state"],
        "suspended": bool(row["suspended_at"]), "suspendedBy": row["suspended_by"],
        "createdAt": row["created_at"], "revokedAt": row["revoked_at"],
        "statementPendingCleanup": bool(row["statement_pending_cleanup"]),
    }


@admin_router.get("/bucket-keys")
async def admin_list_keys(_a: Admin, request: Request, state: str = "live",
                          owner: str | None = None, bucket: str | None = None):
    if state not in ("live", "revoked", "all"):
        raise HTTPException(422, "state must be live, revoked or all.")
    db = request.app.state.db
    rows = await db.run(db.bucket_keys.list, None, None, state != "live")
    if state == "revoked":
        rows = [r for r in rows if r["revoked_at"]]
    if owner:
        rows = [r for r in rows if r["owner_ad_username"].lower() == owner.lower()]
    if bucket:
        rows = [r for r in rows if bucket.lower() in (r["bucket_name"].lower(),
                                                      _short(r["bucket_name"]).lower())]
    return {"keys": [admin_view(r) for r in rows]}


async def _owner_suspended(croit: CroitClient, owner_uid: str) -> bool:
    try:
        user = await croit.get_user(owner_uid)
    except CroitClientError:
        return False
    return bool((user or {}).get("suspended"))


@admin_router.delete("/bucket-keys/{key_id}")
async def admin_revoke_key(key_id: int, admin: Admin, request: Request, response: Response):
    """Revoke any key. The credential dies first (the key user is deleted in croit; RGW
    refuses a deleted user), then the policy statement is removed with the OWNER's S3
    client. If that cannot be done (owner suspended or without a key, bucket unreachable)
    the harmless dangling statement is left and the row records `statement_pending_cleanup`;
    calling this again on such a revoked row retries the removal. Idempotent."""
    response.headers["Cache-Control"] = "no-store"
    croit, db = request.app.state.croit, request.app.state.db
    row = await db.run(db.bucket_keys.get, key_id)
    if row is None:
        raise HTTPException(404, "Bucket key not found.")
    owner_uid = _owner_uid(row)
    short = _short(row["bucket_name"])
    sid = sid_for(key_id)
    async with _key_locks(request)(owner_uid), _bucket_lock(request, row["bucket_name"]):
        row = await db.run(db.bucket_keys.get, key_id)  # may have changed while waiting
        was_revoked = row["revoked_at"] is not None
        if was_revoked and not row["statement_pending_cleanup"]:
            return {"id": key_id, "revoked": True, "statementRemoved": True}
        if not was_revoked:
            try:  # 1) credential dead
                await _delete_key_user(croit, row["rgw_uid"])
            except CroitClientError as e:
                log.warning("admin revoke: user %s not deleted: %s", row["rgw_uid"],
                            type(e).__name__)
                suspended = False
                try:  # at least stop the credential
                    await croit.update_user(row["rgw_uid"], suspended=True)
                    # "direct" overrides "owner": an owner unsuspend must not wake it
                    await db.run(db.bucket_keys.set_suspended, key_id, True, "direct")
                    suspended = True
                except CroitClientError:
                    pass
                await _audit(request, admin.username, "admin.bucket_key.revoke_failed",
                             str(key_id), uid=row["rgw_uid"], bucket=row["bucket_name"],
                             owner=row["owner_ad_username"], suspended=suspended)
                raise HTTPException(502, "The key user could not be deleted in the storage "
                                         "system. The key was suspended if possible; try "
                                         "again.") from None
        # 2) statement, with the owner's client; skipped when it cannot work
        removed, why = False, ""
        if await _owner_suspended(croit, owner_uid):
            why = "owner suspended"
        else:
            try:
                removed = await _remove_statement(request, owner_uid, short, sid)
            except Exception as e:
                why = ("owner has no usable S3 client" if isinstance(e, HTTPException)
                       else type(e).__name__)
                log.warning("admin revoke: statement %s left: %s", sid, why)
        # 3) record
        if not was_revoked:
            await db.run(db.bucket_keys.revoke, key_id)
        await db.run(db.bucket_keys.set_statement_pending, key_id, not removed)
        await _audit(request, admin.username, "admin.bucket_key.revoke", str(key_id),
                     uid=row["rgw_uid"], bucket=row["bucket_name"],
                     owner=row["owner_ad_username"], statement_removed=removed,
                     **({"statement_left": why} if why else {}),
                     retry=was_revoked, access_key=row["access_key"])
    return {"id": key_id, "revoked": True, "statementRemoved": removed}
