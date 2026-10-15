"""One contract for both admin backends: `CroitClient` (mock croit API) and
`RgwAdminClient` (RGW Admin Ops mock) must behave the same for what Bucketeer calls.
RGW-specific behaviour (signing, encoding, gaps, settings, wiring) follows the contract."""

import asyncio
import json
import logging

import httpx
import pytest

from bucketeer.config import Settings, load_settings
from bucketeer.croit import CroitClient, CroitError, CroitUnavailable
from bucketeer.rgw_admin import CONCURRENCY, RgwAdminClient, RgwAdminUnsupported
from mock.croit_mock import create_app as create_croit

from .conftest import REQUIRED

pytestmark = pytest.mark.anyio

RGW_URL = "http://127.0.0.1:18082"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


def rgw_settings(admin_ops, **extra):
    return Settings(
        _env_file=None,
        **{**REQUIRED, "admin_backend": "rgw", "rgw_admin_url": RGW_URL,
           "s3_endpoint_url": "https://s3.example.org",
           "rgw_admin_access_key": admin_ops.admin.access_key,
           "rgw_admin_secret_key": admin_ops.admin.secret_key, **extra},
    )


@pytest.fixture
async def rgw(admin_ops):
    rec = Recorder(httpx.ASGITransport(app=admin_ops.app))
    client = RgwAdminClient(rgw_settings(admin_ops), transport=rec)
    client.recorder = rec
    client.state = admin_ops.state
    yield client
    await client.aclose()


@pytest.fixture(params=["croit", "rgw"])
async def backend(request, admin_ops):
    """The client under test; `.state` is the shared mock state behind it."""
    if request.param == "rgw":
        rec = Recorder(httpx.ASGITransport(app=admin_ops.app))
        client = RgwAdminClient(rgw_settings(admin_ops), transport=rec)
        client.state = admin_ops.state
    else:
        app = create_croit(seed=True, task_seconds=0, prefix="/api", admin_token="tok-contract")
        settings = Settings(_env_file=None, **{**REQUIRED, "croit_api_token": "tok-contract"})
        client = CroitClient(settings, transport=httpx.ASGITransport(app=app))
        client.state = app.state.croit
    client.kind = request.param
    yield client
    await client.aclose()


# --- contract: users ------------------------------------------------------------


async def test_create_user_exists_and_repeat(backend):
    await backend.create_user("dave", "Dave")
    user = await backend.get_user("dave")
    assert user["uid"] == "dave" and user["name"] == "Dave" and user["suspended"] is False
    assert user["email"] == ""
    for name in ("Dave", "Somebody Else"):  # a repeat is never a silent success
        with pytest.raises(CroitError) as e:
            await backend.create_user("dave", name)
        assert e.value.status == 409
    assert [k["user"] for k in await backend.list_keys("dave")] == ["dave"]  # one auto key


async def test_create_user_with_tenant_and_plus_uid(backend):
    await backend.create_user("erin", "Erin", None, "acme")
    assert (await backend.get_user("acme$erin"))["uid"] == "acme$erin"
    assert await backend.get_user("erin") is None
    await backend.create_user("alice+backup-job", "Backup")
    await backend.create_user("carol+job", "Carol job", None, "acme")
    assert (await backend.get_user("alice+backup-job"))["name"] == "Backup"
    assert (await backend.get_user("acme$carol+job"))["uid"] == "acme$carol+job"
    assert {"alice+backup-job", "acme$carol+job"} <= set(await backend.list_user_names())
    await backend.put_key("acme$carol+job", "TENANTPLUSKEY000001", "tenant-plus-secret")
    keys = await backend.list_keys("acme$carol+job")
    assert {k["user"] for k in keys} == {"acme$carol+job"}
    assert "TENANTPLUSKEY000001" in {k["access_key"] for k in keys}


