"""Bucket keys (/api/buckets/{name}/keys, /api/bucket-keys) against the mock croit and moto.

moto does not enforce policies or identities: that the key credentials can reach the bucket
only proves the plumbing; the policy DOCUMENT is what is asserted. Authorization (own
buckets, own rows) is tested at the Bucketeer layer.
"""

import json
from unittest.mock import MagicMock

import httpx
import pytest
from botocore.exceptions import ClientError

from bucketeer import s3docs
from bucketeer.bucket_keys import key_uid

from .backends import (
    admin_backend,  # noqa: F401  (fixture: both admin backends)
    croit_only,
    on_rgw,
)
from .test_auth import TEAM_GROUP, USERS_GROUP
from .test_buckets import ALICE_KEY, Env, H, create


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


@pytest.fixture
def acme(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    yield e
    e.db.close()


def mk(client, bucket="alice-data", label="backup", access="read", **kw):
    return client.post(f"/api/buckets/{bucket}/keys", json={"label": label, "access": access},
                       headers=H, **kw)


def policy(env, bucket="alice-data"):
    try:
        return json.loads(env.moto().get_bucket_policy(Bucket=bucket)["Policy"])
    except ClientError:
        return None


def sids(env, bucket="alice-data"):
    p = policy(env, bucket)
    return [s["Sid"] for s in p["Statement"]] if p else []


FOREIGN = {"Sid": "legacy-allow", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam:::user/bob"},
           "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::alice-data/*"]}


def put_foreign(env, bucket="alice-data"):
    env.moto().put_bucket_policy(Bucket=bucket, Policy=json.dumps(
        {"Version": "2012-10-17", "Statement": [FOREIGN]}))


def secret_of(env, uid):
    return env.croit_state.users[uid].keys[0]["secret_key"]


def break_policy_writes(env, reads=False):
    broken = MagicMock()
    broken.get_bucket_policy.side_effect = ClientError(
        {"Error": {"Code": "InternalError" if reads else "NoSuchBucketPolicy", "Message": "x"}},
        "GetBucketPolicy")
    broken.put_bucket_policy.side_effect = ClientError(
        {"Error": {"Code": "InternalError", "Message": "boom"}}, "PutBucketPolicy")
    env.app.state.s3.factory = lambda *a: broken


def fail_when(env, method, needle, status=500):
    env.transport.fail = lambda r: (
        httpx.Response(status, json={"message": "boom"})
        if r.method == method and needle in r.url.raw_path.decode() else None)


# -- create ----------------------------------------------------------------------------


def test_create_full_flow(env):
    put_foreign(env)
    r = mk(env.alice)
    assert r.status_code == 201
    k = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert k["label"] == "backup" and k["access"] == "read" and k["uid"] == "alice+backup"
    assert k["bucket"] == "alice-data" and k["status"] == "active" and not k["suspended"]
    assert set(k) == {"id", "label", "access", "uid", "accessKey", "bucket", "createdAt",
                      "suspended", "status"}
    # the RGW user: zero quota, no email, exactly one key (the reported one)
    u = env.croit_state.users["alice+backup"]
    assert u.user_quota == {"enabled": True, "maxSize": 0, "maxObjects": 0}
    assert u.email == "" and u.name == "Bucketeer key backup for alice (alice-data)"
    assert [x["access_key"] for x in u.keys] == [k["accessKey"]]
    # the policy: our statement, the foreign one untouched and first
    p = policy(env)
    assert p["Statement"][0] == FOREIGN
    st = p["Statement"][1]
    assert st["Sid"] == f"bucketeer-key-{k['id']}" and st["Effect"] == "Allow"
    assert st["Principal"] == {"AWS": "arn:aws:iam:::user/alice+backup"}
    assert st["Action"] == list(s3docs.READ_ACTIONS)
    assert st["Resource"] == ["arn:aws:s3:::alice-data", "arn:aws:s3:::alice-data/*"]
    # SQLite row
    row = env.db.bucket_keys.get(k["id"])
    assert (row["owner_ad_username"], row["bucket_name"], row["state"]) == (
        "alice", "alice-data", "active")
    assert row["access_key"] == k["accessKey"] and row["revoked_at"] is None
    # the owner's own S3 key wrote the policy, never the new key's
    assert env.used and set(env.used) == {ALICE_KEY}
    (a,) = [a for a in env.audit("bucket_key.") if a["action"] == "bucket_key.create"]
    assert a["actor"] == "alice" and secret_of(env, "alice+backup") not in json.dumps(a)


