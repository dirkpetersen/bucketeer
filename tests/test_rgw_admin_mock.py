"""mock/rgw_admin_mock.py: the RGW Admin Ops mock (signing, caps, endpoints, shared state)."""

import asyncio
from urllib.parse import quote

import httpx
import pytest
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from fastapi.testclient import TestClient

from mock.croit_mock import ADMIN_TOKEN, MockHooks
from mock.croit_mock import create_app as create_croit
from mock.rgw_admin_mock import AdminPrincipal, parse_caps
from mock.rgw_admin_mock import create_app as create_ops
from mock.s3_bridge import S3Bridge

HOST = "http://rgw.test"
ERROR_KEYS = {"Code", "Message", "RequestId", "HostId"}


class Ops:
    """A SigV4-signing client (botocore's signer, independent of the mock's verifier)."""

    def __init__(self, app, principal=None, access=None, secret=None):
        self.http = TestClient(app, base_url=HOST)
        self.access = access or principal.access_key
        self.secret = secret or principal.secret_key

    def request(self, method, path, params=(), body=None, sign=True, extra_headers=None,
                access=None, secret=None):
        pairs = params.items() if isinstance(params, dict) else params
        query = "&".join(k if v is None else f"{k}={quote(str(v), safe='')}" for k, v in pairs)
        url = f"{HOST}{path}" + (f"?{query}" if query else "")
        data = body.encode() if isinstance(body, str) else body
        req = AWSRequest(method=method, url=url, data=data, headers=dict(extra_headers or {}))
        if sign:
            creds = Credentials(access or self.access, secret or self.secret)
            S3SigV4Auth(creds, "s3", "us-east-1").add_auth(req)
        return self.http.request(method, url, content=data, headers=dict(req.headers.items()))

    def get(self, path, **params):
        return self.request("GET", path, params)

    def put(self, path, body=None, **params):
        return self.request("PUT", path, params, body)

    def post(self, path, **params):
        return self.request("POST", path, params)

    def delete(self, path, **params):
        return self.request("DELETE", path, params)


@pytest.fixture
def ops(admin_ops):
    return Ops(admin_ops.app, admin_ops.admin)


def code(r, status):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == ERROR_KEYS and body["Message"] == ""
    return body["Code"]


# -- authentication -------------------------------------------------------------------


def test_signed_request_works(ops):
    r = ops.get("/admin/user", uid="alice")
    assert r.status_code == 200 and r.json()["user_id"] == "alice"


def test_unsigned_is_access_denied(ops):
    assert code(ops.request("GET", "/admin/user", {"uid": "alice"}, sign=False), 403) == \
        "AccessDenied"


def test_wrong_secret_is_signature_mismatch(ops):
    r = ops.request("GET", "/admin/user", {"uid": "alice"}, secret="not-the-secret")
    assert code(r, 403) == "SignatureDoesNotMatch"


def test_unknown_access_key(ops):
    r = ops.request("GET", "/admin/user", {"uid": "alice"}, access="NOSUCHKEY", secret="x")
    assert code(r, 403) == "InvalidAccessKeyId"


def test_signature_covers_query_and_body(admin_ops, ops):
    req = AWSRequest("GET", f"{HOST}/admin/user?uid=alice")
    S3SigV4Auth(Credentials(ops.access, ops.secret), "s3", "us-east-1").add_auth(req)
    r = ops.http.get(f"{HOST}/admin/user?uid=bob", headers=dict(req.headers.items()))
    assert code(r, 403) == "SignatureDoesNotMatch"
    # a body that differs from the signed payload hash
    body = '{"enabled": true, "max_objects": 5}'
    req = AWSRequest("PUT", f"{HOST}/admin/user?quota&uid=alice&quota-type=user", data=body)
    S3SigV4Auth(Credentials(ops.access, ops.secret), "s3", "us-east-1").add_auth(req)
    r = ops.http.put(f"{HOST}/admin/user?quota&uid=alice&quota-type=user",
                     content=b'{"enabled": true, "max_objects": 6}',
                     headers=dict(req.headers.items()))
    assert code(r, 400) == "XAmzContentSHA256Mismatch"
    assert admin_ops.state.users["alice"].user_quota["enabled"] is False


def test_valueless_flag_and_plus_in_uid_sign_consistently(admin_ops, ops):
    # values are decoded twice (Tentacle): `+` goes out as %252B, so the helper gets "%2B"
    r = ops.request("PUT", "/admin/user", [("uid", "alice%2Bbackup"), ("display-name", "B")])
    assert r.status_code == 200 and r.json()["user_id"] == "alice+backup"
    r = ops.request("GET", "/admin/user", [("quota", None), ("uid", "alice%2Bbackup"),
                                           ("quota-type", "user")])
    assert r.status_code == 200 and r.json()["enabled"] is False


def test_clock_skew_is_refused(admin_ops):
    import time

    app = create_ops(admin_ops.state, clock=lambda: time.time() + 3600)
    o = Ops(app, admin_ops.admin)
    assert code(o.get("/admin/user", uid="alice"), 403) == "RequestTimeTooSkewed"
    app = create_ops(admin_ops.state, clock=lambda: time.time() + 60)
    assert Ops(app, admin_ops.admin).get("/admin/user", uid="alice").status_code == 200