async def test_get_and_list_users(backend):
    assert await backend.get_user("nobody") is None
    assert await backend.get_user("ALICE") is None  # exact uid
    assert await backend.get_user("carol") is None  # only acme$carol exists
    alice = await backend.get_user("alice")
    assert alice["name"] == "Alice Anderson" and alice["email"] == "alice@example.org"
    assert set(alice) >= {"uid", "name", "email", "suspended", "userQuota", "bucketQuota",
                          "defaultPlacement", "defaultStorageClass"}
    assert [u["uid"] for u in await backend.list_users(["bob", "alice"])] == ["bob", "alice"]
    with pytest.raises(CroitError) as e:  # an unknown uid in the filter
        await backend.list_users(["bob", "nobody"])
    assert e.value.status == 404
    everyone = {u["uid"] for u in await backend.list_users()}
    assert {"alice", "bob", "acme$carol"} <= everyone
    assert {"alice", "bob", "acme$carol"} <= set(await backend.list_user_names())


async def test_update_suspend_unsuspend(backend):
    await backend.update_user("alice", suspended=True)
    assert (await backend.get_user("alice"))["suspended"] is True
    assert (await backend.get_user("bob"))["suspended"] is False
    await backend.update_user("alice", suspended=False, name="Alice A.")
    user = await backend.get_user("alice")
    assert user["suspended"] is False and user["name"] == "Alice A."
    assert user["email"] == "alice@example.org"  # untouched
    await backend.update_user("acme$carol", suspended=True)
    assert (await backend.get_user("acme$carol"))["suspended"] is True
    with pytest.raises(CroitError) as e:
        await backend.update_user("nobody", suspended=True)
    assert e.value.status == 404


async def test_delete_user(backend):
    await backend.create_user("dave", "Dave")
    await backend.delete_user("dave")
    assert await backend.get_user("dave") is None
    with pytest.raises(CroitError) as e:
        await backend.delete_user("dave")
    assert e.value.status == 404


# --- contract: quotas -----------------------------------------------------------


async def test_user_quotas(backend):
    await backend.set_user_quota("alice", enabled=True, max_size=1000, max_objects=5)
    q = (await backend.get_user("alice"))["userQuota"]
    assert (q["enabled"], q["maxSize"], q["maxObjects"]) == (True, 1000, 5)
    await backend.set_user_quota("alice", enabled=True, max_size=0, max_objects=0)  # zero quota
    q = (await backend.get_user("alice"))["userQuota"]
    assert (q["enabled"], q["maxSize"], q["maxObjects"]) == (True, 0, 0)
    await backend.set_user_quota("alice", enabled=True, max_size=2000, max_objects=-1)
    await backend.set_user_quota("alice", enabled=False)  # keeps the stored limits
    q = (await backend.get_user("alice"))["userQuota"]
    assert q["enabled"] is False and q["maxSize"] == 2000 and q["maxObjects"] < 0
    await backend.set_user_quota("alice", enabled=True, max_objects=7)  # size kept
    q = (await backend.get_user("alice"))["userQuota"]
    assert (q["enabled"], q["maxSize"], q["maxObjects"]) == (True, 2000, 7)
    assert (await backend.get_user("bob"))["userQuota"]["enabled"] is False
    await backend.set_user_quota("acme$carol", enabled=True, max_size=5, max_objects=1)
    assert (await backend.get_user("acme$carol"))["userQuota"]["maxSize"] == 5
    with pytest.raises(CroitError) as e:
        await backend.set_user_quota("nobody", enabled=False)
    assert e.value.status == 404


async def test_user_bucket_quota_and_bucket_quota(backend):
    await backend.set_user_bucket_quota("alice", enabled=True, max_size=4096)
    q = (await backend.get_user("alice"))["bucketQuota"]
    assert (q["enabled"], q["maxSize"]) == (True, 4096)
    assert (await backend.get_user("alice"))["userQuota"]["enabled"] is False
    await backend.set_bucket_quota("alice-data", enabled=True, max_size=9000)
    await backend.set_bucket_quota("alice-data", enabled=True, max_objects=9)  # size kept
    await backend.set_bucket_quota("acme:reports", enabled=True, max_size=77)
    by_name = {b["bucket"]: b for b in await backend.list_buckets()}
    q = by_name["alice-data"]["bucket_quota"]
    assert (q["enabled"], q["maxSize"], q["maxObjects"]) == (True, 9000, 9)
    assert by_name["acme:reports"]["bucket_quota"]["maxSize"] == 77
    assert by_name["alice-empty"]["bucket_quota"]["enabled"] is False
    with pytest.raises(CroitError) as e:
        await backend.set_bucket_quota("nope-bucket", enabled=False)
    assert e.value.status == 404


