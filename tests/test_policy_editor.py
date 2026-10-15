"""GET/PUT/DELETE /api/buckets/{name}/policy (Advanced raw editor) against mock croit and moto.

moto enforces no policies, so the stored DOCUMENT is asserted.
"""

import copy
import json
import threading

import pytest

from bucketeer import s3docs

from .test_bucket_keys import FOREIGN, policy, put_foreign
from .test_buckets import ALICE_KEY, Env, H

SHARE = s3docs.share_statement("alice-data", "bob", None, "read")


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def url(name="alice-data"):
    return f"/api/buckets/{name}/policy"


def doc(*stmts, **top):
    return {"Version": "2012-10-17", "Statement": list(stmts), **top}


def seed_both(env):
    env.moto().put_bucket_policy(Bucket="alice-data", Policy=json.dumps(doc(FOREIGN, SHARE)))


def get(env):
    return env.alice.get(url()).json()


def put(env, policy_, etag, headers=H, **kw):
    return env.alice.put(url(), json={"policy": policy_, "etag": etag, **kw}, headers=headers)


def delete(env, etag, **kw):
    return env.alice.request("DELETE", url(), json={"etag": etag, **kw}, headers=H)


def test_get_none_and_both(env):
    j = get(env)
    assert j["policy"] is None and j["managedSids"] == [] and j["etag"]
    seed_both(env)
    j = get(env)
    assert [s["Sid"] for s in j["policy"]["Statement"]] == ["legacy-allow", SHARE["Sid"]]
    assert j["managedSids"] == [SHARE["Sid"]]
    assert get(env)["etag"] == j["etag"]
    assert env.alice.get(url()).headers["cache-control"] == "no-store"
    assert env.used and set(env.used) == {ALICE_KEY}


def test_put_foreign_change_keeps_managed(env):
    seed_both(env)
    j = get(env)
    new = copy.deepcopy(j["policy"])
    new["Statement"][0]["Action"] = ["s3:GetObject", "s3:ListBucket"]
    new["Statement"].append({**FOREIGN, "Sid": "extra"})
    r = put(env, new, j["etag"])
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True and r.json()["managedChanges"] == []
    stored = policy(env)
    assert [s["Sid"] for s in stored["Statement"]] == ["legacy-allow", SHARE["Sid"], "extra"]
    assert stored["Statement"][1] == SHARE
    assert r.json()["etag"] == get(env)["etag"]
    assert set(env.used) == {ALICE_KEY}


def test_put_string_policy_and_noop(env):
    j = get(env)
    r = put(env, json.dumps(doc(FOREIGN)), j["etag"])
    assert r.status_code == 200 and policy(env)["Statement"] == [FOREIGN]
    r2 = put(env, json.dumps(doc(FOREIGN)), r.json()["etag"])
    assert r2.status_code == 200 and r2.json()["changed"] is False
    assert len(env.audit("bucket.policy")) == 1


def test_version_absent_stays_absent(env):
    j = get(env)
    r = put(env, {"Statement": [FOREIGN]}, j["etag"])
    assert r.status_code == 200
    assert "Version" not in r.json()["policy"]


def test_managed_change_needs_confirmation(env):
    seed_both(env)
    j = get(env)
    changed = copy.deepcopy(j["policy"])
    changed["Statement"][1]["Action"] = ["s3:GetObject"]
    removed = doc(FOREIGN)
    added = doc(FOREIGN, SHARE, {**FOREIGN, "Sid": "bucketeer-share-u-zed"})
    for new, sids in [(changed, [SHARE["Sid"]]), (removed, [SHARE["Sid"]]),
                      (added, ["bucketeer-share-u-zed"])]:
        r = put(env, new, j["etag"])
        assert r.status_code == 409
        assert r.json()["requiresConfirmation"] is True and r.json()["managedChanges"] == sids
        assert policy(env)["Statement"][1] == SHARE
    r = put(env, removed, j["etag"], confirmManaged=True)
    assert r.status_code == 200 and r.json()["managedChanges"] == [SHARE["Sid"]]
    assert policy(env)["Statement"] == [FOREIGN]
    assert r.json()["managedSids"] == []


