"""bucketeer.croit.CroitClient against the in-process mock croit API."""

import json
import logging
import ssl

import httpx
import pytest
from fastapi.testclient import TestClient

from bucketeer.config import Settings
from bucketeer.croit import (
    CroitClient,
    CroitError,
    CroitTaskFailed,
    CroitTaskTimeout,
    CroitUnavailable,
    CroitWarning,
    _path,
)
from bucketeer.main import create_app as create_bucketeer_app
from mock.croit_mock import MockHooks, create_app

from .conftest import REQUIRED

ADMIN_TOKEN = "test-admin-token-aaaa"
VIEWER_TOKEN = "test-viewer-token-bbbb"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Recorder(httpx.AsyncBaseTransport):
    """Wraps another transport and remembers the requests it saw."""

    def __init__(self, inner):
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self.inner.handle_async_request(request)

    @property
    def paths(self):
        return [r.url.raw_path.decode() for r in self.requests]


def make_settings(token=ADMIN_TOKEN, **extra):
    values = {**REQUIRED, "croit_api_token": token, **extra}
    return Settings(_env_file=None, **values)


@pytest.fixture
def mock_app():
    return create_app(
        seed=True, task_seconds=0.05, prefix="/api",
        admin_token=ADMIN_TOKEN, viewer_token=VIEWER_TOKEN,
    )


@pytest.fixture
def recorder(mock_app):
    return Recorder(httpx.ASGITransport(app=mock_app))


@pytest.fixture
async def croit(recorder):
    client = CroitClient(make_settings(), poll_interval=0.01, transport=recorder)
    yield client
    await client.aclose()


# --- auth, errors, transport ---------------------------------------------------


async def test_bearer_header_sent_and_token_info(croit, recorder):
    info = await croit.token_info()
    assert info["username"] == "mock-admin" and "admin" in info["roles"]
    assert recorder.requests[0].headers["authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert recorder.paths == ["/api/auth/token-info"]


async def test_viewer_token_can_read_but_not_mutate(recorder):
    client = CroitClient(make_settings(VIEWER_TOKEN), transport=recorder)
    try:
        assert len(await client.list_users()) == 3
        with pytest.raises(CroitError) as exc:
            await client.create_user("dave", "Dave")
        assert exc.value.status == 403
        assert exc.value.code == 403
        assert "admin role" in exc.value.message
    finally:
        await client.aclose()


async def test_bad_token_is_401(recorder):
    client = CroitClient(make_settings("nope-nope"), transport=recorder)
    try:
        with pytest.raises(CroitError) as exc:
            await client.token_info()
        assert exc.value.status == 401
        assert "nope-nope" not in str(exc.value) + repr(exc.value)
    finally:
        await client.aclose()


async def test_error_message_parsing_with_details(croit):
    await croit.create_user("dave", "Dave", "dave@example.org")
    with pytest.raises(CroitError) as exc:
        await croit.create_user("dave", "Dave again")
    assert (exc.value.status, exc.value.code) == (409, 409)
    assert "already exists" in exc.value.message
    with pytest.raises(CroitError) as exc:
        await croit.delete_user("alice")  # still owns buckets
    assert exc.value.status == 409
    assert "alice-data" in exc.value.details
    assert "alice-data" in str(exc.value)


async def test_non_json_error_body():
    def handler(request):
        return httpx.Response(502, text="<html>Bad gateway</html>")

    client = CroitClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CroitError) as exc:
            await client.token_info()
        assert exc.value.status == 502 and exc.value.code is None
        assert "Bad gateway" in exc.value.message
    finally:
        await client.aclose()


async def test_server_echoing_the_api_token_is_scrubbed():
    def handler(request):
        auth = request.headers["authorization"]
        return httpx.Response(401, json={"code": 401, "message": f"bad header {auth}"})

    client = CroitClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CroitError) as exc:
            await client.token_info()
        assert ADMIN_TOKEN not in str(exc.value) + repr(exc.value) + exc.value.message
    finally:
        await client.aclose()


async def test_connection_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = CroitClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CroitUnavailable) as exc:
            await client.list_buckets()
        assert not isinstance(exc.value, CroitError)
        assert ADMIN_TOKEN not in str(exc.value) + repr(exc.value)
    finally:
        await client.aclose()


