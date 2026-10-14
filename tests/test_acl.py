"""Bucket and object ACLs (/api/buckets/{name}/acl, /objects/acl) against mock croit and moto.

moto enforces no identities, so the ACL DOCUMENT and the key used are asserted.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from bucketeer import acl as aclmod
from bucketeer import sharing

from .test_auth import USERS_GROUP
from .test_buckets import ALICE_KEY, Env, H

KEY = "data/file-0001.bin"
ALL = aclmod.ALL_USERS


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    e.fake.add("erin", groups={USERS_GROUP}, display="Erin E", mail="erin@example.org")
    e.fake.add("frank", groups=set(), display="Frank F", mail="frank@example.org")
    yield e
    e.db.close()


def owner_id(env, bucket="alice-data"):
    return env.moto().get_bucket_acl(Bucket=bucket)["Owner"]["ID"]


def moto_grants(env, bucket="alice-data", key=None):
    m = env.moto()
    g = (m.get_bucket_acl(Bucket=bucket) if key is None
         else m.get_object_acl(Bucket=bucket, Key=key))["Grants"]
    return sorted((x["Grantee"].get("ID") or x["Grantee"].get("URI"), x["Permission"]) for x in g)


def seed(env, extra, bucket="alice-data"):
    oid = owner_id(env, bucket)
    grants = [{"Grantee": {"Type": "CanonicalUser", "ID": oid}, "Permission": "FULL_CONTROL"}]
    for ident, perm in extra:
        g = ({"Type": "Group", "URI": ident} if ident.startswith("http")
             else {"Type": "CanonicalUser", "ID": ident})
        grants.append({"Grantee": g, "Permission": perm})
    env.moto().put_bucket_acl(Bucket=bucket, AccessControlPolicy={
        "Owner": {"ID": oid}, "Grants": grants})


def get(client, bucket="alice-data", key=None):
    if key is None:
        return client.get(f"/api/buckets/{bucket}/acl")
    return client.get(f"/api/buckets/{bucket}/objects/acl", params={"key": key})


def put(client, body, bucket="alice-data", key=None, headers=H):
    if key is None:
        return client.put(f"/api/buckets/{bucket}/acl", json=body, headers=headers)
    return client.put(f"/api/buckets/{bucket}/objects/acl", params={"key": key}, json=body,
                      headers=headers)


def body(env, keep=(), add=(), **kw):
    return {"etag": get(env.alice, **kw).json()["etag"], "keep": list(keep), "add": list(add)}


def acl_audit(env):
    return env.audit("bucket.acl") + env.audit("object.acl")


def test_get_bucket_acl_owner_only(env):
    r = get(env.alice)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["scope"] == "bucket" and "WRITE" in j["permissions"]
    assert len(j["grants"]) == 1
    g = j["grants"][0]
    assert g["kind"] == "owner" and g["permission"] == "FULL_CONTROL" and g["removable"] is False
    assert j["owner"]["id"] == owner_id(env)


def test_add_existing_person_then_change_and_remove(env):
    r = put(env.alice, body(env, add=[{"login": "bob", "permission": "READ"}]))
    assert r.status_code == 200 and r.json()["changed"] is True
    oid = owner_id(env)
    assert moto_grants(env) == sorted([(oid, "FULL_CONTROL"), ("bob", "READ")])
    j = get(env.alice).json()
    bob = [g for g in j["grants"] if g["id"] == "bob"][0]
    assert bob["kind"] == "user" and bob["removable"] is True
    # change permission
    r = put(env.alice, {"etag": j["etag"], "keep": [{"id": "bob", "permission": "READ_ACP"}]})
    assert r.status_code == 200
    assert ("bob", "READ_ACP") in moto_grants(env) and ("bob", "READ") not in moto_grants(env)
    # remove
    r = put(env.alice, body(env))
    assert moto_grants(env) == [(oid, "FULL_CONTROL")]
    actions = [a["action"] for a in acl_audit(env)]
    assert actions.count("bucket.acl") == 3
    d = acl_audit(env)[0]["details"]
    assert d["uid"] == "alice" and "before" in d and "after" in d
    assert set(env.used) == {ALICE_KEY}


def test_add_person_creates_account(env):
    assert "erin" not in env.croit_state.users
    r = put(env.alice, body(env, add=[{"login": "erin@example.org", "permission": "FULL_CONTROL"}]))
    assert r.status_code == 200
    assert "erin" in env.croit_state.users
    assert ("erin", "FULL_CONTROL") in moto_grants(env)
    assert acl_audit(env)[0]["details"]["accountsCreated"] == ["erin"]


def test_owner_cannot_be_dropped_or_changed(env):
    oid = owner_id(env)
    r = put(env.alice, body(env, keep=[{"id": oid, "permission": "READ"}]))
    assert r.status_code == 422
    put(env.alice, body(env))  # empty keep still writes the owner
    assert moto_grants(env) == [(oid, "FULL_CONTROL")]
    r = put(env.alice, body(env, add=[{"login": "alice", "permission": "READ"}]))
    assert r.status_code == 409


@pytest.mark.parametrize("uri", [ALL, aclmod.SIGNED_IN, aclmod.LOG_DELIVERY])
def test_adding_group_grants_is_refused(env, uri):
    r = put(env.alice, body(env, keep=[{"uri": uri, "permission": "READ"}]))
    assert r.status_code == 422 and "Public access is not enabled" in r.json()["detail"]
    assert moto_grants(env) == [(owner_id(env), "FULL_CONTROL")]
    assert acl_audit(env) == []


def test_existing_public_grant_is_shown_kept_and_removable(env):
    seed(env, [(ALL, "READ"), (aclmod.SIGNED_IN, "READ")])
    j = get(env.alice).json()
    groups = {g["uri"]: g for g in j["grants"] if g["kind"] == "group"}
    assert groups[ALL]["scope"] == "public" and groups[aclmod.SIGNED_IN]["scope"] == "signed_in"
    # keeping works, changing the permission of a group grant does not
    r = put(env.alice, {"etag": j["etag"], "keep": [{"uri": ALL, "permission": "READ"}],
                        "add": []})
    assert r.status_code == 200 and (ALL, "READ") in moto_grants(env)
    assert (aclmod.SIGNED_IN, "READ") not in moto_grants(env)
    r = put(env.alice, body(env, keep=[{"uri": ALL, "permission": "FULL_CONTROL"}]))
    assert r.status_code == 422
    r = put(env.alice, body(env))
    assert r.status_code == 200 and moto_grants(env) == [(owner_id(env), "FULL_CONTROL")]


def test_unknown_canonical_id_is_shown_and_removable(env):
    seed(env, [("ghost-id-123", "READ")])
    g = [x for x in get(env.alice).json()["grants"] if x["id"] == "ghost-id-123"][0]
    assert g["kind"] == "unknown" and g["removable"] is True
    r = put(env.alice, body(env, keep=[{"id": "ghost-id-123", "permission": "READ"}]))
    assert r.status_code == 200 and ("ghost-id-123", "READ") in moto_grants(env)
    assert put(env.alice, body(env)).status_code == 200
    assert moto_grants(env) == [(owner_id(env), "FULL_CONTROL")]


def test_keep_of_a_grantee_not_in_the_acl_is_refused(env):
    r = put(env.alice, body(env, keep=[{"id": "bob", "permission": "READ"}]))
    assert r.status_code == 422
    r = put(env.alice, body(env, keep=[{"permission": "READ"}]))
    assert r.status_code == 422


def test_etag_conflict(env):
    old = get(env.alice).json()["etag"]
    r = put(env.alice, body(env, add=[{"login": "bob", "permission": "READ"}]))
    assert r.status_code == 200
    r = put(env.alice, {"etag": old, "keep": [], "add": []})
    assert r.status_code == 409
    j = r.json()
    assert j["etagMismatch"] is True and j["current"]["etag"] != old
    assert any(g["id"] == "bob" for g in j["current"]["grants"])
    assert ("bob", "READ") in moto_grants(env)


def test_ineligible_recipient_is_uniform_404(env):
    for who in ("frank", "nobody"):
        r = put(env.alice, body(env, add=[{"login": who, "permission": "READ"}]))
        assert r.status_code == 404 and r.json()["detail"] == sharing.MSG_NOT_FOUND
    assert acl_audit(env) == []


def test_lookups_are_rate_limited(env):
    for _ in range(sharing.LOOKUP_LIMIT):
        put(env.alice, body(env, add=[{"login": "nobody", "permission": "READ"}]))
    r = put(env.alice, body(env, add=[{"login": "erin", "permission": "READ"}]))
    assert r.status_code == 429


def test_other_users_bucket_is_404(env):
    assert get(env.alice, "bob-shared").status_code == 404
    assert put(env.alice, {"etag": "x"}, "bob-shared").status_code == 404
    assert get(env.alice, "bob-shared", KEY).status_code == 404
    assert get(env.alice, "nope").status_code == 404


def test_suspended_csrf_and_anonymous(env):
    assert put(env.alice, body(env), headers={}).status_code == 403
    assert env.anon().get("/api/buckets/alice-data/acl").status_code == 401
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert get(env.alice).status_code == 403


# -- objects ----------------------------------------------------------------------------------


class ObjectAclStore:
    """moto ignores ACL documents on objects, so object ACLs live in this stand-in (the real
    client still does everything else, e.g. the bucket and the key check)."""

    def __init__(self, real, owner):
        self.real, self.owner = real, owner
        self.acls: dict[str, list] = {}
        self.puts: list[dict] = []

    def get_object_acl(self, Bucket, Key):
        self.real.head_object(Bucket=Bucket, Key=Key)
        grants = self.acls.get(Key) or [{"Grantee": {"Type": "CanonicalUser", "ID": self.owner},
                                         "Permission": "FULL_CONTROL"}]
        return {"Owner": {"ID": self.owner}, "Grants": grants}

    def put_object_acl(self, Bucket, Key, AccessControlPolicy):
        self.puts.append(AccessControlPolicy)
        self.acls[Key] = AccessControlPolicy["Grants"]

    def __getattr__(self, name):
        return getattr(self.real, name)


def fake_objects(env):
    store = {}
    real = env.app.state.s3.factory

    def factory(*a):
        store.setdefault("s", ObjectAclStore(real(*a), owner_id(env)))
        return store["s"]

    env.app.state.s3.factory = factory
    return store


def test_object_acl_roundtrip(env):
    store = fake_objects(env)
    j = get(env.alice, key=KEY).json()
    assert j["scope"] == "object" and j["key"] == KEY and "WRITE" not in j["permissions"]
    r = put(env.alice, body(env, add=[{"login": "bob", "permission": "READ"}], key=KEY), key=KEY)
    assert r.status_code == 200 and r.json()["changed"] is True
    doc = store["s"].puts[0]
    assert doc["Owner"] == {"ID": owner_id(env)}
    assert {(g["Grantee"]["ID"], g["Permission"]) for g in doc["Grants"]} == {
        (owner_id(env), "FULL_CONTROL"), ("bob", "READ")}
    d = env.audit("object.acl")[0]
    assert d["target"] == "alice-data" and d["details"]["key"] == KEY
    assert get(env.alice, key=KEY).json()["grants"][1]["id"] == "bob"
    # bucket ACL of the same bucket is untouched
    assert moto_grants(env) == [(owner_id(env), "FULL_CONTROL")]


def test_object_write_permission_refused(env):
    r = put(env.alice, body(env, add=[{"login": "bob", "permission": "WRITE"}], key=KEY), key=KEY)
    assert r.status_code == 422


def test_object_key_validation_and_missing_object(env):
    assert get(env.alice, key="").status_code == 422
    assert get(env.alice, key="a\nb").status_code == 422
    assert get(env.alice, key="x" * 1025).status_code == 422
    r = get(env.alice, key="no/such/file")
    assert r.status_code == 404
    assert put(env.alice, {"etag": "x"}, key="no/such/file").status_code == 404


def test_access_denied_is_403(env):
    broken = MagicMock()
    err = ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObjectAcl")
    broken.get_object_acl.side_effect = err
    broken.get_bucket_acl.side_effect = err
    env.app.state.s3.factory = lambda *a: broken
    assert get(env.alice, key=KEY).status_code == 403
    assert get(env.alice).status_code == 403
    assert put(env.alice, {"etag": "x"}, key=KEY).status_code == 403


# -- hardening -------------------------------------------------------------------------------


def test_anonymous_id_is_public_removable_never_addable(env):
    seed(env, [("anonymous", "READ")])
    g = [x for x in get(env.alice).json()["grants"] if x["id"] == "anonymous"][0]
    assert g["kind"] == "group" and g["scope"] == "public" and g["removable"] is True
    # cannot change its permission, cannot be added from nothing
    r = put(env.alice, body(env, keep=[{"id": "anonymous", "permission": "FULL_CONTROL"}]))
    assert r.status_code == 422 and "Public access is not enabled" in r.json()["detail"]
    r = put(env.alice, body(env, keep=[{"id": "anonymous", "permission": "READ"}]))
    assert r.status_code == 200
    assert put(env.alice, body(env)).status_code == 200
    assert moto_grants(env) == [(owner_id(env), "FULL_CONTROL")]
    r = put(env.alice, body(env, keep=[{"id": "anonymous", "permission": "READ"}]))
    assert r.status_code == 422


def test_anonymous_cannot_be_an_rgw_account_or_recipient(env):
    env.fake.add("anonymous", groups={USERS_GROUP}, display="Anon", mail="anon@example.org")
    r = put(env.alice, body(env, add=[{"login": "anonymous", "permission": "READ"}]))
    assert r.status_code == 409
    assert "anonymous" not in env.croit_state.users
    r = env.alice.post("/api/buckets/alice-data/shares", json={"login": "anonymous",
                                                               "access": "read"}, headers=H)
    assert r.status_code == 409


def test_etag_is_scoped_to_bucket_and_object(env):
    b = get(env.alice).json()["etag"]
    o = get(env.alice, key=KEY).json()["etag"]
    assert b != o
    fake_objects(env)
    r = put(env.alice, {"etag": b, "keep": [], "add": [{"login": "bob", "permission": "READ"}]},
            key=KEY)
    assert r.status_code == 409 and r.json()["etagMismatch"] is True
    o2 = get(env.alice, key="data/file-0002.bin").json()["etag"]
    assert o2 != o


def test_object_conflict_current_etag_is_usable(env):
    fake_objects(env)
    stale = get(env.alice, key=KEY).json()["etag"]
    assert put(env.alice, {"etag": stale, "keep": [], "add": [{"login": "bob",
                                                               "permission": "READ"}]},
               key=KEY).status_code == 200
    r = put(env.alice, {"etag": stale, "keep": [], "add": []}, key=KEY)
    assert r.status_code == 409
    current = r.json()["current"]
    assert current["etag"] == get(env.alice, key=KEY).json()["etag"]
    assert put(env.alice, {"etag": current["etag"], "keep": [], "add": []},
               key=KEY).status_code == 200
