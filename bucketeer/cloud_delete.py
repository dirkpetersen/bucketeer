"""Emptying and deleting a cloud (AWS) bucket that still holds data (P6-13, owner 2026-11-25).

Bucketeer's AWS admin user is explicitly denied object access, so a cloud bucket is emptied
with a TEMPORARY access key of the OWNER's IAM user (its boundary and the bucket's
`bucketeer-owner` statement allow ListBucketVersions, DeleteObject(Version),
ListBucketMultipartUploads and AbortMultipartUpload). The job shares `bucket_delete_jobs`
(`location = 'cloud'`, migration v19) and the runner of `bucket_delete.py` (same states
`queued|running|done|blocked|failed`, same semaphore, resume at startup, retry), which hands
cloud jobs to `run` here. One run, per round (at most `bucket_delete.MAX_ROUNDS`):

1. make sure the `bucketeer-owner` statement is there (a crash may have left it revoked);
2. the owner's IAM user has two keys already (IAM's limit) -> `failed`, asking the person to
   delete one of their cloud keys; else CreateAccessKey (its id, never the secret, is kept in
   `temp_key` so a crash leftover is deleted on the next run), retry while IAM propagates it
   (`KEY_WAITS`), ListObjectVersions + DeleteObjects in batches of 1000 (progress saved after
   each), abort multipart uploads; the key is deleted in `finally`;
3. with the admin user: revoke the owner statement, DeleteBucket. BucketNotEmpty (something
   was written meanwhile) puts the statement back and runs another round.

Versions S3 refuses to remove end the job `failed` with a count (cloud buckets have no
object lock). Done: SQLite row removed, its shares marked revoked
(`cloud_sharing.revoke_for_deleted_bucket`), audit `cloud.bucket.delete` (objects/bytes deleted,
`job`). Other audit: `cloud.bucket.delete_failed` (from `bucket_delete._end`). The secret
lives only in this function's locals and the boto3 client; it is never stored, logged or
audited.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from botocore.exceptions import ClientError

from .bucket_delete import MAX_BATCHES, MAX_ROUNDS, _abort_uploads, _audit, _batch, _Stop
from .cloud import CloudAdmin, CloudError

log = logging.getLogger("bucketeer.cloud_delete")

# A new IAM key takes a few seconds (sometimes up to a minute) to work in S3.
KEY_WAITS: tuple[float, ...] = (2.0, 3.0, 5.0, 10.0, 10.0, 15.0, 15.0)
_PROPAGATION = ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "403")
_HELD: set[str] = set()  # temporary access key ids this process holds right now

TWO_KEYS = ("To empty this bucket Bucketeer needs a temporary cloud key of yours, but you "
            "already have two cloud keys (the AWS limit). Delete one of your cloud keys "
            "under Credentials, Cloud (AWS), then retry.")


def _code(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", ""))


class _KeyClient:
    """Runs S3 calls on the temporary key; while it is new, retries refusals that mean
    "IAM has not propagated the key yet"."""

    def __init__(self, s3):
        self.s3, self.ready = s3, False

    async def call(self, fn, *args):
        waits = list(KEY_WAITS)
        while True:
            try:
                out = await asyncio.to_thread(fn, self.s3, *args)
                self.ready = True
                return out
            except ClientError as e:
                if self.ready or not waits or _code(e) not in _PROPAGATION:
                    raise
                await asyncio.sleep(waits.pop(0))


async def _drop_key(cloud: CloudAdmin, db, job_id: int, iam_user: str, access: str) -> None:
    try:
        await cloud.delete_access_key(iam_user, access)
    except CloudError as e:
        if e.code != "NoSuchEntity":
            log.warning("temporary key of %s not deleted (%s); the next run retries",
                        iam_user, e.code)
            return
    await db.run(db.delete_jobs.set_temp_key, job_id, None)


@asynccontextmanager
async def temporary_key(app, job_id: int, iam_user: str):
    """An S3 client on a key of the owner's IAM user made for this run, deleted afterwards."""
    cloud: CloudAdmin = app.state.cloud
    db = app.state.db
    if len(await cloud.list_access_keys(iam_user)) >= 2:
        raise _Stop(TWO_KEYS)
    made = await cloud.create_access_key(iam_user)
    access = made["accessKey"]
    _HELD.add(access)
    try:
        await db.run(db.delete_jobs.set_temp_key, job_id, access)
        client = cloud.s3_with_key(access, made["secretKey"])
        made = None  # the secret now lives only in the client
        yield _KeyClient(client)
    finally:
        made = None
        _HELD.discard(access)
        await _drop_key(cloud, db, job_id, iam_user, access)


