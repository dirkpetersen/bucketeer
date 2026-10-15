"""RGW user resolution (bucketeer/rgw_user.py) against the in-process mock croit."""

import asyncio
import json
import time
from typing import Annotated

import httpx
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from bucketeer.db import Database
from bucketeer.ldapauth import AdAuthenticator
from bucketeer.main import create_app
from bucketeer.policy_settings import QUOTA_KEY
from bucketeer.rgw_user import (
    RgwAccount,
    RgwResolver,
    ad_names_for_uid,
    require_active_rgw_user,
    require_rgw_user,
)
from mock.croit_mock import create_app as create_mock

from .backends import (
    admin_backend,  # noqa: F401  (fixture: both admin backends)
    admin_client,
    croit_view,
    on_rgw,
)
from .conftest import REQUIRED
from .test_admin import Transport
from .test_auth import SECRET_PW, USERS_GROUP, FakeLdap, make_settings

H = {"X-Requested-With": "bucketeer"}
TOKEN = REQUIRED["croit_api_token"]
TIB = 2**40


class Env:
    def __init__(self, tmp_path, **over):
        self.mock = create_mock(seed=True, task_seconds=0, prefix="/api", admin_token=TOKEN)
        self.transport = Transport(self.mock)
        self.fake = FakeLdap()
        for sam, mail in [("dave", "dave@example.org"), ("Erin", "erin@example.org"),
                          ("jdoe", "jdoe@example.org"), ("carol", "carol@example.org"),
                          ("a&b", "ab@example.org")]:
            self.fake.add(sam, groups={USERS_GROUP}, display=sam.title() + " X", mail=mail)
        self.settings = make_settings(tmp_path, **over)
        croit = admin_client(self.settings, self.mock, self.transport)
        self.db = Database(":memory:")
        self.app = create_app(
            self.settings, croit=croit, db=self.db,
            authenticator=AdAuthenticator(self.settings, self.fake),
        )
        self.rgw: RgwResolver = self.app.state.rgw

    def resolve(self, username, display=""):
        return asyncio.run(self.rgw.resolve(username, display or username.title()))

    def client(self, login=None):
        c = TestClient(self.app, base_url="https://testserver")
        if login:
            r = c.post("/api/login", json={"login": login, "password": SECRET_PW}, headers=H)
            assert r.status_code == 200
        return c

    def posts(self):
        return [c for c in self.transport.calls if c == ("POST", "/api/s3/users")]

    def audit(self, prefix="rgw."):
        rows = [a for a in self.db.audit.query(limit=1000) if a["action"].startswith(prefix)]
        for a in rows:
            if isinstance(a["details"], str):
                a["details"] = json.loads(a["details"])
        return rows

    @property
    def users(self):
        return self.mock.state.croit.users


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


@pytest.fixture
def tenant_env(tmp_path):
    e = Env(tmp_path, s3_tenant="acme")
    yield e
    e.db.close()


# -- resolution order ---------------------------------------------------------------


def test_existing_user_resolves_ready_without_create(env):
    a = env.resolve("alice")
    assert (a.uid, a.status, a.source, a.created) == ("alice", "ready", "existing", False)
    assert env.posts() == [] and env.audit() == []


def test_mapping_wins_over_the_default_uid(env):
    env.users["alice-legacy"] = type(env.users["alice"])("alice-legacy", "Legacy", "l@example.org")
    env.db.uid_mappings.set("Alice", "alice-legacy")  # case-insensitive on the AD name
    a = env.resolve("alice")
    assert (a.uid, a.source, a.status) == ("alice-legacy", "mapping", "ready")
    assert env.posts() == []


def test_mapping_to_missing_user_does_not_create(env):
    env.db.uid_mappings.set("dave", "dave-s3")
    a = env.resolve("dave")
    assert (a.status, a.reason, a.uid) == ("creation_failed", "mapping_target_missing", None)
    assert env.posts() == [] and "dave-s3" not in env.users and "dave" not in env.users
    assert env.audit()[0]["action"] == "rgw.user.mapping_target_missing"
    # not cached: once an admin creates the RGW user in croit, the next request works
    env.mock.state.croit.create_user("dave-s3", "Dave", "old-dave@example.org")
    assert env.resolve("dave").uid == "dave-s3"


