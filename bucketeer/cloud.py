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
  `RESERVED_USERNAMES` would collide with the system users and are refused for cloud.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
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
RESERVED_USERNAMES = frozenset({"bucketeer-admin", "bucketeer-backup"})
CHECK_ON_START = True
NO_CLOUD = object()  # create_app(cloud_admin=NO_CLOUD): no cloud even when configured
RECHECK_OK = 3600.0
RECHECK_FAILED = 300.0
SERVICES = ("sts", "iam", "s3", "cloudwatch")


class CloudError(Exception):
    """An AWS call failed. `str()` is safe to show an admin: no secrets, URLs or ARNs."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class CloudUnavailable(CloudError):
    """AWS could not be reached (network, DNS, timeout)."""

    def __init__(self, message: str = "AWS could not be reached"):
        super().__init__(503, "unavailable", message)


class NotConfigured(CloudError):
    def __init__(self):
        super().__init__(409, "not_configured", "Cloud storage is not set up")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
            raise CloudError(status, code, f"AWS refused {operation} ({code})") from None
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
        return f"{self.user_prefix}{username.lower()}"

    def bucket_name(self, username: str, name: str) -> str:
        return f"{self.bucket_prefix}{username.lower()}-{name}"

    # -- helpers later items extend ----------------------------------------------------

    async def caller_identity(self) -> dict:
        res = await self.call("sts", "get_caller_identity")
        return {"account": res.get("Account"), "arn": res.get("Arn")}

    async def get_user(self, name: str) -> dict | None:
        """The IAM user, or None when it does not exist."""
        try:
            return (await self.call("iam", "get_user", UserName=name))["User"]
        except CloudError as e:
            if e.code == "NoSuchEntity":
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
            raise

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