# --- contract: buckets ----------------------------------------------------------


async def test_list_user_buckets_with_usage(backend):
    rows = {b["bucket"]: b for b in await backend.list_user_buckets("alice")}
    assert set(rows) == {"alice-data", "alice-empty"}
    data = rows["alice-data"]
    assert data["owner"] == "alice"
    assert data["usageSum"]["size"] == 1048576 and int(data["usageSum"]["numObjects"]) == 42
    assert rows["alice-empty"]["usageSum"]["size"] == 0
    tenant = await backend.list_user_buckets("acme$carol")
    assert [(b["bucket"], b["owner"], b["tenant"]) for b in tenant] == [
        ("acme:reports", "acme$carol", "acme")]
    with pytest.raises(CroitError) as e:
        await backend.list_user_buckets("nobody")
    assert e.value.status == 404


async def test_list_all_buckets(backend):
    rows = {b["bucket"]: b for b in await backend.list_buckets()}
    assert set(rows) == {"alice-data", "alice-empty", "bob-shared", "acme:reports"}
    acme = rows["acme:reports"]
    assert (acme["owner"], acme["tenant"]) == ("acme$carol", "acme")
    assert rows["bob-shared"]["tenant"] == ""
    assert rows["alice-data"]["usageSum"]["size"] == 1048576
    for b in rows.values():
        assert set(b) >= {"bucket", "owner", "tenant", "usageSum", "bucket_quota", "mtime"}


async def test_get_bucket_versioning(backend):
    assert (await backend.get_bucket("alice-data", owner="alice"))["versioning"] == "OFF"
    backend.state.buckets["alice-data"].versioning = "ENABLED"
    assert (await backend.get_bucket("alice-data", owner="alice"))["versioning"] == "ENABLED"
    backend.state.buckets["alice-data"].versioning = "SUSPENDED"
    assert (await backend.get_bucket("alice-data"))["versioning"] == "SUSPENDED"
    assert (await backend.get_bucket("acme:reports", owner="acme$carol"))["versioning"] == "OFF"
    with pytest.raises(CroitError) as e:
        await backend.get_bucket("no-such-bucket")
    assert e.value.status == 404


# --- contract: keys -------------------------------------------------------------


async def test_keys_get_put_delete(backend):
    before = await backend.list_keys("alice")
    assert before == [{"user": "alice", "access_key": "ALICEACCESSKEY00001",
                       "secret_key": "alice-secret-0001"}]
    await backend.put_key("alice", "NEWACCESSKEY0000001", "new-secret-1")
    keys = await backend.list_keys("alice")
    assert {k["access_key"] for k in keys} == {"ALICEACCESSKEY00001", "NEWACCESSKEY0000001"}
    assert (await backend.get_key("alice")) in keys
    await backend.put_key("alice", "NEWACCESSKEY0000001", "replaced-secret")  # replaces the secret
    new = [k for k in await backend.list_keys("alice") if k["access_key"].startswith("NEW")]
    assert [k["secret_key"] for k in new] == ["replaced-secret"]
    await backend.delete_key("alice", "ALICEACCESSKEY00001")
    assert [k["access_key"] for k in await backend.list_keys("alice")] == ["NEWACCESSKEY0000001"]


