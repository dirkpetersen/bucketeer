"""Keeps a moto S3 server in line with the mock croit API (dev/test tooling only).

Imports nothing from `bucketeer/` and nothing from `croit_mock` at runtime, so it
works both in tests (`mock.s3_bridge`) and next to `mock/croit_mock.py` run as a
script. Usage:

    server = start_moto()                       # ThreadedMotoServer on a free port
    bridge = S3Bridge(endpoint_url(server))
    app = create_app(hooks=bridge.make_hooks(MockHooks), rgw_endpoint=bridge.host_port)
    bridge.seed_objects(app.state.croit)        # after the seed buckets exist

What is mirrored: bucket create (owner's own keys, versioning, object lock), versioning
changes, bucket delete (objects and versions removed first). moto does NOT enforce RGW
identities: it accepts any credentials and puts every bucket in one account, so the keys
are only used to create and fill buckets "as the owner". Per-user authorization has to be
tested at the Bucketeer layer. Tenant buckets (`acme:reports`) live in moto under their
short name (`reports`), the way a tenant user addresses them over S3; moto's namespace is
global, so two tenants cannot both own `reports` here.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mock.croit_mock import Bucket, CroitState

REGION = "us-east-1"
# Used for buckets whose owner has no key (any credentials work in moto).
FALLBACK_KEYS = ("mock-admin-access", "mock-admin-secret")
VERSIONING = {"ENABLED": "Enabled", "SUSPENDED": "Suspended"}


def start_moto(host: str = "127.0.0.1", port: int = 0):
    """Start a moto server thread; port 0 picks a free port. Call `.stop()` when done."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address=host, port=port, verbose=False)
    server.start()
    return server


def endpoint_url(server) -> str:
    host, port = server.get_host_and_port()
    return f"http://{host}:{port}"


def short_name(full: str) -> str:
    return full.partition(":")[2] or full


class S3Bridge:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.host_port = endpoint.split("://", 1)[-1]
        self._keys: dict[str, dict[str, str]] = {}  # uid -> {access: secret}
        self._lock = threading.RLock()

    # -- clients --
    def client(self, access: str, secret: str):
        return boto3.client(
            "s3", endpoint_url=self.endpoint, region_name=REGION,
            aws_access_key_id=access, aws_secret_access_key=secret,
            config=Config(retries={"max_attempts": 1}, proxies={}),
        )

    def owner_client(self, uid: str):
        with self._lock:
            keys = self._keys.get(uid)
            access, secret = next(iter(keys.items())) if keys else FALLBACK_KEYS
        return self.client(access, secret)

    # -- hooks (called by CroitState under its own lock) --
    def key_added(self, uid: str, access: str, secret: str):
        with self._lock:
            self._keys.setdefault(uid, {})[access] = secret

    def key_removed(self, uid: str, access: str):
        with self._lock:
            self._keys.get(uid, {}).pop(access, None)

    def bucket_created(self, bucket: Bucket):
        s3 = self.owner_client(bucket.owner)
        name = short_name(bucket.name)
        s3.create_bucket(Bucket=name, ObjectLockEnabledForBucket=bucket.object_lock)
        if bucket.versioning in VERSIONING:
            self._set_versioning(s3, name, bucket.versioning)

    def bucket_updated(self, old: str, bucket: Bucket):
        s3 = self.owner_client(bucket.owner)
        if bucket.versioning in VERSIONING:
            self._set_versioning(s3, short_name(bucket.name), bucket.versioning)

    def bucket_deleted(self, bucket: Bucket):
        s3 = self.owner_client(bucket.owner)
        name = short_name(bucket.name)
        self.purge(s3, name)
        s3.delete_bucket(Bucket=name)

    def bucket_usage(self, bucket: Bucket) -> tuple[int, int]:
        """(objects, bytes) from moto: versions and delete markers count as objects."""
        s3 = self.owner_client(bucket.owner)
        count = total = 0
        try:
            pages = s3.get_paginator("list_object_versions").paginate(
                Bucket=short_name(bucket.name))
            for page in pages:
                for v in page.get("Versions", []):
                    count, total = count + 1, total + v["Size"]
                count += len(page.get("DeleteMarkers", []))
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchBucket":
                raise
        return count, total

    @staticmethod
    def _set_versioning(s3, name: str, state: str):
        s3.put_bucket_versioning(
            Bucket=name, VersioningConfiguration={"Status": VERSIONING[state]})

    @staticmethod
    def purge(s3, name: str):
        """Delete every object, version and delete marker (also under object lock)."""
        try:
            pages = s3.get_paginator("list_object_versions").paginate(Bucket=name)
            for page in pages:
                items = page.get("Versions", []) + page.get("DeleteMarkers", [])
                for item in items:
                    s3.delete_object(Bucket=name, Key=item["Key"], VersionId=item["VersionId"],
                                     BypassGovernanceRetention=True)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchBucket":
                raise

    def make_hooks(self, hooks_cls):
        """Build a `MockHooks` (passed in so the class identity always matches)."""
        return hooks_cls(
            bucket_created=self.bucket_created, bucket_updated=self.bucket_updated,
            bucket_deleted=self.bucket_deleted, key_added=self.key_added,
            key_removed=self.key_removed, bucket_usage=self.bucket_usage,
        )

    # -- data --
    def seed_objects(self, state: CroitState):
        """Fill each seeded bucket that croit reports as non-empty with exactly
        `num_objects` objects totalling `size_bytes`, so usage matches moto."""
        for bucket in list(state.buckets.values()):
            n, total = bucket.num_objects, bucket.size_bytes
            if not n:
                continue
            s3 = self.owner_client(bucket.owner)
            base, extra = divmod(total, n)
            for i in range(n):
                size = base + (1 if i < extra else 0)
                s3.put_object(Bucket=short_name(bucket.name), Key=f"data/file-{i:04d}.bin",
                              Body=b"x" * size)
