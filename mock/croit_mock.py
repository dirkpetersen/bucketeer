#!/usr/bin/env python3
"""In-memory mock of the croit management API, for development and tests.

Dev/test tooling only: it imports nothing from `bucketeer/` and the app never
imports it. It implements the subset of docs/api/api-1.yaml that Bucketeer and
tools/croit-cert use, with croit's rules (duplicate email, user with buckets,
whole-set key PUT, bucket delete confirmation, `tenant$uid`, ErrorMessage).

Run standalone:  .venv/bin/python3 mock/croit_mock.py
  CROIT_MOCK_PORT          port (default 18080)
  CROIT_MOCK_HOST          bind address (default 127.0.0.1)
  CROIT_MOCK_PREFIX        API prefix (default /api), so CROIT_URL=http://127.0.0.1:18080/api
  CROIT_MOCK_TASK_SECONDS  how long tasks stay RUNNING (default 0.5)
  CROIT_MOCK_ADMIN_TOKEN / CROIT_MOCK_VIEWER_TOKEN   fixed API token secrets
  CROIT_MOCK_S3=moto       also start a moto S3 server (mock/s3_bridge.py) that mirrors the
                           mock's buckets and keys; point S3_ENDPOINT_URL at the printed URL
  CROIT_MOCK_S3_PORT       moto port (default 18081)

In-process (tests): `create_app(seed=True, task_seconds=0)`; the state is
`app.state.croit` (a `CroitState`). Side effects of a task apply immediately;
only the task's status lags. Pagination is not implemented: passing the
`pagination` query parameter answers 501.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

# Secrets of the seeded API tokens (random per process unless set in the env).
ADMIN_TOKEN = os.environ.get("CROIT_MOCK_ADMIN_TOKEN") or "mock-admin-" + secrets.token_hex(12)
VIEWER_TOKEN = os.environ.get("CROIT_MOCK_VIEWER_TOKEN") or "mock-viewer-" + secrets.token_hex(12)
# Seeded croit login users: name -> (password, roles).
LOGIN_USERS = {"admin": ("admin", ["admin", "viewer"]), "viewer": ("viewer", ["viewer"])}
# Seeded S3 users: uid -> (display name, email, access key, secret key).
SEED_USERS = {
    "alice": ("Alice Anderson", "alice@example.org", "ALICEACCESSKEY00001", "alice-secret-0001"),
    "bob": ("Bob Brown", "bob@example.org", "BOBACCESSKEY0000002", "bob-secret-0002"),
    "acme$carol": ("Carol Clark", "carol@acme.example", "CAROLACCESSKEY00003", "carol-secret-0003"),
}
# Seeded buckets: tenant-qualified name -> (owner, objects, bytes).
SEED_BUCKETS = {
    "alice-data": ("alice", 42, 1048576),
    "alice-empty": ("alice", 0, 0),
    "bob-shared": ("bob", 0, 0),
    "acme:reports": ("acme$carol", 0, 0),
}
ROLES = ("admin", "viewer", "securefolder")
BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class ApiError(Exception):
    """Rendered as croit's ErrorMessage: {code, message, details?}."""

    def __init__(self, status: int, message: str, details: str | None = None,
                 extra: dict | None = None):
        self.status, self.message, self.details, self.extra = status, message, details, extra


def error_body(status: int, message: str, details: str | None = None,
               extra: dict | None = None) -> dict:
    body = {"code": status, "message": message, **(extra or {})}
    if details:
        body["details"] = details
    return body


# --- state ---------------------------------------------------------------------


@dataclass
class MockHooks:
    """Optional callbacks so an S3 backend (moto) can mirror state changes."""

    bucket_created: Callable[[Bucket], None] | None = None
    bucket_updated: Callable[[str, Bucket], None] | None = None  # (old full name, bucket)
    bucket_deleted: Callable[[Bucket], None] | None = None
    key_added: Callable[[str, str, str], None] | None = None  # (uid, access, secret)
    key_removed: Callable[[str, str], None] | None = None  # (uid, access)
    # (bucket) -> (objects, bytes): the real contents; overrides the stored counters
    bucket_usage: Callable[[Bucket], tuple[int, int]] | None = None


def new_quota() -> dict:
    return {"enabled": False, "maxSize": -1, "maxObjects": -1}


@dataclass
class User:
    uid: str  # tenant-qualified: tenant$uid
    name: str
    email: str = ""
    suspended: bool = False
    keys: list[dict] = field(default_factory=list)  # {access_key, secret_key}
    user_quota: dict = field(default_factory=new_quota)
    bucket_quota: dict = field(default_factory=new_quota)
    default_placement: str | None = None
    default_storage_class: str | None = None

    @property
    def tenant(self) -> str:
        return self.uid.partition("$")[0] if "$" in self.uid else ""


@dataclass
class Bucket:
    name: str  # tenant-qualified: tenant:bucket
    owner: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    versioning: str = "OFF"  # OFF | ENABLED | SUSPENDED
    object_lock: bool = False
    acl: list[dict] | None = None
    policy: str | None = None
    quota: dict = field(default_factory=new_quota)
    placement: str = "default-placement"
    num_objects: int = 0
    size_bytes: int = 0
    mtime: str | None = None

    @property
    def tenant(self) -> str:
        return self.name.partition(":")[0] if ":" in self.name else ""


