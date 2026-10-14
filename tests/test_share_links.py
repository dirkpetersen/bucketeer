"""GET /api/buckets/{name}/objects and POST /api/buckets/{name}/share-link (moto)."""

import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from bucketeer.db import REDACTED, scrub

from .test_buckets import ALICE_KEY, BOB_KEY, Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def objs(name="alice-data"):
    return f"/api/buckets/{name}/objects"


def share(client, key="data/file-0001.bin", expires=3600, name="alice-data", headers=H):
    return client.post(f"/api/buckets/{name}/share-link",
                       json={"key": key, "expiresIn": expires}, headers=headers)


def put(env, key, body=b"hello", bucket="alice-data"):
    env.moto().put_object(Bucket=bucket, Key=key, Body=body)


# -- autocomplete listing ----------------------------------------------------------


def test_listing_prefix_and_delimiter(env):
    put(env, "docs/a.txt", b"aa")
    put(env, "docs/sub/b.txt")
    put(env, "top.txt")
    r = env.alice.get(objs(), params={"prefix": "docs/"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    j = r.json()
    assert [e["key"] for e in j["entries"]] == ["docs/a.txt"]
    assert j["entries"][0]["size"] == 2 and j["entries"][0]["lastModified"].endswith("Z")
    assert j["prefixes"] == [{"prefix": "docs/sub/"}]
    assert j["truncated"] is False
    root = env.alice.get(objs()).json()
    assert {p["prefix"] for p in root["prefixes"]} == {"data/", "docs/"}
    assert [e["key"] for e in root["entries"]] == ["top.txt"]


def test_listing_limit_and_truncated(env):
    r = env.alice.get(objs(), params={"prefix": "data/", "limit": 5}).json()
    assert len(r["entries"]) + len(r["prefixes"]) == 5 and r["truncated"] is True
    default = env.alice.get(objs(), params={"prefix": "data/"}).json()
    assert len(default["entries"]) == 20 and default["truncated"] is True
    big = env.alice.get(objs(), params={"prefix": "data/", "limit": 50}).json()
    assert len(big["entries"]) == 42 and big["truncated"] is False


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"limit": "x"},
                                    {"prefix": "a" * 1025}, {"prefix": "a\x00b"},
                                    {"prefix": "é" * 600}])
def test_listing_bad_parameters(env, params):
    assert env.alice.get(objs(), params=params).status_code == 422


def test_listing_unknown_and_foreign_bucket(env):
    assert env.alice.get(objs("nope")).status_code == 404
    assert env.alice.get(objs("bob-shared")).status_code == 404
    assert env.alice.get(objs("../x")).status_code in (404, 422)


def test_listing_uses_own_key_and_is_read_only(env):
    env.alice.get(objs())
    assert env.used and set(env.used) == {ALICE_KEY}
    assert env.alice.post(objs(), json={}, headers=H).status_code in (404, 405)
    assert env.alice.delete(objs(), headers=H).status_code in (404, 405)