async def test_delete_missing_or_foreign_key_is_404(backend):
    with pytest.raises(CroitError) as e:
        await backend.delete_key("alice", "NOSUCHKEY0000000001")
    assert e.value.status == 404
    with pytest.raises(CroitError) as e:  # bob's key through alice
        await backend.delete_key("alice", "BOBACCESSKEY0000002")
    assert e.value.status == 404
    assert [k["access_key"] for k in await backend.list_keys("bob")] == ["BOBACCESSKEY0000002"]
    await backend.delete_key("bob", "BOBACCESSKEY0000002")
    with pytest.raises(CroitError) as e:  # already gone
        await backend.delete_key("bob", "BOBACCESSKEY0000002")
    assert e.value.status == 404


async def test_put_key_conflicts_and_unknown_user(backend):
    with pytest.raises(CroitError) as e:  # another user's access key
        await backend.put_key("alice", "BOBACCESSKEY0000002", "x-secret")
    assert e.value.status == 409
    with pytest.raises(CroitError) as e:
        await backend.put_key("nobody", "SOMEKEY00000000001", "x-secret")
    assert e.value.status == 404
    with pytest.raises(CroitError) as e:
        await backend.list_keys("nobody")
    assert e.value.status == 404


async def test_unreachable_backend_is_unavailable(backend):
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused")

    backend._http._transport = Down()
    with pytest.raises(CroitUnavailable):
        await backend.get_user("alice")


# --- RGW specifics: wire format -------------------------------------------------


def raw(req: httpx.Request) -> str:
    return req.url.raw_path.decode()


async def test_uids_with_plus_and_dollar_are_encoded_identically_signed_and_sent(rgw):
    await rgw.create_user("job+1", "Job", None, "acme")
    await rgw.put_key("acme$job+1", "PLUSKEY000000000001", "s3cr3t/+=value")
    await rgw.delete_key("acme$job+1", "PLUSKEY000000000001")
    await rgw.set_user_quota("acme$job+1", enabled=True, max_size=1, max_objects=1)
    raws = [raw(r) for r in rgw.recorder.requests]
    assert any("uid=acme%24job%2B1" in r for r in raws)
    assert all("+" not in r and "$" not in r for r in raws)
    put = next(r for r in raws if "secret-key" in r)
    assert "secret-key=s3cr3t%2F%2B%3Dvalue" in put  # nothing left to be read as a space
    # the mock verified every signature over the exact bytes sent (no 403 anywhere)
    assert (await rgw.get_user("acme$job+1"))["uid"] == "acme$job+1"


async def test_signed_headers_include_payload_hash_and_scope(rgw):
    await rgw.get_user("alice")
    req = rgw.recorder.requests[-1]
    auth = req.headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=") and "/us-east-1/s3/aws4_request" in auth
    assert "x-amz-content-sha256" in auth and "host" in auth
    assert req.headers["x-amz-content-sha256"]


async def test_create_is_exclusive(rgw):
    await rgw.create_user("dave", "Dave")
    assert "exclusive=true" in raw(rgw.recorder.requests[0])
    with pytest.raises(CroitError) as e:  # same display name: RGW would modify without it
        await rgw.create_user("dave", "Dave")
    assert e.value.status == 409 and "UserAlreadyExists" in e.value.message


async def test_quota_bodies_replace_so_the_client_merges(rgw, admin_ops):
    await rgw.set_user_quota("alice", enabled=True, max_size=500, max_objects=3)
    rgw.recorder.requests.clear()
    await rgw.set_user_quota("alice", enabled=False)
    methods = [r.method for r in rgw.recorder.requests]
    assert methods == ["GET", "PUT"]  # read the old limits, then write the whole quota
    assert admin_ops.state.users["alice"].user_quota == {
        "enabled": False, "maxSize": 500, "maxObjects": 3}


async def test_key_delete_decides_from_the_key_list_not_the_error(rgw):
    with pytest.raises(CroitError) as e:
        await rgw.delete_key("alice", "GONEKEY0000000000001")
    assert e.value.status == 404
    methods = [(r.method, "key" in raw(r)) for r in rgw.recorder.requests]
    assert methods[0] == ("DELETE", True) and methods[1][0] == "GET"