def test_readwrite_statement_and_moto_access(env):
    k = mk(env.alice, access="readwrite").json()
    (st,) = [s for s in policy(env)["Statement"] if s["Sid"] == f"bucketeer-key-{k['id']}"]
    assert "s3:PutObject" in st["Action"] and "s3:DeleteObject" in st["Action"]
    assert "s3:PutBucketPolicy" not in st["Action"] and "s3:DeleteBucket" not in st["Action"]
    # moto accepts any credentials (it enforces nothing), so this only shows the keys are real
    sk = secret_of(env, "alice+backup")
    s3 = env.bridge.client(k["accessKey"], sk)
    assert "Contents" in s3.list_objects_v2(Bucket="alice-data")


def test_tenant_user_gets_tenant_qualified_key(acme):
    carol = acme.session("carol")
    r = mk(carol, bucket="reports", label="ci", access="readwrite")
    assert r.status_code == 201 and r.json()["uid"] == "acme$carol+ci"
    assert "acme$carol+ci" in acme.croit_state.users
    (st,) = policy(acme, "reports")["Statement"]
    assert st["Principal"] == {"AWS": "arn:aws:iam::acme:user/carol+ci"}
    assert acme.db.bucket_keys.get(r.json()["id"])["bucket_name"] == "acme:reports"


@pytest.mark.parametrize("label", ["", "UPPER", "a_b", "a.b", "-a", "a-", "x" * 21, "a b", "é"])
def test_bad_labels_are_refused_before_anything_happens(env, label):
    r = mk(env.alice, label=label)
    assert r.status_code == 422
    assert env.transport.mutations() == [] and env.db.bucket_keys.list(include_revoked=True) == []


def test_bad_access_and_unknown_fields(env):
    assert mk(env.alice, access="admin").status_code == 422
    r = env.alice.post("/api/buckets/alice-data/keys", headers=H,
                       json={"label": "ok", "access": "read", "uid": "bob"})
    assert r.status_code == 422
    assert env.transport.mutations() == []


def test_key_uid_rules():
    assert key_uid("alice", "job") == ("alice+job", "alice+job")
    assert key_uid("acme$carol", "job") == ("acme$carol+job", "carol+job")
    assert "." not in key_uid("john.smith", "x")[0].partition("+")[2]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        key_uid("u" * 60, "twenty-chars-label-x")
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        key_uid("al ice", "job")


def test_duplicate_labels_and_taken_uids_are_refused(env):
    assert mk(env.alice).status_code == 201
    assert mk(env.alice).status_code == 409                       # live duplicate
    assert mk(env.alice, bucket="alice-empty").status_code == 409  # same uid, other bucket
    k = env.db.bucket_keys.list()[0]
    assert env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H).status_code == 200
    assert mk(env.alice).status_code == 409                       # revoked uid is never reused
    env.croit_state.create_user("alice+other", "Someone")
    assert mk(env.alice, label="other").status_code == 409        # exists in croit
    env.croit_state.create_user("Alice+Case", "Someone")
    assert mk(env.alice, label="case").status_code == 409         # case-insensitive match


def test_other_users_and_unknown_buckets_are_404(env):
    for bucket in ("bob-shared", "nope-nothing", "Bad:Name", "acme:reports"):
        assert mk(env.alice, bucket=bucket).status_code == 404
    assert env.transport.mutations() == [] and env.db.bucket_keys.list() == []
    assert policy(env, "bob-shared") is None
    assert mk(env.bob, bucket="bob-shared").status_code == 201    # an admin on his own bucket


