"""Deleting cloud (AWS) buckets, P6-13 (owner 2026-11-25): the typed name for every delete,
objects and bytes from a live page plus CloudWatch, and the background job that empties a
bucket with a TEMPORARY key of the owner's IAM user (made and deleted even on failure; two keys
already = a clear failure). moto `mock_aws`, no network."""

import asyncio
import json

import pytest
from botocore.exceptions import ClientError

from bucketeer import bucket_delete, cloud_delete

from . import test_cloud_buckets
from .test_admin import H
from .test_cloud_buckets import NEW, B, acts, size

env = test_cloud_buckets.env  # the fixture

FULL = "bucketeer-alice-scans"


def delete(env, name, confirm=None):
    body = {"confirmName": confirm} if confirm is not None else None
    return env.user.request("DELETE", f"{B}/{name}", headers=H, json=body)


def run_job(env, job_id):
    asyncio.run(bucket_delete.run_job(env.app, job_id))
    return env.db.delete_jobs.get(job_id)


def iam_user(env):
    return env.db.cloud_accounts.get("alice")["iam_user"]


def keys_of(env):
    return env.iam.list_access_keys(UserName=iam_user(env))["AccessKeyMetadata"]


def create(env, **kw):
    assert env.user.post(B, json={**NEW, **kw}, headers=H).status_code == 201


@pytest.fixture
def made(env, monkeypatch):
    """The secrets of every temporary key made (each must be deleted again)."""
    monkeypatch.setattr(cloud_delete, "KEY_WAITS", ())
    seen: list[str] = []
    real = env.cloud.create_access_key

    async def spy(name):
        k = await real(name)
        seen.append(k["secretKey"])
        return k
    monkeypatch.setattr(env.cloud, "create_access_key", spy)
    return seen


def no_secret_stored(env, seen):
    rows = json.dumps(env.db.query("SELECT * FROM audit_log")) + \
        json.dumps(env.db.query("SELECT * FROM bucket_delete_jobs"))
    assert seen and not any(s in rows for s in seen)


def fail(code):
    def broken(*_a):
        raise ClientError({"Error": {"Code": code, "Message": "boom"}}, "ListObjectVersions")
    return broken


def test_empty_bucket_needs_the_typed_name(env):
    create(env)
    for confirm in (None, "", "Scans", FULL):
        r = delete(env, "scans", confirm)
        assert r.status_code == 409, confirm
        body = r.json()
        assert body["requiresConfirmation"] is True and body["objects"] == 0
        assert body["bytes"] == 0 and body["countsMayLag"] is True
        assert body["storageClass"] == "intelligent"
    assert env.db.buckets.get(FULL) is not None and acts(env, "cloud.bucket.delete") == []
    r = delete(env, "scans", "scans")
    assert r.status_code == 200 and r.json() == {"name": "scans"}
    assert env.s3.list_buckets()["Buckets"] == [] and env.db.buckets.get(FULL) is None
    [a] = acts(env, "cloud.bucket.delete")
    assert a["target"] == FULL and a["details"]["dataDeleted"] is False


def test_content_is_reported_then_a_job_empties_and_deletes(env, made):
    create(env, storageClass="deep_archive")
    for i in range(3):
        env.s3.put_object(Bucket=FULL, Key=f"k{i}", Body=b"x" * 10)
    env.s3.put_object(Bucket=FULL, Key="k0", Body=b"new")  # an old version too
    env.s3.delete_object(Bucket=FULL, Key="k1")  # and a delete marker
    env.s3.create_multipart_upload(Bucket=FULL, Key="big")
    size(FULL, 5 * 2**30, storage="DeepArchiveStorage")
    r = delete(env, "scans")
    assert r.status_code == 409
    body = r.json()
    assert body["objects"] >= 2 and body["bytes"] >= 5 * 2**30  # live page, CloudWatch
    assert body["storageClass"] == "deep_archive" and body["countsMayLag"] is True
    assert "contains data" in body["detail"]
    assert delete(env, "scans", "wrong").status_code == 409
    assert env.db.delete_jobs.latest_for(FULL) is None
    r = delete(env, "scans", "scans")
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["state"] == "queued" and job["bucket"] == "scans" and job["fullName"] == FULL
    again = delete(env, "scans", "scans")
    assert again.status_code == 409 and again.json()["job"]["id"] == job["id"]
    assert [j["id"] for j in env.user.get(f"{B}/delete-jobs").json()["jobs"]] == [job["id"]]
    assert env.user.get("/api/buckets/delete-jobs").json()["jobs"] == []  # not on prem
    assert keys_of(env) == []
    done = run_job(env, job["id"])
    assert done["state"] == "done", done["last_error"]
    assert done["deleted_objects"] == 5 and done["temp_key"] is None
    assert env.s3.list_buckets()["Buckets"] == [] and env.db.buckets.get(FULL) is None
    assert keys_of(env) == [] and len(made) == 1  # the temporary key is gone again
    [started] = acts(env, "cloud.bucket.delete_started")
    assert started["details"]["job"] == job["id"]
    [a] = acts(env, "cloud.bucket.delete")
    assert a["actor"] == "alice" and a["details"]["objects"] == 5
    assert a["details"]["dataDeleted"] is True
    no_secret_stored(env, made)


