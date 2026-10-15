"""Deleting a bucket on the rgw admin backend (Phase 5 A4).

croit deletes a non-empty bucket as one task. Admin Ops has no such thing (and its
`purge-objects` would go around object lock), so on `ADMIN_BACKEND=rgw` a non-empty bucket is
emptied through the S3 data plane with the USER'S OWN key and then deleted, as a background
job whose progress lives in SQLite (`bucket_delete_jobs`, migration v9):

1. page through ListObjectVersions, removing every version and delete marker with DeleteObjects
   (batches of up to 1000, progress saved after each batch);
2. abort the incomplete multipart uploads;
3. DeleteBucket. RGW still enforces object lock and legal hold: versions it refuses to remove
   leave the job `blocked` (count and reason; nothing is forced), and the user can retry later.

States: `queued`, `running`, `done`, `blocked` (locked versions remain), `failed` (a pause the
user can retry: owner suspended, no key, S3 or admin API trouble). Queued and running jobs are
resumed at startup (`RESUME_ON_START`, a background task that never blocks startup or `/health`);
blocked and failed ones wait for a retry. At most one queued/running job per bucket.

API (`delete_on_rgw` is called by `DELETE /api/buckets/{name}` in `buckets.py` when the
backend is rgw; the croit backend keeps its own path):
- `DELETE /api/buckets/{name}` `{confirmName?}`: 409 `{requiresConfirmation, objects, bytes}`
  until `confirmName` equals the name; then 202 `{job}` for a non-empty bucket, or 200
  `{name, keysRevoked?, warning?}` for an empty one (DeleteBucket with the user's key). A bucket
  with a blocked/failed job is retried; one with an active job answers 409 `{job}`.
- `GET /api/buckets/delete-jobs` -> `{jobs: [job]}` (own jobs only: not done, or done within the
  last hour); job = `{id, bucket, state, deletedObjects, deletedBytes, remainingObjects,
  objectsHint, bytesHint, message, attempts, createdAt, updatedAt, finishedAt}`.
- `POST /api/buckets/delete-jobs/{id}/retry` -> `{job}` (own blocked/failed job, else 404/409).

Done: the bucket's keys and shares are revoked exactly as after a croit delete. Audit:
`bucket.delete_started`, `bucket.delete` (objects and bytes deleted), `bucket.delete_blocked`,
`bucket.delete_failed`, `bucket.delete_retry`. Messages hold no secrets.
"""

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import SessionUser, require_user
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.bucket_delete")

AUTOSTART = True  # request handlers start the job task (tests switch it off and drive it)
RESUME_ON_START = True  # the lifespan resumes unfinished jobs (tests switch it off)
BATCH = 1000  # ListObjectVersions page and DeleteObjects batch (S3 maximum)
MAX_BATCHES = 200_000  # hard cap on batches per pass (200 million entries)
MAX_ROUNDS = 5  # empty passes before giving up on a bucket that keeps receiving objects
MAX_PARALLEL = 3  # jobs running at the same time
SAMPLE = 10  # refused versions looked at to describe the lock
DONE_VISIBLE = timedelta(hours=1)

