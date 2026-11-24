"""Bucket facts as managed tags (P6-2): builder, create/budget/reconcile/quota writes, and the
admin action "Write Bucketeer tags to all buckets". Runs on both admin backends."""

from datetime import UTC, datetime

import pytest

from bucketeer import bucket_facts as bf

from .backends import admin_backend  # noqa: F401
from .test_buckets import ALICE_KEY, BOB_KEY, CAROL_KEY, Env, H

TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def create(client, name="alice-new", budget="WT-123"):
    return client.post("/api/buckets", json={"name": name, "budgetNumber": budget}, headers=H)


# -- pure helpers ---------------------------------------------------------------------


def test_build_values_and_unknowns():
    f = bf.build(bucket="acme:reports", owner_uid="acme$carol", budget="DN1 AC2",
                 set_budget=True, owner_email="Carol.C@example.org", creator="carol",
                 created="2026-11-24T10:00:00.000Z", quota="10")
    assert f == {"bucketeer-budget": "DN1 AC2", "bucketeer-bucket": "acme:reports",
                 "bucketeer-owner": "acme:carol", "bucketeer-owner-email": "Carol.C@example.org",
                 "bucketeer-creator": "carol", "bucketeer-created": "2026-11-24",
                 "bucketeer-quota": "10", "bucketeer-product": "Tier-3-S3",
                 "bucketeer-location": "onprem"}
    # unknown facts are left as stored; without set_budget the budget is untouched
    g = bf.build(bucket="b", owner_uid="u")
    assert set(g) == {"bucketeer-bucket", "bucketeer-owner", "bucketeer-product",
                      "bucketeer-location"}
    assert bf.build(bucket="b", owner_uid="u", set_budget=True)["bucketeer-budget"] is None


def test_values_outside_the_tag_alphabet_are_left_out(caplog):
    f = bf.build(bucket="b", owner_uid="u", owner_email="a#b@example.org",
                 creator="x" * 257)
    assert "bucketeer-owner-email" not in f and "bucketeer-creator" not in f
    assert "not allowed in an S3 tag" in caplog.text


def test_quota_value():
    assert bf.quota_value(None) == "unlimited"
    assert bf.quota_value({"enabled": False, "maxSize": 2**41}) == "unlimited"
    assert bf.quota_value({"enabled": True, "maxSize": -1}) == "unlimited"
    assert bf.quota_value({"enabled": True, "maxSize": 2 * bf.TIB}) == "2"
    assert bf.quota_value({"enabled": True, "maxSize": 1024}) == "1"  # rounded up
    assert bf.quota_value({"enabled": True, "maxSize": 3 * bf.TIB + 1}) == "4"


def test_merge_keeps_user_tags_and_respects_the_limit():
    tags = [{"Key": "team", "Value": "x"}, {"Key": "bucketeer-budget", "Value": "OLD"}]
    out = bf.merge(tags, {"bucketeer-budget": "NEW", "bucketeer-owner": "alice"})
    assert out == [{"Key": "team", "Value": "x"}, {"Key": "bucketeer-budget", "Value": "NEW"},
                   {"Key": "bucketeer-owner", "Value": "alice"}]
    assert bf.merge(tags, {"bucketeer-budget": None}) == [{"Key": "team", "Value": "x"}]
    full = [{"Key": f"k{i}", "Value": ""} for i in range(49)]
    out = bf.merge(full, {"bucketeer-budget": "B", "bucketeer-owner": "alice"})
    assert len(out) == 50 and out[-1] == {"Key": "bucketeer-budget", "Value": "B"}


# -- writes on create, budget change, reconcile --------------------------------------------


def test_create_writes_every_fact_with_the_owners_key(env):
    assert create(env.alice).status_code == 201
    assert env.all_tags("alice-new") == {
        "bucketeer-budget": "WT-123", "bucketeer-bucket": "alice-new",
        "bucketeer-owner": "alice", "bucketeer-owner-email": "alice@example.org",
        "bucketeer-creator": "alice", "bucketeer-created": TODAY,
        "bucketeer-quota": "unlimited", "bucketeer-product": "Tier-3-S3",
        "bucketeer-location": "onprem"}
    assert set(env.used) == {ALICE_KEY}
    assert env.db.buckets.get("alice-new")["tag_synced"] == 1