async def test_real_refused_connection_is_unavailable():
    # nothing listens on port 9 of localhost
    settings = make_settings(croit_url="http://127.0.0.1:9/api", croit_timeout=2)
    client = CroitClient(settings)
    try:
        with pytest.raises(CroitUnavailable):
            await client.token_info()
    finally:
        await client.aclose()


async def test_timeout_is_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = CroitClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CroitUnavailable, match="timed out"):
            await client.token_info()
    finally:
        await client.aclose()


def test_tls_settings(tmp_path):
    # default: system store; disabled: no verification; CA file: own context
    cases = ((make_settings(), True), (make_settings(croit_tls_verify=False), False))
    for settings, expected in cases:
        client = CroitClient(settings)
        assert client._http._transport._pool._ssl_context.verify_mode == (
            ssl.CERT_REQUIRED if expected else ssl.CERT_NONE
        )
    bad = make_settings(croit_ca_cert_file=str(tmp_path / "missing.pem"))
    with pytest.raises(OSError):
        CroitClient(bad)


def test_timeout_setting():
    client = CroitClient(make_settings(croit_timeout=7))
    assert client._http.timeout.read == 7


def test_secrets_not_in_client_repr():
    assert ADMIN_TOKEN not in repr(CroitClient(make_settings()))


# --- path encoding -------------------------------------------------------------


def test_path_helper_encodes_every_parameter():
    assert _path("/s3/users/{uid}", uid="acme$carol") == "/s3/users/acme%24carol"
    assert _path("/s3/users/{uid}", uid="a/b") == "/s3/users/a%2Fb"
    assert _path("/s3/buckets/{n}", n="acme:reports") == "/s3/buckets/acme%3Areports"
    assert _path("/s3/users/{u}/keys/{k}", u="x", k="../y") == "/s3/users/x/keys/..%2Fy"


async def test_tenant_uid_is_encoded_on_the_wire(croit, recorder):
    keys = await croit.list_keys("acme$carol")
    assert keys[0]["user"] == "acme$carol"
    assert recorder.paths == ["/api/s3/users/acme%24carol/keys"]


async def test_slash_in_uid_cannot_add_path_segments(croit, recorder):
    with pytest.raises(CroitError):
        await croit.get_key("alice/keys/../../..")
    with pytest.raises(CroitError):
        await croit.delete_key("alice", "../../x")
    assert recorder.paths == [
        "/api/s3/users/alice%2Fkeys%2F..%2F..%2F../key",
        "/api/s3/users/alice/keys/..%2F..%2Fx",
    ]


async def test_tenant_bucket_name_encoded(croit, recorder):
    details = await croit.get_bucket("acme:reports", owner="acme$carol")
    assert details["versioning"] == "OFF"
    assert recorder.paths == ["/api/s3/buckets/acme%3Areports?owner=acme%24carol"]


