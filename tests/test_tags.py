"""GET/PUT /api/buckets/{name}/tags (Advanced tag editor) against mock croit and moto."""

import threading

import pytest

from .test_buckets import ALICE_KEY, Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def url(name="alice-data"):
    return f"/api/buckets/{name}/tags"


def get(env):
    return env.alice.get(url()).json()


def put(env, tags, etag, headers=H, name="alice-data"):
    return env.alice.put(url(name), json={"tags": tags, "etag": etag}, headers=headers)


def t(k, v=""):
    return {"key": k, "value": v}


def stored(env, bucket):
    from botocore.exceptions import ClientError
    try:
        ts = env.moto().get_bucket_tagging(Bucket=bucket)["TagSet"]
    except ClientError:
        return {}
    return {x["Key"]: x.get("Value", "") for x in ts}  # moto omits empty values


def seed_budget(env, value="WT-123"):
    env.moto().put_bucket_tagging(Bucket="alice-data", Tagging={"TagSet": [
        {"Key": "bucketeer-budget", "Value": value}]})


def test_get_empty(env):
    j = get(env)
    assert j["tags"] == [] and j["etag"]
    assert env.alice.get(url()).headers["cache-control"] == "no-store"
    assert env.used and set(env.used) == {ALICE_KEY}


def test_round_trip_preserves_managed_tag(env):
    seed_budget(env)
    j = get(env)
    assert j["tags"] == [{"key": "bucketeer-budget", "value": "WT-123", "managed": True}]
    r = put(env, [t("team", "genomics"), t("Projekt Ü", "a b+c=d.e_f:g/h@i"), t("empty")],
            j["etag"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changed"] is True
    assert [x["key"] for x in out["tags"]] == ["bucketeer-budget", "team", "Projekt Ü", "empty"]
    assert out["tags"][0]["managed"] is True and out["tags"][1]["managed"] is False
    assert stored(env, "alice-data") == {"bucketeer-budget": "WT-123", "team": "genomics",
                                      "Projekt Ü": "a b+c=d.e_f:g/h@i", "empty": ""}
    assert get(env) == {k: v for k, v in out.items() if k != "changed"}
    # removing every user tag keeps the managed one; unchanged saves are no-ops
    r = put(env, [], out["etag"])
    assert r.json()["changed"] is True and stored(env, "alice-data") == {
        "bucketeer-budget": "WT-123"}
    again = put(env, [], r.json()["etag"])
    assert again.status_code == 200 and again.json()["changed"] is False
    assert set(env.used) == {ALICE_KEY}
    d = env.audit("bucket.tags")
    assert len(d) == 2 and d[0]["details"]["after"] == [] and d[0]["target"] == "alice-data"
    assert d[1]["details"]["before"] == [] and len(d[1]["details"]["after"]) == 3
    assert all("bucketeer-budget" not in str(x["details"]) for x in d)


def test_last_user_tag_removed_deletes_tag_set_without_managed(env):
    r = put(env, [t("a", "1")], get(env)["etag"])
    r = put(env, [], r.json()["etag"])
    assert r.status_code == 200 and stored(env, "alice-data") == {}


@pytest.mark.parametrize("key", ["bucketeer-budget", "bucketeer-x", "Bucketeer-X", "BUCKETEER-1"])
def test_managed_key_in_input_is_422(env, key):
    seed_budget(env)
    r = put(env, [t("ok"), t(key, "x")], get(env)["etag"])
    assert r.status_code == 422, r.text
    e = r.json()["errors"][0]
    assert e["tag"] == 2 and e["field"] == "key" and "managed" in e["message"]
    assert stored(env, "alice-data") == {"bucketeer-budget": "WT-123"}
    assert env.audit("bucket.tags") == []


@pytest.mark.parametrize("bad,field", [
    (t(""), "key"), (t("k" * 129), "key"), (t("a", "v" * 257), "value"),
    (t("bad#key"), "key"), (t("a", "bad<value"), "value"), (t("a\x00b"), "key"),
    (t("a", "line\nbreak"), "value"), (t("aws:thing"), "key"), (t("AWS:Thing"), "key"),
    (t("ok", None) | {"extra": 1}, "tag"),
])
def test_invalid_tag_is_422_with_tag_number(env, bad, field):
    r = put(env, [t("fine"), bad], get(env)["etag"])
    assert r.status_code == 422, r.text
    e = r.json()["errors"][0]
    assert e["tag"] == 2 and e["field"] == field
    assert r.json()["detail"].startswith("Tag 2")
    assert stored(env, "alice-data") == {}


def test_limits_accept_boundaries(env):
    r = put(env, [t("k" * 128, "v" * 256)], get(env)["etag"])
    assert r.status_code == 200, r.text


def test_duplicate_keys_case_sensitive(env):
    et = get(env)["etag"]
    r = put(env, [t("a", "1"), t("a", "2")], et)
    assert r.status_code == 422 and r.json()["errors"][0]["tag"] == 2
    assert put(env, [t("a", "1"), t("A", "2")], et).status_code == 200


def test_fifty_tag_limit_counts_managed(env):
    seed_budget(env)
    et = get(env)["etag"]
    r = put(env, [t(f"k{i}") for i in range(50)], et)
    assert r.status_code == 422 and r.json()["errors"][0]["tag"] is None
    assert "49" in r.json()["detail"]
    assert stored(env, "alice-data") == {"bucketeer-budget": "WT-123"}
    assert put(env, [t(f"k{i}") for i in range(49)], et).status_code == 200
    assert len(stored(env, "alice-data")) == 50
    # without a managed tag all 50 fit; 51 do not
    assert put(env, [t(f"k{i}") for i in range(51)], get(env)["etag"]).status_code == 422


def test_non_object_body(env):
    et = get(env)["etag"]
    assert env.alice.put(url(), json={"tags": ["x"], "etag": et}, headers=H).status_code == 422
    assert env.alice.put(url(), json={"tags": [], "etag": et, "z": 1},
                         headers=H).status_code == 422


def test_etag_conflict(env):
    stale = get(env)["etag"]
    env.moto().put_bucket_tagging(Bucket="alice-data", Tagging={"TagSet": [
        {"Key": "other", "Value": "1"}]})
    r = put(env, [t("mine")], stale)
    assert r.status_code == 409 and r.json()["etagMismatch"] is True
    assert r.json()["current"]["tags"][0]["key"] == "other"
    assert stored(env, "alice-data") == {"other": "1"}
    assert env.audit("bucket.tags") == []


def test_etag_checked_inside_the_lock(env):
    e0 = get(env)["etag"]
    codes = []

    def go(k):
        codes.append(put(env, [t(k)], e0).status_code)

    ts = [threading.Thread(target=go, args=(f"t{i}",)) for i in range(2)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert sorted(codes) == [200, 409]


def test_other_users_bucket_404(env):
    assert env.alice.get(url("bob-shared")).status_code == 404
    assert put(env, [t("a")], "x", name="bob-shared").status_code == 404
    assert env.alice.get(url("Bad:Name")).status_code == 404
    assert env.audit("bucket.tags") == []


def test_suspended_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(url()).status_code == 403
    assert put(env, [t("a")], "x").status_code == 403
    assert stored(env, "alice-data") == {}


def test_csrf_and_login(env):
    assert put(env, [t("a")], get(env)["etag"], headers={}).status_code == 403
    assert env.anon().get(url()).status_code in (401, 403)


def test_externally_set_odd_managed_case_is_kept(env):
    env.moto().put_bucket_tagging(Bucket="alice-data", Tagging={"TagSet": [
        {"Key": "Bucketeer-Old", "Value": "keep"}, {"Key": "mine", "Value": "1"}]})
    j = get(env)
    assert [x["managed"] for x in j["tags"]] == [True, False]
    assert put(env, [t("mine", "2")], j["etag"]).status_code == 200
    assert stored(env, "alice-data") == {"Bucketeer-Old": "keep", "mine": "2"}


def test_externally_set_odd_tags_do_not_block_saves(env):
    odd = [{"Key": "cost-center", "Value": "#42"}, {"Key": "owner", "Value": "Jane (lab)"},
           {"Key": "long", "Value": "v" * 300}]
    env.moto().put_bucket_tagging(Bucket="alice-data", Tagging={"TagSet": odd})
    j = get(env)
    keep = [t(x["key"], x["value"]) for x in j["tags"]]
    r = put(env, keep + [t("new", "1")], j["etag"])
    assert r.status_code == 200, r.text
    assert stored(env, "alice-data")["cost-center"] == "#42"
    assert len(stored(env, "alice-data")) == 4
    # changing an odd tag to another invalid value is still refused
    et = get(env)["etag"]
    bad = [t("cost-center", "#43") if x["key"] == "cost-center" else x for x in keep]
    r = put(env, bad, et)
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "value"
    # uniqueness and the bucketeer- prefix still apply to stored tags
    assert put(env, keep + [t("owner", "again")], et).status_code == 422
    assert stored(env, "alice-data")["cost-center"] == "#42"
