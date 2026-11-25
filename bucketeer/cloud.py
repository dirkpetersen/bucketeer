"""Cloud (AWS) admin client, Phase 6 foundation (P6-8).

`CloudAdmin` is Bucketeer's own AWS identity: the IAM admin user that `tools/aws-setup`
creates (`CLOUD_ADMIN_ACCESS_KEY` / `CLOUD_ADMIN_SECRET_KEY`, region `CLOUD_REGION`).
`create_app` stores one in `app.state.cloud` when `Settings.cloud_configured`, else None.
It never touches user data: objects are reached only with a person's own key (P6-9).

- Every AWS call goes through `call(service, operation, **params)`: one boto3 client per
  service (built lazily, thread-safe), run in `asyncio.to_thread`.
- Errors become `CloudError(status, code, message)`; network trouble and timeouts
  `CloudUnavailable` (a `CloudError`, status 503). Their text names the operation and
  AWS's error code only, never AWS's message (it can carry ARNs), a request URL or a key.
  The full AWS message is logged at WARNING for the operator.
- botocore/boto3 loggers are pinned to WARNING (botocore logs signed requests at DEBUG).
- `check()` = STS GetCallerIdentity, IAM ListUsers under `/bucketeer/` (one item) and S3
  ListBuckets: `{ok, accountId, region, reason, checkedAt}`. The last result is kept
  (`last_check`, `available`); `monitor_loop` (lifespan task, `CHECK_ON_START`, off in
  tests) re-checks hourly, every 5 min after a failure.
- Naming helpers for later items: `user_name(username)` = `<CLOUD_USER_PREFIX><username>`
  (lowercased), `bucket_name(username, name)` = `<CLOUD_BUCKET_PREFIX><username>-<name>`.
  `reserved_usernames()` (`bucketeer-admin`, `bucketeer-backup`) would collide with the
  system users: `user_name` refuses them (`CloudError` 409 `reserved`), so an AD account
  named `bucketeer-backup` never reaches the backup IAM user.
- Backups (P6-12, `backup.AwsDestination`): `backup_user_name()` =
  `<CLOUD_USER_PREFIX>bucketeer-backup` (path `/bucketeer/`, with the boundary
  `boundary_arn(account)`), `new_backup_bucket_name()` = `<CLOUD_BUCKET_PREFIX>backup-<12 hex>`,
  `s3_with_key(access, secret)` a plain S3 client for the backup user's temporary key.
- Person IAM users (P6-9; orchestration, SQLite and audit live in `cloud_users.py`):
  `account_id()` (STS, cached), `person_boundary_arn()`, `create_person_user` (path
  `/bucketeer/`, boundary `BucketeerUserBoundary`, tag `bucketeer-owner`), `is_ours` (path +
  boundary + owner tag; anything else is never adopted), access keys (`list_access_keys`,
  `create_access_key`, `delete_access_key`, `set_access_key_status`), `put_user_policy`.
- **Access to buckets comes from each bucket's own policy, never from a name wildcard.** The
  person's inline policy `bucketeer-buckets` (`user_policy`) allows only `s3:ListAllMyBuckets`
  and `s3:GetBucketLocation`. A wildcard such as `<prefix><username>-*` would be wrong: AD
  names may contain hyphens, so `jdoe-x`'s bucket `<prefix>jdoe-x-data` also matches
  `jdoe`'s wildcard, and a person named `backup` would match the system backup bucket
  `<prefix>backup-<hex>`. Instead every cloud bucket's BUCKET POLICY carries one statement,
  Sid `bucketeer-owner`, with the owner's IAM user ARN as Principal and the bucket-read and
  object actions (`BUCKET_ACTIONS`, `OBJECT_ACTIONS`, within what the boundary allows) on the
  bucket and `bucket/*`: in the same account a bucket policy naming the user grants access on
  its own. `grant_bucket_owner(bucket, user_arn)` / `revoke_bucket_owner(bucket)` (P6-10 calls
  them on create/delete) read-modify-write the policy and touch only `bucketeer-*` Sids.
  If an IAM user is deleted and made again, its bucket statements must be granted again
  (AWS then shows the old principal as a unique id).
"""

