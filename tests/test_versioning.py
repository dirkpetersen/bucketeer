"""PUT /api/buckets/{name}/versioning against the mock croit and moto."""

import pytest

from .backends import admin_backend, on_rgw  # noqa: F401
from .test_buckets import ALICE_KEY, Env, H, create


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def put(client, name, enabled, headers=H):
    return client.put(f"/api/buckets/{name}/versioning", json={"enabled": enabled},
                      headers=headers)


def moto_status(env, name):
    return env.moto().get_bucket_versioning(Bucket=name).get("Status")


def listed(env, name):
    return next(b for b in env.alice.get("/api/buckets").json()["buckets"] if b["name"] == name)


def test_enable_suspend_reenable(env):
    assert listed(env, "alice-data")["versioning"] == "off"
    r = put(env.alice, "alice-data", True)
    assert r.status_code == 200 and r.json() == {
        "name": "alice-data", "versioning": "enabled", "changed": True}
    assert env.croit_state.buckets["alice-data"].versioning == "ENABLED"
    assert moto_status(env, "alice-data") == "Enabled"
    assert listed(env, "alice-data")["versioning"] == "enabled"

    r = put(env.alice, "alice-data", False)
    assert r.json()["versioning"] == "suspended" and r.json()["changed"] is True
    assert moto_status(env, "alice-data") == "Suspended"
    assert listed(env, "alice-data")["versioning"] == "suspended"

    assert put(env.alice, "alice-data", True).json()["versioning"] == "enabled"
    assert moto_status(env, "alice-data") == "Enabled"
    assert [a["details"]["before"] + ">" + a["details"]["after"]
            for a in reversed(env.audit("bucket.versioning"))] == [
        "off>enabled", "enabled>suspended", "suspended>enabled"]


def test_idempotent_makes_no_croit_write(env):
    assert put(env.alice, "alice-data", True).json()["changed"] is True
    n = len(env.mutations())
    r = put(env.alice, "alice-data", True)
    assert r.status_code == 200 and r.json()["changed"] is False
    # never-versioned stays off when asked to be off; suspended stays suspended
    assert put(env.alice, "alice-empty", False).json() == {
        "name": "alice-empty", "versioning": "off", "changed": False}
    put(env.alice, "alice-data", False)
    n = len(env.mutations())
    assert put(env.alice, "alice-data", False).json()["changed"] is False
    assert len(env.mutations()) == n
    assert len(env.audit("bucket.versioning")) == 2


def test_object_lock_bucket_cannot_be_suspended(env):
    assert create(env.alice, name="alice-lock", objectLock=True).status_code == 201
    assert listed(env, "alice-lock")["objectLock"] is True
    assert listed(env, "alice-data")["objectLock"] is False
    n = len(env.mutations())
    r = put(env.alice, "alice-lock", False)
    assert r.status_code == 409 and "object lock" in r.json()["detail"]
    assert len(env.mutations()) == n
    assert env.croit_state.buckets["alice-lock"].versioning == "ENABLED"
    assert put(env.alice, "alice-lock", True).json()["changed"] is False
    assert env.audit("bucket.versioning") == []
    assert set(env.used) == {ALICE_KEY}


def test_other_users_bucket_is_404(env):
    assert put(env.alice, "bob-shared", True).status_code == 404
    assert put(env.alice, "nope-nothing", True).status_code == 404
    assert put(env.alice, "Bad:Name", True).status_code == 404
    assert env.croit_state.buckets["bob-shared"].versioning == "OFF"
    assert env.audit("bucket.versioning") == []


def test_suspended_user_is_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert put(env.alice, "alice-data", True).status_code == 403
    assert env.croit_state.buckets["alice-data"].versioning == "OFF"


def test_needs_login_csrf_and_valid_body(env):
    assert put(env.alice, "alice-data", True, headers={}).status_code == 403
    assert put(env.anon(), "alice-data", True).status_code in (401, 403)
    r = env.alice.put("/api/buckets/alice-data/versioning", json={"enabled": "x", "z": 1},
                      headers=H)
    assert r.status_code == 422
    assert env.croit_state.buckets["alice-data"].versioning == "OFF"
    assert env.audit("bucket.versioning") == []
