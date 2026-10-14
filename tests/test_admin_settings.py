"""/api/admin/settings against the in-process mock croit, with fake-LDAP sessions."""

import json

import httpx
import pytest

from bucketeer.croit import CroitUnavailable

from .test_admin import Env, H


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


S = "/api/admin/settings"
EP = {"type": "https", "url": "https://hooks.example.org/in", "topic": "", "verifyTls": True}

ROUTES = [
    ("GET", S, None),
    ("PUT", f"{S}/naming", {"prefix": "", "pattern": ""}),
    ("POST", f"{S}/naming/test", {"name": "abc", "username": "alice"}),
    ("PUT", f"{S}/placement", {"default": None, "allowed": []}),
    ("PUT", f"{S}/default-quota", {"enabled": False}),
    ("DELETE", f"{S}/default-quota", None),
    ("PUT", f"{S}/uid-mappings/dave", {"rgwUid": "dave-legacy"}),
    ("DELETE", f"{S}/uid-mappings/dave", None),
    ("PUT", f"{S}/notification-endpoints/hook", EP),
    ("DELETE", f"{S}/notification-endpoints/hook", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_gating(env, method, path, body):
    def call(c):
        return c.request(method, path, json=body, headers=H)

    assert call(env.anon).status_code == 401
    assert call(env.user).status_code == 403
    assert env.transport.calls == []
    assert env.audit("admin.settings") == []
    assert env.db.settings.all() == {}


@pytest.mark.parametrize(("method", "path", "body"), [r for r in ROUTES if r[0] != "GET"])
def test_csrf_header_required(env, method, path, body):
    r = env.admin.request(method, path, json=body)
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    assert env.db.settings.all() == {} and env.db.uid_mappings.list() == []


def put(env, path, body):
    return env.admin.put(f"{S}{path}", json=body, headers=H)


# -- read -------------------------------------------------------------------------------


def test_defaults(env):
    r = env.admin.get(S)
    assert r.status_code == 200
    d = r.json()
    assert d["naming"] == {"prefix": "", "pattern": ""}
    one_tib = {"enabled": True, "maxSize": 2**40, "maxObjects": None}
    assert d["defaultQuota"] == {"isSet": False, "quota": None, "effective": one_tib,
                                 "default": one_tib}
    assert d["budgetNumber"] == {"pattern": "", "example": ""}
    assert d["uidMappings"] == [] and d["notificationEndpoints"] == []
    p = d["placement"]
    assert p["default"] is None and p["allowed"] == [] and p["error"] is None
    assert p["croitDefault"] == "default-placement"
    assert [a["name"] for a in p["available"]] == ["default-placement", "nvme-placement"]
    first = p["available"][0]
    assert first["isDefault"] and first["storageClasses"] == ["COLD", "STANDARD"]
    assert env.audit("admin.settings") == []  # reading audits nothing


def test_get_survives_croit_outage(env):
    env.transport.fail = lambda req: CroitUnavailable("down")
    d = env.admin.get(S).json()
    assert d["placement"]["available"] is None and "unreachable" in d["placement"]["error"]
    assert d["naming"]["prefix"] == ""


# -- naming ------------------------------------------------------------------------------


def test_naming_save_audit_and_reload(env):
    r = put(env, "/naming", {"prefix": "{username}-", "pattern": "[a-z0-9.-]+"})
    assert r.status_code == 200 and r.json()["prefix"] == "{username}-"
    assert env.admin.get(S).json()["naming"]["pattern"] == "[a-z0-9.-]+"
    (a,) = env.audit("admin.settings.naming")
    assert a["actor"] == "bob"
    assert a["details"]["before"] == {"prefix": "", "pattern": ""}
    assert a["details"]["after"]["prefix"] == "{username}-"


@pytest.mark.parametrize("body", [
    {"prefix": "BAD"}, {"prefix": "{x}"}, {"pattern": "("}, {"pattern": "x" * 201},
    {"pattern": "(a|aa)+b"}, {"pattern": "a{10000000}"}, {"pattern": "(?:a{3000}){3000}"},
    {"prefix": 1}, {"nope": 1},
])
def test_naming_validation(env, body):
    r = put(env, "/naming", body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert env.db.settings.all() == {} and env.audit("admin.settings") == []


def test_naming_body_must_be_object(env):
    assert env.admin.put(f"{S}/naming", json=[1], headers=H).status_code == 422


def test_naming_test_endpoint(env):
    body = {"rules": {"prefix": "{username}-", "pattern": ""}, "username": "Alice"}
    r = env.admin.post(f"{S}/naming/test", json={**body, "name": "alice-data"}, headers=H)
    assert r.json() == {"rulesError": None, "errors": [], "prefix": "alice-", "dnsOnly": []}
    r = env.admin.post(f"{S}/naming/test", json={**body, "name": "bob-data"}, headers=H).json()
    assert r["errors"] and r["prefix"] == "alice-"
    r = env.admin.post(f"{S}/naming/test", json={**body, "rules": {"pattern": "("}, "name": "x"},
                       headers=H).json()
    assert r["rulesError"] and r["errors"] == []
    assert env.admin.post(f"{S}/naming/test", json={"name": 1, "username": "a"},
                          headers=H).status_code == 422
    assert env.db.settings.all() == {}  # testing never saves


# -- placement ---------------------------------------------------------------------------


def test_placement_save_validated_against_croit(env):
    r = put(env, "/placement", {"default": "nvme-placement",
                                "allowed": ["nvme-placement", "default-placement"]})
    assert r.status_code == 200
    j = r.json()
    assert j["default"] == "nvme-placement"
    assert j["allowed"] == ["default-placement", "nvme-placement"]
    assert j["stale"] == []
    (a,) = env.audit("admin.settings.placement")
    assert a["details"]["after"]["default"] == "nvme-placement"
    assert env.admin.get(S).json()["placement"]["default"] == "nvme-placement"


@pytest.mark.parametrize("body", [
    {"default": "ghost"}, {"allowed": ["default-placement", "ghost"]}, {"default": 5},
    {"allowed": "x"},
])
def test_placement_rejects_unknown(env, body):
    r = put(env, "/placement", body)
    assert r.status_code == 422
    assert env.db.settings.all() == {}


def test_placement_stale_after_cluster_change(env):
    env.db.settings.set("placement", {"default": "gone", "allowed": ["gone", "nvme-placement"]})
    p = env.admin.get(S).json()["placement"]
    assert p["stale"] == ["gone"]
    assert p["allowed"] == ["gone", "nvme-placement"]


def test_placement_croit_down_is_503_and_nothing_saved(env):
    env.transport.fail = lambda req: CroitUnavailable("down")
    r = put(env, "/placement", {"default": None, "allowed": []})
    assert r.status_code == 503
    assert env.db.settings.all() == {}


def test_placement_croit_error_is_502(env):
    env.transport.fail = lambda req: httpx.Response(500, json={"code": 500, "message": "boom"})
    assert put(env, "/placement", {"default": None, "allowed": []}).status_code == 502


# -- default quota -----------------------------------------------------------------------


def test_default_quota_set_unset_and_zero(env):
    r = put(env, "/default-quota", {"enabled": True, "maxSize": 5 * 2**30, "maxObjects": None})
    stored = {"enabled": True, "maxSize": 5 * 2**30, "maxObjects": None}
    assert r.json()["isSet"] is True and r.json()["quota"] == stored
    assert r.json()["effective"] == stored
    assert env.admin.get(S).json()["defaultQuota"]["isSet"] is True
    r = put(env, "/default-quota", {"enabled": True, "maxSize": 0})  # 0 is a real limit
    assert r.status_code == 200 and r.json()["quota"]["maxSize"] == 0
    r = put(env, "/default-quota", {"enabled": False})  # explicitly unlimited != not set
    assert r.json()["isSet"] is True and r.json()["quota"]["enabled"] is False
    r = env.admin.delete(f"{S}/default-quota", headers=H)
    assert r.json()["isSet"] is False and r.json()["quota"] is None
    assert r.json()["effective"]["maxSize"] == 2**40  # unset = the 1 TiB default
    assert env.admin.get(S).json()["defaultQuota"]["isSet"] is False
    assert len(env.audit("admin.settings.default_quota")) == 4
    # unsetting something not set is a no-op without an audit entry
    env.admin.delete(f"{S}/default-quota", headers=H)
    assert len(env.audit("admin.settings.default_quota")) == 4
    assert env.transport.mutations() == []  # store only: croit untouched


@pytest.mark.parametrize("body", [
    {"enabled": True}, {"enabled": True, "maxSize": -1}, {"enabled": True, "maxSize": 2**62 + 1},
    {"enabled": True, "maxObjects": 1.5}, {"enabled": "yes", "maxSize": 1}, {"maxSize": 1},
    {"enabled": True, "maxSize": "5"}, {"enabled": True, "maxSize": True},
])
def test_default_quota_validation(env, body):
    assert put(env, "/default-quota", body).status_code == 422
    assert env.db.settings.all() == {}


# -- uid mappings ------------------------------------------------------------------------


def test_mapping_crud_and_audit(env):
    r = put(env, "/uid-mappings/dave", {"rgwUid": "alice"})  # alice exists in the mock
    assert r.status_code == 200
    assert r.json()["exists"] is True and r.json()["warning"] is None
    r = put(env, "/uid-mappings/erin", {"rgwUid": "acme$carol"})
    assert r.status_code == 200 and r.json()["rgwUid"] == "acme$carol"
    lst = env.admin.get(S).json()["uidMappings"]
    assert [(m["adUsername"], m["rgwUid"], m["exists"]) for m in lst] == [
        ("dave", "alice", True), ("erin", "acme$carol", True)]
    # change a mapping
    assert put(env, "/uid-mappings/dave", {"rgwUid": "bob"}).status_code == 200
    d = env.audit("admin.settings.uid_mapping.set")
    assert [x["details"]["after"] for x in d] == ["bob", "acme$carol", "alice"]
    assert d[0]["details"]["before"] == "alice"
    r = env.admin.delete(f"{S}/uid-mappings/dave", headers=H)
    assert r.status_code == 200
    assert env.admin.delete(f"{S}/uid-mappings/dave", headers=H).status_code == 404
    assert [x["details"] for x in env.audit("admin.settings.uid_mapping.delete")] == [
        {"before": "bob", "after": None}]
    assert env.transport.mutations() == []


def test_mapping_to_the_reserved_anonymous_uid_is_refused(env):
    for uid in ("anonymous", "Anonymous", "acme$anonymous"):
        r = put(env, "/uid-mappings/someone", {"rgwUid": uid})
        assert r.status_code == 422 and "reserved" in r.json()["detail"], uid
    assert env.db.uid_mappings.list() == []


def test_mapping_to_a_bucket_key_uid_is_refused(env):
    assert put(env, "/uid-mappings/dave", {"rgwUid": "john+backup"}).status_code == 422
    assert put(env, "/uid-mappings/dave", {"rgwUid": "acme$john+x"}).status_code == 422
    env.db.bucket_keys.create("legacy.key", "alice", "alice-data", "read")
    env.db.bucket_keys.revoke(1)  # revoked rows stay reserved
    r = put(env, "/uid-mappings/dave", {"rgwUid": "legacy.key"})
    assert r.status_code == 422 and "bucket key" in r.json()["detail"]
    assert env.db.uid_mappings.list() == []


def test_mapping_warns_when_rgw_user_missing(env):
    r = put(env, "/uid-mappings/dave", {"rgwUid": "nobody-here"})
    assert r.status_code == 200  # a warning, not an error
    assert r.json()["exists"] is False and "No RGW user" in r.json()["warning"]
    assert env.admin.get(S).json()["uidMappings"][0]["exists"] is False
    assert env.db.uid_mappings.get("dave")["rgw_uid"] == "nobody-here"


def test_mapping_warns_when_croit_down(env):
    env.transport.fail = lambda req: CroitUnavailable("down")
    r = put(env, "/uid-mappings/dave", {"rgwUid": "x1"})
    assert r.status_code == 200 and r.json()["exists"] is None and r.json()["warning"]


def test_mapping_uniqueness_both_ways(env):
    assert put(env, "/uid-mappings/dave", {"rgwUid": "shared"}).status_code == 200
    r = put(env, "/uid-mappings/erin", {"rgwUid": "SHARED"})
    assert r.status_code == 409 and "already mapped from dave" in r.json()["detail"]
    # AD names are case-insensitive: DAVE is the same row, so it updates
    assert put(env, "/uid-mappings/DAVE", {"rgwUid": "other"}).status_code == 200
    assert len(env.db.uid_mappings.list()) == 1
    assert put(env, "/uid-mappings/erin", {"rgwUid": "shared"}).status_code == 200
    # identity mapping is pointless
    assert put(env, "/uid-mappings/frank", {"rgwUid": "Frank"}).status_code == 409


@pytest.mark.parametrize(("ad", "body"), [
    ("dave", {"rgwUid": ""}), ("dave", {"rgwUid": "a b"}), ("dave", {"rgwUid": "a/b"}),
    ("dave", {"rgwUid": "$x"}), ("dave", {"rgwUid": "t$"}), ("dave", {"rgwUid": "a$b$c"}),
    ("dave", {"rgwUid": 5}), ("dave", {}), ("dave", {"rgwUid": "ok", "extra": 1}),
    ("da ve", {"rgwUid": "ok"}), ("da%40ve", {"rgwUid": "ok"}), ("x" * 65, {"rgwUid": "ok"}),
])
def test_mapping_syntax(env, ad, body):
    assert put(env, f"/uid-mappings/{ad}", body).status_code == 422
    assert env.db.uid_mappings.list() == [] and env.audit("admin.settings") == []


# -- notification endpoints --------------------------------------------------------------


def test_endpoint_crud_and_audit(env):
    r = put(env, "/notification-endpoints/ci-hook", EP)
    assert r.status_code == 200 and r.json()["name"] == "ci-hook"
    k = {"type": "kafka", "url": "kafka://k1.example.org:9092", "topic": "events"}
    assert put(env, "/notification-endpoints/bus", k).status_code == 200
    a = {"type": "amqps", "url": "amqps://mq:5671"}
    assert put(env, "/notification-endpoints/q", {**a, "type": "amqp"}).status_code == 200
    names = [e["name"] for e in env.admin.get(S).json()["notificationEndpoints"]]
    assert names == ["bus", "ci-hook", "q"]
    # replace
    r = put(env, "/notification-endpoints/CI-HOOK", {**EP, "verifyTls": False})
    assert r.status_code == 200
    eps = env.admin.get(S).json()["notificationEndpoints"]
    assert len(eps) == 3 and next(e for e in eps if e["name"] == "CI-HOOK")["verifyTls"] is False
    assert env.admin.delete(f"{S}/notification-endpoints/bus", headers=H).status_code == 200
    assert env.admin.delete(f"{S}/notification-endpoints/bus", headers=H).status_code == 404
    audit = env.audit("admin.settings.notification_endpoint")
    assert [x["action"].rsplit(".", 1)[1] for x in audit] == ["delete", "set", "set", "set", "set"]
    assert audit[1]["details"]["before"]["name"] == "ci-hook"


@pytest.mark.parametrize("body", [
    {"type": "https", "url": "http://x.org"}, {"type": "http", "url": "https://x.org"},
    {"type": "kafka", "url": "https://x.org"}, {"type": "amqp", "url": "http://x"},
    {"type": "gopher", "url": "gopher://x"}, {"type": "https", "url": "https://u:p@x.org"},
    {"type": "https", "url": "https://x.org/?t=1"}, {"type": "https", "url": "x.org"},
    {"type": "https"}, {"url": "https://x.org"}, {**EP, "password": "hunter2"},
    {**EP, "username": "u"}, {**EP, "topic": "a b"},
])
def test_endpoint_validation(env, body):
    r = put(env, "/notification-endpoints/e1", body)
    assert r.status_code == 422
    assert env.db.settings.all() == {} and env.audit("admin.settings") == []


@pytest.mark.parametrize("name", ["-x", "a b", "x" * 49, "a%25b"])
def test_endpoint_name_validation(env, name):
    assert put(env, f"/notification-endpoints/{name}", EP).status_code == 422


def test_endpoint_limit(env):
    for i in range(50):
        assert put(env, f"/notification-endpoints/e{i}", EP).status_code == 200
    assert put(env, "/notification-endpoints/one-too-many", EP).status_code == 422
    assert put(env, "/notification-endpoints/e7", EP).status_code == 200  # replace still ok


def test_no_secrets_anywhere(env):
    """Credentials are refused outright in v1, so none reach storage, the audit log
    or any response."""
    secret = "hunter2-SECRET"
    for body in ({**EP, "password": secret}, {**EP, "url": f"https://u:{secret}@x.org/"},
                 {**EP, "url": f"https://x.org/?token={secret}"}):
        assert put(env, "/notification-endpoints/e1", body).status_code == 422
    put(env, "/notification-endpoints/e1", EP)
    put(env, "/uid-mappings/dave", {"rgwUid": "alice"})
    put(env, "/naming", {"prefix": "{username}-"})
    blob = json.dumps(env.admin.get(S).json()) + json.dumps(env.db.audit.query(limit=1000))
    blob += json.dumps(env.db.settings.all())
    assert secret not in blob
    assert "token" not in env.admin.get(S).text.lower().replace("pattern", "")


def test_one_audit_entry_per_change_and_actor(env):
    put(env, "/naming", {"prefix": "a-"})
    put(env, "/default-quota", {"enabled": True, "maxSize": 1})
    put(env, "/placement", {"default": None, "allowed": []})
    acts = [(a["action"], a["actor"]) for a in env.audit("admin.settings")]
    assert sorted(acts) == [
        ("admin.settings.default_quota", "bob"),
        ("admin.settings.naming", "bob"),
        ("admin.settings.placement", "bob"),
    ]