@dataclass
class Task:
    id: int
    name: str
    object: str | None
    started_by: str
    started_at: str
    started_mono: float


@dataclass
class Principal:
    username: str
    roles: list[str]


def locked(fn):
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(n)


def gen_access_key() -> str:
    return "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(20))


def gen_secret_key() -> str:
    return secrets.token_urlsafe(30)[:40]


class CroitState:
    def __init__(
        self,
        task_seconds: float = 0.5,
        session_seconds: int = 3600,
        rgw_endpoint: str = "127.0.0.1:7480",
        rgw_dns_name: str = "s3.mock.example.org",
        admin_token: str | None = None,
        viewer_token: str | None = None,
        hooks: MockHooks | None = None,
    ):
        self.lock = threading.RLock()
        self.task_seconds = task_seconds
        self.session_seconds = session_seconds
        self.rgw_endpoint = rgw_endpoint
        self.rgw_dns_name = rgw_dns_name
        self.admin_token = admin_token or ADMIN_TOKEN
        self.viewer_token = viewer_token or VIEWER_TOKEN
        self.hooks = hooks or MockHooks()
        self.logins = dict(LOGIN_USERS)
        self.sessions: dict[str, dict] = {}
        self.api_tokens: dict[int, dict] = {}  # id -> {id, name, roles, secret, expires}
        self.users: dict[str, User] = {}
        self.buckets: dict[str, Bucket] = {}
        self.access_keys: dict[str, str] = {}  # access key -> uid
        self.delete_tokens: dict[str, str] = {}  # issued ignoreWarning token -> bucket
        self.tasks: dict[int, Task] = {}
        self.next_task_id = 1
        self.next_token_id = 1
        self.cert_pem = ""
        self.ha_groups = [default_ha_group()]

    def _fire(self, name: str, *args):
        cb = getattr(self.hooks, name)
        if cb:
            try:
                cb(*args)
            except ApiError:
                raise
            except Exception as e:  # a failing S3 backend becomes an ErrorMessage
                detail = f"{type(e).__name__}: {e}"
                raise ApiError(500, f"S3 backend failed in {name}", detail) from e

    def refresh_usage(self, bucket: Bucket):
        """With a usage hook (moto), the real contents decide emptiness and usage."""
        if self.hooks.bucket_usage:
            bucket.num_objects, bucket.size_bytes = self.hooks.bucket_usage(bucket)

    # -- seed --
    def seed(self):
        self.add_api_token("mock-admin", ["admin", "viewer"], secret=self.admin_token)
        self.add_api_token("mock-viewer", ["viewer"], secret=self.viewer_token)
        for uid, (name, email, access, secret) in SEED_USERS.items():
            self.create_user(uid, name, email, keys=[(access, secret)])
        for name, (owner, objects, size) in SEED_BUCKETS.items():
            self.create_bucket(name.partition(":")[2] or name, owner)
            self.set_bucket_usage(name, objects, size)

    # -- auth --
    @locked
    def login(self, username: str, password: str) -> dict:
        entry = self.logins.get(username)
        if not entry or not secrets.compare_digest(entry[0], password):
            raise ApiError(401, "Invalid username or password")
        token = str(uuid.uuid4())
        self.sessions[token] = {
            "username": username,
            "roles": list(entry[1]),
            "expires": time.time() + self.session_seconds,
        }
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": self.session_seconds,
            "roles": list(entry[1]),
            "userId": list(self.logins).index(username) + 1,
            "passwordNeedsChange": False,
        }

    @locked
    def principal(self, bearer: str | None) -> tuple[Principal, str | None]:
        """Resolve a bearer token; returns (principal, session expiry ISO or None)."""
        if not bearer:
            raise ApiError(401, "Authentication required")
        sess = self.sessions.get(bearer)
        if sess:
            if sess["expires"] < time.time():
                del self.sessions[bearer]
                raise ApiError(401, "Session expired")
            return Principal(sess["username"], sess["roles"]), None
        for tok in self.api_tokens.values():
            if secrets.compare_digest(tok["secret"], bearer):
                if tok["expires"] and tok["expires"] < time.time():
                    raise ApiError(401, "API token expired")
                return Principal(tok["name"], list(tok["roles"])), None
        raise ApiError(401, "Invalid token")

    @locked
    def refresh_session(self, bearer: str) -> str | None:
        sess = self.sessions.get(bearer)
        if not sess:
            return None
        sess["expires"] = time.time() + self.session_seconds
        return iso_ts(sess["expires"])

    @staticmethod
    def check_roles(roles: list[str]):
        if not roles:
            raise ApiError(400, "At least one role is required")
        if "admin" in roles and "viewer" not in roles:
            raise ApiError(400, "The admin role also needs the viewer role")

    @locked
    def add_api_token(self, name, roles, expiry=None, secret=None) -> str:
        self.check_roles(roles)
        expires = None
        if expiry:
            try:
                expires = (
                    float(expiry)
                    if expiry.isdigit()
                    else datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                )
            except ValueError:
                raise ApiError(400, f"Invalid expiry: {expiry}") from None
        secret = secret or secrets.token_urlsafe(32)
        tid = self.next_token_id
        self.next_token_id += 1
        self.api_tokens[tid] = {
            "id": tid, "name": name, "roles": list(dict.fromkeys(roles)),
            "secret": secret, "expires": expires,
        }
        return secret

    @locked
    def list_api_tokens(self) -> list[dict]:
        return [
            {
                "id": t["id"], "name": t["name"], "roles": list(t["roles"]),
                "expiresBy": None if not t["expires"] else
                iso_ts(t["expires"]),
            }
            for t in self.api_tokens.values()
        ]

    @locked
    def update_api_token(self, tid: int, roles: list[str] | None):
        tok = self.api_tokens.get(tid)
        if not tok:
            raise ApiError(404, f"API token {tid} not found")
        if roles is not None:
            self.check_roles(roles)
            tok["roles"] = list(dict.fromkeys(roles))

    @locked
    def delete_api_token(self, tid: int):
        if self.api_tokens.pop(tid, None) is None:
            raise ApiError(404, f"API token {tid} not found")

    # -- tasks --
    @locked
    def new_task(self, name: str, obj: str | None, user: str) -> dict:
        task = Task(self.next_task_id, name, obj, user, now_iso(), time.monotonic())
        self.next_task_id += 1
        self.tasks[task.id] = task
        return self.task_view(task)

    @locked
    def get_task(self, tid: int) -> dict:
        task = self.tasks.get(tid)
        if not task:
            raise ApiError(404, f"Task {tid} not found")
        return self.task_view(task)

    def task_view(self, t: Task) -> dict:
        done = time.monotonic() - t.started_mono >= self.task_seconds
        return {
            "id": t.id, "name": t.name, "object": t.object, "done": done, "cancelled": False,
            "statusCode": 0 if done else -1, "state": "FINISHED" if done else "RUNNING",
            "startedAt": t.started_at, "startedBy": t.started_by,
            "output": "Done\n" if done else "", "result": None,
        }

    # -- users --
    def get_user(self, uid: str) -> User:
        user = self.users.get(uid)
        if not user:
            raise ApiError(404, f"User {uid} not found")
        return user

    def _check_email(self, email: str, owner: str | None = None):
        if email and any(
            u.email.lower() == email.lower() and u.uid != owner for u in self.users.values()
        ):
            raise ApiError(409, f"Email {email} is already used by another user")

    @locked
    def create_user(self, uid, name, email=None, tenant=None, keys=None, **extra) -> User:
        if not uid or any(c in uid for c in "/: ") or uid.count("$") > 1:
            raise ApiError(400, f"Invalid uid: {uid!r}")
        if tenant and "$" not in uid:
            uid = f"{tenant}${uid}"
        if uid in self.users:
            raise ApiError(409, f"User {uid} already exists")
        self._check_email(email or "")
        user = User(
            uid, name, email or "",
            default_placement=extra.get("defaultPlacement"),
            default_storage_class=extra.get("defaultStorageClass"),
        )
        self.users[uid] = user
        for access, secret in keys or [(gen_access_key(), gen_secret_key())]:
            self.put_key(uid, access, secret)
        return user

    @locked
    def update_user(self, uid: str, body: dict):
        user = self.get_user(uid)
        if body.get("email") is not None:
            self._check_email(body["email"], uid)
            user.email = body["email"]
        if body.get("name") is not None:
            user.name = body["name"]
        if body.get("suspended") is not None:
            user.suspended = body["suspended"]
        if "defaultPlacement" in body and body["defaultPlacement"] is not None:
            user.default_placement = body["defaultPlacement"] or None
        if "defaultStorageClass" in body and body["defaultStorageClass"] is not None:
            user.default_storage_class = body["defaultStorageClass"] or None

    @locked
    def delete_user(self, uid: str):
        user = self.get_user(uid)
        owned = [b.name for b in self.buckets.values() if b.owner == uid]
        if owned:
            raise ApiError(
                409, f"User {uid} still owns buckets and cannot be deleted",
                "Buckets: " + ", ".join(owned),
            )
        for key in list(user.keys):
            self.remove_key(uid, key["access_key"])
        del self.users[uid]

    @locked
    def set_quota(self, target: dict, body: dict):
        target["enabled"] = body["enabled"]
        if body.get("maxSize") is not None:
            target["maxSize"] = body["maxSize"]
        elif body.get("max_size_kb") is not None:
            target["maxSize"] = body["max_size_kb"] * 1024
        if body.get("maxObjects") is not None:
            target["maxObjects"] = body["maxObjects"]
        elif body.get("max_objects") is not None:
            target["maxObjects"] = body["max_objects"]

    # -- keys --
    @locked
    def put_key(self, uid: str, access: str, secret: str):
        user = self.get_user(uid)
        if not access or not secret:
            raise ApiError(400, "access key and secret key must not be empty")
        owner = self.access_keys.get(access)
        if owner and owner != uid:
            raise ApiError(409, f"Access key {access} belongs to another user")
        user.keys = [k for k in user.keys if k["access_key"] != access]
        user.keys.append({"access_key": access, "secret_key": secret})
        self.access_keys[access] = uid
        self._fire("key_added", uid, access, secret)

    @locked
    def remove_key(self, uid: str, access: str):
        user = self.get_user(uid)
        if not any(k["access_key"] == access for k in user.keys):
            raise ApiError(404, f"Key {access} not found for user {uid}")
        user.keys = [k for k in user.keys if k["access_key"] != access]
        self.access_keys.pop(access, None)
        self._fire("key_removed", uid, access)

    @locked
    def replace_keys(self, uid: str, keys: list[dict]):
        """Whole-set PUT: every key not listed is deleted."""
        user = self.get_user(uid)
        for k in keys:
            if k["user"] != uid:
                raise ApiError(400, f"Key user {k['user']!r} does not match {uid!r}",
                               "The mock supports no subusers")
        wanted = {k["access_key"] for k in keys}
        for old in [k["access_key"] for k in user.keys if k["access_key"] not in wanted]:
            self.remove_key(uid, old)
        for k in keys:
            self.put_key(uid, k["access_key"], k["secret_key"])

    def cfg_file(self, uid: str) -> dict:
        user = self.get_user(uid)
        key = user.keys[0] if user.keys else {"access_key": "", "secret_key": ""}
        text = (
            "[default]\n"
            f"access_key = {key['access_key']}\n"
            f"secret_key = {key['secret_key']}\n"
            f"host_base = {self.rgw_endpoint}\n"
            f"host_bucket = {self.rgw_endpoint}\n"
            "use_https = False\n"
            f"# DNS name: host_base = {self.rgw_dns_name}\n"
        )
        return {
            "fileName": f"{uid.replace('$', '_')}.s3cfg",
            "url": f"/s3/users/{uid}/cfg-file/{uuid.uuid4().hex}",
            "fileContents": text,
        }

    # -- buckets --
    def get_bucket(self, name: str) -> Bucket:
        bucket = self.buckets.get(name)
        if not bucket:
            raise ApiError(404, f"Bucket {name} not found")
        return bucket

    @staticmethod
    def delete_sentence(name: str) -> str:
        return f"I understand that this will delete all data in bucket {name}"

    @locked
    def create_bucket(self, name, owner, acl=None, versioning=None, object_lock=None,
                      placement=None) -> Bucket:
        user = self.get_user(owner)
        if ":" in name or not BUCKET_NAME.match(name):
            raise ApiError(400, f"Invalid bucket name {name!r}",
                           "3-63 chars of a-z 0-9 . - ; the tenant comes from the owner")
        if versioning not in (None, "OFF", "ENABLED"):
            raise ApiError(400, "Only OFF and ENABLED may be given at creation")
        if acl is not None and not acl:
            raise ApiError(400, "An empty ACL is refused")
        full = f"{user.tenant}:{name}" if user.tenant else name
        if full in self.buckets:
            raise ApiError(409, f"Bucket {full} already exists")
        bucket = Bucket(
            full, owner,
            versioning="ENABLED" if (object_lock or versioning == "ENABLED") else "OFF",
            object_lock=bool(object_lock), acl=acl, placement=placement or "default-placement",
        )
        self._fire("bucket_created", bucket)  # state changes only if the backend succeeded
        self.buckets[full] = bucket
        return bucket

    @locked
    def update_bucket(self, name: str, body: dict):
        bucket = self.get_bucket(name)
        old = bucket.name
        if body.get("acl") is not None:
            if not body["acl"]:
                raise ApiError(400, "An empty ACL is refused")
            bucket.acl = body["acl"]
        v = body.get("versioning")
        if v is not None:
            if v == "UNKNOWN":
                raise ApiError(400, "UNKNOWN is not a valid versioning setting")
            if bucket.object_lock and v != "ENABLED":
                raise ApiError(400, "Versioning cannot be changed on an object-lock bucket")
            if v == "OFF" and bucket.versioning == "ENABLED":
                v = "SUSPENDED"  # S3 cannot turn versioning off again
            elif v == "OFF" and bucket.versioning == "SUSPENDED":
                v = "SUSPENDED"
            bucket.versioning = v
        if body.get("owner") and body["owner"] != bucket.owner:
            new_owner = self.get_user(body["owner"])
            short = bucket.name.partition(":")[2] or bucket.name
            new_name = f"{new_owner.tenant}:{short}" if new_owner.tenant else short
            if new_name != old and new_name in self.buckets:
                raise ApiError(409, f"Bucket {new_name} already exists")
            del self.buckets[old]
            bucket.owner, bucket.name = new_owner.uid, new_name
            self.buckets[new_name] = bucket
        self._fire("bucket_updated", old, bucket)

    @locked
    def delete_bucket(self, name: str, body: dict, ignore_warning: str | None = None) -> Bucket:
        """Empty bucket: deleted at once. Non-empty: needs deleteData, the exact
        sentence and an `ignoreWarning` token issued by an earlier refusal."""
        bucket = self.get_bucket(name)
        self.refresh_usage(bucket)
        if body["name"] != name:
            raise ApiError(400, f"The name {body['name']!r} does not match bucket {name!r}")
        if bucket.num_objects > 0:
            sentence = self.delete_sentence(name)
            token_ok = ignore_warning is not None and \
                self.delete_tokens.get(ignore_warning) == name
            if not (body.get("deleteData") and body.get("thisWillDeleteData") == sentence
                    and token_ok):
                self._refuse_delete(name, sentence)
        self._fire("bucket_deleted", bucket)  # may fail (e.g. retained objects): keep state
        self.delete_tokens.pop(ignore_warning, None)
        del self.buckets[name]
        return bucket

    def _refuse_delete(self, name: str, sentence: str):
        token = str(uuid.uuid4())
        self.delete_tokens[token] = name
        text = (
            f"Bucket {name} is not empty. To delete it and its data, send deleteData=true, "
            f'thisWillDeleteData="{sentence}" and ignoreWarning={token}'
        )
        raise ApiError(400, text, sentence, {"ignorableWarning": {
            "warningText": text, "token": token, "affirmativeLabel": "Delete"}})

    @locked
    def set_bucket_usage(self, name: str, objects: int, size_bytes: int):
        bucket = self.get_bucket(name)
        bucket.num_objects, bucket.size_bytes = objects, size_bytes
        bucket.mtime = now_iso() if objects else None

    # -- views --
    @staticmethod
    def quota_view(q: dict) -> dict:
        return {
            **q, "maxSizeFormatted": "unlimited" if q["maxSize"] < 0 else human(q["maxSize"]),
            "maxObjectsFormatted": "unlimited" if q["maxObjects"] < 0 else str(q["maxObjects"]),
            "max_objects": q["maxObjects"],
            "max_size_kb": -1 if q["maxSize"] < 0 else q["maxSize"] // 1024,
        }

    def user_view(self, u: User) -> dict:
        return {
            "uid": u.uid, "name": u.name, "email": u.email, "suspended": u.suspended,
            "subusers": [], "subusersFormatted": "",
            "userQuota": self.quota_view(u.user_quota),
            "bucketQuota": self.quota_view(u.bucket_quota),
            "defaultPlacement": u.default_placement,
            "defaultStorageClass": u.default_storage_class,
        }

    def bucket_view(self, b: Bucket) -> dict:
        self.refresh_usage(b)
        kib = math.ceil(b.size_bytes / 1024)
        usage = {
            "numObjects": float(b.num_objects), "num_objects": float(b.num_objects),
            "size": b.size_bytes, "sizeKiB": kib, "sizeKiBActual": kib,
            "size_kb": kib, "size_kb_actual": kib, "sizeFormatted": human(b.size_bytes),
        }
        return {
            "bucket": b.name, "id": b.id, "owner": b.owner, "tenant": b.tenant,
            "placement_rule": b.placement, "zonegroup": "default", "mtime": b.mtime,
            "bucket_quota": self.quota_view(b.quota),
            "usage": {"default": usage}, "usageSum": usage,
        }

    def bucket_details(self, b: Bucket) -> dict:
        acl = b.acl or [{
            "grantee": "SINGLE_USER", "permission": "FullControl", "userId": b.owner,
            "displayName": self.users[b.owner].name if b.owner in self.users else b.owner,
        }]
        return {"acl": acl, "cors": [], "versioning": b.versioning}

    def usage_entry(self, b: Bucket) -> dict:
        self.refresh_usage(b)
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        return {
            "bucketName": b.name, "bucketOwner": b.owner,
            "time": hour.isoformat().replace("+00:00", "Z"),
            "bytesUploaded": b.size_bytes, "bytesDownloaded": 0,
            "metadataBytesDownloaded": 0, "successfulOperations": b.num_objects,
            "failedOperations": 0,
        }

    # -- config / HA --
    @locked
    def set_cert(self, pem: str | None):
        if not pem or not pem.strip():
            raise ApiError(400, "The mock cannot generate a self-signed certificate; send one")
        self.cert_pem = pem

    @locked
    def haproxy(self, server_id: int, action: str):
        if action not in ("start", "stop", "restart"):
            raise ApiError(400, f"Unknown HAProxy action {action!r}")
        members = [m for g in self.ha_groups for m in g["servers"] if m["server"] == server_id]
        if not members:
            raise ApiError(404, f"Server {server_id} is in no HA group")
        for m in members:
            m["haproxyRunning"] = action != "stop"