def test_regular_user_key_signs_but_has_no_caps(admin_ops):
    key = admin_ops.state.users["alice"].keys[0]
    me = Ops(admin_ops.app, access=key["access_key"], secret=key["secret_key"])
    assert code(me.get("/admin/user", uid="alice"), 403) == "AccessDenied"
    admin_ops.state.update_user("alice", {"suspended": True})
    assert code(me.get("/admin/user", uid="alice"), 403) == "UserSuspended"


# -- caps -----------------------------------------------------------------------------


def test_parse_caps():
    assert parse_caps("users=*; usage=read") == {
        "users": frozenset({"read", "write"}), "usage": frozenset({"read"})}


def test_readonly_caps_can_read_but_not_write(admin_ops):
    ro = Ops(admin_ops.app, admin_ops.readonly)
    assert ro.get("/admin/user", uid="alice").status_code == 200
    assert ro.get("/admin/bucket", stats="true").status_code == 200
    assert ro.get("/admin/usage").status_code == 200
    writes = [
        ro.put("/admin/user", uid="x", **{"display-name": "X"}),
        ro.post("/admin/user", uid="alice", suspended="true"),
        ro.delete("/admin/user", uid="alice"),
        ro.put("/admin/user", key=None, uid="alice", **{"access-key": "A" * 20}),
        ro.delete("/admin/bucket", bucket="alice-empty"),
        ro.put("/admin/bucket", '{"enabled": true}', quota=None, bucket="alice-data"),
    ]
    assert all(code(r, 403) == "AccessDenied" for r in writes)
    assert "x" not in admin_ops.state.users and "alice-empty" in admin_ops.state.buckets


def test_each_endpoint_group_needs_its_own_cap(admin_ops):
    only_users = AdminPrincipal.make("ONLYUSERS" + "0" * 11, "s" * 40, "users=read,write")
    app = create_ops(admin_ops.state, admins=[only_users])
    o = Ops(app, only_users)
    assert o.get("/admin/user", uid="alice").status_code == 200
    assert code(o.get("/admin/bucket"), 403) == "AccessDenied"
    assert code(o.get("/admin/usage"), 403) == "AccessDenied"
    assert code(o.get("/admin/metadata/user"), 403) == "AccessDenied"
    assert o.get("/admin/user", list=None).status_code == 200  # only the users cap


# -- users ----------------------------------------------------------------------------


def test_user_info_shape(ops):
    u = ops.get("/admin/user", uid="alice").json()
    assert u["user_id"] == "alice" and u["display_name"] == "Alice Anderson"
    assert u["suspended"] == 0 and u["tenant"] == "" and u["caps"] == []
    assert u["keys"] == [{"user": "alice", "access_key": "ALICEACCESSKEY00001",
                          "secret_key": "alice-secret-0001"}]
    assert u["user_quota"] == {"enabled": False, "check_on_raw": False, "max_size": -1,
                               "max_size_kb": 0, "max_objects": -1}
    assert u["bucket_quota"]["enabled"] is False


def test_user_info_errors_and_lookup_by_access_key(ops):
    assert code(ops.get("/admin/user", uid="nobody"), 404) == "NoSuchUser"
    assert code(ops.get("/admin/user"), 400) == "InvalidArgument"
    r = ops.get("/admin/user", **{"access-key": "BOBACCESSKEY0000002"})
    assert r.json()["user_id"] == "bob"
    assert code(ops.get("/admin/user", **{"access-key": "NOPE"}), 404) == "NoSuchUser"


def test_create_user_without_email_generates_a_key(admin_ops, ops):
    r = ops.put("/admin/user", uid="dave", **{"display-name": "Dave D"})
    assert r.status_code == 200
    u = r.json()
    assert u["email"] == "" and len(u["keys"]) == 1 and u["keys"][0]["user"] == "dave"
    assert len(u["keys"][0]["access_key"]) == 20
    assert admin_ops.state.users["dave"].email == ""


def test_create_user_options_and_errors(admin_ops, ops):
    r = ops.put("/admin/user", uid="erin", **{"display-name": "Erin", "generate-key": "false"})
    assert r.json()["keys"] == [] and admin_ops.state.users["erin"].keys == []
    r = ops.put("/admin/user", uid="fay", **{"display-name": "F", "access-key": "FAYKEY0001",
                                              "secret-key": "fay-secret", "suspended": "true",
                                              "email": "fay@example.org"})
    u = r.json()
    assert u["suspended"] == 1 and u["email"] == "fay@example.org"
    assert u["keys"][0]["secret_key"] == "fay-secret"
    r = ops.put("/admin/user", uid="erin", **{"display-name": "E"})
    assert code(r, 409) == "UserAlreadyExists"
    r = ops.put("/admin/user", uid="g", **{"display-name": "G", "email": "FAY@example.org"})
    assert code(r, 409) == "EmailExists"
    assert code(ops.put("/admin/user", uid="h", **{"display-name": "H",
                                                   "access-key": "FAYKEY0001"}), 409) == "KeyExists"
    assert "h" not in admin_ops.state.users
    assert code(ops.put("/admin/user", uid="i"), 400) == "InvalidArgument"
    assert code(ops.put("/admin/user", **{"display-name": "x"}), 400) == "InvalidArgument"
    # an unparsable boolean falls back to the default (generate-key=true), never a 400
    r = ops.put("/admin/user", uid="j", **{"display-name": "x", "generate-key": "maybe"})
    assert r.status_code == 200 and len(r.json()["keys"]) == 1