def test_limits_per_bucket_and_per_user(env):
    for i in range(5):
        assert mk(env.alice, label=f"k{i}").status_code == 201
    r = mk(env.alice, label="k5")
    assert r.status_code == 409 and "at most 5" in r.json()["detail"]
    for i in range(5):
        assert mk(env.alice, bucket="alice-empty", label=f"e{i}").status_code == 201
    assert create(env.alice, name="alice-third").status_code == 201
    r = mk(env.alice, bucket="alice-third", label="over")
    assert r.status_code == 409 and "at most 10" in r.json()["detail"]
    assert "alice+over" not in env.croit_state.users
    # a revoked key frees a slot
    first = env.db.bucket_keys.list(bucket_name="alice-data")[0]
    assert env.alice.delete(f"/api/bucket-keys/{first['id']}", headers=H).status_code == 200
    assert mk(env.alice, bucket="alice-third", label="over").status_code == 201
    assert env.bob.get("/api/bucket-keys").json() == {"keys": []}  # bob's count is his own


def test_create_needs_session_csrf_and_active_account(env):
    anon = env.anon()
    assert anon.post("/api/buckets/alice-data/keys",
                     json={"label": "a", "access": "read"}, headers=H).status_code == 401
    r = env.alice.post("/api/buckets/alice-data/keys", json={"label": "a", "access": "read"})
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    env.bob.post("/api/admin/users/alice/suspend", headers=H)
    assert mk(env.alice).status_code == 403
    assert env.db.bucket_keys.list(include_revoked=True) == []


# -- rollback ----------------------------------------------------------------------------


def assert_clean_failure(env, status=(502, 503)):
    rows = env.db.bucket_keys.list(include_revoked=True)
    assert len(rows) == 1 and rows[0]["state"] == "failed" and rows[0]["revoked_at"]
    assert "alice+backup" not in env.croit_state.users
    assert not any(s.startswith("bucketeer-key-") for s in sids(env))
    (a,) = env.audit("bucket_key.create_failed")
    assert a["details"]["cleaned"] is True
    assert [a for a in env.audit("bucket_key.") if a["action"] == "bucket_key.create"] == []


@pytest.mark.parametrize(("method", "needle", "step"), [
    ("POST", "/s3/users", "create the key user"),
    ("PUT", "/s3/users/alice%2Bbackup/quota", "quota"),
    ("PUT", "/s3/users/alice%2Bbackup/keys/", "access key"),
])
def test_rollback_on_croit_failures(env, method, needle, step):
    put_foreign(env)
    fail_when(env, method, needle)
    r = mk(env.alice)
    assert r.status_code in (502, 503)
    assert_clean_failure(env)
    assert policy(env)["Statement"] == [FOREIGN]
    env.transport.fail = None
    assert mk(env.alice, label="again").status_code == 201  # the account is not stuck


def test_rollback_when_the_policy_cannot_be_written(env):
    put_foreign(env)
    real = env.real_factory
    break_policy_writes(env)
    r = mk(env.alice)
    assert r.status_code == 502
    env.app.state.s3.factory = real
    assert_clean_failure(env)
    assert policy(env)["Statement"] == [FOREIGN]


def test_rollback_when_the_row_cannot_be_activated(env, monkeypatch):
    real = env.db.bucket_keys.set_state

    def flaky(key_id, state, access_key=None):
        if state == "active":
            raise RuntimeError("disk full")
        return real(key_id, state, access_key)

    monkeypatch.setattr(env.db.bucket_keys, "set_state", flaky)
    assert mk(env.alice).status_code == 502
    assert_clean_failure(env)  # statement removed again, user deleted


def test_zero_quota_not_in_effect_fails_closed(env):
    # a backend that silently ignores the quota call must not leave a key that can store data
    if on_rgw():  # answer 200 to the quota write without applying it
        env.transport.fail = lambda req: (
            httpx.Response(200) if req.method == "PUT" and "/quota" in req.url.path else None)
    else:
        env.croit_state.set_quota = lambda target, body: None
    assert mk(env.alice).status_code == 502
    assert_clean_failure(env)


