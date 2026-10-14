"""Admin budget-number setting (pattern + example) and the 1 TiB default quota."""

import pytest

from bucketeer import policy_settings as ps
from bucketeer import rgw_user
from bucketeer.budget import BUDGET_KEY

from .test_buckets import Env, H

S = "/api/admin/settings"
TIB = 2**40


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def put(env, body):
    return env.bob.put(f"{S}/budget-number", json=body, headers=H)


def audit(env):
    return [a for a in env.db.audit.query(limit=500)
            if a["action"] == "admin.settings.budget_number"]


# -- the setting ----------------------------------------------------------------------


def test_default_is_empty_and_gated(env):
    assert env.bob.get(S).json()["budgetNumber"] == {"pattern": "", "example": ""}
    for c in (env.alice, env.anon()):
        assert c.put(f"{S}/budget-number", json={}, headers=H).status_code in (401, 403)
        assert c.post(f"{S}/budget-number/test", json={}, headers=H).status_code in (401, 403)
    assert env.bob.put(f"{S}/budget-number", json={}).status_code == 403  # CSRF
    assert env.db.settings.get(BUDGET_KEY) is None


def test_save_roundtrip_and_audit(env):
    body = {"pattern": r" [0-9]{6}|WT-[A-Z]{3} ", "example": " 123456 "}
    r = put(env, body)
    assert r.status_code == 200
    assert r.json() == {"pattern": "[0-9]{6}|WT-[A-Z]{3}", "example": "123456"}
    assert env.bob.get(S).json()["budgetNumber"] == r.json()
    assert env.db.settings.get(BUDGET_KEY) == r.json()
    (row,) = audit(env)
    assert (row["actor"], row["target_type"], row["target"]) == ("bob", "setting", BUDGET_KEY)
    assert row["details"]["before"] == {"pattern": "", "example": ""}
    assert row["details"]["after"] == r.json()
    assert env.alice.get("/api/profile").json()["budgetExample"] == "123456"


@pytest.mark.parametrize("body", [
    {"pattern": "(a{60}){60}"},                       # static check: nested product
    {"pattern": "a{100000}"},                         # counted repeat too big
    {"pattern": "(unclosed"},                         # invalid regex
    {"pattern": "x" * 201},                           # too long
    {"pattern": "(?x) a b"},                          # verbose mode refused
    {"pattern": "[0-9]+ # c"},                        # comments refused
    {"pattern": 5}, {"example": 5}, {"other": "x"}, {"pattern": ["a"]},
])
def test_unsafe_or_bad_patterns_rejected(env, body):
    r = put(env, body)
    assert r.status_code == 422, body
    assert env.db.settings.get(BUDGET_KEY) is None and audit(env) == []


def test_not_an_object(env):
    assert env.bob.put(f"{S}/budget-number", json=["x"], headers=H).status_code == 422


@pytest.mark.parametrize(("pattern", "example"), [
    ("[0-9]{6}", "12345"),       # does not match the pattern
    ("", "bad#value"),           # base charset
    ("", "ünï"),
    ("", "x" * 65),
    ("[0-9]+", "12 3"),
])
def test_example_must_pass_base_rules_and_pattern(env, pattern, example):
    r = put(env, {"pattern": pattern, "example": example})
    assert r.status_code == 422 and "example" in r.json()["detail"].lower()
    assert env.db.settings.get(BUDGET_KEY) is None


def test_example_without_pattern_and_pattern_without_example(env):
    assert put(env, {"example": "WT-ABC"}).status_code == 200
    assert put(env, {"pattern": "[0-9]{6}"}).json() == {"pattern": "[0-9]{6}", "example": ""}
    assert env.alice.get("/api/profile").json()["budgetExample"] is None


def test_dry_run_test_endpoint_stores_nothing(env):
    def t(rules, value):
        return env.bob.post(f"{S}/budget-number/test", headers=H,
                            json={"rules": rules, "value": value}).json()

    assert t({"pattern": "[0-9]{6}"}, "123456") == {"rulesError": None, "error": None, "ok": True}
    r = t({"pattern": "[0-9]{6}"}, "12")
    assert r["ok"] is False and "pattern" in r["error"]
    assert t({"pattern": ""}, "bad#")["ok"] is False  # base rules still apply
    assert t({"pattern": "(unclosed"}, "1")["rulesError"]
    assert t({"pattern": "[0-9]+", "example": "abc"}, "1")["rulesError"]
    assert env.db.settings.get(BUDGET_KEY) is None and audit(env) == []
    r = env.bob.post(f"{S}/budget-number/test", json={"rules": {}}, headers=H)
    assert r.status_code == 422


# -- enforcement through the APIs ---------------------------------------------------------


def test_profile_and_create_enforce_pattern_and_show_example(env):
    put(env, {"pattern": r"[0-9]{6}|WT-[A-Z]+", "example": "123456"})
    p = env.alice.put("/api/profile", json={"defaultBudgetNumber": "12"}, headers=H)
    assert p.status_code == 422 and "Example: 123456" in p.json()["detail"]
    ok = env.alice.put("/api/profile", json={"defaultBudgetNumber": "WT-ABC"}, headers=H)
    assert ok.status_code == 200 and ok.json()["budgetExample"] == "123456"
    assert env.alice.get("/api/buckets/options").json()["budgetExample"] == "123456"
    bad = env.alice.post("/api/buckets", json={"name": "alice-new", "budgetNumber": "nope"},
                         headers=H)
    assert bad.status_code == 422 and "format" in bad.json()["detail"]
    assert "alice-new" not in env.moto_buckets()
    good = env.alice.post("/api/buckets", json={"name": "alice-new", "budgetNumber": "654321"},
                          headers=H)
    assert good.status_code == 201


