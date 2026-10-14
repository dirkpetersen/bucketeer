"""Tests for the mock croit API (mock/croit_mock.py), including schema checks
of its responses against docs/api/api-1.yaml."""

import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from mock.croit_mock import ADMIN_TOKEN, VIEWER_TOKEN, create_app

SPEC_PATH = Path(__file__).parent.parent / "docs" / "api" / "api-1.yaml"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}


@pytest.fixture
def app():
    return create_app(seed=True, task_seconds=0)


@pytest.fixture
def c(app):
    """Admin-authenticated client."""
    return TestClient(app, headers=ADMIN)


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def delete_bucket(c, name, token=None, **body):
    params = {"ignoreWarning": token} if token else None
    return c.request("DELETE", f"/s3/buckets/{name}", params=params, json={"name": name, **body})


def confirm_delete(c, name):
    """Delete a non-empty bucket through the full refusal + confirmation flow."""
    warning = delete_bucket(c, name).json()["ignorableWarning"]
    sentence = c.app.state.croit.delete_sentence(name)
    return delete_bucket(c, name, warning["token"], deleteData=True, thisWillDeleteData=sentence)


# --- auth -----------------------------------------------------------------------


def test_unauthenticated_is_401_in_error_shape(app):
    r = TestClient(app).get("/s3/users")
    assert r.status_code == 401
    assert {"code", "message"} <= set(r.json()) and r.json()["code"] == 401
    bad = {"Authorization": "Bearer nope"}
    assert TestClient(app).get("/s3/users", headers=bad).status_code == 401


