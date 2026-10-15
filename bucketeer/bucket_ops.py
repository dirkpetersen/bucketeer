"""How a bucket is created and its versioning changed, per admin backend (Phase 5 A3).

`buckets.py` calls `ops_for(settings)` and never branches on the backend itself:

- `CroitBucketOps` (`ADMIN_BACKEND=croit`): the croit API (`PUT /s3/buckets/{name}` returns a
  task that is awaited, `PATCH /s3/buckets/{name}` for versioning). Exactly the behaviour
  the portal had before Phase 5.
- `S3BucketOps` (`ADMIN_BACKEND=rgw`): the S3 data plane with the USER'S OWN key, through
  `app.state.s3.call` (never an admin identity; RGW itself enforces who may create and
  what). Admin Ops has no bucket create, and croit's tasks do not exist.

Rgw create, in order (the backend is chosen from the admin CLIENT, `ops_for(client, settings)`,
like placements and the endpoint: `client.backend == "rgw"`):

0. `HeadBucket` with the user's key. On Reef a repeat CreateBucket of a bucket you already
   own answers 200 (`RGWCreateBucket_ObjStore_S3::send_response` maps `-ERR_BUCKET_EXISTS`
   to 0), not `BucketAlreadyOwnedByYou`, so it cannot be told from a fresh create. 200 means
   "exists and you can reach it": raise `CroitError(409)` and let the caller adopt it (own,
   unrecorded) or refuse it (anything else). 403 and 404 go on; other errors fail the call.
1. `CreateBucket`. Placement goes in `CreateBucketConfiguration.LocationConstraint` as
   `:<placement>` (EMPTY api name): RGW checks the part before the colon against the
   zonegroup's api name and skips the check when it is empty, so `S3_REGION` (a client
   setting, `us-east-1` by default) never has to match the cluster. With no placement
   chosen NO `LocationConstraint` is sent, so RGW uses the default placement. Object lock
   goes in `ObjectLockEnabledForBucket=True` (RGW then enables versioning itself, so no
   separate call).
2. `PutBucketVersioning Enabled` when versioning is wanted and object lock is not. If that
   call fails the bucket is kept: after a definite refusal `applied` is False (the answer
   says versioning is off); after a timeout or 5xx `applied` is None (state unknown, the
   warning says so and the audit has `versioningApplied: null`).

The callers add the SQLite row, the budget tag and the `bucketeer-expire-noncurrent` rule.

Errors from the S3 side become `CroitError(409)` for an existing bucket (HeadBucket 200,
`BucketAlreadyOwnedByYou` on releases that answer it, `BucketAlreadyExists`; the caller
decides: a bucket that is the user's own and unrecorded is adopted, any other is "name
taken", exactly like croit's 409), and `S3OpError` for the rest: a status below 500 is a
definite refusal with a clear public message and is not audited; 502/503/504 (RGW 5xx,
unreachable, timeout) mean the outcome is unknown and the caller audits
`bucket.create_failed` / `bucket.versioning_failed`.

Cluster check (TODO.md Phase 5): that RGW accepts `:<placement>` as `LocationConstraint`
with the user's key (fallback: set `S3_REGION` to the zonegroup api name and send
`<api>:<placement>`), and what a repeat CreateBucket of an own bucket answers.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ReadTimeoutError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from fastapi import HTTPException, Request

from .config import Settings
from .croit import CroitClient, CroitError
from .rgw_user import RgwAccount

log = logging.getLogger("bucketeer.bucket_ops")

_EXISTS = ("BucketAlreadyOwnedByYou", "BucketAlreadyExists")
_REFUSALS = {
    "InvalidBucketName": "The storage system does not accept that bucket name.",
    "TooManyBuckets": "You have reached the maximum number of buckets for your account.",
    "InvalidLocationConstraint": "The storage system does not know that placement.",
    "AccessDenied": "The storage system refused this request. Your account may be suspended.",
    "InvalidAccessKeyId": "The storage system rejected your access key. Rotate it on the "
                          "credentials card.",
    "SignatureDoesNotMatch": "The storage system rejected your access key. Rotate it on the "
                             "credentials card.",
    "InvalidBucketState": "The storage system refused the change in the bucket's current state.",
}


class S3OpError(CroitError):
    """A failed S3 data-plane operation with a message that is safe to show the user."""

    def __init__(self, status: int, public: str, code: str | None = None):
        super().__init__(status, None, public)
        self.public, self.s3_code = public, code


def http_error(e: CroitError) -> HTTPException | None:
    """The HTTP answer for an `S3OpError` (None for any other error)."""
    if not isinstance(e, S3OpError):
        return None
    return HTTPException(e.status if e.status in (400, 403, 404) else 502, e.public)


def s3_failure(e: Exception, what: str) -> CroitError:
    """Map a botocore exception to the errors `buckets.py` already handles."""
    if isinstance(e, ClientError):
        err = e.response.get("Error", {})
        code = str(err.get("Code") or "")
        status = int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 500)
        if code in _EXISTS:
            return CroitError(409, None, f"Bucket exists ({code})")
        if status >= 500:
            log.warning("S3 %s failed: %s (HTTP %s)", what, code, status)
            return S3OpError(502, f"The storage system could not {what} ({code or status}). "
                                  "Check the list and try again.", code)
        known = _REFUSALS.get(code)
        log.info("S3 %s refused: %s", what, code)
        msg = known or f"The storage system refused the request to {what} ({code or status})."
        return S3OpError(403 if status == 403 else 400, msg, code)
    if isinstance(e, ReadTimeoutError):
        return S3OpError(504, f"The storage system did not answer in time while trying to "
                              f"{what}. Check the list and try again.")
    if isinstance(e, BotoConnectionError):
        return S3OpError(503, "The storage system is unreachable. Try again later.")
    log.warning("S3 %s failed: %s", what, type(e).__name__)
    return S3OpError(502, f"The storage system could not {what}. Check the list and try again.")


@dataclass
class CreateResult:
    versioned: bool
    warning: str | None = None
    applied: bool | None = None  # versioning step: True done, False refused, None unknown


class BucketOps(Protocol):
    async def create(self, request: Request, acct: RgwAccount, name: str, *, versioned: bool,
                     object_lock: bool, placement: str | None) -> CreateResult: ...

    async def set_versioning(self, request: Request, acct: RgwAccount, full: str, short: str,
                             target: str) -> None: ...


class CroitBucketOps:
    async def create(self, request, acct, name, *, versioned, object_lock, placement):
        croit: CroitClient = request.app.state.croit
        task = await croit.create_bucket(
            name, acct.uid, versioning="ENABLED" if versioned else None,
            object_lock=True if object_lock else None, placement=placement)
        await croit.wait_for_task(task)
        return CreateResult(versioned, applied=True)

    async def set_versioning(self, request, acct, full, short, target):
        await request.app.state.croit.update_bucket(full, owner=None, versioning=target.upper())


def _status(e: ClientError) -> int:
    return int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 500)


def _create_sync(s3, name: str, location: str | None, object_lock: bool, versioned: bool):
    """Returns the versioning step's outcome: True (done or not wanted), False (refused),
    None (unknown: timeout or 5xx)."""
    try:
        s3.head_bucket(Bucket=name)
    except ClientError as e:
        if _status(e) not in (403, 404):
            raise
    else:  # Reef answers 200 to a repeat create of an own bucket: never "create" it again
        raise CroitError(409, None, "Bucket exists (HeadBucket)")
    kwargs: dict[str, Any] = {"Bucket": name}
    if location:
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": location}
    if object_lock:
        kwargs["ObjectLockEnabledForBucket"] = True
    s3.create_bucket(**kwargs)
    if versioned and not object_lock:  # object lock turned versioning on already
        try:
            _put_versioning(s3, name, "Enabled")
        except ClientError as e:
            log.warning("versioning not enabled on new bucket %s: %s", name, type(e).__name__)
            return None if _status(e) >= 500 else False
        except BotoCoreError as e:
            log.warning("versioning not enabled on new bucket %s: %s", name, type(e).__name__)
            return None
    return True


def _put_versioning(s3, name: str, status: str) -> None:
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": status})


class S3BucketOps:
    def __init__(self, settings: Settings):
        self.settings = settings

    def location(self, placement: str | None) -> str | None:
        """`:<placement>` (empty api name, so RGW skips the zonegroup check), or None (no
        constraint: RGW's default placement)."""
        return f":{placement}" if placement else None

    async def create(self, request, acct, name, *, versioned, object_lock, placement):
        try:
            ok = await request.app.state.s3.call(
                acct.uid, _create_sync, name, self.location(placement), object_lock,
                versioned or object_lock)
        except CroitError:
            raise  # exists (409)
        except (ClientError, BotoCoreError) as e:
            raise s3_failure(e, "create the bucket") from None
        if ok:
            return CreateResult(versioned or object_lock, applied=True)
        if ok is None:
            return CreateResult(False, "The bucket was created, but Bucketeer could not tell "
                                       "whether versioning was turned on. Check the "
                                       "versioning switch.", None)
        return CreateResult(False, "The bucket was created, but versioning could not be "
                                   "turned on. Use the versioning switch to enable it.", False)

    async def set_versioning(self, request, acct, full, short, target):
        status = "Enabled" if target == "enabled" else "Suspended"
        try:
            await request.app.state.s3.call(acct.uid, _put_versioning, short, status)
        except (ClientError, BotoCoreError) as e:
            raise s3_failure(e, "change versioning") from None


def ops_for(client: Any, settings: Settings) -> BucketOps:
    """The ops of the admin CLIENT's backend (`client.backend`), never of Settings."""
    return S3BucketOps(settings) if getattr(client, "backend", "croit") == "rgw" \
        else CroitBucketOps()