def test_modify_user(admin_ops, ops):
    r = ops.post("/admin/user", uid="alice", suspended="true", **{"display-name": "Alice A"})
    u = r.json()
    assert u["suspended"] == 1 and u["display_name"] == "Alice A"
    assert admin_ops.state.users["alice"].suspended is True
    assert ops.post("/admin/user", uid="alice", suspended="false").json()["suspended"] == 0
    assert code(ops.post("/admin/user", uid="nobody", suspended="true"), 404) == "NoSuchUser"
    r = ops.post("/admin/user", uid="alice", **{"generate-key": "true"})
    assert len(r.json()["keys"]) == 2


def test_delete_user_rules(admin_ops, ops):
    assert code(ops.delete("/admin/user", uid="alice"), 409) == "BucketAlreadyExists"
    assert "alice" in admin_ops.state.users
    r = ops.delete("/admin/user", uid="alice", **{"purge-data": "true"})
    assert r.status_code == 200 and r.content == b""
    assert "alice" not in admin_ops.state.users
    assert not [b for b in admin_ops.state.buckets.values() if b.owner == "alice"]
    assert admin_ops.state.access_keys.get("ALICEACCESSKEY00001") is None
    assert code(ops.delete("/admin/user", uid="alice"), 404) == "NoSuchUser"
    admin_ops.state.create_user("empty", "Empty")
    assert ops.delete("/admin/user", uid="empty").status_code == 200


# -- keys -----------------------------------------------------------------------------


def test_key_put_and_delete(admin_ops, ops):
    r = ops.put("/admin/user", key=None, uid="alice", **{"key-type": "s3",
                "access-key": "ALICESECONDKEY00002", "secret-key": "second-secret"})
    keys = r.json()
    assert [k["access_key"] for k in keys] == ["ALICEACCESSKEY00001", "ALICESECONDKEY00002"]
    assert all(k["user"] == "alice" for k in keys)
    gen = ops.put("/admin/user", key=None, uid="alice").json()
    new = [k for k in gen if k["access_key"] not in {"ALICEACCESSKEY00001", "ALICESECONDKEY00002"}]
    assert len(gen) == 3 and len(new) == 1 and len(new[0]["secret_key"]) == 40
    assert [k["access_key"] for k in gen] == sorted(k["access_key"] for k in gen)
    # same user, same access key: the secret is replaced
    ops.put("/admin/user", key=None, uid="alice", **{"access-key": "ALICESECONDKEY00002",
                                                       "secret-key": "newer-secret"})
    mine = {k["access_key"]: k["secret_key"] for k in admin_ops.state.users["alice"].keys}
    assert mine["ALICESECONDKEY00002"] == "newer-secret" and len(mine) == 3
    r = ops.delete("/admin/user", key=None, uid="alice", **{"access-key": "ALICESECONDKEY00002"})
    assert r.status_code == 200 and r.content == b""
    assert len(admin_ops.state.users["alice"].keys) == 2


def test_key_of_another_user(admin_ops, ops):
    bob_key = "BOBACCESSKEY0000002"
    r = ops.put("/admin/user", key=None, uid="alice", **{"access-key": bob_key,
                                                          "secret-key": "stolen"})
    assert code(r, 409) == "KeyExists"
    assert admin_ops.state.access_keys[bob_key] == "bob"
    assert [k["secret_key"] for k in admin_ops.state.users["bob"].keys] == ["bob-secret-0002"]
    r = ops.delete("/admin/user", key=None, uid="alice", **{"access-key": bob_key})
    assert code(r, 403) == "InvalidAccessKeyId"
    assert admin_ops.state.access_keys[bob_key] == "bob"


def test_key_errors(ops):
    assert code(ops.put("/admin/user", key=None, uid="nobody"), 404) == "NoSuchUser"
    assert code(ops.put("/admin/user", key=None, uid="alice", **{"generate-key": "false"}),
                400) == "InvalidArgument"
    assert code(ops.put("/admin/user", key=None, uid="alice", **{"key-type": "swift"}),
                501) == "NotImplemented"
    assert code(ops.put("/admin/user", key=None, uid="alice", **{"key-type": "foo"}),
                400) == "InvalidKeyType"
    assert code(ops.delete("/admin/user", key=None, uid="alice"), 400) == "InvalidArgument"
    assert code(ops.delete("/admin/user", key=None, uid="alice",
                           **{"access-key": "MISSING"}), 403) == "InvalidAccessKeyId"


# -- quotas ---------------------------------------------------------------------------