def test_unfinished_cleanup_leaves_a_revocable_row(env):
    env.transport.fail = lambda r: (
        httpx.Response(500, json={"message": "boom"})
        if (r.method == "PUT" and "/quota" in r.url.raw_path.decode())
        or (r.method == "DELETE" and "/s3/users/alice%2Bbackup" in r.url.raw_path.decode())
        else None)
    r = mk(env.alice)
    assert r.status_code == 502 and "clean-up" in r.json()["detail"]
    (row,) = env.db.bucket_keys.list()  # still live: the user may exist
    assert row["state"] == "failed" and row["revoked_at"] is None
    assert "alice+backup" in env.croit_state.users
    (listed,) = env.alice.get("/api/buckets/alice-data/keys").json()["keys"]
    assert listed["status"] == "failed"
    assert env.alice.post(f"/api/bucket-keys/{row['id']}/reveal", headers=H).status_code == 409
    env.transport.fail = None
    assert env.alice.delete(f"/api/bucket-keys/{row['id']}", headers=H).status_code == 200
    assert "alice+backup" not in env.croit_state.users
    assert env.db.bucket_keys.get(row["id"])["revoked_at"]


# -- list ------------------------------------------------------------------------------


def test_lists_are_own_only_and_secretless(env):
    a = mk(env.alice).json()
    mk(env.alice, bucket="alice-empty", label="e", access="readwrite")
    b = mk(env.bob, bucket="bob-shared", label="b").json()
    r = env.alice.get("/api/buckets/alice-data/keys")
    assert [k["id"] for k in r.json()["keys"]] == [a["id"]]
    allk = env.alice.get("/api/bucket-keys").json()["keys"]
    assert {k["label"] for k in allk} == {"backup", "e"} and b["id"] not in {k["id"] for k in allk}
    assert "secret" not in r.text.lower() and "secret" not in json.dumps(allk).lower()
    assert env.alice.get("/api/buckets/bob-shared/keys").status_code == 404
    assert env.anon().get("/api/bucket-keys").status_code == 401
    # revoked keys disappear from the lists
    env.alice.delete(f"/api/bucket-keys/{a['id']}", headers=H)
    assert env.alice.get("/api/buckets/alice-data/keys").json() == {"keys": []}


# -- reveal ----------------------------------------------------------------------------


def test_reveal_own_key_only_and_audited(env):
    k = mk(env.alice).json()
    r = env.alice.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"accessKey": k["accessKey"], "secretKey": secret_of(env, "alice+backup")}
    (a,) = env.audit("bucket_key.reveal")
    assert a["actor"] == "alice" and a["target"] == str(k["id"])
    assert secret_of(env, "alice+backup") not in json.dumps(a)
    for client in (env.bob,):  # another user's id looks like any unknown id
        assert client.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H).status_code == 404
    assert env.anon().post(f"/api/bucket-keys/{k['id']}/reveal", headers=H).status_code == 401
    assert env.alice.post(f"/api/bucket-keys/{k['id']}/reveal").status_code == 403  # CSRF
    assert env.alice.post("/api/bucket-keys/9999/reveal", headers=H).status_code == 404
    assert len(env.audit("bucket_key.reveal")) == 1


def test_reveal_refused_when_audit_fails_suspended_or_revoked(env, monkeypatch):
    k = mk(env.alice).json()
    real = env.db.audit.add

    def boom(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(env.db.audit, "add", boom)
    with pytest.raises(RuntimeError, match="audit down"):  # a 500 with no body, no secret
        env.alice.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H)
    monkeypatch.setattr(env.db.audit, "add", real)
    env.db.bucket_keys.set_suspended(k["id"], True, "direct")
    assert env.alice.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H).status_code == 403
    env.db.bucket_keys.set_suspended(k["id"], False)
    env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H)
    assert env.alice.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H).status_code == 404


# -- revoke ----------------------------------------------------------------------------


