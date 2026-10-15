"""Keeps a moto S3 server in line with the mock croit API (dev/test tooling only).

Buckets that users create or delete over S3 (`CreateBucket`, `PutBucketVersioning`,
`DeleteBucket` with their own keys, Phase 5 A3 and A4) are mirrored the other way:
`start_moto` wraps moto in a WSGI middleware that, after moto accepted a path-style
`PUT /<bucket>`, `PUT /<bucket>?versioning` or `DELETE /<bucket>`, tells the bridge
registered for that endpoint (`seed_objects` registers it) which access key did it.
The bridge maps the key to its owner in the mock croit state (`mirror_s3_bucket` /
`mirror_s3_versioning` / `mirror_s3_delete`, no hook fired), so the croit mock and the RGW
Admin Ops mock list the bucket with the right owner, versioning, object lock and placement. A
`LocationConstraint` of the form `<region>:<placement>` is recorded as the placement and
removed from the request moto sees (moto knows no placements). Like RGW, the part before
the colon must be empty or equal the zonegroup api name (`ZONEGROUP_API_NAME`, `default`),
and the placement id must be one the mock croit lists; anything else is a 400
`InvalidLocationConstraint`. The bridge's own requests carry
a marker user agent and are skipped. Keys that belong to no mock user are ignored.

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

import io
import re
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
ZONEGROUP_API_NAME = "default"  # what RGW compares the LocationConstraint prefix with
BRIDGE_AGENT = "bucketeer-mock-bridge"  # user agent of the bridge's own requests
_BRIDGES: dict[str, S3Bridge] = {}  # moto endpoint -> the bridge mirroring S3 calls
_ACCESS = re.compile(r"Credential=([^/,\s]+)/")
_LOCATION = re.compile(rb"<LocationConstraint>([^<]*)</LocationConstraint>")
_STATUS = re.compile(rb"<Status>([A-Za-z]+)</Status>")


class S3Mirror:
    """WSGI middleware around moto: reports successful user bucket creates and versioning
    changes to the bridge of that endpoint (see the module docstring)."""

    def __init__(self, app, endpoint):
        self.app, self.endpoint = app, endpoint  # endpoint: callable -> str

    def __call__(self, environ, start_response):
        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO", "")
        query = environ.get("QUERY_STRING", "")
        bridge = _BRIDGES.get(self.endpoint())
        kind = None
        if (bridge is not None and path.count("/") == 1 and len(path) > 1
                and BRIDGE_AGENT not in environ.get("HTTP_USER_AGENT", "")):
            if method == "PUT":
                kind = "versioning" if query == "versioning" else "create" if not query else None
            elif method == "DELETE" and not query:
                kind = "delete"
        if kind is None:
            return self.app(environ, start_response)
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        info = {"object_lock": environ.get("HTTP_X_AMZ_BUCKET_OBJECT_LOCK_ENABLED", "")
                .lower() == "true", "placement": None}
        if kind == "create":
            m = _LOCATION.search(body)
            if m and b":" in m.group(1):
                problem = bridge.check_location(m.group(1).decode())
                if problem:
                    return _refuse(start_response, problem)
                info["placement"] = m.group(1).decode().partition(":")[2] or None
                body = _LOCATION.sub(b"", body)  # moto knows regions, not placements
        elif kind == "versioning":
            m = _STATUS.search(body)
            info["status"] = m.group(1).decode() if m else ""
        environ["wsgi.input"] = io.BytesIO(body)
        environ["CONTENT_LENGTH"] = str(len(body))
        seen = {}

        def capture(status, headers, exc_info=None):
            seen["status"] = int(status.split()[0])
            return start_response(status, headers, exc_info)

        result = self.app(environ, capture)
        if 200 <= seen.get("status", 500) < 300:
            auth = _ACCESS.search(environ.get("HTTP_AUTHORIZATION", ""))
            try:
                bridge.s3_event(kind, auth.group(1) if auth else "", path[1:], info)
            except Exception:  # mirroring is best effort; never break the S3 answer
                pass
        return result


def _refuse(start_response, message: str):
    xml = (f"<?xml version='1.0' encoding='UTF-8'?><Error><Code>InvalidLocationConstraint"
           f"</Code><Message>{message}</Message></Error>").encode()
    start_response("400 Bad Request", [("Content-Type", "application/xml"),
                                       ("Content-Length", str(len(xml)))])
    return [xml]


def start_moto(host: str = "127.0.0.1", port: int = 0):
    """Start a moto server thread (with the S3 mirror middleware); port 0 picks a free
    port. Call `.stop()` when done."""
    from moto.server import DomainDispatcherApplication, ThreadedMotoServer, create_backend_app
    from werkzeug.serving import make_server

    class MirroredMoto(ThreadedMotoServer):
        def _server_entry(self) -> None:
            app = S3Mirror(DomainDispatcherApplication(create_backend_app),
                           lambda: endpoint_url(self))
            self._server = make_server(self._ip_address, self._port, app, True)
            self._server_ready_event.set()
            self._server.serve_forever()

    server = MirroredMoto(ip_address=host, port=port, verbose=False)
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
        self.state: CroitState | None = None  # set by seed_objects: S3 creates are mirrored
        self._lock = threading.RLock()

    # -- clients --
    def client(self, access: str, secret: str):
        return boto3.client(
            "s3", endpoint_url=self.endpoint, region_name=REGION,
            aws_access_key_id=access, aws_secret_access_key=secret,
            config=Config(retries={"max_attempts": 1}, proxies={},
                          user_agent_extra=BRIDGE_AGENT),
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

    def check_location(self, value: str) -> str | None:
        """RGW's rule for `<api name>:<placement id>`: an empty api name skips the zonegroup
        check; the placement must exist. None when fine."""
        api, _, placement = value.partition(":")
        if api and api != ZONEGROUP_API_NAME:
            return f"zonegroup api name {api!r} does not match {ZONEGROUP_API_NAME!r}"
        known = {row[0] for row in getattr(self.state, "placements", [])} | {"default-placement"}
        if self.state is not None and placement not in known:
            return f"unknown placement {placement!r}"
        return None

    def s3_event(self, kind: str, access: str, short: str, info: dict):
        """A user's S3 call reached moto (see `S3Mirror`): mirror it into the croit state."""
        state = self.state
        owner = state.access_keys.get(access) if state is not None else None
        if owner is None:
            return
        if kind == "create":
            state.mirror_s3_bucket(
                short, owner, object_lock=info["object_lock"], placement=info["placement"],
                versioning="ENABLED" if info["object_lock"] else "OFF")
        elif kind == "delete":
            state.mirror_s3_delete(short, owner)
        else:
            state.mirror_s3_versioning(short, owner, info.get("status", ""))

    # -- data --
    def seed_objects(self, state: CroitState):
        """Register this bridge to mirror users' S3 creates into `state`, then fill each
        seeded bucket that croit reports as non-empty with exactly `num_objects` objects
        totalling `size_bytes`, so usage matches moto."""
        self.state = state
        _BRIDGES[self.endpoint] = self
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
