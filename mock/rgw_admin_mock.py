#!/usr/bin/env python3
"""In-memory mock of the Ceph RGW Admin Ops REST API (Reef), for development and tests.

Dev/test tooling only: it imports nothing from `bucketeer/` (and nothing from
`croit_mock` at runtime, so it works both as `mock.rgw_admin_mock` and next to
`mock/croit_mock.py` run as a script), and the app never imports it. The coming
`RgwAdminClient` (TODO.md Phase 5, A2) is built and tested against it.

Structure. A separate FastAPI app built over the mock croit's state object:
`create_app(croit_state, admins=None)`. Users, keys, quotas, suspension and buckets live
in that one `CroitState`, so whatever is created through croit or through Admin Ops is
visible through the other, and the `MockHooks` (mock/s3_bridge.py) mirror key changes and
bucket deletes into moto either way. It is its own app on its own port (not extra routes in
the croit app) because in production the Admin Ops endpoint is an RGW address, not croit:
the client signs the request for that host and path (`/admin/...`, no `/api` prefix), and a
shared app would hide a client that mixes the two up.

Run it next to the croit mock: `CROIT_MOCK_ADMIN_OPS=1 .venv/bin/python3 mock/croit_mock.py`
(port `CROIT_MOCK_ADMIN_OPS_PORT`, default 18082; it prints the admin keys), or with
`mock/demo.py --admin-ops`. Key/caps variables (random per process unless set):
`CROIT_MOCK_ADMIN_OPS_ACCESS_KEY|SECRET_KEY|CAPS` (the full-cap admin) and
`CROIT_MOCK_ADMIN_OPS_RO_ACCESS_KEY|RO_SECRET_KEY` (a read-only one). In tests:
`create_app(state)`; the principals are in `app.state.admin_ops.admins`.

Authentication. Every request must carry an AWS Signature V4 header (`Authorization:
AWS4-HMAC-SHA256 ...`, service `s3`, any region) checked here with a plain SigV4
implementation (canonical request, string to sign, HMAC chain; tests sign with botocore's
`S3SigV4Auth`, an independent implementation). **Clients must send `x-amz-content-sha256`**
(botocore's `S3SigV4Auth` does, plain `SigV4Auth` does not): without the header the payload
hash is `UNSIGNED-PAYLOAD`, as in RGW, so a signature computed over the real body hash is
refused with 403 SignatureDoesNotMatch. Missing/garbled -> 403 AccessDenied; unknown access
key -> 403 InvalidAccessKeyId; wrong signature -> 403 SignatureDoesNotMatch; clock off by more
than 15 minutes -> 403 RequestTimeTooSkewed; a payload hash that does not match the body ->
400 XAmzContentSHA256Mismatch.

The admin principals are real RGW users in the shared `CroitState` (`create_app` creates
`bucketeer-admin` and `bucketeer-readonly`, not the croit seed), authenticated through the
state's access keys, so they show up in `/admin/metadata/user`, `/admin/user?list` and
`GET /admin/user?access-key=<their key>`, user info lists their `caps`, a suspended admin gets
403 UserSuspended, and giving an admin's access key to another user is 409 KeyExists. Caps
are `type=perm[,perm]; ...` with `read`, `write` or `*`; a regular user's key signs fine but
has no caps (403 AccessDenied). GET needs `read`, PUT/POST/DELETE need `write`:
`/admin/user*` -> `users`, `/admin/bucket*` -> `buckets`, `/admin/usage` -> `usage`,
`/admin/metadata/*` -> `metadata`. The default admin caps are `users=read,write;
buckets=read,write; usage=read; metadata=read` (the planned production caps plus
`metadata=read`, which real RGW demands for `GET /admin/metadata/user`; the client may prefer
`GET /admin/user?list`, which needs only `users`).

Errors are RGW's JSON shape `{"Code", "Message", "RequestId", "HostId"}` with RGW's codes
(`NoSuchUser` 404, `UserAlreadyExists` 409, `EmailExists` 409, `KeyExists` 409,
`InvalidAccessKeyId` 403, `NoSuchBucket` 404, `BucketNotEmpty` 409,
`BucketAlreadyExists` 409, `InvalidArgument` 400, `AccessDenied` 403, `MethodNotAllowed` 405,
`NotImplemented` 501). `Message` is empty like RGW's mostly is; the human text is in the
`x-mock-detail` response header. Successful writes with nothing to return answer 200 with an
empty body. Query parameter names match exactly (`display-name`, never `display_name`);
booleans are lenient like RGW's: missing = the default, empty / `true` (any case) / `1` =
true, `false` / `0` = false, anything else = the default (never an error), so
`?purge-objects` purges and `purge-objects=yes` does not.

!! A2 MUST HANDLE: `PUT /admin/user` of an EXISTING uid with the same `display-name` and no
`exclusive=true` SUCCEEDS (200) as a modify, as RGW does (it adds no key unless asked; a
different display name, or `exclusive=true`, gives 409 `UserAlreadyExists`). So the resolver
and the bucket-key saga must never read "create succeeded" as "the user is new": send
`exclusive=true` and treat 409 as "exists". Likewise a key delete answers 403
`InvalidAccessKeyId` both for a key that is already gone and for another user's key (the same
code as a bad admin signature): decide whether a key is gone from the user's key list, never
from the delete's error. (Verify this group on the cluster.)

Implemented: user info (by `uid` or `access-key`), create (`generate-key=false` allowed,
email optional, `tenant=` only here, where it overrides), modify (`suspended`,
`display-name`, `email`, key add), remove (`purge-data`), key put/delete (`key-type=s3`
only; keys are returned sorted by access key), user/bucket quota get/set, user list
(`/admin/metadata/user` and `/admin/user?list`), bucket list/stats/info, bucket quota set,
bucket delete (`purge-objects`), minimal usage, subusers (`PUT|POST|DELETE
/admin/user?uid=&subuser=name`, `access=read|write|readwrite|full`, delete purges the
subuser's keys by default; their keys via `?key&uid=&subuser=name&access-key=&secret-key=` and
`DELETE ?key&subuser=name&access-key=`, listed in `keys[]` with `user` = `uid:name`; user info
shows `subusers: [{id, permissions}]`). Cluster check: how real RGW routes `?key&subuser=`
(its handler may pick the subuser op first), the 409 code for an existing subuser, and
whether S3 requests honour a subuser's permission. Not implemented (501/405): caps
changes, swift keys, user stats, bucket link/unlink/index check, usage trim, `format=xml`.
Uids are `tenant$uid` in the `uid` parameter; user info reports `"tenant": "acme",
"user_id": "carol"` and only `keys[].user` stays `acme$carol`. `+` is fine in a uid but, like
any query value, must be percent-encoded (`%2B`) because a literal `+` is a space.
Buckets: `GET /admin/bucket?uid=` lists that user's buckets with BARE names; with `bucket=`
as well it still answers an array (that bucket only, empty if not the user's), holding stats
objects only with `stats=true`. A bare `bucket` is looked up in the tenant taken from `uid`
(`uid=acme$carol&bucket=reports`); without `uid` use `tenant/bucket` (`tenant:bucket` is
accepted too); `bucket=` alone answers one stats object. The global name list is
`tenant/bucket`. Bucket quota PUT does no ownership check; `uid` only supplies the tenant.
Quota PUT with a JSON body decodes it into a FRESH quota (omitted fields reset to disabled /
unlimited, `max_size` wins over `max_size_kb`, so `{"max_size": 0, "max_objects": 0}` turns it
off); only the query-parameter form (`enabled`, `max-size`, `max-size-kb`, `max-objects`)
keeps old values. Without `quota-type` the body is `{"user_quota": {...}, "bucket_quota":
{...}}` and sets both.

CLUSTER-CHECK ITEMS (shapes the author was not sure of; verify on the real Reef cluster):
 1. Error body format when no `format=` is sent (the mock always answers JSON) and whether
    `Message` is empty.
 2. The create/exists/error-code group: repeat create as modify (above), 409
    `UserAlreadyExists`, `BucketAlreadyExists` for a user with buckets and no `purge-data`,
    `KeyExists` for a key of another user (an own existing key just gets its secret
    replaced), 403 `InvalidAccessKeyId` on deleting a key the user doesn't have (missing or another
    user's), 200 empty
    (not 204) on successful deletes, and whether a repeat create also generates a key.
 3. `GET /admin/user?list` existence in Reef and its output (mock: `{"keys": [...],
    "truncated": false, "count": n}`); `GET /admin/metadata/user` is a plain array of uids,
    an object with `keys`/`truncated`/`count`/`marker` once `max-entries` or `marker` is sent.
 4. `GET /admin/user?quota&uid=` without `quota-type` (mock: both quotas in one object) and
    the body format of a PUT without it.
 5. Quota JSON keys `enabled`, `max_size`, `max_size_kb`, `max_objects`, and the fresh-quota
    semantics above.
 6. `GET /admin/bucket?bucket=` without `stats` returns the full stats object (mock), the
    exact `usage` content (mock: `{}` for an empty bucket, `rgw.main` with `size`,
    `size_actual`, `size_utilized`, `size_kb*`, `num_objects` otherwise), and the field names
    `versioned`, `versioning_enabled`, `object_lock_enabled`, `mfa_enabled`, `creation_time`.
 7. Tenant bucket handling: `tenant/bucket` in the `bucket` parameter and in the global
    listing; bare names with `uid`. In Reef, `GET /admin/bucket?stats=true` WITHOUT `uid` may
    drop tenant buckets (the mock lists them): query per user.
 8. User info fields (`suspended` as 0/1, `max_buckets`, `caps` as `{type, perm}`, `op_mask`)
    and the key object (mock: only `user`, `access_key`, `secret_key`; newer releases add
    `active`, `create_date`). `max-buckets` is accepted and ignored.
 9. Usage output (mock: empty `entries`, a zero `summary` row per user).
10. Missing `display-name` on create (mock: 400 `InvalidArgument`); a missing `uid` (400);
    an unknown `access-key` lookup (mock: 404 `NoSuchUser`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

DEFAULT_CAPS = "users=read,write; buckets=read,write; usage=read; metadata=read"
READONLY_CAPS = "users=read; buckets=read; usage=read; metadata=read"
SKEW_SECONDS = 900
ALGORITHM = "AWS4-HMAC-SHA256"
MAX_BODY = 1 << 20


def _rand_access() -> str:
    return "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(20))


def _rand_secret() -> str:
    return secrets.token_urlsafe(30)[:40]


ADMIN_ACCESS_KEY = os.environ.get("CROIT_MOCK_ADMIN_OPS_ACCESS_KEY") or _rand_access()
ADMIN_SECRET_KEY = os.environ.get("CROIT_MOCK_ADMIN_OPS_SECRET_KEY") or _rand_secret()
ADMIN_CAPS = os.environ.get("CROIT_MOCK_ADMIN_OPS_CAPS") or DEFAULT_CAPS
RO_ACCESS_KEY = os.environ.get("CROIT_MOCK_ADMIN_OPS_RO_ACCESS_KEY") or _rand_access()
RO_SECRET_KEY = os.environ.get("CROIT_MOCK_ADMIN_OPS_RO_SECRET_KEY") or _rand_secret()


class AdminError(Exception):
    """Rendered as RGW's JSON error: {Code, Message, RequestId, HostId}."""

    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{status} {code} {detail}")
        self.status, self.code, self.detail = status, code, detail