async def _empty(app, job_id: int, iam_user: str, full: str) -> dict[tuple[str, str], str]:
    """Remove every version and delete marker (batches of 1000) and abort uploads; returns
    what S3 refused. Raises `_Ended` when the job was ended elsewhere."""
    db = app.state.db
    refused: dict[tuple[str, str], str] = {}
    async with temporary_key(app, job_id, iam_user) as s3:
        marker = None
        await db.run(db.delete_jobs.progress, job_id, 0, 0, 0)
        for _ in range(MAX_BATCHES):
            res = await s3.call(_batch, full, marker)
            if res.get("gone"):
                return refused
            for code, k, v in res["errors"]:
                refused[(k, v)] = code
            await db.run(db.delete_jobs.progress, job_id, res["deleted"], res["bytes"],
                         len(refused))
            if (await db.run(db.delete_jobs.get, job_id))["state"] != "running":
                raise _Ended
            if not res["more"]:
                break
            if not res["deleted"]:
                if not res["next"] or res["next"] == marker:
                    break
                marker = res["next"]
        else:
            raise _Stop("This bucket is too large to empty in one go. Retry to continue.")
        if not refused:
            await s3.call(_abort_uploads, full)
    return refused


class _Ended(Exception):
    """The job was ended elsewhere: stop quietly."""


async def _owner_check(app, job: dict) -> tuple[dict | None, str]:
    """(the cloud row or None, the owner's IAM user); _Stop when the run must not go on."""
    db = app.state.db
    try:
        owner = await app.state.croit.get_user(job["owner_uid"])
    except Exception:
        raise _Stop("The storage admin API could not be reached. Try again in a minute.") \
            from None
    if owner is not None and owner.get("suspended"):
        raise _Stop("Your account is suspended, so the bucket cannot be emptied. Ask an "
                    "administrator, then retry.")
    row = await db.run(db.buckets.get, job["bucket"], "cloud")
    if row is not None and row["owner_uid"] != job["owner_uid"]:
        raise _Stop("This bucket now belongs to someone else, so it was not emptied.")
    account = await db.run(db.cloud_accounts.get, job["owner_ad"])
    if account is None:
        raise _Stop("Your cloud account is not known to Bucketeer. Ask an administrator.")
    return row, account["iam_user"]


async def run(app, job_id: int) -> None:
    """One run of a cloud job (already marked running). Raises `_Stop` for `failed`."""
    db = app.state.db
    cloud: CloudAdmin | None = getattr(app.state, "cloud", None)
    if cloud is None or not cloud.available:
        raise _Stop("Cloud (AWS) can't be reached right now. Retry in a few minutes.")
    job = await db.run(db.delete_jobs.get, job_id)
    full = job["bucket"]
    row, iam_user = await _owner_check(app, job)
    if job.get("temp_key") and job["temp_key"] not in _HELD:  # a crash leftover
        await _drop_key(cloud, db, job_id, iam_user, job["temp_key"])
    if row is None and await cloud.bucket_status(full) == "free":
        await _finish(app, job_id)  # gone already (an earlier run stopped after DeleteBucket)
        return
    arn = await cloud.user_arn(iam_user)
    try:
        for _ in range(MAX_ROUNDS):
            await cloud.grant_bucket_owner(full, arn)  # the temporary key reaches it this way
            refused = await _empty(app, job_id, iam_user, full)
            if refused:
                codes = ", ".join(sorted({c for c in refused.values() if c})) or "refused"
                raise _Stop(f"{len(refused)} object versions could not be removed ({codes}). "
                            "Retry later.")
            await cloud.revoke_bucket_owner(full)  # no new uploads from here on
            try:
                await cloud.delete_bucket(full)
            except CloudError as e:
                if e.code == "BucketNotEmpty":
                    continue  # written meanwhile: the next round grants access again
                if e.code != "NoSuchBucket":
                    await _regrant(cloud, full, arn)
                    raise
            await _finish(app, job_id)
            return
    except _Ended:
        return
    await _regrant(cloud, full, arn)
    raise _Stop("New objects keep arriving in this bucket, so it could not be emptied. "
                "Stop whatever writes to it, then retry.")


async def _regrant(cloud: CloudAdmin, full: str, arn: str) -> None:
    try:
        await cloud.grant_bucket_owner(full, arn)
    except CloudError as e:
        log.warning("owner statement of %s not restored: %s", full, e.code)


async def _finish(app, job_id: int) -> None:
    db = app.state.db
    job = await db.run(db.delete_jobs.get, job_id)
    full = job["bucket"]
    row = await db.run(db.buckets.get, full, "cloud")
    details: dict[str, Any] = {}
    if row is not None and row["owner_uid"] == job["owner_uid"]:
        details = {"storageClass": row.get("storage_class"), "quotaTiB": row.get("quota_tib")}
        await db.run(db.buckets.delete, full, "cloud")
    getattr(app.state, "cloud_sizes", {}).pop(full, None)
    await db.run(db.delete_jobs.finish, job_id, "done", None)
    job = await db.run(db.delete_jobs.get, job_id)
    await _audit(app, job["owner_ad"], "cloud.bucket.delete", full, uid=job["owner_uid"],
                 objects=job["deleted_objects"], bytes=job["deleted_bytes"],
                 dataDeleted=job["deleted_objects"] > 0, job=job_id, **details)
    from .cloud_sharing import revoke_for_deleted_bucket

    await revoke_for_deleted_bucket(app, job["owner_ad"], full, job["owner_uid"])
    log.info("cloud bucket %s emptied and deleted (job %s)", full, job_id)

