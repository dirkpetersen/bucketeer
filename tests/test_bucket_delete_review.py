# ruff: noqa: F401, F811  (pytest fixtures imported from test_bucket_delete_job)
"""Bucket delete jobs: ownership changes, endless pages and a retry race (A4 review fixes)."""

import asyncio

from bucketeer import bucket_delete

from .test_bucket_delete_job import (  # noqa: F401  (fixtures)
    delete,
    env,
    make,
    retry,
    rgw_backend,
    run,
    small_batches,
    versions,
)
from .test_buckets import BOB_KEY


def held_bucket(env, name="x-shared"):
    s3 = make(env, name, lock=True)
    s3.put_object(Bucket=name, Key="held", Body=b"1", ObjectLockLegalHoldStatus="ON")
    job = delete(env, name, name).json()["job"]
    assert run(env, job["id"])["state"] == "blocked"
    return s3, job


def test_retry_after_the_bucket_changed_hands_touches_nothing(env):
    s3, job = held_bucket(env)
    env.croit_state.buckets["x-shared"].owner = "bob"
    s3.put_object_legal_hold(Bucket="x-shared", Key="held", LegalHold={"Status": "OFF"})
    s3.put_object(Bucket="x-shared", Key="bobs-data", Body=b"precious")
    assert retry(env, job["id"]).status_code == 200
    env.used.clear()
    j = run(env, job["id"])
    assert j["state"] == "failed" and "belongs to someone else" in j["last_error"]
    assert env.used == []  # no S3 call with alice's key
    assert versions(s3, "x-shared") == 2 and "x-shared" in env.moto_buckets()


def test_other_owners_confirmed_delete_does_not_requeue_the_old_job(env):
    s3, job = held_bucket(env)
    env.croit_state.buckets["x-shared"].owner = "bob"
    r = delete(env, "x-shared", "x-shared", env.bob)
    assert r.status_code == 202 and r.json()["job"]["id"] != job["id"]
    old = env.db.delete_jobs.get(job["id"])
    assert old["state"] == "failed" and old["owner_uid"] == "alice"
    new = env.db.delete_jobs.get(r.json()["job"]["id"])
    assert new["owner_uid"] == "bob"
    assert retry(env, job["id"], env.bob).status_code == 404  # still alice's row
    assert [j["id"] for j in env.bob.get("/api/buckets/delete-jobs").json()["jobs"]] == [new["id"]]
    s3.put_object_legal_hold(Bucket="x-shared", Key="held", LegalHold={"Status": "OFF"})
    env.used.clear()
    assert run(env, new["id"])["state"] == "done"
    assert set(env.used) == {BOB_KEY}


def test_owner_change_before_the_last_step_stops_before_delete_bucket(env, monkeypatch):
    s3 = make(env, "alice-big")
    s3.put_object(Bucket="alice-big", Key="k", Body=b"1")
    job = delete(env, "alice-big", "alice-big").json()["job"]
    real_check = bucket_delete._check_owner
    calls = []

    async def check(app, j):
        calls.append(1)
        if len(calls) == 2:  # the check right before DeleteBucket
            env.croit_state.buckets["alice-big"].owner = "bob"
        await real_check(app, j)

    monkeypatch.setattr(bucket_delete, "_check_owner", check)
    j = run(env, job["id"])
    assert j["state"] == "failed" and "someone else" in j["last_error"]
    assert "alice-big" in env.moto_buckets()


def test_finish_leaves_other_owners_records_alone(env):
    env.croit_state.create_bucket("alice-big", "alice")
    env.db.buckets.create("alice-big", "bob", "WT-1", "bob", True)
    key = env.db.bucket_keys.create("bob+k", "bob", "alice-big", "read", "k", "active")
    env.db.shares.create("alice-big", "bob", "Bob", "carol", "carol", "C", "read", "bob")
    job = env.db.delete_jobs.create("alice-big", "alice", "alice", 1, 1)
    asyncio.run(bucket_delete._finish(env.app, job["id"]))
    assert env.db.buckets.get("alice-big")["owner_uid"] == "bob"
    assert env.db.bucket_keys.get(key["id"])["revoked_at"] is None
    assert len(env.db.shares.list_live("alice-big")) == 1


def test_a_page_of_only_held_versions_ends_blocked_in_bounded_time(env, monkeypatch):
    s3 = make(env, "alice-w", lock=True)
    for _ in range(12):
        s3.put_object(Bucket="alice-w", Key="h", Body=b"0", ObjectLockLegalHoldStatus="ON")
    for i in range(7):
        s3.put_object(Bucket="alice-w", Key=f"z{i}", Body=b"1")
    calls = []
    real = bucket_delete._batch

    def counting(client, bucket, marker):
        calls.append(marker)
        assert len(calls) < 40, "endless loop"
        return real(client, bucket, marker)

    monkeypatch.setattr(bucket_delete, "_batch", counting)
    job = delete(env, "alice-w", "alice-w").json()["job"]
    j = run(env, job["id"])
    assert j["state"] == "blocked" and j["remaining_objects"] == 12
    assert j["deleted_objects"] == 7  # every unlocked version went, also behind the held ones
    assert versions(s3, "alice-w") == 12


def test_only_held_versions_and_nothing_else(env):
    s3 = make(env, "alice-w", lock=True)
    for _ in range(11):
        s3.put_object(Bucket="alice-w", Key="h", Body=b"0", ObjectLockLegalHoldStatus="ON")
    job = delete(env, "alice-w", "alice-w").json()["job"]
    j = run(env, job["id"])
    assert j["state"] == "blocked" and j["remaining_objects"] == 11


def test_a_new_task_starts_once_the_old_one_is_forgotten(env, monkeypatch):
    s3, job = held_bucket(env)
    s3.put_object_legal_hold(Bucket="x-shared", Key="held", LegalHold={"Status": "OFF"})
    monkeypatch.setattr(bucket_delete, "AUTOSTART", True)

    async def go():
        env.db.delete_jobs.requeue(job["id"])
        bucket_delete.start(env.app, job["id"])
        tasks = bucket_delete._state(env.app).tasks
        await tasks[job["id"]]
        assert job["id"] not in tasks

    asyncio.run(go())
    assert env.db.delete_jobs.get(job["id"])["state"] == "done"