def test_two_cloud_keys_fail_the_job_with_a_clear_message(env, made):
    create(env)
    env.s3.put_object(Bucket=FULL, Key="a", Body=b"x")
    for _ in range(2):
        env.iam.create_access_key(UserName=iam_user(env))
    job = delete(env, "scans", "scans").json()["job"]
    done = run_job(env, job["id"])
    assert done["state"] == "failed" and "two cloud keys" in done["last_error"]
    assert len(keys_of(env)) == 2 and made == []
    assert env.db.buckets.get(FULL) is not None
    [f] = acts(env, "cloud.bucket.delete_failed")
    assert f["details"]["job"] == job["id"]
    # the person deletes one of their keys and retries
    env.iam.delete_access_key(UserName=iam_user(env), AccessKeyId=keys_of(env)[0]["AccessKeyId"])
    r = env.user.post(f"{B}/delete-jobs/{job['id']}/retry", headers=H)
    assert r.status_code == 200 and r.json()["job"]["state"] == "queued"
    assert run_job(env, job["id"])["state"] == "done"
    assert len(keys_of(env)) == 1  # only the person's own key is left
    assert acts(env, "cloud.bucket.delete_retry")
    assert env.user.post(f"{B}/delete-jobs/{job['id']}/retry", headers=H).status_code == 409


def test_the_temporary_key_is_deleted_when_the_job_fails(env, made, monkeypatch):
    create(env)
    env.s3.put_object(Bucket=FULL, Key="a", Body=b"x")
    job = delete(env, "scans", "scans").json()["job"]
    monkeypatch.setattr(cloud_delete, "_batch", fail("InternalError"))
    done = run_job(env, job["id"])
    assert done["state"] == "failed" and "InternalError" in done["last_error"]
    assert keys_of(env) == [] and len(made) == 1 and done["temp_key"] is None
    # the bucket stays, and so does the owner's access to it
    stmts = json.loads(env.s3.get_bucket_policy(Bucket=FULL)["Policy"])["Statement"]
    assert [s["Sid"] for s in stmts] == ["bucketeer-owner"]
    no_secret_stored(env, made)


def test_a_new_key_is_retried_while_iam_propagates_it(env, made, monkeypatch):
    monkeypatch.setattr(cloud_delete, "KEY_WAITS", (0, 0))
    create(env)
    env.s3.put_object(Bucket=FULL, Key="a", Body=b"x")
    job = delete(env, "scans", "scans").json()["job"]
    real, calls = cloud_delete._batch, []

    def slow(*a):
        calls.append(1)
        if len(calls) < 3:
            return fail("InvalidAccessKeyId")()
        return real(*a)
    monkeypatch.setattr(cloud_delete, "_batch", slow)
    assert run_job(env, job["id"])["state"] == "done"
    assert len(calls) >= 3 and keys_of(env) == []


def test_a_crash_leftover_key_is_deleted_on_the_next_run(env, made):
    create(env)
    env.s3.put_object(Bucket=FULL, Key="a", Body=b"x")
    job = delete(env, "scans", "scans").json()["job"]
    left = env.iam.create_access_key(UserName=iam_user(env))["AccessKey"]["AccessKeyId"]
    env.db.delete_jobs.set_temp_key(job["id"], left)
    assert run_job(env, job["id"])["state"] == "done"
    assert keys_of(env) == []


def test_only_old_versions_left_starts_a_job(env, made):
    create(env)
    env.s3.put_object(Bucket=FULL, Key="a", Body=b"x")
    env.s3.delete_object(Bucket=FULL, Key="a")  # no current object, a version and a marker
    r = delete(env, "scans")
    assert r.status_code == 409 and r.json()["objects"] == 0
    r = delete(env, "scans", "scans")
    assert r.status_code == 202  # S3 said BucketNotEmpty: the job removes the versions
    assert json.loads(env.s3.get_bucket_policy(Bucket=FULL)["Policy"])["Statement"]
    assert run_job(env, r.json()["job"]["id"])["state"] == "done"
    assert env.s3.list_buckets()["Buckets"] == []


def test_cloud_jobs_stay_apart_from_on_prem_jobs(env, made):
    create(env)
    env.s3.put_object(Bucket=FULL, Key="a", Body=b"x")
    job = delete(env, "scans", "scans").json()["job"]
    env.db.delete_jobs.finish(job["id"], "failed", "x")
    assert env.user.post(f"/api/buckets/delete-jobs/{job['id']}/retry",
                         headers=H).status_code == 404
    assert env.admin.post(f"{B}/delete-jobs/{job['id']}/retry", headers=H).status_code == 404
