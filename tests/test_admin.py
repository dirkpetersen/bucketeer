"""/api/admin against the in-process mock croit, with fake-LDAP sessions."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bucketeer.croit import CroitClient
from bucketeer.db import Database
from bucketeer.ldapauth import AdAuthenticator
from bucketeer.main import create_app
from mock.croit_mock import create_app as create_mock

from .conftest import REQUIRED
from .test_auth import SECRET_PW, FakeLdap, make_settings

H = {"X-Requested-With": "bucketeer"}
TOKEN = REQUIRED["croit_api_token"]


class Transport(httpx.AsyncBaseTransport):
    """ASGI transport to the mock that records calls and can inject failures."""

    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.calls: list[tuple[str, str]] = []
        self.fail = None  # callable(request) -> httpx.Response | Exception | None

    async def handle_async_request(self, request):
        self.calls.append((request.method, request.url.raw_path.decode()))
        if self.fail and (out := self.fail(request)) is not None:
            if isinstance(out, Exception):
                raise out
            return out
        return await self.inner.handle_async_request(request)

    def mutations(self):
        return [c for c in self.calls if c[0] != "GET"]


class Env:
    def __init__(self, tmp_path):
        self.mock = create_mock(seed=True, task_seconds=0, prefix="/api", admin_token=TOKEN)
        self.transport = Transport(self.mock)
        settings = make_settings(tmp_path)
        croit = CroitClient(settings, poll_interval=0.01, transport=self.transport)
        self.db = Database(":memory:")
        self.fake = FakeLdap()
        self.app = create_app(
            settings, croit=croit, db=self.db,
            authenticator=AdAuthenticator(settings, self.fake),
        )
        self.anon = TestClient(self.app, base_url="https://testserver")
        self.admin = self.session("bob")  # bob is in the admin group
        self.user = self.session("alice")

    def session(self, login):
        c = TestClient(self.app, base_url="https://testserver")
        r = c.post("/api/login", json={"login": login, "password": SECRET_PW}, headers=H)
        assert r.status_code == 200
        return c

    def audit(self, prefix="admin."):
        return [a for a in self.db.audit.query(limit=1000) if a["action"].startswith(prefix)]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


ROUTES = [
    ("GET", "/api/admin/users", None),
    ("GET", "/api/admin/buckets", None),
    ("PUT", "/api/admin/users/alice/quota", {"enabled": False}),
    ("PUT", "/api/admin/buckets/alice-data/quota", {"enabled": False}),
    ("POST", "/api/admin/users/alice/suspend", None),
    ("POST", "/api/admin/users/alice/unsuspend", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_gating(env, method, path, body):
    call = lambda c, **kw: c.request(method, path, json=body, **kw)  # noqa: E731
    assert call(env.anon, headers=H).status_code == 401
    assert call(env.user, headers=H).status_code == 403
    assert env.transport.calls == []  # nothing reached croit
    assert env.audit() == []


@pytest.mark.parametrize(("method", "path", "body"), [r for r in ROUTES if r[0] != "GET"])
def test_csrf_header_required_on_mutations(env, method, path, body):
    r = env.admin.request(method, path, json=body)
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    assert env.transport.mutations() == []


# -- lists ---------------------------------------------------------------------


def test_users_list_shape_and_no_secrets(env):
    env.db.bucket_keys.create("alice.job", "alice", "alice-data", "read")
    r = env.admin.get("/api/admin/users")
    assert r.status_code == 200
    users = {u["uid"]: u for u in r.json()["users"]}
    assert set(users) == {"alice", "bob", "acme$carol"}
    a = users["alice"]
    assert a == {
        "uid": "alice", "name": "Alice Anderson", "email": "alice@example.org", "tenant": "",
        "suspended": False, "quota": {"enabled": False, "maxSize": None, "maxObjects": None},
        "bucketQuota": {"enabled": False, "maxSize": None, "maxObjects": None},
        "bucketCount": 2, "bucketKey": None,
    }
    assert users["acme$carol"]["tenant"] == "acme" and users["acme$carol"]["bucketCount"] == 1
    text = r.text.lower()
    assert "secret" not in text and "access_key" not in text and "accesskey" not in text


def test_users_list_marks_bucket_key_users(env):
    env.db.bucket_keys.create("bob", "alice", "alice-data", "read")  # a live key record
    bob = next(u for u in env.admin.get("/api/admin/users").json()["users"] if u["uid"] == "bob")
    assert bob["bucketKey"] == {"owner": "alice", "bucket": "alice-data", "access": "read"}


def test_buckets_list_joins_budget_number(env):
    env.db.buckets.create("alice-data", "alice", "IDX-1234")
    r = env.admin.get("/api/admin/buckets")
    assert r.status_code == 200
    b = {x["name"]: x for x in r.json()["buckets"]}
    assert set(b) == {"alice-data", "alice-empty", "bob-shared", "acme:reports"}
    assert b["alice-data"] == {
        "name": "alice-data", "tenant": "", "owner": "alice", "objects": 42, "bytes": 1048576,
        "quota": {"enabled": False, "maxSize": None, "maxObjects": None},
        "budgetNumber": "IDX-1234", "budgetMatches": None,
    }
    assert b["alice-empty"]["budgetNumber"] is None  # missing, not invalid
    assert b["acme:reports"]["tenant"] == "acme" and b["acme:reports"]["owner"] == "acme$carol"
    assert "secret" not in r.text.lower()


# -- quotas --------------------------------------------------------------------


def test_set_user_quota(env):
    body = {"enabled": True, "maxSize": 5 * 1024**3, "maxObjects": None}
    r = env.admin.put("/api/admin/users/alice/quota", json=body, headers=H)
    assert r.status_code == 200
    assert r.json() == {
        "uid": "alice", "quota": {"enabled": True, "maxSize": 5 * 1024**3, "maxObjects": None},
    }
    assert ("PUT", "/api/s3/users/alice/quota") in env.transport.calls
    sent = env.mock.state.croit.users["alice"].user_quota
    assert sent["enabled"] and sent["maxSize"] == 5 * 1024**3 and sent["maxObjects"] == -1
    (entry,) = env.audit()
    assert entry["actor"] == "bob" and entry["action"] == "admin.user.quota"
    assert entry["target_type"] == "rgw_user" and entry["target"] == "alice"
    d = json.loads(entry["details"]) if isinstance(entry["details"], str) else entry["details"]
    assert d["before"]["enabled"] is False and d["after"]["maxSize"] == 5 * 1024**3


def test_disable_user_quota_keeps_limits(env):
    body = {"enabled": True, "maxObjects": 10}
    env.admin.put("/api/admin/users/alice/quota", json=body, headers=H)
    r = env.admin.put("/api/admin/users/alice/quota", json={"enabled": False}, headers=H)
    assert r.json()["quota"] == {"enabled": False, "maxSize": None, "maxObjects": 10}


def test_tenant_user_and_bucket_paths_are_encoded(env):
    r = env.admin.put("/api/admin/users/acme$carol/quota",
                      json={"enabled": True, "maxSize": 0}, headers=H)
    assert r.status_code == 200 and r.json()["quota"]["maxSize"] == 0  # zero is allowed
    assert ("PUT", "/api/s3/users/acme%24carol/quota") in env.transport.calls
    r = env.admin.put("/api/admin/buckets/acme:reports/quota",
                      json={"enabled": True, "maxObjects": 99}, headers=H)
    assert r.status_code == 200 and r.json()["quota"]["maxObjects"] == 99
    assert ("PUT", "/api/s3/buckets/acme%3Areports/quota") in env.transport.calls


def test_set_bucket_quota_and_audit(env):
    r = env.admin.put("/api/admin/buckets/alice-data/quota",
                      json={"enabled": True, "maxSize": 2048, "maxObjects": 7}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"bucket": "alice-data",
                        "quota": {"enabled": True, "maxSize": 2048, "maxObjects": 7}}
    (entry,) = env.audit()
    assert entry["action"] == "admin.bucket.quota" and entry["target"] == "alice-data"
    listed = {b["name"]: b for b in env.admin.get("/api/admin/buckets").json()["buckets"]}
    assert listed["alice-data"]["quota"]["maxSize"] == 2048


@pytest.mark.parametrize("body", [
    {"enabled": True},  # nothing to enforce
    {"enabled": True, "maxSize": -1},
    {"enabled": True, "maxObjects": -5},
    {"enabled": True, "maxSize": 1.5},
    {"enabled": True, "maxSize": "10"},
    {"enabled": True, "maxSize": True},
    {"enabled": True, "maxSize": 2**63},
    {"enabled": "yes", "maxSize": 1},
    {"maxSize": 1},
    {"enabled": True, "maxSize": 1, "extra": 1},
])
def test_quota_validation(env, body):
    for path in ("/api/admin/users/alice/quota", "/api/admin/buckets/alice-data/quota"):
        assert env.admin.put(path, json=body, headers=H).status_code == 422
    assert env.transport.mutations() == [] and env.audit() == []


def test_unknown_targets_and_bad_names(env):
    ok = {"enabled": False}
    assert env.admin.put("/api/admin/users/nobody/quota", json=ok, headers=H).status_code == 404
    assert env.admin.put("/api/admin/buckets/nope/quota", json=ok, headers=H).status_code == 404
    assert env.admin.put("/api/admin/buckets/bad name/quota", json=ok, headers=H).status_code == 422
    assert env.admin.put("/api/admin/users/%2E%2E/quota", json=ok, headers=H).status_code == 422
    assert env.admin.post("/api/admin/users/nobody/suspend", headers=H).status_code == 404
    assert env.transport.mutations() == [] and env.audit() == []


# -- suspend -------------------------------------------------------------------


def users(env):
    return {u["uid"]: u for u in env.admin.get("/api/admin/users").json()["users"]}


def test_suspend_and_unsuspend_with_bucket_keys(env):
    # alice owns two live bucket keys (bob and acme$carol stand in as the key users)
    k1 = env.db.bucket_keys.create("bob", "alice", "alice-data", "read")
    k2 = env.db.bucket_keys.create("acme$carol", "alice", "alice-empty", "readwrite")
    revoked = env.db.bucket_keys.create("alice.old", "alice", "alice-data", "read")
    env.db.bucket_keys.revoke(revoked["id"])
    r = env.admin.post("/api/admin/users/alice/suspend", headers=H)
    assert r.status_code == 200
    assert r.json() == {"uid": "alice", "suspended": True, "keys": ["bob", "acme$carol"]}
    u = users(env)
    assert u["alice"]["suspended"] and u["bob"]["suspended"] and u["acme$carol"]["suspended"]
    assert env.db.bucket_keys.get(k1["id"])["suspended_at"]
    assert env.db.bucket_keys.get(k2["id"])["suspended_at"]
    assert env.db.bucket_keys.get(revoked["id"])["suspended_at"] is None
    assert not any(m == "DELETE" for m, _ in env.transport.calls)  # never deletes
    assert ("PATCH", "/api/s3/users/acme%24carol") in env.transport.calls

    r = env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert r.status_code == 200 and r.json()["suspended"] is False
    u = users(env)
    assert not (u["alice"]["suspended"] or u["bob"]["suspended"] or u["acme$carol"]["suspended"])
    assert env.db.bucket_keys.get(k1["id"])["suspended_at"] is None
    a1, a2 = env.audit()[::-1]
    assert (a1["action"], a1["actor"], a1["target"]) == ("admin.user.suspend", "bob", "alice")
    assert a2["action"] == "admin.user.unsuspend"
    d = json.loads(a1["details"]) if isinstance(a1["details"], str) else a1["details"]
    assert d["before"] == {"suspended": False} and d["keys"] == ["bob", "acme$carol"]


def test_unsuspend_does_not_wake_independently_untouched_keys(env):
    k = env.db.bucket_keys.create("bob", "alice", "alice-data", "read")
    env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert ("PATCH", "/api/s3/users/bob") not in env.transport.calls  # key was not suspended
    assert env.db.bucket_keys.get(k["id"])["suspended_at"] is None


def test_suspending_a_bucket_key_user_directly(env):
    k = env.db.bucket_keys.create("bob", "alice", "alice-data", "read")
    k2 = env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    r = env.admin.post("/api/admin/users/acme$carol/suspend", headers=H)
    assert r.status_code == 200 and r.json()["keys"] == ["acme$carol"]
    assert env.db.bucket_keys.get(k2["id"])["suspended_at"]
    assert env.db.bucket_keys.get(k["id"])["suspended_at"] is None


def test_cannot_suspend_self(env):
    r = env.admin.post("/api/admin/users/bob/suspend", headers=H)
    assert r.status_code == 409 and env.transport.mutations() == []
    assert env.audit() == []


def test_partial_key_failure_is_reported_and_audited(env):
    env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")

    def fail(req):
        if req.method == "PATCH" and req.url.raw_path.endswith(b"acme%24carol"):
            return httpx.Response(500, json={"code": 500, "message": "boom"})

    env.transport.fail = fail
    r = env.admin.post("/api/admin/users/alice/suspend", headers=H)
    assert r.status_code == 502 and "acme$carol" in r.json()["detail"]
    (entry,) = env.audit()
    assert "keys_failed" in entry["details"]
    assert users(env)["alice"]["suspended"] is True


# -- error mapping ---------------------------------------------------------------


def test_croit_unreachable_is_503_without_leaks(env):
    env.transport.fail = lambda req: httpx.ConnectError("refused " + TOKEN)
    for path in ("/api/admin/users", "/api/admin/buckets"):
        r = env.admin.get(path)
        assert r.status_code == 503 and TOKEN not in r.text and "croit.example.org" not in r.text
    r = env.admin.put("/api/admin/users/alice/quota", json={"enabled": False}, headers=H)
    assert r.status_code == 503 and TOKEN not in r.text
    assert env.audit() == []


def test_croit_errors_are_mapped(env):
    env.transport.fail = lambda req: httpx.Response(
        500, json={"code": 500, "message": f"internal {TOKEN}"})
    r = env.admin.get("/api/admin/users")
    assert r.status_code == 502 and TOKEN not in r.text and "internal" not in r.text

    env.transport.fail = lambda req: httpx.Response(401, json={"code": 401, "message": "bad"})
    r = env.admin.get("/api/admin/buckets")
    assert r.status_code == 502 and "service credentials" in r.json()["detail"]

    env.transport.fail = lambda req: (
        httpx.Response(400, json={"code": 400, "message": f"nope {TOKEN}"})
        if req.method == "PUT" else None)
    r = env.admin.put("/api/admin/buckets/alice-data/quota", json={"enabled": False}, headers=H)
    assert r.status_code == 400 and TOKEN not in r.text and "nope" in r.json()["detail"]
    assert env.audit() == []


# -- suspension provenance (bucket_keys.suspended_by) --------------------------


def key_row(env, rgw_uid):
    return env.db.bucket_keys.get_by_uid(rgw_uid)


def test_direct_suspended_key_stays_suspended_when_owner_is_unsuspended(env):
    k1 = env.db.bucket_keys.create("bob", "alice", "alice-data", "read")
    k2 = env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    # bob is the acting admin, so suspend the compromised key user via carol's uid instead
    assert env.admin.post("/api/admin/users/acme$carol/suspend", headers=H).status_code == 200
    assert key_row(env, "acme$carol")["suspended_by"] == "direct"
    assert env.admin.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    assert key_row(env, "bob")["suspended_by"] == "owner"
    assert key_row(env, "acme$carol")["suspended_by"] == "direct"  # not taken over
    r = env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert r.json()["keys"] == ["bob"]
    assert key_row(env, "bob")["suspended_at"] is None
    assert key_row(env, "acme$carol")["suspended_at"] and users(env)["acme$carol"]["suspended"]
    assert env.db.bucket_keys.get(k1["id"]) and env.db.bucket_keys.get(k2["id"])
    # unsuspending the key user directly clears its own row
    assert env.admin.post("/api/admin/users/acme$carol/unsuspend", headers=H).status_code == 200
    row = key_row(env, "acme$carol")
    assert row["suspended_at"] is None and row["suspended_by"] is None


def test_unsuspending_a_never_suspended_owner_touches_no_keys(env):
    env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    env.admin.post("/api/admin/users/acme$carol/suspend", headers=H)  # direct
    env.transport.calls.clear()
    r = env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert r.status_code == 200 and r.json()["keys"] == []
    assert env.transport.mutations() == [("PATCH", "/api/s3/users/alice")]
    assert key_row(env, "acme$carol")["suspended_at"]


def test_owner_round_trip_and_revoked_keys_never_wake(env):
    live = env.db.bucket_keys.create("bob", "alice", "alice-data", "read")
    old = env.db.bucket_keys.create("alice.old", "alice", "alice-data", "read")
    env.admin.post("/api/admin/users/alice/suspend", headers=H)
    env.db.bucket_keys.revoke(old["id"])  # revoked while suspended
    env.db.bucket_keys.set_suspended(old["id"], True, "owner")
    r = env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert r.json()["keys"] == ["bob"]
    assert env.db.bucket_keys.get(live["id"])["suspended_by"] is None
    assert env.db.bucket_keys.get(old["id"])["suspended_at"]


def test_v1_database_migrates_to_v2(tmp_path):
    import sqlite3

    from bucketeer import db as dbmod

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(dbmod.MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO bucket_keys (rgw_uid, owner_ad_username, bucket_name, access, created_at,"
        " suspended_at) VALUES ('k1','alice','b','read','t','t'),"
        " ('k2','alice','b','read','t',NULL)"
    )
    conn.commit()
    conn.close()
    d = Database(path)
    try:
        assert d.version == len(dbmod.MIGRATIONS) >= 2
        rows = {r["rgw_uid"]: r for r in d.bucket_keys.list()}
        assert rows["k1"]["suspended_by"] == "direct" and rows["k2"]["suspended_by"] is None
        with pytest.raises(sqlite3.IntegrityError):
            d.bucket_keys.set_suspended(rows["k1"]["id"], True, "bogus")
    finally:
        d.close()


# -- bucket quota 0 means "no bound" in croit ------------------------------------


@pytest.mark.parametrize(
    "body", [{"enabled": True, "maxSize": 0}, {"enabled": True, "maxObjects": 0}]
)
def test_bucket_quota_rejects_zero(env, body):
    r = env.admin.put("/api/admin/buckets/alice-data/quota", json=body, headers=H)
    assert r.status_code == 422 and "suspend the owner" in r.json()["detail"]
    assert env.transport.mutations() == [] and env.audit() == []
    assert env.admin.put("/api/admin/users/alice/quota", json=body, headers=H).status_code == 200


def test_bucket_quota_reads_nonpositive_as_unlimited(env):
    q = env.mock.state.croit.buckets["alice-data"].quota
    q.update(enabled=True, maxSize=0, maxObjects=-1)
    b = {x["name"]: x for x in env.admin.get("/api/admin/buckets").json()["buckets"]}
    assert b["alice-data"]["quota"] == {"enabled": True, "maxSize": None, "maxObjects": None}
    # a user quota of 0 is a real zero
    env.admin.put("/api/admin/users/alice/quota", json={"enabled": True, "maxSize": 0}, headers=H)
    assert users(env)["alice"]["quota"]["maxSize"] == 0