class NeverCalled(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        pytest.fail(f"request sent: {request.method} {request.url}")


@pytest.mark.parametrize("call", [
    lambda c: c.delete_key("alice", ".."),
    lambda c: c.delete_key("alice", "."),
    lambda c: c.delete_key("..", "KEY"),
    lambda c: c.get_key(".."),
    lambda c: c.delete_bucket("."),
    lambda c: c.delete_bucket_policy(".."),
    lambda c: c.get_bucket(""),
    lambda c: c.list_keys(""),
    lambda c: c.get_task(".."),
])
async def test_dot_segments_and_empty_params_rejected_before_request(call):
    client = CroitClient(make_settings(), transport=NeverCalled())
    try:
        with pytest.raises(ValueError):
            await call(client)
    finally:
        await client.aclose()


# --- users, keys, quotas -------------------------------------------------------


async def test_user_lifecycle(croit):
    await croit.create_user("dave", "Dave", "dave@example.org")
    await croit.create_user("erin", "Erin", tenant="acme")
    user = await croit.get_user("dave")
    assert user["name"] == "Dave" and user["suspended"] is False
    assert (await croit.get_user("acme$erin"))["uid"] == "acme$erin"
    await croit.update_user("dave", suspended=True, name="David")
    user = await croit.get_user("dave")
    assert user["suspended"] is True and user["name"] == "David"
    assert [u["uid"] for u in await croit.list_users(["dave", "alice"])] == ["dave", "alice"]
    await croit.delete_user("dave")
    assert await croit.get_user("dave") is None


async def test_get_user_requires_an_exact_uid_and_tenant_match(croit, monkeypatch):
    assert await croit.get_user("ALICE") is None
    assert await croit.get_user("carol") is None  # only acme$carol exists

    async def sloppy(uids=None):  # croit answering with other users than asked for
        return [{"uid": "bob"}, {"uid": "acme$carol"}]

    monkeypatch.setattr(croit, "list_users", sloppy)
    assert await croit.get_user("alice") is None
    assert await croit.get_user("carol") is None
    assert (await croit.get_user("acme$carol"))["uid"] == "acme$carol"
    assert (await croit.get_user("bob"))["uid"] == "bob"


async def test_key_add_rotate_delete_without_whole_set_put(croit, recorder):
    before = await croit.list_keys("alice")
    assert len(before) == 1
    await croit.put_key("alice", "NEWACCESSKEY0000001", "new-secret-1")
    keys = await croit.list_keys("alice")
    assert {k["access_key"] for k in keys} == {before[0]["access_key"], "NEWACCESSKEY0000001"}
    await croit.delete_key("alice", before[0]["access_key"])
    keys = await croit.list_keys("alice")
    assert [k["access_key"] for k in keys] == ["NEWACCESSKEY0000001"]
    assert (await croit.get_key("alice"))["secret_key"] == "new-secret-1"
    # no request ever used PUT on the whole key set
    assert not any(r.method == "PUT" and r.url.path.endswith("/keys") for r in recorder.requests)
    assert not hasattr(croit, "put_keys") and not hasattr(croit, "update_keys")


async def test_put_key_secret_goes_in_query_and_not_in_logs(croit, recorder, caplog):
    caplog.set_level(logging.DEBUG)
    await croit.put_key("alice", "LOGKEY00000000001", "super-secret-value")
    assert recorder.requests[0].url.params["secretKey"] == "super-secret-value"
    assert "super-secret-value" not in caplog.text
    assert ADMIN_TOKEN not in caplog.text


async def test_quotas(croit, mock_app):
    await croit.set_user_quota("alice", enabled=True, max_size=1000, max_objects=5)
    await croit.set_user_bucket_quota("alice", enabled=True, max_size=-1)
    await croit.set_bucket_quota("alice-data", enabled=True, max_objects=9)
    users = mock_app.state.croit.users
    assert users["alice"].user_quota == {"enabled": True, "maxSize": 1000, "maxObjects": 5}
    assert users["alice"].bucket_quota["enabled"] is True
    assert mock_app.state.croit.buckets["alice-data"].quota["maxObjects"] == 9
    listed = (await croit.list_users(["alice"]))[0]
    assert listed["userQuota"]["maxSize"] == 1000


async def test_cfg_file(croit):
    cfg = await croit.cfg_file("alice")
    assert "host_base" in cfg["fileContents"]


# --- tasks and buckets ---------------------------------------------------------


async def test_create_bucket_and_wait_for_task(croit, mock_app):
    task = await croit.create_bucket(
        "dave-new", "alice", versioning="ENABLED", placement="default-placement"
    )
    assert task["done"] is False and task["state"] == "RUNNING"
    done = await croit.wait_for_task(task, timeout=5)
    assert done["state"] == "FINISHED" and done["statusCode"] == 0
    bucket = mock_app.state.croit.buckets["dave-new"]
    assert bucket.versioning == "ENABLED" and bucket.owner == "alice"
    assert (await croit.get_bucket("dave-new"))["versioning"] == "ENABLED"
    assert "dave-new" in {b["bucket"] for b in await croit.list_buckets()}
    assert "dave-new" in {b["bucket"] for b in await croit.list_user_buckets("alice")}
    assert (await croit.wait_for_task(task["id"]))["done"] is True  # by id too


async def test_tenant_bucket_create_uses_owner_tenant(croit, mock_app):
    await croit.wait_for_task(await croit.create_bucket("tbucket", "acme$carol", object_lock=True))
    assert mock_app.state.croit.buckets["acme:tbucket"].object_lock is True


async def test_update_bucket(croit, mock_app):
    await croit.update_bucket("alice-empty", versioning="ENABLED")
    assert mock_app.state.croit.buckets["alice-empty"].versioning == "ENABLED"
    with pytest.raises(CroitError) as exc:
        await croit.update_bucket("alice-empty", acl=[])
    assert exc.value.status == 400


async def test_task_failure_raises():
    answers = iter([
        {"id": 7, "done": False, "state": "RUNNING", "statusCode": -1, "output": ""},
        {"id": 7, "done": True, "state": "FAILED", "statusCode": 2, "name": "Create",
         "output": "x" * 5000 + "the end"},
    ])
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=next(answers))

    transport = httpx.MockTransport(handler)
    client = CroitClient(make_settings(), poll_interval=0.01, transport=transport)
    try:
        with pytest.raises(CroitTaskFailed) as exc:
            await client.wait_for_task(7, timeout=5)
        err = exc.value
        assert (err.state, err.status_code, err.task_id) == ("FAILED", 2, 7)
        assert err.output_tail.endswith("the end") and len(err.output_tail) <= 500
        assert len(str(err)) < 200
        assert seen == ["/api/tasks/7", "/api/tasks/7"]
    finally:
        await client.aclose()