def test_revoke_removes_statement_user_and_marks_row(env):
    put_foreign(env)
    k1 = mk(env.alice).json()
    k2 = mk(env.alice, label="other", access="readwrite").json()
    assert env.alice.delete(f"/api/bucket-keys/{k1['id']}", headers=H).json() == {
        "id": k1["id"], "revoked": True}
    assert sids(env) == ["legacy-allow", f"bucketeer-key-{k2['id']}"]
    assert "alice+backup" not in env.croit_state.users and "alice+other" in env.croit_state.users
    row = env.db.bucket_keys.get(k1["id"])
    assert row["revoked_at"] and env.db.bucket_keys.get(k2["id"])["revoked_at"] is None
    (a,) = env.audit("bucket_key.revoke")
    assert a["target"] == str(k1["id"]) and secret_of(env, "alice+other") not in json.dumps(a)
    # idempotent
    n = len(env.transport.mutations())
    assert env.alice.delete(f"/api/bucket-keys/{k1['id']}", headers=H).status_code == 200
    assert len(env.transport.mutations()) == n and len(env.audit("bucket_key.revoke")) == 1


def test_revoking_the_last_statement_deletes_the_policy(env):
    k = mk(env.alice).json()
    assert policy(env) is not None
    env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H)
    assert policy(env) is None


def test_revoke_other_users_key_and_csrf(env):
    k = mk(env.alice).json()
    assert env.bob.delete(f"/api/bucket-keys/{k['id']}", headers=H).status_code == 404
    assert env.alice.delete(f"/api/bucket-keys/{k['id']}").status_code == 403
    assert env.anon().delete(f"/api/bucket-keys/{k['id']}", headers=H).status_code == 401
    assert env.db.bucket_keys.get(k["id"])["revoked_at"] is None
    assert "alice+backup" in env.croit_state.users


def test_revoke_when_croit_refuses_keeps_the_row(env):
    k = mk(env.alice).json()
    # the key user created a bucket of its own: croit refuses to delete the user
    env.croit_state.create_bucket("sneaky", "alice+backup")
    r = env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H)
    assert r.status_code == 502 and "administrator" in r.json()["detail"]
    assert env.db.bucket_keys.get(k["id"])["revoked_at"] is None
    assert not any(s.startswith("bucketeer-key-") for s in sids(env))  # access is already gone
    assert env.audit("bucket_key.revoke_failed")
    env.croit_state.delete_bucket("sneaky", {"name": "sneaky"})
    assert env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H).status_code == 200
    assert "alice+backup" not in env.croit_state.users


def test_revoke_keeps_the_row_when_the_policy_cannot_be_changed(env):
    k = mk(env.alice).json()
    real = env.real_factory
    break_policy_writes(env, reads=True)
    assert env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H).status_code == 502
    env.app.state.s3.factory = real
    assert "alice+backup" in env.croit_state.users  # not deleted while its access remains
    assert env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H).status_code == 200


# -- bucket delete, resolver, admin ----------------------------------------------------------


def test_bucket_delete_revokes_its_keys(env):
    k = mk(env.alice, bucket="alice-empty").json()
    keep = mk(env.alice, bucket="alice-data", label="keep").json()
    r = env.alice.delete("/api/buckets/alice-empty", headers=H)
    assert r.status_code == 200 and r.json() == {"name": "alice-empty", "keysRevoked": 1}
    assert "alice+backup" not in env.croit_state.users and "alice+keep" in env.croit_state.users
    assert env.db.bucket_keys.get(k["id"])["revoked_at"]
    assert env.db.bucket_keys.get(keep["id"])["revoked_at"] is None
    (a,) = env.audit("bucket_key.revoke")
    assert a["details"]["reason"] == "bucket_deleted"


def test_bucket_delete_with_a_stuck_key_user_still_deletes_and_warns(env):
    mk(env.alice, bucket="alice-empty")
    fail_when(env, "DELETE", "/s3/users/alice%2Bbackup")
    r = env.alice.delete("/api/buckets/alice-empty", headers=H)
    assert r.status_code == 200 and "warning" in r.json()
    assert "alice-empty" not in env.croit_state.buckets
    (row,) = env.db.bucket_keys.list()
    assert row["revoked_at"] is None  # an administrator can still see and finish it


