"""/api/profile: auth, CSRF, validation, per-user storage, audit, admin pattern."""

import pytest

from bucketeer.budget import BUDGET_KEY, BudgetNumberError, validate_budget_number

from .test_credentials import Env, H


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    yield e
    e.db.close()


def put(client, value, headers=H):
    return client.put("/api/profile", json={"defaultBudgetNumber": value}, headers=headers)


def audit(env):
    return [a for a in env.db.audit.query(limit=1000) if a["action"] == "profile.update"]


def test_auth_required(env):
    anon = env.anon()
    assert anon.get("/api/profile").status_code == 401
    assert put(anon, "X1").status_code == 401


def test_csrf_required(env):
    assert env.alice.put("/api/profile", json={"defaultBudgetNumber": "X1"}).status_code == 403
    assert env.alice.get("/api/profile").status_code == 200


def test_get_defaults(env):
    r = env.alice.get("/api/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["defaultBudgetNumber"] is None
    assert set(body) == {"username", "displayName", "email", "defaultBudgetNumber"}
    assert r.headers["cache-control"] == "no-store"


def test_save_trim_and_persist(env):
    r = put(env.alice, "  IDX-123/45  ")
    assert r.status_code == 200 and r.json()["defaultBudgetNumber"] == "IDX-123/45"
    assert env.alice.get("/api/profile").json()["defaultBudgetNumber"] == "IDX-123/45"
    assert env.db.profiles.get("alice")["default_budget_number"] == "IDX-123/45"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_clear(env, value):
    put(env.alice, "X1")
    r = put(env.alice, value)
    assert r.status_code == 200 and r.json()["defaultBudgetNumber"] is None
    assert env.alice.get("/api/profile").json()["defaultBudgetNumber"] is None


@pytest.mark.parametrize("value", ["A", "a b", "WT-1_2.3/4:5+6=7@x", "x" * 64, "0123"])
def test_valid_values(env, value):
    assert put(env.alice, value).status_code == 200


@pytest.mark.parametrize(
    "value",
    ["x" * 65, "a\tb", "a\nb", "a\x00b", "a\x7fb", "<b>", "a;b", "a'b", "ünï",
     "é", "a,b", "a#b", "#", "a​b"],
)
def test_invalid_values(env, value):
    r = put(env.alice, value)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert env.alice.get("/api/profile").json()["defaultBudgetNumber"] is None


def test_control_char_message(env):
    assert "control" in put(env.alice, "a\nb").json()["detail"]


def test_bad_body(env):
    r = env.alice.put("/api/profile", json={"defaultBudgetNumber": 5}, headers=H)
    assert r.status_code == 422
    assert env.alice.put("/api/profile", json={"username": "bob"}, headers=H).status_code == 422
    assert env.alice.put("/api/profile", content=b"nope", headers=H).status_code == 422


def test_isolation_between_users(env):
    bob = env.session("bob")
    put(env.alice, "ALICE-1")
    assert bob.get("/api/profile").json()["defaultBudgetNumber"] is None
    put(bob, "BOB-1")
    assert env.alice.get("/api/profile").json()["defaultBudgetNumber"] == "ALICE-1"
    # identity comes from the session only: a username in the request is refused/ignored
    assert bob.get("/api/profile?username=alice").json()["username"] == "bob"
    assert bob.put("/api/profile", json={"defaultBudgetNumber": "Z", "username": "alice"},
                   headers=H).status_code == 422
    assert env.db.profiles.get("alice")["default_budget_number"] == "ALICE-1"


def test_audit(env):
    put(env.alice, "A-1")
    put(env.alice, "A-1")  # unchanged: no new entry
    put(env.alice, "A-2")
    put(env.alice, "")
    put(env.alice, "bad;")  # rejected: no entry
    rows = audit(env)
    assert [(r["details"]["old"], r["details"]["new"]) for r in reversed(rows)] == [
        (None, "A-1"), ("A-1", "A-2"), ("A-2", None)]
    assert all(r["actor"] == "alice" and r["target"] == "alice" for r in rows)


def test_pattern_setting_applied(env):
    env.db.settings.set(BUDGET_KEY, {"pattern": r"[0-9]{6}|WT-[A-Z]+", "example": "123456"})
    assert put(env.alice, "123456").status_code == 200
    assert put(env.alice, "WT-ABC").status_code == 200
    r = put(env.alice, "12345")
    assert r.status_code == 422 and "Example: 123456" in r.json()["detail"]
    assert put(env.alice, "").status_code == 200  # clearing is always allowed


def test_pattern_unsafe_refused(env):
    env.db.settings.set(BUDGET_KEY, {"pattern": "(a{60}){60}"})
    r = put(env.alice, "aaaa")
    assert r.status_code == 422 and "misconfigured" in r.json()["detail"]


def test_pure_function(env):
    s = env.db.settings
    assert validate_budget_number(" X ", s) == "X"
    assert validate_budget_number(None, s) is None
    with pytest.raises(BudgetNumberError):
        validate_budget_number(5, s)
    s.set(BUDGET_KEY, "garbage")  # malformed setting is ignored
    assert validate_budget_number("X", s) == "X"


def test_allowed_set_is_s3_tag_subset():
    # S3 tag values: letters, digits, space and + - = . _ : / @ (we are stricter: ASCII)
    s3 = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 +-=._:/@")
    for code in range(0x110):
        ch = chr(code)
        try:
            ok = validate_budget_number(ch, _Store()) is not None
        except BudgetNumberError:
            ok = False
        if ok:
            assert ch in s3


class _Store:
    def get(self, key, default=None):
        return default
