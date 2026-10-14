"""/api/buckets against the mock croit and a moto S3 server.

moto does not enforce identities, so "the user's own key was used" is asserted with a
spy on the S3 client factory (`app.state.s3.factory`) and ownership at the API layer.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from bucketeer.croit import CroitClient
from bucketeer.db import Database
from bucketeer.ldapauth import AdAuthenticator
from bucketeer.main import create_app
from mock.croit_mock import SEED_USERS, MockHooks
from mock.croit_mock import create_app as create_mock
from mock.s3_bridge import S3Bridge

from .conftest import REQUIRED
from .test_admin import Transport
from .test_auth import SECRET_PW, TEAM_GROUP, USERS_GROUP, FakeLdap, make_settings

H = {"X-Requested-With": "bucketeer"}
TOKEN = REQUIRED["croit_api_token"]
ALICE_KEY = SEED_USERS["alice"][2]
BOB_KEY = SEED_USERS["bob"][2]
CAROL_KEY = SEED_USERS["acme$carol"][2]


class Env:
    def __init__(self, tmp_path, s3_endpoint, **over):
        self.bridge = S3Bridge(s3_endpoint)
        self.mock = create_mock(
            seed=True, task_seconds=0, prefix="/api", admin_token=TOKEN,
            hooks=self.bridge.make_hooks(MockHooks), rgw_endpoint=self.bridge.host_port,
        )
        self.croit_state = self.mock.state.croit
        self.bridge.seed_objects(self.croit_state)
        self.transport = Transport(self.mock)
        self.settings = make_settings(tmp_path, s3_endpoint_url=s3_endpoint, **over)
        croit = CroitClient(self.settings, poll_interval=0.01, transport=self.transport)
        self.db = Database(":memory:")
        self.fake = FakeLdap()
        self.fake.add("carol", groups={USERS_GROUP, TEAM_GROUP}, display="Carol C",
                      mail="carol@example.org")
        self.app = create_app(self.settings, croit=croit, db=self.db,
                              authenticator=AdAuthenticator(self.settings, self.fake))
        self.used: list[str] = []  # access keys the S3 factory was asked to sign with
        real = self.app.state.s3.factory

        def spy(endpoint, access, secret, settings):
            self.used.append(access)
            return real(endpoint, access, secret, settings)

        self.app.state.s3.factory = spy
        self.real_factory = spy
        self.alice = self.session("alice")
        self.bob = self.session("bob")  # an administrator

    def session(self, login):
        c = TestClient(self.app, base_url="https://testserver")
        r = c.post("/api/login", json={"login": login, "password": SECRET_PW}, headers=H)
        assert r.status_code == 200
        return c

    def anon(self):
        return TestClient(self.app, base_url="https://testserver")

    def moto(self):
        return self.bridge.client("any", "any")

    def moto_buckets(self):
        return {b["Name"] for b in self.moto().list_buckets()["Buckets"]}

    def tags(self, bucket):
        try:
            tagset = self.moto().get_bucket_tagging(Bucket=bucket)["TagSet"]
        except ClientError:
            return {}
        return {t["Key"]: t["Value"] for t in tagset}

    def audit(self, prefix="bucket."):
        return [a for a in self.db.audit.query(limit=1000) if a["action"].startswith(prefix)]

    def mutations(self, path_part="/s3/buckets"):
        return [c for c in self.transport.mutations() if path_part in c[1]]

    def break_tagging(self):
        broken = MagicMock()
        err = ClientError({"Error": {"Code": "InternalError", "Message": "boom"}},
                          "GetBucketTagging")
        broken.get_bucket_tagging.side_effect = err
        broken.put_bucket_tagging.side_effect = err
        self.app.state.s3.factory = lambda *a: broken

    def fix_tagging(self):
        self.app.state.s3.factory = self.real_factory


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def create(client, name="alice-new", budget="WT-123", **extra):
    return client.post("/api/buckets", json={"name": name, "budgetNumber": budget, **extra},
                       headers=H)


# -- list --------------------------------------------------------------------------


def test_list_only_own_buckets(env):
    r = env.alice.get("/api/buckets")
    assert r.status_code == 200
    by_name = {b["name"]: b for b in r.json()["buckets"]}
    assert set(by_name) == {"alice-data", "alice-empty"}
    data = by_name["alice-data"]
    assert data["objects"] == 42 and data["bytes"] == 1048576
    assert data["quota"] == {"enabled": False, "maxSize": None, "maxObjects": None}
    assert data["versioning"] == "off"
    assert data["budgetNumber"] is None and data["budgetMissing"] is True
    assert data["tagPending"] is False and data["tenant"] == ""
    assert env.bob.get("/api/buckets").json()["buckets"][0]["name"] == "bob-shared"


def test_list_shows_budget_and_creation_date_after_create(env):
    assert create(env.alice).status_code == 201
    b = next(b for b in env.alice.get("/api/buckets").json()["buckets"] if b["name"] == "alice-new")
    assert b["budgetNumber"] == "WT-123" and b["budgetMissing"] is False
    assert b["createdAt"].endswith("Z")


def test_list_quota_is_reported(env):
    env.croit_state.buckets["alice-data"].quota = {
        "enabled": True, "maxSize": 2097152, "maxObjects": -1}
    b = next(b for b in env.alice.get("/api/buckets").json()["buckets"]
             if b["name"] == "alice-data")
    assert b["quota"] == {"enabled": True, "maxSize": 2097152, "maxObjects": None}


def test_tenant_user_sees_short_names(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    try:
        carol = e.session("carol")
        (b,) = carol.get("/api/buckets").json()["buckets"]
        assert b["name"] == "reports" and b["tenant"] == "acme"
        r = create(carol, name="carol-new")
        assert r.status_code == 201 and r.json()["name"] == "carol-new"
        assert "acme:carol-new" in e.croit_state.buckets
        assert e.db.buckets.get("acme:carol-new")["owner_uid"] == "acme$carol"
        assert e.tags("carol-new") == {"bucketeer-budget": "WT-123"}
        assert e.used == [CAROL_KEY]
        assert carol.delete("/api/buckets/carol-new", headers=H).status_code == 200
        assert "acme:carol-new" not in e.croit_state.buckets
        assert e.db.buckets.get("acme:carol-new") is None
    finally:
        e.db.close()


# -- options -----------------------------------------------------------------------


def test_options_for_user_and_admin(env):
    env.db.settings.set("naming", {"prefix": "{username}-", "pattern": ""})
    env.db.profiles.set_default_budget_number("alice", "WT-9")
    o = env.alice.get("/api/buckets/options").json()
    assert o["naming"]["prefix"] == "alice-" and o["naming"]["hasPattern"] is False
    assert "alice-" in o["naming"]["hint"]
    assert o["placements"] == [] and o["defaultPlacement"] == "default-placement"
    assert o["defaultBudgetNumber"] == "WT-9" and o["budgetRequired"] is True
    assert env.bob.get("/api/buckets/options").json()["budgetRequired"] is False


def test_options_offer_placements_only_when_more_than_one(env):
    env.db.settings.set("placement",
                        {"default": "default-placement", "allowed": ["nvme-placement"]})
    o = env.alice.get("/api/buckets/options").json()
    assert o["placements"] == ["default-placement", "nvme-placement"]
    env.db.settings.set("placement", {"default": None, "allowed": ["nvme-placement"]})
    assert env.alice.get("/api/buckets/options").json()["placements"] == [
        "default-placement", "nvme-placement"]
    env.db.settings.set("placement", {"default": None, "allowed": []})
    assert env.alice.get("/api/buckets/options").json()["placements"] == []


# -- create ------------------------------------------------------------------------


def test_create_success_row_tag_and_own_key(env):
    r = create(env.alice, versioning=True)
    assert r.status_code == 201
    assert r.json() == {"name": "alice-new", "budgetNumber": "WT-123", "versioning": "enabled",
                        "objectLock": False, "tagSynced": True}
    assert "alice-new" in env.moto_buckets()
    assert env.croit_state.buckets["alice-new"].owner == "alice"
    assert env.tags("alice-new") == {"bucketeer-budget": "WT-123"}
    assert env.moto().get_bucket_versioning(Bucket="alice-new")["Status"] == "Enabled"
    assert env.used == [ALICE_KEY]  # the tag was written with alice's own key only
    row = env.db.buckets.get("alice-new")
    assert row["owner_uid"] == "alice" and row["budget_number"] == "WT-123"
    assert row["created_by"] == "alice" and row["tag_synced"] == 1
    (entry,) = env.audit("bucket.create")
    assert entry["actor"] == "alice" and entry["target"] == "alice-new"
    assert entry["details"]["budget"] == "WT-123" and entry["details"]["tagSynced"] is True


def test_create_without_versioning_leaves_it_off(env):
    assert create(env.alice).json()["versioning"] == "off"
    assert env.croit_state.buckets["alice-new"].versioning == "OFF"


def test_create_with_object_lock(env):
    r = create(env.alice, objectLock=True)
    assert r.status_code == 201 and r.json()["objectLock"] is True
    assert r.json()["versioning"] == "enabled"
    lock = env.moto().get_object_lock_configuration(Bucket="alice-new")
    assert lock["ObjectLockConfiguration"]["ObjectLockEnabled"] == "Enabled"
    assert env.audit("bucket.create")[0]["details"]["objectLock"] is True


@pytest.mark.parametrize("name", ["ab", "Alice-New", "alice_new", "-alice", "alice..new",
                                  "a" * 64, "alice-new-", "acme:alice-new"])
def test_create_rejects_invalid_dns_names(env, name):
    r = create(env.alice, name=name)
    assert r.status_code == 422
    assert env.mutations() == []


def test_create_enforces_naming_prefix_and_pattern(env):
    env.db.settings.set("naming", {"prefix": "{username}-", "pattern": r"[a-z]+-[a-z]+"})
    r = create(env.alice, name="data-set")
    assert r.status_code == 422 and 'must start with "alice-"' in r.json()["detail"]
    r = create(env.alice, name="alice-new1")
    assert r.status_code == 422 and "format" in r.json()["detail"]
    assert create(env.alice, name="alice-new").status_code == 201
    assert env.mutations() == [("PUT", "/api/s3/buckets/alice-new")]


def test_budget_required_for_users_optional_for_admins(env):
    for body in ({"name": "alice-new"}, {"name": "alice-new", "budgetNumber": "  "},
                 {"name": "alice-new", "budgetNumber": None}):
        r = env.alice.post("/api/buckets", json=body, headers=H)
        assert r.status_code == 422 and "budget number" in r.json()["detail"]
    assert env.mutations() == []
    r = env.bob.post("/api/buckets", json={"name": "bob-new"}, headers=H)
    assert r.status_code == 201 and r.json()["budgetNumber"] is None
    assert env.db.buckets.get("bob-new")["budget_number"] is None
    assert env.tags("bob-new") == {}
    assert env.used == []  # nothing to tag, no S3 call


@pytest.mark.parametrize("budget", ["bad#value", "<script>", "ünï", "x" * 65, "a\nb"])
def test_invalid_budget_numbers(env, budget):
    r = create(env.alice, budget=budget)
    assert r.status_code == 422
    assert env.mutations() == []


def test_admin_budget_is_validated_when_given(env):
    r = env.bob.post("/api/buckets", json={"name": "bob-new", "budgetNumber": "bad#"}, headers=H)
    assert r.status_code == 422


def test_budget_admin_pattern_is_applied(env):
    env.db.settings.set("budget_number", {"pattern": r"WT-[0-9]{3}", "example": "WT-123"})
    r = create(env.alice, budget="nope")
    assert r.status_code == 422 and "WT-123" in r.json()["detail"]
    assert env.alice.get("/api/buckets/options").json()["budgetExample"] == "WT-123"
    assert create(env.alice, budget="WT-777").status_code == 201


def test_placement_must_be_allowed(env):
    r = create(env.alice, placement="nvme-placement")
    assert r.status_code == 422 and "placement" in r.json()["detail"]
    env.db.settings.set("placement", {"default": None, "allowed": ["nvme-placement"]})
    assert create(env.alice, placement="mystery").status_code == 422
    assert env.mutations() == []
    r = create(env.alice, placement="nvme-placement")
    assert r.status_code == 201
    assert env.croit_state.buckets["alice-new"].placement == "nvme-placement"
    # the default is always acceptable
    r = create(env.alice, name="alice-two", placement="default-placement")
    assert r.status_code == 201


def test_duplicate_name_is_409(env):
    r = create(env.alice, name="bob-shared")  # another user's bucket is never adopted
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    assert env.mutations() == []
    assert env.db.buckets.get("bob-shared") is None
    assert env.audit("bucket.create") == []


def test_tag_failure_keeps_bucket_and_reconciles_later(env):
    env.break_tagging()
    r = create(env.alice)
    assert r.status_code == 201
    assert r.json()["tagSynced"] is False and "retried" in r.json()["warning"]
    assert "alice-new" in env.moto_buckets() and env.tags("alice-new") == {}
    row = env.db.buckets.get("alice-new")
    assert row["budget_number"] == "WT-123" and row["tag_synced"] == 0
    assert env.audit("bucket.create")[0]["details"]["tagSynced"] is False
    # the list flags it and does not touch anything
    b = next(b for b in env.alice.get("/api/buckets").json()["buckets"]
             if b["name"] == "alice-new")
    assert b["tagPending"] is True and env.tags("alice-new") == {}
    # still broken: stays pending
    r = env.alice.post("/api/buckets/reconcile", headers=H)
    assert r.json() == {"synced": [], "pending": ["alice-new"]}
    env.fix_tagging()
    r = env.alice.post("/api/buckets/reconcile", headers=H)
    assert r.json() == {"synced": ["alice-new"], "pending": []}
    assert env.tags("alice-new") == {"bucketeer-budget": "WT-123"}
    assert env.db.buckets.get("alice-new")["tag_synced"] == 1
    assert env.alice.get("/api/buckets").json()["buckets"][2]["tagPending"] is False
    assert [a["target"] for a in env.audit("bucket.tag_sync")] == ["alice-new"]
    assert env.alice.post("/api/buckets/reconcile", headers=H).json() == {
        "synced": [], "pending": []}


def test_reconcile_touches_only_own_buckets(env):
    env.db.buckets.create("bob-shared", "bob", "WT-5", "bob", tag_synced=False)
    env.db.buckets.create("alice-empty", "alice", "WT-6", "alice", tag_synced=False)
    r = env.alice.post("/api/buckets/reconcile", headers=H)
    assert r.json() == {"synced": ["alice-empty"], "pending": []}
    assert env.tags("bob-shared") == {} and env.db.buckets.get("bob-shared")["tag_synced"] == 0
    assert env.used == [ALICE_KEY]


def test_tag_keeps_foreign_tags(env):
    env.db.buckets.create("alice-empty", "alice", "WT-6", "alice", tag_synced=False)
    env.moto().put_bucket_tagging(
        Bucket="alice-empty", Tagging={"TagSet": [{"Key": "team", "Value": "x"}]})
    env.alice.post("/api/buckets/reconcile", headers=H)
    assert env.tags("alice-empty") == {"team": "x", "bucketeer-budget": "WT-6"}


def test_no_access_key_means_clear_error_for_tag(env):
    env.croit_state.users["alice"].keys.clear()
    r = create(env.alice)
    # the bucket is created (croit is the admin plane); only the tag is deferred
    assert r.status_code == 201 and r.json()["tagSynced"] is False


# -- delete ------------------------------------------------------------------------


def test_delete_empty_bucket(env):
    r = env.alice.request("DELETE", "/api/buckets/alice-empty", headers=H)
    assert r.status_code == 200 and r.json() == {"name": "alice-empty"}
    assert "alice-empty" not in env.moto_buckets()
    assert "alice-empty" not in env.croit_state.buckets
    (entry,) = env.audit("bucket.delete")
    assert entry["target"] == "alice-empty"
    assert entry["details"]["objects"] == 0 and entry["details"]["dataDeleted"] is False


def test_delete_removes_sqlite_row(env):
    assert create(env.alice).status_code == 201
    assert env.alice.delete("/api/buckets/alice-new", headers=H).status_code == 200
    assert env.db.buckets.get("alice-new") is None


def test_delete_non_empty_requires_confirmation(env):
    r = env.alice.request("DELETE", "/api/buckets/alice-data", headers=H)
    assert r.status_code == 409
    assert r.json()["requiresConfirmation"] is True
    assert r.json()["objects"] == 42 and r.json()["bytes"] == 1048576
    assert "alice-data" in env.moto_buckets()
    for wrong in ("alice", "ALICE-DATA", "alice-data ", ""):
        r = env.alice.request("DELETE", "/api/buckets/alice-data", json={"confirmName": wrong},
                              headers=H)
        assert r.status_code == 409
    assert "alice-data" in env.moto_buckets()
    assert env.audit("bucket.delete") == []
    r = env.alice.request("DELETE", "/api/buckets/alice-data",
                          json={"confirmName": "alice-data"}, headers=H)
    assert r.status_code == 200
    assert "alice-data" not in env.moto_buckets()
    assert "alice-data" not in env.croit_state.buckets
    (entry,) = env.audit("bucket.delete")
    assert entry["details"]["objects"] == 42 and entry["details"]["dataDeleted"] is True


def test_delete_with_confirmation_name_works_for_empty_bucket_too(env):
    r = env.alice.request("DELETE", "/api/buckets/alice-empty",
                          json={"confirmName": "alice-empty"}, headers=H)
    assert r.status_code == 200 and "alice-empty" not in env.moto_buckets()


@pytest.mark.parametrize("name", ["bob-shared", "reports", "acme:reports", "Alice-Data",
                                  "ALICE-DATA", "nope-nope", "x", "alice-data%00"])
def test_delete_other_or_unknown_bucket_is_404(env, name):
    r = env.alice.request("DELETE", f"/api/buckets/{name}",
                          json={"confirmName": name}, headers=H)
    assert r.status_code == 404
    assert env.mutations() == []
    assert {"bob-shared", "reports", "alice-data"} <= env.moto_buckets()
    assert env.audit("bucket.delete") == []


def test_tenant_variations_cannot_cross(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    try:
        carol = e.session("carol")
        # a tenant user cannot reach the non-tenant "alice-data" nor "acme:" spellings
        for name in ("alice-data", "bob-shared", "acme:reports"):
            r = carol.request("DELETE", f"/api/buckets/{name}", json={"confirmName": name},
                              headers=H)
            assert r.status_code == 404
        assert e.mutations() == []
        # and the non-tenant user cannot delete the tenant's "reports"
        alice = e.alice
        assert alice.request("DELETE", "/api/buckets/reports", headers=H).status_code == 404
        assert e.mutations() == []
    finally:
        e.db.close()


# -- access control ----------------------------------------------------------------


def test_suspended_user_is_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get("/api/buckets").status_code == 403
    assert env.alice.get("/api/buckets/options").status_code == 403
    assert create(env.alice).status_code == 403
    assert env.alice.delete("/api/buckets/alice-empty", headers=H).status_code == 403
    assert env.alice.post("/api/buckets/reconcile", headers=H).status_code == 403
    assert env.mutations() == []


def test_unauthenticated_is_401(env):
    anon = env.anon()
    assert anon.get("/api/buckets").status_code == 401
    assert anon.post("/api/buckets", json={"name": "abc"}, headers=H).status_code == 401
    assert anon.delete("/api/buckets/alice-empty", headers=H).status_code == 401
    assert env.mutations() == []


def test_mutations_need_the_csrf_header(env):
    assert env.alice.post("/api/buckets", json={"name": "alice-new", "budgetNumber": "1"}
                          ).status_code == 403
    assert env.alice.delete("/api/buckets/alice-empty").status_code == 403
    assert env.alice.post("/api/buckets/reconcile").status_code == 403
    assert env.mutations() == []
    assert "alice-empty" in env.moto_buckets()


def test_unknown_body_fields_are_rejected(env):
    r = env.alice.post("/api/buckets", json={"name": "alice-new", "budgetNumber": "1",
                                              "owner": "bob"}, headers=H)
    assert r.status_code == 422
    assert env.mutations() == []


def test_croit_down_is_503(env):
    import httpx

    env.transport.fail = lambda req: httpx.ConnectError("down")
    assert env.alice.get("/api/buckets").status_code == 503


def test_s3_client_config(env):
    from bucketeer.s3client import default_factory

    client = default_factory("http://127.0.0.1:1", "AK", "SK", env.settings)
    cfg = client.meta.config
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert cfg.s3["addressing_style"] == "path" and client.meta.region_name == "us-east-1"
    assert cfg.connect_timeout and cfg.read_timeout
    assert "SK" not in repr(client) and "SK" not in repr(env.app.state.s3.__dict__)


def test_failing_task_output_never_reaches_the_response(env, caplog):
    marker = "SECRET-HOST-radosgw-admin-acme:private"
    real_view = env.croit_state.task_view

    def view(t):
        return {**real_view(t), "state": "FAILED", "statusCode": 1, "done": True,
                "output": marker}

    env.croit_state.task_view = view
    with caplog.at_level("WARNING", logger="bucketeer.buckets"):
        r = create(env.alice)
    assert r.status_code == 502
    assert marker not in r.text and "ask an administrator" in r.json()["detail"]
    assert marker in caplog.text  # kept for the operator


def test_create_adopts_own_unrecorded_bucket(env):
    env.croit_state.create_bucket("alice-late", "alice")  # created, but Bucketeer never heard
    assert env.db.buckets.get("alice-late") is None
    r = create(env.alice, name="alice-late", budget="WT-77")
    assert r.status_code == 201 and r.json()["adopted"] is True and "note" in r.json()
    row = env.db.buckets.get("alice-late")
    assert row["budget_number"] == "WT-77" and row["owner_uid"] == "alice"
    assert env.tags("alice-late") == {"bucketeer-budget": "WT-77"}
    assert env.mutations() == []  # croit was not asked to create anything
    assert env.audit("bucket.create")[0]["details"]["adopted"] is True


def test_create_does_not_adopt_a_bucket_recorded_for_someone_else(env):
    env.db.buckets.create("alice-empty", "bob", "WT-1", "bob")
    r = create(env.alice, name="alice-empty")
    assert r.status_code == 409
    assert env.db.buckets.get("alice-empty")["owner_uid"] == "bob"


def test_sqlite_failure_after_create_is_not_a_500(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.db.buckets, "create", boom)
    r = create(env.alice)
    assert r.status_code == 201 and "could not record" in r.json()["warning"]
    assert "alice-new" in env.croit_state.buckets


def test_set_tag_synced_failure_leaves_it_pending(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("locked")

    monkeypatch.setattr(env.db.buckets, "set_tag_synced", boom)
    r = create(env.alice)
    assert r.status_code == 201 and r.json()["tagSynced"] is False
    assert env.db.buckets.get("alice-new")["tag_synced"] == 0


def test_admin_default_placement_used_when_croit_placements_unreadable(env):
    import httpx

    env.db.settings.set("placement", {"default": "nvme-placement", "allowed": []})
    env.transport.fail = lambda req: (
        httpx.ConnectError("down") if req.url.path.endswith("/s3/placements") else None)
    r = create(env.alice)
    assert r.status_code == 201
    assert env.croit_state.buckets["alice-new"].placement == "nvme-placement"


def test_retyping_an_owned_recorded_bucket_is_409_and_changes_nothing(env):
    assert create(env.alice, name="alice-keep", budget="WT-1").status_code == 201
    row = env.db.buckets.get("alice-keep")
    r = create(env.alice, name="alice-keep", budget="WT-2")
    assert r.status_code == 409 and "already own" in r.json()["detail"]
    assert env.db.buckets.get("alice-keep") == row
    assert env.tags("alice-keep") == {"bucketeer-budget": "WT-1"}
    # an admin retyping without a budget must not null it either
    assert create(env.bob, name="bob-keep", budget="WT-9").status_code == 201
    row = env.db.buckets.get("bob-keep")
    r = env.bob.post("/api/buckets", json={"name": "bob-keep"}, headers=H)
    assert r.status_code == 409
    assert env.db.buckets.get("bob-keep") == row
    assert env.tags("bob-keep") == {"bucketeer-budget": "WT-9"}
