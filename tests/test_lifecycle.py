"""GET/PUT /api/buckets/{name}/lifecycle against the mock croit and moto."""

import pytest
from botocore.exceptions import ClientError

from bucketeer import s3docs

from .test_buckets import ALICE_KEY, Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def url(name="alice-data"):
    return f"/api/buckets/{name}/lifecycle"


def body(cur=None, non=None, mp=None, **extra):
    return {"expireCurrentDays": cur, "expireNoncurrentDays": non,
            "abortMultipartDays": mp, **extra}


def put(client, name="alice-data", headers=H, **kw):
    return client.put(url(name), json=body(**kw), headers=headers)


def moto_rules(env, bucket="alice-data"):
    try:
        return env.moto().get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    except ClientError:
        return None


FOREIGN = {"ID": "team-tmp", "Status": "Enabled", "Filter": {"Prefix": "tmp/"},
           "Expiration": {"Days": 3}}


def seed_foreign(env, bucket="alice-data"):
    env.moto().put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration={"Rules": [FOREIGN]})


def test_get_empty(env):
    r = env.alice.get(url())
    assert r.status_code == 200
    assert r.json() == {"expireCurrentDays": None, "expireNoncurrentDays": None,
                        "abortMultipartDays": None, "foreignRules": 0, "versioning": "off"}


def test_set_each_rule(env):
    r = put(env.alice, cur=30, non=7, mp=2, confirm=True)
    assert r.status_code == 200
    j = r.json()
    assert (j["expireCurrentDays"], j["expireNoncurrentDays"],
            j["abortMultipartDays"]) == (30, 7, 2)
    assert j["changed"] is True
    ids = {x["ID"] for x in moto_rules(env)}
    assert ids == {s3docs.EXPIRE_CURRENT_ID, s3docs.EXPIRE_NONCURRENT_ID,
                   s3docs.ABORT_MULTIPART_ID}
    got = env.alice.get(url()).json()
    assert (got["expireCurrentDays"], got["expireNoncurrentDays"],
            got["abortMultipartDays"]) == (30, 7, 2)


@pytest.mark.parametrize("kw,rid", [
    ({"non": 5}, s3docs.EXPIRE_NONCURRENT_ID),
    ({"mp": 3}, s3docs.ABORT_MULTIPART_ID),
    ({"cur": 9, "confirm": True}, s3docs.EXPIRE_CURRENT_ID),
])
def test_single_rule(env, kw, rid):
    assert put(env.alice, **kw).status_code == 200
    assert [x["ID"] for x in moto_rules(env)] == [rid]


def test_update_and_noop(env):
    put(env.alice, mp=5)
    r = put(env.alice, mp=9)
    assert r.json()["abortMultipartDays"] == 9 and r.json()["changed"] is True
    assert len(moto_rules(env)) == 1
    assert put(env.alice, mp=9).json()["changed"] is False
    assert len(env.audit("bucket.lifecycle")) == 2


def test_remove_and_empty_deletes_configuration(env):
    put(env.alice, non=4, mp=2)
    r = put(env.alice, mp=2)
    assert r.json()["expireNoncurrentDays"] is None
    assert [x["ID"] for x in moto_rules(env)] == [s3docs.ABORT_MULTIPART_ID]
    assert put(env.alice).json()["changed"] is True
    assert moto_rules(env) is None  # configuration deleted, not an empty one


def test_foreign_rules_preserved(env):
    seed_foreign(env)
    assert env.alice.get(url()).json()["foreignRules"] == 1
    r = put(env.alice, non=7, mp=1)
    assert r.json()["foreignRules"] == 1
    rules = moto_rules(env)
    assert [x["ID"] for x in rules][0] == "team-tmp"
    assert next(x for x in rules if x["ID"] == "team-tmp")["Expiration"] == {"Days": 3}
    assert len(rules) == 3
    put(env.alice)  # remove all ours
    rules = moto_rules(env)
    assert [x["ID"] for x in rules] == ["team-tmp"]


def test_validation_errors(env):
    for bad in (0, -1, s3docs.MAX_DAYS + 1, 1.5, "7", True):
        r = env.alice.put(url(), json=body(mp=bad), headers=H)
        assert r.status_code == 422, bad
    assert env.alice.put(url(), json={"abortMultipartDays": 3}, headers=H).status_code == 422
    assert env.alice.put(url(), json=body(extra=1, mp=1), headers=H).status_code == 422
    assert moto_rules(env) is None
    assert env.audit("bucket.lifecycle") == []


def test_expire_current_needs_confirm(env):
    r = put(env.alice, cur=30)
    assert r.status_code == 409 and r.json()["requiresConfirmation"] is True
    assert moto_rules(env) is None and env.audit("bucket.lifecycle") == []
    assert put(env.alice, cur=30, confirm=True).status_code == 200
    # lengthening and keeping need no confirm; shortening does
    assert put(env.alice, cur=60).status_code == 200
    assert put(env.alice, cur=60, mp=1).status_code == 200
    r = put(env.alice, cur=10, mp=1)
    assert r.status_code == 409
    assert moto_rules(env)[0]["Expiration"]["Days"] == 60
    assert put(env.alice, cur=10, mp=1, confirm=True).status_code == 200
    # removing it needs none, other rules never do
    assert put(env.alice, mp=1).status_code == 200
    assert put(env.alice, non=3, mp=1).status_code == 200