def test_user_quota_json_body_and_query_forms(admin_ops, ops):
    body = '{"enabled": true, "max_size": 1073741824, "max_objects": 1000}'
    r = ops.put("/admin/user", body, quota=None, uid="alice", **{"quota-type": "user"})
    assert r.status_code == 200 and r.content == b""
    q = ops.get("/admin/user", quota=None, uid="alice", **{"quota-type": "user"}).json()
    assert q == {"enabled": True, "check_on_raw": False, "max_size": 1073741824,
                 "max_size_kb": 1048576, "max_objects": 1000}
    assert admin_ops.state.users["alice"].user_quota == {
        "enabled": True, "maxSize": 1073741824, "maxObjects": 1000}
    # a JSON body decodes into a FRESH quota: omitted fields reset to the defaults
    ops.put("/admin/user", '{"max_size_kb": 2}', quota=None, uid="alice",
            **{"quota-type": "user"})
    assert admin_ops.state.users["alice"].user_quota == {
        "enabled": False, "maxSize": 2048, "maxObjects": -1}
    # max_size wins over max_size_kb
    ops.put("/admin/user", '{"enabled": true, "max_size": 4096, "max_size_kb": 1}', quota=None,
            uid="alice", **{"quota-type": "user"})
    assert admin_ops.state.users["alice"].user_quota["maxSize"] == 4096
    # zero limits with no "enabled" turn the quota OFF
    ops.put("/admin/user", '{"max_size": 0, "max_objects": 0}', quota=None, uid="alice",
            **{"quota-type": "user"})
    assert admin_ops.state.users["alice"].user_quota == {
        "enabled": False, "maxSize": 0, "maxObjects": 0}
    # the query form keeps old values
    ops.put("/admin/user", quota=None, uid="alice", **{"quota-type": "user", "enabled": "true"})
    assert admin_ops.state.users["alice"].user_quota == {
        "enabled": True, "maxSize": 0, "maxObjects": 0}
    ops.put("/admin/user", quota=None, uid="alice", **{"quota-type": "user", "enabled": "false",
                                                        "max-size": "-1", "max-objects": "-1"})
    q = ops.get("/admin/user", quota=None, uid="alice", **{"quota-type": "user"}).json()
    assert q["enabled"] is False and q["max_size"] == -1 and q["max_objects"] == -1
    # in the query form max-size-kb wins over max-size
    ops.put("/admin/user", quota=None, uid="alice", **{"quota-type": "user",
                                                        "max-size": "4096", "max-size-kb": "2"})
    assert admin_ops.state.users["alice"].user_quota["maxSize"] == 2048


def test_quota_put_without_type_sets_both(admin_ops, ops):
    body = ('{"user_quota": {"enabled": true, "max_objects": 5},'
            ' "bucket_quota": {"enabled": true, "max_size": 99}}')
    assert ops.put("/admin/user", body, quota=None, uid="bob").status_code == 200
    bob = admin_ops.state.users["bob"]
    assert bob.user_quota == {"enabled": True, "maxSize": -1, "maxObjects": 5}
    assert bob.bucket_quota == {"enabled": True, "maxSize": 99, "maxObjects": -1}


def test_user_bucket_quota_and_both(admin_ops, ops):
    ops.put("/admin/user", '{"enabled": true, "max_objects": 7}', quota=None, uid="bob",
            **{"quota-type": "bucket"})
    both = ops.get("/admin/user", quota=None, uid="bob").json()
    assert both["bucket_quota"]["max_objects"] == 7 and both["user_quota"]["enabled"] is False
    assert admin_ops.state.users["bob"].bucket_quota["maxObjects"] == 7


def test_quota_errors(ops):
    p = {"quota": None, "uid": "alice"}
    assert code(ops.request("PUT", "/admin/user", {**p, "quota-type": "x"}, "{}"), 400) == \
        "InvalidArgument"
    assert code(ops.request("PUT", "/admin/user", p, "{}"), 400) == "InvalidArgument"
    assert code(ops.request("PUT", "/admin/user", {**p, "quota-type": "user"}, "nope"), 400) == \
        "InvalidArgument"
    assert code(ops.request("PUT", "/admin/user", {**p, "quota-type": "user"},
                            '{"max_size": "big"}'), 400) == "InvalidArgument"
    assert code(ops.request("PUT", "/admin/user", {"quota": None, "uid": "nobody",
                                                   "quota-type": "user"}, "{}"), 404) == \
        "NoSuchUser"


def test_bucket_quota(admin_ops, ops):
    r = ops.put("/admin/bucket", '{"enabled": true, "max_size": 4096, "max_objects": 3}',
                quota=None, uid="alice", bucket="alice-data")
    assert r.status_code == 200
    assert admin_ops.state.buckets["alice-data"].quota == {
        "enabled": True, "maxSize": 4096, "maxObjects": 3}
    stats = ops.get("/admin/bucket", bucket="alice-data").json()
    assert stats["bucket_quota"]["max_size"] == 4096 and stats["bucket_quota"]["enabled"]
    # no ownership check: uid only supplies the tenant
    assert ops.put("/admin/bucket", '{"enabled": true, "max_objects": 9}', quota=None,
                   uid="bob", bucket="alice-data").status_code == 200
    assert admin_ops.state.buckets["alice-data"].quota == {
        "enabled": True, "maxSize": -1, "maxObjects": 9}
    assert ops.put("/admin/bucket", '{"max_objects": 4}', quota=None,
                   uid="acme$carol", bucket="reports").status_code == 200
    assert admin_ops.state.buckets["acme:reports"].quota["maxObjects"] == 4
    assert code(ops.put("/admin/bucket", "{}", quota=None, bucket="nope"), 404) == "NoSuchBucket"
    assert code(ops.put("/admin/bucket", "{}", quota=None), 400) == "InvalidArgument"


