"""The Admin Ops service user is an ordinary RGW user; the portal must never hand it to a
person, map to it, share with it, suspend it or list it (rgw backend only).

Plus the Admin Ops gaps (bucket create, versioning) answering 501 cleanly; delete works on
rgw as a background job (`tests/test_bucket_delete_job.py`).
"""

import asyncio

import httpx
import pytest

from .backends import (  # noqa: F401  (admin_backend is the fixture running both backends)
    admin_backend,
    admin_secret,
    rgw_only,
)
from .test_auth import USERS_GROUP
from .test_buckets import Env, H, create

SERVICE = "bucketeer-admin"


@pytest.fixture
def env(tmp_path, s3_endpoint):
    rgw_only("the Admin Ops service user exists only with the rgw backend")
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


@pytest.fixture
def tenant_env(tmp_path, s3_endpoint):
    rgw_only("the Admin Ops service user exists only with the rgw backend")
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    yield e
    e.db.close()


def add_person(env, sam):
    env.fake.add(sam, groups={USERS_GROUP}, display="Service Look-alike",
                 mail=f"{sam}@example.org")


def service_access_key(env):
    return env.croit_state.users[SERVICE].keys[0]["access_key"]


@pytest.mark.parametrize("sam", [SERVICE, "Bucketeer-Admin"])
def test_login_cannot_reach_the_service_user_or_its_keys(env, sam):
    add_person(env, sam)
    client = env.session(sam)
    account = client.get("/api/account").json()
    assert account["status"] == "creation_failed" and account["uid"] is None
    assert client.get("/api/credentials").status_code != 200
    r = client.post("/api/credentials/reveal", json={"accessKey": service_access_key(env)},
                    headers=H)
    assert r.status_code != 200 and admin_secret() not in r.text
    assert not [a for a in env.audit("credentials.")]
    assert client.get("/api/buckets").status_code != 200
    assert not env.croit_state.users[SERVICE].suspended


def test_the_tenant_form_is_refused_too(tenant_env):
    env = tenant_env
    add_person(env, SERVICE)
    env.croit_state.create_user("acme$bucketeer-admin", "Look-alike")
    client = env.session(SERVICE)
    assert client.get("/api/account").json()["status"] == "creation_failed"
    assert client.get("/api/credentials").status_code != 200


@pytest.mark.parametrize("target", [SERVICE, "Bucketeer-Admin", "acme$bucketeer-admin"])
def test_a_uid_mapping_cannot_target_it(env, target):
    r = env.bob.put("/api/admin/settings/uid-mappings/carol", json={"rgwUid": target},
                    headers=H)
    assert r.status_code == 422
    assert env.db.uid_mappings.get("carol") is None


def test_an_existing_mapping_to_it_is_not_followed(env):
    add_person(env, "carol2")
    env.db.uid_mappings.set("carol2", SERVICE, "bob")  # e.g. written before the rule existed
    client = env.session("carol2")
    assert client.get("/api/account").json()["status"] == "creation_failed"
    assert client.get("/api/credentials").status_code != 200


def test_it_cannot_be_a_share_recipient(env):
    add_person(env, SERVICE)
    r = env.alice.post("/api/buckets/alice-data/shares",
                       json={"login": SERVICE, "access": "read"}, headers=H)
    assert r.status_code in (404, 409)  # refused either way
    assert env.alice.get("/api/buckets/alice-data/shares").json()["shares"] == []
    with pytest.raises(Exception):  # noqa: B017  (no policy was written at all)
        env.moto().get_bucket_policy(Bucket="alice-data")


@pytest.mark.parametrize("uid", [SERVICE, "acme$bucketeer-admin", "Bucketeer-Admin"])
def test_admin_cannot_suspend_unsuspend_or_set_quota_on_it(env, uid):
    for path, method, body in (
        (f"/api/admin/users/{uid}/suspend", "POST", None),
        (f"/api/admin/users/{uid}/unsuspend", "POST", None),
        (f"/api/admin/users/{uid}/quota", "PUT", {"enabled": True, "maxSize": 1}),
    ):
        r = env.bob.request(method, path, json=body, headers=H)
        assert r.status_code == 409, (path, r.text)
    assert not env.croit_state.users[SERVICE].suspended
    assert env.croit_state.users[SERVICE].user_quota["enabled"] is False
    assert env.audit("admin.") == []


def test_it_is_hidden_from_the_admin_lists_the_export_and_offboarding(env):
    env.croit_state.create_bucket("svc-bucket", SERVICE)
    uids = {u["uid"] for u in env.bob.get("/api/admin/users").json()["users"]}
    assert SERVICE not in uids and {"alice", "bob"} <= uids
    names = {b["name"] for b in env.bob.get("/api/admin/buckets").json()["buckets"]}
    assert "svc-bucket" not in names
    csv = env.bob.get("/api/admin/export/chargeback.csv").text
    assert "svc-bucket" not in csv and SERVICE not in csv
    people = asyncio.run(env.app.state.offboarding._scope())[0]
    assert SERVICE not in people and all(SERVICE not in p["names"] for p in people.values())


def test_when_the_service_user_cannot_be_identified_nobody_resolves(env):
    env.transport.fail = lambda req: (  # the access-key lookup (a user read without a uid)
        httpx.ConnectError("down")
        if req.url.path == "/api/s3/users" and req.url.query == b"uid=" else None)
    assert env.alice.get("/api/account").json()["status"] == "unavailable"
    assert env.alice.get("/api/credentials").status_code == 503
    assert env.bob.get("/api/admin/users").status_code == 503
    r = env.bob.post(f"/api/admin/users/{SERVICE}/suspend", headers=H)
    assert r.status_code == 503
    env.transport.fail = None  # recovered: nothing was cached
    assert env.alice.get("/api/account").json()["status"] == "ready"


# -- bucket operations that have no Admin Ops counterpart still work -----------------


def test_bucket_create_versioning_and_delete_work_without_failure_audits(env):
    # nothing answers 501 on rgw any more: A3 (S3 create, versioning), A4 (delete job)
    assert create(env.alice, name="alice-new").status_code == 201
    r = env.alice.put("/api/buckets/alice-data/versioning", json={"enabled": True}, headers=H)
    assert r.status_code == 200
    r = env.alice.request("DELETE", "/api/buckets/alice-empty", headers=H)
    assert r.status_code == 200, r.text
    assert [a["action"] for a in env.audit("bucket.") if a["action"].endswith("_failed")] == []
    names = {b["name"] for b in env.alice.get("/api/buckets").json()["buckets"]}
    assert "alice-new" in names and "alice-empty" not in names


def test_a_bucket_key_uid_never_equals_the_service_uid(env, monkeypatch):
    """An operator could name the service user like a key user (`<owner>+<label>`); the
    service user is hidden from croit's user list, so create_key checks it explicitly."""
    async def looks_like_a_key_user():
        return "alice+svc"
    monkeypatch.setattr(env.app.state.croit, "service_uid", looks_like_a_key_user)
    r = env.alice.post("/api/buckets/alice-data/keys", json={"label": "svc", "access": "read"},
                       headers=H)
    assert r.status_code == 409, r.text
    assert env.db.bucket_keys.list(include_revoked=True) == []
