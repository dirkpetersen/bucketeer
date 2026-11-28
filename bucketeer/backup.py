"""Nightly backup of the database and `.env` to an object-locked bucket (admin, PLAN 5).

`BACKUP_BUCKET` (empty = off; the log and the Backup tab then suggest
`<CLOUD_BUCKET_PREFIX>backup-<12 hex>`, since the app never writes `.env`) names the bucket.
While cloud is configured the SAME name is the bucket in AWS, so the name must be
`<CLOUD_BUCKET_PREFIX>backup-<12 lowercase hex>` (`config.Settings` refuses another name). On
premises the bucket belongs to the RGW user
`bucketeer-backup` (`croit.BACKUP_UID`), created through the admin client on the first run;
its key is read from the admin client each time (never stored) and, if it has none, one is
generated (`credentials.generate_key_pair`). That user is hidden from the admin lists and
refused as a login, mapping, share or ACL target like the Admin Ops service user.

The bucket is created once (idempotent) with that key: object lock at create, default
retention COMPLIANCE `retentionDays`, and the lifecycle rules `bucketeer-backup-expire`
(current objects after retention + 1 days), `bucketeer-backup-noncurrent` (noncurrent
versions after 1 day) and `bucketeer-backup-markers` (expired delete markers), written through
`s3docs.update_bucket_lifecycle`. Through S3 nobody, not even Bucketeer, can delete or shorten
a backup before it rolls off. Retention can only be lengthened; the lock period applies to
archives written from then on (older ones keep theirs), the lifecycle to all.

Each run uploads `backup/bucketeer-YYYY-MM-DDTHHMMZ.tar.gz` (UTC) holding `bucketeer.sqlite3`
(a consistent snapshot, sqlite backup API), `.env` (the file `load_settings` read: `./.env`;
left out with a note when absent) and `meta.json` (`format`, `createdAt`, `appVersion`,
`schemaVersion`, `tables` {name: rows}, `files` {name: {sha256, bytes}}, `envIncluded`,
`note`). Not encrypted (Q21): the bucket must stay private. A successful run also removes the
database's pre-upgrade copies (`db.remove_upgrade_copies`). One run at a time.

Destinations (same methods `ensure`, `inspect`, `upload`, `download`, `latest`): every run
writes the SAME archive to `OnPremDestination` (RGW, the backup user's key, `S3_ENDPOINT_URL`
or croit's endpoint discovery) and, while cloud is configured, to `AwsDestination` (P6-12).
Each destination is tried on its own: the run is `ok` only when all of them got the archive;
a failure at one is recorded (`destinations[]`, `error`, audit `backup.failed`) and the other
still gets its copy. `BACKUP_BUCKET` stays the on/off switch for both.

`AwsDestination`: the bucket is `BACKUP_BUCKET` itself, in `CLOUD_REGION`. The CloudAdmin
creates it with object lock when it is free, then (every run, idempotent) Block Public Access,
SSE-S3, the tags `bucketeer-bucket`, `bucketeer-product=system`, `bucketeer-location=cloud`,
the same COMPLIANCE retention and lifecycle rules. A name taken meanwhile (by another account,
or one of ours not created by this code) is replaced by a fresh one. The admin user has an
explicit Deny on object reads and writes, so objects go through the IAM user
`<CLOUD_USER_PREFIX>bucketeer-backup` (path `/bucketeer/`, the `BucketeerUserBoundary`
boundary, inline policy `bucketeer-backup`: PutObject, GetObject, GetObjectVersion on the
bucket's objects, ListBucket and GetBucketLocation on the bucket). No secret is ever stored:
each upload or download deletes the user's keys this process does not hold (leftovers of a
crash), creates one key, uses it (retrying while IAM propagates it) and deletes it in a
`finally`. Listing (`latest`) and `inspect` use the admin user (ListBucket is allowed).

API (`/api/admin/backup`, admin, CSRF via the parent router):

* `GET` -> `{configured, bucket, suggestedBucket (only while not configured), onpremBucket,
  awsBucket (null unless cloud is configured), region, user, settings: {time, retentionDays,
  restoreTestDays}, destinations: [{kind onprem|cloud, bucket, user, state: {exists,
  objectLock, mode, retentionDays, versioning, error, note} | null}], nextRun, running,
  lastRun, lastSuccess, runs: [...], testing, restoreTests: [...], nextRestoreTest}`; never
  changes anything (no user or bucket is created by a GET).
* `PUT /settings {time?, retentionDays?, restoreTestDays?}` -> `{settings, warning?}`; time
  `HH:MM` (server local), retentionDays 1..36500 and never below the current value,
  restoreTestDays 0..365 (0 = no automatic restore test) (422); audit `admin.settings.backup`
  (target_type `setting`) when changed; a longer retention is applied to the bucket at once
  (created if needed; best effort, `warning` on failure, the next run applies it).
* `POST /run` -> the finished run `{id, startedAt, finishedAt, trigger manual|schedule, actor,
  outcome ok|failed, key, bytes, sha256, schemaVersion, envIncluded, destinations: [{kind,
  bucket, outcome ok|failed, error, bucketCreated, retentionDays}], error}`; 409 while
  backups are off or a run is going. Audit `backup.run` / `backup.failed` once per
  destination (target_type `backup`, target that bucket; actor `system` for the schedule).
  Runs are kept in the settings key `backup_runs` (newest first, the last 30). A failed run
  emails the alert contacts (`alerts.py`, kind `backup_failed`).
* `POST /restore-test` -> the finished restore test `{id, startedAt, finishedAt, trigger
  manual|schedule, actor, outcome ok|failed, error, destinations: [{kind, bucket, key, outcome
  ok|failed, error, checkedAt, schemaVersion, tables {name: rows}, createdAt, envIncluded}]}`;
  409 while backups are off, a backup runs or a test runs (one at a time, shielded like a run).

Restore test (`restore_test`): proves that the NEWEST archive in each destination can be
restored without touching the live database or `.env`: download into a temporary folder under
`DATA_DIR`, `read_archive` (every SHA-256 listed in meta.json), open the database copy read-only,
`PRAGMA integrity_check`, schema version not newer than this code (`len(db.MIGRATIONS)`) and
equal to meta.json's, row counts equal to meta.json's; the folder is removed afterwards. It runs
by hand or right after a successful SCHEDULED backup when the newest test is at least
`restoreTestDays` calendar days old or failed. The last 12 tests are kept in the settings key
`backup_restore_tests` (newest first). Audit `backup.restore_test` / `backup.restore_test_failed`
once per destination (target_type `backup`, target the bucket; details trigger, destination, key,
error, schemaVersion; actor `system` when automatic); a failed test emails the alert contacts
(kind `restore_test_failed`). The schedule loop also emails them (kind `backup_stale`) when no
backup succeeded for more than 48 hours, at most once per 24 hours (settings key
`backup_stale_alert` `{at, since}`).

Restore: `python3 app.py --restore-backup <object key|latest> [--from onprem|cloud]
[--aws-bucket NAME] [--backup-bucket NAME] [--force]` (`restore_main`). Without `--from` the
source is on premises: the bucket `--backup-bucket` (an older on-premises bucket, e.g. the one
used before `BACKUP_BUCKET` was renamed; checked with `check_dns_name`, refused with
`--from cloud`), else `BACKUP_BUCKET`. From the cloud the bucket is `--aws-bucket`, else
`BACKUP_BUCKET`, else the one `<prefix>backup-*` bucket in the account; the download uses a
temporary key exactly like a run.
"""

import asyncio
import base64
import contextlib
import hashlib
import importlib
import inspect
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from . import alerts, s3docs
from .auth import SessionUser, require_admin
from .cloud import USER_PATH, CloudError, NotConfigured
from .config import Settings
from .credentials import generate_key_pair
from .croit import BACKUP_UID, CroitError, CroitUnavailable
from .db import DB_FILENAME, MIGRATIONS, Database, get_db
from .policy_settings import check_dns_name
from .s3client import default_factory

log = logging.getLogger("bucketeer.backup")