# -- lists ----------------------------------------------------------------------------


def test_metadata_user_list(admin_ops, ops):
    every = ["acme$carol", "alice", "bob", "bucketeer-admin", "bucketeer-readonly"]
    assert ops.get("/admin/metadata/user").json() == every
    page = ops.get("/admin/metadata/user", **{"max-entries": "2"}).json()
    assert page["keys"] == every[:2] and page["truncated"] is True
    nxt = ops.get("/admin/metadata/user", **{"max-entries": "9", "marker": page["marker"]}).json()
    assert nxt["keys"] == every[2:] and nxt["truncated"] is False


def test_user_list_flag(ops):
    r = ops.get("/admin/user", list=None).json()
    assert r["keys"] == ["acme$carol", "alice", "bob", "bucketeer-admin",
                         "bucketeer-readonly"] and r["truncated"] is False


# -- buckets --------------------------------------------------------------------------


def test_bucket_names_and_stats(admin_ops, ops):
    assert sorted(ops.get("/admin/bucket").json()) == [
        "acme/reports", "alice-data", "alice-empty", "bob-shared"]
    assert ops.get("/admin/bucket", uid="bob").json() == ["bob-shared"]
    allstats = ops.get("/admin/bucket", stats="true").json()
    assert len(allstats) == 4
    mine = ops.get("/admin/bucket", stats="true", uid="alice").json()
    assert {b["bucket"] for b in mine} == {"alice-data", "alice-empty"}
    # uid + bucket: ONE stats object, whoever owns it (`uid` only gives the tenant), as on Reef
    one = ops.get("/admin/bucket", stats="true", uid="alice", bucket="bob-shared").json()
    assert isinstance(one, dict) and one["owner"] == "bob"
    data = ops.get("/admin/bucket", bucket="alice-data").json()
    assert data["bucket"] == "alice-data" and data["owner"] == "alice" and data["tenant"] == ""
    assert data["usage"]["rgw.main"]["num_objects"] == 42
    assert data["usage"]["rgw.main"]["size_actual"] >= data["usage"]["rgw.main"]["size"] == 1048576
    assert data["placement_rule"] == "default-placement"
    assert data["bucket_quota"]["max_size"] == -1 and data["versioning"] == "off"
    assert data["object_lock_enabled"] is False
    assert ops.get("/admin/bucket", bucket="alice-empty").json()["usage"] == {}
    assert code(ops.get("/admin/bucket", bucket="nope"), 404) == "NoSuchBucket"
    assert code(ops.get("/admin/bucket", uid="nobody"), 404) == "NoSuchUser"


def test_versioning_and_lock_flags(admin_ops, ops):
    admin_ops.state.create_bucket("locked", "bob", object_lock=True)
    s = ops.get("/admin/bucket", bucket="locked").json()
    assert s["object_lock_enabled"] is True and s["versioning"] == "enabled"
    admin_ops.state.update_bucket("bob-shared", {"versioning": "ENABLED"})
    admin_ops.state.update_bucket("bob-shared", {"versioning": "SUSPENDED"})
    s = ops.get("/admin/bucket", bucket="bob-shared").json()
    assert s["versioning"] == "suspended"


def test_tenant_uids_and_buckets(admin_ops, ops):
    u = ops.get("/admin/user", uid="acme$carol").json()
    assert u["user_id"] == "carol" and u["tenant"] == "acme"
    assert u["keys"][0]["user"] == "acme$carol"
    # `tenant=` is read only by create; elsewhere it is ignored
    assert code(ops.get("/admin/user", uid="carol", tenant="acme"), 404) == "NoSuchUser"
    for name in ("acme/reports", "acme:reports"):
        s = ops.get("/admin/bucket", bucket=name).json()
        assert s["bucket"] == "reports" and s["tenant"] == "acme" and s["owner"] == "acme$carol"
    assert code(ops.get("/admin/bucket", bucket="reports"), 404) == "NoSuchBucket"
    r = ops.put("/admin/user", uid="dan", tenant="acme", **{"display-name": "Dan"})
    assert r.json()["user_id"] == "dan" and r.json()["tenant"] == "acme"
    assert "acme$dan" in admin_ops.state.users
    r = ops.put("/admin/user", uid="other$eve", tenant="acme", **{"display-name": "Eve"})
    assert "acme$eve" in admin_ops.state.users  # tenant= overrides
    assert ops.delete("/admin/bucket", bucket="acme/reports").status_code == 200
    assert "acme:reports" not in admin_ops.state.buckets


def test_bucket_ops_with_uid(admin_ops, ops):
    assert ops.get("/admin/bucket", uid="acme$carol").json() == ["reports"]
    assert sorted(ops.get("/admin/bucket", uid="alice").json()) == ["alice-data", "alice-empty"]
    # bucket= with uid answers that bucket's ONE stats object, whoever owns it (Reef)
    stats = ops.get("/admin/bucket", uid="acme$carol", bucket="reports", stats="true").json()
    assert stats["bucket"] == "reports" and stats["tenant"] == "acme"
    assert ops.get("/admin/bucket", uid="bob", bucket="alice-data").json()["owner"] == "alice"
    assert code(ops.get("/admin/bucket", uid="alice", bucket="reports"), 404) == "NoSuchBucket"
    # without uid a single bucket answers one stats object
    assert isinstance(ops.get("/admin/bucket", bucket="acme/reports").json(), dict)
    r = ops.delete("/admin/bucket", uid="acme$carol", bucket="reports")
    assert r.status_code == 200 and "acme:reports" not in admin_ops.state.buckets