router = APIRouter(prefix="/buckets", tags=["buckets"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]


def uses_jobs(app) -> bool:
    """Only the rgw backend deletes through jobs; croit has its own delete task."""
    return getattr(app.state.croit, "backend", "croit") == "rgw"


def _short(full: str) -> str:
    return full.partition(":")[2] or full


class _Shim:
    """Just enough of a Request for the shared helpers outside a request."""

    def __init__(self, app):
        self.app = app


class _State:
    def __init__(self):
        self.sem = asyncio.Semaphore(MAX_PARALLEL)
        self.tasks: dict[int, asyncio.Task] = {}


def _state(app) -> _State:
    if not hasattr(app.state, "delete_jobs_state"):
        app.state.delete_jobs_state = _State()
    return app.state.delete_jobs_state


def job_view(job: dict) -> dict[str, Any]:
    return {
        "id": job["id"], "bucket": _short(job["bucket"]), "state": job["state"],
        "deletedObjects": job["deleted_objects"], "deletedBytes": job["deleted_bytes"],
        "remainingObjects": job["remaining_objects"],
        "objectsHint": job["objects_hint"], "bytesHint": job["bytes_hint"],
        "message": job["last_error"], "attempts": job["attempts"],
        "createdAt": job["created_at"], "updatedAt": job["updated_at"],
        "finishedAt": job["finished_at"],
    }


async def _audit(app, actor: str, action: str, full: str, **details) -> None:
    db = app.state.db
    try:
        await db.run(db.audit.add, actor, action, "bucket", full, details)
    except Exception:
        log.exception("could not audit %s for %s", action, full)


# -- S3 calls (synchronous: run through `app.state.s3.call`) ------------------------------


def _code(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", ""))


def _batch(s3, bucket: str, marker: tuple[str, str | None] | None) -> dict:
    """One page of versions and delete markers, removed with one DeleteObjects."""
    kw: dict[str, Any] = {"Bucket": bucket, "MaxKeys": BATCH}
    if marker:
        kw["KeyMarker"] = marker[0]
        if marker[1]:
            kw["VersionIdMarker"] = marker[1]
    try:
        page = s3.list_object_versions(**kw)
    except ClientError as e:
        if _code(e) == "NoSuchBucket":
            return {"gone": True}
        raise
    items = [(v["Key"], v.get("VersionId") or "null", int(v.get("Size") or 0))
             for v in page.get("Versions", [])]
    items += [(d["Key"], d.get("VersionId") or "null", 0) for d in page.get("DeleteMarkers", [])]
    errors: list[tuple[str, str, str]] = []
    if items:
        resp = s3.delete_objects(Bucket=bucket, Delete={
            "Objects": [{"Key": k, "VersionId": v} for k, v, _ in items], "Quiet": True})
        by_pair = {(e["Key"], e["VersionId"]): e.get("Code", "")
                   for e in resp.get("Errors", []) if e.get("VersionId")}
        by_key = {e["Key"]: e.get("Code", "") for e in resp.get("Errors", [])
                  if not e.get("VersionId")}  # some servers omit the version id: all of the key
        errors = [(by_pair.get((k, v)) or by_key.get(k, ""), k, v) for k, v, _ in items
                  if (k, v) in by_pair or k in by_key]
    refused = {(k, v) for _, k, v in errors}
    removed = [(k, v, size) for k, v, size in items if (k, v) not in refused]
    # Deleted entries vanish from the listing, so the next page is the same request again,
    # unless nothing on it could be removed: then the caller steps past it with `next`, the
    # last entry of the page (a refused one, so the marker still exists).
    nxt = None
    if page.get("IsTruncated") and page.get("NextKeyMarker"):
        nxt = (page["NextKeyMarker"], page.get("NextVersionIdMarker") or None)
    return {"deleted": len(removed), "bytes": sum(size for _, _, size in removed),
            "errors": errors, "more": bool(page.get("IsTruncated")), "next": nxt,
            "items": len(items)}


def _abort_uploads(s3, bucket: str) -> int:
    """Abort every incomplete multipart upload; returns how many."""
    count = 0
    for _ in range(1000):  # bound: 1000 pages of 1000
        try:
            page = s3.list_multipart_uploads(Bucket=bucket, MaxUploads=BATCH)
        except ClientError as e:
            if _code(e) == "NoSuchBucket":
                return count
            raise
        uploads = page.get("Uploads", [])
        for u in uploads:
            s3.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
            count += 1
        if not uploads:
            break
    return count


def _delete_bucket(s3, bucket: str) -> str:
    """'deleted', 'gone' (it was not there) or 'notempty'."""
    try:
        s3.delete_bucket(Bucket=bucket)
    except ClientError as e:
        code = _code(e)
        if code == "NoSuchBucket":
            return "gone"
        if code == "BucketNotEmpty":
            return "notempty"
        raise
    return "deleted"


def _describe_locks(s3, bucket: str, samples: list[tuple[str, str]]) -> dict:
    """Why were these versions refused: object lock retention and/or legal hold?"""
    until, hold, locked = None, False, 0
    for key, vid in samples:
        try:
            h = s3.head_object(Bucket=bucket, Key=key, VersionId=vid)
        except ClientError:
            continue
        end = h.get("ObjectLockRetainUntilDate")
        active = bool(h.get("ObjectLockMode")) and end is not None and end > datetime.now(UTC)
        on = h.get("ObjectLockLegalHoldStatus") == "ON"
        if active or on:
            locked += 1
        if active and (until is None or end > until):
            until = end
        hold = hold or on
    return {"locked": locked, "until": until, "hold": hold}


def _blocked_message(locked: int, other: int, info: dict, partial: bool) -> str:
    what = "object version is" if locked == 1 else "object versions are"
    parts = []
    if info["until"] is not None:
        parts.append(f"retention {'lasts at least ' if partial else 'lasts '}until "
                     f"{info['until']:%Y-%m-%d %H:%M} UTC")
    if info["hold"]:
        parts.append("a legal hold stays until its owner removes it")
    msg = f"{locked} {what} under object lock or legal hold ({'; '.join(parts) or 'no end date'})."
    if other:
        msg += (f" {other} more could not be removed because of storage errors."
                if other != 1 else " 1 more could not be removed because of a storage error.")
    return (msg + " All other versions were removed. The bucket can be deleted after that: "
            "use Retry then.")


# -- the job ----------------------------------------------------------------------------


class _Stop(Exception):
    """End the run in `failed` with this user-facing message."""


def _failure_text(e: Exception) -> str:
    if isinstance(e, HTTPException):
        return str(e.detail)
    if isinstance(e, ClientError):
        code = _code(e)
        if code in ("AccessDenied", "403"):
            return "The storage system refused the request (access denied)."
        return f"The storage system answered with an error ({code or 'unknown'})."
    if isinstance(e, BotoCoreError):
        return "The storage system could not be reached."
    return "Unexpected error while deleting the bucket."


async def run_job(app, job_id: int) -> None:
    """Run (or resume) one job to done, blocked or failed. Never raises, except for
    cancellation at shutdown, which leaves the job `running` so the next start resumes it."""
    async with _state(app).sem:
        try:
            await _run(app, job_id)
        except asyncio.CancelledError:
            raise
        except _Stop as e:
            await _end(app, job_id, "failed", str(e))
        except Exception as e:
            if not isinstance(e, HTTPException | ClientError | BotoCoreError):
                log.exception("bucket delete job %s crashed", job_id)
            await _end(app, job_id, "failed", _failure_text(e), error=type(e).__name__)
        finally:
            _forget_task(app, job_id)


def _forget_task(app, job_id: int) -> None:
    """Drop this job's task entry (only if it is the current task, never a newer one)."""
    tasks = _state(app).tasks
    if tasks.get(job_id) is asyncio.current_task():
        del tasks[job_id]


async def _end(app, job_id: int, state: str, message: str | None, **details) -> None:
    db = app.state.db
    job = await db.run(db.delete_jobs.get, job_id)
    if job is None or job["state"] not in ("queued", "running"):
        return
    # Before the new state is visible: a retry right after must be able to start a new task.
    _forget_task(app, job_id)
    await db.run(db.delete_jobs.finish, job_id, state, message)
    action = {"failed": "bucket.delete_failed", "blocked": "bucket.delete_blocked"}[state]
    await _audit(app, job["owner_ad"], action, job["bucket"], uid=job["owner_uid"], job=job_id,
                 reason=message, deleted=job["deleted_objects"],
                 remaining=job["remaining_objects"], **details)


class _Moved(Exception):
    """The job was ended by someone else (e.g. superseded): stop quietly."""


async def _check_owner(app, job: dict) -> None:
    """Refuse to touch a bucket that is listed with another owner (no S3 call is made).
    A direct per-bucket lookup (the global list can omit tenant buckets); only a definite
    NoSuchBucket means "gone" (S3 then takes the gone path), any other error ends the run
    for a retry."""
    croit, uid, full = app.state.croit, job["owner_uid"], job["bucket"]
    try:
        if hasattr(croit, "bucket_owner"):
            owner = await croit.bucket_owner(full)
        else:  # croit lists every bucket
            rows = [b for b in await croit.list_buckets() or []
                    if isinstance(b, dict) and b.get("bucket") == full]
            owner = rows[0].get("owner") if rows else None
    except Exception:
        raise _Stop("The storage admin API could not be reached. Try again in a minute.") \
            from None
    if owner is not None and owner != uid:
        raise _Stop("This bucket now belongs to someone else, so it was not emptied.")


async def _run(app, job_id: int) -> None:
    db, s3 = app.state.db, app.state.s3
    job = await db.run(db.delete_jobs.get, job_id)
    if job is None or job["state"] not in ("queued", "running"):
        return
    if not await db.run(db.delete_jobs.start, job_id):
        return
    uid, full = job["owner_uid"], job["bucket"]
    short = _short(full)
    try:
        owner = await app.state.croit.get_user(uid)
    except Exception:
        raise _Stop("The storage admin API could not be reached. Try again in a minute.") \
            from None
    if owner is None:
        raise _Stop("The bucket owner has no storage account any more. Ask an administrator.")
    if owner.get("suspended"):
        raise _Stop("The owner's account is suspended, so the bucket cannot be emptied. "
                    "Ask an administrator, then retry.")
    await _check_owner(app, job)

    for _ in range(MAX_ROUNDS):  # a round ends in BucketNotEmpty only when objects arrive
        refused: dict[tuple[str, str], str] = {}
        removed = 0
        marker = None
        gone = False
        await db.run(db.delete_jobs.progress, job_id, 0, 0, 0)
        for _batch_no in range(MAX_BATCHES):
            res = await s3.call(uid, _batch, short, marker)
            if res.get("gone"):
                gone = True
                break
            for code, k, v in res["errors"]:
                refused[(k, v)] = code
            removed += res["deleted"]
            await db.run(db.delete_jobs.progress, job_id, res["deleted"], res["bytes"],
                         len(refused))
            if (await db.run(db.delete_jobs.get, job_id))["state"] != "running":
                return  # superseded or ended elsewhere
            if not res["more"]:
                break
            if not res["deleted"]:
                # a whole page was refused: step past it, or stop if that is not possible
                if not res["next"] or res["next"] == marker:
                    break
                marker = res["next"]
        else:
            raise _Stop("This bucket is too large to empty in one go. Retry to continue.")
        if refused:
            await _stuck(app, job, short, refused)
            return
        if not gone:
            await _check_owner(app, job)
            await s3.call(uid, _abort_uploads, short)
            outcome = await s3.call(uid, _delete_bucket, short)
            if outcome == "notempty":
                continue  # something was written meanwhile
        await _finish(app, job_id)
        return
    raise _Stop("New objects keep arriving in this bucket, so it could not be emptied. "
                "Stop whatever writes to it, then retry.")


async def _stuck(app, job: dict, short: str, refused: dict) -> None:
    samples = list(refused)[:SAMPLE]
    info = await app.state.s3.call(job["owner_uid"], _describe_locks, short, samples)
    other = sum(1 for c in refused.values() if c not in ("AccessDenied", "403"))
    locked = len(refused) - other
    if info["locked"]:
        msg = _blocked_message(locked, other, info, partial=locked > len(samples))
        await _end(app, job["id"], "blocked", msg)
        return
    codes = ", ".join(sorted({c for c in refused.values() if c})) or "refused"
    await _end(app, job["id"], "failed",
               f"{len(refused)} object versions could not be removed ({codes}).")


async def _finish(app, job_id: int) -> None:
    """The bucket is gone: forget it, revoke its keys and shares, audit."""
    from .bucket_keys import revoke_for_deleted_bucket
    from .sharing import revoke_for_deleted_bucket as revoke_shares

    db = app.state.db
    job = await db.run(db.delete_jobs.get, job_id)
    full, owner = job["bucket"], SimpleNamespace(uid=job["owner_uid"], username=job["owner_ad"])
    # Only the job owner's records: after an ownership change the rest are someone else's.
    row = await db.run(db.buckets.get, full)
    if row is None or row["owner_uid"] == owner.uid:
        await db.run(db.buckets.delete, full)
    keys = await revoke_for_deleted_bucket(_Shim(app), owner, full, only_owner=True)
    await revoke_shares(_Shim(app), owner.username, full, owner_uid=owner.uid)
    warning = None
    if keys["failed"]:
        warning = ("The bucket was deleted, but some of its keys could not be removed yet. "
                   "Ask an administrator to check them.")
    await db.run(db.delete_jobs.finish, job_id, "done", warning)
    job = await db.run(db.delete_jobs.get, job_id)
    await _audit(app, owner.username, "bucket.delete", full, uid=owner.uid,
                 objects=job["deleted_objects"], bytes=job["deleted_bytes"],
                 dataDeleted=job["deleted_objects"] > 0, job=job_id)


def start(app, job_id: int) -> None:
    """Run the job as a background task (no-op when one runs already or AUTOSTART is off)."""
    st = _state(app)
    if not AUTOSTART or job_id in st.tasks:
        return
    st.tasks[job_id] = asyncio.get_running_loop().create_task(run_job(app, job_id))


async def resume(app) -> int:
    """Start every queued/running job (what a restart left behind). Returns how many."""
    db = app.state.db
    try:
        jobs = await db.run(db.delete_jobs.unfinished)
    except Exception:
        log.exception("could not list unfinished bucket delete jobs")
        return 0
    for j in jobs:
        start(app, j["id"])
    return len(jobs)


async def shutdown(app) -> None:
    """Cancel running jobs at shutdown; their rows stay `running` for the next start."""
    tasks = list(_state(app).tasks.values())
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# -- request side -----------------------------------------------------------------------


def _s3_error(e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, ClientError) and _code(e) in ("AccessDenied", "403"):
        return HTTPException(403, "The storage system refused to delete this bucket.")
    if isinstance(e, BotoCoreError):
        return HTTPException(503, "The storage system could not be reached. Try again.")
    return HTTPException(502, "The storage system could not delete the bucket.")


async def _delete_empty(request: Request, acct, user, full: str, short: str, objects: int,
                        size: int) -> dict | None:
    """DeleteBucket with the user's own key. None when the bucket turned out not to be
    empty (stats lag); otherwise the answer."""
    app = request.app
    try:
        outcome = await app.state.s3.call(acct.uid, _delete_bucket, short)
    except Exception as e:
        err = _s3_error(e)
        if err.status_code >= 500:
            await _audit(app, user.username, "bucket.delete_failed", full, uid=acct.uid,
                         error=type(e).__name__, confirmed=False)
        raise err from None
    if outcome == "notempty":
        return None
    db = app.state.db
    await db.run(db.buckets.delete, full)
    await _audit(app, user.username, "bucket.delete", full, uid=acct.uid, objects=objects,
                 bytes=size, dataDeleted=False)
    from .bucket_keys import revoke_for_deleted_bucket
    from .sharing import revoke_for_deleted_bucket as revoke_shares
    keys = await revoke_for_deleted_bucket(request, acct, full)
    await revoke_shares(request, user.username, full)
    out: dict[str, Any] = {"name": short}
    if keys["revoked"]:
        out["keysRevoked"] = keys["revoked"]
    if keys["failed"]:
        out["warning"] = ("The bucket was deleted, but some of its keys could not be removed "
                          "yet. Ask an administrator to check them.")
    return out


def _confirmation(objects: int, size: int) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": "This bucket still contains data. Type its name to delete it and "
                  "everything in it.",
        "requiresConfirmation": True, "objects": objects, "bytes": size})