SCHEDULE_ON_START = True  # tests switch the nightly scheduler off and call run()
TICK = 60.0
KEY = "backup"
RUNS_KEY = "backup_runs"
KEEP_RUNS = 30
TESTS_KEY = "backup_restore_tests"
KEEP_TESTS = 12
STALE_KEY = "backup_stale_alert"
STALE_AFTER = timedelta(hours=48)  # no successful backup for longer: alert
STALE_REPEAT = timedelta(hours=24)  # ... at most this often
DEFAULTS = {"time": "03:00", "retentionDays": 30, "restoreTestDays": 30}
MAX_RETENTION = 36500
MAX_TEST_DAYS = 365
PREFIX = "backup/"
ARCHIVE_RE = re.compile(r"^backup/bucketeer-\d{4}-\d{2}-\d{2}T\d{4}Z\.tar\.gz\Z")
DB_MEMBER, ENV_MEMBER, META_MEMBER = DB_FILENAME, ".env", "meta.json"
RULE_IDS = ("bucketeer-backup-expire", "bucketeer-backup-noncurrent", "bucketeer-backup-markers")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\Z")
ENV_FILE: Path | None = None  # tests point it elsewhere; None = ./.env (what load_settings read)
REPO = Path(__file__).resolve().parent.parent


class BackupError(Exception):
    """A failure with a message fit for the admin page and the audit log (no secrets)."""


class AlreadyRunning(Exception):
    pass


def env_path() -> Path:
    return ENV_FILE or Path.cwd() / ".env"


def suggest_bucket(prefix: str = "bucketeer-") -> str:
    """A free-looking name in the form cloud backups need (see config.Settings)."""
    return f"{prefix}backup-{secrets.token_hex(6)}"


def archive_key(at: datetime) -> str:
    return f"{PREFIX}bucketeer-{at.astimezone(UTC):%Y-%m-%dT%H%M}Z.tar.gz"


def load(db: Database) -> dict[str, Any]:
    saved = db.settings.get(KEY) or {}
    out = dict(DEFAULTS)
    if _TIME.match(str(saved.get("time", ""))):
        out["time"] = saved["time"]
    days = saved.get("retentionDays")
    if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= MAX_RETENTION:
        out["retentionDays"] = days
    every = saved.get("restoreTestDays")
    if isinstance(every, int) and not isinstance(every, bool) and 0 <= every <= MAX_TEST_DAYS:
        out["restoreTestDays"] = every
    return out


def local_time(iso: Any) -> datetime | None:
    """A stored ISO time as naive server-local time (what `now()` and `next_run` use)."""
    try:
        return datetime.fromisoformat(str(iso)).astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def next_run(now: datetime, hhmm: str) -> datetime:
    """The first HH:MM strictly after `now` (naive server-local time)."""
    m = _TIME.match(hhmm)
    if not m:
        raise ValueError(f"bad time {hhmm!r}")
    at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def describe(exc: BaseException) -> str:
    """A short reason without secrets (admin error texts are scrubbed by the clients)."""
    if isinstance(exc, BackupError):
        return str(exc)
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"S3 refused {exc.operation_name}: {err.get('Code') or 'error'}"
    if isinstance(exc, BotoCoreError):
        return f"S3 not reachable ({type(exc).__name__})"
    if isinstance(exc, CloudError):  # its text names the operation and AWS's code only
        return exc.message
    if isinstance(exc, CroitError):
        return f"admin API error {exc.status}: {exc.message}"[:300]
    if isinstance(exc, CroitUnavailable):
        return "the admin API is not reachable"
    return f"{type(exc).__name__}"


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "")


def _status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)


# -- the bucket (sync boto3, run in a thread) ----------------------------------------------


def lifecycle_rules(days: int) -> list[dict]:
    every = {"Status": "Enabled", "Filter": {"Prefix": ""}}
    return [
        {"ID": RULE_IDS[0], **every, "Expiration": {"Days": days + 1}},
        {"ID": RULE_IDS[1], **every, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
        {"ID": RULE_IDS[2], **every, "Expiration": {"ExpiredObjectDeleteMarker": True}},
    ]


def _lock_state(s3, bucket: str) -> tuple[bool, str | None, int | None]:
    try:
        cfg = s3.get_object_lock_configuration(Bucket=bucket).get("ObjectLockConfiguration") or {}
    except ClientError as e:
        if "ObjectLockConfiguration" in _code(e) or _status(e) == 404:
            return False, None, None
        raise
    ret = (cfg.get("Rule") or {}).get("DefaultRetention") or {}
    days = ret.get("Days") or (ret["Years"] * 365 if ret.get("Years") else None)
    return cfg.get("ObjectLockEnabled") == "Enabled", ret.get("Mode"), days


def ensure_bucket(s3, bucket: str, days: int) -> dict[str, Any]:
    """Create the bucket if needed and (re)apply lock and lifecycle. Never shortens."""
    created = False
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if _status(e) in (401, 403) or _code(e) in ("403", "AccessDenied", "Forbidden"):
            raise BackupError(f"The bucket {bucket} exists but belongs to another account. "
                              "Choose another BACKUP_BUCKET name.") from None
        if _status(e) != 404 and _code(e) not in ("404", "NoSuchBucket", "NotFound"):
            raise
        try:
            s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
            created = True
            log.info("created backup bucket %s with object lock", bucket)
        except ClientError as e2:
            if _code(e2) == "BucketAlreadyExists":
                raise BackupError(f"The bucket name {bucket} is taken by another account. "
                                  "Choose another BACKUP_BUCKET name.") from None
            if _code(e2) != "BucketAlreadyOwnedByYou":
                raise
    return {"created": created, "retentionDays": apply_lock_and_lifecycle(s3, bucket, days)}


def apply_lock_and_lifecycle(s3, bucket: str, days: int) -> int:
    """COMPLIANCE default retention (never shortened) and the lifecycle rules; returns the
    retention in force."""
    enabled, mode, current = _lock_state(s3, bucket)
    if not enabled:
        raise BackupError(f"The bucket {bucket} has no object lock (it was not created by "
                          "Bucketeer). Choose a new backup bucket name.")
    effective = max(days, current or 0)
    if mode != "COMPLIANCE" or current != effective:
        s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration={
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": effective}}})
    s3docs.update_bucket_lifecycle(
        s3, bucket, lambda cur: s3docs.upsert_rules(cur, lifecycle_rules(effective)))
    return effective


def inspect_bucket(s3, bucket: str) -> dict[str, Any]:
    out: dict[str, Any] = {"exists": False}
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if _status(e) == 404 or _code(e) in ("404", "NoSuchBucket", "NotFound"):
            out["note"] = "The first backup run creates it."
            return out
        if _status(e) in (401, 403):
            out["error"] = "The bucket exists but belongs to another account."
            return out
        raise
    enabled, mode, days = _lock_state(s3, bucket)
    versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status") or "Off"
    out.update(exists=True, objectLock=enabled, mode=mode, retentionDays=days,
               versioning=versioning.lower())
    return out


def upload_file(s3, bucket: str, key: str, path: Path) -> None:
    data = path.read_bytes()
    md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
    # object lock with a default retention: S3 wants Content-MD5 (or a checksum) on PUT
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentMD5=md5,
                  ContentType="application/gzip")
    head = s3.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength") or -1) != len(data):
        raise BackupError("The uploaded archive has the wrong size.")


def latest_key(s3, bucket: str) -> str | None:
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        keys += [o["Key"] for o in page.get("Contents") or [] if ARCHIVE_RE.match(o["Key"])]
    return max(keys) if keys else None


# -- destinations ---------------------------------------------------------------------------


class OnPremDestination:
    """The backup bucket on the Ceph cluster, owned by the RGW user `bucketeer-backup`."""

    kind = "onprem"

    def __init__(self, client, settings: Settings, endpoints,
                 factory: Callable[..., Any] = default_factory, bucket: str | None = None):
        self.client, self.settings, self.endpoints = client, settings, endpoints
        self.factory = factory
        self.bucket = bucket or settings.backup_bucket  # `bucket`: restore --backup-bucket

    async def _key(self, create: bool) -> tuple[str, str] | None:
        """The backup user's first key (creating user and key when `create`)."""
        c = self.client
        user = await c.get_user(BACKUP_UID)
        if user is None:
            if not create:
                return None
            try:
                await c.create_user(BACKUP_UID, "Bucketeer backups")
            except CroitError as e:
                if e.status not in (400, 409, 422) or await c.get_user(BACKUP_UID) is None:
                    raise
            log.info("created the RGW user %s for backups", BACKUP_UID)
            user = await c.get_user(BACKUP_UID) or {}
        if user.get("suspended"):
            raise BackupError(f"The RGW user {BACKUP_UID} is suspended in the cluster.")

        def own(keys):
            return [k for k in keys or [] if isinstance(k, dict) and k.get("user") == BACKUP_UID
                    and k.get("access_key") and k.get("secret_key")]

        keys = own(await c.list_keys(BACKUP_UID))
        if not keys:
            if not create:
                return None
            access, secret = generate_key_pair()
            await c.put_key(BACKUP_UID, access, secret)
            keys = own(await c.list_keys(BACKUP_UID))
            if not keys:
                raise BackupError(f"Could not give the RGW user {BACKUP_UID} an access key.")
        return keys[0]["access_key"], keys[0]["secret_key"]

    async def _s3(self, create: bool = True):
        key = await self._key(create)
        if key is None:
            return None
        endpoint = await self.endpoints.resolve(BACKUP_UID)
        if not endpoint:
            raise BackupError("The S3 endpoint is not known (set S3_ENDPOINT_URL).")
        return self.factory(endpoint, key[0], key[1], self.settings)

    async def ensure(self, retention_days: int) -> dict[str, Any]:
        s3 = await self._s3()
        return await asyncio.to_thread(ensure_bucket, s3, self.bucket, retention_days)

    async def inspect(self) -> dict[str, Any]:
        s3 = await self._s3(create=False)
        if s3 is None:
            return {"exists": False, "note": "The first backup run creates the backup "
                                             "account and the bucket."}
        return await asyncio.to_thread(inspect_bucket, s3, self.bucket)

    async def upload(self, key: str, path: Path) -> None:
        s3 = await self._s3()
        await asyncio.to_thread(upload_file, s3, self.bucket, key, path)

    async def latest(self) -> str | None:
        s3 = await self._s3(create=False)
        if s3 is None:
            return None
        return await asyncio.to_thread(latest_key, s3, self.bucket)

    async def download(self, key: str, path: Path) -> None:
        s3 = await self._s3(create=False)
        if s3 is None:
            raise BackupError(f"The RGW user {BACKUP_UID} or its key does not exist.")
        await asyncio.to_thread(s3.download_file, self.bucket, key, str(path))

    @property
    def owner(self) -> str:
        return BACKUP_UID


# -- AWS (P6-12) ------------------------------------------------------------------------------

AWS_POLICY_NAME = "bucketeer-backup"
# A new IAM key takes a few seconds to work in S3: wait this long between attempts.
KEY_WAITS: tuple[float, ...] = (2.0, 3.0, 5.0, 10.0, 10.0)
_PROPAGATION_CODES = ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "403",
                      "Forbidden")
