"""GET/PUT /api/buckets/{name}/lifecycle/rules (Advanced full editor) against mock croit + moto.

moto drops object-size filters and NewerNoncurrentVersions when it stores a lifecycle
configuration, so those are asserted on the request the S3 client sends (`sent`)."""

import copy
import threading

import pytest
from botocore.exceptions import ClientError

from .test_buckets import ALICE_KEY, Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    e.sent = []
    real = e.app.state.s3.factory

    def recording(endpoint, access, secret, settings):
        client = real(endpoint, access, secret, settings)

        def grab(params, **_):
            e.sent.append(copy.deepcopy(params["LifecycleConfiguration"]))
        client.meta.events.register(
            "before-parameter-build.s3.PutBucketLifecycleConfiguration", grab)
        return client
    e.app.state.s3.factory = recording
    yield e
    e.db.close()


URL = "/api/buckets/alice-data/lifecycle/rules"
SIMPLE = "/api/buckets/alice-data/lifecycle"


def rule(rid="r1", **kw):
    base = {"id": rid, "status": "Enabled", "filter": {"prefix": "logs/"},
            "expiration": {"days": 30}, "noncurrentExpiration": None,
            "abortMultipartDays": None, "transitions": None}
    base.update(kw)
    return base


def get(env, url=URL):
    return env.alice.get(url).json()


def put(env, rules, etag, headers=H, **extra):
    return env.alice.put(URL, json={"rules": rules, "etag": etag, **extra}, headers=headers)


def save(env, rules, **extra):
    r = put(env, rules, get(env)["etag"], **extra)
    assert r.status_code == 200, r.text
    return r.json()


def stored(env):
    try:
        return env.moto().get_bucket_lifecycle_configuration(Bucket="alice-data")["Rules"]
    except ClientError:
        return None


def seed(env, rules):
    env.moto().put_bucket_lifecycle_configuration(
        Bucket="alice-data", LifecycleConfiguration={"Rules": rules})


def test_get_empty(env):
    j = get(env)
    assert j["rules"] == [] and j["warnings"] == [] and j["etag"]
    assert j["versioning"] in ("off", "enabled", "suspended", None)
    assert env.alice.get(URL).headers["cache-control"] == "no-store"
    assert env.used and set(env.used) == {ALICE_KEY}


def test_round_trip_each_filter_type(env):
    rules = [
        rule("pre", filter={"prefix": "logs/"}),
        rule("tag", filter={"tags": [{"key": "team", "value": "bio"}]}),
        rule("and", filter={"prefix": "p/", "tags": [{"key": "a", "value": "1"},
                                                     {"key": "b", "value": "2"}],
                            "sizeGreaterThan": 10, "sizeLessThan": 1000}),
        rule("big", filter={"sizeGreaterThan": 1024}),
        rule("small", filter={"sizeLessThan": 5}),
        rule("none", filter={}),
        rule("tagsize", filter={"tags": [{"key": "k", "value": ""}], "sizeLessThan": 7}),
    ]
    j = save(env, rules)
    assert j["changed"] is True
    sent = {r["ID"]: r["Filter"] for r in env.sent[-1]["Rules"]}
    assert sent["pre"] == {"Prefix": "logs/"}
    assert sent["tag"] == {"Tag": {"Key": "team", "Value": "bio"}}
    assert sent["and"] == {"And": {"Prefix": "p/", "Tags": [{"Key": "a", "Value": "1"},
                                                              {"Key": "b", "Value": "2"}],
                                     "ObjectSizeGreaterThan": 10, "ObjectSizeLessThan": 1000}}
    assert sent["big"] == {"ObjectSizeGreaterThan": 1024}
    assert sent["small"] == {"ObjectSizeLessThan": 5}
    assert sent["none"] == {"Prefix": ""}
    assert sent["tagsize"] == {"And": {"Tags": [{"Key": "k", "Value": ""}],
                                       "ObjectSizeLessThan": 7}}
    got = {r["id"]: r for r in get(env)["rules"]}
    assert got["pre"]["filter"] == {"prefix": "logs/"}
    assert got["tag"]["filter"] == {"tags": [{"key": "team", "value": "bio"}]}
    assert got["and"]["filter"]["tags"] == [{"key": "a", "value": "1"}, {"key": "b", "value": "2"}]
    assert got["and"]["filter"]["prefix"] == "p/"
    assert got["none"]["filter"] == {} and all(not r["managed"] for r in got.values())
    assert set(env.used) == {ALICE_KEY}
    again = put(env, j["rules"], j["etag"])
    assert again.status_code == 200 and again.json()["changed"] is False


