"""Unfinished-upload cleanup under /api/buckets/{name}/multipart against mock croit and moto."""

import pytest

from bucketeer import multipart

from .test_buckets import ALICE_KEY, Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def url(name="alice-data", tail=""):
    return f"/api/buckets/{name}/multipart{tail}"


def start(env, *keys, bucket="alice-data"):
    s3 = env.moto()
    return [s3.create_multipart_upload(Bucket=bucket, Key=k)["UploadId"] for k in keys]


def pending(env, bucket="alice-data"):
    return sorted((u["Key"], u["UploadId"]) for u in
                  env.moto().list_multipart_uploads(Bucket=bucket).get("Uploads", []))


def abort(env, body, headers=H, name="alice-data"):
    return env.alice.post(url(name, "/abort"), json=body, headers=headers)


@pytest.fixture
def paged(monkeypatch):
    """moto ignores max-uploads and markers; slice its full answer the way S3 pages it."""
    real = multipart._list_page

    def page(s3, bucket, prefix, key_marker, upload_marker, limit):
        ups = sorted(real(s3, bucket, prefix, "", "", 1000).get("Uploads", []),
                     key=lambda u: (u["Key"], u["UploadId"]))
        if key_marker:
            ups = [u for u in ups if (u["Key"], u["UploadId"]) > (key_marker, upload_marker)]
        out = {"Uploads": ups[:limit], "IsTruncated": len(ups) > limit}
        if out["IsTruncated"]:
            last = out["Uploads"][-1]
            out.update(NextKeyMarker=last["Key"], NextUploadIdMarker=last["UploadId"])
        return out
    monkeypatch.setattr(multipart, "_list_page", page)


def test_list_empty_and_own_key(env):
    r = env.alice.get(url())
    assert r.status_code == 200
    assert r.json() == {"uploads": [], "truncated": False, "nextKeyMarker": None,
                        "nextUploadIdMarker": None, "totalShown": 0}
    assert r.headers["cache-control"] == "no-store"
    assert env.used and set(env.used) == {ALICE_KEY}


def test_list_shape(env):
    (uid,) = start(env, "big/file.bin")
    j = env.alice.get(url()).json()
    assert j["totalShown"] == 1 and j["truncated"] is False
    u = j["uploads"][0]
    assert u["key"] == "big/file.bin" and u["uploadId"] == uid
    assert u["initiated"].endswith("Z")


def test_paging_and_prefix(env, paged):
    start(env, *[f"a/{i:02d}" for i in range(5)], "b/x")
    seen, params = [], {"limit": 2}
    for _ in range(10):
        j = env.alice.get(url(), params=params).json()
        seen += [u["key"] for u in j["uploads"]]
        if not j["truncated"]:
            break
        assert j["nextKeyMarker"]
        params = {"limit": 2, "keyMarker": j["nextKeyMarker"],
                  "uploadIdMarker": j["nextUploadIdMarker"]}
    assert sorted(seen) == sorted([f"a/{i:02d}" for i in range(5)] + ["b/x"])
    only = env.alice.get(url(), params={"prefix": "b/"}).json()
    assert [u["key"] for u in only["uploads"]] == ["b/x"]


def test_validation_of_list_params(env):
    assert env.alice.get(url(), params={"limit": 0}).status_code == 422
    assert env.alice.get(url(), params={"limit": 101}).status_code == 422
    assert env.alice.get(url(), params={"prefix": "a\nb"}).status_code == 422
    assert env.alice.get(url(), params={"keyMarker": "x" * 1025}).status_code == 422


def test_other_users_bucket_404(env):
    assert env.alice.get(url("bob-shared")).status_code == 404
    assert abort(env, {"uploads": [{"key": "k", "uploadId": "u"}]},
                 name="bob-shared").status_code == 404
    assert env.alice.get(url("Bad:Name")).status_code == 404
    assert env.alice.get(url("nope-nothing")).status_code == 404