def parse_caps(text: str) -> dict[str, frozenset[str]]:
    """`users=read,write; buckets=*` -> {"users": {"read","write"}, "buckets": {...}}."""
    caps: dict[str, frozenset[str]] = {}
    for part in filter(None, (p.strip() for p in text.split(";"))):
        kind, _, perms = part.partition("=")
        wanted = {p.strip() for p in perms.split(",") if p.strip()}
        if "*" in wanted:
            wanted |= {"read", "write"}
        caps[kind.strip()] = frozenset(wanted - {"*"})
    return caps


@dataclass
class AdminPrincipal:
    """An RGW user that may call Admin Ops (`radosgw-admin caps add`). `create_app` makes
    it a real user in the shared `CroitState` (uid, display name, this key); authentication
    then goes through the state's access keys and the caps are looked up by uid."""

    access_key: str
    secret_key: str
    caps: dict[str, frozenset[str]] = field(default_factory=dict)
    name: str = "admin"
    uid: str = ""  # the RGW user in CroitState (default `bucketeer-<name>`)

    def __post_init__(self):
        self.uid = self.uid or f"bucketeer-{self.name}"

    @classmethod
    def make(cls, access_key: str, secret_key: str, caps: str | dict, name: str = "admin",
             uid: str = ""):
        return cls(access_key, secret_key, parse_caps(caps) if isinstance(caps, str) else caps,
                   name, uid)