def test_noncurrent_hint_when_versioning_off(env):
    r = put(env.alice, non=7)
    assert r.status_code == 200 and "versioning" in r.json()["hint"].lower()
    assert "hint" not in put(env.alice, mp=1).json()
    env.alice.put("/api/buckets/alice-data/versioning", json={"enabled": True}, headers=H)
    r = put(env.alice, non=7, mp=1)
    assert "hint" not in r.json() and r.json()["versioning"] == "enabled"


def test_other_users_bucket_is_404(env):
    assert env.alice.get(url("bob-shared")).status_code == 404
    assert put(env.alice, "bob-shared", mp=1).status_code == 404
    assert put(env.alice, "nope-nothing", mp=1).status_code == 404
    assert put(env.alice, "Bad:Name", mp=1).status_code == 404
    assert moto_rules(env, "bob-shared") is None
    assert env.audit("bucket.lifecycle") == []


def test_suspended_user_is_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(url()).status_code == 403
    assert put(env.alice, mp=1).status_code == 403
    assert moto_rules(env) is None


def test_csrf_and_login(env):
    assert put(env.alice, headers={}, mp=1).status_code == 403
    assert env.anon().get(url()).status_code in (401, 403)
    assert put(env.anon(), mp=1).status_code in (401, 403)
    assert moto_rules(env) is None


def test_audit_before_after(env):
    put(env.alice, non=7)
    put(env.alice, non=14, mp=2)
    rows = list(reversed(env.audit("bucket.lifecycle")))
    assert rows[0]["target"] == "alice-data" and rows[0]["actor"] == "alice"
    assert rows[0]["details"]["before"] == {
        "expireCurrentDays": None, "expireNoncurrentDays": None, "abortMultipartDays": None}
    assert rows[0]["details"]["after"]["expireNoncurrentDays"] == 7
    assert rows[1]["details"]["before"]["expireNoncurrentDays"] == 7
    assert rows[1]["details"]["after"] == {
        "expireCurrentDays": None, "expireNoncurrentDays": 14, "abortMultipartDays": 2}


def test_users_own_key_is_used(env):
    env.used.clear()
    env.alice.get(url())
    put(env.alice, mp=1)
    assert env.used and set(env.used) == {ALICE_KEY}


def test_s3_failure_is_502(env):
    from unittest.mock import MagicMock
    broken = MagicMock()
    broken.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": "InternalError", "Message": "boom"}}, "GetBucketLifecycle")
    env.app.state.s3.factory = lambda *a: broken
    assert env.alice.get(url()).status_code == 502
    assert put(env.alice, mp=1).status_code == 502


# -- foreign rules in unusual shapes, byte-for-byte -----------------------------------

LEGACY_PREFIX = {"ID": "legacy", "Status": "Enabled", "Prefix": "logs/",
                 "Expiration": {"Days": 5}}
AND_TAGS = {"ID": "and-tags", "Status": "Enabled",
            "Filter": {"And": {"Prefix": "p/", "Tags": [{"Key": "k", "Value": "v"},
                                                         {"Key": "k2", "Value": "v2"}]}},
            "Expiration": {"Days": 9}}
DISABLED_TAG = {"ID": "tag-off", "Status": "Disabled",
                "Filter": {"Tag": {"Key": "a", "Value": "b"}},
                "Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}],
                "NoncurrentVersionExpiration": {"NoncurrentDays": 60}}


def foreign_in_moto(env):
    rules = moto_rules(env) or []
    return [r for r in rules if not r["ID"].startswith("bucketeer-")]


def seed(env, *rules):
    env.moto().put_bucket_lifecycle_configuration(
        Bucket="alice-data", LifecycleConfiguration={"Rules": list(rules)})
    return foreign_in_moto(env)  # what moto itself reports back, the reference


@pytest.mark.parametrize("rule", [LEGACY_PREFIX, AND_TAGS, DISABLED_TAG],
                         ids=["legacy-prefix", "and-tags", "disabled-tag-transition"])
def test_unusual_foreign_rules_survive(env, rule):
    ref = seed(env, rule)
    assert len(ref) == 1
    assert env.alice.get(url()).json()["foreignRules"] == 1
    assert put(env.alice, mp=4, non=6).status_code == 200
    assert foreign_in_moto(env) == ref
    assert put(env.alice, cur=20).status_code == 409  # refused confirm writes nothing
    assert foreign_in_moto(env) == ref
    assert len(moto_rules(env)) == 3
    assert put(env.alice).status_code == 200  # remove all of ours
    assert moto_rules(env) == ref


def test_all_unusual_rules_together(env):
    ref = seed(env, LEGACY_PREFIX, AND_TAGS, DISABLED_TAG)
    assert len(ref) == 3
    put(env.alice, cur=20, mp=1, confirm=True)
    assert foreign_in_moto(env) == ref
    put(env.alice)
    assert moto_rules(env) == ref


def test_409_then_confirmed_put_succeeds(env):
    first = put(env.alice, cur=15)
    assert first.status_code == 409 and first.json()["requiresConfirmation"] is True
    assert moto_rules(env) is None
    second = put(env.alice, cur=15, confirm=True)
    assert second.status_code == 200 and second.json()["expireCurrentDays"] == 15
    assert moto_rules(env)[0]["Expiration"]["Days"] == 15
    assert len(env.audit("bucket.lifecycle")) == 1