async def test_task_done_but_cancelled_or_nonzero_is_failure():
    def handler(request):
        return httpx.Response(200, json={"id": 1, "done": True, "state": "FINISHED",
                                         "statusCode": 3, "output": ""})

    client = CroitClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CroitTaskFailed):
            await client.wait_for_task(1)
    finally:
        await client.aclose()


async def test_task_timeout():
    def handler(request):
        return httpx.Response(200, json={"id": 3, "done": False, "state": "RUNNING",
                                         "statusCode": -1, "output": ""})

    transport = httpx.MockTransport(handler)
    client = CroitClient(make_settings(), poll_interval=0.01, transport=transport)
    try:
        with pytest.raises(CroitTaskTimeout) as exc:
            await client.wait_for_task(3, timeout=0.1)
        assert exc.value.task_id == 3
    finally:
        await client.aclose()


async def test_unknown_task_is_404(croit):
    with pytest.raises(CroitError) as exc:
        await croit.wait_for_task(9999)
    assert exc.value.status == 404


# --- bucket delete handshake ---------------------------------------------------


async def test_delete_empty_bucket_needs_no_warning(croit, mock_app):
    task = await croit.delete_bucket("alice-empty")
    await croit.wait_for_task(task)
    assert "alice-empty" not in mock_app.state.croit.buckets


async def test_delete_non_empty_bucket_raises_warning(croit, mock_app):
    with pytest.raises(CroitWarning) as exc:
        await croit.delete_bucket("alice-data")
    w = exc.value
    assert w.status == 400 and w.affirmative_label == "Delete"
    assert w.token and w.warning_text
    assert w.token not in str(w) + repr(w) + w.message
    assert "alice-data" in mock_app.state.croit.buckets
    assert isinstance(w, CroitError)


async def test_manual_handshake_with_token(croit, mock_app):
    with pytest.raises(CroitWarning) as exc:
        await croit.delete_bucket("alice-data")
    w = exc.value
    sentence = mock_app.state.croit.delete_sentence("alice-data")
    # a wrong sentence is refused (and issues a fresh warning)
    with pytest.raises(CroitWarning):
        await croit.delete_bucket("alice-data", delete_data=True, confirmation="yes",
                                  ignore_warning=w.token)
    with pytest.raises(CroitWarning) as exc2:
        await croit.delete_bucket("alice-data")
    task = await croit.delete_bucket("alice-data", delete_data=True, confirmation=sentence,
                                     ignore_warning=exc2.value.token)
    await croit.wait_for_task(task)
    assert "alice-data" not in mock_app.state.croit.buckets


async def test_full_handshake_helper(croit, recorder, mock_app):
    task = await croit.delete_bucket_and_data("alice-data", user_confirmed=True)
    await croit.wait_for_task(task)
    assert "alice-data" not in mock_app.state.croit.buckets
    deletes = [r for r in recorder.requests if r.method == "DELETE"]
    assert len(deletes) == 2
    assert "ignoreWarning" not in deletes[0].url.params
    assert "ignoreWarning" in deletes[1].url.params
    body = json.loads(deletes[1].content)
    assert body["deleteData"] is True and body["name"] == "alice-data"
    assert body["thisWillDeleteData"].endswith("alice-data")


