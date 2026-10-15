"""Async client for the Ceph RGW Admin Ops API: the admin plane without croit.

`RgwAdminClient` has the same public async methods, return shapes and exceptions as
`CroitClient` (bucketeer/croit.py) for everything Bucketeer calls, so `create_app` can put
either one into `app.state.croit` (settings `ADMIN_BACKEND=croit|rgw`). Callers do not change.
It is developed against `mock/rgw_admin_mock.py`, whose docstring is the contract.

Mapping (croit method -> Admin Ops call; the returned dicts have croit's keys):
- `list_users` / `get_user` / `list_user_names`: `GET /admin/metadata/user` (uid list, paged;
  falls back to `GET /admin/user?list` when the metadata cap is missing) plus one
  `GET /admin/user?uid=` per user with bounded concurrency. User dicts carry `uid` (with
  `tenant$`), `name`, `email`, `suspended` (bool), `userQuota`, `bucketQuota` as `{enabled,
  maxSize, maxObjects}` (negative = unlimited, as in croit), `defaultPlacement`,
  `defaultStorageClass`, `subusers`.
- `create_user`: `PUT /admin/user` with `exclusive=true` (a repeat create without it is a
  silent modify in RGW), so an existing uid is 409 like croit. `tenant` is folded into the
  uid (`tenant$uid`). RGW generates a key (as croit did).
- `update_user`: `POST /admin/user` (`display-name`, `email`, `suspended`). `delete_user`:
  `DELETE /admin/user` WITHOUT `purge-data`: a user that owns buckets is refused (409).
- `set_user_quota` / `set_user_bucket_quota` / `set_bucket_quota`: `PUT ...?quota` with a JSON
  body. RGW replaces the WHOLE quota from a JSON body, so omitted fields are first read back
  (read-modify-write) to keep croit's "only the given fields change" meaning.
- `list_keys` / `get_key` (`keys[]` of user info), `put_key` (`PUT /admin/user?key` with
  `generate-key=false`; the secret travels as a query value), `delete_key` (`DELETE
  /admin/user?key`). RGW answers 403 `InvalidAccessKeyId` both for a key that is already gone
  and for another user's key: the user's key list decides, and a missing key becomes 404 like
  croit (an own key that stays listed keeps the 403).
- `list_user_buckets` / `list_buckets`: `GET /admin/bucket?stats=true[&uid=]`. Dicts carry
  `bucket` (`tenant:name` for a tenant bucket), `id`, `owner`, `tenant`, `placement_rule`,
  `mtime`, `bucket_quota`, `usage`, `usageSum {numObjects, size, ...}` (the `rgw.main`
  category). Tenant caveat: on Reef `GET /admin/bucket?stats=true` WITHOUT `uid` may omit
  tenant buckets (the mock lists them); `list_buckets` is one call by design (A6 may query per
  user), verify on the cluster.
- `get_bucket`: `GET /admin/bucket?uid=<owner>&bucket=<bare>&stats=true` (the owner is known
  to callers; without it `bucket=tenant/name`); only `versioning` (OFF|ENABLED|SUSPENDED) is
  supplied, not croit's `acl` and `cors`.

Service user: the identity behind `RGW_ADMIN_ACCESS_KEY` is an ordinary RGW user. `service_uid()`
(`GET /admin/user?access-key=`, cached after the first success, raises when the lookup fails)
names it; `list_users`/`list_user_names`/`list_buckets` leave it and its buckets out, and the
app refuses it as a login, mapping target, share/ACL recipient and admin suspend/quota target
(`rgw_user.is_service_uid`, bare uid, case-insensitive, any tenant). While the lookup fails,
those paths fail closed (unavailable/503). `CroitClient.service_uid()` is None.
`list_users` reads concurrently (bounded per client and loop) and cancels the rest on the
first failure. Only a 404 with RGW's `NoSuchUser`/`NoSuchBucket` code means "gone"; any other
404 (wrong `RGW_ADMIN_URL`) is a 502. Quota PUT bodies carry over `check_on_raw`.

Gaps: no Admin Ops counterpart, so these raise `RgwAdminUnsupported` (a `CroitError`, status
501, "not_supported_by_backend: ..." naming the Phase 5 item that replaces the caller):
`create_bucket` and the versioning PATCH of `update_bucket` (A3, S3 data plane), `delete_bucket`
and `delete_bucket_and_data` (A4; croit returns a task there and a task cannot be faked),
`wait_for_task` and `get_task` (no tasks exist: A3/A4 remove the callers), `list_placements`
and `cfg_file` (A5: no caller reaches them; the admin types the placements and
`S3_ENDPOINT_URL` is required), plus
`token_info`, the bucket policy calls (S3 data plane) and the usage calls (not mapped yet).

Signing: every request is signed with botocore's `S3SigV4Auth` (service `s3`, `S3_REGION`),
which also sends `x-amz-content-sha256`. The query string is encoded once here
(`quote(safe="-_.~")`: `+` -> `%2B`, `$` -> `%24`), signed as that exact text and sent as that
exact text. Secrets (the admin secret key and the secret of `put_key`) never appear in logs,
exception text or repr; request URLs are never put into an exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import weakref
from typing import Any
from urllib.parse import quote

import httpx
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from .config import Settings
from .croit import REDACTED, CroitError, CroitUnavailable

log = logging.getLogger(__name__)

CONCURRENCY = 8  # parallel user reads in list_users
PAGE = 1000  # uids per metadata page
UNSUPPORTED = "not_supported_by_backend"


class RgwAdminUnsupported(CroitError):
    """The call has no RGW Admin Ops counterpart (status 501)."""

    def __init__(self, method: str, replacement: str):
        super().__init__(
            501, None, f"{UNSUPPORTED}: {method} needs croit; {replacement}", UNSUPPORTED
        )


def _enc(value: str) -> str:
    return quote(value, safe="-_.~")


def _query(pairs: list[tuple[str, str | None]]) -> str:
    """`key=value` joined by `&`; a None value is a bare flag (`?quota`)."""
    return "&".join(_enc(k) if v is None else f"{_enc(k)}={_enc(v)}" for k, v in pairs)


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _verify(settings: Settings) -> ssl.SSLContext | bool:
    if not settings.s3_tls_verify:
        return False
    if settings.s3_ca_cert_file:
        return ssl.create_default_context(cafile=settings.s3_ca_cert_file)
    return True


def _quota_view(q: dict | None) -> dict:
    q = q or {}
    size = q.get("max_size")
    if size is None and q.get("max_size_kb") is not None:
        size = int(q["max_size_kb"]) * 1024 if int(q["max_size_kb"]) >= 0 else -1
    objects = q.get("max_objects")
    return {
        "enabled": bool(q.get("enabled")),
        "maxSize": -1 if size is None else int(size),
        "maxObjects": -1 if objects is None else int(objects),
    }


def _user_view(info: dict) -> dict:
    user_id = str(info.get("user_id") or "")
    tenant = str(info.get("tenant") or "")
    uid = user_id if "$" in user_id or not tenant else f"{tenant}${user_id}"
    return {
        "uid": uid,
        "name": info.get("display_name") or "",
        "email": info.get("email") or "",
        "suspended": bool(info.get("suspended")),
        "subusers": info.get("subusers") or [],
        "userQuota": _quota_view(info.get("user_quota")),
        "bucketQuota": _quota_view(info.get("bucket_quota")),
        "defaultPlacement": info.get("default_placement") or None,
        "defaultStorageClass": info.get("default_storage_class") or None,
    }


def _bucket_view(stats: dict) -> dict:
    tenant = str(stats.get("tenant") or "")
    name = str(stats.get("bucket") or "")
    main = (stats.get("usage") or {}).get("rgw.main") or {}
    objects, size = int(main.get("num_objects") or 0), int(main.get("size") or 0)
    kib = -(-size // 1024)
    usage = {"numObjects": float(objects), "num_objects": float(objects), "size": size,
             "sizeKiB": kib, "size_kb": kib}
    return {
        "bucket": f"{tenant}:{name}" if tenant else name,
        "id": stats.get("id"),
        "owner": stats.get("owner") or "",
        "tenant": tenant,
        "placement_rule": stats.get("placement_rule"),
        "zonegroup": stats.get("zonegroup"),
        "mtime": stats.get("mtime"),
        "bucket_quota": _quota_view(stats.get("bucket_quota")),
        "usage": {"default": usage},
        "usageSum": usage,
    }


def _bucket_ref(name: str) -> str:
    """croit's `tenant:bucket` -> Admin Ops' `tenant/bucket`."""
    return name.replace(":", "/", 1)


class RgwAdminClient:
    backend = "rgw"  # callers branch on this (placements, endpoint discovery), not on Settings

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base = settings.rgw_admin_url.rstrip("/")
        self._creds = Credentials(
            settings.rgw_admin_access_key, settings.rgw_admin_secret_key.get_secret_value()
        )
        self._region = settings.s3_region
        self._secrets = {settings.rgw_admin_secret_key.get_secret_value()}
        self._service_uid: str | None = None
        self._sems: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # one per loop
        # httpx logs every request URL at INFO and put_key carries a secret in its query;
        # botocore logs the canonical request (that query) and signatures at DEBUG.
        for name in ("httpx", "httpcore", "botocore", "botocore.auth", "boto3"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(
            headers={"Accept": "application/json"},
            verify=_verify(settings),
            timeout=settings.rgw_admin_timeout,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    def __repr__(self) -> str:
        return f"RgwAdminClient(base_url={self._base!r})"

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing --

    def _scrub(self, text: str, extra: tuple[str, ...] = ()) -> str:
        for secret in (*self._secrets, *(x for x in extra if x)):
            text = text.replace(secret, REDACTED)
        return text

    def _error_for(self, resp: httpx.Response, extra: tuple[str, ...]) -> CroitError:
        try:
            body = resp.json()
        except ValueError:
            body = None
        rgw_code = None
        if isinstance(body, dict) and body.get("Code"):
            rgw_code = str(body["Code"])
            code = self._scrub(rgw_code, extra)
            message = self._scrub(str(body.get("Message") or ""), extra)
            text = f"{code}: {message}" if message else code
        else:
            text = self._scrub(resp.text.strip(), extra)[:200] or resp.reason_phrase
        if resp.status_code == 404 and rgw_code not in ("NoSuchUser", "NoSuchBucket"):
            # Not RGW's "no such user/bucket" (a wrong RGW_ADMIN_URL path, a proxy): callers
            # read a 404 as "it is gone", so this must not look like one.
            err = CroitError(502, None, f"RGW Admin Ops answered 404 ({text}); check RGW_ADMIN_URL")
        else:
            err = CroitError(resp.status_code, None, text)
        err.rgw_code = rgw_code
        return err

    async def _request(
        self,
        method: str,
        path: str,
        pairs: list[tuple[str, str | None]] | None = None,
        *,
        body: Any = None,
        secrets: tuple[str, ...] = (),
    ) -> Any:
        query = _query(pairs or [])
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        data = json.dumps(body).encode() if body is not None else b""
        signed = AWSRequest(method=method, url=url, data=data)
        S3SigV4Auth(self._creds, "s3", self._region).add_auth(signed)
        headers = {k: str(v) for k, v in signed.headers.items()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = await self._http.request(method, url, content=data, headers=headers)
        except httpx.TimeoutException as e:
            raise CroitUnavailable(f"RGW Admin Ops timed out: {method} {path}") from e
        except httpx.HTTPError as e:
            raise CroitUnavailable(
                f"RGW Admin Ops unreachable: {method} {path} ({type(e).__name__})"
            ) from e
        if not resp.is_success:
            raise self._error_for(resp, secrets)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise CroitError(resp.status_code, None, "RGW sent a non-JSON answer") from e

    # -- users --

    async def _user_info(self, uid: str) -> dict:
        info = await self._request("GET", "/admin/user", [("uid", uid)])
        if not isinstance(info, dict):
            raise CroitError(502, None, "RGW sent an unexpected user answer")
        return info

    async def _all_uids(self) -> list[str]:
        """Every uid except the service user's (it is not a Bucketeer-managed account)."""
        mine = await self.service_uid()
        return [u for u in await self._raw_uids()
                if u.rpartition("$")[2].lower() != mine.rpartition("$")[2].lower()]

    async def _raw_uids(self) -> list[str]:
        uids: list[str] = []
        marker = ""
        try:
            while True:
                pairs: list[tuple[str, str | None]] = [("max-entries", str(PAGE))]
                if marker:
                    pairs.append(("marker", marker))
                page = await self._request("GET", "/admin/metadata/user", pairs)
                if isinstance(page, list):  # a plain array: everything
                    return [str(u) for u in page]
                keys = [str(u) for u in (page or {}).get("keys") or []]
                uids += keys
                if not (page or {}).get("truncated") or not keys:
                    return uids
                marker = str((page or {}).get("marker") or keys[-1])
        except CroitError as e:
            if e.status not in (403, 404, 405, 501) or uids:
                raise
        page = await self._request("GET", "/admin/user", [("list", None)])
        return [str(u) for u in (page or {}).get("keys") or []]

    def _semaphore(self) -> asyncio.Semaphore:
        """One concurrency limit per client and event loop (a semaphore binds to the loop
        it first waits in), so overlapping admin requests share the bound too."""
        loop = asyncio.get_running_loop()
        sem = self._sems.get(loop)
        if sem is None:
            sem = self._sems[loop] = asyncio.Semaphore(CONCURRENCY)
        return sem

    async def service_uid(self) -> str:
        """The RGW uid that owns `RGW_ADMIN_ACCESS_KEY` (looked up once, then cached). The
        portal's own Admin Ops identity is an ordinary RGW user: callers must never hand it
        to a person. Raises when the lookup fails, so they fail closed."""
        if self._service_uid is None:
            try:
                info = await self._request(
                    "GET", "/admin/user", [("access-key", self._creds.access_key)])
                uid = _user_view(info)["uid"] if isinstance(info, dict) else ""
            except CroitError as e:
                raise CroitError(  # keep a credentials refusal visible as such
                    e.status if e.status in (401, 403) else 502, None,
                    f"could not identify the Admin Ops service user ({e.message})",
                ) from e
            if not uid:
                raise CroitError(502, None, "could not identify the Admin Ops service user")
            self._service_uid = uid
        return self._service_uid

    async def list_users(self, uids: list[str] | None = None) -> list[dict]:
        """All users except the Admin Ops service user, or only `uids` (as croit does, an
        unknown uid in the filter is a 404). Reads run concurrently (bounded); the first
        failure cancels the rest, so nothing keeps hitting RGW after the caller has an error."""
        wanted = list(uids) if uids else await self._all_uids()
        sem = self._semaphore()

        async def one(uid: str) -> dict | None:
            async with sem:
                return await self.get_user(uid)

        tasks = [asyncio.ensure_future(one(u)) for u in wanted]
        try:
            if tasks:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for t in tasks:  # the error that stopped the run (cancelled siblings are not errors)
            if not t.cancelled() and t.exception() is not None:
                raise t.exception()
        found = [t.result() for t in tasks]
        if uids:
            for uid, user in zip(wanted, found, strict=True):
                if user is None:
                    raise CroitError(404, None, f"User {uid} not found")
            return found
        return [u for u in found if u is not None]  # deleted while listing

    async def list_user_names(self) -> list[str]:
        return await self._all_uids()

    async def get_user(self, uid: str) -> dict | None:
        try:
            return _user_view(await self._user_info(uid))
        except CroitError as e:
            if e.status == 404 and getattr(e, "rgw_code", None) == "NoSuchUser":
                return None
            raise

    async def create_user(
        self,
        uid: str,
        name: str,
        email: str | None = None,
        tenant: str | None = None,
        *,
        default_placement: str | None = None,
        default_storage_class: str | None = None,
    ) -> None:
        full = f"{tenant}${uid}" if tenant and "$" not in uid else uid
        pairs: list[tuple[str, str | None]] = [
            ("uid", full), ("display-name", name), ("exclusive", "true"),
        ]
        for key, value in (("email", email), ("default-placement", default_placement),
                           ("default-storage-class", default_storage_class)):
            if value is not None:
                pairs.append((key, value))
        await self._request("PUT", "/admin/user", pairs)

    async def update_user(
        self,
        uid: str,
        *,
        name: str | None = None,
        email: str | None = None,
        suspended: bool | None = None,
        default_placement: str | None = None,
        default_storage_class: str | None = None,
    ) -> None:
        pairs: list[tuple[str, str | None]] = [("uid", uid)]
        for key, value in (("display-name", name), ("email", email),
                           ("default-placement", default_placement),
                           ("default-storage-class", default_storage_class)):
            if value is not None:
                pairs.append((key, value))
        if suspended is not None:
            pairs.append(("suspended", _flag(suspended)))
        await self._request("POST", "/admin/user", pairs)

    async def delete_user(self, uid: str) -> None:
        await self._request("DELETE", "/admin/user", [("uid", uid)])

    # -- quotas --

    @staticmethod
    def _quota_body(raw: dict | None, enabled: bool, max_size: int | None,
                    max_objects: int | None) -> dict:
        """The body for a PUT: RGW replaces the WHOLE quota from a JSON body, so everything
        not given (limits, `check_on_raw`) is carried over from the quota read back first."""
        current = _quota_view(raw)
        size = current["maxSize"] if max_size is None else max_size
        objects = current["maxObjects"] if max_objects is None else max_objects
        body = {"enabled": enabled, "max_size": -1 if size < 0 else size,
                "max_objects": -1 if objects < 0 else objects}
        if isinstance(raw, dict) and "check_on_raw" in raw:
            body["check_on_raw"] = raw["check_on_raw"]
        return body

    async def _set_quota_of_user(self, uid: str, kind: str, enabled: bool,
                                 max_size: int | None, max_objects: int | None) -> None:
        pairs: list[tuple[str, str | None]] = [
            ("quota", None), ("uid", uid), ("quota-type", kind)]
        raw = await self._request("GET", "/admin/user", pairs)
        body = self._quota_body(raw, enabled, max_size, max_objects)
        await self._request("PUT", "/admin/user", pairs, body=body)

    async def set_user_quota(
        self, uid: str, *, enabled: bool, max_size: int | None = None,
        max_objects: int | None = None,
    ) -> None:
        await self._set_quota_of_user(uid, "user", enabled, max_size, max_objects)

    async def set_user_bucket_quota(
        self, uid: str, *, enabled: bool, max_size: int | None = None,
        max_objects: int | None = None,
    ) -> None:
        await self._set_quota_of_user(uid, "bucket", enabled, max_size, max_objects)

    async def set_bucket_quota(
        self, name: str, *, enabled: bool, max_size: int | None = None,
        max_objects: int | None = None,
    ) -> None:
        stats = await self._bucket_stats(name)
        body = self._quota_body(stats.get("bucket_quota"), enabled, max_size, max_objects)
        short = str(stats.get("bucket") or name.partition(":")[2] or name)
        await self._request(
            "PUT", "/admin/bucket",
            [("quota", None), ("uid", str(stats.get("owner") or "")), ("bucket", short)],
            body=body,
        )

    # -- keys --

    async def list_keys(self, uid: str) -> list[dict]:
        """`[{user, access_key, secret_key}]`."""
        keys = (await self._user_info(uid)).get("keys") or []
        return [{"user": k.get("user") or uid, "access_key": k.get("access_key"),
                 "secret_key": k.get("secret_key")} for k in keys if isinstance(k, dict)]

    async def get_key(self, uid: str) -> dict | None:
        keys = await self.list_keys(uid)
        return keys[0] if keys else None

    async def put_key(self, uid: str, access_key: str, secret_key: str) -> None:
        await self._request(
            "PUT", "/admin/user",
            [("key", None), ("uid", uid), ("key-type", "s3"), ("access-key", access_key),
             ("secret-key", secret_key), ("generate-key", "false")],
            secrets=(secret_key,),
        )

    async def delete_key(self, uid: str, access_key: str) -> None:
        try:
            await self._request(
                "DELETE", "/admin/user",
                [("key", None), ("uid", uid), ("key-type", "s3"), ("access-key", access_key)],
            )
        except CroitError as e:
            if e.status != 403 or "InvalidAccessKeyId" not in e.message:
                raise
            # The same answer as a bad admin key: the user's key list tells which it was.
            if not any(k["access_key"] == access_key for k in await self.list_keys(uid)):
                raise CroitError(404, None, f"Key {access_key} not found for user {uid}") from e
            raise

    # -- buckets --

    async def _bucket_stats(self, name: str, owner: str | None = None) -> dict:
        """Stats of one bucket. With the owner: `uid=<owner>&bucket=<bare>&stats=true` (an
        array; empty when the owner does not own it). Without: `bucket=tenant/name`."""
        if owner is not None:
            rows = await self._request("GET", "/admin/bucket", [
                ("uid", owner), ("bucket", name.rpartition(":")[2]), ("stats", "true")])
            bare = name.rpartition(":")[2]
            stats = next((r for r in rows if isinstance(r, dict) and r.get("bucket") == bare),
                         None) if isinstance(rows, list) else None
        else:
            stats = await self._request("GET", "/admin/bucket", [("bucket", _bucket_ref(name))])
            if isinstance(stats, list):
                stats = stats[0] if stats else None
        if not isinstance(stats, dict):
            raise CroitError(404, None, f"Bucket {name} not found")
        return stats

    async def _stats_list(self, pairs: list[tuple[str, str | None]]) -> list[dict]:
        rows = await self._request("GET", "/admin/bucket", [*pairs, ("stats", "true")])
        return [_bucket_view(b) for b in rows or [] if isinstance(b, dict)]

    async def list_user_buckets(self, uid: str) -> list[dict]:
        return await self._stats_list([("uid", uid)])

    async def list_buckets(self) -> list[dict]:
        mine = await self.service_uid()
        return [b for b in await self._stats_list([]) if b["owner"] != mine]

    async def get_bucket(self, name: str, owner: str | None = None) -> dict:
        stats = await self._bucket_stats(name, owner)
        if owner is not None and stats.get("owner") != owner:
            raise CroitError(404, None, f"Bucket {name} not found")
        if not stats.get("versioned"):
            state = "OFF"
        else:
            state = "ENABLED" if stats.get("versioning_enabled") else "SUSPENDED"
        return {"versioning": state}

    # -- gaps: no Admin Ops counterpart --

    async def create_bucket(self, name: str, owner: str, **_kw: Any) -> dict:
        raise RgwAdminUnsupported(
            "create_bucket", "Phase 5 A3 creates buckets on the S3 data plane")

    async def update_bucket(
        self, name: str, *, owner: str | None = None, acl: list[dict] | None = None,
        versioning: str | None = None,
    ) -> None:
        raise RgwAdminUnsupported(
            "update_bucket", "Phase 5 A3 sets versioning on the S3 data plane")

    async def delete_bucket(self, name: str, **_kw: Any) -> dict:
        raise RgwAdminUnsupported(
            "delete_bucket", "croit returns a task here; Phase 5 A4 deletes as a background job")

    async def delete_bucket_and_data(self, name: str, *, user_confirmed: bool) -> dict:
        raise RgwAdminUnsupported(
            "delete_bucket_and_data",
            "the croit confirmation handshake and task; Phase 5 A4 replaces the caller")

    async def get_task(self, task_id: int) -> dict:
        raise RgwAdminUnsupported("get_task", "there are no tasks; Phase 5 A3/A4 remove callers")

    async def wait_for_task(self, task: dict | int, timeout: float = 0) -> dict:
        raise RgwAdminUnsupported(
            "wait_for_task", "there are no tasks; Phase 5 A3/A4 remove the callers")

    async def list_placements(self) -> dict:
        raise RgwAdminUnsupported(
            "list_placements", "A5: placements are the ids admins enter on the Settings tab")

    async def cfg_file(self, uid: str) -> dict:
        raise RgwAdminUnsupported(
            "cfg_file", "A5: S3_ENDPOINT_URL is required with ADMIN_BACKEND=rgw (no .s3cfg)")

    async def token_info(self) -> dict:
        raise RgwAdminUnsupported("token_info", "RGW has no API token session")

    async def get_bucket_policy(self, name: str, owner: str | None = None) -> dict | None:
        raise RgwAdminUnsupported("get_bucket_policy", "policies use the S3 data plane")

    async def put_bucket_policy(self, name: str, policy: dict | str,
                                owner: str | None = None) -> None:
        raise RgwAdminUnsupported("put_bucket_policy", "policies use the S3 data plane")

    async def delete_bucket_policy(self, name: str, owner: str | None = None) -> None:
        raise RgwAdminUnsupported("delete_bucket_policy", "policies use the S3 data plane")

    async def usage_by_user(self, uid: str, hours: int) -> dict:
        raise RgwAdminUnsupported("usage_by_user", "not mapped to /admin/usage yet")

    async def usage_by_bucket(self, name: str, hours: int) -> dict:
        raise RgwAdminUnsupported("usage_by_bucket", "not mapped to /admin/usage yet")