def test_listing_suspended_and_anonymous(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(objs()).status_code == 403
    assert env.anon().get(objs()).status_code in (401, 403)


# -- share links -------------------------------------------------------------------


def test_share_link_works_and_is_presigned_for_public_endpoint(env, s3_endpoint):
    put(env, "docs/hello.txt", b"hello world")
    r = share(env.alice, "docs/hello.txt", 3600)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["expiresAt"].endswith("Z")
    url = urlsplit(j["url"])
    assert f"{url.scheme}://{url.netloc}" == s3_endpoint
    assert url.path == "/alice-data/docs/hello.txt"  # path-style
    q = parse_qs(url.query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"] and q["X-Amz-Expires"] == ["3600"]
    assert q["X-Amz-Credential"][0].startswith(ALICE_KEY + "/")
    got = httpx.get(j["url"])
    assert got.status_code == 200 and got.content == b"hello world"


def test_client_is_built_for_the_resolved_public_endpoint(env, s3_endpoint):
    seen = []
    real = env.app.state.s3.factory
    env.app.state.s3.factory = lambda ep, *a: (seen.append(ep), real(ep, *a))[1]
    share(env.alice)
    assert seen == [s3_endpoint]


@pytest.mark.parametrize("seconds", [60, 3600, 86400, 604800])
def test_expiry_bounds_accepted(env, seconds):
    assert share(env.alice, expires=seconds).status_code == 200


@pytest.mark.parametrize("seconds", [0, -1, 59, 604801, 10**9])
def test_expiry_bounds_rejected(env, seconds):
    assert share(env.alice, expires=seconds).status_code == 422
    assert env.audit("bucket.share_link") == []


@pytest.mark.parametrize("body", [{"key": "a"}, {"expiresIn": 60}, {"key": "a", "expiresIn": "60"},
                                  {"key": "a", "expiresIn": 60.5}, {"key": 1, "expiresIn": 60},
                                  {"key": "", "expiresIn": 60}, {"key": "a\nb", "expiresIn": 60},
                                  {"key": "k" * 1025, "expiresIn": 60},
                                  {"key": "a", "expiresIn": 60, "extra": 1}])
def test_bad_bodies(env, body):
    r = env.alice.post("/api/buckets/alice-data/share-link", json=body, headers=H)
    assert r.status_code == 422


def test_expiresat_matches_expiry(env):
    from datetime import UTC, datetime
    j = share(env.alice, expires=7200).json()
    delta = datetime.strptime(j["expiresAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC) - datetime.now(UTC)
    assert 7190 <= delta.total_seconds() <= 7200


def test_nonexistent_key_is_404(env):
    r = share(env.alice, "data/missing.bin")
    assert r.status_code == 404
    assert env.audit("bucket.share_link") == []


def test_folder_prefix_is_not_an_object(env):
    assert share(env.alice, "data/").status_code == 404


def test_other_users_bucket_and_unknown_bucket_404(env):
    env.moto().put_object(Bucket="bob-shared", Key="secret.txt", Body=b"x")
    assert share(env.alice, "secret.txt", name="bob-shared").status_code == 404
    assert share(env.alice, name="nope").status_code == 404
    assert env.audit("bucket.share_link") == []


def test_suspended_is_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert share(env.alice).status_code == 403


def test_csrf_and_login(env):
    assert share(env.alice, headers={}).status_code == 403
    assert share(env.anon()).status_code in (401, 403)


def test_signs_with_the_users_own_key(env):
    j = share(env.alice).json()
    assert set(env.used) == {ALICE_KEY}
    assert ALICE_KEY in j["url"] and BOB_KEY not in j["url"]
    env.used.clear()
    env.moto().put_object(Bucket="bob-shared", Key="k", Body=b"1")
    assert share(env.bob, "k", name="bob-shared").status_code == 200
    assert set(env.used) == {BOB_KEY}


def test_audit_has_no_url_or_signature(env, caplog):
    caplog.set_level(logging.DEBUG)
    j = share(env.alice, "data/file-0002.bin", 86400).json()
    rows = env.audit("bucket.share_link")
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "alice" and row["target"] == "alice-data"
    details = row["details"] if isinstance(row["details"], dict) else json.loads(row["details"])
    assert details["key"] == "data/file-0002.bin" and details["expiresAt"] == j["expiresAt"]
    dump = json.dumps(rows, default=str)
    sig = parse_qs(urlsplit(j["url"]).query)["X-Amz-Signature"][0]
    for needle in (j["url"], sig, "Signature", "X-Amz-"):
        assert needle not in dump
        assert needle not in caplog.text
    # even if a URL ever slipped into the details, the scrubber redacts it
    assert scrub({"note": j["url"]}) == {"note": REDACTED}


def test_s3_failure_is_502_without_leaking(env):
    from unittest.mock import MagicMock

    from botocore.exceptions import ClientError
    broken = MagicMock()
    broken.head_object.side_effect = ClientError(
        {"Error": {"Code": "InternalError", "Message": "boom SECRET"}}, "HeadObject")
    env.app.state.s3.factory = lambda *a: broken
    r = share(env.alice)
    assert r.status_code == 502 and "SECRET" not in r.text


def test_head_403_is_a_clear_403(env):
    from unittest.mock import MagicMock

    from botocore.exceptions import ClientError
    broken = MagicMock()
    broken.head_object.side_effect = ClientError(
        {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
    env.app.state.s3.factory = lambda *a: broken
    r = share(env.alice)
    assert r.status_code == 403 and "bucket key" in r.json()["detail"]
    assert env.audit("bucket.share_link") == []


SPECIAL_KEYS = ["with space.txt", "plus+sign.txt", "literal%20percent.txt", "ünï/cødé-日本.txt",
                "q?x=1&y=2#frag.txt", "double//slash.txt", "quote\"and'apostrophe.txt",
                "a b/c+d/e%2Ff.txt"]


@pytest.mark.parametrize("key", SPECIAL_KEYS)
def test_special_character_keys_download(env, key):
    body = key.encode()
    put(env, key, body)
    r = share(env.alice, key)
    assert r.status_code == 200
    got = httpx.get(r.json()["url"])
    assert got.status_code == 200 and got.content == body