def default_admins() -> list[AdminPrincipal]:
    return [
        AdminPrincipal.make(ADMIN_ACCESS_KEY, ADMIN_SECRET_KEY, ADMIN_CAPS, "admin"),
        AdminPrincipal.make(RO_ACCESS_KEY, RO_SECRET_KEY, READONLY_CAPS, "readonly"),
    ]


def describe_admins(app: FastAPI) -> str:
    lines = []
    for p in app.state.admin_ops.admins:
        caps = "; ".join(f"{k}={','.join(sorted(v))}" for k, v in p.caps.items())
        lines.append(f"  Admin Ops {p.name}: access key {p.access_key}  secret key "
                     f"{p.secret_key}  caps: {caps}")
    return "\n".join(lines)


# --- SigV4 -----------------------------------------------------------------------


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode(), date)
    return _hmac(_hmac(_hmac(k, region), service), "aws4_request")


def canonical_query(raw_query: str) -> str:
    pairs = parse_qsl(raw_query, keep_blank_values=True)
    return "&".join(f"{k}={v}" for k, v in sorted(
        (quote(k, safe="-_.~"), quote(v, safe="-_.~")) for k, v in pairs))


# --- request helpers -------------------------------------------------------------


class Params:
    """Query parameters, names matched exactly (`display-name`, not `display_name`).
    Valueless ones (`?quota`, `?key`, `?purge-objects`) are flags with an empty value."""

    def __init__(self, raw_query: str):
        self.pairs = parse_qsl(raw_query, keep_blank_values=True)
        self.values = dict(self.pairs)

    def has(self, name: str) -> bool:
        return name in self.values

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.values.get(name, default)

    def bool(self, name: str, default: bool) -> bool:
        """RGW's lenient flag parsing: missing -> default; empty, `true` (any case) or `1`
        -> true; `false` or `0` -> false; anything else -> the default (never an error)."""
        raw = self.values.get(name)
        if raw is None:
            return default
        low = raw.lower()
        if low in ("", "true", "1"):
            return True
        if low in ("false", "0"):
            return False
        return default