def test_candidate_is_the_lowercased_sam_account_name(env):
    a = env.resolve("Erin", "Erin X")
    assert (a.uid, a.created, a.status) == ("erin", True, "ready")
    u = env.users["erin"]
    assert (u.name, u.email) == ("Erin X", "")


def test_case_insensitive_match_uses_the_existing_spelling_and_audits(env):
    env.mock.state.croit.create_user("JDoe", "J Doe", "old@example.org")
    a = env.resolve("jdoe")
    assert (a.uid, a.source, a.created) == ("JDoe", "case_match", False)
    assert env.posts() == [] and "jdoe" not in env.users
    (row,) = env.audit()
    assert row["action"] == "rgw.user.case_match" and row["actor"] == "jdoe"
    assert row["details"] == {"candidate": "jdoe", "matched": "JDoe"}


def test_exact_lowercase_match_is_not_audited(env):
    env.mock.state.croit.create_user("jdoe", "J Doe", "old@example.org")
    assert env.resolve("JDoe").uid == "jdoe"
    assert env.audit() == []


def test_two_case_variants_are_ambiguous_not_guessed(env):
    for n, uid in enumerate(("JDoe", "JDOE")):
        env.mock.state.croit.create_user(uid, uid, f"old{n}@example.org")
    a = env.resolve("jdoe")
    assert (a.status, a.reason, a.uid) == ("creation_failed", "ambiguous_uid", None)
    assert env.posts() == []
    assert env.audit()[0]["action"] == "rgw.user.ambiguous_uid"


def test_tenant_prefix_for_derived_and_matched_uids(tenant_env):
    e = tenant_env
    assert e.resolve("carol").uid == "acme$carol"  # seeded, exact
    a = e.resolve("dave")
    assert (a.uid, a.created) == ("acme$dave", True) and "acme$dave" in e.users
    # a user of another tenant (or none) with the same name is not the tenant's user
    assert e.resolve("alice").uid == "acme$alice"
    assert "alice" in e.users  # untouched


def test_tenant_case_match_stays_within_the_tenant(tenant_env):
    e = tenant_env
    e.mock.state.croit.create_user("JDoe", "x", "x@example.org", tenant="acme")
    assert e.resolve("jdoe").uid == "acme$JDoe"


def test_invalid_sam_account_name_is_rejected_without_calling_croit(env):
    for bad in ("a&b", "john smith", "x$", "-lead", "a" * 65, "täst", "a/b", "john+backup"):
        a = env.resolve(bad)
        assert (a.status, a.reason, a.uid) == ("creation_failed", "invalid_username", None), bad
    assert env.transport.mutations() == [] and env.posts() == []
    assert env.audit()[0]["action"] == "rgw.user.invalid_username"


def test_uid_is_never_derived_from_the_email(env):
    # the email local part differs from the sAMAccountName: only the latter counts
    a = env.resolve("dave")
    assert a.uid == "dave" and "somebody.else" not in env.users


# -- auto-create and quota ------------------------------------------------------------


def test_auto_create_applies_1_tib_by_default_and_audits(env):
    a = env.resolve("dave", "Dave X")
    assert a.created and a.status == "ready"
    assert a.quota == {"enabled": True, "maxSize": TIB, "maxObjects": None}
    q = env.users["dave"].user_quota
    assert q["enabled"] and q["maxSize"] == TIB and q["maxObjects"] < 0
    (row,) = env.audit()
    assert (row["action"], row["actor"], row["target"]) == ("rgw.user.create", "dave", "dave")
    assert row["details"] == {
        "quota": {"enabled": True, "maxSize": TIB, "maxObjects": None}, "quota_applied": True,
    }


def test_auto_create_uses_the_admin_default_quota(env):
    env.db.settings.set(QUOTA_KEY, {"enabled": True, "maxSize": 5 * 2**30, "maxObjects": 1000})
    a = env.resolve("dave")
    assert a.quota == {"enabled": True, "maxSize": 5 * 2**30, "maxObjects": 1000}
    assert env.audit()[0]["details"]["quota"]["maxObjects"] == 1000