def test_suspended_and_csrf(env):
    assert abort(env, {"uploads": [{"key": "k", "uploadId": "u"}]},
                 headers={}).status_code == 403
    assert env.anon().get(url()).status_code in (401, 403)
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(url()).status_code == 403
    assert abort(env, {"uploads": [{"key": "k", "uploadId": "u"}]}).status_code == 403


def test_abort_selected(env):
    a, b, c = start(env, "one", "two", "three")
    r = abort(env, {"uploads": [{"key": "one", "uploadId": a}, {"key": "two", "uploadId": b}]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert sorted(x["key"] for x in j["aborted"]) == ["one", "two"]
    assert j["failed"] == [] and j["notFound"] == [] and j["truncated"] is False
    assert pending(env) == [("three", c)]
    assert set(env.used) == {ALICE_KEY}
    (e,) = env.audit("bucket.multipart_abort")
    d = e["details"]
    assert e["target"] == "alice-data" and d["mode"] == "selected"
    assert d["aborted"] == 2 and d["failed"] == 0 and d["keys"] == ["one", "two"]
    assert a not in str(e) and b not in str(e)


def test_no_such_upload_is_not_found_and_not_audited(env):
    r = abort(env, {"uploads": [{"key": "ghost", "uploadId": "does-not-exist"}]})
    assert r.status_code == 200
    j = r.json()
    assert j["aborted"] == [] and j["failed"] == []
    assert j["notFound"] == [{"key": "ghost", "uploadId": "does-not-exist"}]
    assert env.audit("bucket.multipart_abort") == []


def test_mixed_and_failures_are_independent(env):
    (a,) = start(env, "real")
    r = abort(env, {"uploads": [{"key": "ghost", "uploadId": "x"},
                                {"key": "real", "uploadId": a}]})
    j = r.json()
    assert [x["key"] for x in j["aborted"]] == ["real"] and len(j["notFound"]) == 1
    # an S3 refusal for one upload is reported per upload, the others still run
    from botocore.exceptions import ClientError
    real = multipart._abort_all
    (b, c) = start(env, "k1", "k2")

    def flaky(s3, bucket, items):
        orig = s3.abort_multipart_upload

        def abort_one(**kw):
            if kw["Key"] == "k1":
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}},
                                  "AbortMultipartUpload")
            return orig(**kw)
        s3.abort_multipart_upload = abort_one
        return real(s3, bucket, items)

    env_patch = pytest.MonkeyPatch()
    env_patch.setattr(multipart, "_abort_all", flaky)
    try:
        r = abort(env, {"uploads": [{"key": "k1", "uploadId": b}, {"key": "k2", "uploadId": c}]})
    finally:
        env_patch.undo()
    j = r.json()
    assert j["failed"] == [{"key": "k1", "uploadId": b, "error": "AccessDenied"}]
    assert [x["key"] for x in j["aborted"]] == ["k2"]
    d = env.audit("bucket.multipart_abort")[0]["details"]
    assert d["aborted"] == 1 and d["failed"] == 1


def test_older_than_needs_confirm(env):
    start(env, "x1", "x2", "y1")  # moto stamps every upload 2010-11-10
    r = abort(env, {"olderThanDays": 5})
    assert r.status_code == 409
    assert r.json() == {**r.json(), "requiresConfirmation": True, "count": 3, "truncated": False}
    assert len(pending(env)) == 3 and env.audit("bucket.multipart_abort") == []
    # a prefix narrows the set
    r = abort(env, {"olderThanDays": 5, "prefix": "x"})
    assert r.status_code == 409 and r.json()["count"] == 2
    r = abort(env, {"olderThanDays": 5, "prefix": "x", "confirm": True})
    assert r.status_code == 200, r.text
    assert len(r.json()["aborted"]) == 2 and r.json()["truncated"] is False
    assert [k for k, _ in pending(env)] == ["y1"]
    (e,) = env.audit("bucket.multipart_abort")
    d = e["details"]
    assert d["mode"] == "older_than" and d["olderThanDays"] == 5 and d["prefix"] == "x"
    assert d["aborted"] == 2 and d["keys"] == ["x1", "x2"]
    # nothing is older than 30000 days: confirm is a no-op without an audit row
    r = abort(env, {"olderThanDays": 30000, "confirm": True})
    assert r.status_code == 200 and r.json()["aborted"] == []
    assert len(env.audit("bucket.multipart_abort")) == 1
    assert [k for k, _ in pending(env)] == ["y1"]