def default_ha_group() -> dict:
    def member(sid, host):
        return {
            "server": sid, "serverHostname": host, "nic": 1, "runsService": True,
            "serviceRunning": True, "haproxyRunning": True, "keepafloatdRunning": True,
        }

    return {
        "id": 1, "description": "rgw-ha", "virtualIp": "10.0.0.10", "vips": ["10.0.0.10"],
        "service": "rgw", "backend": "keepafloatd", "haproxy": True, "haproxyPort": 443,
        "proxyMode": "http", "servers": [member(1, "ceph-gw1"), member(2, "ceph-gw2")],
        "failback": False, "failoverTime": 5, "failbackTime": 5,
        "evictNfsClientsOnFailback": False,
    }


# --- request models ------------------------------------------------------------


class Model(BaseModel):
    model_config = {"extra": "ignore"}


class NewUserBody(Model):
    uid: str
    name: str
    email: str | None = None
    tenant: str | None = None
    defaultPlacement: str | None = None
    defaultStorageClass: str | None = None


class UserUpdateBody(Model):
    name: str | None = None
    email: str | None = None
    suspended: bool | None = None
    defaultPlacement: str | None = None
    defaultStorageClass: str | None = None


class KeyBody(Model):
    user: str
    access_key: str
    secret_key: str