def test_admin_default_with_quota_disabled_is_honored(env):
    env.db.settings.set(QUOTA_KEY, {"enabled": False, "maxSize": None, "maxObjects": None})
    assert env.resolve("dave").quota["enabled"] is False


def test_create_sends_only_uid_and_name_never_an_email(env):
    bodies = []
    env.transport.fail = lambda req: (
        bodies.append(json.loads(req.content)) if req.method == "POST" else None
    )
    # alice's address belongs to a legacy account; AD mail must not matter
    env.fake.add("dave", groups={USERS_GROUP}, display="Dave X", mail="alice@example.org")
    a = env.resolve("dave")
    assert a.status == "ready" and env.users["dave"].email == ""
    assert len(bodies) == 1 and set(bodies[0]) <= {"uid", "name"}


def test_croit_4xx_on_create_is_croit_rejected_audited_and_cached(env):
    env.transport.fail = lambda req: (
        httpx.Response(422, json={"code": 422, "message": "nope"}) if req.method == "POST" else None
    )
    a = env.resolve("dave")
    assert (a.status, a.reason, a.uid) == ("creation_failed", "croit_rejected", None)
    assert "dave" not in env.users and len(env.posts()) == 1
    (row,) = env.audit()
    assert row["action"] == "rgw.user.create_failed"
    assert row["details"]["reason"] == "croit_rejected"
    assert "contact your administrator" in a.public()["message"]
    assert env.resolve("dave").status == "creation_failed"
    assert len(env.posts()) == 1  # cached for the TTL, no hammering


def test_concurrent_first_requests_create_once(env):
    async def go():
        return await asyncio.gather(
            *[env.rgw.resolve("dave", "Dave X") for _ in range(6)]
        )

    out = asyncio.run(go())
    assert {a.uid for a in out} == {"dave"} and all(a.status == "ready" for a in out)
    assert len(env.posts()) == 1
    assert len([a for a in env.audit() if a["action"] == "rgw.user.create"]) == 1


def test_already_exists_from_croit_counts_as_success(env):
    """A second process/resolver without our lock loses the race: 409 -> re-resolve."""
    other = RgwResolver(env.rgw.croit, lambda: env.db, env.rgw.tenant)

    async def go():
        # Force the race: each POST is held until both resolvers have reached
        # their create (both already saw "dave" missing), then both go out.
        both_at_create = asyncio.Event()
        arrived = 0
        inner = env.transport.handle_async_request

        async def gated(request):
            nonlocal arrived
            view = croit_view(request) if on_rgw() else request  # rgw: PUT /admin/user
            if view.method == "POST" and view.url.path.endswith("/s3/users"):
                arrived += 1
                if arrived >= 2:
                    both_at_create.set()
                await asyncio.wait_for(both_at_create.wait(), 5)
            return await inner(request)

        env.transport.handle_async_request = gated
        return await asyncio.gather(
            env.rgw.resolve("dave", "Dave X"),
            other.resolve("dave", "Dave X"),
        )

    a, b = asyncio.run(go())
    assert a.uid == b.uid == "dave" and a.status == b.status == "ready"
    assert len(env.posts()) == 2  # both tried; croit refused one
    assert "dave" in env.users


# -- failures, status and cache ------------------------------------------------------------


def test_croit_down_is_unavailable_and_not_cached(env):
    env.transport.fail = lambda req: httpx.ConnectError("refused " + TOKEN)
    a = env.resolve("dave")
    assert (a.status, a.uid) == ("unavailable", None)
    assert "temporarily unavailable" in a.public()["message"]
    assert TOKEN not in json.dumps(a.public())
    env.transport.fail = None
    b = env.resolve("dave")
    assert (b.status, b.uid, b.created) == ("ready", "dave", True)


def test_other_croit_failure_on_create_is_unavailable_not_cached(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"code": 500, "message": "boom"})
        if req.method == "POST" else None
    )
    assert env.resolve("dave").status == "unavailable"
    env.transport.fail = None
    assert env.resolve("dave").status == "ready"