def test_young_uploads_survive(env):
    start(env, "fresh")
    r = abort(env, {"olderThanDays": 30000})
    assert r.status_code == 409 and r.json()["count"] == 0
    assert len(pending(env)) == 1


def test_older_than_is_bounded(env, paged, monkeypatch):
    start(env, *[f"k{i}" for i in range(5)])
    monkeypatch.setattr(multipart, "MAX_ABORT", 3)
    monkeypatch.setattr(multipart, "SCAN_PAGE", 2)
    r = abort(env, {"olderThanDays": 5})
    assert r.json()["count"] == 3 and r.json()["truncated"] is True
    r = abort(env, {"olderThanDays": 5, "confirm": True})
    assert len(r.json()["aborted"]) == 3 and r.json()["truncated"] is True
    assert len(pending(env)) == 2
    r = abort(env, {"olderThanDays": 5, "confirm": True})
    assert len(r.json()["aborted"]) == 2 and r.json()["truncated"] is False
    assert pending(env) == []
    assert len(env.audit("bucket.multipart_abort")) == 2


def test_audit_keys_capped(env):
    ids = start(env, *[f"key{i:03d}" for i in range(60)])
    items = [{"key": f"key{i:03d}", "uploadId": u} for i, u in enumerate(ids)]
    assert abort(env, {"uploads": items}).status_code == 200
    d = env.audit("bucket.multipart_abort")[0]["details"]
    assert d["aborted"] == 60 and len(d["keys"]) == 50


@pytest.mark.parametrize("body", [
    {},
    {"uploads": []},
    {"uploads": [{"key": "k", "uploadId": "u"}], "olderThanDays": 3},
    {"uploads": [{"key": "k", "uploadId": "u"}], "confirm": True},
    {"uploads": [{"key": "k", "uploadId": "u"}] * 101},
    {"uploads": [{"key": "", "uploadId": "u"}]},
    {"uploads": [{"key": "k", "uploadId": ""}]},
    {"uploads": [{"key": "k\n", "uploadId": "u"}]},
    {"uploads": [{"key": "k", "uploadId": "u", "extra": 1}]},
    {"olderThanDays": 0},
    {"olderThanDays": 36501},
    {"olderThanDays": "3"},
    {"olderThanDays": 3, "confirm": "yes"},
    {"olderThanDays": 3, "prefix": "a\x00"},
    {"olderThanDays": 3, "bogus": 1},
])
def test_validation(env, body):
    assert abort(env, body).status_code == 422
    assert env.audit("bucket.multipart_abort") == []


def test_scan_budget_reports_truncated(env, paged, monkeypatch):
    start(env, *[f"s{i}" for i in range(5)])
    monkeypatch.setattr(multipart, "SCAN_PAGE", 2)
    monkeypatch.setattr(multipart, "SCAN_PAGES", 1)
    r = abort(env, {"olderThanDays": 5})
    assert r.status_code == 409 and r.json()["count"] == 2 and r.json()["truncated"] is True


class StubS3:
    def __init__(self, uploads):
        self.uploads = uploads

    def list_multipart_uploads(self, **kw):
        return {"Uploads": self.uploads, "IsTruncated": False}


def _u(key, when):
    d = {"Key": key, "UploadId": "id-" + key}
    if when is not False:
        d["Initiated"] = when
    return d