async def test_bad_admin_key_and_missing_caps(admin_ops):
    bad = RgwAdminClient(
        rgw_settings(admin_ops, rgw_admin_secret_key="wrong-secret-xyz"),
        transport=httpx.ASGITransport(app=admin_ops.app))
    with pytest.raises(CroitError) as e:
        await bad.get_user("alice")
    assert e.value.status == 403 and "SignatureDoesNotMatch" in e.value.message
    await bad.aclose()
    ro = RgwAdminClient(
        rgw_settings(admin_ops, rgw_admin_access_key=admin_ops.readonly.access_key,
                     rgw_admin_secret_key=admin_ops.readonly.secret_key),
        transport=httpx.ASGITransport(app=admin_ops.app))
    assert (await ro.get_user("alice"))["uid"] == "alice"
    with pytest.raises(CroitError) as e:
        await ro.update_user("alice", suspended=True)
    assert e.value.status == 403
    await ro.aclose()


async def test_list_users_falls_back_without_the_metadata_cap(admin_ops):
    admin_ops.app.state.admin_ops.caps[admin_ops.admin.uid] = {
        "users": frozenset({"read", "write"}), "buckets": frozenset({"read"})}
    client = RgwAdminClient(rgw_settings(admin_ops),
                            transport=httpx.ASGITransport(app=admin_ops.app))
    assert {"alice", "bob"} <= {u["uid"] for u in await client.list_users()}
    await client.aclose()


# --- RGW specifics: secrets -----------------------------------------------------


async def test_secrets_not_in_repr_logs_or_errors(rgw, admin_ops, caplog):
    caplog.set_level(logging.DEBUG)
    admin_secret = admin_ops.admin.secret_key
    await rgw.put_key("alice", "LOGKEY00000000001", "super-secret-value")
    with pytest.raises(CroitError) as e:
        await rgw.put_key("alice", "BOBACCESSKEY0000002", "another-secret-value")
    shown = " ".join([repr(rgw), str(e.value), repr(e.value), caplog.text])
    for secret in ("super-secret-value", "another-secret-value", admin_secret):
        assert secret not in shown
    assert "Signature=" not in caplog.text

    class Echo(httpx.AsyncBaseTransport):  # a server that echoes the secret into its error
        async def handle_async_request(self, request):
            q = request.url.query.decode()
            return httpx.Response(409, json={"Code": "KeyExists", "Message": q})

    echo = RgwAdminClient(rgw_settings(admin_ops), transport=Echo())
    with pytest.raises(CroitError) as e:
        await echo.put_key("alice", "ECHOKEY0000000001", "echoed-secret-value")
    assert "echoed-secret-value" not in str(e.value) + repr(e.value)
    assert "<redacted>" in str(e.value)
    await echo.aclose()


async def test_unavailable_message_has_no_query(admin_ops):
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused")

    client = RgwAdminClient(rgw_settings(admin_ops), transport=Down())
    with pytest.raises(CroitUnavailable) as e:
        await client.put_key("alice", "KEY", "leaky-secret")
    assert "leaky-secret" not in str(e.value) and "?" not in str(e.value)
    await client.aclose()


# --- RGW specifics: gaps --------------------------------------------------------


@pytest.mark.parametrize("call,item", [
    (lambda c: c.create_bucket("b", "alice"), "A3"),
    (lambda c: c.update_bucket("b", versioning="ENABLED"), "A3"),
    (lambda c: c.delete_bucket("b"), "A4"),
    (lambda c: c.delete_bucket_and_data("b", user_confirmed=True), "A4"),
    (lambda c: c.wait_for_task({"id": 1}), "A3"),
    (lambda c: c.get_task(1), "A3"),
    (lambda c: c.list_placements(), "A5"),
    (lambda c: c.cfg_file("alice"), "A5"),
])
async def test_calls_without_an_admin_ops_counterpart(rgw, call, item):
    with pytest.raises(RgwAdminUnsupported) as e:
        await call(rgw)
    assert isinstance(e.value, CroitError) and e.value.status == 501
    assert "not_supported_by_backend" in e.value.message and item in e.value.message
    assert rgw.recorder.requests == []  # nothing was sent