def test_plus_in_uid(admin_ops, ops):
    # the helper encodes once and the mock decodes twice (Tentacle): "%2B" here is a `+`
    uid, sent = "alice+backup-job", "alice%2Bbackup-job"
    r = ops.put("/admin/user", uid=sent, **{"display-name": "alice backup"})
    assert r.json()["user_id"] == uid and uid in admin_ops.state.users
    k = ops.put("/admin/user", key=None, uid=sent, **{"access-key": "PLUSKEY0000000000001"}).json()
    assert k[-1]["user"] == uid
    ops.put("/admin/user", '{"enabled": true, "max_size": 0, "max_objects": 0}', quota=None,
            uid=sent, **{"quota-type": "user"})
    q = ops.get("/admin/user", quota=None, uid=sent, **{"quota-type": "user"}).json()
    assert q["enabled"] is True and q["max_size"] == 0 and q["max_objects"] == 0
    ops.put("/admin/user", uid="acme$carol%2Bjob", **{"display-name": "x"})
    assert ops.get("/admin/user", uid="acme$carol%2Bjob").json()["tenant"] == "acme"
    assert ops.delete("/admin/user", uid=sent).status_code == 200
    # a `+` encoded only once arrives as a space (the mock then refuses the uid)
    ops.put("/admin/user", uid="bob+once", **{"display-name": "x"})
    assert "bob+once" not in admin_ops.state.users


def test_bucket_delete_without_purge_when_empty(admin_ops, ops):
    assert ops.delete("/admin/bucket", bucket="alice-empty").status_code == 200
    assert "alice-empty" not in admin_ops.state.buckets
    assert code(ops.delete("/admin/bucket", bucket="alice-empty"), 404) == "NoSuchBucket"
    assert code(ops.delete("/admin/bucket"), 400) == "InvalidArgument"


def test_bucket_delete_not_empty_needs_purge(admin_ops, ops):
    r = ops.delete("/admin/bucket", bucket="alice-data")
    assert code(r, 409) == "BucketNotEmpty" and "alice-data" in admin_ops.state.buckets
    assert ops.delete("/admin/bucket", bucket="alice-data",
                      **{"purge-objects": "true"}).status_code == 200
    assert "alice-data" not in admin_ops.state.buckets


# -- usage and routing ----------------------------------------------------------------


def test_usage_minimal(ops):
    r = ops.get("/admin/usage").json()
    assert r["entries"] == [] and {s["user"] for s in r["summary"]} == {
        "alice", "bob", "acme$carol", "bucketeer-admin", "bucketeer-readonly"}
    assert [s["user"] for s in ops.get("/admin/usage", uid="bob").json()["summary"]] == ["bob"]


def test_unsupported_routes(ops):
    assert code(ops.put("/admin/user", caps="users=read", uid="alice"), 501) == "NotImplemented"
    assert code(ops.get("/admin/user", stats="true", uid="alice"), 501) == "NotImplemented"
    assert code(ops.post("/admin/bucket", bucket="alice-data"), 501) == "NotImplemented"
    assert code(ops.request("POST", "/admin/usage"), 405) == "MethodNotAllowed"
    assert code(ops.get("/admin/nothing"), 404) == "NoSuchKey"


# -- shared state ---------------------------------------------------------------------


def croit_client(admin_ops):
    return TestClient(admin_ops.croit_app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})


def test_created_through_admin_ops_is_visible_in_croit(admin_ops, ops):
    c = croit_client(admin_ops)
    ops.put("/admin/user", uid="zoe", **{"display-name": "Zoe", "access-key": "ZOEKEY",
                                          "secret-key": "zoe-secret"})
    users = {u["uid"]: u for u in c.get("/s3/users").json()}
    assert users["zoe"]["name"] == "Zoe"
    assert c.get("/s3/users/zoe/keys").json() == [
        {"user": "zoe", "access_key": "ZOEKEY", "secret_key": "zoe-secret"}]
    ops.post("/admin/user", uid="zoe", suspended="true")
    assert {u["uid"]: u for u in c.get("/s3/users").json()}["zoe"]["suspended"] is True
    ops.put("/admin/user", '{"enabled": true, "max_size": 1024}', quota=None, uid="zoe",
            **{"quota-type": "user"})
    assert {u["uid"]: u for u in c.get("/s3/users").json()}["zoe"]["userQuota"]["maxSize"] == 1024