def test_expiration_variants(env):
    j = save(env, [
        rule("days", expiration={"days": 7}),
        rule("date", expiration={"date": "2099-01-31"}),
        rule("off", status="Disabled", expiration={"days": 3}),
        rule("marker", filter={"prefix": "m/"}, expiration={"expiredObjectDeleteMarker": True}),
    ])
    by = {r["id"]: r for r in j["rules"]}
    assert by["date"]["expiration"] == {"date": "2099-01-31T00:00:00Z"}
    assert by["off"]["status"] == "Disabled"
    assert by["marker"]["expiration"] == {"expiredObjectDeleteMarker": True}
    st = {r["ID"]: r for r in stored(env)}
    assert st["days"]["Expiration"] == {"Days": 7}
    assert st["marker"]["Expiration"] == {"ExpiredObjectDeleteMarker": True}
    assert st["date"]["Expiration"]["Date"].year == 2099


def test_noncurrent_newer_versions_and_abort(env):
    j = save(env, [
        rule("nc", expiration=None,
             noncurrentExpiration={"days": 14, "newerNoncurrentVersions": 3}),
        rule("ab", expiration=None, abortMultipartDays=2),
    ])
    assert env.sent[-1]["Rules"][0]["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": 14, "NewerNoncurrentVersions": 3}
    assert env.sent[-1]["Rules"][1]["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 2}
    by = {r["id"]: r for r in j["rules"]}
    assert by["nc"]["noncurrentExpiration"]["days"] == 14
    assert by["ab"]["abortMultipartDays"] == 2


def test_warnings(env):
    j = save(env, [rule("all", filter={}), rule("old", filter={"prefix": "x/"},
                                                  expiration={"date": "2001-01-01"}),
                   rule("nc", filter={"prefix": "y/"}, expiration=None,
                        noncurrentExpiration={"days": 5}),
                   rule("quiet", status="Disabled", filter={})])
    assert any(w["id"] == "all" and "no filter" in w["message"] for w in j["warnings"])
    assert any(w["id"] == "old" and "passed" in w["message"] for w in j["warnings"])
    if j["versioning"] == "off":
        assert any(w["id"] == "nc" and "Versioning is off" in w["message"] for w in j["warnings"])
    assert not any(w["id"] == "quiet" for w in j["warnings"])
    assert get(env)["warnings"] == j["warnings"]


def test_empty_deletes_configuration(env):
    save(env, [rule()])
    assert stored(env)
    j = save(env, [])
    assert j["rules"] == [] and stored(env) is None
    assert len(env.audit("bucket.lifecycle_rules")) == 2


def test_managed_rule_edit_needs_confirmation(env):
    assert env.alice.put(SIMPLE, headers=H, json={
        "expireCurrentDays": 30, "expireNoncurrentDays": None, "abortMultipartDays": 7,
        "confirm": True}).status_code == 200
    j = get(env)
    by = {r["id"]: r for r in j["rules"]}
    assert by["bucketeer-expire-current"]["managed"] is True
    edited = copy.deepcopy(j["rules"])
    edited[0]["expiration"] = {"days": 60}
    r = put(env, edited, j["etag"])
    assert r.status_code == 409 and r.json()["requiresConfirmation"] is True
    assert r.json()["managedChanges"] == ["bucketeer-expire-current"]
    assert stored(env)[0]["Expiration"] == {"Days": 30}
    # removing a managed rule needs it too; leaving them alone does not
    assert put(env, edited[1:], j["etag"]).status_code == 409
    other = j["rules"] + [rule("mine")]
    assert put(env, other, j["etag"]).status_code == 200
    j2 = get(env)
    r = put(env, edited + [rule("mine")], j2["etag"], confirmManaged=True)
    assert r.status_code == 200, r.text
    assert stored(env)[0]["Expiration"] == {"Days": 60}
    d = env.audit("bucket.lifecycle_rules")[0]["details"]
    assert d["confirm_managed"] is True and "bucketeer-expire-current" in d["changed_ids"]
    # the simple dialog still reads the edited managed rule
    assert get(env, SIMPLE)["expireCurrentDays"] == 60


def _managed(env, current=30, abort=7):
    assert env.alice.put(SIMPLE, headers=H, json={
        "expireCurrentDays": current, "expireNoncurrentDays": 5, "abortMultipartDays": abort,
        "confirm": True}).status_code == 200
    return get(env)


@pytest.mark.parametrize("change", [
    lambda r: r.update(filter={"prefix": "tmp/"}),
    lambda r: r.update(status="Disabled"),
    lambda r: r.update(filter={"sizeLessThan": 5}),
    lambda r: r.update(abortMultipartDays=3),
    lambda r: r.update(id="bucketeer-other"),
])
def test_managed_rule_only_days_or_removal(env, change):
    j = _managed(env)
    bad = copy.deepcopy(j["rules"])
    change(bad[0])
    for confirm in (False, True):
        r = put(env, bad, j["etag"], confirmManaged=confirm)
        assert r.status_code == 422, r.text
    assert stored(env)[0]["Filter"] == {"Prefix": ""} and stored(env)[0]["Status"] == "Enabled"
    assert get(env, SIMPLE)["expireCurrentDays"] == 30


def test_managed_days_change_and_remove_with_confirm(env):
    j = _managed(env)
    ids = [r["id"] for r in j["rules"]]
    edited = copy.deepcopy(j["rules"])
    edited[0]["expiration"] = {"days": 45}
    assert put(env, edited, j["etag"]).status_code == 409
    assert put(env, edited, j["etag"], confirmManaged=True).status_code == 200
    assert get(env, SIMPLE)["expireCurrentDays"] == 45
    j = get(env)
    assert put(env, j["rules"], j["etag"]).status_code == 200  # untouched: no confirm
    assert put(env, j["rules"][1:], j["etag"]).status_code == 409
    r = put(env, j["rules"][1:], j["etag"], confirmManaged=True)
    assert r.status_code == 200 and [x["id"] for x in r.json()["rules"]] == ids[1:]
    assert get(env, SIMPLE)["expireCurrentDays"] is None
    # bad days on a managed rule
    j = get(env)
    edited = copy.deepcopy(j["rules"])
    edited[0]["noncurrentExpiration"] = {"days": 0}
    assert put(env, edited, j["etag"], confirmManaged=True).status_code == 422


def test_cannot_create_managed_id(env):
    r = put(env, [rule("bucketeer-mine")], get(env)["etag"])
    assert r.status_code == 422 and "reserved" in r.json()["errors"][0]["message"]


TRANS = {"ID": "cold", "Status": "Enabled", "Filter": {"Prefix": "cold/"},
         "Expiration": {"Days": 400},
         "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}],
         "NoncurrentVersionTransitions": [{"NoncurrentDays": 30, "StorageClass": "GLACIER"}]}


def test_transition_rule_kept_or_removed_only(env):
    seed(env, [TRANS])
    j = get(env)
    r0 = j["rules"][0]
    assert r0["transitions"] == {
        "current": [{"Days": 30, "StorageClass": "GLACIER"}],
        "noncurrent": [{"NoncurrentDays": 30, "StorageClass": "GLACIER"}]}
    # kept as stored next to a new rule: written back exactly
    j2 = save(env, [r0, rule("mine")])
    st = stored(env)[0]
    assert st["Transitions"] == TRANS["Transitions"] and st["Expiration"] == {"Days": 400}
    assert j2["rules"][0]["transitions"] == r0["transitions"]
    # any change is refused: transitions, expiration, widening the filter, enabling
    def widen(r):
        r["filter"] = {}
        r["status"] = "Enabled"
    for mutate in (lambda r: r["transitions"]["current"][0].update(Days=1),
                   lambda r: r.update(transitions=None),
                   lambda r: r["transitions"]["current"].append(
                       {"Days": 9, "StorageClass": "X"}),
                   lambda r: r.update(expiration={"days": 500}),
                   widen,
                   lambda r: r.update(status="Disabled")):
        bad = copy.deepcopy(j2["rules"])
        mutate(bad[0])
        r = put(env, bad, j2["etag"])
        assert r.status_code == 422, r.text
        assert r.json()["errors"][0]["field"] == "transitions"
        assert "aren't available yet" in r.json()["detail"]
    fresh = rule("new", transitions={"current": [{"Days": 1, "StorageClass": "X"}],
                                     "noncurrent": []})
    assert put(env, j2["rules"] + [fresh], j2["etag"]).status_code == 422
    assert stored(env)[0]["Transitions"] == TRANS["Transitions"]
    # removal is allowed
    assert put(env, [rule("mine")], j2["etag"]).status_code == 200
    assert [r["ID"] for r in stored(env)] == ["mine"]


def test_unchanged_invalid_rule_is_accepted(env):
    seed(env, [{"ID": "x" * 300, "Status": "Enabled", "Filter": {"Prefix": ""},
                "Expiration": {"Days": 50000}}])
    j = get(env)
    r = put(env, j["rules"] + [rule("ok")], j["etag"])
    assert r.status_code == 200, r.text
    assert stored(env)[0]["Expiration"] == {"Days": 50000}
    # touching it makes the strict checks apply
    j = get(env)
    bad = copy.deepcopy(j["rules"])
    bad[0]["status"] = "Disabled"
    r = put(env, bad, j["etag"])
    assert r.status_code == 422 and {e["field"] for e in r.json()["errors"]} >= {"id", "expiration"}


def test_legacy_prefix_rule_written_back_as_stored(env):
    seed(env, [{"ID": "old", "Status": "Enabled", "Prefix": "old/", "Expiration": {"Days": 3}}])
    j = get(env)
    assert j["rules"][0]["filter"] == {"prefix": "old/"}
    assert put(env, j["rules"] + [rule("n")], j["etag"]).status_code == 200
    assert env.sent[-1]["Rules"][0].get("Prefix") == "old/"
    assert "Filter" not in env.sent[-1]["Rules"][0]


@pytest.mark.parametrize("bad,field", [
    (dict(id=""), "id"),
    (dict(id="x" * 256), "id"),
    (dict(id="a\x00b"), "id"),
    (dict(status="Paused"), "status"),
    (dict(expiration=None), "rule"),
    (dict(expiration={"days": 0}), "expiration"),
    (dict(expiration={"days": 36501}), "expiration"),
    (dict(expiration={"days": True}), "expiration"),
    (dict(expiration={"days": 5, "date": "2099-01-01"}), "expiration"),
    (dict(expiration={"days": 5, "expiredObjectDeleteMarker": True}), "expiration"),
    (dict(expiration={"date": "2099-01-01T05:00:00Z"}), "expiration"),
    (dict(expiration={"date": "tomorrow"}), "expiration"),
    (dict(expiration={"expiredObjectDeleteMarker": True},
          filter={"tags": [{"key": "a", "value": "b"}]}), "expiration"),
    (dict(expiration=None, noncurrentExpiration={"newerNoncurrentVersions": 2}),
     "noncurrentExpiration"),
    (dict(expiration=None, noncurrentExpiration={"days": 0}), "noncurrentExpiration"),
    (dict(expiration=None, noncurrentExpiration={"days": 5, "newerNoncurrentVersions": 0}),
     "noncurrentExpiration"),
    (dict(expiration=None, noncurrentExpiration={"days": 5, "newerNoncurrentVersions": 101}),
     "noncurrentExpiration"),
    (dict(expiration=None, abortMultipartDays=0), "abortMultipartDays"),
    (dict(expiration=None, abortMultipartDays=3, filter={"sizeLessThan": 5}), "abortMultipartDays"),
    (dict(filter={"prefix": "a" * 1025}), "filter"),
    (dict(filter={"tags": [{"key": "", "value": "x"}]}), "filter"),
    (dict(filter={"tags": [{"key": "k", "value": "x#"}]}), "filter"),
    (dict(filter={"tags": [{"key": "k", "value": "1"}, {"key": "k", "value": "2"}]}), "filter"),
    (dict(filter={"tags": [{"key": f"k{i}", "value": ""} for i in range(11)]}), "filter"),
    (dict(filter={"sizeGreaterThan": -1}), "filter"),
    (dict(filter={"sizeGreaterThan": 10, "sizeLessThan": 10}), "filter"),
    (dict(filter={"sizeLessThan": 1.5}), "filter"),
    (dict(filter={"bogus": 1}), "filter"),
    (dict(bogus=1), "rule"),
])
def test_invalid_is_422_with_rule_number(env, bad, field):
    r = put(env, [rule("fine"), rule("bad", **bad)], get(env)["etag"])
    assert r.status_code == 422, r.text
    errs = r.json()["errors"]
    assert errs[0]["rule"] == 2 and errs[0]["field"] == field
    assert r.json()["detail"].startswith("Rule 2")
    assert stored(env) is None


def test_duplicate_ids_and_rule_limit(env):
    et = get(env)["etag"]
    r = put(env, [rule("a"), rule("a")], et)
    assert r.status_code == 422 and r.json()["errors"][0]["rule"] == 2
    r = put(env, [rule(f"r{i}") for i in range(1001)], et)
    assert r.status_code == 422 and r.json()["errors"][0]["rule"] is None
    assert stored(env) is None


def test_etag_conflict(env):
    stale = get(env)["etag"]
    seed(env, [{"ID": "other", "Status": "Enabled", "Filter": {"Prefix": ""},
                "Expiration": {"Days": 9}}])
    r = put(env, [rule()], stale)
    assert r.status_code == 409 and r.json()["etagMismatch"] is True
    assert r.json()["current"]["rules"][0]["id"] == "other"
    assert stored(env)[0]["ID"] == "other"
    assert env.audit("bucket.lifecycle_rules") == []


def test_etag_checked_inside_the_lock(env):
    e0 = get(env)["etag"]
    codes = []

    def go(n):
        codes.append(put(env, [rule(f"t{n}")], e0).status_code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [200, 409]


def test_simple_api_coexists(env):
    save(env, [rule("mine", filter={"prefix": "x/"})])
    r = env.alice.put(SIMPLE, headers=H, json={
        "expireCurrentDays": 10, "expireNoncurrentDays": None, "abortMultipartDays": None,
        "confirm": True})
    assert r.status_code == 200 and r.json()["foreignRules"] == 1
    ids = [x["id"] for x in get(env)["rules"]]
    assert ids == ["mine", "bucketeer-expire-current"]
    assert get(env)["rules"][1]["managed"] is True


def test_audit_entry(env):
    save(env, [rule("a")])
    save(env, [rule("a"), rule("b", filter={"sizeGreaterThan": 3})])
    a = env.audit("bucket.lifecycle_rules")
    assert len(a) == 2
    d = a[0]["details"]
    assert d["rules_before"] == 1 and d["rules_after"] == 2 and d["changed_ids"] == ["b"]
    assert d["confirm_managed"] is False and a[0]["target"] == "alice-data"
    assert d["before"][0]["id"] == "a" and d["after"][1]["id"] == "b"
    save(env, [rule("a"), rule("b", filter={"sizeGreaterThan": 3})])  # unchanged: no entry
    assert len(env.audit("bucket.lifecycle_rules")) == 2


def test_audit_summary_is_capped(env):
    save(env, [rule(f"rule-{i}", filter={"prefix": "p" * 200 + str(i)}) for i in range(60)])
    d = env.audit("bucket.lifecycle_rules")[0]["details"]
    assert d["after"] == {"omitted": "larger than 8 KB"} and d["before"] == []


def test_other_users_bucket_404(env):
    assert env.alice.get("/api/buckets/bob-shared/lifecycle/rules").status_code == 404
    r = env.alice.put("/api/buckets/bob-shared/lifecycle/rules", headers=H,
                      json={"rules": [], "etag": "x"})
    assert r.status_code == 404
    assert env.audit("bucket.lifecycle_rules") == []


def test_suspended_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(URL).status_code == 403
    assert put(env, [rule()], "x").status_code == 403
    assert stored(env) is None


def test_csrf_and_login(env):
    assert put(env, [rule()], get(env)["etag"], headers={}).status_code == 403
    assert env.anon().get(URL).status_code in (401, 403)
    assert stored(env) is None