# --- settings and wiring --------------------------------------------------------

RGW_ENV = {"admin_backend": "rgw", "rgw_admin_url": "https://rgw.example.org",
           "rgw_admin_access_key": "ADMINKEY", "rgw_admin_secret_key": "admin-secret",
           "s3_endpoint_url": "https://s3.example.org"}
NO_CROIT = {k: v for k, v in REQUIRED.items()
            if not k.startswith("croit") and k != "admin_backend"}


def test_rgw_backend_needs_its_settings_and_not_croit():
    s = Settings(_env_file=None, **NO_CROIT, **RGW_ENV)
    assert s.admin_backend == "rgw" and s.croit_url == ""
    for name in ("rgw_admin_url", "rgw_admin_access_key", "rgw_admin_secret_key",
                 "s3_endpoint_url"):
        with pytest.raises(ValueError) as e:
            Settings(_env_file=None, **NO_CROIT, **{**RGW_ENV, name: ""})
        assert name.upper() in str(e.value)


def test_rgw_backend_requires_s3_endpoint_croit_does_not():
    env = {k: v for k, v in RGW_ENV.items() if k != "s3_endpoint_url"}
    with pytest.raises(ValueError, match="S3_ENDPOINT_URL: REQUIRED when ADMIN_BACKEND=rgw"):
        Settings(_env_file=None, **NO_CROIT, **env)
    assert Settings(_env_file=None, **REQUIRED).s3_endpoint_url == ""  # croit: optional


def test_croit_backend_needs_croit_and_ignores_rgw():
    assert Settings(_env_file=None, **REQUIRED).admin_backend == "croit"
    for name in ("croit_url", "croit_api_token"):
        with pytest.raises(ValueError) as e:
            Settings(_env_file=None, **{**REQUIRED, name: ""})
        assert name.upper() in str(e.value)
    # a broken RGW_ADMIN_URL is ignored while the backend is croit
    assert Settings(_env_file=None, **REQUIRED, rgw_admin_url="ftp://x y").admin_backend == "croit"


def test_default_backend_is_rgw_and_never_falls_back_to_croit():
    """Phase 5 A7: with no ADMIN_BACKEND the rgw settings are required, croit ones don't help."""
    s = Settings(_env_file=None, **NO_CROIT, **{k: v for k, v in RGW_ENV.items()
                                                if k != "admin_backend"})
    assert s.admin_backend == "rgw"
    with pytest.raises(ValueError, match="RGW_ADMIN_URL.*REQUIRED when ADMIN_BACKEND=rgw"):
        Settings(_env_file=None, **{k: v for k, v in REQUIRED.items() if k != "admin_backend"})


def test_unknown_backend_refused():
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{**REQUIRED, "admin_backend": "both"})


@pytest.mark.parametrize("url,ok", [
    ("https://rgw.example.org", True),
    ("https://rgw.example.org:7480/", True),
    ("http://127.0.0.1:7480", True),
    ("http://localhost:7480", True),
    ("http://[::1]:7480", False),  # not matched by the URL alphabet; use localhost
    ("http://rgw.example.org", False),
    ("http://10.0.0.5:7480", False),
    ("ftp://rgw.example.org", False),
    ("https://user:pw@rgw.example.org", False),
    ("https://rgw.example.org/a b", False),
])
def test_rgw_admin_url_rules(url, ok):
    if ok:
        s = Settings(_env_file=None, **NO_CROIT, **{**RGW_ENV, "rgw_admin_url": url})
        assert not s.rgw_admin_url.endswith("/")
    else:
        with pytest.raises(ValueError):
            Settings(_env_file=None, **NO_CROIT, **{**RGW_ENV, "rgw_admin_url": url})