async def delete_on_rgw(request: Request, acct, user, full: str, short: str, objects: int,
                        size: int, confirmed: bool):
    """The rgw side of `DELETE /api/buckets/{name}` (ownership is already verified)."""
    app, db = request.app, request.app.state.db
    prior = await db.run(db.delete_jobs.latest_for, full)
    if prior and prior["owner_uid"] != acct.uid:
        # another owner's old job: the bucket changed hands, so end it for good
        if prior["state"] != "done":
            await db.run(db.delete_jobs.supersede, prior["id"])
            await _audit(app, user.username, "bucket.delete_superseded", full,
                         uid=prior["owner_uid"], job=prior["id"], previous=prior["state"],
                         newOwner=acct.uid)
        prior = None
    if prior and prior["state"] in ("queued", "running"):
        return JSONResponse(status_code=409, content={
            "detail": "This bucket is already being deleted.", "job": job_view(prior)})
    if objects == 0:
        done = await _delete_empty(request, acct, user, full, short, objects, size)
        if done is not None:
            return done
        objects = max(objects, 1)  # not empty after all (stats lag)
    if not confirmed:
        return _confirmation(objects, size)
    if prior and prior["state"] in ("blocked", "failed") and \
            await db.run(db.delete_jobs.requeue, prior["id"]):
        job = await db.run(db.delete_jobs.get, prior["id"])
        await _audit(app, user.username, "bucket.delete_retry", full, uid=acct.uid,
                     job=job["id"], confirmed=True)
    else:
        try:
            job = await db.run(db.delete_jobs.create, full, acct.uid, user.username,
                               objects, size)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "This bucket is already being deleted.") from None
        await _audit(app, user.username, "bucket.delete_started", full, uid=acct.uid,
                     job=job["id"], objects=objects, bytes=size)
    start(app, job["id"])
    return JSONResponse(status_code=202, content={"job": job_view(job)})