import asyncio
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from .config import Settings

log = logging.getLogger("bucketeer.cloud")
for _name in ("botocore", "boto3", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)

USER_PATH = "/bucketeer/"  # per-person IAM users (tools/aws-setup grants only this path)
USER_POLICY = "bucketeer-buckets"  # the inline policy of a person's IAM user
OWNER_TAG = "bucketeer-owner"
OWNER_SID = "bucketeer-owner"  # the bucket-policy statement that gives the owner access
MAX_ACCESS_KEYS = 2  # IAM's hard limit per user
# What the owner may do in their own bucket (the boundary in tools/aws-setup allows these):
# bucket reads plus object access. Bucket set-up (create, delete, lifecycle) is Bucketeer's.
BUCKET_ACTIONS = [
    "s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketVersions",
    "s3:ListBucketMultipartUploads", "s3:GetBucketVersioning", "s3:GetBucketTagging",
    "s3:GetLifecycleConfiguration",
]
OBJECT_ACTIONS = [
    "s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectAttributes", "s3:GetObjectTagging",
    "s3:GetObjectVersionTagging", "s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObject",
    "s3:DeleteObjectVersion", "s3:DeleteObjectTagging", "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts", "s3:RestoreObject",
]
# `backup` / `billing` would put a person's cloud buckets next to the system buckets
# `<prefix>backup-*` and `<prefix>billing-*` (P6-10), so they are refused for cloud too.
RESERVED_USERNAMES = frozenset({"bucketeer-admin", "bucketeer-backup", "backup", "billing"})
SYSTEM_BUCKET_WORDS = ("backup", "billing")
# A new IAM user's ARN is accepted in a bucket policy only after IAM propagates it (about 5 s
# on AWS): until then PutBucketPolicy answers MalformedPolicy "Invalid principal in policy".
# grant_bucket_owner retries with these waits (seconds, about a minute in all).
GRANT_WAITS = (1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0)
STORAGE_CLASSES = {"intelligent": "INTELLIGENT_TIERING", "deep_archive": "DEEP_ARCHIVE"}
BACKUP_USER = "bucketeer-backup"  # after CLOUD_USER_PREFIX: the backup IAM user (P6-12)
BOUNDARY_POLICY = "BucketeerUserBoundary"  # tools/aws-setup, under USER_PATH
CHECK_ON_START = True
NO_CLOUD = object()  # create_app(cloud_admin=NO_CLOUD): no cloud even when configured
RECHECK_OK = 3600.0
RECHECK_FAILED = 300.0
SERVICES = ("sts", "iam", "s3", "cloudwatch")


class CloudError(Exception):
    """An AWS call failed. `str()` is safe to show an admin: no secrets, URLs or ARNs."""

    def __init__(self, status: int, code: str, message: str, aws_message: str = ""):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        # AWS's own text: only for decisions (e.g. "Invalid principal") and the log, never shown
        self.aws_message = aws_message


class CloudUnavailable(CloudError):
    """AWS could not be reached (network, DNS, timeout)."""

    def __init__(self, message: str = "AWS could not be reached"):
        super().__init__(503, "unavailable", message)


class NotConfigured(CloudError):
    def __init__(self):
        super().__init__(409, "not_configured", "Cloud storage is not set up")


def reserved_usernames() -> frozenset[str]:
    """AD usernames that never get cloud: they would name a system IAM user."""
    return RESERVED_USERNAMES


async def _sleep(seconds: float) -> None:  # tests replace it
    await asyncio.sleep(seconds)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return str(value) if value else None