def test_older_cutoff_is_judged_on_aware_times():
    from datetime import UTC, datetime, timedelta, timezone
    now = datetime.now(UTC)
    plus5 = timezone(timedelta(hours=5))
    ups = [_u("young", now - timedelta(days=1)), _u("old-utc", now - timedelta(days=10)),
           _u("old-offset", (now - timedelta(days=10)).astimezone(plus5)),
           _u("edge-young", (now - timedelta(days=6)).astimezone(plus5)),
           _u("missing", False)]
    found, trunc = multipart._older(StubS3(ups), "b", "", now - timedelta(days=7))
    assert [f["key"] for f in found] == ["old-utc", "old-offset"] and trunc is False


def test_naive_start_time_fails_closed(env, monkeypatch):
    from datetime import datetime, timedelta
    naive = datetime.now() - timedelta(days=10)
    with pytest.raises(multipart.NaiveTimestamp):
        multipart._older(StubS3([_u("n", naive)]), "b", "",
                         datetime.now().astimezone() - timedelta(days=7))
    start(env, "keep")
    monkeypatch.setattr(multipart, "_list_page",
                        lambda *a, **k: {"Uploads": [_u("keep", naive)], "IsTruncated": False})
    r = abort(env, {"olderThanDays": 7, "confirm": True})
    assert r.status_code == 502 and len(pending(env)) == 1
    assert env.audit("bucket.multipart_abort") == []


def test_plain_exception_is_unavailable_and_others_abort(env, monkeypatch):
    a, b, c = start(env, "k1", "k2", "k3")
    real = multipart._abort_all

    def flaky(s3, bucket, items):
        orig = s3.abort_multipart_upload

        def one(**kw):
            if kw["Key"] == "k2":
                raise ConnectionError("boom")
            return orig(**kw)
        s3.abort_multipart_upload = one
        return real(s3, bucket, items)
    monkeypatch.setattr(multipart, "_abort_all", flaky)
    j = abort(env, {"uploads": [{"key": "k1", "uploadId": a}, {"key": "k2", "uploadId": b},
                                {"key": "k3", "uploadId": c}]}).json()
    assert [x["key"] for x in j["aborted"]] == ["k1", "k3"]
    assert j["failed"] == [{"key": "k2", "uploadId": b, "error": "Unavailable"}]


def test_no_such_bucket_midway_keeps_progress(env, monkeypatch):
    from botocore.exceptions import ClientError
    a, b, c = start(env, "k1", "k2", "k3")
    real = multipart._abort_all

    def gone(s3, bucket, items):
        orig = s3.abort_multipart_upload

        def one(**kw):
            if kw["Key"] == "k2":
                raise ClientError({"Error": {"Code": "NoSuchBucket", "Message": "x"}}, "Abort")
            return orig(**kw)
        s3.abort_multipart_upload = one
        return real(s3, bucket, items)
    monkeypatch.setattr(multipart, "_abort_all", gone)
    r = abort(env, {"uploads": [{"key": "k1", "uploadId": a}, {"key": "k2", "uploadId": b},
                                {"key": "k3", "uploadId": c}]})
    j = r.json()
    assert r.status_code == 200 and [x["key"] for x in j["aborted"]] == ["k1"]
    assert [x["key"] for x in j["failed"]] == ["k2", "k3"]
    d = env.audit("bucket.multipart_abort")[0]["details"]
    assert d["aborted"] == 1 and d["failed"] == 2


def test_audit_key_list_is_size_capped(env):
    keys = ["k" * 900 + f"{i:02d}" for i in range(20)]
    ids = start(env, *keys)
    assert abort(env, {"uploads": [{"key": k, "uploadId": u}
                                   for k, u in zip(keys, ids, strict=True)]}).status_code == 200
    d = env.audit("bucket.multipart_abort")[0]["details"]
    assert d["aborted"] == 20 and 0 < len(d["keys"]) < 20
    assert sum(len(k) for k in d["keys"]) <= 8192