def test_quota_failure_leaves_unavailable_and_is_retried_next_time(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"code": 500, "message": "boom"})
        if req.method == "PUT" and req.url.path.endswith("/quota") else None
    )
    assert env.resolve("dave").status == "unavailable"
    assert "dave" in env.users and not env.users["dave"].user_quota["enabled"]
    env.transport.fail = None
    a = env.resolve("dave")
    assert a.status == "ready" and env.users["dave"].user_quota["maxSize"] == TIB
    assert len(env.posts()) == 1  # not created twice


def test_suspended_user_resolves_with_suspended_status(env):
    env.users["alice"].suspended = True
    a = env.resolve("alice")
    assert (a.status, a.uid) == ("suspended", "alice")
    assert a.public()["uid"] == "alice"


def test_result_is_cached_until_the_ttl(env):
    t = [1000.0]
    env.rgw.clock = lambda: t[0]
    env.resolve("alice")
    n = len(env.transport.calls)
    env.resolve("alice")
    assert len(env.transport.calls) == n  # served from memory
    t[0] += 301
    env.resolve("alice")
    assert len(env.transport.calls) > n


def test_invalidate_by_username_and_uid(env):
    env.resolve("alice")
    env.users["alice"].suspended = True
    assert env.resolve("alice").status == "ready"  # stale on purpose
    env.rgw.invalidate(uid="ALICE")
    assert env.resolve("alice").status == "suspended"
    env.users["alice"].suspended = False
    env.rgw.invalidate(username="Alice")
    assert env.resolve("alice").status == "ready"


# -- dependencies and /api/account ---------------------------------------------------------


def test_account_endpoint_requires_a_session(env):
    assert env.client().get("/api/account").status_code == 401
    assert env.transport.calls == []


def test_account_endpoint_first_login_creates_user_and_hides_keys(env):
    c = env.client("dave")
    r = c.get("/api/account")
    assert r.status_code == 200
    body = r.json()
    assert body["uid"] == "dave" and body["status"] == "ready" and body["created"] is True
    assert body["displayName"] == "Dave X"
    assert body["quota"] == {"enabled": True, "maxSize": TIB, "maxObjects": None}
    assert "key" not in r.text.lower() and "secret" not in r.text.lower()
    assert r.headers["cache-control"] == "no-store"


def test_account_endpoint_reports_creation_failure_and_unavailable(env):
    env.transport.fail = lambda req: (
        httpx.Response(422, json={"code": 422, "message": "nope"}) if req.method == "POST" else None
    )
    env.fake.add("frank", groups={USERS_GROUP}, display="Frank", mail="frank@example.org")
    c = env.client("frank")
    body = c.get("/api/account").json()
    assert body["status"] == "creation_failed" and body["uid"] is None
    assert "contact your administrator" in body["message"]
    env.fake.add("gina", groups={USERS_GROUP}, display="Gina", mail="gina@example.org")
    g = env.client("gina")
    env.transport.fail = lambda req: httpx.ConnectError("down")
    r = g.get("/api/account")
    assert r.status_code == 200 and r.json()["status"] == "unavailable"


def _probe(env):
    """Routes using the dependencies, placed before the static mount."""
    seen = {}

    @env.app.get("/api/_probe")
    async def probe(acct: Annotated[RgwAccount, Depends(require_rgw_user)], uid: str = ""):
        seen["uid"] = acct.uid
        return {"uid": acct.uid, "status": acct.status}

    @env.app.get("/api/_probe_active")
    async def probe_active(acct: Annotated[RgwAccount, Depends(require_active_rgw_user)]):
        return {"uid": acct.uid}

    env.app.router.routes[:] = env.app.router.routes[-2:] + env.app.router.routes[:-2]
    return seen


def test_uid_comes_from_the_session_never_from_the_request(env):
    seen = _probe(env)
    c = env.client("dave")
    r = c.get("/api/_probe?uid=bob", headers={"X-Rgw-Uid": "bob"})
    assert r.json()["uid"] == "dave" and seen["uid"] == "dave"
    c.cookies.set("uid", "bob")
    assert c.get("/api/_probe?uid=alice").json()["uid"] == "dave"
    assert env.client().get("/api/_probe?uid=bob").status_code == 401