@router.get("/delete-jobs")
async def list_jobs(request: Request, acct: Account):
    db = request.app.state.db
    since = (datetime.now(UTC) - DONE_VISIBLE).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows = await db.run(db.delete_jobs.list_for, acct.uid, since)
    return {"jobs": [job_view(r) for r in rows]}


@router.post("/delete-jobs/{job_id}/retry")
async def retry_job(job_id: int, request: Request, acct: Account, user: User):
    app, db = request.app, request.app.state.db
    job = await db.run(db.delete_jobs.get, job_id)
    if job is None or job["owner_uid"] != acct.uid:
        raise HTTPException(404, "Delete job not found.")
    if job["state"] not in ("blocked", "failed"):
        raise HTTPException(409, "This job is not waiting for a retry.")
    previous = job["state"]
    try:
        requeued = await db.run(db.delete_jobs.requeue, job_id)
    except sqlite3.IntegrityError:
        requeued = False  # another job for this bucket is active
    if not requeued:
        raise HTTPException(409, "This job is not waiting for a retry.")
    job = await db.run(db.delete_jobs.get, job_id)
    await _audit(app, user.username, "bucket.delete_retry", job["bucket"], uid=acct.uid,
                 job=job_id, previous=previous)
    start(app, job_id)
    return {"job": job_view(job)}