def test_login_basic_and_token_info(app):
    anon = TestClient(app)
    r = anon.post("/auth/login", data={"grant_type": "client_credentials"},
                  headers=basic("admin", "admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and "admin" in body["roles"]
    info = anon.get("/auth/token-info", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert info.json()["username"] == "admin"
    # the session is a usable bearer token
    assert anon.get("/s3/user-names",
                    headers={"Authorization": f"Bearer {body['access_token']}"}).status_code == 200


def test_login_rejects_bad_credentials_and_grant_type(app):
    anon = TestClient(app)
    form = {"grant_type": "client_credentials"}
    assert anon.post("/auth/login", data=form, headers=basic("admin", "wrong")).status_code == 401
    assert anon.post("/auth/login", data=form).status_code == 401
    r = anon.post("/auth/login", data={"grant_type": "password"}, headers=basic("admin", "admin"))
    assert r.status_code == 400


def test_login_form(app):
    anon = TestClient(app)
    ok = anon.post("/auth/login-form",
                   data={"grant_type": "password", "username": "viewer", "password": "viewer"})
    assert ok.status_code == 200 and ok.json()["roles"] == ["viewer"]
    bad = anon.post("/auth/login-form",
                    data={"grant_type": "password", "username": "viewer", "password": "x"})
    assert bad.status_code == 401
    wrong = anon.post("/auth/login-form",
                      data={"grant_type": "client_credentials", "username": "a", "password": "b"})
    assert wrong.status_code == 400


def test_viewer_can_read_but_not_mutate(app):
    v = TestClient(app, headers=VIEWER)
    assert v.get("/s3/users").status_code == 200
    assert v.get("/auth/token-info").json()["roles"] == ["viewer"]
    r = v.patch("/s3/users/alice", json={"suspended": True})
    assert r.status_code == 403 and r.json()["code"] == 403
    assert v.put("/s3/buckets/newone", json={"owner": "alice"}).status_code == 403
    assert v.get("/api-tokens").status_code == 403
    assert v.get("/config/rgw-ssl-cert").status_code == 403


def test_securefolder_token_has_no_s3_access(c, app):
    secret = c.post("/api-tokens", json={"name": "sf", "roles": ["securefolder"]}).json()
    sf = TestClient(app, headers={"Authorization": f"Bearer {secret}"})
    assert sf.get("/auth/token-info").status_code == 200
    assert sf.get("/s3/users").status_code == 403


def test_api_token_crud(c, app):
    secret = c.post("/api-tokens", json={"name": "ci", "roles": ["admin", "viewer"],
                                         "expiry": "2999-01-01T00:00:00Z"}).json()
    assert isinstance(secret, str)
    tokens = c.get("/api-tokens").json()
    mine = next(t for t in tokens if t["name"] == "ci")
    assert mine["expiresBy"].startswith("2999") and "secret" not in mine
    new = TestClient(app, headers={"Authorization": f"Bearer {secret}"})
    assert new.get("/s3/users").status_code == 200
    assert c.patch(f"/api-tokens/{mine['id']}", json={"roles": ["viewer"]}).status_code == 204
    assert new.patch("/s3/users/alice", json={"name": "A"}).status_code == 403
    assert c.delete(f"/api-tokens/{mine['id']}").status_code == 204
    assert new.get("/s3/users").status_code == 401
    assert c.delete(f"/api-tokens/{mine['id']}").status_code == 404


def test_api_token_validation_and_expiry(c, app):
    assert c.post("/api-tokens", json={"name": "x", "roles": ["admin"]}).status_code == 400
    assert c.post("/api-tokens", json={"name": "x", "roles": []}).status_code == 400
    assert c.post("/api-tokens", json={"name": "", "roles": ["viewer"]}).status_code == 400
    assert c.post("/api-tokens", json={"name": "x", "roles": ["root"]}).status_code == 400
    old = c.post("/api-tokens", json={"name": "old", "roles": ["viewer"],
                                      "expiry": "2000-01-01T00:00:00Z"}).json()
    r = TestClient(app).get("/s3/users", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401


# --- users ----------------------------------------------------------------------


def test_seed_users_and_names(c):
    assert set(c.get("/s3/user-names").json()) == {"alice", "bob", "acme$carol"}
    users = {u["uid"]: u for u in c.get("/s3/users").json()}
    assert users["alice"]["email"] == "alice@example.org"
    only = c.get("/s3/users", params=[("uid", "bob")]).json()
    assert [u["uid"] for u in only] == ["bob"]


def test_user_create_get_patch_suspend_delete(c):
    r = c.post("/s3/users", json={"uid": "dave", "name": "Dave", "email": "dave@example.org"})
    assert r.status_code == 204
    assert c.post("/s3/users", json={"uid": "dave", "name": "Again"}).status_code == 409
    assert len(c.get("/s3/users/dave/keys").json()) == 1  # RGW generates a key pair
    assert c.patch("/s3/users/dave", json={"suspended": True, "name": "D"}).status_code == 204
    user = c.get("/s3/users", params={"uid": "dave"}).json()[0]
    assert user["suspended"] is True and user["name"] == "D"
    assert c.patch("/s3/users/dave", json={"suspended": False}).status_code == 204
    assert c.delete("/s3/users/dave").status_code == 204
    assert c.get("/s3/users/dave/keys").status_code == 404
    assert c.patch("/s3/users/dave", json={"name": "x"}).status_code == 404


def test_duplicate_email_rejected(c):
    r = c.post("/s3/users", json={"uid": "eve", "name": "Eve", "email": "ALICE@example.org"})
    assert r.status_code == 409 and "email" in r.json()["message"].lower()
    c.post("/s3/users", json={"uid": "eve", "name": "Eve", "email": "eve@example.org"})
    assert c.patch("/s3/users/eve", json={"email": "bob@example.org"}).status_code == 409
    assert c.patch("/s3/users/eve", json={"email": "eve@example.org"}).status_code == 204


def test_delete_user_with_buckets_refused(c):
    r = c.delete("/s3/users/alice")
    assert r.status_code == 409
    assert "alice-data" in r.json()["details"]
    assert delete_bucket(c, "alice-empty").status_code == 200
    assert c.delete("/s3/users/alice").status_code == 409  # alice-data remains
    assert confirm_delete(c, "alice-data").status_code == 200
    assert c.delete("/s3/users/alice").status_code == 204


def test_user_quota_and_bucket_quota(c):
    body = {"enabled": True, "maxSize": 1000, "maxObjects": 5}
    assert c.put("/s3/users/bob/quota", json=body).status_code == 204
    assert c.put("/s3/users/bob/bucket-quota", json={"enabled": False}).status_code == 204
    bob = c.get("/s3/users", params={"uid": "bob"}).json()[0]
    assert bob["userQuota"]["enabled"] is True and bob["userQuota"]["maxSize"] == 1000
    assert bob["bucketQuota"]["enabled"] is False
    assert c.put("/s3/users/nobody/quota", json=body).status_code == 404


def test_tenant_uid_encoding(c):
    for path in ("acme$carol", "acme%24carol"):
        r = c.get(f"/s3/users/{path}/keys")
        assert r.status_code == 200 and r.json()[0]["user"] == "acme$carol"
    c.post("/s3/users", json={"uid": "dan", "tenant": "acme", "name": "Dan"})
    assert "acme$dan" in c.get("/s3/user-names").json()
    r = c.put("/s3/buckets/dan-bucket", json={"owner": "acme$dan"})
    assert r.status_code == 200
    names = [b["bucket"] for b in c.get("/s3/users/acme%24dan/buckets").json()]
    assert names == ["acme:dan-bucket"]
    b = c.get("/s3/users/acme%24dan/buckets").json()[0]
    assert b["tenant"] == "acme" and b["owner"] == "acme$dan"
    assert c.get("/s3/buckets/acme:dan-bucket").status_code == 200
    assert c.get("/s3/buckets/dan-bucket").status_code == 404
    # a bucket name that carries its own tenant is refused: the owner decides
    assert c.put("/s3/buckets/acme:other", json={"owner": "alice"}).status_code == 400


# --- keys -----------------------------------------------------------------------


def test_keys_get_single_and_cfg_file(c):
    keys = c.get("/s3/users/alice/keys").json()
    assert keys == [{"user": "alice", "access_key": "ALICEACCESSKEY00001",
                     "secret_key": "alice-secret-0001"}]
    assert c.get("/s3/users/alice/key").json() == keys[0]
    cfg = c.get("/s3/users/alice/cfg-file/download").json()
    assert "access_key = ALICEACCESSKEY00001" in cfg["fileContents"]
    assert "host_base = 127.0.0.1:7480" in cfg["fileContents"]


def test_single_key_put_delete(c):
    assert c.put("/s3/users/alice/keys/NEWKEY1", params={"secretKey": "s1"}).status_code == 204
    assert {k["access_key"] for k in c.get("/s3/users/alice/keys").json()} == {
        "ALICEACCESSKEY00001", "NEWKEY1"}
    # overwrite
    c.put("/s3/users/alice/keys/NEWKEY1", params={"secretKey": "s2"})
    secrets_ = {k["access_key"]: k["secret_key"] for k in c.get("/s3/users/alice/keys").json()}
    assert secrets_["NEWKEY1"] == "s2"
    # a key already owned by another user is refused
    r = c.put("/s3/users/bob/keys/NEWKEY1", params={"secretKey": "x"})
    assert r.status_code == 409
    assert c.delete("/s3/users/alice/keys/NEWKEY1").status_code == 204
    assert c.delete("/s3/users/alice/keys/NEWKEY1").status_code == 404
    c.delete("/s3/users/alice/keys/ALICEACCESSKEY00001")
    assert c.get("/s3/users/alice/key").json() is None


def test_whole_set_put_drops_missing_keys_and_returns_task(c):
    c.put("/s3/users/alice/keys/EXTRA", params={"secretKey": "e"})
    r = c.put("/s3/users/alice/keys", json={"keys": [
        {"user": "alice", "access_key": "ONLYKEY", "secret_key": "only"}]})
    assert r.status_code == 200
    task = r.json()
    assert task["done"] is True and task["state"] == "FINISHED" and task["statusCode"] == 0
    assert [k["access_key"] for k in c.get("/s3/users/alice/keys").json()] == ["ONLYKEY"]
    # the dropped keys no longer exist for single-key DELETE
    assert c.delete("/s3/users/alice/keys/EXTRA").status_code == 404
    # and an empty set removes everything
    c.put("/s3/users/alice/keys", json={"keys": []})
    assert c.get("/s3/users/alice/keys").json() == []
    bad = c.put("/s3/users/alice/keys", json={"keys": [
        {"user": "bob", "access_key": "K", "secret_key": "s"}]})
    assert bad.status_code == 400


# --- buckets --------------------------------------------------------------------


def sentence_for(c, name):
    """Provoke the first refusal and read the confirmation sentence from it."""
    r = delete_bucket(c, name)
    assert r.status_code == 400
    return r.json()["details"]


def test_bucket_list_create_patch(c):
    names = {b["bucket"] for b in c.get("/s3/buckets").json()}
    assert names == {"alice-data", "alice-empty", "bob-shared", "acme:reports"}
    r = c.put("/s3/buckets/fresh", json={"owner": "bob", "versioning": "ENABLED"})
    assert r.status_code == 200 and r.json()["name"]
    assert c.get("/s3/buckets/fresh").json()["versioning"] == "ENABLED"
    assert c.put("/s3/buckets/fresh", json={"owner": "bob"}).status_code == 409
    assert c.put("/s3/buckets/Bad_Name", json={"owner": "bob"}).status_code == 400
    assert c.put("/s3/buckets/ghost-owned", json={"owner": "nobody"}).status_code == 404
    assert c.put("/s3/buckets/x1234", json={"owner": "bob", "acl": []}).status_code == 400
    # S3 cannot switch versioning off again: OFF suspends
    assert c.patch("/s3/buckets/fresh", json={"versioning": "OFF"}).status_code == 204
    assert c.get("/s3/buckets/fresh").json()["versioning"] == "SUSPENDED"
    # owner and ACL
    acl = [{"grantee": "ALL_USERS", "permission": "Read"}]
    assert c.patch("/s3/buckets/fresh", json={"owner": "alice", "acl": acl}).status_code == 204
    assert c.get("/s3/buckets/fresh").json()["acl"] == acl
    assert [b["bucket"] for b in c.get("/s3/users/alice/buckets").json()].count("fresh") == 1


def test_object_lock_turns_versioning_on(c):
    c.put("/s3/buckets/locked-one", json={"owner": "bob", "objectLock": True})
    assert c.get("/s3/buckets/locked-one").json()["versioning"] == "ENABLED"
    assert c.patch("/s3/buckets/locked-one", json={"versioning": "OFF"}).status_code == 400


def test_delete_empty_bucket_needs_no_confirmation(c):
    r = delete_bucket(c, "alice-empty")
    assert r.status_code == 200 and r.json()["name"]
    assert c.get("/s3/buckets/alice-empty").status_code == 404


def test_delete_non_empty_bucket_flow(c):
    first = delete_bucket(c, "alice-data")
    assert first.status_code == 400
    sentence = first.json()["details"]
    assert sentence in first.json()["message"]
    # deleteData without the sentence, and with a wrong one, are both refused
    assert delete_bucket(c, "alice-data", deleteData=True).status_code == 400
    wrong = delete_bucket(c, "alice-data", deleteData=True, thisWillDeleteData="yes")
    assert wrong.status_code == 400
    # the sentence without deleteData is refused too
    assert delete_bucket(c, "alice-data", thisWillDeleteData=sentence).status_code == 400
    # the repeated name has to match the path
    r = c.request("DELETE", "/s3/buckets/alice-data",
                  json={"name": "other", "deleteData": True, "thisWillDeleteData": sentence})
    assert r.status_code == 400
    assert c.get("/s3/buckets/alice-data").status_code == 200
    # right body but no token, wrong token: refused (and each refusal issues a new token)
    body = {"deleteData": True, "thisWillDeleteData": sentence}
    assert delete_bucket(c, "alice-data", **body).status_code == 400
    assert delete_bucket(c, "alice-data", "bogus", **body).status_code == 400
    # a token only counts for the bucket it was issued for
    other = delete_bucket(c, "alice-empty")  # empty: deleted straight away
    assert other.status_code == 200
    token = first.json()["ignorableWarning"]["token"]
    ok = delete_bucket(c, "alice-data", token, **body)
    assert ok.status_code == 200 and ok.json()["done"] is True
    assert c.get("/s3/buckets/alice-data").status_code == 404
    # tokens are single use
    c.put("/s3/buckets/alice-data", json={"owner": "alice"})
    c.app.state.croit.set_bucket_usage("alice-data", 1, 1)
    assert delete_bucket(c, "alice-data", token, **body).status_code == 400


def test_tenant_bucket_delete_uses_tenant_name(c):
    assert delete_bucket(c, "acme:reports").status_code == 200
    assert c.delete("/s3/users/acme%24carol").status_code == 204


def test_bucket_policy_roundtrip(c):
    assert c.get("/s3/buckets/bob-shared/policy").json() == {"policyText": None}
    policy = {"Version": "2012-10-17",
              "Statement": [{"Sid": "bucketeer-share-alice", "Effect": "Allow",
                             "Principal": {"AWS": ["arn:aws:iam:::user/alice"]},
                             "Action": ["s3:GetObject"],
                             "Resource": ["arn:aws:s3:::bob-shared/*"]}]}
    assert c.post("/s3/buckets/bob-shared/policy", json=policy).status_code == 204
    assert json.loads(c.get("/s3/buckets/bob-shared/policy").json()["policyText"]) == policy
    assert c.post("/s3/buckets/bob-shared/policy", content=b"not json").status_code == 400
    assert c.delete("/s3/buckets/bob-shared/policy").status_code == 204
    assert c.get("/s3/buckets/bob-shared/policy").json() == {"policyText": None}


def test_bucket_quota_and_usage(c):
    q = {"enabled": True, "maxSize": 2048, "maxObjects": 10}
    assert c.put("/s3/buckets/alice-data/quota", json=q).status_code == 204
    b = next(b for b in c.get("/s3/buckets").json() if b["bucket"] == "alice-data")
    assert b["bucket_quota"]["maxSize"] == 2048
    assert b["usageSum"]["numObjects"] == 42 and b["usageSum"]["size"] == 1048576
    by_bucket = c.get("/s3/usage/by-bucket/alice-data", params={"hours": 24}).json()
    assert "alice" in by_bucket["buckets"]
    by_user = c.get("/s3/usage/by-user/alice", params={"hours": 24}).json()
    assert set(by_user["buckets"]) == {"alice-data", "alice-empty"}
    assert c.get("/s3/usage/by-user/nobody", params={"hours": 1}).status_code == 404
    assert c.get("/s3/usage/by-user/alice").status_code == 400  # hours is required


def test_pagination_is_not_implemented(c):
    r = c.get("/s3/users", params={"pagination": "x"})
    assert r.status_code == 501 and r.json()["code"] == 501


def test_unknown_path_is_error_shaped(c):
    r = c.get("/nope")
    assert r.status_code == 404 and r.json()["code"] == 404


# --- tasks ----------------------------------------------------------------------


def test_task_lifecycle():
    app = create_app(seed=True, task_seconds=0.3)
    c = TestClient(app, headers=ADMIN)
    task = c.put("/s3/buckets/slow-one", json={"owner": "bob"}).json()
    assert task["state"] == "RUNNING" and task["done"] is False and task["statusCode"] == -1
    assert c.get(f"/tasks/{task['id']}").json()["state"] == "RUNNING"
    time.sleep(0.4)
    done = c.get(f"/tasks/{task['id']}").json()
    assert done["state"] == "FINISHED" and done["done"] is True and done["statusCode"] == 0
    assert c.get("/tasks/9999").status_code == 404


def test_task_seconds_zero_finishes_immediately(c):
    task = c.put("/s3/buckets/fast-one", json={"owner": "bob"}).json()
    assert task["state"] == "FINISHED"


# --- cert and HA groups (tools/croit-cert) ----------------------------------------


def test_rgw_cert_and_haproxy(c):
    assert c.get("/config/rgw-ssl-cert").json() == ""
    pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
    task = c.put("/config/rgw-ssl-cert", json={"certificate": pem, "doRollingRestart": True}).json()
    assert task["state"] == "FINISHED"
    assert c.get("/config/rgw-ssl-cert").json() == pem
    assert c.put("/config/rgw-ssl-cert", json={"certificate": None}).status_code == 400
    groups = c.get("/servers/ha-groups").json()
    assert groups[0]["haproxy"] and groups[0]["proxyMode"] == "http"
    sid = groups[0]["servers"][0]["server"]
    assert c.post(f"/servers/ha-groups/servers/{sid}/haproxy/stop").status_code == 204
    assert c.get("/servers/ha-groups").json()[0]["servers"][0]["haproxyRunning"] is False
    assert c.post(f"/servers/ha-groups/servers/{sid}/haproxy/restart").status_code == 204
    assert c.get("/servers/ha-groups").json()[0]["servers"][0]["haproxyRunning"] is True
    assert c.post(f"/servers/ha-groups/servers/{sid}/haproxy/explode").status_code == 400
    assert c.post("/servers/ha-groups/servers/99/haproxy/start").status_code == 404


# --- hooks and prefix -------------------------------------------------------------


def test_hooks_fire_for_buckets_and_keys():
    from mock.croit_mock import MockHooks

    events = []
    hooks = MockHooks(
        bucket_created=lambda b: events.append(("bucket+", b.name, b.owner)),
        bucket_deleted=lambda b: events.append(("bucket-", b.name)),
        key_added=lambda uid, a, s: events.append(("key+", uid, a)),
        key_removed=lambda uid, a: events.append(("key-", uid, a)),
    )
    c = TestClient(create_app(seed=False, task_seconds=0, hooks=hooks))
    app_state = c.app.state.croit
    app_state.add_api_token("t", ["admin", "viewer"], secret="tok")
    c.headers["Authorization"] = "Bearer tok"
    c.post("/s3/users", json={"uid": "zed", "name": "Zed"})
    c.put("/s3/buckets/zed-bucket", json={"owner": "zed"})
    delete_bucket(c, "zed-bucket")
    assert [e[0] for e in events] == ["key+", "bucket+", "bucket-"]
    c.delete("/s3/users/zed")
    assert events[-1][0] == "key-"


def test_prefix():
    c = TestClient(create_app(task_seconds=0, prefix="/api"), headers=ADMIN)
    assert c.get("/api/s3/user-names").status_code == 200
    assert c.get("/s3/user-names").status_code == 404


# --- schema validation against docs/api/api-1.yaml --------------------------------


class SpecLoader(yaml.SafeLoader):
    """YAML 1.2 booleans: the spec has an unquoted enum value `OFF`, which a
    YAML 1.1 loader would turn into False."""


SpecLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
SpecLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
)


@pytest.fixture(scope="module")
def spec():
    return yaml.load(SPEC_PATH.read_text(), Loader=SpecLoader)


@pytest.fixture(scope="module")
def registry(spec):
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    return Registry().with_resource("urn:croit-api", resource)


def check(spec, registry, resp, template, method=None):
    """Assert the response matches the spec for `template` and its status code."""
    method = (method or resp.request.method).lower()
    op = spec["paths"][template][method]
    responses = op["responses"]
    status = str(resp.status_code)
    assert status in responses or "default" in responses, f"{template}: undocumented {status}"
    declared = responses.get(status) or responses["default"]
    content = declared.get("content", {}).get("application/json")
    if not content:
        assert resp.content == b"", f"{template}: {status} should have no body"
        return
    pointer = "/".join(
        ["paths", template.replace("~", "~0").replace("/", "~1"), method, "responses",
         status if status in responses else "default", "content", "application~1json", "schema"]
    )
    schema = {"$ref": "urn:croit-api#/" + quote(pointer, safe="/~")}
    errors = list(Draft202012Validator(schema, registry=registry).iter_errors(resp.json()))
    assert not errors, f"{method.upper()} {template} {status}: {errors[0].message}"


def test_mock_routes_exist_in_spec(app, spec):
    from fastapi.routing import APIRoute

    for route in app.routes:
        if isinstance(route, APIRoute):
            assert route.path in spec["paths"], route.path
            for m in route.methods:
                assert m.lower() in spec["paths"][route.path], f"{m} {route.path}"


def test_responses_match_spec_schemas(app, c, spec, registry):
    def ck(resp, template, method=None):
        check(spec, registry, resp, template, method)
        return resp

    anon = TestClient(app)
    ck(anon.post("/auth/login", data={"grant_type": "client_credentials"},
                 headers=basic("admin", "admin")), "/auth/login")
    ck(anon.post("/auth/login-form",
                 data={"grant_type": "password", "username": "admin", "password": "admin"}),
       "/auth/login-form")
    ck(anon.post("/auth/login", data={"grant_type": "client_credentials"},
                 headers=basic("admin", "bad")), "/auth/login")  # 401 ErrorMessage
    ck(c.get("/auth/token-info"), "/auth/token-info")
    ck(c.post("/api-tokens", json={"name": "s", "roles": ["viewer"]}), "/api-tokens")
    ck(c.get("/api-tokens"), "/api-tokens")
    tid = c.get("/api-tokens").json()[-1]["id"]
    ck(c.patch(f"/api-tokens/{tid}", json={"roles": ["viewer"]}), "/api-tokens/{apiTokenId}")
    ck(c.delete(f"/api-tokens/{tid}"), "/api-tokens/{apiTokenId}")

    ck(c.get("/s3/user-names"), "/s3/user-names")
    ck(c.get("/s3/users"), "/s3/users")
    ck(c.post("/s3/users", json={"uid": "zoe", "name": "Zoe", "email": "z@example.org"}),
       "/s3/users")
    ck(c.post("/s3/users", json={"uid": "zoe", "name": "Zoe"}), "/s3/users")  # 409
    ck(c.patch("/s3/users/zoe", json={"suspended": True}), "/s3/users/{uid}")
    ck(c.put("/s3/users/zoe/quota", json={"enabled": True, "maxSize": 5}), "/s3/users/{uid}/quota")
    ck(c.put("/s3/users/zoe/bucket-quota", json={"enabled": False}),
       "/s3/users/{uid}/bucket-quota")
    ck(c.get("/s3/users/alice/buckets"), "/s3/users/{uid}/buckets")
    ck(c.get("/s3/users/alice/keys"), "/s3/users/{uid}/keys")
    ck(c.get("/s3/users/alice/key"), "/s3/users/{uid}/key")
    ck(c.get("/s3/users/alice/cfg-file/download"), "/s3/users/{uid}/cfg-file/download")
    ck(c.put("/s3/users/alice/keys/K1", params={"secretKey": "s"}),
       "/s3/users/{uid}/keys/{accessKey}")
    ck(c.delete("/s3/users/alice/keys/K1"), "/s3/users/{uid}/keys/{accessKey}")
    keys = c.get("/s3/users/alice/keys").json()
    ck(c.put("/s3/users/alice/keys", json={"keys": keys}), "/s3/users/{uid}/keys")
    ck(c.get("/s3/users/nobody/keys"), "/s3/users/{uid}/keys")  # 404 ErrorMessage

    ck(c.get("/s3/buckets"), "/s3/buckets")
    task = ck(c.put("/s3/buckets/schema-one", json={"owner": "acme$carol"}),
              "/s3/buckets/{bucketName}").json()
    ck(c.get(f"/tasks/{task['id']}"), "/tasks/{taskId}")
    ck(c.get("/s3/buckets/acme:schema-one"), "/s3/buckets/{bucketName}")
    ck(c.patch("/s3/buckets/acme:schema-one", json={"versioning": "ENABLED"}),
       "/s3/buckets/{bucketName}")
    ck(c.get("/s3/buckets/acme:schema-one/policy"), "/s3/buckets/{bucketName}/policy")
    ck(c.post("/s3/buckets/acme:schema-one/policy", json={"Statement": []}),
       "/s3/buckets/{bucketName}/policy")
    ck(c.get("/s3/buckets/acme:schema-one/policy"), "/s3/buckets/{bucketName}/policy")
    ck(c.delete("/s3/buckets/acme:schema-one/policy"), "/s3/buckets/{bucketName}/policy")
    ck(c.put("/s3/buckets/acme:schema-one/quota", json={"enabled": True}),
       "/s3/buckets/{bucketName}/quota")
    ck(c.get("/s3/usage/by-bucket/alice-data", params={"hours": 1}),
       "/s3/usage/by-bucket/{bucketName}")
    ck(c.get("/s3/usage/by-user/alice", params={"hours": 1}), "/s3/usage/by-user/{uid}")
    ck(delete_bucket(c, "acme:schema-one"), "/s3/buckets/{bucketName}")
    refusal = ck(delete_bucket(c, "alice-data"), "/s3/buckets/{bucketName}")
    assert refusal.status_code == 400
    ck(confirm_delete(c, "alice-data"), "/s3/buckets/{bucketName}")

    ck(c.get("/config/rgw-ssl-cert"), "/config/rgw-ssl-cert")
    ck(c.put("/config/rgw-ssl-cert", json={"certificate": "x"}), "/config/rgw-ssl-cert")
    ck(c.get("/servers/ha-groups"), "/servers/ha-groups")
    ck(c.post("/servers/ha-groups/servers/1/haproxy/restart"),
       "/servers/ha-groups/servers/{serverId}/haproxy/{action}")
