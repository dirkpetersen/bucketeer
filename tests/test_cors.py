"""GET/PUT /api/buckets/{name}/cors (Advanced CORS editor) against mock croit and moto."""

import threading

import pytest

from .test_buckets import ALICE_KEY, Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def url(name="alice-data"):
    return f"/api/buckets/{name}/cors"


RULE = {"allowedOrigins": ["https://example.org"], "allowedMethods": ["GET", "HEAD"]}


def get(env):
    return env.alice.get(url()).json()


def put(env, rules, etag, headers=H):
    return env.alice.put(url(), json={"rules": rules, "etag": etag}, headers=headers)


def stored(env):
    from botocore.exceptions import ClientError
    try:
        return env.moto().get_bucket_cors(Bucket="alice-data")["CORSRules"]
    except ClientError:
        return None


def test_get_empty(env):
    j = get(env)
    assert j["rules"] == [] and j["warnings"] == [] and j["etag"]
    assert env.alice.get(url()).headers["cache-control"] == "no-store"
    assert env.used and set(env.used) == {ALICE_KEY}


def test_round_trip_and_delete(env):
    rule = {"id": "web", **RULE, "allowedHeaders": ["Authorization", "x-amz-*"],
            "exposeHeaders": ["ETag"], "maxAgeSeconds": 3000}
    r = put(env, [rule, {"allowedOrigins": ["https://*.example.com"],
                         "allowedMethods": ["PUT", "GET"]}], get(env)["etag"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True and j["rules"][0] == rule
    assert j["rules"][1]["allowedMethods"] == ["GET", "PUT"]
    assert j["rules"][1]["maxAgeSeconds"] is None and j["warnings"] == []
    assert stored(env)[0]["MaxAgeSeconds"] == 3000
    assert get(env) == {k: v for k, v in j.items() if k != "changed"}
    assert set(env.used) == {ALICE_KEY}
    r2 = put(env, j["rules"], j["etag"])
    assert r2.status_code == 200 and r2.json()["changed"] is False
    r3 = put(env, [], j["etag"])
    assert r3.status_code == 200 and r3.json()["rules"] == [] and stored(env) is None
    assert len(env.audit("bucket.cors")) == 2


@pytest.mark.parametrize("bad,field", [
    ({**RULE, "allowedOrigins": ["exa mple.com"]}, "allowedOrigins"),
    ({**RULE, "allowedOrigins": ["https://*.*.example.com"]}, "allowedOrigins"),
    ({**RULE, "allowedOrigins": ["https://*.exa*mple.com"]}, "allowedOrigins"),
    ({**RULE, "allowedOrigins": []}, "allowedOrigins"),
    ({**RULE, "allowedMethods": ["PATCH"]}, "allowedMethods"),
    ({**RULE, "allowedMethods": []}, "allowedMethods"),
    ({**RULE, "allowedHeaders": ["bad header"]}, "allowedHeaders"),
    ({**RULE, "allowedHeaders": ["a*b*"]}, "allowedHeaders"),
    ({**RULE, "exposeHeaders": ["x*"]}, "exposeHeaders"),
    ({**RULE, "maxAgeSeconds": -1}, "maxAgeSeconds"),
    ({**RULE, "maxAgeSeconds": 2147483648}, "maxAgeSeconds"),
    ({**RULE, "maxAgeSeconds": True}, "maxAgeSeconds"),
    ({**RULE, "id": "x" * 256}, "id"),
    ({**RULE, "id": "bad\x00id"}, "id"),
    ({**RULE, "allowedOrigins": ["https://a\x01.example.com"]}, "allowedOrigins"),
    ({**RULE, "allowedOrigins": ["https://a.example.com\x7f"]}, "allowedOrigins"),
    ({**RULE, "bogus": 1}, "rule"),
])
def test_invalid_is_422_with_rule_number(env, bad, field):
    r = put(env, [RULE, bad], get(env)["etag"])
    assert r.status_code == 422, r.text
    errs = r.json()["errors"]
    assert errs[0]["rule"] == 2 and errs[0]["field"] == field
    assert r.json()["detail"].startswith("Rule 2")
    assert stored(env) is None


def test_too_many_rules_and_too_large(env):
    et = get(env)["etag"]
    assert put(env, [RULE] * 101, et).status_code == 422
    big = [{**RULE, "allowedHeaders": [f"x-h{n}-{j}" for j in range(50)]} for n in range(100)]
    r = put(env, big, et)
    assert r.status_code == 422 and "64 KB" in r.json()["detail"]
    assert stored(env) is None


def test_wildcard_origin_warns_but_saves(env):
    rules = [RULE, {"allowedOrigins": ["*"], "allowedMethods": ["GET", "PUT"]},
             {"allowedOrigins": ["*"], "allowedMethods": ["GET"]}]
    r = put(env, rules, get(env)["etag"])
    assert r.status_code == 200
    w = r.json()["warnings"]
    assert len(w) == 1 and w[0]["rule"] == 2 and "PUT" in w[0]["message"]
    assert get(env)["warnings"] == w
    assert len(stored(env)) == 3


def test_externally_configured_values_stay_saveable(env):
    rules = [{**RULE, "allowedOrigins": ["example.com", "https://a.example.org/x"],
              "maxAgeSeconds": 604800}]
    r = put(env, rules, get(env)["etag"])
    assert r.status_code == 200, r.text
    assert r.json()["rules"][0]["maxAgeSeconds"] == 604800
    w = r.json()["warnings"]
    assert len(w) == 1 and "example.com" in w[0]["message"] and w[0]["rule"] == 1
    again = put(env, r.json()["rules"], r.json()["etag"])
    assert again.status_code == 200 and again.json()["changed"] is False


def test_readback_failure_still_audits(env):
    from unittest.mock import patch

    from bucketeer import cors
    real = cors._read
    calls = []

    def flaky(s3, bucket):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("boom")
        return real(s3, bucket)
    with patch.object(cors, "_read", flaky):
        r = put(env, [RULE], get(env)["etag"])
    assert r.status_code == 200 and r.json()["rules"][0]["allowedOrigins"] == RULE["allowedOrigins"]
    assert len(env.audit("bucket.cors")) == 1


def test_audit_summary_is_capped(env):
    from bucketeer import cors
    rules = [{**RULE, "allowedHeaders": [f"x-h{n}-{j}" for j in range(40)]} for n in range(20)]
    assert put(env, rules, get(env)["etag"]).status_code == 200
    d = env.audit("bucket.cors")[0]["details"]
    assert d["after"] == {"omitted": "larger than 8 KB"} and d["before"] == []
    assert cors.AUDIT_MAX == 8192


def test_etag_conflict(env):
    stale = get(env)["etag"]
    env.moto().put_bucket_cors(Bucket="alice-data", CORSConfiguration={"CORSRules": [
        {"AllowedOrigins": ["https://other.org"], "AllowedMethods": ["GET"]}]})
    r = put(env, [RULE], stale)
    assert r.status_code == 409 and r.json()["etagMismatch"] is True
    assert r.json()["current"]["rules"][0]["allowedOrigins"] == ["https://other.org"]
    assert stored(env)[0]["AllowedOrigins"] == ["https://other.org"]
    assert env.audit("bucket.cors") == []


def test_etag_checked_inside_the_lock(env):
    e0 = get(env)["etag"]
    codes = []

    def go(origin):
        codes.append(put(env, [{**RULE, "allowedOrigins": [origin]}], e0).status_code)

    ts = [threading.Thread(target=go, args=(f"https://t{i}.org",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [200, 409]


def test_other_users_bucket_404(env):
    assert env.alice.get(url("bob-shared")).status_code == 404
    assert put_to(env, "bob-shared").status_code == 404
    assert env.audit("bucket.cors") == []


def put_to(env, name):
    return env.alice.put(url(name), json={"rules": [RULE], "etag": "x"}, headers=H)


def test_suspended_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(url()).status_code == 403
    assert put(env, [RULE], "x").status_code == 403
    assert stored(env) is None


def test_csrf_and_login(env):
    assert put(env, [RULE], get(env)["etag"], headers={}).status_code == 403
    assert env.anon().get(url()).status_code in (401, 403)
    assert stored(env) is None


def test_audit_rows(env):
    j = put(env, [RULE], get(env)["etag"]).json()
    put(env, [], j["etag"])
    rows = list(reversed(env.audit("bucket.cors")))
    assert len(rows) == 2 and rows[0]["target"] == "alice-data" and rows[0]["actor"] == "alice"
    d = rows[0]["details"]
    assert d["rules_before"] == 0 and d["rules_after"] == 1
    assert d["after"][0]["origins"] == ["https://example.org"]
    assert rows[1]["details"]["rules_after"] == 0
