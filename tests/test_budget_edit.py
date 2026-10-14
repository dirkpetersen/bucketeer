"""PUT /api/buckets/{name}/budget: change the budget number later (SQLite row + managed tag)."""

import pytest

from .test_buckets import ALICE_KEY, Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    r = e.alice.post("/api/buckets", json={"name": "alice-new", "budgetNumber": "WT-123"},
                     headers=H)
    assert r.status_code == 201, r.text
    e.used.clear()
    yield e
    e.db.close()


def edit(client, value, name="alice-new", headers=H):
    return client.put(f"/api/buckets/{name}/budget", json={"budgetNumber": value},
                      headers=headers)


def test_change_updates_row_tag_and_audit(env):
    r = edit(env.alice, "  WT-999 ")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "alice-new", "budgetNumber": "WT-999", "changed": True,
                        "tagSynced": True}
    row = env.db.buckets.get("alice-new")
    assert row["budget_number"] == "WT-999" and row["tag_synced"] == 1
    assert env.tags("alice-new") == {"bucketeer-budget": "WT-999"}
    assert set(env.used) == {ALICE_KEY}
    a = env.audit("bucket.budget")
    assert len(a) == 1 and a[0]["target"] == "alice-new"
    assert a[0]["details"]["old"] == "WT-123" and a[0]["details"]["new"] == "WT-999"
    b = next(b for b in env.alice.get("/api/buckets").json()["buckets"]
             if b["name"] == "alice-new")
    assert b["budgetNumber"] == "WT-999" and b["budgetMissing"] is False


def test_same_value_is_a_no_op(env):
    r = edit(env.alice, "WT-123")
    assert r.status_code == 200 and r.json()["changed"] is False
    assert env.audit("bucket.budget") == [] and env.used == []


@pytest.mark.parametrize("bad", ["bad#", "x" * 65, "ä", "a\nb"])
def test_invalid_is_422(env, bad):
    assert edit(env.alice, bad).status_code == 422
    assert env.db.buckets.get("alice-new")["budget_number"] == "WT-123"
    assert env.audit("bucket.budget") == []


def test_pattern_enforced(env):
    env.db.settings.set("budget_number", {"pattern": r"WT-[0-9]{3}", "example": "WT-123"})
    r = edit(env.alice, "nope")
    assert r.status_code == 422 and "WT-123" in r.json()["detail"]
    assert edit(env.alice, "WT-555").status_code == 200


def test_non_admin_cannot_clear(env):
    for v in (None, "", "   "):
        r = edit(env.alice, v)
        assert r.status_code == 422, v
    assert env.db.buckets.get("alice-new")["budget_number"] == "WT-123"


def test_admin_may_clear_and_tag_is_removed(env):
    r = env.bob.post("/api/buckets", json={"name": "bob-new", "budgetNumber": "WT-1"}, headers=H)
    assert r.status_code == 201
    r = edit(env.bob, None, name="bob-new")
    assert r.status_code == 200, r.text
    assert r.json()["budgetNumber"] is None and r.json()["tagSynced"] is True
    assert env.db.buckets.get("bob-new")["budget_number"] is None
    assert env.tags("bob-new") == {}
    a = env.audit("bucket.budget")[0]["details"]
    assert a["old"] == "WT-1" and a["new"] is None


def test_other_users_bucket_404_and_bad_names(env):
    assert edit(env.alice, "WT-1", name="bob-shared").status_code == 404
    assert edit(env.alice, "WT-1", name="Bad:Name").status_code == 404
    assert env.db.buckets.get("bob-shared") is None


def test_suspended_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert edit(env.alice, "WT-9").status_code == 403


def test_csrf_and_login(env):
    assert edit(env.alice, "WT-9", headers={}).status_code == 403
    assert edit(env.anon(), "WT-9").status_code in (401, 403)


def test_unknown_fields_422(env):
    r = env.alice.put("/api/buckets/alice-new/budget", json={"budgetNumber": "WT-9", "x": 1},
                      headers=H)
    assert r.status_code == 422


