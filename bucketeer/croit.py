"""Async client for the croit management API (the admin plane).

One `CroitClient` per app, created in the app lifespan (`bucketeer/main.py`) and
closed on shutdown; handlers get it with `get_croit`. Methods are thin: they
return croit's JSON as plain dicts/lists (`None` for 204 answers). Calls that
croit answers with a task return the task dict; pass it to `wait_for_task`.

Rules:
- Every path parameter goes through `_path()`, which percent-encodes it with
  `quote(value, safe="")`: `tenant$uid` becomes `tenant%24uid`, and a `/` in a
  value can never add a path segment.
- Non-2xx answers raise `CroitError` (from croit's `ErrorMessage`); a 400 with
  `ignorableWarning` raises `CroitWarning`; network failures and timeouts raise
  `CroitUnavailable`. The API token and warning tokens never appear in an
  exception's str/repr or in log output.
- Never call `/auth/logout`: croit allows one session per user.
- There is deliberately no method for `PUT /s3/users/{uid}/keys`. It replaces
  the user's whole key set and deletes every key left out, so key creation and
  rotation use `put_key` / `delete_key` (add the new key, then remove the old).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request

from .config import Settings

log = logging.getLogger(__name__)

REDACTED = "<redacted>"
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_TASK_TIMEOUT = 300.0
OUTPUT_TAIL = 500  # characters of task output kept in a CroitTaskFailed


def _quote(value: object) -> str:
    """Percent-encode one path parameter. Empty values and the dot segments `.`/`..`
    are rejected: they are not changed by quoting, and HTTP clients and servers
    resolve them, which would retarget the request to another endpoint."""
    text = str(value)
    if text in ("", ".", ".."):
        raise ValueError(f"invalid croit path parameter: {text!r}")
    return quote(text, safe="")


def _path(template: str, **params: object) -> str:
    """Fill `{name}` placeholders with percent-encoded values (the only place
    where request paths are built)."""
    return template.format(**{k: _quote(v) for k, v in params.items()})


def _drop_none(**fields: object) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


# --- errors --------------------------------------------------------------------


class CroitClientError(Exception):
    """Base class of everything the client raises on purpose."""


class CroitUnavailable(CroitClientError):
    """croit could not be reached: connection failure, TLS error or timeout."""


class CroitError(CroitClientError):
    """croit answered with a non-2xx status (its `ErrorMessage`)."""

    def __init__(self, status: int, code: int | None, message: str, details: str | None = None):
        super().__init__(status, code, message, details)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def __str__(self) -> str:
        text = f"croit {self.status}: {self.message}"
        return f"{text} ({self.details})" if self.details else text

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, "
            f"message={self.message!r}, details={self.details!r})"
        )


class CroitWarning(CroitError):
    """A 400 whose body carries `ignorableWarning`: croit asks for confirmation.

    Resend the request with `ignoreWarning=<token>` to proceed. The token is an
    attribute but is kept out of str/repr (and out of the message text, where
    croit may repeat it).
    """

    def __init__(
        self,
        status: int,
        code: int | None,
        message: str,
        details: str | None,
        warning_text: str,
        token: str,
        affirmative_label: str,
    ):
        super().__init__(status, code, message.replace(token, REDACTED), details)
        self.warning_text = warning_text
        self.token = token
        self.affirmative_label = affirmative_label

    def __repr__(self) -> str:
        return (
            f"CroitWarning(status={self.status!r}, message={self.message!r}, "
            f"affirmative_label={self.affirmative_label!r})"
        )


class CroitTaskError(CroitClientError):
    """Base of task problems; `task_id` identifies the croit task."""

    def __init__(self, message: str, task_id: int | None = None):
        super().__init__(message)
        self.task_id = task_id


class CroitTaskFailed(CroitTaskError):
    """A task ended in any way other than state FINISHED with statusCode 0."""

    def __init__(self, task: dict):
        self.state = task.get("state")
        self.status_code = task.get("statusCode")
        self.name = task.get("name")
        self.output_tail = str(task.get("output") or "")[-OUTPUT_TAIL:]
        super().__init__(
            f"croit task {task.get('id')} ({self.name}) ended {self.state} "
            f"with status code {self.status_code}",
            task.get("id"),
        )


class CroitTaskTimeout(CroitTaskError):
    """A task was still running when `wait_for_task` gave up."""


# --- client --------------------------------------------------------------------


def _verify_setting(settings: Settings) -> ssl.SSLContext | bool:
    if not settings.croit_tls_verify:
        return False
    if settings.croit_ca_cert_file:
        return ssl.create_default_context(cafile=settings.croit_ca_cert_file)
    return True  # system store


class CroitClient:
    def __init__(
        self,
        settings: Settings,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._secrets = {settings.croit_api_token.get_secret_value()}
        self.poll_interval = poll_interval
        # httpx logs every request URL at INFO, and put_key carries the secret
        # key in its query string (croit's API requires that).
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(
            base_url=settings.croit_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {settings.croit_api_token.get_secret_value()}",
                "Accept": "application/json",
            },
            verify=_verify_setting(settings),
            timeout=settings.croit_timeout,
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"CroitClient(base_url={str(self._http.base_url)!r})"

    @property
    def closed(self) -> bool:
        return self._http.is_closed

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing --

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def _error_for(self, resp: httpx.Response) -> CroitError:
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or "message" not in body:
            text = self._scrub(resp.text.strip())[:200] or resp.reason_phrase
            return CroitError(status, None, text)
        code = body.get("code")
        message = self._scrub(str(body["message"]))
        details = body.get("details")
        details = self._scrub(str(details)) if details else None
        warning = body.get("ignorableWarning")
        if status == 400 and isinstance(warning, dict) and warning.get("token"):
            return CroitWarning(
                status,
                code if isinstance(code, int) else None,
                message,
                details,
                warning_text=str(warning.get("warningText") or message),
                token=str(warning["token"]),
                affirmative_label=str(warning.get("affirmativeLabel") or ""),
            )
        return CroitError(status, code if isinstance(code, int) else None, message, details)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        # `path` is relative ("s3/users"), so it joins onto the base URL's /api.
        try:
            resp = await self._http.request(
                method, path.lstrip("/"), params=params, json=json
            )
        except httpx.TimeoutException as e:
            raise CroitUnavailable(f"croit timed out: {method} {path}") from e
        except httpx.HTTPError as e:
            raise CroitUnavailable(
                f"croit unreachable: {method} {path} ({type(e).__name__})"
            ) from e
        if not resp.is_success:
            raise self._error_for(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise CroitError(resp.status_code, None, "croit sent a non-JSON answer") from e

    # -- session --

    async def token_info(self) -> dict:
        """Who the API token belongs to (`username`, `roles`, `expiry`)."""
        return await self._request("GET", "/auth/token-info")

    # -- tasks --

    async def get_task(self, task_id: int) -> dict:
        return await self._request("GET", _path("/tasks/{id}", id=task_id))

    async def wait_for_task(
        self, task: dict | int, timeout: float = DEFAULT_TASK_TIMEOUT
    ) -> dict:
        """Poll until the task is `done`; return it if FINISHED with statusCode 0.

        Raises CroitTaskFailed for any other end and CroitTaskTimeout when it is
        still running after `timeout` seconds.
        """
        task_id = task["id"] if isinstance(task, dict) else task
        current = task if isinstance(task, dict) and "done" in task else None
        deadline = time.monotonic() + timeout
        while True:
            if current is None:
                current = await self.get_task(task_id)
            if current.get("done"):
                if current.get("state") == "FINISHED" and current.get("statusCode") == 0:
                    return current
                raise CroitTaskFailed(current)
            if time.monotonic() >= deadline:
                raise CroitTaskTimeout(
                    f"croit task {task_id} still {current.get('state')} after {timeout}s",
                    task_id,
                )
            await asyncio.sleep(self.poll_interval)
            current = None

    # -- S3 users --

    async def list_users(self, uids: list[str] | None = None) -> list[dict]:
        """All users, or only `uids`. Note the spec marks `uid` as required."""
        return await self._request("GET", "/s3/users", params={"uid": uids} if uids else None)

    async def get_user(self, uid: str) -> dict:
        users = await self.list_users([uid])
        if not users:
            raise CroitError(404, 404, f"User {uid} not found")
        return users[0]

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
        body = _drop_none(
            uid=uid, name=name, email=email, tenant=tenant,
            defaultPlacement=default_placement, defaultStorageClass=default_storage_class,
        )
        await self._request("POST", "/s3/users", json=body)

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
        """Only the given fields change; `email=""` or `default_placement=""` clears."""
        body = _drop_none(
            name=name, email=email, suspended=suspended,
            defaultPlacement=default_placement, defaultStorageClass=default_storage_class,
        )
        await self._request("PATCH", _path("/s3/users/{uid}", uid=uid), json=body)

    async def delete_user(self, uid: str) -> None:
        await self._request("DELETE", _path("/s3/users/{uid}", uid=uid))

    async def list_user_buckets(self, uid: str) -> list[dict]:
        return await self._request("GET", _path("/s3/users/{uid}/buckets", uid=uid))

    async def set_user_quota(
        self, uid: str, *, enabled: bool, max_size: int | None = None,
        max_objects: int | None = None,
    ) -> None:
        """`max_size` in bytes; a negative bound means unlimited."""
        await self._request(
            "PUT", _path("/s3/users/{uid}/quota", uid=uid),
            json=_drop_none(enabled=enabled, maxSize=max_size, maxObjects=max_objects),
        )

    async def set_user_bucket_quota(
        self, uid: str, *, enabled: bool, max_size: int | None = None,
        max_objects: int | None = None,
    ) -> None:
        """The same bound applied to each bucket the user owns."""
        await self._request(
            "PUT", _path("/s3/users/{uid}/bucket-quota", uid=uid),
            json=_drop_none(enabled=enabled, maxSize=max_size, maxObjects=max_objects),
        )

    async def cfg_file(self, uid: str) -> dict:
        """The `.s3cfg` download (`fileName`, `fileContents`), for endpoint discovery."""
        return await self._request("GET", _path("/s3/users/{uid}/cfg-file/download", uid=uid))

    # -- keys --

    async def list_keys(self, uid: str) -> list[dict]:
        """`[{user, access_key, secret_key}]`; either key can be null."""
        return await self._request("GET", _path("/s3/users/{uid}/keys", uid=uid))

    async def get_key(self, uid: str) -> dict | None:
        return await self._request("GET", _path("/s3/users/{uid}/key", uid=uid))

    async def put_key(self, uid: str, access_key: str, secret_key: str) -> None:
        """Add one key (or replace the secret of an existing one). The secret goes in
        the query string because that is how croit takes it."""
        await self._request(
            "PUT",
            _path("/s3/users/{uid}/keys/{access_key}", uid=uid, access_key=access_key),
            params={"secretKey": secret_key},
        )

    async def delete_key(self, uid: str, access_key: str) -> None:
        await self._request(
            "DELETE", _path("/s3/users/{uid}/keys/{access_key}", uid=uid, access_key=access_key)
        )

    # -- placements --

    async def list_placements(self) -> dict:
        """`RgwPlacementResponse`: `placements` (one row per placement rule and
        storage class, `isDefault` marks the zonegroup default) plus
        `defaultPlacementRule` / `defaultStorageClass` (null when none is set)."""
        return await self._request("GET", "/s3/placements")

    # -- buckets --

    async def list_buckets(self) -> list[dict]:
        return await self._request("GET", "/s3/buckets")

    async def get_bucket(self, name: str, owner: str | None = None) -> dict:
        """Details read over S3: `acl`, `cors`, `versioning`."""
        return await self._request(
            "GET", _path("/s3/buckets/{name}", name=name), params=_drop_none(owner=owner) or None
        )

    async def create_bucket(
        self,
        name: str,
        owner: str,
        *,
        versioning: str | None = None,
        object_lock: bool | None = None,
        placement: str | None = None,
        acl: list[dict] | None = None,
        expected_objects: int | None = None,
    ) -> dict:
        """Create a bucket (`name` without tenant: the owner's tenant decides). Returns a task."""
        body = _drop_none(
            owner=owner, versioning=versioning, objectLock=object_lock, placement=placement,
            acl=acl, expectedNumberOfObjects=expected_objects,
        )
        return await self._request("PUT", _path("/s3/buckets/{name}", name=name), json=body)

    async def update_bucket(
        self,
        name: str,
        *,
        owner: str | None = None,
        acl: list[dict] | None = None,
        versioning: str | None = None,
    ) -> None:
        body = _drop_none(owner=owner, acl=acl, versioning=versioning)
        await self._request("PATCH", _path("/s3/buckets/{name}", name=name), json=body)

    async def delete_bucket(
        self,
        name: str,
        *,
        delete_data: bool = False,
        confirmation: str | None = None,
        ignore_warning: str | None = None,
    ) -> dict:
        """Delete a bucket; returns a task. An empty bucket goes at once. A non-empty one
        raises CroitWarning until `delete_data`, the `confirmation` sentence and the
        `ignore_warning` token are all given (see `delete_bucket_and_data`)."""
        body = _drop_none(
            name=name,
            deleteData=True if delete_data else None,
            thisWillDeleteData=confirmation,
        )
        return await self._request(
            "DELETE", _path("/s3/buckets/{name}", name=name),
            params=_drop_none(ignoreWarning=ignore_warning) or None, json=body,
        )

    async def delete_bucket_and_data(self, name: str, *, user_confirmed: bool) -> dict:
        """Delete a bucket even if it holds objects, running croit's confirmation handshake.

        `user_confirmed` must be True, meaning the caller already had the user
        confirm (type the bucket name); otherwise this raises ValueError before
        touching croit. First call plain; on the CroitWarning for a non-empty bucket,
        resend with deleteData, the sentence from the refusal and its token.
        """
        if user_confirmed is not True:
            raise ValueError("bucket data deletion needs the user's explicit confirmation")
        try:
            return await self.delete_bucket(name)  # empty bucket: done
        except CroitWarning as warning:
            sentence = _confirmation_sentence(warning)
            return await self.delete_bucket(
                name, delete_data=True, confirmation=sentence, ignore_warning=warning.token
            )

    async def set_bucket_quota(
        self, name: str, *, enabled: bool, max_size: int | None = None,
        max_objects: int | None = None,
    ) -> None:
        await self._request(
            "PUT", _path("/s3/buckets/{name}/quota", name=name),
            json=_drop_none(enabled=enabled, maxSize=max_size, maxObjects=max_objects),
        )

    # -- bucket policy --

    async def get_bucket_policy(self, name: str, owner: str | None = None) -> dict | None:
        """The parsed policy document, or None for a bucket without one."""
        reply = await self._request(
            "GET", _path("/s3/buckets/{name}/policy", name=name),
            params=_drop_none(owner=owner) or None,
        )
        text = (reply or {}).get("policyText")
        return json.loads(text) if text else None

    async def put_bucket_policy(
        self, name: str, policy: dict | str, owner: str | None = None
    ) -> None:
        """Replace the whole policy (`Version`, `Id`, `Statement`)."""
        doc = json.loads(policy) if isinstance(policy, str) else policy
        await self._request(
            "POST", _path("/s3/buckets/{name}/policy", name=name),
            params=_drop_none(owner=owner) or None, json=doc,
        )

    async def delete_bucket_policy(self, name: str, owner: str | None = None) -> None:
        await self._request(
            "DELETE", _path("/s3/buckets/{name}/policy", name=name),
            params=_drop_none(owner=owner) or None,
        )

    # -- usage --

    async def usage_by_user(self, uid: str, hours: int) -> dict:
        """Expensive on the cluster: do not poll."""
        return await self._request(
            "GET", _path("/s3/usage/by-user/{uid}", uid=uid), params={"hours": hours}
        )

    async def usage_by_bucket(self, name: str, hours: int) -> dict:
        """Expensive on the cluster: do not poll."""
        return await self._request(
            "GET", _path("/s3/usage/by-bucket/{name}", name=name), params={"hours": hours}
        )


_SENTENCE = re.compile(r'thisWillDeleteData="([^"]+)"')


def _confirmation_sentence(warning: CroitWarning) -> str:
    """The sentence croit wants typed back: its `details`, else quoted in the text."""
    if warning.details:
        return warning.details
    found = _SENTENCE.search(warning.warning_text)
    if found:
        return found.group(1)
    raise CroitError(
        warning.status, warning.code,
        "croit's refusal did not name the confirmation sentence",
    )


def get_croit(request: Request) -> CroitClient:
    """FastAPI dependency: the app's single client."""
    return request.app.state.croit