def test_require_rgw_user_maps_states_to_http_errors(env):
    _probe(env)
    env.users["alice"].suspended = True
    c = env.client("alice")
    assert c.get("/api/_probe").json()["status"] == "suspended"
    assert c.get("/api/_probe_active").status_code == 403
    env.transport.fail = lambda req: (
        httpx.Response(422, json={"code": 422, "message": "nope"}) if req.method == "POST" else None
    )
    env.fake.add("frank", groups={USERS_GROUP}, display="Frank", mail="frank@example.org")
    assert env.client("frank").get("/api/_probe").status_code == 409
    env.rgw.invalidate()
    env.transport.fail = lambda req: httpx.ConnectError("down")
    assert c.get("/api/_probe").status_code == 503


# -- resolution right after login ------------------------------------------------------------


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_login_triggers_background_resolution(env, monkeypatch):
    monkeypatch.setattr(RgwResolver, "warm_on_login", True)
    with TestClient(env.app, base_url="https://testserver") as c:
        r = c.post("/api/login", json={"login": "dave", "password": SECRET_PW}, headers=H)
        assert r.status_code == 200
        assert _wait(lambda: "dave" in env.users)


def test_login_succeeds_when_croit_is_down(env, monkeypatch):
    monkeypatch.setattr(RgwResolver, "warm_on_login", True)
    env.transport.fail = lambda req: httpx.ConnectError("down")
    with TestClient(env.app, base_url="https://testserver") as c:
        r = c.post("/api/login", json={"login": "dave", "password": SECRET_PW}, headers=H)
        assert r.status_code == 200 and c.get("/api/me").status_code == 200
        assert _wait(lambda: not env.rgw._tasks)
    assert "dave" not in env.users


# -- admin side: mapping/suspend invalidation and owner lookup -----------------------------------


def test_admin_mapping_change_invalidates_the_cache(env):
    admin = env.client("bob")
    c = env.client("dave")
    assert c.get("/api/account").json()["uid"] == "dave"
    env.mock.state.croit.create_user("dave-new", "Dave", "dave-old@example.org")
    r = admin.put("/api/admin/settings/uid-mappings/dave", json={"rgwUid": "dave-new"}, headers=H)
    assert r.status_code == 200
    assert c.get("/api/account").json()["uid"] == "dave-new"
    r = admin.delete("/api/admin/settings/uid-mappings/dave", headers=H)
    assert r.status_code == 200
    assert c.get("/api/account").json()["uid"] == "dave"


def test_admin_suspend_invalidates_the_cache(env):
    admin = env.client("bob")
    c = env.client("alice")
    assert c.get("/api/account").json()["status"] == "ready"
    assert admin.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    assert c.get("/api/account").json()["status"] == "suspended"
    assert admin.post("/api/admin/users/alice/unsuspend", headers=H).status_code == 200
    assert c.get("/api/account").json()["status"] == "ready"


def test_ad_names_for_uid_follows_the_resolution_rules(env):
    d = env.db
    d.uid_mappings.set("dirk", "dirk-old")
    assert ad_names_for_uid(d, "dirk-old", "") == ["dirk"]
    assert ad_names_for_uid(d, "dirk", "") == []  # dirk is mapped elsewhere
    assert ad_names_for_uid(d, "alice", "") == ["alice"]
    assert ad_names_for_uid(d, "acme$alice", "") == []  # wrong tenant for this deployment
    assert ad_names_for_uid(d, "acme$alice", "acme") == ["alice"]
    assert ad_names_for_uid(d, "JDoe", "") == ["JDoe"]  # owner column is NOCASE


def test_admin_suspend_finds_bucket_keys_of_a_mapped_owner(env):
    cs = env.mock.state.croit
    cs.create_user("dirk-old", "Dirk", "dirk@example.org")
    cs.create_user("dirk-key", "key", "")
    env.db.uid_mappings.set("dirk", "dirk-old")
    k = env.db.bucket_keys.create("dirk-key", "dirk", "dirk-data", "read")
    r = env.client("bob").post("/api/admin/users/dirk-old/suspend", headers=H)
    assert r.status_code == 200 and r.json()["keys"] == ["dirk-key"]
    assert env.db.bucket_keys.get(k["id"])["suspended_at"]
    assert cs.users["dirk-key"].suspended
    r = env.client("bob").post("/api/admin/users/dirk-old/unsuspend", headers=H)
    assert r.json()["keys"] == ["dirk-key"] and not cs.users["dirk-key"].suspended