def test_created_through_croit_is_visible_in_admin_ops(admin_ops, ops):
    c = croit_client(admin_ops)
    assert c.post("/s3/users", json={"uid": "yan", "name": "Yan"}).status_code in (200, 204)
    c.put("/s3/users/yan/keys/YANKEY", params={"secretKey": "yan-secret"})
    u = ops.get("/admin/user", uid="yan").json()
    assert u["display_name"] == "Yan"
    assert "YANKEY" in {k["access_key"] for k in u["keys"]}
    c.patch("/s3/users/yan", json={"suspended": True})
    assert ops.get("/admin/user", uid="yan").json()["suspended"] == 1
    c.put("/s3/users/yan/quota", json={"enabled": True, "maxSize": 2048, "maxObjects": 9})
    q = ops.get("/admin/user", quota=None, uid="yan", **{"quota-type": "user"}).json()
    assert q["max_size"] == 2048 and q["max_objects"] == 9
    c.put("/s3/buckets/yan-box", json={"owner": "yan"})
    assert ops.get("/admin/bucket", bucket="yan-box").json()["owner"] == "yan"
    assert code(ops.put("/admin/user", key=None, uid="alice",
                        **{"access-key": "YANKEY"}), 409) == "KeyExists"


# -- moto propagation -------------------------------------------------------------------


@pytest.fixture
def wired(s3_endpoint):
    bridge = S3Bridge(s3_endpoint)
    croit_app = create_croit(seed=True, task_seconds=0, hooks=bridge.make_hooks(MockHooks),
                             rgw_endpoint=bridge.host_port)
    bridge.seed_objects(croit_app.state.croit)
    app = create_ops(croit_app.state.croit)
    return bridge, croit_app, Ops(app, app.state.admin_ops.admins[0])


def moto_buckets(bridge):
    return {b["Name"] for b in bridge.client("x", "y").list_buckets()["Buckets"]}


def test_purge_delete_removes_bucket_and_objects_from_moto(wired):
    bridge, croit_app, ops = wired
    assert "alice-data" in moto_buckets(bridge)
    assert ops.get("/admin/bucket", bucket="alice-data").json()["usage"][
        "rgw.main"]["num_objects"] == 42
    assert code(ops.delete("/admin/bucket", bucket="alice-data"), 409) == "BucketNotEmpty"
    assert "alice-data" in moto_buckets(bridge)
    r = ops.delete("/admin/bucket", bucket="alice-data", **{"purge-objects": "true"})
    assert r.status_code == 200
    assert "alice-data" not in moto_buckets(bridge)
    assert "alice-data" not in croit_app.state.croit.buckets


def test_purge_user_removes_owned_buckets_from_moto(wired):
    bridge, croit_app, ops = wired
    ops.delete("/admin/user", uid="alice", **{"purge-data": "true"})
    assert moto_buckets(bridge) == {"bob-shared", "reports"}


def test_new_keys_reach_the_bridge_and_work_for_buckets(wired):
    bridge, croit_app, ops = wired
    ops.put("/admin/user", uid="kim", **{"display-name": "Kim", "secret-key": "kim-secret",
                                          "access-key": "KIMKEY00000000000001"})
    assert bridge._keys["kim"] == {"KIMKEY00000000000001": "kim-secret"}
    croit_app.state.croit.create_bucket("kim-box", "kim")
    assert "kim-box" in moto_buckets(bridge)
    bridge.client("KIMKEY00000000000001", "kim-secret").put_object(
        Bucket="kim-box", Key="a", Body=b"1234")
    assert ops.get("/admin/bucket", bucket="kim-box").json()["usage"]["rgw.main"][
        "size"] == 4
    ops.delete("/admin/user", key=None, uid="kim", **{"access-key": "KIMKEY00000000000001"})
    assert bridge._keys["kim"] == {}


def test_httpx_asgi_transport_works_for_a_client(admin_ops):
    """The in-process wiring a client test (A2) would use."""
    req = AWSRequest("GET", f"{HOST}/admin/user?uid=bob")
    S3SigV4Auth(Credentials(admin_ops.admin.access_key, admin_ops.admin.secret_key), "s3",
                "us-east-1").add_auth(req)

    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_ops.app)) as h:
            return await h.get(f"{HOST}/admin/user?uid=bob", headers=dict(req.headers.items()))

    r = asyncio.run(call())
    assert r.status_code == 200 and r.json()["user_id"] == "bob"


# -- review fixes ---------------------------------------------------------------------


def test_repeat_create_is_a_modify_unless_exclusive(admin_ops, ops):
    state = admin_ops.state
    keys_before = list(state.users["alice"].keys)
    r = ops.put("/admin/user", uid="alice", **{"display-name": "Alice Anderson",
                                                "suspended": "true"})
    assert r.status_code == 200 and r.json()["suspended"] == 1
    assert state.users["alice"].suspended is True and state.users["alice"].keys == keys_before
    r = ops.put("/admin/user", uid="alice", **{"display-name": "Alice Anderson",
                                                "exclusive": "true"})
    assert code(r, 409) == "UserAlreadyExists"
    r = ops.put("/admin/user", uid="alice", **{"display-name": "Somebody Else"})
    assert code(r, 409) == "UserAlreadyExists"
    assert state.users["alice"].name == "Alice Anderson"


