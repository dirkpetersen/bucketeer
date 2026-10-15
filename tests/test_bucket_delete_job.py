"""Deleting a non-empty bucket as a background job on the rgw admin backend (Phase 5 A4).

Runs on the rgw backend only (the croit path is unchanged and covered by test_buckets.py).
Jobs are driven explicitly with `bucket_delete.run_job` (AUTOSTART is off in tests); two tests
turn the background task on. moto enforces object lock and legal hold on DeleteObjects but no
identities, so the user's own key is asserted with the factory spy.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from bucketeer import bucket_delete

from .backends import CURRENT
from .test_buckets import ALICE_KEY, Env, H


@pytest.fixture(autouse=True)
def rgw_backend():
    CURRENT["name"] = "rgw"
    yield
    CURRENT["name"] = "croit"


@pytest.fixture(autouse=True)
def small_batches(monkeypatch):
    monkeypatch.setattr(bucket_delete, "BATCH", 5)


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def run(env, job_id):
    asyncio.run(bucket_delete.run_job(env.app, job_id))
    job = env.db.delete_jobs.get(job_id)
    return job


def make(env, name="alice-big", versioned=True, lock=False, owner="alice"):
    env.croit_state.create_bucket(name, owner, versioning="ENABLED" if versioned else None,
                                  object_lock=lock)
    return env.bridge.owner_client(owner)


def fill(s3, bucket, n=12):
    """n objects, a second version of three, three deleted keys (delete markers), an upload."""
    for i in range(n):
        s3.put_object(Bucket=bucket, Key=f"d/{i:03d}", Body=b"x" * 10)
    for i in range(3):
        s3.put_object(Bucket=bucket, Key=f"d/{i:03d}", Body=b"y" * 20)
    for i in range(3, 6):
        s3.delete_object(Bucket=bucket, Key=f"d/{i:03d}")
    s3.create_multipart_upload(Bucket=bucket, Key="big/upload")


def delete(env, name, confirm=None, client=None):
    body = {"confirmName": confirm} if confirm else None
    return (client or env.alice).request("DELETE", f"/api/buckets/{name}", headers=H, json=body)


def versions(s3, bucket):
    page = s3.list_object_versions(Bucket=bucket)
    return len(page.get("Versions", [])) + len(page.get("DeleteMarkers", []))


def audited(env, action):
    return [a for a in env.audit("bucket.") if a["action"] == action]


def retry(env, job_id, client=None):
    return (client or env.alice).post(f"/api/buckets/delete-jobs/{job_id}/retry", headers=H)


# -- request ------------------------------------------------------------------------------


def test_empty_bucket_is_deleted_directly_with_the_users_key(env):
    r = delete(env, "alice-empty")
    assert r.status_code == 200 and r.json() == {"name": "alice-empty"}
    assert "alice-empty" not in env.moto_buckets() and "alice-empty" not in env.croit_state.buckets
    assert set(env.used) == {ALICE_KEY}
    (a,) = audited(env, "bucket.delete")
    assert a["target"] == "alice-empty" and a["details"]["objects"] == 0
    assert env.db.delete_jobs.latest_for("alice-empty") is None


def test_non_empty_needs_the_typed_name_then_starts_a_job(env):
    r = delete(env, "alice-data")
    assert r.status_code == 409 and r.json()["requiresConfirmation"] is True
    assert r.json()["objects"] == 42 and r.json()["bytes"] == 1048576
    assert delete(env, "alice-data", "wrong").status_code == 409
    assert env.db.delete_jobs.latest_for("alice-data") is None
    r = delete(env, "alice-data", "alice-data")
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["state"] == "queued" and job["bucket"] == "alice-data"
    assert job["objectsHint"] == 42 and job["deletedObjects"] == 0
    assert "alice-data" in env.moto_buckets()  # nothing happens until the job runs
    (a,) = audited(env, "bucket.delete_started")
    assert a["details"]["job"] == job["id"]


def test_one_active_job_per_bucket(env):
    first = delete(env, "alice-data", "alice-data").json()["job"]
    again = delete(env, "alice-data", "alice-data")
    assert again.status_code == 409 and again.json()["job"]["id"] == first["id"]
    assert len(env.db.query("SELECT * FROM bucket_delete_jobs")) == 1
    with pytest.raises(Exception, match="UNIQUE"):  # the database refuses it as well
        env.db.delete_jobs.create("alice-data", "alice", "alice", 1, 1)


def test_only_the_owner_deletes_and_sees_jobs(env):
    assert delete(env, "alice-data", "alice-data", env.bob).status_code == 404
    job = delete(env, "alice-data", "alice-data").json()["job"]
    assert env.bob.get("/api/buckets/delete-jobs").json() == {"jobs": []}
    assert retry(env, job["id"], env.bob).status_code == 404
    assert [j["id"] for j in env.alice.get("/api/buckets/delete-jobs").json()["jobs"]] == [
        job["id"]]


# -- the job ------------------------------------------------------------------------------


def test_job_empties_a_versioned_bucket_and_cleans_up(env):
    s3 = make(env)
    fill(s3, "alice-big")
    total = versions(s3, "alice-big")
    assert total == 12 + 3 + 3
    env.alice.post("/api/buckets/alice-big/keys", json={"label": "ci", "access": "read"},
                   headers=H)
    env.alice.post("/api/buckets/alice-big/shares", json={"login": "bob", "access": "read"},
                   headers=H)
    job = delete(env, "alice-big", "alice-big").json()["job"]
    assert job["objectsHint"] == total
    env.used.clear()
    done = run(env, job["id"])
    assert done["state"] == "done" and done["deleted_objects"] == total
    assert done["deleted_bytes"] == 9 * 10 + 3 * 10 + 3 * 20  # 9 current + 3 old + 3 new
    assert len(env.used) >= 6 and set(env.used) == {ALICE_KEY}  # batches of 5, the user's key
    assert "alice-big" not in env.moto_buckets() and "alice-big" not in env.croit_state.buckets
    assert env.db.buckets.get("alice-big") is None
    # keys and shares revoked as after a croit delete
    assert all(k["revoked_at"] for k in env.db.bucket_keys.list(include_revoked=True))
    assert "alice+ci" not in env.croit_state.users
    assert env.db.shares.list_live("alice-big") == []
    (a,) = audited(env, "bucket.delete")
    assert a["actor"] == "alice" and a["details"]["objects"] == total
    assert a["details"]["bytes"] == done["deleted_bytes"] and a["details"]["job"] == job["id"]
    assert [x["details"]["reason"] for x in env.audit("bucket_key.revoke")] == ["bucket_deleted"]
    assert env.audit("bucket.unshare")
    listed = env.alice.get("/api/buckets/delete-jobs").json()["jobs"]
    assert listed[0]["state"] == "done" and listed[0]["deletedObjects"] == total
    assert run(env, job["id"])["state"] == "done"  # running a finished job again does nothing
    assert len(audited(env, "bucket.delete")) == 1


def test_unversioned_bucket(env):
    s3 = make(env, "alice-plain", versioned=False)
    for i in range(7):
        s3.put_object(Bucket="alice-plain", Key=f"k{i}", Body=b"z")
    job = delete(env, "alice-plain", "alice-plain").json()["job"]
    done = run(env, job["id"])
    assert done["state"] == "done" and done["deleted_objects"] == 7
    assert "alice-plain" not in env.moto_buckets()


def test_progress_is_saved_after_each_batch(env, monkeypatch):
    s3 = make(env)
    fill(s3, "alice-big")
    job = delete(env, "alice-big", "alice-big").json()["job"]
    seen = []
    real = env.db.delete_jobs.progress

    def spy(job_id, deleted, size, remaining):
        real(job_id, deleted, size, remaining)
        seen.append(env.db.delete_jobs.get(job_id)["deleted_objects"])

    monkeypatch.setattr(env.db.delete_jobs, "progress", spy)
    run(env, job["id"])
    assert seen == sorted(seen) and len(set(seen)) >= 4 and seen[-1] == 18


def test_resume_after_a_restart(env, monkeypatch):
    s3 = make(env)
    fill(s3, "alice-big")
    job = delete(env, "alice-big", "alice-big").json()["job"]
    # the process died mid-way: the row says running and part of the data is gone
    env.db.delete_jobs.start(job["id"])
    page = s3.list_object_versions(Bucket="alice-big", MaxKeys=5)
    gone = [{"Key": v["Key"], "VersionId": v["VersionId"]}
            for v in page["Versions"] + page.get("DeleteMarkers", [])]
    s3.delete_objects(Bucket="alice-big", Delete={"Objects": gone})
    env.db.delete_jobs.progress(job["id"], len(gone), 50, None)
    assert env.db.delete_jobs.get(job["id"])["state"] == "running"
    monkeypatch.setattr(bucket_delete, "AUTOSTART", True)

    async def restart():
        assert await bucket_delete.resume(env.app) == 1
        await asyncio.gather(*bucket_delete._state(env.app).tasks.values())

    asyncio.run(restart())
    done = env.db.delete_jobs.get(job["id"])
    assert done["state"] == "done" and done["attempts"] == 2  # once before the crash, once resumed
    assert done["deleted_objects"] >= 18  # counts carry over
    assert "alice-big" not in env.moto_buckets()


def test_lifespan_resumes_unfinished_jobs_in_the_background(env, monkeypatch):
    s3 = make(env)
    fill(s3, "alice-big")
    job = delete(env, "alice-big", "alice-big").json()["job"]
    monkeypatch.setattr(bucket_delete, "RESUME_ON_START", True)
    monkeypatch.setattr(bucket_delete, "AUTOSTART", True)
    with TestClient(env.app) as client:
        assert client.get("/health").status_code == 200  # never waits for the job
        for _ in range(100):
            if env.db.delete_jobs.get(job["id"])["state"] == "done":
                break
            time.sleep(0.1)
    assert env.db.delete_jobs.get(job["id"])["state"] == "done"


def test_request_starts_the_task_itself(env, monkeypatch):
    s3 = make(env)
    fill(s3, "alice-big")
    monkeypatch.setattr(bucket_delete, "AUTOSTART", True)
    with TestClient(env.app, base_url="https://testserver") as c:
        c.cookies.update(env.alice.cookies)
        r = c.request("DELETE", "/api/buckets/alice-big", headers=H,
                      json={"confirmName": "alice-big"})
        assert r.status_code == 202
        state = ""
        for _ in range(100):
            state = c.get("/api/buckets/delete-jobs").json()["jobs"][0]["state"]
            if state == "done":
                break
            time.sleep(0.1)
        assert state == "done"
    assert "alice-big" not in env.moto_buckets()


# -- blocked by object lock -------------------------------------------------------------


def lock_up(s3, bucket, hold=True, retention=False):
    s3.put_object(Bucket=bucket, Key="free", Body=b"1")
    if hold:
        s3.put_object(Bucket=bucket, Key="held", Body=b"1", ObjectLockLegalHoldStatus="ON")
    if retention:
        s3.put_object(Bucket=bucket, Key="kept", Body=b"1", ObjectLockMode="GOVERNANCE",
                      ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=30))


def test_object_lock_and_legal_hold_block_the_delete(env):
    s3 = make(env, "alice-lock", lock=True)
    lock_up(s3, "alice-lock", hold=True, retention=True)
    job = delete(env, "alice-lock", "alice-lock").json()["job"]
    blocked = run(env, job["id"])
    assert blocked["state"] == "blocked" and blocked["remaining_objects"] == 2
    assert blocked["deleted_objects"] == 1  # "free" went
    msg = blocked["last_error"]
    assert "2 object versions are under object lock or legal hold" in msg
    assert "retention lasts until" in msg and "legal hold" in msg and "Retry" in msg
    assert "alice-lock" in env.moto_buckets() and "alice-lock" in env.croit_state.buckets
    assert versions(s3, "alice-lock") == 2  # nothing was forced
    (a,) = audited(env, "bucket.delete_blocked")
    assert a["details"]["remaining"] == 2 and a["details"]["job"] == job["id"]
    assert audited(env, "bucket.delete_failed") == [] and audited(env, "bucket.delete") == []
    # a second delete request retries the blocked job instead of making another
    again = delete(env, "alice-lock", "alice-lock")
    assert again.status_code == 202 and again.json()["job"]["id"] == job["id"]
    assert run(env, job["id"])["state"] == "blocked"
    assert len(env.db.query("SELECT * FROM bucket_delete_jobs")) == 1


def test_retry_after_the_hold_is_lifted(env):
    s3 = make(env, "alice-lock", lock=True)
    lock_up(s3, "alice-lock")
    job = delete(env, "alice-lock", "alice-lock").json()["job"]
    assert run(env, job["id"])["state"] == "blocked"
    r = retry(env, job["id"])
    assert r.status_code == 200 and r.json()["job"]["state"] == "queued"
    assert retry(env, job["id"]).status_code == 409  # not waiting any more
    s3.put_object_legal_hold(Bucket="alice-lock", Key="held", LegalHold={"Status": "OFF"})
    done = run(env, job["id"])
    assert done["state"] == "done" and "alice-lock" not in env.moto_buckets()
    assert [a["details"]["previous"] for a in audited(env, "bucket.delete_retry")] == ["blocked"]


# -- failures that can be retried -------------------------------------------------------


def test_suspended_owner_pauses_the_job_and_retry_finishes_it(env):
    job = delete(env, "alice-data", "alice-data").json()["job"]
    env.croit_state.users["alice"].suspended = True
    failed = run(env, job["id"])
    assert failed["state"] == "failed" and "suspended" in failed["last_error"]
    assert "alice-data" in env.moto_buckets()
    (a,) = audited(env, "bucket.delete_failed")
    assert a["details"]["job"] == job["id"]
    env.croit_state.users["alice"].suspended = False
    assert retry(env, job["id"]).status_code == 200
    done = run(env, job["id"])
    assert done["state"] == "done" and done["deleted_objects"] == 42
    assert "alice-data" not in env.moto_buckets()


def test_owner_without_a_key_pauses_the_job(env):
    job = delete(env, "alice-data", "alice-data").json()["job"]
    env.croit_state.users["alice"].keys.clear()
    failed = run(env, job["id"])
    assert failed["state"] == "failed" and "no access key" in failed["last_error"]
    assert audited(env, "bucket.delete_failed")


def test_s3_error_fails_the_job_without_leaking_details(env):
    job = delete(env, "alice-data", "alice-data").json()["job"]
    broken = MagicMock()
    broken.list_object_versions.side_effect = ClientError(
        {"Error": {"Code": "InternalError", "Message": "secret-ish internals"}}, "List")
    env.app.state.s3.factory = lambda *a: broken
    failed = run(env, job["id"])
    assert failed["state"] == "failed" and "InternalError" in failed["last_error"]
    assert "secret-ish" not in failed["last_error"]
    env.app.state.s3.factory = env.real_factory
    assert retry(env, job["id"]).status_code == 200
    assert run(env, job["id"])["state"] == "done"


def test_stale_stats_do_not_hide_data(env):
    s3 = make(env, "alice-late", versioned=False)
    s3.put_object(Bucket="alice-late", Key="k", Body=b"1")
    # croit's counters say empty, but the bucket is not: DeleteBucket answers BucketNotEmpty
    env.croit_state.hooks.bucket_usage = None
    env.croit_state.set_bucket_usage("alice-late", 0, 0)
    r = delete(env, "alice-late")
    assert r.status_code == 409 and r.json()["requiresConfirmation"] is True
    r = delete(env, "alice-late", "alice-late")
    assert r.status_code == 202
    assert run(env, r.json()["job"]["id"])["state"] == "done"


def test_croit_backend_is_unchanged(tmp_path, s3_endpoint):
    CURRENT["name"] = "croit"
    e = Env(tmp_path, s3_endpoint)
    try:
        r = delete(e, "alice-data", "alice-data")
        assert r.status_code == 200 and r.json() == {"name": "alice-data"}
        assert e.db.query("SELECT * FROM bucket_delete_jobs") == []
    finally:
        e.db.close()