def test_human_login_with_a_key_uid_is_refused(env):
    k = mk(env.alice).json()
    env.fake.add("alice+backup", groups={USERS_GROUP, TEAM_GROUP}, display="Mallory",
                 mail="m@example.org")
    acct = env.session("alice+backup").get("/api/account").json()
    assert acct["status"] == "creation_failed" and acct["reason"] == "invalid_username"
    # a uid that is merely claimed (any key row, revoked too) is uid_taken
    env.db.bucket_keys.create("legacy", "alice", "alice-data", "read")
    env.fake.add("legacy", groups={USERS_GROUP, TEAM_GROUP}, display="L", mail="l@example.org")
    legacy = env.session("legacy")
    assert legacy.get("/api/account").json()["reason"] == "uid_taken"
    env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H)


def test_admin_suspend_cascades_to_bucket_keys(env):
    k = mk(env.alice).json()
    assert env.bob.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    row = env.db.bucket_keys.get(k["id"])
    assert row["suspended_at"] and row["suspended_by"] == "owner"
    assert env.croit_state.users["alice+backup"].suspended is True
    assert env.alice.get("/api/buckets/alice-data/keys").status_code == 403
    assert env.alice.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H).status_code == 403
    assert env.bob.post("/api/admin/users/alice/unsuspend", headers=H).status_code == 200
    assert env.db.bucket_keys.get(k["id"])["suspended_at"] is None
    assert env.croit_state.users["alice+backup"].suspended is False
    (listed,) = env.alice.get("/api/buckets/alice-data/keys").json()["keys"]
    assert listed["status"] == "active"
    # a key suspended on its own is shown as suspended and cannot be revealed
    env.bob.post("/api/admin/users/alice+backup/suspend", headers=H)
    (listed,) = env.alice.get("/api/buckets/alice-data/keys").json()["keys"]
    assert listed["suspended"] and listed["status"] == "suspended"
    assert env.alice.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H).status_code == 403


def test_nothing_secret_in_audit_or_responses(env):
    k = mk(env.alice).json()
    secret = secret_of(env, "alice+backup")
    env.alice.post(f"/api/bucket-keys/{k['id']}/reveal", headers=H)
    env.alice.delete(f"/api/bucket-keys/{k['id']}", headers=H)
    blob = json.dumps(env.db.audit.query(limit=1000), default=str)
    assert secret not in blob and secret not in json.dumps(k)


# -- `+` separator, races, admin 404 ------------------------------------------------------


def test_plus_survives_croit_round_trip_and_s3docs_rules(env):
    k = mk(env.alice).json()
    croit = env.app.state.croit
    import asyncio
    run = asyncio.run
    assert run(croit.get_user("alice+backup"))["uid"] == "alice+backup"  # ?uid= keeps the +
    assert "alice+backup" in run(croit.list_user_names())
    assert [x["access_key"] for x in run(croit.list_keys("alice+backup"))] == [k["accessKey"]]
    assert run(croit.get_user("alice backup")) is None  # a + never turns into a space
    # `+` is for key users only: shares and Sids keep the strict rules
    with pytest.raises(s3docs.S3DocError):
        s3docs.share_statement("alice-data", "alice+backup", None, "read")
    with pytest.raises(s3docs.S3DocError):
        s3docs.bucket_key_statement("alice-data", "alice+backup", None, "read", "a+b")
    with pytest.raises(s3docs.S3DocError):
        s3docs.bucket_key_statement("alice-data", "alice.backup", None, "read", 1)


def test_resolver_refuses_a_uid_claimed_during_resolution(env):
    from .test_auth import TEAM_GROUP, USERS_GROUP
    env.fake.add("zed", groups={USERS_GROUP, TEAM_GROUP}, display="Zed", mail="z@example.org")
    rgw = env.app.state.rgw
    real = rgw._get

    async def racing(uid):
        out = await real(uid)
        if uid == "zed" and not env.db.bucket_keys.get_by_uid("zed"):
            env.db.bucket_keys.create("zed", "alice", "alice-data", "read")  # a key claims it
        return out

    rgw._get = racing
    zed = env.session("zed")
    a = zed.get("/api/account").json()
    assert a["status"] == "creation_failed" and a["reason"] == "uid_taken"
    assert "zed" not in rgw._cache


def _inject_create(env, make_user, exc=None):
    """On POST /s3/users: someone else's user appears, then croit answers 409 (or times out)."""
    def fail(r):
        if r.method == "POST" and r.url.raw_path.decode().endswith("/s3/users"):
            make_user()
            return exc or httpx.Response(409, json={"message": "exists"})
        return None
    env.transport.fail = fail