def test_tenant_owner_and_bucket_are_colon_qualified(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    try:
        carol = e.session("carol")
        assert create(carol, name="carol-new").status_code == 201
        t = e.all_tags("carol-new")
        assert t["bucketeer-bucket"] == "acme:carol-new" and t["bucketeer-owner"] == "acme:carol"
        assert set(e.used) == {CAROL_KEY}
    finally:
        e.db.close()


def test_admin_create_without_budget_still_writes_facts(env):
    assert create(env.bob, name="bob-new", budget=None).status_code == 201
    t = env.all_tags("bob-new")
    assert "bucketeer-budget" not in t and t["bucketeer-owner"] == "bob"
    assert t["bucketeer-creator"] == "bob"
    assert env.db.buckets.get("bob-new")["tag_synced"] == 1


def test_unreadable_quota_leaves_the_row_unsynced(env, monkeypatch):
    async def none(*a, **k):
        return None
    monkeypatch.setattr(bf, "bucket_row", none)
    r = create(env.alice)
    assert r.status_code == 201 and r.json()["tagSynced"] is False
    t = env.all_tags("alice-new")
    assert "bucketeer-quota" not in t and t["bucketeer-budget"] == "WT-123"
    assert env.db.buckets.get("alice-new")["tag_synced"] == 0
    monkeypatch.undo()
    r = env.alice.post("/api/buckets/reconcile", headers=H)
    assert r.json()["synced"] == ["alice-new"]
    assert env.all_tags("alice-new")["bucketeer-quota"] == "unlimited"


def test_budget_change_refreshes_facts_and_keeps_user_tags(env):
    assert create(env.alice).status_code == 201
    env.moto().put_bucket_tagging(Bucket="alice-new", Tagging={"TagSet": [
        {"Key": "team", "Value": "x"}, {"Key": "bucketeer-owner", "Value": "mallory"}]})
    r = env.alice.put("/api/buckets/alice-new/budget", json={"budgetNumber": "WT-9"}, headers=H)
    assert r.status_code == 200 and r.json()["tagSynced"] is True
    t = env.all_tags("alice-new")
    assert t["team"] == "x" and t["bucketeer-owner"] == "alice"  # a reconcile rewrites
    assert t["bucketeer-budget"] == "WT-9" and t["bucketeer-created"] == TODAY


def test_reconcile_writes_facts_after_a_failed_create(env):
    env.break_tagging()
    r = create(env.alice)
    assert r.status_code == 201 and r.json()["tagSynced"] is False
    env.fix_tagging()
    assert env.alice.get("/api/buckets").json()["buckets"]
    r = env.alice.post("/api/buckets/reconcile", headers=H)
    assert r.json() == {"synced": ["alice-new"], "pending": []}
    assert env.all_tags("alice-new")["bucketeer-owner-email"] == "alice@example.org"


def test_admin_quota_change_updates_the_quota_tag(env):
    assert create(env.alice).status_code == 201
    env.used.clear()
    r = env.bob.put("/api/admin/buckets/alice-new/quota",
                    json={"enabled": True, "maxSize": 3 * bf.TIB, "maxObjects": None},
                    headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["tagSynced"] is True
    t = env.all_tags("alice-new")
    assert t["bucketeer-quota"] == "3"
    assert t["bucketeer-owner-email"] == "alice@example.org"  # kept: unknown to the admin
    assert set(env.used) == {ALICE_KEY}  # the owner's key, never the admin's


# -- admin: write the tags of every bucket ----------------------------------------------


def write_all(client, headers=H):
    return client.post("/api/admin/buckets/write-tags", headers=headers)


def test_write_all_tags_every_bucket_with_its_owners_key(env):
    r = write_all(env.bob)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["failed"] == 0 and j["truncated"] is False
    assert j["updated"] >= 3
    t = env.all_tags("alice-data")
    assert t["bucketeer-owner"] == "alice" and t["bucketeer-bucket"] == "alice-data"
    assert t["bucketeer-quota"] == "unlimited" and t["bucketeer-location"] == "onprem"
    assert "bucketeer-budget" not in t and "bucketeer-created" not in t  # no row: unknown
    assert env.all_tags("bob-shared")["bucketeer-owner"] == "bob"
    assert {ALICE_KEY, BOB_KEY} <= set(env.used)
    (a,) = env.audit("admin.buckets.write_tags")
    assert a["actor"] == "bob" and a["target"] == "*" and a["details"]["updated"] == j["updated"]
    # safe to repeat: nothing changes the second time
    again = write_all(env.bob).json()
    assert again["updated"] == 0 and again["unchanged"] == j["updated"]


def test_write_all_skips_suspended_owners_and_marks_rows_synced(env):
    assert create(env.alice).status_code == 201
    env.db.buckets.set_tag_synced("alice-new", False)
    env.moto().delete_bucket_tagging(Bucket="alice-new")
    env.croit_state.users["bob"].suspended = True
    env.app.state.croit.invalidate_lists()
    j = write_all(env.bob).json()
    skipped = {s["bucket"]: s["reason"] for s in j["details"]["skipped"]}
    assert skipped.get("bob-shared") == "owner_suspended"
    assert "bucketeer-owner" not in env.all_tags("bob-shared")
    t = env.all_tags("alice-new")
    assert t["bucketeer-budget"] == "WT-123" and t["bucketeer-creator"] == "alice"
    assert t["bucketeer-created"] == TODAY
    assert env.db.buckets.get("alice-new")["tag_synced"] == 1


def test_write_all_is_admin_only_and_needs_csrf(env):
    assert write_all(env.alice).status_code == 403
    assert write_all(env.bob, headers={}).status_code == 403
    assert env.audit("admin.buckets.write_tags") == []