def test_tag_failure_sets_pending_and_reconcile_fixes(env):
    env.break_tagging()
    r = edit(env.alice, "WT-777")
    assert r.status_code == 200
    j = r.json()
    assert j["tagSynced"] is False and "retried" in j["warning"]
    row = env.db.buckets.get("alice-new")
    assert row["budget_number"] == "WT-777" and row["tag_synced"] == 0
    assert env.tags("alice-new") == {"bucketeer-budget": "WT-123"}
    assert env.audit("bucket.budget")[0]["details"]["tag_synced"] is False
    b = next(b for b in env.alice.get("/api/buckets").json()["buckets"]
             if b["name"] == "alice-new")
    assert b["tagPending"] is True
    assert env.alice.post("/api/buckets/reconcile", headers=H).json() == {
        "synced": [], "pending": ["alice-new"]}
    env.fix_tagging()
    assert env.alice.post("/api/buckets/reconcile", headers=H).json() == {
        "synced": ["alice-new"], "pending": []}
    assert env.tags("alice-new") == {"bucketeer-budget": "WT-777"}
    # an unchanged value with an unsynced tag retries the tag
    env.db.buckets.set_tag_synced("alice-new", False)
    r = edit(env.alice, "WT-777")
    assert r.json()["changed"] is False and r.json()["tagSynced"] is True
    assert env.db.buckets.get("alice-new")["tag_synced"] == 1


def test_clear_with_tag_failure_reconciles_removal(env):
    r = env.bob.post("/api/buckets", json={"name": "bob-new", "budgetNumber": "WT-1"}, headers=H)
    assert r.status_code == 201
    env.break_tagging()
    r = edit(env.bob, None, name="bob-new")
    assert r.json()["tagSynced"] is False
    env.fix_tagging()
    assert env.bob.post("/api/buckets/reconcile", headers=H).json()["synced"] == ["bob-new"]
    assert env.tags("bob-new") == {}


def test_keeps_foreign_tags(env):
    env.moto().put_bucket_tagging(Bucket="alice-new", Tagging={"TagSet": [
        {"Key": "team", "Value": "x"}, {"Key": "bucketeer-budget", "Value": "WT-123"}]})
    assert edit(env.alice, "WT-2").status_code == 200
    assert env.tags("alice-new") == {"team": "x", "bucketeer-budget": "WT-2"}


def test_reconcile_holds_the_user_lock_and_uses_the_fresh_row(env):
    """Reconcile runs under the per-user lock (the budget edit takes the same one) and writes
    the row as it is inside the lock, not the stale one it listed earlier."""
    from unittest.mock import patch

    from bucketeer import buckets
    env.db.buckets.set_budget_number("alice-new", "WT-AAA")
    env.db.buckets.set_tag_synced("alice-new", False)
    stale = [dict(env.db.buckets.get("alice-new"))]
    # ... then the user saves BBB (row changes, still unsynced for this test)
    env.db.buckets.set_budget_number("alice-new", "WT-BBB")
    real = buckets._write_budget_tag
    held = []

    def spy(s3, bucket, value):
        held.append(env.app.state.bucket_locks["alice"].locked())
        return real(s3, bucket, value)
    with patch.object(buckets, "_write_budget_tag", spy), \
            patch.object(env.db.buckets, "list", lambda owner=None: stale):
        r = env.alice.post("/api/buckets/reconcile", headers=H)
    assert r.json()["synced"] == ["alice-new"]
    assert held == [True]
    assert env.tags("alice-new") == {"bucketeer-budget": "WT-BBB"}
    assert env.db.buckets.get("alice-new")["tag_synced"] == 1


def test_reconcile_skips_row_synced_meanwhile(env):
    env.db.buckets.set_tag_synced("alice-new", False)
    from unittest.mock import patch

    from bucketeer import buckets
    real = env.db.buckets.get

    def get_and_sync(name):
        env.db.buckets.set_tag_synced(name, True)  # someone synced it between list and lock
        return real(name)
    with patch.object(env.db.buckets, "get", get_and_sync):
        r = env.alice.post("/api/buckets/reconcile", headers=H)
    assert r.json() == {"synced": [], "pending": []} and env.used == []
    assert buckets.BUDGET_TAG