def test_failed_create_never_deletes_a_users_that_is_not_ours(env):
    _inject_create(env, lambda: env.croit_state.create_user("alice+backup", "A human"))
    assert mk(env.alice).status_code in (400, 409, 502)
    assert "alice+backup" in env.croit_state.users  # the other creator's user survives
    (row,) = env.db.bucket_keys.list(include_revoked=True)
    assert row["state"] == "failed" and row["revoked_at"]


def test_timeout_deletes_only_a_user_with_our_display_name(env):
    name = "Bucketeer key backup for alice (alice-data)"
    boom = httpx.ReadTimeout("slow")
    _inject_create(env, lambda: env.croit_state.create_user("alice+backup", "A human"), boom)
    assert mk(env.alice).status_code in (502, 503)
    assert "alice+backup" in env.croit_state.users  # not ours: kept
    env.croit_state.delete_user("alice+backup")
    _inject_create(env, lambda: env.croit_state.create_user("alice+again", name.replace(
        "backup", "again")), boom)
    assert mk(env.alice, label="again").status_code in (502, 503)
    assert "alice+again" not in env.croit_state.users  # ours: cleaned up


def test_suspend_cascade_counts_a_missing_user_of_a_failed_row_as_done(env):
    row = env.db.bucket_keys.create("alice+ghost", "alice", "alice-data", "read", "ghost",
                                    "failed")
    r = env.bob.post("/api/admin/users/alice/suspend", headers=H)
    assert r.status_code == 200 and "alice+ghost" in r.json()["keys"]
    assert env.db.bucket_keys.get(row["id"])["suspended_at"]
    # an active row whose user is missing is a real failure
    env.bob.post("/api/admin/users/alice/unsuspend", headers=H)
    env.db.bucket_keys.create("alice+gone", "alice", "alice-data", "read", "gone", "active")
    assert env.bob.post("/api/admin/users/alice/suspend", headers=H).status_code == 502


# -- startup clean-up of pending rows ----------------------------------------------------

OURS = "Bucketeer key backup for alice (alice-data)"