def test_etag_mismatch_returns_current(env):
    put_foreign(env)
    stale = get(env)["etag"]
    env.moto().put_bucket_policy(Bucket="alice-data", Policy=json.dumps(doc(FOREIGN, SHARE)))
    r = put(env, doc(FOREIGN, {**FOREIGN, "Sid": "mine"}), stale)
    assert r.status_code == 409 and r.json()["etagMismatch"] is True
    cur = r.json()["current"]
    assert cur["managedSids"] == [SHARE["Sid"]] and cur["etag"] != stale
    assert [s["Sid"] for s in policy(env)["Statement"]] == ["legacy-allow", SHARE["Sid"]]
    assert delete(env, stale).status_code == 409
    assert env.audit("bucket.policy") == []


def test_etag_checked_inside_the_lock(env):
    put_foreign(env)
    e0 = get(env)["etag"]
    results = []

    def go(sid):
        results.append(put(env, doc(FOREIGN, {**FOREIGN, "Sid": sid}), e0).status_code)

    ts = [threading.Thread(target=go, args=(f"t{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == [200, 409]


@pytest.mark.parametrize("bad", [
    "{not json", "[1]", "42", {"Version": "2020-01-01", "Statement": []},
    {"Statement": "x"}, {"Statement": [1]}, {"Version": "2012-10-17"},
    {"Hello": 1, "Statement": []},
    {"Statement": [{**FOREIGN, "Effect": "Maybe"}]},
    {"Statement": [{k: v for k, v in FOREIGN.items() if k != "Principal"}]},
    {"Statement": [{k: v for k, v in FOREIGN.items() if k != "Action"}]},
    {"Statement": [{k: v for k, v in FOREIGN.items() if k != "Resource"}]},
    {"Statement": [{**FOREIGN, "NotAction": "s3:*"}]},
    {"Statement": [{**FOREIGN, "Sid": 5}]},
    {"Statement": [FOREIGN, FOREIGN]},
])
def test_invalid_is_422(env, bad):
    r = put(env, bad, get(env)["etag"])
    assert r.status_code == 422, r.text
    assert policy(env) is None


def test_too_large_is_422(env):
    big = doc({**FOREIGN, "Action": ["s3:GetObject"] * 2000})
    assert put(env, big, get(env)["etag"]).status_code == 422
    assert put(env, "x" * 300_000, get(env)["etag"]).status_code == 422
    assert policy(env) is None


def test_malformed_for_s3_is_422(env):
    bad = doc({**FOREIGN, "Principal": "nobody", "Effect": "Allow", "Resource": "nonsense"})
    r = put(env, bad, get(env)["etag"])
    assert r.status_code in (200, 422)  # moto's strictness varies; never a 5xx
    assert r.status_code < 500


def test_empty_statement_deletes_policy(env):
    put_foreign(env)
    j = get(env)
    r = put(env, {"Version": "2012-10-17", "Statement": []}, j["etag"])
    assert r.status_code == 200 and r.json()["policy"] is None
    assert policy(env) is None
    assert r.json()["etag"] == get(env)["etag"]


def test_delete_foreign_only_and_idempotent(env):
    put_foreign(env)
    r = delete(env, get(env)["etag"])
    assert r.status_code == 200 and r.json()["policy"] is None and policy(env) is None
    r = delete(env, get(env)["etag"])
    assert r.status_code == 200 and r.json()["changed"] is False


def test_delete_with_managed_needs_confirm(env):
    seed_both(env)
    et = get(env)["etag"]
    r = delete(env, et)
    assert r.status_code == 409 and r.json()["managedChanges"] == [SHARE["Sid"]]
    assert policy(env) is not None
    r = delete(env, et, confirmManaged=True)
    assert r.status_code == 200 and policy(env) is None


def test_other_users_bucket_404(env):
    seed_both(env)
    for m, kw in [("get", {}), ("put", {"json": {"policy": doc(FOREIGN), "etag": "x"}}),
                  ("request", {"json": {"etag": "x"}})]:
        args = ("DELETE",) if m == "request" else ()
        r = getattr(env.alice, m)(*args, url("bob-shared"), headers=H, **kw)
        assert r.status_code == 404
    assert env.audit("bucket.policy") == []


def test_suspended_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(url()).status_code == 403
    assert put(env, doc(FOREIGN), "x").status_code == 403
    assert policy(env) is None


def test_csrf_and_login(env):
    assert put(env, doc(FOREIGN), get(env)["etag"], headers={}).status_code == 403
    assert env.alice.request("DELETE", url(), json={"etag": "x"}).status_code == 403
    assert env.anon().get(url()).status_code in (401, 403)
    assert policy(env) is None


def test_advanced_mode_is_not_consulted(env):
    # the server never reads the per-user toggle: it is off by default and PUT still works
    assert get(env) and env.alice.get("/api/profile").json()["advancedMode"] is False
    assert put(env, doc(FOREIGN), get(env)["etag"]).status_code == 200


def test_audit_rows(env):
    seed_both(env)
    j = get(env)
    put(env, doc(FOREIGN), j["etag"], confirmManaged=True)
    delete(env, get(env)["etag"])
    rows = list(reversed(env.audit("bucket.policy")))
    assert [r["action"] for r in rows] == ["bucket.policy", "bucket.policy_delete"]
    d = rows[0]["details"]
    assert rows[0]["target"] == "alice-data" and rows[0]["actor"] == "alice"
    assert d["statements_before"] == 2 and d["statements_after"] == 1
    assert d["changed_sids"] == [SHARE["Sid"]] and d["managed_changes"] == [SHARE["Sid"]]
    assert d["before"]["Statement"][1] == SHARE and d["after"]["Statement"] == [FOREIGN]
    assert rows[1]["details"]["statements_after"] == 0 and rows[1]["details"]["after"] is None


def nested(depth):
    v = "x"
    for _ in range(depth):
        v = {"a": v}
    return v


def test_deep_nesting_is_422_object_and_string(env):
    et = get(env)["etag"]
    deep = doc({**FOREIGN, "Condition": nested(550)})
    assert put(env, deep, et).status_code == 422
    assert put(env, doc({**FOREIGN, "Condition": nested(33)}), et).status_code == 422
    assert put(env, "[" * 50_000 + "]" * 50_000, et).status_code == 422
    assert put(env, '{"Statement":' + "[" * 50_000, et).status_code == 422
    ok = put(env, doc({**FOREIGN, "Condition": {"StringEquals": nested(5)}}), et)
    assert ok.status_code in (200, 422) and ok.status_code < 500
    assert policy(env) is None or ok.status_code == 200


def test_widened_share_principal_is_out_of_sync(env):
    r = env.alice.post("/api/buckets/alice-data/shares",
                       json={"login": "bob", "access": "read"}, headers=H)
    assert r.status_code == 201
    sid = r.json()["id"]
    j = get(env)
    new = copy.deepcopy(j["policy"])
    new["Statement"][0]["Principal"] = "*"
    assert put(env, new, j["etag"], confirmManaged=True).status_code == 422  # public (Q12)
    new["Statement"][0]["Principal"] = {"AWS": "arn:aws:iam:::user/zed"}
    assert put(env, new, j["etag"], confirmManaged=True).status_code == 200
    listing = env.alice.get("/api/buckets/alice-data/shares").json()["shares"]
    assert [s["status"] for s in listing] == ["out_of_sync"]
    r = env.alice.put(f"/api/buckets/alice-data/shares/{sid}", json={"access": "read"}, headers=H)
    assert r.status_code == 200
    assert policy(env)["Statement"][0] == SHARE
    assert env.alice.get("/api/buckets/alice-data/shares").json()["shares"][0]["status"] == "active"


# -- public and self-lockout statements (Q12) -----------------------------------------------

from bucketeer import policy_guard as pg  # noqa: E402

BUCKET_ARN = "arn:aws:s3:::alice-data"
ALICE_ARN = "arn:aws:iam:::user/alice"


def allow(principal, **kw):
    return {"Sid": "s", "Effect": "Allow", "Principal": principal, "Action": ["s3:GetObject"],
            "Resource": [BUCKET_ARN + "/*"], **kw}


def deny(principal, action="s3:*", resource=BUCKET_ARN, **kw):
    s = {"Sid": "d", "Effect": "Deny", "Principal": principal, "Action": action,
         "Resource": resource, **kw}
    return s


def kind(stmt, tenant="", uid="alice", bucket="alice-data"):
    return pg.classify(stmt, bucket, tenant, uid)


@pytest.mark.parametrize("principal", [
    "*", {"AWS": "*"}, ["*", "arn:aws:iam:::user/bob"], {"AWS": ["arn:aws:iam:::user/bob", "*"]},
    {"AWS": " * "}, {"CanonicalUser": "*"}, {"AWS": "arn:aws:iam::*:root"},
    {"AWS": "arn:aws:iam::*:user/*"}, {"AWS": "arn:aws:iam:::root"}, {"AWS": ""},
    {"AWS": "acme"}, {"AWS": "arn:aws:iam::acme:root"}, ["", "arn:aws:iam:::user/bob"]])
def test_public_forms(principal):
    assert kind(allow(principal)) == "public"


def test_public_notprincipal_and_condition():
    s = {"Effect": "Allow", "NotPrincipal": {"AWS": "arn:aws:iam:::user/bob"},
         "Action": "s3:GetObject", "Resource": BUCKET_ARN + "/*"}
    assert kind(s) == "public"
    assert kind(allow("*", Condition={"IpAddress": {"aws:SourceIp": "10.0.0.0/8"}})) == "public"


def test_not_public():
    assert kind(allow({"AWS": "arn:aws:iam:::user/bob"})) is None
    assert kind(allow({"AWS": "arn:aws:iam::acme:user/*"})) is None
    assert kind(deny("*", resource=BUCKET_ARN + "/*", action="s3:GetObject")) is None


@pytest.mark.parametrize("principal", [
    "*", {"AWS": "*"}, ["*"], {"AWS": ALICE_ARN}, {"AWS": ["x", ALICE_ARN]},
    {"AWS": "arn:aws:iam:::user/*"}])
def test_lockout_principals(principal):
    assert kind(deny(principal)) == "lockout"


def test_lockout_notprincipal():
    assert kind({"Effect": "Deny", "NotPrincipal": {"AWS": "arn:aws:iam:::user/bob"},
                 "Action": "s3:*", "Resource": BUCKET_ARN}) == "lockout"
    assert kind({"Effect": "Deny", "NotPrincipal": {"AWS": ALICE_ARN},
                 "Action": "s3:*", "Resource": BUCKET_ARN}) is None


@pytest.mark.parametrize("action", [
    "s3:*", "*", "S3:PUTBUCKETPOLICY", ["s3:GetObject", "s3:GetBucketPolicy"],
    "s3:*Policy", "s3:Put*", "s3:DeleteBucketPolic?"])
def test_lockout_actions(action):
    assert kind(deny("*", action=action)) == "lockout"


def test_lockout_notaction():
    base = {"Effect": "Deny", "Principal": "*", "Resource": BUCKET_ARN}
    assert kind({**base, "NotAction": "s3:GetObject"}) == "lockout"
    all3 = ["s3:PutBucketPolicy", "s3:GetBucketPolicy", "s3:DeleteBucketPolicy"]
    assert kind({**base, "NotAction": all3}) is None
    assert kind({**base, "NotAction": "s3:*Policy"}) is None
    assert kind({**base, "NotAction": ["s3:PutBucketPolicy"]}) == "lockout"


def test_lockout_resources_and_unrelated():
    assert kind(deny("*", resource="*")) == "lockout"
    assert kind(deny("*", resource=[BUCKET_ARN, BUCKET_ARN + "/*"])) == "lockout"
    assert kind(deny("*", resource="arn:aws:s3:::*")) == "lockout"
    assert kind(deny("*", resource="arn:aws:s3:::other-bucket")) is None
    assert kind(deny("*", resource=BUCKET_ARN + "/*")) is None  # objects only
    assert kind(deny("*", action="s3:GetObject")) is None
    assert kind(deny("*", action="s3:Put*Object")) is None
    assert kind(deny({"AWS": "arn:aws:iam:::user/bob"})) is None
    assert kind(deny("*", Condition={"Bool": {"aws:SecureTransport": "false"}})) == "lockout"


def test_tenant_owner_arn():
    arn = "arn:aws:iam::acme:user/carol"
    assert kind(deny({"AWS": arn}), tenant="acme", uid="carol") == "lockout"
    assert kind(deny({"AWS": ALICE_ARN}), tenant="acme", uid="carol") is None
    assert kind(deny({"AWS": "arn:aws:iam::acme:root"}), tenant="acme", uid="carol") == "lockout"
    assert kind(deny("*", resource="arn:aws:s3::acme:reports"), "acme", "carol", "reports") \
        == "lockout"
    assert kind(deny("*", resource="arn:aws:s3:::reports"), "acme", "carol", "reports") \
        == "lockout"


def err(r):
    return r.json()["errors"]


def test_put_refuses_public_and_lockout(env):
    put_foreign(env)
    j = get(env)
    before = j["etag"]
    r = put(env, doc(FOREIGN, allow("*", Sid="pub")), before)
    assert r.status_code == 422, r.text
    e = err(r)
    assert e[0]["statement"] == 1 and e[0]["sid"] == "pub" and e[0]["kind"] == "public"
    assert "public" in e[0]["message"] and r.json()["detail"] == e[0]["message"]
    r = put(env, doc(deny({"AWS": ALICE_ARN}, Sid="lock")), before)
    assert r.status_code == 422 and err(r)[0]["kind"] == "lockout"
    r = put(env, doc(deny("*", action=["s3:GetBucketPolicy"])), before)
    assert r.status_code == 422
    assert policy(env)["Statement"] == [FOREIGN]
    assert get(env)["etag"] == before
    assert env.audit("bucket.policy") == []


def test_other_deny_is_allowed(env):
    put_foreign(env)
    new = doc(FOREIGN, deny({"AWS": "arn:aws:iam:::user/bob"}, Sid="d1"),
              deny("*", action="s3:GetObject", resource=BUCKET_ARN + "/*", Sid="d2"),
              deny("*", resource="arn:aws:s3:::other", Sid="d3"))
    r = put(env, new, get(env)["etag"])
    assert r.status_code == 200, r.text


def test_stored_public_kept_edited_and_removed(env):
    pub = allow("*", Sid="pub")
    env.moto().put_bucket_policy(Bucket="alice-data", Policy=json.dumps(doc(FOREIGN, pub)))
    j = get(env)
    assert j["flags"] == [{"index": 1, "sid": "pub", "kind": "public"}]
    assert j["owner"]["arn"] == ALICE_ARN
    # kept unchanged next to a new harmless statement
    r = put(env, doc(FOREIGN, pub, {**FOREIGN, "Sid": "x"}), j["etag"])
    assert r.status_code == 200, r.text
    assert r.json()["flags"] == [{"index": 1, "sid": "pub", "kind": "public"}]
    # reordering is still "kept"
    j = get(env)
    r = put(env, doc(pub, FOREIGN, {**FOREIGN, "Sid": "x"}), j["etag"])
    assert r.status_code == 200
    # edited is refused
    j = get(env)
    edited = {**pub, "Action": ["s3:GetObject", "s3:PutObject"]}
    r = put(env, doc(FOREIGN, edited, {**FOREIGN, "Sid": "x"}), j["etag"])
    assert r.status_code == 422 and err(r)[0]["kind"] == "public"
    # removal passes
    r = put(env, doc(FOREIGN), j["etag"])
    assert r.status_code == 200 and r.json()["flags"] == []
    # and the whole policy can be deleted
    env.moto().put_bucket_policy(Bucket="alice-data", Policy=json.dumps(doc(pub)))
    assert delete(env, get(env)["etag"]).status_code == 200


def test_bucketeer_statements_unaffected(env):
    key = s3docs.bucket_key_statement("alice-data", "alice+job", None, "readwrite", "7")
    assert pg.classify(SHARE, "alice-data", "", "alice") is None
    assert pg.classify(key, "alice-data", "", "alice") is None
    seed_both(env)
    j = get(env)
    assert j["flags"] == []
    new = doc(FOREIGN, SHARE, key)
    r = put(env, new, j["etag"], confirmManaged=True)
    assert r.status_code == 200, r.text


def test_tenant_user_owner_arn_via_api(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    try:
        carol = e.session("carol")
        u = "/api/buckets/reports/policy"
        j = carol.get(u).json()
        assert j["owner"]["arn"] == "arn:aws:iam::acme:user/carol"
        lock = deny({"AWS": "arn:aws:iam::acme:user/carol"}, resource="arn:aws:s3:::reports")
        r = carol.put(u, json={"policy": doc(lock), "etag": j["etag"]}, headers=H)
        assert r.status_code == 422 and r.json()["errors"][0]["kind"] == "lockout"
        ok = deny({"AWS": ALICE_ARN}, resource="arn:aws:s3:::reports")
        r = carol.put(u, json={"policy": doc(ok), "etag": j["etag"]}, headers=H)
        assert r.status_code == 200, r.text
    finally:
        e.db.close()


def test_tenant_principals_lockout():
    assert kind(deny({"AWS": ""})) == "lockout"
    assert kind(deny({"AWS": "arn:aws:iam:::root"})) == "lockout"
    assert kind(deny({"AWS": "acme"}), tenant="acme", uid="carol", bucket="reports",
                ) is None  # resource is alice-data's ARN
    s = deny({"AWS": "acme"}, resource="arn:aws:s3:::reports")
    assert kind(s, tenant="acme", uid="carol", bucket="reports") == "lockout"
    assert kind(s, tenant="", uid="alice", bucket="reports") is None
    assert kind(deny({"AWS": ""}, resource="arn:aws:s3:::reports"), tenant="acme",
                uid="carol", bucket="reports") is None


@pytest.mark.parametrize("principal", [
    {"Service": "logging.s3.amazonaws.com"}, {"Federated": "cognito-identity.amazonaws.com"},
    {"CanonicalUser": "bob"}, {"Service": ["a.amazonaws.com", "b"]}])
def test_non_aws_principal_types_are_not_tenants(env, principal):
    assert kind(allow(principal)) is None
    assert kind(deny({"CanonicalUser": "alice"})) is None
    assert kind(deny({"Service": "acme"}, resource="arn:aws:s3:::reports"), "acme", "carol",
                "reports") is None
    put_foreign(env)
    r = put(env, doc(FOREIGN, allow(principal, Sid="svc")), get(env)["etag"])
    assert r.status_code == 200, r.text


def test_wildcard_under_any_type_stays_public():
    assert kind(allow({"Service": "*"})) == "public"
    assert kind(allow({"Federated": "*"})) == "public"
    assert kind(deny({"CanonicalUser": "*"})) == "lockout"