class CloudAdmin:
    backend = "aws"

    def __init__(self, settings: Settings,
                 session_factory: Callable[..., Any] = boto3.session.Session):
        if not settings.cloud_configured:
            raise NotConfigured
        self.region = settings.cloud_region
        self.bucket_prefix = settings.cloud_bucket_prefix
        self.user_prefix = settings.cloud_user_prefix
        self._session_factory = session_factory
        self._access = settings.cloud_admin_access_key
        self._secret = settings.cloud_admin_secret_key
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.last_check: dict[str, Any] | None = None
        self._account: str | None = None
        self._caller_arn: str | None = None
        self._config = Config(
            region_name=self.region, connect_timeout=5, read_timeout=20,
            retries={"max_attempts": 3, "mode": "standard"},
            user_agent_extra="bucketeer",
        )

    def __repr__(self) -> str:  # never the keys
        return f"CloudAdmin(region={self.region!r})"

    # -- plumbing ----------------------------------------------------------------------

    def client(self, service: str):
        """The boto3 client for `service` (cached; boto3 clients are thread-safe)."""
        if service not in SERVICES:
            raise ValueError(f"unknown service {service}")
        with self._lock:
            c = self._clients.get(service)
            if c is None:
                session = self._session_factory(
                    aws_access_key_id=self._access,
                    aws_secret_access_key=self._secret.get_secret_value(),
                    region_name=self.region)
                c = self._clients[service] = session.client(service, config=self._config)
            return c

    def _call_sync(self, service: str, operation: str, params: dict) -> dict:
        try:
            return getattr(self.client(service), operation)(**params)
        except ClientError as e:
            err = e.response.get("Error", {})
            code = str(err.get("Code") or "Unknown")
            status = int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 502)
            # 404 is an expected answer (free bucket name, unknown user)
            log.log(logging.DEBUG if status == 404 else logging.WARNING,
                    "AWS %s.%s refused: %s: %s", service, operation, code,
                    err.get("Message", ""))
            raise CloudError(status, code, f"AWS refused {operation} ({code})",
                             str(err.get("Message") or "")) from None
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as e:
            log.warning("AWS %s.%s unreachable: %s", service, operation, type(e).__name__)
            raise CloudUnavailable(f"AWS could not be reached ({operation})") from None
        except BotoCoreError as e:
            # e.g. no credentials, bad parameters: the class name says enough
            log.warning("AWS %s.%s failed: %s", service, operation, type(e).__name__)
            raise CloudError(502, type(e).__name__,
                             f"AWS call {operation} failed ({type(e).__name__})") from None

    async def call(self, service: str, operation: str, **params) -> dict:
        return await asyncio.to_thread(self._call_sync, service, operation, params)

    async def aclose(self) -> None:
        """Nothing to close (boto3 pools close with the process); kept for symmetry."""

    # -- naming ------------------------------------------------------------------------

    def user_name(self, username: str) -> str:
        if username.lower() in reserved_usernames():
            raise CloudError(409, "reserved",
                             f"The username {username.lower()} is reserved for Bucketeer itself")
        return f"{self.user_prefix}{username.lower()}"

    def backup_user_name(self) -> str:
        return f"{self.user_prefix}{BACKUP_USER}"

    def new_backup_bucket_name(self) -> str:
        return f"{self.bucket_prefix}backup-{secrets.token_hex(6)}"

    @staticmethod
    def boundary_arn(account: str) -> str:
        return f"arn:aws:iam::{account}:policy{USER_PATH}{BOUNDARY_POLICY}"

    def s3_with_key(self, access: str, secret: str):
        """An S3 client signed with another identity's key (the backup user's temporary
        key). Not cached: the key lives for one operation."""
        session = self._session_factory(aws_access_key_id=access, aws_secret_access_key=secret,
                                        region_name=self.region)
        return session.client("s3", config=self._config)

    @staticmethod
    def owner_part(username: str) -> str:
        """The username part of a person's cloud bucket names: lowercased, every character a
        bucket name can't hold (`.`, `_`, `@`, `+`) a hyphen. `backup`/`billing` (alone or
        followed by `-`) are refused (409 `reserved`): those names belong to system buckets.
        Access never depends on this name (bucket policies do), so two names sharing a slug
        only compete for bucket names."""
        slug = re.sub(r"[^a-z0-9-]", "-", username.lower()).strip("-")
        if not slug or any(slug == w or slug.startswith(w + "-") for w in SYSTEM_BUCKET_WORDS):
            raise CloudError(409, "reserved", "Your user name can't be used for cloud bucket "
                                              "names. Ask an administrator.")
        return slug

    def bucket_name(self, username: str, name: str) -> str:
        return f"{self.bucket_prefix}{self.owner_part(username)}-{name}"

    # -- helpers later items extend ----------------------------------------------------

    async def caller_identity(self) -> dict:
        res = await self.call("sts", "get_caller_identity")
        return {"account": res.get("Account"), "arn": res.get("Arn")}

    async def account_id(self) -> str:
        """The AWS account id (STS, cached after the first success)."""
        if self._account is None:
            ident = await self.caller_identity()
            self._account, self._caller_arn = ident["account"], ident["arn"]
        return str(self._account)

    async def own_user_name(self) -> str | None:
        """The IAM user name of Bucketeer's own identity (from the STS ARN), or None."""
        await self.account_id()
        arn = self._caller_arn or ""
        return arn.rsplit("/", 1)[-1] if ":user/" in arn else None

    async def person_boundary_arn(self) -> str:
        return self.boundary_arn(await self.account_id())

    async def user_arn(self, name: str) -> str:
        return f"arn:aws:iam::{await self.account_id()}:user{USER_PATH}{name}"

    async def user_tags(self, name: str) -> dict[str, str]:
        res = await self.call("iam", "list_user_tags", UserName=name)
        return {t["Key"]: t["Value"] for t in res.get("Tags", [])}

    async def is_ours(self, user: dict, owner: str) -> bool:
        """True only for a person user Bucketeer made: path `/bucketeer/`, the boundary and
        the `bucketeer-owner` tag naming `owner`. Anything else is never adopted."""
        boundary = (user.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
        if user.get("Path") != USER_PATH or boundary != await self.person_boundary_arn():
            return False
        tags = await self.user_tags(user["UserName"])
        return tags.get(OWNER_TAG, "").lower() == owner.lower()

    async def create_person_user(self, name: str, owner: str) -> dict:
        """Create `name` under `/bucketeer/` WITH the boundary (the only way the admin policy
        lets Bucketeer create users), tagged with its owner's AD name."""
        res = await self.call(
            "iam", "create_user", UserName=name, Path=USER_PATH,
            PermissionsBoundary=await self.person_boundary_arn(),
            Tags=[{"Key": OWNER_TAG, "Value": owner.lower()},
                  {"Key": "bucketeer-role", "Value": "person"}])
        return res["User"]

    async def list_access_keys(self, name: str) -> list[dict]:
        """`[{accessKey, status (Active|Inactive), createdAt}]`, oldest first. No secrets
        exist here: AWS returns a secret only once, from CreateAccessKey."""
        res = await self.call("iam", "list_access_keys", UserName=name)
        keys = [{"accessKey": k["AccessKeyId"], "status": k.get("Status", "Active"),
                 "createdAt": _iso(k.get("CreateDate"))}
                for k in res.get("AccessKeyMetadata", [])]
        return sorted(keys, key=lambda k: (k["createdAt"] or "", k["accessKey"]))

    async def create_access_key(self, name: str) -> dict:
        """`{accessKey, secretKey, status, createdAt}`: the ONLY time the secret exists."""
        k = (await self.call("iam", "create_access_key", UserName=name))["AccessKey"]
        return {"accessKey": k["AccessKeyId"], "secretKey": k["SecretAccessKey"],
                "status": k.get("Status", "Active"), "createdAt": _iso(k.get("CreateDate"))}

    async def delete_access_key(self, name: str, access_key: str) -> None:
        await self.call("iam", "delete_access_key", UserName=name, AccessKeyId=access_key)

    async def set_access_key_status(self, name: str, access_key: str, active: bool) -> None:
        await self.call("iam", "update_access_key", UserName=name, AccessKeyId=access_key,
                        Status="Active" if active else "Inactive")

    @staticmethod
    def user_policy() -> dict:
        """The inline policy of every person's IAM user: list buckets, nothing else. Bucket
        access comes from each bucket's own policy (`grant_bucket_owner`)."""
        return {"Version": "2012-10-17", "Statement": [{
            "Sid": "ListBuckets", "Effect": "Allow",
            "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"], "Resource": "*"}]}

    async def put_user_policy(self, name: str) -> dict:
        """Write `bucketeer-buckets` on the IAM user `name`."""
        doc = self.user_policy()
        await self.call("iam", "put_user_policy", UserName=name, PolicyName=USER_POLICY,
                        PolicyDocument=json.dumps(doc, separators=(",", ":")))
        return doc

    @staticmethod
    def owner_statement(bucket: str, user_arn: str) -> dict:
        arn = f"arn:aws:s3:::{bucket}"
        return {"Sid": OWNER_SID, "Effect": "Allow", "Principal": {"AWS": user_arn},
                "Action": BUCKET_ACTIONS + OBJECT_ACTIONS, "Resource": [arn, f"{arn}/*"]}

    async def _bucket_policy(self, bucket: str) -> dict:
        try:
            res = await self.call("s3", "get_bucket_policy", Bucket=bucket)
        except CloudError as e:
            if e.code == "NoSuchBucketPolicy":
                return {"Version": "2012-10-17", "Statement": []}
            raise
        doc = json.loads(res.get("Policy") or "{}")
        stmts = doc.get("Statement", [])
        doc["Statement"] = stmts if isinstance(stmts, list) else [stmts]
        return doc

    async def _write_bucket_policy(self, bucket: str, doc: dict) -> None:
        if doc["Statement"]:
            await self.call("s3", "put_bucket_policy", Bucket=bucket,
                            Policy=json.dumps(doc, separators=(",", ":")))
        else:  # S3 refuses an empty policy
            await self.call("s3", "delete_bucket_policy", Bucket=bucket)

    async def grant_bucket_owner(self, bucket: str, user_arn: str) -> dict:
        """Give `user_arn` (the owner's IAM user) access to `bucket` through the bucket
        policy statement `bucketeer-owner`. Other statements are kept as they are; only
        `bucketeer-*` Sids are Bucketeer's. Read-modify-write (no conditional write in S3)."""
        if not bucket.startswith(self.bucket_prefix):
            raise ValueError(f"{bucket} is not a Bucketeer bucket")
        doc = await self._bucket_policy(bucket)
        doc["Statement"] = [s for s in doc["Statement"] if s.get("Sid") != OWNER_SID]
        doc["Statement"].append(self.owner_statement(bucket, user_arn))
        doc.setdefault("Version", "2012-10-17")
        waits = list(GRANT_WAITS)
        while True:
            try:
                await self._write_bucket_policy(bucket, doc)
                return doc
            except CloudError as e:
                fresh = (e.code == "MalformedPolicy"
                         and "invalid principal" in e.aws_message.lower())
                if not fresh or not waits:
                    raise
                log.info("bucket policy of %s: the owner's IAM user is not visible yet; "
                         "retrying", bucket)
                await _sleep(waits.pop(0))

    async def revoke_bucket_owner(self, bucket: str) -> dict:
        """Remove the `bucketeer-owner` statement (the policy goes when it was the last)."""
        if not bucket.startswith(self.bucket_prefix):
            raise ValueError(f"{bucket} is not a Bucketeer bucket")
        doc = await self._bucket_policy(bucket)
        kept = [s for s in doc["Statement"] if s.get("Sid") != OWNER_SID]
        if len(kept) != len(doc["Statement"]):
            doc["Statement"] = kept
            await self._write_bucket_policy(bucket, doc)
        return doc

    async def get_user(self, name: str) -> dict | None:
        """The IAM user under `/bucketeer/`, or None when there is none we can see.

        The admin policy allows GetUser only on `user/bucketeer/<prefix>*`. IAM checks a name
        it cannot find, or one under another path, as `user/<name>`, so real AWS answers
        AccessDenied (not NoSuchEntity) for both: neither is one of ours. CreateUser then
        tells them apart (EntityAlreadyExists = the name is taken outside our path)."""
        try:
            return (await self.call("iam", "get_user", UserName=name))["User"]
        except CloudError as e:
            if e.code == "NoSuchEntity":
                return None
            if e.code == "AccessDenied":
                # Expected for a missing user or one outside our path, but a broken AWS policy
                # looks the same. Never log AWS's message: it holds ARNs and the account id.
                log.info("IAM GetUser for %s answered AccessDenied: treated as no such user "
                         "under %s. If this user should exist, the AWS policy may be out of "
                         "date (re-run tools/aws-setup).", name, USER_PATH)
                return None
            raise

    async def list_users(self) -> list[dict]:
        """Every IAM user under the per-person path (all pages)."""
        out, marker = [], None
        while True:
            params = {"PathPrefix": USER_PATH, **({"Marker": marker} if marker else {})}
            res = await self.call("iam", "list_users", **params)
            out += res.get("Users", [])
            if not res.get("IsTruncated"):
                return out
            marker = res.get("Marker")

    async def list_buckets(self) -> list[dict]:
        """The account's buckets that carry the Bucketeer prefix."""
        res = await self.call("s3", "list_buckets")
        return [b for b in res.get("Buckets", [])
                if str(b.get("Name", "")).startswith(self.bucket_prefix)]

    async def bucket_status(self, name: str) -> str:
        """`free` (404), `taken` (403: someone else's), `exists` (200: in this account)."""
        try:
            await self.call("s3", "head_bucket", Bucket=name)
            return "exists"
        except CloudError as e:
            if e.status == 404 or e.code in ("404", "NoSuchBucket", "NotFound"):
                return "free"
            if e.status == 403 or e.code in ("403", "Forbidden"):
                return "taken"
            if e.status == 301 or e.code in ("301", "PermanentRedirect"):
                return "elsewhere"
            raise

    # -- cloud buckets (P6-10, cloud_buckets.py) -----------------------------------------

    async def create_bucket(self, name: str) -> None:
        if not name.startswith(self.bucket_prefix):
            raise ValueError(f"{name} is not a Bucketeer bucket")
        params: dict[str, Any] = {"Bucket": name}
        if self.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        await self.call("s3", "create_bucket", **params)

    async def delete_bucket(self, name: str) -> None:
        if not name.startswith(self.bucket_prefix):
            raise ValueError(f"{name} is not a Bucketeer bucket")
        await self.call("s3", "delete_bucket", Bucket=name)

    async def secure_bucket(self, name: str, versioning: bool) -> None:
        """Block Public Access, SSE-S3 default encryption, BucketOwnerEnforced, versioning."""
        await self.call("s3", "put_public_access_block", Bucket=name,
                        PublicAccessBlockConfiguration={
                            "BlockPublicAcls": True, "IgnorePublicAcls": True,
                            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        await self.call("s3", "put_bucket_encryption", Bucket=name,
                        ServerSideEncryptionConfiguration={"Rules": [{
                            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        await self.call("s3", "put_bucket_ownership_controls", Bucket=name,
                        OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
        if versioning:
            await self.call("s3", "put_bucket_versioning", Bucket=name,
                            VersioningConfiguration={"Status": "Enabled"})

    async def object_probe(self, name: str) -> tuple[int, bool]:
        """(current objects seen, more exist) from one ListObjectsV2 page (ListBucket only:
        the admin user may neither read objects nor list versions)."""
        res = await self.call("s3", "list_objects_v2", Bucket=name, MaxKeys=1000)
        count = int(res.get("KeyCount") or len(res.get("Contents", [])))
        return count, bool(res.get("IsTruncated"))

    async def bucket_size(self, bucket: str, now: datetime | None = None,
                          days: int = 4) -> dict[str, Any] | None:
        """The bucket's size from CloudWatch `BucketSizeBytes`, summed over every storage
        type it reports (daily, 1-2 days late): `{bytes, asOf: YYYY-MM-DD}`, or None when
        CloudWatch has nothing yet."""
        end = now or datetime.now(UTC)
        start = end - timedelta(days=days)
        res = await self.call("cloudwatch", "list_metrics", Namespace="AWS/S3",
                              MetricName="BucketSizeBytes",
                              Dimensions=[{"Name": "BucketName", "Value": bucket}])
        types = sorted({d["Value"] for m in res.get("Metrics", [])
                        for d in m.get("Dimensions", []) if d.get("Name") == "StorageType"})
        total, latest = 0.0, None
        for t in types:
            pts = (await self.call(
                "cloudwatch", "get_metric_statistics", Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[{"Name": "BucketName", "Value": bucket},
                            {"Name": "StorageType", "Value": t}],
                StartTime=start, EndTime=end, Period=86400, Statistics=["Average"],
            )).get("Datapoints", [])
            if not pts:
                continue
            last = max(pts, key=lambda p: p["Timestamp"])
            total += float(last["Average"])
            latest = last["Timestamp"] if latest is None else max(latest, last["Timestamp"])
        if latest is None:
            return None
        return {"bytes": int(total), "asOf": latest.astimezone(UTC).date().isoformat()}

    async def bucket_metric(self, bucket: str, metric: str, storage_type: str,
                            start: datetime, end: datetime) -> float | None:
        """Latest daily value of an S3 CloudWatch storage metric (`BucketSizeBytes` with
        e.g. `StandardStorage`, or `NumberOfObjects` with `AllStorageTypes`), or None."""
        res = await self.call(
            "cloudwatch", "get_metric_statistics", Namespace="AWS/S3", MetricName=metric,
            Dimensions=[{"Name": "BucketName", "Value": bucket},
                        {"Name": "StorageType", "Value": storage_type}],
            StartTime=start, EndTime=end, Period=86400, Statistics=["Average"])
        points = sorted(res.get("Datapoints", []), key=lambda p: p["Timestamp"])
        return float(points[-1]["Average"]) if points else None

    # -- connection check --------------------------------------------------------------

    async def check(self) -> dict[str, Any]:
        """STS identity plus one harmless IAM and S3 read. Never raises."""
        result: dict[str, Any] = {"ok": False, "accountId": None, "region": self.region,
                                  "reason": None, "checkedAt": _now()}
        try:
            ident = await self.caller_identity()
            result["accountId"] = ident["account"]
            await self.call("iam", "list_users", PathPrefix=USER_PATH, MaxItems=1)
            await self.call("s3", "list_buckets")
            result["ok"] = True
        except CloudError as e:
            result["reason"] = e.message
        except Exception as e:  # noqa: BLE001 - a check reports, it never crashes a page
            log.exception("cloud check failed")
            result["reason"] = f"The check failed ({type(e).__name__})"
        self.last_check = result
        log.info("cloud check: %s", "ok" if result["ok"] else result["reason"])
        return result

    @property
    def available(self) -> bool:
        """True once the most recent connection check passed."""
        return bool(self.last_check and self.last_check["ok"])

    async def monitor_loop(self, sleep: Callable = asyncio.sleep) -> None:
        """Check at start, then hourly (every 5 min while failing). Lifespan task."""
        while True:
            res = await self.check()
            await sleep(RECHECK_OK if res["ok"] else RECHECK_FAILED)