def stale_pending(env, label="backup", age=3600.0, user_name=OURS, statement=True):
    """A pending row as a crash leaves it: user, statement and row, `age` seconds old."""
    from datetime import UTC, datetime, timedelta
    uid = f"alice+{label}"
    row = env.db.bucket_keys.create(uid, "alice", "alice-data", "read", label, "pending")
    if user_name is not None:
        env.croit_state.create_user(uid, user_name)
    if statement:
        st = s3docs.bucket_key_statement("alice-data", uid, "", "read", row["id"])
        env.moto().put_bucket_policy(Bucket="alice-data", Policy=json.dumps(
            {"Version": "2012-10-17", "Statement": [FOREIGN, st]}))
    old = (datetime.now(UTC) - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with env.db.tx() as c:
        c.execute("UPDATE bucket_keys SET created_at = ? WHERE id = ?", (old, row["id"]))
    return row


def run_cleanup(env, **kw):
    import asyncio

    from bucketeer.bucket_keys import cleanup_pending
    kw.setdefault("delays", (0.01, 0.01))
    return asyncio.run(cleanup_pending(env.app, **kw))


def test_cleanup_rolls_back_an_old_pending_row(env):
    row = stale_pending(env)
    assert any(s.startswith("bucketeer-key-") for s in sids(env))
    assert run_cleanup(env)["rolled_back"] == 1
    assert "alice+backup" not in env.croit_state.users
    assert sids(env) == ["legacy-allow"]
    got = env.db.bucket_keys.get(row["id"])
    assert got["state"] == "failed" and got["revoked_at"]
    (a,) = env.audit("bucket_key.cleanup")
    assert a["actor"] == "system" and a["target"] == str(row["id"])
    assert "secret" not in json.dumps(a).lower()
    assert run_cleanup(env)["rolled_back"] == 0  # idempotent


def test_cleanup_leaves_young_active_and_revoked_rows(env):
    young = stale_pending(env, "young", age=5)
    active = env.db.bucket_keys.create("alice+act", "alice", "alice-data", "read", "act",
                                       "active")
    revoked = env.db.bucket_keys.create("alice+rev", "alice", "alice-data", "read", "rev",
                                        "pending")
    env.db.bucket_keys.revoke(revoked["id"])
    assert run_cleanup(env) == {"rolled_back": 0, "needs_cleanup": 0, "left": 0}
    assert "alice+young" in env.croit_state.users
    assert env.db.bucket_keys.get(young["id"])["state"] == "pending"
    assert env.db.bucket_keys.get(active["id"])["state"] == "active"
    assert not env.audit("bucket_key.cleanup")


def test_cleanup_never_deletes_a_user_that_is_not_ours(env):
    row = stale_pending(env, user_name="A human", statement=False)
    assert run_cleanup(env)["rolled_back"] == 1
    assert "alice+backup" in env.croit_state.users
    assert env.db.bucket_keys.get(row["id"])["revoked_at"]


def test_cleanup_retries_when_croit_is_down_then_gives_up(env):
    row = stale_pending(env)
    env.transport.fail = lambda r: httpx.Response(503, json={"message": "down"})
    assert run_cleanup(env) == {"rolled_back": 0, "needs_cleanup": 0, "left": 1}
    assert env.db.bucket_keys.get(row["id"])["state"] == "pending"
    assert "alice+backup" in env.croit_state.users
    # croit comes back during the retries
    calls = {"n": 0}

    def flaky(r):
        calls["n"] += 1
        return httpx.Response(503, json={"message": "down"}) if calls["n"] < 4 else None

    env.transport.fail = flaky
    assert run_cleanup(env, delays=(0.01, 0.01, 0.01, 0.01))["rolled_back"] == 1
    assert env.db.bucket_keys.get(row["id"])["revoked_at"]


def test_cleanup_without_owner_s3_client_marks_row_needs_cleanup(env):
    row = stale_pending(env)
    for k in list(env.croit_state.users["alice"].keys):
        env.croit_state.users["alice"].keys.remove(k)
    assert run_cleanup(env)["needs_cleanup"] == 1
    got = env.db.bucket_keys.get(row["id"])
    assert got["state"] == "failed" and got["revoked_at"] is None
    assert "alice+backup" in env.croit_state.users  # untouched: a revoke finishes it
    assert any(s.startswith("bucketeer-key-") for s in sids(env))
    (a,) = env.audit("bucket_key.cleanup")
    assert "needs_cleanup" in json.dumps(a)


def test_startup_cleanup_runs_in_background_and_health_is_instant(env, monkeypatch):
    import asyncio

    from bucketeer import bucket_keys
    monkeypatch.setattr(bucket_keys, "CLEANUP_ON_START", True)
    started, release = [], asyncio.Event()

    async def slow(app, *a, **kw):
        started.append(1)
        await release.wait()

    monkeypatch.setattr(bucket_keys, "cleanup_pending", slow)
    from fastapi.testclient import TestClient
    with TestClient(env.app) as c:
        assert c.get(env.settings.health_check_path).status_code == 200
    assert started == [1]  # ran, never awaited by startup, cancelled on shutdown


def test_real_startup_cleanup_through_the_lifespan(env, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from bucketeer import bucket_keys
    row = stale_pending(env)
    done = threading.Event()
    real = bucket_keys.cleanup_pending

    async def signalled(*a, **kw):
        try:
            return await real(*a, **kw)
        finally:
            done.set()  # the task finished: rows and audit are written

    monkeypatch.setattr(bucket_keys, "cleanup_pending", signalled)
    monkeypatch.setattr(bucket_keys, "CLEANUP_ON_START", True)
    monkeypatch.setattr(bucket_keys, "CLEANUP_GRACE", 0.0)
    with TestClient(env.app) as c:
        assert c.get(env.settings.health_check_path).status_code == 200
        assert done.wait(60), "the startup clean-up never finished"
    assert env.db.bucket_keys.get(row["id"])["revoked_at"]
    assert "alice+backup" not in env.croit_state.users
    assert env.audit("bucket_key.cleanup")