def test_query_parameter_names_are_exact_and_booleans_lenient(admin_ops, ops):
    r = ops.put("/admin/user", uid="u1", display_name="X")  # underscore is not a name here
    assert code(r, 400) == "InvalidArgument"
    state = admin_ops.state
    # unparsable boolean -> default (false): nothing is purged
    r = ops.delete("/admin/bucket", bucket="alice-data", **{"purge-objects": "yes"})
    assert code(r, 409) == "BucketNotEmpty" and "alice-data" in state.buckets
    r = ops.delete("/admin/bucket", bucket="alice-data", **{"purge-objects": "FALSE"})
    assert code(r, 409) == "BucketNotEmpty"
    r = ops.request("DELETE", "/admin/bucket", [("bucket", "alice-data"), ("purge-objects", None)])
    assert r.status_code == 200 and "alice-data" not in state.buckets
    r = ops.delete("/admin/user", uid="bob", **{"purge-data": "TRUE"})
    assert r.status_code == 200 and "bob" not in state.users


def test_plain_sigv4_without_payload_header_is_refused(admin_ops):
    from botocore.auth import SigV4Auth

    def call(signer):
        req = AWSRequest("GET", f"{HOST}/admin/user?uid=alice")
        signer(Credentials(admin_ops.admin.access_key, admin_ops.admin.secret_key), "s3",
               "us-east-1").add_auth(req)
        assert ("X-Amz-Content-SHA256" in req.headers) == (signer is S3SigV4Auth)
        return TestClient(admin_ops.app, base_url=HOST).get(
            req.url, headers=dict(req.headers.items()))

    assert code(call(SigV4Auth), 403) == "SignatureDoesNotMatch"
    assert call(S3SigV4Auth).status_code == 200


def test_admin_principals_are_real_users(admin_ops, ops):
    state = admin_ops.state
    assert state.users["bucketeer-admin"].keys[0]["access_key"] == admin_ops.admin.access_key
    assert "bucketeer-admin" not in create_croit(seed=True).state.croit.users  # not the seed
    info = ops.get("/admin/user", uid="bucketeer-admin").json()
    assert info["user_id"] == "bucketeer-admin" and info["keys"][0]["user"] == "bucketeer-admin"
    assert {"type": "users", "perm": "*"} in info["caps"]
    assert {"type": "usage", "perm": "read"} in info["caps"]
    found = ops.get("/admin/user", **{"access-key": admin_ops.admin.access_key}).json()
    assert found["user_id"] == "bucketeer-admin"
    assert "bucketeer-admin" in ops.get("/admin/metadata/user").json()
    assert "bucketeer-admin" in ops.get("/admin/user", list=None).json()["keys"]


def test_suspended_admin_is_refused(admin_ops):
    state = admin_ops.state
    state.update_user("bucketeer-readonly", {"suspended": True})
    ro = Ops(admin_ops.app, admin_ops.readonly)
    assert code(ro.get("/admin/user", uid="alice"), 403) == "UserSuspended"
    state.update_user("bucketeer-readonly", {"suspended": False})
    assert ro.get("/admin/user", uid="alice").status_code == 200


def test_admin_access_key_cannot_be_given_to_another_user(admin_ops, ops):
    key = admin_ops.admin.access_key
    r = ops.put("/admin/user", key=None, uid="bob", **{"access-key": key, "secret-key": "zzz"})
    assert code(r, 409) == "KeyExists"
    r = ops.put("/admin/user", uid="newbie", **{"display-name": "N", "access-key": key})
    assert code(r, 409) == "KeyExists" and "newbie" not in admin_ops.state.users
    assert ops.get("/admin/user", uid="bob").status_code == 200  # the admin still works
    # a second app over the same state keeps the same admin user and key
    app2 = create_ops(admin_ops.state)
    assert Ops(app2, admin_ops.admin).get("/admin/user", uid="bob").status_code == 200


def test_keys_are_sorted_by_access_key(admin_ops, ops):
    admin_ops.state.put_key("alice", "AAAAFIRST000000000", "s")
    keys = ops.get("/admin/user", uid="alice").json()["keys"]
    assert [k["access_key"] for k in keys] == sorted(k["access_key"] for k in keys)
    assert keys[0]["access_key"] == "AAAAFIRST000000000"
    put = ops.put("/admin/user", key=None, uid="alice", **{"access-key": "ZZZZLAST0000000000"})
    assert [k["access_key"] for k in put.json()] == sorted(k["access_key"] for k in put.json())


def test_server_helper_serves_signed_requests(admin_ops):
    """The start-up path shared by `CROIT_MOCK_ADMIN_OPS=1` and `demo.py --admin-ops`."""
    import socket

    import mock.demo
    from mock.rgw_admin_mock import describe_admins, start_server

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = start_server(admin_ops.app, "127.0.0.1", port)
    try:
        url = f"http://127.0.0.1:{port}/admin/user?uid=alice"
        req = AWSRequest("GET", url)
        S3SigV4Auth(Credentials(admin_ops.admin.access_key, admin_ops.admin.secret_key), "s3",
                    "us-east-1").add_auth(req)
        r = httpx.get(url, headers=dict(req.headers.items()), trust_env=False)
        assert r.status_code == 200 and r.json()["user_id"] == "alice"
        assert admin_ops.admin.access_key in describe_admins(admin_ops.app)
    finally:
        server.should_exit = True
    assert mock.demo.parse_args(["--admin-ops"]).admin_ops is True
    assert mock.demo.parse_args([]).admin_ops is False