def test_rgw_secret_not_in_settings_repr():
    s = Settings(_env_file=None, **NO_CROIT, **RGW_ENV)
    assert "admin-secret" not in repr(s) + str(s)


def test_startup_message_names_the_missing_backend_settings(monkeypatch, tmp_path):
    # through the environment (monkeypatch undoes it): load_dotenv would leak into later tests
    for k, v in NO_CROIT.items():
        monkeypatch.setenv(k.upper(), v)
    monkeypatch.setenv("ADMIN_BACKEND", "rgw")
    monkeypatch.chdir(tmp_path)  # no .env here
    with pytest.raises(SystemExit) as e:
        load_settings()
    for name in ("RGW_ADMIN_URL", "RGW_ADMIN_ACCESS_KEY", "RGW_ADMIN_SECRET_KEY",
                 "S3_ENDPOINT_URL"):
        assert name in str(e.value)
    assert "CROIT_URL" not in str(e.value)


def test_create_app_builds_the_chosen_client(admin_ops, tmp_path):
    from fastapi.testclient import TestClient

    from bucketeer.main import create_app

    rgw_s = rgw_settings(admin_ops, data_dir=str(tmp_path / "d1"))
    with TestClient(create_app(rgw_s)) as c:
        assert isinstance(c.app.state.croit, RgwAdminClient)
        assert c.app.state.rgw.croit is c.app.state.croit
        client = c.app.state.croit
    assert client.closed
    croit_s = Settings(_env_file=None, data_dir=str(tmp_path / "d2"), **REQUIRED)
    with TestClient(create_app(croit_s)) as c:
        assert isinstance(c.app.state.croit, CroitClient)


# --- review fixes: cancellation, service user, 404s, details ---------------------


class Counting(httpx.AsyncBaseTransport):
    """Wraps a transport, counts per-user reads and can fail the n-th one."""

    def __init__(self, inner, fail_at=None):
        self.inner, self.fail_at, self.reads = inner, fail_at, 0

    async def handle_async_request(self, request):
        if request.url.path == "/admin/user" and b"uid=" in request.url.query and \
                request.method == "GET":
            self.reads += 1
            mine = self.reads
            await asyncio.sleep(0.005)  # let the other reads queue up behind the semaphore
            if mine == self.fail_at:
                raise httpx.ReadTimeout("slow")
        return await self.inner.handle_async_request(request)


async def test_list_users_stops_reading_after_a_failure(admin_ops):
    for i in range(200):
        admin_ops.state.create_user(f"u{i:03d}", "U")
    transport = Counting(httpx.ASGITransport(app=admin_ops.app), fail_at=3)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    with pytest.raises(CroitUnavailable):
        await client.list_users()
    assert transport.reads <= 3 + CONCURRENCY  # in flight when it failed; none after
    after = transport.reads
    await asyncio.sleep(0.1)
    assert transport.reads == after  # nothing is still running in the background
    await client.aclose()


async def test_list_users_reads_everything_when_nothing_fails(admin_ops):
    for i in range(30):
        admin_ops.state.create_user(f"u{i:03d}", "U")
    transport = Counting(httpx.ASGITransport(app=admin_ops.app))
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    uids = {u["uid"] for u in await client.list_users()}
    assert {f"u{i:03d}" for i in range(30)} <= uids and "bucketeer-admin" not in uids
    await client.aclose()


async def test_service_user_is_identified_and_hidden(rgw, admin_ops):
    assert await rgw.service_uid() == "bucketeer-admin"
    admin_ops.state.create_bucket("svc-bucket", "bucketeer-admin")
    assert "bucketeer-admin" not in await rgw.list_user_names()
    assert "bucketeer-admin" not in {u["uid"] for u in await rgw.list_users()}
    assert "svc-bucket" not in {b["bucket"] for b in await rgw.list_buckets()}
    assert "bucketeer-readonly" in await rgw.list_user_names()  # only the service user hides
    assert (await rgw.get_user("bucketeer-admin"))["uid"] == "bucketeer-admin"
    n = len(rgw.recorder.requests)
    await rgw.service_uid()
    assert len(rgw.recorder.requests) == n  # cached