async def test_handshake_helper_on_empty_bucket_is_one_call(croit, recorder):
    await croit.delete_bucket_and_data("alice-empty", user_confirmed=True)
    assert len([r for r in recorder.requests if r.method == "DELETE"]) == 1


@pytest.mark.parametrize("flag", [False, None, "yes", 1])
async def test_handshake_helper_requires_explicit_confirmation(croit, recorder, flag):
    with pytest.raises(ValueError):
        await croit.delete_bucket_and_data("alice-data", user_confirmed=flag)
    assert recorder.requests == []


async def test_handshake_sentence_parsed_from_text_when_details_missing():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            text = 'not empty; send thisWillDeleteData="type this" and ignoreWarning=tok-1'
            return httpx.Response(400, json={
                "code": 400, "message": text,
                "ignorableWarning": {"warningText": text, "token": "tok-1",
                                     "affirmativeLabel": "Delete"}})
        return httpx.Response(200, json={"id": 1, "done": True, "state": "FINISHED",
                                         "statusCode": 0})

    client = CroitClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        await client.delete_bucket_and_data("b", user_confirmed=True)
        assert json.loads(seen[1].content)["thisWillDeleteData"] == "type this"
        assert seen[1].url.params["ignoreWarning"] == "tok-1"
    finally:
        await client.aclose()


# --- policy and usage ----------------------------------------------------------


async def test_bucket_policy_roundtrip(croit):
    assert await croit.get_bucket_policy("alice-data") is None
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "bucketeer-share-bob", "Effect": "Allow",
         "Principal": {"AWS": ["arn:aws:iam:::user/bob"]},
         "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::alice-data/*"]}]}
    await croit.put_bucket_policy("alice-data", policy, owner="alice")
    assert await croit.get_bucket_policy("alice-data") == policy
    await croit.put_bucket_policy("alice-data", json.dumps(policy))
    await croit.delete_bucket_policy("alice-data")
    assert await croit.get_bucket_policy("alice-data") is None


async def test_usage(croit):
    by_user = await croit.usage_by_user("alice", 24)
    assert "alice-data" in by_user["buckets"]
    by_bucket = await croit.usage_by_bucket("acme:reports", 24)
    assert by_bucket["buckets"]["acme$carol"][0]["bucketName"] == "acme:reports"


# --- moto bridge ---------------------------------------------------------------


async def test_client_against_mock_with_moto_bridge(s3_endpoint):
    from mock.s3_bridge import S3Bridge

    bridge = S3Bridge(s3_endpoint)
    app = create_app(seed=True, task_seconds=0, prefix="/api", admin_token=ADMIN_TOKEN,
                     hooks=bridge.make_hooks(MockHooks), rgw_endpoint=bridge.host_port)
    bridge.seed_objects(app.state.croit)
    client = CroitClient(make_settings(), poll_interval=0.01,
                         transport=httpx.ASGITransport(app=app))
    try:
        await client.wait_for_task(await client.create_bucket("viaclient", "alice"))
        key = (await client.list_keys("alice"))[0]
        s3 = bridge.client(key["access_key"], key["secret_key"])
        assert "viaclient" in {b["Name"] for b in s3.list_buckets()["Buckets"]}
        await client.wait_for_task(
            await client.delete_bucket_and_data("alice-data", user_confirmed=True))
        assert "alice-data" not in {b["Name"] for b in s3.list_buckets()["Buckets"]}
    finally:
        await client.aclose()


# --- app lifecycle -------------------------------------------------------------


def test_app_owns_one_client_and_closes_it_on_shutdown(tmp_path):
    settings = make_settings(data_dir=str(tmp_path / "data"))
    app = create_bucketeer_app(settings)
    client = app.state.croit
    assert isinstance(client, CroitClient) and not client.closed
    with TestClient(app) as tc:
        assert tc.get("/health").status_code == 200
        assert app.state.croit is client and not client.closed
    assert client.closed