def test_admin_suspend_finds_bucket_keys_with_a_tenant(tenant_env):
    e = tenant_env
    cs = e.mock.state.croit
    cs.create_user("carol-key", "key", "")
    k = e.db.bucket_keys.create("carol-key", "carol", "reports", "read")
    r = e.client("bob").post("/api/admin/users/acme%24carol/suspend", headers=H)
    assert r.status_code == 200 and r.json()["keys"] == ["carol-key"]
    assert e.db.bucket_keys.get(k["id"])["suspended_by"] == "owner"


def test_admin_cannot_suspend_self_via_a_mapped_uid(env):
    env.mock.state.croit.create_user("bob-s3", "Bob", "bob2@example.org")
    env.db.uid_mappings.set("bob", "bob-s3")
    r = env.client("bob").post("/api/admin/users/bob-s3/suspend", headers=H)
    assert r.status_code == 409
    assert "bob-s3" in env.users and not env.users["bob-s3"].suspended


# -- a uid belongs to its mapping / bucket key, not to a look-alike AD user ---------------------


def test_unmapped_user_cannot_resolve_to_a_mapping_target(env):
    env.db.uid_mappings.set("alice", "bob")  # alice -> RGW bob, which exists
    assert env.resolve("alice").uid == "bob"
    b = env.resolve("bob")  # the real AD user bob
    assert (b.status, b.reason, b.uid) == ("creation_failed", "uid_taken", None)
    assert env.posts() == []
    assert any(a["action"] == "rgw.user.uid_taken" and a["actor"] == "bob" for a in env.audit())
    assert "contact your administrator" in b.public()["message"]


def test_case_variant_of_a_mapping_target_is_taken_too(env):
    env.mock.state.croit.create_user("JoeX", "x", "x@example.org")
    env.db.uid_mappings.set("alice", "JoeX")
    a = env.resolve("joex")
    assert (a.status, a.reason) == ("creation_failed", "uid_taken")
    assert not any(x["action"] == "rgw.user.case_match" for x in env.audit())  # refused first
    env.db.uid_mappings.set("alice", "JOEX")  # even a mapping target spelled differently
    env.rgw.invalidate()
    assert env.resolve("joex").reason == "uid_taken"


def test_derived_uid_that_is_a_mapping_target_is_not_created(env):
    env.db.uid_mappings.set("alice", "dave")  # target does not even exist yet
    a = env.resolve("dave")
    assert (a.status, a.reason) == ("creation_failed", "uid_taken") and env.posts() == []


def test_user_named_like_a_bucket_key_uid_is_refused(env):
    cs = env.mock.state.croit
    cs.create_user("jo.bkp", "key", "")
    live = env.db.bucket_keys.create("jo.bkp", "alice", "alice-data", "read")
    assert env.resolve("jo.bkp").reason == "uid_taken"
    env.db.bucket_keys.revoke(live["id"])  # revoked rows still reserve the uid
    env.rgw.invalidate()
    assert env.resolve("JO.bkp").reason == "uid_taken"
    assert env.posts() == []


def test_tenant_is_compared_exactly(tenant_env):
    e = tenant_env
    e.mock.state.croit.create_user("Bob", "x", "x@example.org", tenant="ACME")
    a = e.resolve("bob")
    assert (a.uid, a.created) == ("acme$bob", True)  # ACME$Bob is another tenant's user
    e.db.uid_mappings.set("alice", "ACME$Zed")
    assert e.resolve("zed").uid == "acme$zed"  # different tenant


def test_ad_names_for_uid_skips_claimed_uids(env):
    d = env.db
    d.uid_mappings.set("alice", "bob")
    assert ad_names_for_uid(d, "bob", "") == ["alice"]  # not also "bob"
    d.bucket_keys.create("jo.bkp", "alice", "alice-data", "read")
    assert ad_names_for_uid(d, "jo.bkp", "") == []
    assert ad_names_for_uid(d, "BOB", "") == ["alice"]