class UpdateKeysBody(Model):
    keys: list[KeyBody]


class QuotaBody(Model):
    enabled: bool
    maxSize: int | None = None
    maxObjects: int | None = None
    max_size_kb: int | None = None
    max_objects: int | None = None


class AclEntry(Model):
    grantee: Literal["ALL_USERS", "AUTHENTICATED_USERS", "SINGLE_USER", "LOG_DELIVERY"]
    permission: Literal["Read", "Write", "ReadAcp", "WriteAcp", "FullControl"]
    userId: str | None = None
    displayName: str | None = None


class BucketCreateBody(Model):
    owner: str
    acl: list[AclEntry] | None = None
    versioning: Literal["OFF", "ENABLED", "SUSPENDED", "UNKNOWN"] | None = None
    objectLock: bool | None = None
    placement: str | None = None
    expectedNumberOfObjects: int | None = None


class BucketUpdateBody(Model):
    owner: str | None = None
    acl: list[AclEntry] | None = None
    versioning: Literal["OFF", "ENABLED", "SUSPENDED", "UNKNOWN"] | None = None


class BucketDeleteBody(Model):
    name: str
    deleteData: bool | None = None
    thisWillDeleteData: str | None = None


class ApiTokenBody(Model):
    name: str = Field(min_length=1, max_length=64)
    roles: list[Literal["admin", "viewer", "securefolder"]]
    expiry: str | None = None