def as_int(value, name: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        raise AdminError(400, "InvalidArgument", f"{name} must be an integer") from None


def now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def kb(n: int) -> int:
    return 0 if n < 0 else math.ceil(n / 1024)


def quota_view(q: dict) -> dict:
    size = -1 if q["maxSize"] < 0 else q["maxSize"]
    objects = -1 if q["maxObjects"] < 0 else q["maxObjects"]
    return {"enabled": bool(q["enabled"]), "check_on_raw": False, "max_size": size,
            "max_size_kb": kb(size), "max_objects": objects}


# --- the Admin Ops state around a CroitState ---------------------------------------


class AdminOps:
    def __init__(self, croit, admins: list[AdminPrincipal], clock: Callable[[], float]):
        self.croit = croit  # the shared CroitState
        self.admins = admins
        self.clock = clock
        self.created = now_iso()
        self.caps: dict[str, dict[str, frozenset[str]]] = {}  # uid -> caps
        for p in admins:
            self._register(p)

    def _register(self, p: AdminPrincipal):
        with self.croit.lock:
            if p.uid in self.croit.users:
                self.croit.put_key(p.uid, p.access_key, p.secret_key)
            else:
                self.croit.create_user(p.uid, f"Admin Ops {p.name}", None,
                                       keys=[(p.access_key, p.secret_key)])
            self.caps[p.uid] = p.caps

    # -- authentication ---------------------------------------------------------
    def authenticate(self, request: Request, body: bytes) -> AdminPrincipal:
        auth = request.headers.get("authorization")
        if not auth or not auth.startswith(ALGORITHM + " "):
            raise AdminError(403, "AccessDenied", "a SigV4 Authorization header is required")
        fields = {}
        for part in auth[len(ALGORITHM):].split(","):
            k, _, v = part.strip().partition("=")
            fields[k] = v
        try:
            access, date, region, service, term = fields["Credential"].split("/")
            signed = fields["SignedHeaders"].split(";")
            given = fields["Signature"]
        except (KeyError, ValueError):
            raise AdminError(403, "AccessDenied", "malformed Authorization header") from None
        if term != "aws4_request" or "host" not in signed:
            raise AdminError(403, "AccessDenied", "bad credential scope or signed headers")
        principal, secret = self._secret_for(access)  # unknown key -> 403 InvalidAccessKeyId
        stamp = request.headers.get("x-amz-date")
        try:
            when = datetime.strptime(stamp or "", "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            raise AdminError(403, "AccessDenied", "x-amz-date is required") from None
        if date != stamp[:8]:
            raise AdminError(403, "AccessDenied", "credential date differs from x-amz-date")
        if abs(self.clock() - when.timestamp()) > SKEW_SECONDS:
            raise AdminError(403, "RequestTimeTooSkewed", "")
        actual_hash = hashlib.sha256(body).hexdigest()
        claimed = request.headers.get("x-amz-content-sha256")
        if claimed is None:
            claimed = "UNSIGNED-PAYLOAD"  # what RGW signs when the header is absent
        elif claimed != "UNSIGNED-PAYLOAD" and claimed != actual_hash:
            raise AdminError(400, "XAmzContentSHA256Mismatch", "")
        headers = ""
        for name in signed:
            values = request.headers.getlist(name)
            if not values:
                raise AdminError(403, "SignatureDoesNotMatch", f"signed header {name} missing")
            headers += f"{name}:{','.join(' '.join(v.split()) for v in values)}\n"
        raw_path = (request.scope.get("raw_path") or request.url.path.encode()).split(b"?")[0]
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        canonical = "\n".join([
            request.method, raw_path.decode("latin-1"), canonical_query(raw_query), headers,
            ";".join(signed), claimed,
        ])
        to_sign = "\n".join([ALGORITHM, stamp, f"{date}/{region}/{service}/aws4_request",
                             hashlib.sha256(canonical.encode()).hexdigest()])
        expect = hmac.new(signing_key(secret, date, region, service), to_sign.encode(),
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, given):
            raise AdminError(403, "SignatureDoesNotMatch", "")
        return principal

    def _secret_for(self, access: str) -> tuple[AdminPrincipal, str]:
        with self.croit.lock:
            uid = self.croit.access_keys.get(access)
            user = self.croit.users.get(uid) if uid else None
            key = next((k for k in user.keys if k["access_key"] == access), None) if user \
                else None
            if not (user and key):
                raise AdminError(403, "InvalidAccessKeyId", "")
            if user.suspended:
                raise AdminError(403, "UserSuspended", "")
            return (AdminPrincipal(access, key["secret_key"], self.caps.get(uid, {}), uid, uid),
                    key["secret_key"])


def check_cap(principal: AdminPrincipal, kind: str, perm: str):
    if perm not in principal.caps.get(kind, frozenset()):
        raise AdminError(403, "AccessDenied", f"needs {kind}={perm}")


# --- operations (run under the croit state lock, in a worker thread) -----------------


def resolve_uid(params: Params, required: bool = True) -> str:
    """The `uid` parameter (`tenant$uid` form). `tenant=` is honoured only by user create."""
    uid = params.get("uid")
    if not uid and required:
        raise AdminError(400, "InvalidArgument", "uid is required")
    return uid or ""


def get_user(ops: AdminOps, uid: str):
    user = ops.croit.users.get(uid)
    if not user:
        raise AdminError(404, "NoSuchUser", uid)
    return user


def key_list(user) -> list[dict]:
    return [{"user": f"{user.uid}:{k['subuser']}" if k.get("subuser") else user.uid,
             "access_key": k["access_key"], "secret_key": k["secret_key"]}
            for k in sorted(user.keys, key=lambda k: k["access_key"])]


ACCESS_TO_PERMISSION = {"read": "read", "write": "write", "readwrite": "read-write",
                        "full": "full-control"}


def subuser_list(user) -> list[dict]:
    return [{"id": f"{user.uid}:{n}", "permissions": p or "<none>"}
            for n, p in sorted(user.subusers.items())]


def caps_view(ops: AdminOps, uid: str) -> list[dict]:
    out = []
    for kind, perms in sorted(ops.caps.get(uid, {}).items()):
        out.append({"type": kind, "perm": "*" if perms >= {"read", "write"} else
                    ", ".join(sorted(perms))})
    return out


def user_view(ops: AdminOps, user) -> dict:
    return {
        "tenant": user.tenant, "user_id": user.uid.partition("$")[2] or user.uid,
        "display_name": user.name,
        "email": user.email, "suspended": int(user.suspended), "max_buckets": 1000,
        "subusers": subuser_list(user), "keys": key_list(user), "swift_keys": [],
        "caps": caps_view(ops, user.uid),
        "op_mask": "read, write, delete", "system": False, "admin": False,
        "default_placement": user.default_placement or "",
        "default_storage_class": user.default_storage_class or "",
        "placement_tags": [], "bucket_quota": quota_view(user.bucket_quota),
        "user_quota": quota_view(user.user_quota), "temp_url_keys": [], "type": "rgw",
        "mfa_ids": [],
    }


def find_bucket(ops: AdminOps, raw: str | None, uid: str = ""):
    """`tenant/bucket` or `tenant:bucket`; a bare name is looked up in the tenant of `uid`
    (no tenant when `uid` is empty or has none)."""
    if not raw:
        raise AdminError(400, "InvalidArgument", "bucket is required")
    names = [raw, raw.replace("/", ":", 1)]
    if "/" not in raw and ":" not in raw and "$" in uid:
        names = [f"{uid.partition('$')[0]}:{raw}"]
    for name in names:
        bucket = ops.croit.buckets.get(name)
        if bucket:
            return bucket
    raise AdminError(404, "NoSuchBucket", raw)


def rgw_bucket_name(bucket) -> str:
    return bucket.name.replace(":", "/", 1)


def bare_bucket_name(bucket) -> str:
    return bucket.name.partition(":")[2] or bucket.name


def bucket_view(ops: AdminOps, b) -> dict:
    ops.croit.refresh_usage(b)
    usage = {}
    if b.num_objects or b.size_bytes:
        actual = math.ceil(b.size_bytes / 4096) * 4096
        usage = {"rgw.main": {
            "size": b.size_bytes, "size_actual": actual, "size_utilized": b.size_bytes,
            "size_kb": kb(b.size_bytes), "size_kb_actual": kb(actual),
            "size_kb_utilized": kb(b.size_bytes), "num_objects": b.num_objects}}
    short = b.name.partition(":")[2] or b.name
    return {
        "bucket": short, "num_shards": 11, "tenant": b.tenant, "zonegroup": "mock-zonegroup",
        "placement_rule": b.placement,
        "explicit_placement": {"data_pool": "", "data_extra_pool": "", "index_pool": ""},
        "id": b.id, "marker": b.id, "index_type": "Normal",
        "versioned": b.versioning != "OFF", "versioning_enabled": b.versioning == "ENABLED",
        "object_lock_enabled": b.object_lock, "mfa_enabled": False, "owner": b.owner,
        "ver": "0#1", "master_ver": "0#0", "mtime": b.mtime or ops.created,
        "creation_time": ops.created, "max_marker": "0#", "usage": usage,
        "bucket_quota": quota_view(b.quota),
    }


def check_key_type(params: Params):
    kt = params.get("key-type", "s3")
    if kt == "swift":
        raise AdminError(501, "NotImplemented", "swift keys")
    if kt != "s3":
        raise AdminError(400, "InvalidKeyType", kt)


def check_key_free(ops: AdminOps, access: str, uid: str):
    owner = ops.croit.access_keys.get(access)
    if owner and owner != uid:
        raise AdminError(409, "KeyExists", "access key belongs to another user")


def check_email(ops: AdminOps, email: str, owner: str | None = None):
    if email and any(u.email.lower() == email.lower() and u.uid != owner
                     for u in ops.croit.users.values()):
        raise AdminError(409, "EmailExists", "")


def key_material(params: Params, uid: str, ops: AdminOps, *, default_generate: bool = True):
    """(access, secret) to add from access-key / secret-key / generate-key, or None."""
    access, secret = params.get("access-key"), params.get("secret-key")
    generate = params.bool("generate-key", default_generate)
    if access:
        check_key_free(ops, access, uid)
        if not secret:
            if not generate:
                raise AdminError(400, "InvalidArgument", "secret-key is required")
            secret = _rand_secret()
        return access, secret
    if secret:
        raise AdminError(400, "InvalidArgument", "secret-key needs an access-key")
    if generate:
        return _rand_access(), _rand_secret()
    return None


def op_user_info(ops, params, body):
    uid = resolve_uid(params, required=False)
    if not uid:
        access = params.get("access-key")
        if not access:
            raise AdminError(400, "InvalidArgument", "uid is required")
        uid = ops.croit.access_keys.get(access, "")
        if not uid:
            raise AdminError(404, "NoSuchUser", "no user has that access key")
    return user_view(ops, get_user(ops, uid))


def op_user_create(ops, params, body):
    """RGW's create is also a modify: an existing uid with the SAME display name and no
    `exclusive=true` answers 200 with the (updated) user instead of 409."""
    uid = resolve_uid(params)
    tenant = params.get("tenant")
    if tenant:  # overrides any tenant in the uid
        uid = f"{tenant}${uid.partition('$')[2] or uid}"
    name = params.get("display-name")
    if not name:
        raise AdminError(400, "InvalidArgument", "display-name is required")
    existing = ops.croit.users.get(uid)
    if existing:
        if params.bool("exclusive", False) or existing.name != name:
            raise AdminError(409, "UserAlreadyExists", uid)
        return modify_user(ops, uid, params, default_generate=False)
    email = params.get("email", "") or ""
    check_email(ops, email)
    check_key_type(params)
    material = key_material(params, uid, ops)
    suspended = params.bool("suspended", False)
    ops.croit.create_user(uid, name, email or None, keys=[material] if material else [])
    if suspended:
        ops.croit.update_user(uid, {"suspended": True})
    return user_view(ops, ops.croit.users[uid])


def modify_user(ops, uid: str, params: Params, default_generate: bool = False):
    user = get_user(ops, uid)
    changes = {}
    if params.has("display-name"):
        changes["name"] = params.get("display-name")
    if params.has("email"):
        email = params.get("email") or ""
        check_email(ops, email, uid)
        changes["email"] = email
    if params.has("suspended"):
        changes["suspended"] = params.bool("suspended", False)
    if params.has("max-buckets"):
        as_int(params.get("max-buckets"), "max-buckets")
    check_key_type(params)
    material = None
    if params.has("access-key") or params.has("secret-key") or params.bool(
            "generate-key", default_generate):
        material = key_material(params, uid, ops, default_generate=False)
    ops.croit.update_user(uid, changes)
    if material:
        ops.croit.put_key(uid, *material)
    return user_view(ops, user)


def op_user_modify(ops, params, body):
    return modify_user(ops, resolve_uid(params), params)


def op_user_delete(ops, params, body):
    uid = resolve_uid(params)
    get_user(ops, uid)
    owned = [b.name for b in ops.croit.buckets.values() if b.owner == uid]
    if owned and not params.bool("purge-data", False):
        raise AdminError(409, "BucketAlreadyExists", "user owns buckets; use purge-data")
    for name in owned:
        ops.croit.force_delete_bucket(name)
    ops.croit.delete_user(uid)


def subuser_name(ops, params, uid: str) -> str:
    name = params.get("subuser") or ""
    if not name:
        raise AdminError(400, "InvalidArgument", "subuser is required")
    if name not in get_user(ops, uid).subusers:
        raise AdminError(404, "NoSuchSubUser", f"{uid}:{name}")
    return name


def access_permission(params):
    raw = params.get("access")
    if raw is None:
        return None
    if raw not in ACCESS_TO_PERMISSION:
        raise AdminError(400, "InvalidArgument", "access must be read, write, readwrite or full")
    return ACCESS_TO_PERMISSION[raw]


def api_error(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        if type(e).__name__ != "ApiError":  # croit_mock.ApiError, by duck type
            raise
        code = {404: "NoSuchSubUser", 409: "KeyExists"}.get(e.status, "InvalidArgument")
        raise AdminError(e.status, code, str(e)) from None


def op_subuser_create(ops, params, body):
    uid = resolve_uid(params)
    name = params.get("subuser") or ""
    check_key_type(params)
    permission = access_permission(params)
    material = key_material(params, uid, ops, default_generate=False) \
        if params.has("access-key") or params.has("secret-key") else None
    api_error(ops.croit.create_subuser, uid, name, permission)
    if material:
        api_error(ops.croit.put_subuser_key, uid, name, *material)
    return subuser_list(get_user(ops, uid))


def op_subuser_modify(ops, params, body):
    uid = resolve_uid(params)
    name = subuser_name(ops, params, uid)
    permission = access_permission(params)
    if permission is not None:
        api_error(ops.croit.set_subuser_permission, uid, name, permission)
    return subuser_list(get_user(ops, uid))


def op_subuser_remove(ops, params, body):
    uid = resolve_uid(params)
    name = subuser_name(ops, params, uid)
    api_error(ops.croit.delete_subuser, uid, name, params.bool("purge-keys", True))


def op_key_put(ops, params, body):
    uid = resolve_uid(params)
    user = get_user(ops, uid)
    check_key_type(params)
    sub = subuser_name(ops, params, uid) if params.has("subuser") else None
    material = key_material(params, uid, ops)
    if not material:
        raise AdminError(400, "InvalidArgument", "access-key is required without generate-key")
    if sub:
        api_error(ops.croit.put_subuser_key, uid, sub, *material)
    else:
        ops.croit.put_key(uid, *material)
    return key_list(user)


def op_key_delete(ops, params, body):
    uid = resolve_uid(params)
    user = get_user(ops, uid)
    check_key_type(params)
    access = params.get("access-key")
    if not access:
        raise AdminError(400, "InvalidArgument", "access-key is required")
    sub = subuser_name(ops, params, uid) if params.has("subuser") else None
    if not any(k["access_key"] == access and k.get("subuser") == sub for k in user.keys):
        # RGW answers the same for another user's key and a key that exists nowhere
        raise AdminError(403, "InvalidAccessKeyId", "")
    if sub:
        ops.croit.remove_subuser_key(uid, sub, access)
    else:
        ops.croit.remove_key(uid, access)


def _quota_by_type(ops, user, kind: str) -> dict:
    if kind == "user":
        return user.user_quota
    if kind == "bucket":
        return user.bucket_quota
    raise AdminError(400, "InvalidArgument", "quota-type must be user or bucket")


def op_quota_get(ops, params, body):
    user = get_user(ops, resolve_uid(params))
    kind = params.get("quota-type")
    if kind is None:
        return {"bucket_quota": quota_view(user.bucket_quota),
                "user_quota": quota_view(user.user_quota)}
    return quota_view(_quota_by_type(ops, user, kind))


def fresh_quota() -> dict:
    return {"enabled": False, "maxSize": -1, "maxObjects": -1}


def decode_quota(data: dict) -> dict:
    """A JSON quota decoded into a FRESH quota: omitted fields fall back to the defaults
    (disabled, unlimited); `max_size` wins over `max_size_kb`."""
    q = fresh_quota()
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise AdminError(400, "InvalidArgument", "enabled must be a boolean")
        q["enabled"] = data["enabled"]
    if "max_size_kb" in data:
        n = as_int(data["max_size_kb"], "max_size_kb")
        q["maxSize"] = -1 if n < 0 else n * 1024
    if "max_size" in data:
        n = as_int(data["max_size"], "max_size")
        q["maxSize"] = -1 if n < 0 else n
    if "max_objects" in data:
        n = as_int(data["max_objects"], "max_objects")
        q["maxObjects"] = -1 if n < 0 else n
    return q


def json_object(body: bytes) -> dict | None:
    if not body.strip():
        return None
    try:
        data = json.loads(body)
    except ValueError:
        raise AdminError(400, "InvalidArgument", "the quota body is not JSON") from None
    if not isinstance(data, dict):
        raise AdminError(400, "InvalidArgument", "the quota body must be an object")
    return data


def query_quota(target: dict, params: Params) -> dict:
    """The query-parameter form: only the given values change."""
    q = dict(target)
    if params.has("enabled"):
        q["enabled"] = params.bool("enabled", target["enabled"])
    if params.has("max-size"):
        n = as_int(params.get("max-size"), "max-size")
        q["maxSize"] = -1 if n < 0 else n
    if params.has("max-size-kb"):  # in the query form max-size-kb wins, as in RGW
        n = as_int(params.get("max-size-kb"), "max-size-kb")
        q["maxSize"] = -1 if n < 0 else n * 1024
    if params.has("max-objects"):
        n = as_int(params.get("max-objects"), "max-objects")
        q["maxObjects"] = -1 if n < 0 else n
    return q


def set_quota(target: dict, new: dict):
    target.update(enabled=new["enabled"], maxSize=new["maxSize"], maxObjects=new["maxObjects"])


def op_quota_put(ops, params, body):
    user = get_user(ops, resolve_uid(params))
    kind = params.get("quota-type")
    data = json_object(body)
    if kind is None:
        # no type: the body carries both, {"user_quota": {...}, "bucket_quota": {...}}
        if data is None or not ({"user_quota", "bucket_quota"} & set(data)):
            raise AdminError(400, "InvalidArgument", "quota-type is required")
        for key, target in (("user_quota", user.user_quota), ("bucket_quota", user.bucket_quota)):
            if key in data:
                if not isinstance(data[key], dict):
                    raise AdminError(400, "InvalidArgument", f"{key} must be an object")
                set_quota(target, decode_quota(data[key]))
        return
    target = _quota_by_type(ops, user, kind)
    set_quota(target, decode_quota(data) if data is not None else query_quota(target, params))


def op_bucket_quota_put(ops, params, body):
    """`uid` only supplies the tenant for a bare bucket name; there is no ownership check."""
    bucket = find_bucket(ops, params.get("bucket"), resolve_uid(params, required=False))
    data = json_object(body)
    set_quota(bucket.quota,
              decode_quota(data) if data is not None else query_quota(bucket.quota, params))


def op_user_list(ops, params, body, *, plain_default: bool = False):
    uids = sorted(ops.croit.users)
    paged = params.has("max-entries") or params.has("marker")
    marker = params.get("marker") or ""
    if marker:
        uids = [u for u in uids if u > marker]
    limit = as_int(params.get("max-entries"), "max-entries") if params.has("max-entries") \
        else len(uids)
    page = uids[:max(limit, 0)]
    if plain_default and not paged:
        return page
    out = {"keys": page, "truncated": len(uids) > len(page), "count": len(page)}
    if out["truncated"] or marker:
        out["marker"] = page[-1] if page else marker
    return out


def op_metadata_user(ops, params, body):
    if params.has("key"):
        raise AdminError(501, "NotImplemented", "metadata get")
    return op_user_list(ops, params, body, plain_default=True)


def op_bucket_info(ops, params, body):
    uid = resolve_uid(params, required=False)
    stats = params.bool("stats", False)
    if uid:
        get_user(ops, uid)
    name = params.get("bucket")
    if name and not uid:
        return bucket_view(ops, find_bucket(ops, name))  # one bucket: its stats object
    if name:
        wanted = find_bucket(ops, name, uid)
        buckets = [wanted] if wanted.owner == uid else []
    else:
        buckets = [b for b in ops.croit.buckets.values() if not uid or b.owner == uid]
    if stats:
        return [bucket_view(ops, b) for b in buckets]
    # names: bare for one user's buckets, `tenant/bucket` in the global listing
    return [bare_bucket_name(b) if uid else rgw_bucket_name(b) for b in buckets]


def op_bucket_delete(ops, params, body):
    bucket = find_bucket(ops, params.get("bucket"), resolve_uid(params, required=False))
    purge = params.bool("purge-objects", False)
    ops.croit.refresh_usage(bucket)
    if bucket.num_objects > 0 and not purge:
        raise AdminError(409, "BucketNotEmpty", bucket.name)
    ops.croit.force_delete_bucket(bucket.name)


def op_usage(ops, params, body):
    uid = resolve_uid(params, required=False)
    users = [u for u in sorted(ops.croit.users) if not uid or u == uid]
    zero = {"bytes_sent": 0, "bytes_received": 0, "ops": 0, "successful_ops": 0}
    return {"entries": [] if params.bool("show-entries", True) else [],
            "summary": [{"user": u, "categories": [], "total": zero} for u in users]
            if params.bool("show-summary", True) else []}


def not_allowed(method: str) -> AdminError:
    return AdminError(405, "MethodNotAllowed", method)


def not_implemented(what: str) -> AdminError:
    return AdminError(501, "NotImplemented", what)


def find_route(method: str, path: str, params: Params):
    """(cap type, needed perm, operation) for a request, or an AdminError."""
    perm = "read" if method in ("GET", "HEAD") else "write"
    if path == "user":
        if params.has("caps"):
            return "users", perm, _raise(not_implemented("user caps"))
        if method == "GET":
            if params.has("quota"):
                return "users", perm, op_quota_get
            if params.has("list"):
                return "users", perm, op_user_list
            if params.has("stats"):
                return "users", perm, _raise(not_implemented("user stats"))
            return "users", perm, op_user_info
        if method == "PUT":
            if params.has("key"):
                return "users", perm, op_key_put
            if params.has("subuser"):
                return "users", perm, op_subuser_create
            return "users", perm, op_quota_put if params.has("quota") else op_user_create
        if method == "POST":
            return "users", perm, op_subuser_modify if params.has("subuser") else op_user_modify
        if method == "DELETE":
            if params.has("key"):
                return "users", perm, op_key_delete
            return "users", perm, op_subuser_remove if params.has("subuser") else op_user_delete
    elif path == "bucket":
        if method == "GET":
            return "buckets", perm, op_bucket_info
        if method == "PUT" and params.has("quota"):
            return "buckets", perm, op_bucket_quota_put
        if method == "DELETE" and not params.has("index"):
            return "buckets", perm, op_bucket_delete
        if method in ("PUT", "POST", "DELETE"):
            return "buckets", perm, _raise(not_implemented("bucket link/unlink/index"))
    elif path == "usage":
        if method == "GET":
            return "usage", perm, op_usage
        if method == "DELETE":
            return "usage", perm, _raise(not_implemented("usage trim"))
    elif path == "metadata/user":
        if method == "GET":
            return "metadata", perm, op_metadata_user
    else:
        raise AdminError(404, "NoSuchKey", path)
    raise not_allowed(method)


def _raise(exc: AdminError):
    def op(*_a, **_k):
        raise exc

    return op


# --- app -------------------------------------------------------------------------


def _error_response(exc: AdminError) -> JSONResponse:
    tx = uuid.uuid4().hex
    body = {"Code": exc.code, "Message": "", "RequestId": f"tx{tx[:21]}-{tx[21:31]}-mock-default",
            "HostId": f"{tx[:8]}-mock-default"}
    detail = re.sub(r"[^\x20-\x7e]", "?", exc.detail)[:200]
    return JSONResponse(body, exc.status, headers={"x-mock-detail": detail} if detail else None)


def _code_for(status: int) -> str:
    return {400: "InvalidArgument", 404: "NoSuchEntity", 409: "Conflict"}.get(
        status, "InternalError")


def create_app(
    croit_state,
    admins: list[AdminPrincipal] | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """The Admin Ops app over a `CroitState` (the shared in-memory state)."""
    app = FastAPI(title="RGW Admin Ops mock", docs_url=None, redoc_url=None, openapi_url=None)
    ops = AdminOps(croit_state, admins if admins is not None else default_admins(), clock)
    app.state.admin_ops = ops
    app.state.croit = croit_state

    @app.api_route("/admin/{path:path}", methods=["GET", "PUT", "POST", "DELETE", "HEAD"])
    async def admin(request: Request, path: str):
        body = await request.body()
        if len(body) > MAX_BODY:
            raise AdminError(400, "InvalidArgument", "body too large")
        principal = ops.authenticate(request, body)
        params = Params(request.scope.get("query_string", b"").decode("latin-1"))
        kind, perm, fn = find_route(request.method, path.rstrip("/"), params)
        check_cap(principal, kind, perm)

        def run():
            with croit_state.lock:
                try:
                    return fn(ops, params, body)
                except AdminError:
                    raise
                except Exception as e:
                    if type(e).__name__ != "ApiError":  # croit_mock.ApiError, by duck type
                        raise
                    raise AdminError(e.status, _code_for(e.status), e.message) from e

        result = await run_in_threadpool(run)
        if result is None:
            return Response(status_code=200)
        return JSONResponse(result)

    @app.exception_handler(AdminError)
    async def _admin_error(_req, exc: AdminError):
        return _error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req, exc: StarletteHTTPException):
        return _error_response(AdminError(exc.status_code, "NoSuchKey" if exc.status_code == 404
                                          else "MethodNotAllowed", str(exc.detail)))

    return app


def start_server(app: FastAPI, host: str, port: int):
    """Serve `app` on a background thread and wait until it listens (dev/demo helper).
    Returns the uvicorn server; set `.should_exit = True` to stop it."""
    import threading

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, name="mock-admin-ops", daemon=True).start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("the Admin Ops mock did not start")
        time.sleep(0.05)
    return server


def main():
    """Standalone: a bare croit state (seeded) plus this API, no croit HTTP server."""
    import uvicorn

    try:
        from mock.croit_mock import create_app as create_croit
    except ImportError:
        from croit_mock import create_app as create_croit
    host = os.environ.get("CROIT_MOCK_HOST", "127.0.0.1")
    port = int(os.environ.get("CROIT_MOCK_ADMIN_OPS_PORT", "18082"))
    app = create_app(create_croit(seed=True, task_seconds=0).state.croit)
    print(f"RGW Admin Ops mock: RGW_ADMIN_URL=http://{host}:{port}", flush=True)
    print(describe_admins(app), flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
