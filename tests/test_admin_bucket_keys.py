"""/api/admin/bucket-keys: list every bucket key and revoke any of them as an admin."""

import json

import pytest

from .test_bucket_keys import break_policy_writes, mk, put_foreign, sids
from .test_bucket_keys import env as env  # noqa: F401  (fixture)
from .test_buckets import H

LIST = "/api/admin/bucket-keys"


def revoke(env, key_id, client=None):
    return (client or env.bob).delete(f"{LIST}/{key_id}", headers=H)


def two_keys(env):
    k1 = mk(env.alice, label="ci").json()
    k2 = mk(env.alice, bucket="alice-data", label="rw", access="readwrite").json()
    return k1, k2


def test_needs_a_session_and_the_admin_role(env):
    for method, path in (("GET", LIST), ("DELETE", f"{LIST}/1")):
        assert env.anon().request(method, path, headers=H).status_code == 401
        assert env.alice.request(method, path, headers=H).status_code == 403
    assert env.alice.get(LIST).status_code == 403


def test_delete_needs_csrf_and_changes_nothing(env):
    k1, _ = two_keys(env)
    n = len(env.transport.mutations())
    r = env.bob.delete(f"{LIST}/{k1['id']}")
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    assert len(env.transport.mutations()) == n
    assert env.db.bucket_keys.get(k1["id"])["revoked_at"] is None


def test_list_shape_filters_and_no_secrets(env):
    k1, k2 = two_keys(env)
    put_foreign(env)  # not touched by the listing
    assert revoke(env, k2["id"]).status_code == 200
    r = env.bob.get(LIST)
    assert r.status_code == 200
    (live,) = r.json()["keys"]
    assert live == {
        "id": k1["id"], "ownerAd": "alice", "ownerUid": "alice", "bucket": "alice-data",
        "bucketFull": "alice-data", "label": "ci", "access": "read", "uid": "alice+ci",
        "accessKey": k1["accessKey"], "state": "active", "suspended": False,
        "suspendedBy": None, "createdAt": live["createdAt"], "revokedAt": None,
        "statementPendingCleanup": False,
    }
    allk = env.bob.get(LIST, params={"state": "all"}).json()["keys"]
    assert [k["state"] for k in allk] == ["active", "revoked"] and allk[1]["revokedAt"]
    assert [k["id"] for k in env.bob.get(LIST, params={"state": "revoked"}).json()["keys"]] \
        == [k2["id"]]
    assert env.bob.get(LIST, params={"owner": "ALICE"}).json()["keys"][0]["id"] == k1["id"]
    assert env.bob.get(LIST, params={"owner": "bob"}).json()["keys"] == []
    both = env.bob.get(LIST, params={"bucket": "alice-data", "state": "all"})
    assert len(both.json()["keys"]) == 2
    assert env.bob.get(LIST, params={"bucket": "nope"}).json()["keys"] == []
    assert env.bob.get(LIST, params={"state": "bogus"}).status_code == 422
    text = env.bob.get(LIST, params={"state": "all"}).text.lower()
    assert "secret" not in text
    for u in env.croit_state.users.values():
        for key in u.keys:
            assert key["secret_key"] not in text
    assert env.bob.post(f"/api/bucket-keys/{k1['id']}/reveal", headers=H).status_code == 404


def test_admin_revoke_normal_path(env):
    put_foreign(env)
    k1, k2 = two_keys(env)
    r = revoke(env, k1["id"])
    assert r.status_code == 200
    assert r.json() == {"id": k1["id"], "revoked": True, "statementRemoved": True}
    assert "alice+ci" not in env.croit_state.users and "alice+rw" in env.croit_state.users
    assert sids(env) == ["legacy-allow", f"bucketeer-key-{k2['id']}"]
    row = env.db.bucket_keys.get(k1["id"])
    assert row["revoked_at"] and not row["statement_pending_cleanup"]
    (a,) = env.audit("admin.bucket_key.revoke")
    assert a["actor"] == "bob" and a["target"] == str(k1["id"])
    assert a["target_type"] == "bucket_key"
    assert a["details"]["owner"] == "alice" and a["details"]["statement_removed"] is True
    assert env.audit("bucket_key.revoke") == []  # the owner's own action name is not used
    assert "secret" not in json.dumps(a).lower().replace("secret_key", "")
    # the owner sees it as gone
    assert [k["id"] for k in env.alice.get("/api/bucket-keys").json()["keys"]] == [k2["id"]]