def test_unsafe_stored_pattern_is_misconfigured_not_executed(env):
    env.db.settings.set(BUDGET_KEY, {"pattern": "(a{60}){60}", "example": ""})
    r = env.alice.put("/api/profile", json={"defaultBudgetNumber": "aaa"}, headers=H)
    assert r.status_code == 422 and "misconfigured" in r.json()["detail"]


def test_empty_pattern_means_base_rules_only(env):
    put(env, {"pattern": "[0-9]{6}", "example": "123456"})
    put(env, {"pattern": "", "example": ""})
    assert env.alice.put("/api/profile", json={"defaultBudgetNumber": "anything goes 1"},
                         headers=H).status_code == 200
    assert env.alice.put("/api/profile", json={"defaultBudgetNumber": "bad#"},
                         headers=H).status_code == 422
    assert env.alice.get("/api/profile").json()["budgetExample"] is None
    assert env.alice.get("/api/buckets/options").json()["budgetExample"] is None
    assert len(audit(env)) == 2


def test_pattern_change_is_not_retroactive_and_admin_buckets_flag_mismatch(env):
    env.db.buckets.create("alice-data", "alice", "OLD-1")
    env.db.buckets.create("bob-shared", "bob", "123456")
    rows = {b["name"]: b for b in env.bob.get("/api/admin/buckets").json()["buckets"]}
    assert rows["alice-data"]["budgetMatches"] is None  # no pattern yet
    put(env, {"pattern": "[0-9]{6}", "example": "123456"})
    rows = {b["name"]: b for b in env.bob.get("/api/admin/buckets").json()["buckets"]}
    assert rows["alice-data"]["budgetNumber"] == "OLD-1"  # kept
    assert rows["alice-data"]["budgetMatches"] is False
    assert rows["bob-shared"]["budgetMatches"] is True
    assert env.db.buckets.get("alice-data")["budget_number"] == "OLD-1"
    missing = [b for b in rows.values() if b["budgetNumber"] is None]
    assert all(b["budgetMatches"] is None for b in missing)


# -- default quota ------------------------------------------------------------------------


def test_default_quota_effective_is_1_tib_when_unset(env):
    q = env.bob.get(S).json()["defaultQuota"]
    assert q["isSet"] is False and q["quota"] is None
    assert q["effective"] == {"enabled": True, "maxSize": TIB, "maxObjects": None}
    assert q["default"] == q["effective"]
    assert ps.effective_default_quota(env.db.settings) == q["default"]
    # a copy: callers cannot mutate the built-in
    ps.effective_default_quota(env.db.settings)["maxSize"] = 1
    assert ps.DEFAULT_QUOTA["maxSize"] == TIB


def test_default_quota_single_source_of_truth():
    assert not hasattr(rgw_user, "DEFAULT_QUOTA")
    assert rgw_user.effective_default_quota is ps.effective_default_quota


def test_explicit_unlimited_differs_from_unset(env):
    r = env.bob.put(f"{S}/default-quota", json={"enabled": False}, headers=H).json()
    assert r["isSet"] is True and r["effective"]["enabled"] is False
    assert ps.effective_default_quota(env.db.settings)["enabled"] is False
    r = env.bob.delete(f"{S}/default-quota", headers=H).json()
    assert r["isSet"] is False and r["effective"]["maxSize"] == TIB
    set_q = env.bob.put(f"{S}/default-quota", json={"enabled": True, "maxSize": 5}, headers=H)
    assert set_q.json()["effective"]["maxSize"] == 5


def test_backtracking_pattern_rejected_at_save(env):
    r = put(env, {"pattern": "(A|A)+Z", "example": ""})
    assert r.status_code == 422 and "slow" in r.json()["detail"]
    assert env.db.settings.get(BUDGET_KEY) is None


def test_admin_buckets_matching_is_cached_and_stops_after_timeouts(env, monkeypatch):
    from bucketeer import admin
    put(env, {"pattern": "[0-9]{6}", "example": ""})
    for i in range(10):
        env.db.buckets.create(f"x{i}", "alice", f"N-{i}")
    env.db.buckets.create("dup1", "alice", "SAME")
    env.db.buckets.create("dup2", "alice", "SAME")
    calls = []

    def slow(value, pattern):
        calls.append(value)
        raise TimeoutError

    monkeypatch.setattr(admin, "budget_matches", slow)
    # croit lists only its own buckets, so seed meta for them plus the shared value
    for name in ("alice-data", "alice-empty", "bob-shared", "acme:reports"):
        env.db.buckets.create(name, "alice", "SAME")
    rows = env.bob.get("/api/admin/buckets").json()["buckets"]
    assert all(b["budgetMatches"] is None for b in rows)
    assert calls == ["SAME"]  # one distinct value in croit's list, cached per value