class ApiTokenUpdateBody(Model):
    roles: list[Literal["admin", "viewer", "securefolder"]] | None = None


class CertBody(Model):
    certificate: str | None = None
    doRollingRestart: bool = False


def dump(model: BaseModel) -> dict:
    return model.model_dump(exclude_unset=True)


# --- routes --------------------------------------------------------------------


def st(request: Request) -> CroitState:
    return request.app.state.croit


def bearer_of(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def authed(request: Request) -> Principal:
    return st(request).principal(bearer_of(request))[0]


def viewer(request: Request) -> Principal:
    p = authed(request)
    if not {"viewer", "admin"} & set(p.roles):
        raise ApiError(403, "The viewer role is required")
    return p


def admin(request: Request) -> Principal:
    p = authed(request)
    if "admin" not in p.roles:
        raise ApiError(403, "The admin role is required")
    return p


def no_pagination(request: Request):
    if any(k.split("[")[0].split(".")[0] == "pagination" for k in request.query_params):
        raise ApiError(501, "The mock croit API does not implement pagination")


Admin = Annotated[Principal, Depends(admin)]
Viewer = Annotated[Principal, Depends(viewer)]
NoPagination = Annotated[None, Depends(no_pagination)]
NO_CONTENT = {"status_code": 204, "response_class": Response}
router = APIRouter()


# auth
@router.post("/auth/login")
async def auth_login(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if form.get("grant_type") != "client_credentials":
        raise ApiError(400, 'grant_type must be "client_credentials"')
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        raise ApiError(401, "HTTP Basic credentials are required")
    try:
        username, _, password = base64.b64decode(value).decode().partition(":")
    except ValueError:
        raise ApiError(401, "Malformed Basic credentials") from None
    return st(request).login(username, password)


@router.post("/auth/login-form")
async def auth_login_form(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if form.get("grant_type") != "password":
        raise ApiError(400, 'grant_type must be "password"')
    if "username" not in form or "password" not in form:
        raise ApiError(400, "username and password are required")
    return st(request).login(form["username"], form["password"])


@router.get("/auth/token-info")
def token_info(request: Request):
    state = st(request)
    token = bearer_of(request)
    p, _ = state.principal(token)
    return {"username": p.username, "roles": p.roles, "expiry": state.refresh_session(token)}


@router.get("/api-tokens")
def api_tokens_list(request: Request, _a: Admin, _p: NoPagination):
    return st(request).list_api_tokens()


@router.post("/api-tokens")
def api_tokens_create(body: ApiTokenBody, request: Request, _a: Admin):
    return st(request).add_api_token(body.name, body.roles, body.expiry)


@router.patch("/api-tokens/{apiTokenId}", **NO_CONTENT)
def api_tokens_update(apiTokenId: int, body: ApiTokenUpdateBody, request: Request,
                      _a: Admin):
    st(request).update_api_token(apiTokenId, body.roles)


@router.delete("/api-tokens/{apiTokenId}", **NO_CONTENT)
def api_tokens_delete(apiTokenId: int, request: Request, _a: Admin):
    st(request).delete_api_token(apiTokenId)


# tasks
@router.get("/tasks/{taskId}")
def tasks_get(taskId: int, request: Request, _v: Viewer):
    return st(request).get_task(taskId)


# S3 users
@router.get("/s3/user-names")
def s3_user_names(request: Request, _v: Viewer):
    return list(st(request).users)


@router.get("/s3/users")
def s3_users_list(request: Request, _v: Viewer, _p: NoPagination,
                  uid: Annotated[list[str] | None, Query()] = None):
    state = st(request)
    users = [state.get_user(u) for u in uid] if uid else list(state.users.values())
    return [state.user_view(u) for u in users]


@router.post("/s3/users", **NO_CONTENT)
def s3_users_create(body: NewUserBody, request: Request, _a: Admin):
    st(request).create_user(
        body.uid, body.name, body.email, body.tenant,
        defaultPlacement=body.defaultPlacement, defaultStorageClass=body.defaultStorageClass)


@router.patch("/s3/users/{uid}", **NO_CONTENT)
def s3_users_update(uid: str, body: UserUpdateBody, request: Request, _a: Admin):
    st(request).update_user(uid, dump(body))


@router.delete("/s3/users/{uid}", **NO_CONTENT)
def s3_users_delete(uid: str, request: Request, _a: Admin):
    st(request).delete_user(uid)


@router.get("/s3/users/{uid}/buckets")
def s3_user_buckets(uid: str, request: Request, _v: Viewer):
    state = st(request)
    state.get_user(uid)
    return [state.bucket_view(b) for b in state.buckets.values() if b.owner == uid]


@router.put("/s3/users/{uid}/quota", **NO_CONTENT)
def s3_user_quota(uid: str, body: QuotaBody, request: Request, _a: Admin):
    state = st(request)
    state.set_quota(state.get_user(uid).user_quota, dump(body))


@router.put("/s3/users/{uid}/bucket-quota", **NO_CONTENT)
def s3_user_bucket_quota(uid: str, body: QuotaBody, request: Request, _a: Admin):
    state = st(request)
    state.set_quota(state.get_user(uid).bucket_quota, dump(body))


# keys
@router.get("/s3/users/{uid}/keys")
def s3_keys_get(uid: str, request: Request, _v: Viewer):
    user = st(request).get_user(uid)
    return [{"user": uid, **k} for k in user.keys]


@router.get("/s3/users/{uid}/key")
def s3_key_get(uid: str, request: Request, _v: Viewer):
    user = st(request).get_user(uid)
    return {"user": uid, **user.keys[0]} if user.keys else None


@router.put("/s3/users/{uid}/keys")
def s3_keys_replace(uid: str, body: UpdateKeysBody, request: Request, p: Admin):
    state = st(request)
    state.replace_keys(uid, [k.model_dump() for k in body.keys])
    return state.new_task("Update S3 keys", uid, p.username)


@router.put("/s3/users/{uid}/keys/{accessKey}", **NO_CONTENT)
def s3_key_put(uid: str, accessKey: str, secretKey: str, request: Request, _a: Admin):
    st(request).put_key(uid, accessKey, secretKey)


@router.delete("/s3/users/{uid}/keys/{accessKey}", **NO_CONTENT)
def s3_key_delete(uid: str, accessKey: str, request: Request, _a: Admin):
    st(request).remove_key(uid, accessKey)


@router.get("/s3/users/{uid}/cfg-file/download")
def s3_cfg_file(uid: str, request: Request, _v: Viewer):
    return st(request).cfg_file(uid)


# buckets
@router.get("/s3/buckets")
def s3_buckets_list(request: Request, _v: Viewer, _p: NoPagination):
    state = st(request)
    return [state.bucket_view(b) for b in state.buckets.values()]


@router.get("/s3/buckets/{bucketName}")
def s3_bucket_get(bucketName: str, request: Request, _v: Viewer):
    state = st(request)
    return state.bucket_details(state.get_bucket(bucketName))


@router.put("/s3/buckets/{bucketName}")
def s3_bucket_create(bucketName: str, body: BucketCreateBody, request: Request, p: Admin):
    state = st(request)
    acl = [a.model_dump(exclude_none=True) for a in body.acl] if body.acl is not None else None
    bucket = state.create_bucket(bucketName, body.owner, acl, body.versioning, body.objectLock,
                                 body.placement)
    return state.new_task("Create S3 bucket", bucket.name, p.username)


@router.patch("/s3/buckets/{bucketName}", **NO_CONTENT)
def s3_bucket_update(bucketName: str, body: BucketUpdateBody, request: Request,
                     _a: Admin):
    data = dump(body)
    if data.get("acl"):
        data["acl"] = [a.model_dump(exclude_none=True) for a in body.acl]
    st(request).update_bucket(bucketName, data)


@router.delete("/s3/buckets/{bucketName}")
def s3_bucket_delete(bucketName: str, body: BucketDeleteBody, request: Request, p: Admin,
                     ignoreWarning: str | None = None):
    st(request).delete_bucket(bucketName, dump(body), ignoreWarning)
    return st(request).new_task("Delete S3 bucket", bucketName, p.username)


@router.get("/s3/buckets/{bucketName}/policy")
def s3_policy_get(bucketName: str, request: Request, _v: Viewer):
    return {"policyText": st(request).get_bucket(bucketName).policy}


@router.post("/s3/buckets/{bucketName}/policy", **NO_CONTENT)
async def s3_policy_set(bucketName: str, request: Request, _a: Admin):
    state = st(request)
    bucket = state.get_bucket(bucketName)
    try:
        doc = json.loads(await request.body())
    except ValueError:
        raise ApiError(400, "The policy is not valid JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("Statement"), list):
        raise ApiError(400, "A policy needs a Statement list")
    with state.lock:
        bucket.policy = json.dumps(doc)


@router.delete("/s3/buckets/{bucketName}/policy", **NO_CONTENT)
def s3_policy_delete(bucketName: str, request: Request, _a: Admin):
    st(request).get_bucket(bucketName).policy = None


@router.put("/s3/buckets/{bucketName}/quota", **NO_CONTENT)
def s3_bucket_quota(bucketName: str, body: QuotaBody, request: Request, _a: Admin):
    state = st(request)
    state.set_quota(state.get_bucket(bucketName).quota, dump(body))


# usage
@router.get("/s3/usage/by-bucket/{bucketName}")
def s3_usage_bucket(bucketName: str, hours: int, request: Request, _v: Viewer):
    state = st(request)
    b = state.get_bucket(bucketName)
    return {"buckets": {b.owner: [state.usage_entry(b)]}}


@router.get("/s3/usage/by-user/{uid}")
def s3_usage_user(uid: str, hours: int, request: Request, _v: Viewer):
    state = st(request)
    state.get_user(uid)
    return {"buckets": {b.name: [state.usage_entry(b)]
                        for b in state.buckets.values() if b.owner == uid}}


# config and HA groups (tools/croit-cert)
@router.get("/config/rgw-ssl-cert")
def cert_get(request: Request, _a: Admin):
    return st(request).cert_pem


@router.put("/config/rgw-ssl-cert")
def cert_put(body: CertBody, request: Request, p: Admin):
    state = st(request)
    state.set_cert(body.certificate)
    return state.new_task("Set RGW SSL certificate", None, p.username)


@router.get("/servers/ha-groups")
def ha_groups(request: Request, _v: Viewer, _p: NoPagination):
    return copy.deepcopy(st(request).ha_groups)


@router.post("/servers/ha-groups/servers/{serverId}/haproxy/{action}", **NO_CONTENT)
def haproxy_control(serverId: int, action: str, request: Request, _a: Admin):
    st(request).haproxy(serverId, action)


# --- app factory ---------------------------------------------------------------


def create_app(
    seed: bool = True,
    task_seconds: float = 0.5,
    prefix: str = "",
    hooks: MockHooks | None = None,
    **state_kwargs,
) -> FastAPI:
    app = FastAPI(title="croit mock API", docs_url=None, redoc_url=None, openapi_url=None)
    state = CroitState(task_seconds=task_seconds, hooks=hooks, **state_kwargs)
    if seed:
        state.seed()
    app.state.croit = state
    app.include_router(router, prefix=prefix)

    @app.exception_handler(ApiError)
    async def _api_error(_req, exc: ApiError):
        return JSONResponse(error_body(exc.status, exc.message, exc.details, exc.extra), exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req, exc: StarletteHTTPException):
        return JSONResponse(error_body(exc.status_code, str(exc.detail)), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_req, exc: RequestValidationError):
        details = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return JSONResponse(error_body(400, "Invalid request", details), 400)

    return app


def main():
    import uvicorn

    host = os.environ.get("CROIT_MOCK_HOST", "127.0.0.1")
    port = int(os.environ.get("CROIT_MOCK_PORT", "18080"))
    prefix = os.environ.get("CROIT_MOCK_PREFIX", "/api")
    seconds = float(os.environ.get("CROIT_MOCK_TASK_SECONDS", "0.5"))
    bridge = None
    kwargs = {}
    if os.environ.get("CROIT_MOCK_S3", "").lower() == "moto":
        try:
            from mock.s3_bridge import S3Bridge, endpoint_url, start_moto
        except ImportError:  # run as a script: mock/ itself is on sys.path
            from s3_bridge import S3Bridge, endpoint_url, start_moto
        server = start_moto(host, int(os.environ.get("CROIT_MOCK_S3_PORT", "18081")))
        bridge = S3Bridge(endpoint_url(server))
        kwargs = {"hooks": bridge.make_hooks(MockHooks), "rgw_endpoint": bridge.host_port}
    app = create_app(seed=True, task_seconds=seconds, prefix=prefix, **kwargs)
    if bridge:
        bridge.seed_objects(app.state.croit)
        print(f"moto S3 endpoint: S3_ENDPOINT_URL={bridge.endpoint}", flush=True)
    print(f"croit mock API: CROIT_URL=http://{host}:{port}{prefix}", flush=True)
    print(f"  admin API token:  {ADMIN_TOKEN}")
    print(f"  viewer API token: {VIEWER_TOKEN}", flush=True)
    print("  croit logins: admin/admin (admin), viewer/viewer (viewer)")
    print("  S3 users: alice, bob, acme$carol; buckets: " + ", ".join(SEED_BUCKETS), flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