def test_admin_side_alice_mapped_to_bob(env):
    d, cs = env.db, env.mock.state.croit
    d.uid_mappings.set("alice", "bob")
    cs.create_user("alice-key", "key", "")
    k = d.bucket_keys.create("alice-key", "alice", "alice-data", "read")
    cs.create_user("bob-key", "key", "")
    kb = d.bucket_keys.create("bob-key", "bob", "bob-shared", "read")
    r = env.client("bob").post("/api/admin/users/bob/suspend", headers=H)
    # admin bob is not the owner of RGW bob (alice is), so the self guard must not fire,
    # and only alice's keys cascade
    assert r.status_code == 200 and r.json()["keys"] == ["alice-key"]
    assert d.bucket_keys.get(k["id"])["suspended_at"]
    assert d.bucket_keys.get(kb["id"])["suspended_at"] is None


def test_remapping_invalidates_old_and_new_targets_and_look_alikes(env):
    admin = env.client("bob")
    cs = env.mock.state.croit
    cs.create_user("zed1", "z", "z1@example.org")
    cs.create_user("zed2", "z", "z2@example.org")
    assert admin.put("/api/admin/settings/uid-mappings/dave",
                     json={"rgwUid": "zed1"}, headers=H).status_code == 200
    d = env.client("dave")
    assert d.get("/api/account").json()["uid"] == "zed1"
    # a look-alike AD user "zed1" is refused while the mapping stands...
    env.fake.add("zed1", groups={USERS_GROUP}, display="Zed", mail="zz@example.org")
    z = env.client("zed1")
    assert z.get("/api/account").json()["status"] == "creation_failed"
    # ...re-mapping dave elsewhere clears both cached results at once
    assert admin.put("/api/admin/settings/uid-mappings/dave",
                     json={"rgwUid": "zed2"}, headers=H).status_code == 200
    assert d.get("/api/account").json()["uid"] == "zed2"
    assert z.get("/api/account").json()["uid"] == "zed1"
    assert admin.delete("/api/admin/settings/uid-mappings/dave", headers=H).status_code == 200
    assert d.get("/api/account").json()["uid"] == "dave"


def test_pending_default_quota_survives_a_restart(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"code": 500, "message": "boom"})
        if req.method == "PUT" and req.url.path.endswith("/quota") else None
    )
    assert env.resolve("dave").status == "unavailable"
    env.transport.fail = None
    fresh = RgwResolver(env.rgw.croit, lambda: env.db, env.rgw.tenant)  # "restart": no memory
    a = asyncio.run(fresh.resolve("dave", "Dave X"))
    assert a.status == "ready" and a.quota["maxSize"] == TIB
    assert env.users["dave"].user_quota["maxSize"] == TIB
    assert env.audit()[0]["action"] == "rgw.user.default_quota"
    # settled: an admin who later removes the quota is not overruled
    env.users["dave"].user_quota["enabled"] = False
    fresh.invalidate()
    assert asyncio.run(fresh.resolve("dave", "Dave X")).quota["enabled"] is False


def test_pending_quota_entry_survives_many_unrelated_audit_rows(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"code": 500, "message": "boom"})
        if req.method == "PUT" and req.url.path.endswith("/quota") else None
    )
    assert env.resolve("dave").status == "unavailable"
    env.transport.fail = None
    for i in range(70):
        env.db.audit.add("x", "rgw.user.uid_taken", "rgw_user", "dave", {"n": i})
    fresh = RgwResolver(env.rgw.croit, lambda: env.db, env.rgw.tenant)
    assert asyncio.run(fresh.resolve("dave", "Dave X")).quota["maxSize"] == TIB
    assert env.users["dave"].user_quota["maxSize"] == TIB


def test_mapping_target_missing_audits_once_per_period(env):
    t = [1000.0]
    env.rgw.clock = lambda: t[0]
    env.db.uid_mappings.set("dave", "dave-s3")
    for _ in range(5):
        assert env.resolve("dave").reason == "mapping_target_missing"
    rows = [a for a in env.audit() if a["action"] == "rgw.user.mapping_target_missing"]
    assert len(rows) == 1
    t[0] += 301
    env.resolve("dave")
    assert len(env.audit("rgw.user.mapping_target_missing")) == 2
    env.mock.state.croit.create_user("dave-s3", "Dave", "old@example.org")
    assert env.resolve("dave").uid == "dave-s3"  # immediate, no waiting for a cache
