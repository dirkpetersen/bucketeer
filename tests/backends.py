"""Run app-level tests on either admin backend.

A test module opts in by importing the autouse fixture:

    from .backends import admin_backend  # noqa: F401  (fixture: runs each test on both)

and builds its client with `admin_client(settings, mock, transport)` instead of
`CroitClient(...)`. On the `rgw` backend the client is a `RgwAdminClient` that talks to the
RGW Admin Ops mock over the SAME state as the mock croit app (`mock.state.croit`), through the
same recording `Transport` (whose `.inner` is swapped), so seeded users, buckets and keys are
identical and tests keep asserting on `mock.state.croit`. Tests that depend on croit's HTTP
shape or on bucket create/delete/versioning/placements (Phase 5 A3 to A5) call `croit_only()`.
"""

import json
from urllib.parse import parse_qsl, quote, unquote_plus

import httpx
import pytest

from bucketeer.croit import CroitClient
from bucketeer.rgw_admin import RgwAdminClient
from mock.rgw_admin_mock import create_app as create_ops

CURRENT = {"name": "croit", "secret": ""}


@pytest.fixture(params=["croit", "rgw"], autouse=True)
def admin_backend(request):
    CURRENT["name"] = request.param
    yield request.param
    CURRENT["name"] = "croit"


def admin_secret() -> str:
    """The secret the current backend's client authenticates with (croit's token, or the
    Admin Ops secret key): it must never show up in a response, audit row or log."""
    return CURRENT["secret"]


def error_response(status: int, message: str) -> httpx.Response:
    """An error answer in the current backend's own shape (croit's ErrorMessage, or RGW's
    {Code, Message}), for `transport.fail` hooks."""
    if on_rgw():
        code = {400: "InvalidArgument", 401: "AccessDenied", 403: "AccessDenied"}.get(
            status, "InternalError")
        return httpx.Response(status, json={"Code": code, "Message": message})
    return httpx.Response(status, json={"code": status, "message": message})


def on_rgw() -> bool:
    return CURRENT["name"] == "rgw"


def rgw_only(reason: str):
    """Skip on the croit backend (the behaviour exists only with Admin Ops)."""
    if not on_rgw():
        pytest.skip(f"croit admin backend: {reason}")


def croit_only(reason: str):
    """Skip on the rgw backend; `reason` names what is missing (an A3 to A5 item, or the
    croit-specific thing the test asserts)."""
    if on_rgw():
        pytest.skip(f"rgw admin backend: {reason}")


def admin_client(settings, mock, transport, poll_interval: float = 0.01):
    CURRENT["secret"] = settings.croit_api_token.get_secret_value()
    if not on_rgw():
        return CroitClient(settings, poll_interval=poll_interval, transport=transport)
    ops = create_ops(mock.state.croit)
    admin = ops.state.admin_ops.admins[0]
    rgw_settings = settings.model_copy(update={
        "admin_backend": "rgw", "rgw_admin_url": "http://127.0.0.1:18082",
        "rgw_admin_access_key": admin.access_key,
        "rgw_admin_secret_key": type(settings.croit_api_token)(admin.secret_key),
    })
    CURRENT["secret"] = admin.secret_key
    transport.inner = httpx.ASGITransport(app=ops)
    return RgwAdminClient(rgw_settings, transport=transport)


def croit_view(request: httpx.Request) -> httpx.Request:
    """An Admin Ops request as the croit API call it replaces (method, path, JSON body), so
    the recording `Transport` and tests that inject failures per croit endpoint work on both
    backends. Only the shapes Bucketeer uses are mapped; the real request is what is sent."""
    # decoded twice, like RGW (Tentacle): a `+` is sent as %252B
    q = {k: unquote_plus(v) for k, v in parse_qsl(request.url.query.decode(),
                                                   keep_blank_values=True)}
    enc = lambda x: quote(x, safe="")  # noqa: E731
    uid, m, path = q.get("uid", ""), request.method, request.url.path
    body = json.loads(request.content) if request.content else {}
    out_method, out_path, out_body = m, path, None
    if path == "/admin/user":
        base = f"/api/s3/users/{enc(uid)}"
        sub = f"{base}/subusers/{enc(q.get('subuser', ''))}"
        perm = {"read": "read", "write": "write", "readwrite": "read-write",
                "full": "full-control"}.get(q.get("access", ""))
        if "subuser" in q and "key" in q:
            out_path = f"{sub}/keys/{enc(q['access-key'])}"
            if m == "PUT":
                out_path += f"?secretKey={enc(q['secret-key'])}"
        elif "subuser" in q and m == "PUT":
            out_method, out_path = "POST", base
            out_body = {"subuser": q["subuser"], "permissions": perm}
        elif "subuser" in q and m == "POST":
            out_method, out_path = "PATCH", sub
            out_body = {"permissions": perm}
        elif "subuser" in q and m == "DELETE":
            out_path = sub
        elif m == "PUT" and "key" in q:
            out_path = f"{base}/keys/{enc(q['access-key'])}?secretKey={enc(q['secret-key'])}"
        elif m == "DELETE" and "key" in q:
            out_path = f"{base}/keys/{enc(q['access-key'])}"
        elif m == "PUT" and "quota" in q:
            kind = "quota" if q.get("quota-type") == "user" else "bucket-quota"
            out_path = f"{base}/{kind}"
            out_body = {"enabled": body.get("enabled"), "maxSize": body.get("max_size"),
                        "maxObjects": body.get("max_objects")}
        elif m == "PUT":
            tenant, _, bare = uid.rpartition("$") if "$" in uid else ("", "", uid)
            out_method, out_path = "POST", "/api/s3/users"
            out_body = {"uid": bare, "name": q.get("display-name")}
            if tenant:
                out_body["tenant"] = tenant
            if "email" in q:
                out_body["email"] = q["email"]
        elif m == "POST":
            out_method, out_path = "PATCH", base
            out_body = {}
            if "suspended" in q:
                out_body["suspended"] = q["suspended"] == "true"
            if "display-name" in q:
                out_body["name"] = q["display-name"]
            if "email" in q:
                out_body["email"] = q["email"]
        elif m == "DELETE":
            out_path = base
        else:
            out_path = f"/api/s3/users?uid={enc(uid)}"
    elif path == "/admin/metadata/user":
        out_path = "/api/s3/users"
    elif path == "/admin/bucket":
        name = q.get("bucket", "").replace("/", ":", 1)
        if "$" in uid and ":" not in name and name:
            name = f"{uid.partition('$')[0]}:{name}"
        if m == "PUT" and "quota" in q:
            out_path = f"/api/s3/buckets/{enc(name)}/quota"
            out_body = {"enabled": body.get("enabled"), "maxSize": body.get("max_size"),
                        "maxObjects": body.get("max_objects")}
        elif uid:
            out_path = f"/api/s3/users/{enc(uid)}/buckets"
        elif name:
            out_path = f"/api/s3/buckets/{enc(name)}"
        else:
            out_path = "/api/s3/buckets"
    content = json.dumps(out_body).encode() if out_body is not None else b""
    return httpx.Request(out_method, f"http://croit.test{out_path}", content=content)