def test_idempotent(env):
    k1, _ = two_keys(env)
    assert revoke(env, k1["id"]).status_code == 200
    n = len(env.transport.mutations())
    r = revoke(env, k1["id"])
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert len(env.transport.mutations()) == n
    assert len(env.audit("admin.bucket_key.revoke")) == 1
    assert revoke(env, 99999).status_code == 404


def test_owner_suspended_kills_credential_and_leaves_flagged_statement(env):
    k1, _ = two_keys(env)
    assert env.bob.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    r = revoke(env, k1["id"])
    assert r.status_code == 200 and r.json()["statementRemoved"] is False
    assert "alice+ci" not in env.croit_state.users  # credential dead
    assert f"bucketeer-key-{k1['id']}" in sids(env)  # dangling, harmless
    row = env.db.bucket_keys.get(k1["id"])
    assert row["revoked_at"] and row["statement_pending_cleanup"] == 1
    (a,) = env.audit("admin.bucket_key.revoke")
    assert a["details"]["statement_removed"] is False
    assert a["details"]["statement_left"] == "owner suspended"
    (listed,) = env.bob.get(LIST, params={"state": "revoked"}).json()["keys"]
    assert listed["statementPendingCleanup"] is True
    # once the owner works again, a repeated admin revoke finishes the cleanup
    env.bob.post("/api/admin/users/alice/unsuspend", headers=H)
    r = revoke(env, k1["id"])
    assert r.json()["statementRemoved"] is True
    assert f"bucketeer-key-{k1['id']}" not in sids(env)
    assert env.db.bucket_keys.get(k1["id"])["statement_pending_cleanup"] == 0
    assert len(env.audit("admin.bucket_key.revoke")) == 2
    assert revoke(env, k1["id"]).status_code == 200
    assert len(env.audit("admin.bucket_key.revoke")) == 2


def test_policy_write_failure_still_revokes_and_flags(env):
    k1, _ = two_keys(env)
    break_policy_writes(env, reads=True)
    r = revoke(env, k1["id"])
    assert r.status_code == 200 and r.json()["statementRemoved"] is False
    assert "alice+ci" not in env.croit_state.users
    assert env.db.bucket_keys.get(k1["id"])["statement_pending_cleanup"] == 1


def test_croit_refusing_the_delete_keeps_the_row_live(env):
    import httpx
    k1, _ = two_keys(env)
    def boom(q):
        hit = q.method == "DELETE" and "alice%2Bci" in q.url.raw_path.decode()
        return httpx.Response(500, json={"message": "boom"}) if hit else None

    env.transport.fail = boom
    r = revoke(env, k1["id"])
    assert r.status_code == 502
    row = env.db.bucket_keys.get(k1["id"])
    assert row["revoked_at"] is None and row["suspended_by"] == "direct"
    assert env.audit("admin.bucket_key.revoke_failed")[0]["details"]["suspended"] is True
    env.transport.fail = None
    # an owner suspend/unsuspend must not wake the credential
    assert env.croit_state.users["alice+ci"].suspended is True
    env.bob.post("/api/admin/users/alice/suspend", headers=H)
    env.bob.post("/api/admin/users/alice/unsuspend", headers=H)
    assert env.croit_state.users["alice+ci"].suspended is True
    assert env.db.bucket_keys.get(k1["id"])["suspended_by"] == "direct"
    assert revoke(env, k1["id"]).status_code == 200
    assert env.db.bucket_keys.get(k1["id"])["revoked_at"]


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_revokes_unfinished_rows_too(env, state):
    row = env.db.bucket_keys.create("alice+half", "alice", "alice-data", "read", "half", state)
    assert revoke(env, row["id"]).status_code == 200  # user never existed: croit 404 is fine
    assert env.db.bucket_keys.get(row["id"])["revoked_at"]


def test_failed_revoke_overrides_an_owner_suspension(env):
    import httpx
    k1, _ = two_keys(env)
    env.bob.post("/api/admin/users/alice/suspend", headers=H)
    assert env.db.bucket_keys.get(k1["id"])["suspended_by"] == "owner"
    env.transport.fail = lambda q: (httpx.Response(500, json={"message": "boom"})
                                    if q.method == "DELETE" else None)
    assert revoke(env, k1["id"]).status_code == 502
    env.transport.fail = None
    assert env.db.bucket_keys.get(k1["id"])["suspended_by"] == "direct"
    env.bob.post("/api/admin/users/alice/unsuspend", headers=H)
    assert env.croit_state.users["alice+ci"].suspended is True
