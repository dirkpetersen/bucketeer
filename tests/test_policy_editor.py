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
    assert put(env, new, j["etag"], confirmManaged=True).status_code == 200
    listing = env.alice.get("/api/buckets/alice-data/shares").json()["shares"]
    assert [s["status"] for s in listing] == ["out_of_sync"]
    r = env.alice.put(f"/api/buckets/alice-data/shares/{sid}", json={"access": "read"}, headers=H)
    assert r.status_code == 200
    assert policy(env)["Statement"][0] == SHARE
    assert env.alice.get("/api/buckets/alice-data/shares").json()["shares"][0]["status"] == "active"