async def test_service_uid_lookup_failure_fails_closed_and_recovers(admin_ops):
    state = {"down": True}

    class Flaky(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if state["down"] and b"access-key=" in request.url.query:
                raise httpx.ConnectError("down")
            return await httpx.ASGITransport(app=admin_ops.app).handle_async_request(request)

    client = RgwAdminClient(rgw_settings(admin_ops), transport=Flaky())
    for call in (client.service_uid, client.list_users, client.list_buckets):
        with pytest.raises(CroitUnavailable):
            await call()
    state["down"] = False
    assert await client.service_uid() == "bucketeer-admin"
    await client.aclose()


async def test_a_404_that_is_not_no_such_user_is_not_user_gone(admin_ops):
    class WrongPath(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(404, json={"Code": "NoSuchKey", "Message": ""})

    client = RgwAdminClient(rgw_settings(admin_ops), transport=WrongPath())
    for call in (lambda: client.get_user("alice"), lambda: client.delete_user("alice"),
                 lambda: client.list_keys("alice"), lambda: client.get_bucket("alice-data")):
        with pytest.raises(CroitError) as e:
            await call()
        assert e.value.status == 502 and "RGW_ADMIN_URL" in e.value.message
    await client.aclose()


async def test_real_no_such_user_is_still_404(rgw):
    assert await rgw.get_user("nobody") is None
    with pytest.raises(CroitError) as e:
        await rgw.delete_user("nobody")
    assert e.value.status == 404


async def test_get_bucket_asks_with_owner_and_bare_name(rgw):
    assert (await rgw.get_bucket("acme:reports", owner="acme$carol"))["versioning"] == "OFF"
    query = rgw.recorder.requests[-1].url.query.decode()
    assert "uid=acme%24carol" in query and "bucket=reports" in query and "stats=true" in query
    with pytest.raises(CroitError) as e:  # someone else's bucket
        await rgw.get_bucket("alice-data", owner="bob")
    assert e.value.status == 404


async def test_quota_put_carries_check_on_raw_and_always_reads_first(admin_ops):
    seen = []

    class Quota(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append((request.method, request.content))
            if request.method == "GET":
                return httpx.Response(200, json={
                    "enabled": False, "check_on_raw": True, "max_size": 5, "max_objects": 6})
            return httpx.Response(200)

    client = RgwAdminClient(rgw_settings(admin_ops), transport=Quota())
    await client.set_user_quota("alice", enabled=True, max_size=100, max_objects=200)
    assert [m for m, _ in seen] == ["GET", "PUT"]  # read even though both limits were given
    assert json.loads(seen[1][1]) == {
        "enabled": True, "max_size": 100, "max_objects": 200, "check_on_raw": True}
    await client.aclose()


def test_timeout_setting_and_no_mock_header_in_production_code(admin_ops):
    from pathlib import Path

    import bucketeer.rgw_admin as module

    client = RgwAdminClient(rgw_settings(admin_ops, rgw_admin_timeout=7, croit_timeout=3))
    assert client._http.timeout.read == 7
    assert "x-mock" not in Path(module.__file__).read_text().lower()
    assert Settings(_env_file=None, **REQUIRED).rgw_admin_timeout == 15


def test_user_s3_clients_honour_the_tls_settings(tmp_path):
    from bucketeer.s3client import default_factory, default_sns_factory, tls_verify

    ca = str(tmp_path / "internal-ca.pem")
    cases = [({}, True), ({"s3_tls_verify": False}, False), ({"s3_ca_cert_file": ca}, ca),
             ({"s3_tls_verify": False, "s3_ca_cert_file": ca}, False)]
    for extra, expected in cases:
        s = Settings(_env_file=None, **REQUIRED, **extra)
        assert tls_verify(s) == expected
        for factory in (default_factory, default_sns_factory):
            client = factory("https://s3.example.org", "AK", "SK", s)
            assert client._endpoint.http_session._verify == expected