_HELD_KEYS: set[str] = set()  # access key ids whose secret this process holds right now


def _save_settings(db: Database, values: dict[str, Any], actor: str) -> None:
    """Time and retention."""
    with db.tx():
        saved = dict(db.settings.get(KEY) or {})
        saved.update(values)
        db.settings.set(KEY, saved, actor)


def aws_user_policy(bucket: str) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "BackupBucket", "Effect": "Allow",
         "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": f"arn:aws:s3:::{bucket}"},
        {"Sid": "BackupObjects", "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"],
         "Resource": f"arn:aws:s3:::{bucket}/*"},
    ]}


def aws_bucket_tags(bucket: str) -> list[dict[str, str]]:
    return [{"Key": "bucketeer-bucket", "Value": bucket},
            {"Key": "bucketeer-product", "Value": "system"},
            {"Key": "bucketeer-location", "Value": "cloud"}]


def setup_aws_bucket(s3, bucket: str, days: int) -> int:
    """Everything after the create (admin client, idempotent); returns the retention."""
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3docs.update_bucket_tagging(s3, bucket,
                                 lambda cur: s3docs.upsert_tags(cur, aws_bucket_tags(bucket)))
    return apply_lock_and_lifecycle(s3, bucket, days)


def _retry_new_key(fn: Callable[..., Any], *args) -> Any:
    """Run `fn`, retrying while a just-created IAM key is not yet known to S3."""
    import time

    for wait in (*KEY_WAITS, None):
        try:
            return fn(*args)
        except ClientError as e:
            if wait is None or _code(e) not in _PROPAGATION_CODES:
                raise
            time.sleep(wait)
    return None  # pragma: no cover


_AWS_TAKEN = ("The name {name} (BACKUP_BUCKET) is already taken by another AWS account. "
              "Choose another name of the form <CLOUD_BUCKET_PREFIX>backup-<12 hex> and set it "
              "in BACKUP_BUCKET.")


class AwsDestination:
    """The backup bucket in AWS: the same name as on premises (BACKUP_BUCKET), set up by the
    CloudAdmin and written by the backup IAM user through a key that exists only for one
    upload or download."""

    kind = "cloud"

    def __init__(self, cloud, bucket: str):
        self.cloud = cloud
        self.bucket = bucket
        self.owner = cloud.backup_user_name()

    async def _ensure_bucket(self) -> bool:
        """Create the bucket with object lock if the name is free; True when this call did."""
        c, name = self.cloud, self.bucket
        status = await c.bucket_status(name)
        if status == "exists":
            return False
        if status in ("taken", "elsewhere"):
            raise BackupError(_AWS_TAKEN.format(name=name))
        params: dict[str, Any] = {"Bucket": name, "ObjectLockEnabledForBucket": True}
        if c.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": c.region}
        try:
            await c.call("s3", "create_bucket", **params)
        except CloudError as e:
            if e.code == "BucketAlreadyOwnedByYou":
                return False
            if e.code == "BucketAlreadyExists":
                raise BackupError(_AWS_TAKEN.format(name=name)) from None
            raise
        log.info("created the AWS backup bucket %s in %s with object lock", name, c.region)
        return True

    async def _ensure_user(self, bucket: str) -> None:
        c = self.cloud

        boundary = c.boundary_arn((await c.caller_identity())["account"])
        user = await c.get_user(self.owner)
        if user is None:
            try:
                await c.call("iam", "create_user", UserName=self.owner, Path=USER_PATH,
                             PermissionsBoundary=boundary,
                             Tags=[{"Key": "bucketeer-role", "Value": "backup"}])
            except CloudError as e:
                if e.code != "EntityAlreadyExists":
                    raise
                raise BackupError(
                    f"The backup IAM user name {self.owner} already exists outside "
                    f"Bucketeer's path {USER_PATH}, or Bucketeer may not read it. An AWS "
                    "administrator must check that user (or re-run tools/aws-setup).") from None
            log.info("created the backup IAM user %s", self.owner)
        # (the admin may create users under /bucketeer/ only WITH the boundary, so an
        # existing one has it unless an AWS administrator made it by hand)
        await c.call("iam", "put_user_policy", UserName=self.owner, PolicyName=AWS_POLICY_NAME,
                     PolicyDocument=json.dumps(aws_user_policy(bucket)))

    async def ensure(self, retention_days: int) -> dict[str, Any]:
        made = await self._ensure_bucket()
        days = await asyncio.to_thread(setup_aws_bucket, self.cloud.client("s3"), self.bucket,
                                       retention_days)
        await self._ensure_user(self.bucket)
        return {"created": made, "retentionDays": days}

    async def inspect(self) -> dict[str, Any]:
        return await asyncio.to_thread(inspect_bucket, self.cloud.client("s3"), self.bucket)

    @contextlib.asynccontextmanager
    async def _session(self):
        """An S3 client on a key made for this one operation, deleted afterwards."""
        c = self.cloud
        if await c.get_user(self.owner) is None:
            raise BackupError(f"The backup IAM user {self.owner} does not exist (yet), "
                              "or Bucketeer may not read it (re-run tools/aws-setup).")
        res = await c.call("iam", "list_access_keys", UserName=self.owner)
        left = []
        for k in res.get("AccessKeyMetadata") or []:
            if k["AccessKeyId"] in _HELD_KEYS:
                left.append(k)
                continue
            await c.call("iam", "delete_access_key", UserName=self.owner,
                         AccessKeyId=k["AccessKeyId"])
            log.info("removed a leftover access key of the backup IAM user")
        if len(left) >= 2:
            raise BackupError("The backup IAM user has two keys in use already.")
        made = (await c.call("iam", "create_access_key", UserName=self.owner))["AccessKey"]
        access = made["AccessKeyId"]
        _HELD_KEYS.add(access)
        try:
            yield c.s3_with_key(access, made["SecretAccessKey"])
        finally:
            made = None
            _HELD_KEYS.discard(access)
            try:
                await c.call("iam", "delete_access_key", UserName=self.owner, AccessKeyId=access)
            except Exception as e:  # noqa: BLE001  the next run removes it
                log.warning("could not delete the backup user's temporary key (%s); the next "
                            "run removes it", type(e).__name__)

    async def upload(self, key: str, path: Path) -> None:
        async with self._session() as s3:
            await asyncio.to_thread(_retry_new_key, upload_file, s3, self.bucket, key, path)

    async def latest(self) -> str | None:
        return await asyncio.to_thread(latest_key, self.cloud.client("s3"), self.bucket)

    async def download(self, key: str, path: Path) -> None:
        async with self._session() as s3:
            await asyncio.to_thread(_retry_new_key, s3.download_file, self.bucket, key, str(path))


# -- the archive ----------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def app_version() -> str | None:
    try:
        out = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=REPO, timeout=3,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001  no git, not a checkout: the version is optional
        return None


def table_counts(path: Path) -> tuple[int, dict[str, int]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        return version, {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                         for n in names}
    finally:
        conn.close()


def build_archive(db: Database, env_file: Path, workdir: Path,
                  at: datetime) -> tuple[Path, dict[str, Any]]:
    """Write the tar.gz into `workdir`; returns its path and the meta document."""
    snap = workdir / DB_MEMBER
    db.snapshot(snap)
    files: dict[str, bytes] = {DB_MEMBER: snap.read_bytes()}
    note = None
    try:
        files[ENV_MEMBER] = env_file.read_bytes()
    except FileNotFoundError:
        note = f"{env_file} does not exist: the archive holds no .env"
    except OSError as e:
        note = f"{env_file} could not be read ({type(e).__name__}): the archive holds no .env"
    if note:
        log.warning("backup: %s", note)
    version, tables = table_counts(snap)
    meta = {
        "format": 1, "createdAt": at.astimezone(UTC).isoformat(timespec="seconds"),
        "appVersion": app_version(), "schemaVersion": version, "tables": tables,
        "files": {n: {"sha256": _sha256(b), "bytes": len(b)} for n, b in files.items()},
        "envIncluded": ENV_MEMBER in files, "note": note,
    }
    files[META_MEMBER] = json.dumps(meta, indent=2, sort_keys=True).encode()
    out = workdir / "archive.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for name in (META_MEMBER, DB_MEMBER, ENV_MEMBER):
            if name not in files:
                continue
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(files[name]), 0o600, int(at.timestamp())
            tar.addfile(info, io.BytesIO(files[name]))
    snap.unlink()
    return out, meta


def read_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Members of a backup archive, checked against meta.json. Raises BackupError."""
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name not in (META_MEMBER, DB_MEMBER, ENV_MEMBER) or not m.isfile():
                    raise BackupError(f"Unexpected archive member {m.name!r}.")
                f = tar.extractfile(m)
                files[m.name] = f.read() if f else b""
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BackupError(f"Not a readable backup archive ({type(e).__name__}).") from None
    if META_MEMBER not in files or DB_MEMBER not in files:
        raise BackupError("The archive has no meta.json or no database.")
    try:
        meta = json.loads(files.pop(META_MEMBER))
        listed = meta["files"]
    except (ValueError, KeyError, TypeError):
        raise BackupError("meta.json is not valid.") from None
    if set(listed) != set(files):
        raise BackupError("The files in the archive do not match meta.json.")
    for name, data in files.items():
        if _sha256(data) != listed[name].get("sha256"):
            raise BackupError(f"SHA-256 mismatch for {name}: the archive is damaged.")
    return meta, files


def check_archive(path: Path, workdir: Path) -> dict[str, Any]:
    """The restore test of one downloaded archive (sync; nothing outside `workdir` is
    written): every SHA-256, then the database copy opened read-only must pass
    `PRAGMA integrity_check`, have a schema this code can open and the row counts of
    meta.json. Raises BackupError; returns {schemaVersion, tables, createdAt, envIncluded}."""
    meta, files = read_archive(path)
    env_included = ENV_MEMBER in files
    copy = _unique(workdir / f"test-{DB_MEMBER}")
    _write_private(copy, files.pop(DB_MEMBER))
    files.clear()  # the .env copy is not needed: drop it from memory at once
    try:
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise BackupError(f"The database in the archive can't be opened ({e}).") from None
        finally:
            conn.close()
        if ok != "ok":
            raise BackupError(f"The database in the archive fails the integrity check ({ok}).")
        try:
            version, tables = table_counts(copy)
        except sqlite3.DatabaseError as e:
            raise BackupError(f"The database in the archive can't be read ({e}).") from None
    finally:
        copy.unlink(missing_ok=True)
    if version > len(MIGRATIONS):
        raise BackupError(f"The database has schema v{version}, newer than this version of "
                          f"Bucketeer (v{len(MIGRATIONS)}): it can't be restored here.")
    if meta.get("schemaVersion") != version:
        raise BackupError(f"meta.json says schema v{meta.get('schemaVersion')}, the database "
                          f"is v{version}.")
    if isinstance(meta.get("tables"), dict) and meta["tables"] != tables:
        raise BackupError("The tables or row counts of the database differ from meta.json.")
    return {"schemaVersion": version, "tables": tables, "createdAt": meta.get("createdAt"),
            "envIncluded": env_included}


# -- the service ----------------------------------------------------------------------------


def _close_open(rec: dict[str, Any], message: str) -> None:
    """A run that ends early: the destinations not finished yet failed with `message`."""
    for entry in rec["destinations"]:
        if entry["outcome"] == "running":
            entry.update(outcome="failed", error=message)


def _settle(rec: dict[str, Any]) -> None:
    """The run's outcome: ok only when every destination got the archive."""
    failed = [e for e in rec["destinations"] if e["outcome"] != "ok"]
    rec["outcome"] = "failed" if failed or not rec["destinations"] else "ok"
    rec["error"] = "; ".join(f"{e['kind']}: {e['error']}" for e in failed) or None


async def _send(dest, entry: dict[str, Any], days: int, key: str, path: Path) -> None:
    """Ensure one destination's bucket and upload the archive there. A failure is recorded on
    its entry only, so the other destinations still get their copy."""
    try:
        made = await dest.ensure(days)
        entry.update(bucketCreated=made["created"], retentionDays=made["retentionDays"])
        await dest.upload(key, path)
        entry["outcome"] = "ok"
    except Exception as e:  # noqa: BLE001  recorded per destination
        entry.update(outcome="failed", error=describe(e))
        log.warning("backup to %s (%s) failed: %s", dest.bucket, dest.kind, entry["error"])


async def _test_one(dest, entry: dict[str, Any], data_dir: Path) -> None:
    """Restore-test the newest archive of one destination; the result goes on `entry`."""
    try:
        key = await dest.latest()
        if not key:
            raise BackupError(f"No backup found in {dest.bucket}.")
        entry["key"] = key
        with tempfile.TemporaryDirectory(prefix=".restore-test-", dir=data_dir) as tmp:
            path = Path(tmp) / "archive.tar.gz"
            await dest.download(key, path)
            entry.update(await asyncio.to_thread(check_archive, path, Path(tmp)))
        entry["outcome"] = "ok"
    except Exception as e:  # noqa: BLE001  recorded per destination
        entry.update(outcome="failed", error=describe(e))
        log.warning("restore test of %s (%s) failed: %s", dest.bucket, dest.kind,
                    entry["error"])
    finally:
        entry["checkedAt"] = datetime.now(UTC).isoformat(timespec="seconds")


PLACES = {"onprem": "on premises", "cloud": "AWS"}


def _places(entries: list[dict[str, Any]]) -> str:
    return ", ".join(PLACES.get(e["kind"], str(e["kind"])) for e in entries) or "no place"


def _failure_lines(rec: dict[str, Any]) -> str:
    failed = [e for e in rec["destinations"] if e["outcome"] != "ok"]
    lines = [f"- {PLACES.get(e['kind'], e['kind'])}, bucket {e['bucket']}: {e['error']}"
             for e in failed]
    return "\n".join(lines) or f"- {rec.get('error') or 'it stopped before reaching a bucket'}"


def _by(rec: dict[str, Any]) -> str:
    return "the nightly schedule" if rec["trigger"] == "schedule" else rec["actor"]


def run_alert(rec: dict[str, Any]) -> tuple[str, str]:
    """Subject and body of the `backup_failed` alert (no secrets: errors are `describe`d)."""
    failed = [e for e in rec["destinations"] if e["outcome"] != "ok"]
    return (f"Backup failed ({_places(failed)})",
            f"The backup run started {rec['startedAt']} by {_by(rec)} did not write the "
            f"archive {rec['key']} everywhere:\n\n{_failure_lines(rec)}\n\n"
            "A place where it worked keeps its copy. The Backup tab of the administration page "
            "shows the run; the next scheduled run tries again.")


def restore_alert(rec: dict[str, Any]) -> tuple[str, str]:
    """Subject and body of the `restore_test_failed` alert."""
    failed = [e for e in rec["destinations"] if e["outcome"] != "ok"]
    return (f"Restore test failed ({_places(failed)})",
            f"The restore test started {rec['startedAt']} by {_by(rec)} could not prove that "
            f"the newest backup can be restored:\n\n{_failure_lines(rec)}\n\n"
            "The live database was not touched. Check the Backup tab of the administration "
            "page, then run \"Test restore now\" again.")


class BackupService:
    def __init__(self, app, now: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.app, self.now, self.sleep = app, now, sleep
        self.running = False
        self.testing = False  # a restore test is going
        prefix = getattr(app.state.settings, "cloud_bucket_prefix", "bucketeer-")
        self.suggestion = suggest_bucket(prefix)
        self._floor = datetime.min
        self._tasks: set[asyncio.Task] = set()
        self.factory: Callable[..., Any] = default_factory  # tests spy on the backup key

    @property
    def db(self) -> Database:
        return self.app.state.db

    @property
    def bucket(self) -> str:
        return self.app.state.settings.backup_bucket

    def destinations(self) -> list["OnPremDestination | AwsDestination"]:
        """Every run writes the archive on premises, and to AWS while cloud is configured."""
        s = self.app.state
        out: list = [OnPremDestination(s.croit, s.settings, s.endpoints, self.factory)]
        cloud = getattr(s, "cloud", None)
        if cloud is not None:
            out.append(AwsDestination(cloud, self.bucket))
        return out

    def settings(self) -> dict[str, Any]:
        return load(self.db)

    def runs(self) -> list[dict[str, Any]]:
        return list(self.db.settings.get(RUNS_KEY) or [])

    def tests(self) -> list[dict[str, Any]]:
        """The restore tests, newest first."""
        return list(self.db.settings.get(TESTS_KEY) or [])

    def describe_off(self) -> str:
        return (f"BACKUP_BUCKET is not set: backups are off. Suggested: "
                f"BACKUP_BUCKET={self.suggestion}")

    def _save_run(self, rec: dict[str, Any]) -> None:
        with self.db.tx():
            runs = [r for r in self.runs() if r.get("id") != rec["id"]]
            self.db.settings.set(RUNS_KEY, [rec, *runs][:KEEP_RUNS], rec["actor"])

    async def run(self, trigger: str, actor: str) -> dict[str, Any]:
        """One backup run, shielded so a dropped request does not cancel it."""
        if not self.bucket:
            raise BackupError("Backups are off: BACKUP_BUCKET is not set.")
        if self.running:
            raise AlreadyRunning
        self.running = True
        task = asyncio.get_running_loop().create_task(self._run(trigger, actor))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def _run(self, trigger: str, actor: str) -> dict[str, Any]:
        db = self.db
        at = datetime.now(UTC)
        runs = await db.run(self.runs)
        key = archive_key(at)
        rec: dict[str, Any] = {
            "id": max((r.get("id", 0) for r in runs), default=0) + 1,
            "startedAt": at.isoformat(timespec="seconds"), "finishedAt": None,
            "trigger": trigger, "actor": actor, "bucket": self.bucket, "key": key,
            "outcome": "running", "destinations": [], "bytes": None, "sha256": None,
            "schemaVersion": None, "envIncluded": None, "note": None, "error": None,
        }
        dests: list = []
        try:
            dests = self.destinations()
            rec["destinations"] = [{"kind": d.kind, "bucket": d.bucket, "outcome": "running",
                                    "error": None, "bucketCreated": None,
                                    "retentionDays": None} for d in dests]
            s = await db.run(self.settings)
            data_dir = Path(self.app.state.settings.data_dir)
            with tempfile.TemporaryDirectory(
                    prefix=".backup-", dir=data_dir if data_dir.is_dir() else None) as tmp:
                path, meta = await asyncio.to_thread(build_archive, db, env_path(), Path(tmp), at)
                body = path.read_bytes()
                rec.update(bytes=len(body), sha256=_sha256(body),
                           schemaVersion=meta["schemaVersion"], envIncluded=meta["envIncluded"],
                           note=meta["note"])
                for dest, entry in zip(dests, rec["destinations"], strict=True):
                    await _send(dest, entry, s["retentionDays"], key, path)
        except asyncio.CancelledError:
            _close_open(rec, "Stopped by a shutdown.")
            raise
        except Exception as e:  # noqa: BLE001  every failure is recorded and audited
            _close_open(rec, describe(e))
            log.warning("backup %s failed: %s", key, describe(e))
        finally:
            self.running = False
            rec["finishedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
            _settle(rec)
            with contextlib.suppress(Exception):
                await db.run(self._save_run, rec)
        await self._audit(rec, trigger, actor)
        if rec["outcome"] != "ok":
            await alerts.send_alert(self.app, "backup_failed", *run_alert(rec))
        return rec

    async def _audit(self, rec: dict[str, Any], trigger: str, actor: str) -> None:
        db = self.db
        try:
            for e in rec["destinations"]:
                if e["outcome"] == "ok":
                    await db.run(db.audit.add, actor, "backup.run", "backup", e["bucket"], {
                        "trigger": trigger, "key": rec["key"], "bytes": rec["bytes"],
                        "sha256": rec["sha256"], "schemaVersion": rec["schemaVersion"],
                        "envIncluded": rec["envIncluded"], "bucketCreated": e["bucketCreated"],
                        "retentionDays": e["retentionDays"], "destination": e["kind"]})
                else:
                    await db.run(db.audit.add, actor, "backup.failed", "backup",
                                 e["bucket"] or "-", {
                                     "trigger": trigger, "key": rec["key"], "error": e["error"],
                                     "destination": e["kind"]})
            if rec["outcome"] == "ok":
                for p in await db.run(db.remove_upgrade_copies):
                    log.info("backup done: removed pre-upgrade copy %s", p)
                log.info("backup %s written to %s", rec["key"],
                         ", ".join(f"{e['kind']} {e['bucket']}" for e in rec["destinations"]))
        except Exception:  # noqa: BLE001  the run itself is already recorded
            log.exception("could not write the audit entries of backup %s", rec["key"])

    # -- restore test ---------------------------------------------------------------------

    async def restore_test(self, trigger: str, actor: str) -> dict[str, Any]:
        """Restore-test the newest archive in every destination; shielded like a run.
        Raises BackupError (backups off) or AlreadyRunning (a backup or a test is going)."""
        if not self.bucket:
            raise BackupError("Backups are off: BACKUP_BUCKET is not set.")
        if self.running:
            raise AlreadyRunning("A backup is running: test the restore when it is done.")
        if self.testing:
            raise AlreadyRunning("A restore test is running already.")
        self.testing = True
        try:
            task = asyncio.get_running_loop().create_task(self._restore_test(trigger, actor))
        except BaseException:
            self.testing = False
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    def _save_test(self, rec: dict[str, Any]) -> None:
        with self.db.tx():
            tests = [t for t in self.tests() if t.get("id") != rec["id"]]
            self.db.settings.set(TESTS_KEY, [rec, *tests][:KEEP_TESTS], rec["actor"])

    async def _restore_test(self, trigger: str, actor: str) -> dict[str, Any]:
        db = self.db
        tests = await db.run(self.tests)
        rec: dict[str, Any] = {
            "id": max((t.get("id", 0) for t in tests), default=0) + 1,
            "startedAt": datetime.now(UTC).isoformat(timespec="seconds"), "finishedAt": None,
            "trigger": trigger, "actor": actor, "outcome": "running", "destinations": [],
            "error": None,
        }
        try:
            dests = self.destinations()
            rec["destinations"] = [{
                "kind": d.kind, "bucket": d.bucket, "key": None, "outcome": "running",
                "error": None, "checkedAt": None, "schemaVersion": None, "tables": None}
                for d in dests]
            # under DATA_DIR (0700): the archive holds the database and .env
            data_dir = Path(self.app.state.settings.data_dir)
            await asyncio.to_thread(data_dir.mkdir, mode=0o700, parents=True, exist_ok=True)
            for dest, entry in zip(dests, rec["destinations"], strict=True):
                await _test_one(dest, entry, data_dir)
        except asyncio.CancelledError:
            _close_open(rec, "Stopped by a shutdown.")
            raise
        except Exception as e:  # noqa: BLE001  every failure is recorded and audited
            _close_open(rec, describe(e))
            log.warning("restore test failed: %s", describe(e))
        finally:
            self.testing = False
            rec["finishedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
            _settle(rec)
            with contextlib.suppress(Exception):
                await db.run(self._save_test, rec)
        await self._audit_test(rec)
        if rec["outcome"] != "ok":
            await alerts.send_alert(self.app, "restore_test_failed", *restore_alert(rec))
        return rec

    async def _audit_test(self, rec: dict[str, Any]) -> None:
        db = self.db
        try:
            for e in rec["destinations"]:
                ok = e["outcome"] == "ok"
                await db.run(db.audit.add, rec["actor"],
                             "backup.restore_test" if ok else "backup.restore_test_failed",
                             "backup", e["bucket"] or "-", {
                                 "trigger": rec["trigger"], "destination": e["kind"],
                                 "key": e["key"], "error": e["error"],
                                 "schemaVersion": e["schemaVersion"],
                                 "rows": sum((e["tables"] or {}).values()) if ok else None})
        except Exception:  # noqa: BLE001  the test itself is already recorded
            log.exception("could not write the audit entries of restore test %s", rec["id"])

    def restore_test_due(self) -> bool:
        """After a successful scheduled backup: the newest test failed, or it is at least
        `restoreTestDays` calendar days old (0 = never automatically)."""
        days = self.settings()["restoreTestDays"]
        if not days:
            return False
        tests = self.tests()
        if not tests or tests[0].get("outcome") != "ok":
            return True
        last = local_time(tests[0].get("finishedAt") or tests[0].get("startedAt"))
        return last is None or (self.now().date() - last.date()).days >= days

    def next_restore_test(self) -> datetime | None:
        """The scheduled backup after which the next automatic test runs (if it succeeds)."""
        s = self.settings()
        if not self.bucket or not s["restoreTestDays"]:
            return None
        base = max(self.now(), self._floor)
        tests = self.tests()
        if tests and tests[0].get("outcome") == "ok":
            last = local_time(tests[0].get("finishedAt") or tests[0].get("startedAt"))
            if last is not None:
                due = datetime.combine(last.date() + timedelta(days=s["restoreTestDays"]),
                                       datetime.min.time())
                base = max(base, due - timedelta(microseconds=1))
        return next_run(base, s["time"])

    # -- no backup for too long ----------------------------------------------------------

    async def check_stale(self, since_start: datetime) -> bool:
        """Email the alert contacts when no backup succeeded for more than 48 hours (counted
        from the last success, else from the oldest kept run or this process's start,
        `since_start`), at most once per 24 hours. True when an alert went out."""
        db, now = self.db, self.now()
        runs = await db.run(self.runs)
        good = next((r for r in runs if r.get("outcome") == "ok"), None)
        if good:
            since = local_time(good.get("finishedAt") or good.get("startedAt"))
        else:
            times = [t for r in runs if (t := local_time(r.get("startedAt")))]
            since = min([since_start, *times])
        if since is None or now - since <= STALE_AFTER:
            return False
        last = local_time(((await db.run(db.settings.get, STALE_KEY)) or {}).get("at"))
        if last is not None and now - last < STALE_REPEAT:
            return False
        await db.run(db.settings.set, STALE_KEY,
                     {"at": now.astimezone().isoformat(timespec="seconds"),
                      "since": since.astimezone().isoformat(timespec="seconds")}, "system")
        hours = int((now - since).total_seconds() // 3600)
        what = (f"The last successful backup finished {since:%Y-%m-%d %H:%M} (server time)"
                if good else "No backup run succeeded since "
                             f"{since:%Y-%m-%d %H:%M} (server time)")
        last_run = runs[0] if runs else None
        detail = "No backup run is recorded."
        if last_run:
            detail = (f"The last run, started {last_run.get('startedAt')}, ended "
                      f"{last_run.get('outcome')}"
                      + (f": {last_run['error']}" if last_run.get("error") else "."))
        await alerts.send_alert(
            self.app, "backup_stale", f"No successful backup for {hours} hours",
            f"{what}, {hours} hours ago, although backups are on (bucket {self.bucket}).\n\n"
            f"{detail}\n\nCheck the Backup tab of the administration page and the server log. "
            "This reminder is sent at most once a day until a backup succeeds.")
        return True

    async def apply_retention(self) -> None:
        """Lengthen the lock and lifecycle of every destination's bucket. Raises with every
        failure named, after trying all of them."""
        days = (await self.db.run(self.settings))["retentionDays"]
        errors = []
        for dest in self.destinations():
            try:
                await dest.ensure(days)
            except Exception as e:  # noqa: BLE001  collected below
                errors.append(f"{dest.kind}: {describe(e)}")
        if errors:
            raise BackupError("; ".join(errors))

    async def state(self) -> dict[str, Any]:
        s = await self.db.run(self.settings)
        runs = await self.db.run(self.runs)
        entries = []
        for dest in self.destinations():
            entry: dict[str, Any] = {
                "kind": dest.kind, "bucket": dest.bucket or None, "user": dest.owner,
                "region": dest.cloud.region if dest.kind == "cloud" else None, "state": None}
            if self.bucket:
                try:
                    entry["state"] = await dest.inspect()
                except Exception as e:  # noqa: BLE001  shown on the page, not raised
                    entry["state"] = {"exists": None, "error": describe(e)}
            entries.append(entry)
        aws = next((e for e in entries if e["kind"] == "cloud"), None)
        return {
            "configured": bool(self.bucket),
            "bucket": self.bucket or None,
            "suggestedBucket": None if self.bucket else self.suggestion,
            "onpremBucket": self.bucket or None,
            "awsBucket": (aws["bucket"] if aws else None),
            "region": aws["region"] if aws else None,
            "user": entries[0]["user"],
            "destinations": entries,
            "settings": s,
            "nextRun": (self.next_run().isoformat(timespec="minutes") if self.bucket
                        else None),
            "running": self.running,
            "lastRun": runs[0] if runs else None,
            "lastSuccess": next((r for r in runs if r.get("outcome") == "ok"), None),
            "runs": runs,
            "testing": self.testing,
            "restoreTests": await self.db.run(self.tests),
            "nextRestoreTest": (t.isoformat(timespec="minutes")
                                if (t := await self.db.run(self.next_restore_test)) else None),
        }

    def note_settings_changed(self) -> None:
        self._floor = self.now()

    def next_run(self) -> datetime:
        return next_run(max(self.now(), self._floor), self.settings()["time"])

    async def schedule_loop(self) -> None:
        """Back up daily at the configured time; re-reads the settings every `TICK`. After a
        successful run, the automatic restore test when it is due; every tick, the check
        for no successful backup in 48 hours."""
        if not self.bucket:  # the lifespan logged the suggestion
            return
        after = started = self.now()
        while True:
            target = None
            try:
                try:
                    await self.check_stale(started)
                except Exception:  # noqa: BLE001  never in the way of the backup itself
                    log.exception("backup stale check")
                after = max(after, self._floor)
                target = next_run(after, self.settings()["time"])
                wait = (target - self.now()).total_seconds()
                if wait > 0:
                    await self.sleep(min(wait, TICK))
                    continue
                rec = None
                try:
                    rec = await self.run("schedule", "system")
                except AlreadyRunning:
                    pass
                after = max(self.now(), target)
                if rec and rec["outcome"] == "ok" and await self.db.run(self.restore_test_due):
                    try:
                        await self.restore_test("schedule", "system")
                    except (AlreadyRunning, BackupError):
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("backup scheduler")
                after = max(self.now(), target or self.now())
                await self.sleep(TICK)

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


# -- API --------------------------------------------------------------------------------------


router = APIRouter(prefix="/admin/backup", tags=["admin"], dependencies=[Depends(require_admin)])
Db = Annotated[Database, Depends(get_db)]
Admin = Annotated[SessionUser, Depends(require_admin)]


def get_service(request: Request) -> BackupService:
    return request.app.state.backup


Svc = Annotated[BackupService, Depends(get_service)]


@router.get("")
async def get_backup(svc: Svc):
    return await svc.state()


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: StrictStr | None = None
    retentionDays: StrictInt | None = None
    restoreTestDays: StrictInt | None = None


@router.put("/settings")
async def put_settings(body: SettingsBody, admin: Admin, db: Db, svc: Svc):
    before = await db.run(load, db)
    errors = []
    if body.time is not None and not _TIME.match(body.time):
        errors.append({"field": "time", "message": "Time must be HH:MM (24 hours, server time)."})
    if body.retentionDays is not None:
        if not 1 <= body.retentionDays <= MAX_RETENTION:
            errors.append({"field": "retentionDays",
                           "message": f"Enter a whole number of days from 1 to {MAX_RETENTION}."})
        elif body.retentionDays < before["retentionDays"]:
            errors.append({"field": "retentionDays", "message": (
                f"Retention can only be lengthened (it is {before['retentionDays']} days): "
                "backups already written stay locked for their period.")})
    if body.restoreTestDays is not None and not 0 <= body.restoreTestDays <= MAX_TEST_DAYS:
        errors.append({"field": "restoreTestDays", "message": (
            f"Enter a whole number of days from 0 to {MAX_TEST_DAYS} (0 = no automatic "
            "restore test).")})
    if errors:
        return JSONResponse(status_code=422,
                            content={"detail": errors[0]["message"], "errors": errors})
    after = dict(before)
    if body.time is not None:
        after["time"] = body.time
    if body.retentionDays is not None:
        after["retentionDays"] = body.retentionDays
    if body.restoreTestDays is not None:
        after["restoreTestDays"] = body.restoreTestDays
    out: dict[str, Any] = {"settings": after}
    if after != before:
        await db.run(_save_settings, db, after, admin.username)
        svc.note_settings_changed()
        await db.run(db.audit.add, admin.username, "admin.settings.backup", "setting", KEY,
                     {"before": before, "after": after})
        if after["retentionDays"] > before["retentionDays"] and svc.bucket:
            try:
                await svc.apply_retention()
            except Exception as e:  # noqa: BLE001  the next run applies it
                out["warning"] = (f"Saved. The bucket could not be updated now "
                                  f"({describe(e)}); the next backup run applies it.")
    return out


@router.post("/run")
async def run_now(admin: Admin, svc: Svc):
    if not svc.bucket:
        raise HTTPException(409, "Backups are off: set BACKUP_BUCKET in .env first.")
    try:
        return await svc.run("manual", admin.username)
    except AlreadyRunning:
        raise HTTPException(409, "A backup is running already.") from None


@router.post("/restore-test")
async def restore_test_now(admin: Admin, svc: Svc):
    """Restore-test the newest archive in every destination now (nothing live is touched)."""
    if not svc.bucket:
        raise HTTPException(409, "Backups are off: set BACKUP_BUCKET in .env first.")
    try:
        return await svc.restore_test("manual", admin.username)
    except AlreadyRunning as e:
        raise HTTPException(409, str(e)) from None


# -- restore (command line) -------------------------------------------------------------------


CATCH_UP_MODULES = ("bucketeer.rebuild", "bucketeer.ceph_rebuild", "bucketeer.rebuild_from_ceph")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def app_is_running(settings: Settings) -> bool:
    import httpx

    host = "127.0.0.1" if settings.host in ("0.0.0.0", "", "::") else settings.host
    try:
        r = httpx.get(f"http://{host}:{settings.port}{settings.health_check_path}", timeout=2)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _unique(path: Path) -> Path:
    """`path`, or `path-1`, `path-2`... when it exists (two restores in one second)."""
    out, i = path, 0
    while out.exists():
        i += 1
        out = path.with_name(f"{path.name}-{i}")
    return out


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(0o600)


def replace_database(target: Path, data: bytes) -> Path | None:
    """Put `data` in place of the database at `target`; the old one (WAL folded in) is kept
    as `<file>.replaced-<time>`. Returns that copy's path, or None when there was none."""
    stamp = _stamp()
    new = _unique(target.with_name(f"{target.name}.restoring-{stamp}"))
    _write_private(new, data)
    conn = sqlite3.connect(f"file:{new}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if ok != "ok":
        new.unlink()
        raise BackupError(f"The database in the archive fails the integrity check ({ok}).")
    kept = None
    if target.exists():
        kept = _unique(target.with_name(f"{target.name}.replaced-{stamp}"))
        os.close(os.open(kept, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        src, out = sqlite3.connect(target), sqlite3.connect(kept)
        try:
            src.backup(out)  # includes what is still in the WAL
            out.execute("PRAGMA journal_mode = DELETE")
        finally:
            out.close()
            src.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
    os.replace(new, target)
    return kept


def _is_report(result: Any) -> bool:
    return isinstance(result, dict) and {"mode", "added", "skipped", "problems"} <= set(result)


MAX_PROBLEM_ITEMS = 50


def catch_up_digest(result: Any) -> Any:
    """What the `system.restore` audit entry keeps of the catch-up: for a rebuild report
    only the mode, counts and at most 50 problem items (a big cluster's report is huge)."""
    if _is_report(result):
        problems = result["problems"] or []
        reasons: dict[str, int] = {}
        for p in problems:
            reasons[str(p.get("reason"))] = reasons.get(str(p.get("reason")), 0) + 1
        return {"mode": result["mode"], "added": result["added"],
                "skipped": len(result["skipped"] or []), "problems": len(problems),
                "problemReasons": reasons,
                "problemItems": [f"{p.get('table')} {p.get('item')}"
                                 for p in problems[:MAX_PROBLEM_ITEMS]]}
    if result is None or isinstance(result, int | str):
        return result
    text = json.dumps(result, default=str) if isinstance(result, dict | list) else str(result)
    return result if len(text) <= 2000 else text[:2000]


async def _catch_up(settings: Settings, db: Database, client) -> tuple[str, Any]:
    for name in CATCH_UP_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        fn = getattr(mod, "rebuild_from_ceph", None)
        if fn is None:
            continue
        offered = {"fill_gaps": True, "settings": settings, "db": db, "client": client,
                   "croit": client}
        params = inspect.signature(fn).parameters
        result = fn(**{k: v for k, v in offered.items() if k in params})
        if inspect.isawaitable(result):
            result = await result
        return "done", result
    return "unavailable", None


SOURCES = ("onprem", "cloud")


async def find_aws_bucket(cloud) -> str:
    """The one `<prefix>backup-<12 hex>` bucket in the account (restore on a new machine)."""
    pattern = re.compile(rf"{re.escape(cloud.bucket_prefix)}backup-[0-9a-f]{{12}}\Z")
    names = sorted(b["Name"] for b in await cloud.list_buckets() if pattern.match(b["Name"]))
    if not names:
        raise BackupError("No backup bucket found in AWS.")
    if len(names) > 1:
        raise BackupError(f"Several backup buckets in AWS ({', '.join(names)}): pass "
                          "--aws-bucket NAME.")
    return names[0]


def check_backup_bucket(name: str, source: str | None) -> None:
    """`--backup-bucket NAME`: an on-premises bucket name (DNS rules). Raises BackupError."""
    if source == "cloud":
        raise BackupError("--backup-bucket names an on-premises bucket; with --from cloud "
                          "use --aws-bucket NAME.")
    if errors := check_dns_name(name):
        raise BackupError(f"--backup-bucket {name[:63]!r}: {' '.join(errors)}")


async def restore(settings: Settings, which: str, *, force: bool = False,
                  source: str | None = None, aws_bucket: str | None = None,
                  backup_bucket: str | None = None,
                  client=None, cloud_admin=None,
                  factory: Callable[..., Any] = default_factory,
                  out: Callable[[str], None] = print) -> dict[str, Any]:
    """Download, verify and install a backup. Raises BackupError with a clear message.
    `source` onprem|cloud; None = on premises. `backup_bucket` reads another on-premises
    bucket than BACKUP_BUCKET (e.g. the one used before it was renamed)."""
    from .cloud import CloudAdmin
    from .credentials import EndpointResolver
    from .croit import CroitClient
    from .rgw_admin import RgwAdminClient

    if source not in (None, *SOURCES):
        raise BackupError("--from must be onprem or cloud.")
    if backup_bucket is not None:
        check_backup_bucket(backup_bucket, source)
    data_dir = Path(settings.data_dir)
    source = source or "onprem"
    if source == "onprem" and not (backup_bucket or settings.backup_bucket):
        raise BackupError("BACKUP_BUCKET is not set in .env (or name the bucket with "
                          "--backup-bucket NAME).")
    if source == "cloud" and cloud_admin is None:
        try:
            cloud_admin = CloudAdmin(settings)
        except NotConfigured:
            raise BackupError("Cloud is not set up: the CLOUD_* variables in .env are "
                              "needed to restore from AWS.") from None
    if which != "latest" and not ARCHIVE_RE.match(which):
        raise BackupError("Give an object key like backup/bucketeer-2026-01-31T0300Z.tar.gz "
                          "or 'latest'.")
    if not force and app_is_running(settings):
        raise BackupError(f"Bucketeer answers on port {settings.port}: stop it first "
                          "(or pass --force).")
    own = client is None
    if own:
        client = (RgwAdminClient(settings) if settings.admin_backend == "rgw"
                  else CroitClient(settings))
    try:
        import time

        if source == "cloud":
            bucket = aws_bucket or settings.backup_bucket or await find_aws_bucket(cloud_admin)
            dest = AwsDestination(cloud_admin, bucket)
        else:
            dest = OnPremDestination(client, settings,
                                     EndpointResolver(client, settings, time.time), factory,
                                     bucket=backup_bucket)
        bucket = dest.bucket
        where = f"{bucket} ({'AWS' if source == 'cloud' else 'on premises'})"
        key = which
        if which == "latest":
            key = await dest.latest()
            if not key:
                raise BackupError(f"No backup found in {where}.")
        out(f"Downloading {key} from {where} ...")
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=data_dir) as tmp:
            path = Path(tmp) / "archive.tar.gz"
            try:
                await dest.download(key, path)
            except (ClientError, CloudError) as e:
                raise BackupError(describe(e)) from None
            meta, files = read_archive(path)
        out(f"Checked SHA-256 of {', '.join(sorted(files))} (schema v{meta.get('schemaVersion')},"
            f" written {meta.get('createdAt')}).")
        target = data_dir / DB_FILENAME
        kept = replace_database(target, files[DB_MEMBER])
        if kept:
            out(f"The previous database is kept as {kept}")
        db = Database(target)  # runs the migrations (with a pre-upgrade copy) when older
        try:
            out(f"Database installed at {target} (schema v{db.version}).")
            env_written = None
            if ENV_MEMBER in files:
                live = env_path()
                base = live.resolve() if live.exists() else live
                env_written = _unique(base.with_name(f".env.restored-{_stamp()}"))
                _write_private(env_written, files[ENV_MEMBER])
                out(f"The backed-up .env is at {env_written} (0600). Compare it with "
                    f"{live} and copy what you need by hand.")
            else:
                out("The archive holds no .env.")
            try:
                status, result = await _catch_up(settings, db, client)
            except Exception as e:  # noqa: BLE001  the restore itself is done
                status, result = "failed", describe(e)
            if status == "unavailable":
                out("Catch-up from Ceph (rebuild_from_ceph) isn't available in this version: "
                    "buckets, keys and shares made after the backup are not added.")
            elif _is_report(result):
                from .rebuild import summary

                out(summary(result))
            else:
                out(f"Catch-up from Ceph: {status}: {result}")
            details = {"key": key, "bucket": bucket, "source": source,
                       "createdAt": meta.get("createdAt"),
                       "schemaFrom": meta.get("schemaVersion"), "schemaTo": db.version,
                       "replacedCopy": str(kept) if kept else None,
                       "envRestored": str(env_written) if env_written else None,
                       "catchUp": status,
                       "catchUpResult": catch_up_digest(result)}
            db.audit.add("system", "system.restore", "backup", bucket, details)
            return details
        finally:
            db.close()
    finally:
        if own:
            await client.aclose()


USAGE = ("usage: python3 app.py --restore-backup <object key|latest> [--from onprem|cloud] "
         "[--aws-bucket NAME] [--backup-bucket NAME] [--force]")


def _option(args: list[str], name: str) -> str | None:
    """The value after `name`; raises ValueError when it is missing."""
    if name not in args:
        return None
    i = args.index(name)
    if i + 1 >= len(args) or args[i + 1].startswith("--"):
        raise ValueError(name)
    return args[i + 1]


def restore_main(argv: list[str]) -> int:
    """`app.py --restore-backup <object key|latest> [--from onprem|cloud] [--aws-bucket NAME]
    [--backup-bucket NAME] [--force]`."""
    from .config import load_settings

    args = list(argv)
    force = "--force" in args
    try:
        which = _option(args, "--restore-backup")
        source = _option(args, "--from")
        aws_bucket = _option(args, "--aws-bucket")
        backup_bucket = _option(args, "--backup-bucket")
    except ValueError:
        print(USAGE)
        return 2
    if which is None or source not in (None, *SOURCES):
        print(USAGE)
        return 2
    if backup_bucket is not None:
        try:
            check_backup_bucket(backup_bucket, source)
        except BackupError as e:
            print(f"{e}\n{USAGE}")
            return 2
    settings = load_settings()
    try:
        asyncio.run(restore(settings, which, force=force, source=source,
                            aws_bucket=aws_bucket, backup_bucket=backup_bucket))
    except BackupError as e:
        print(f"Restore failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"Restore failed: {describe(e)}")
        return 1
    print("Restore finished. Start Bucketeer again.")
    return 0
