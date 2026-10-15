# ruff: noqa: F401, F811  (pytest fixtures imported from test_bucket_delete_job)
"""Bucket delete jobs, review round 2: direct owner lookup, exact blocked message, scoped finish."""

import asyncio

import httpx
import pytest

from bucketeer import bucket_delete
from bucketeer.croit import CroitError, CroitUnavailable

from .test_bucket_delete_job import (
    delete,
    env,
    make,
    rgw_backend,
    run,
    small_batches,
)
from .test_bucket_delete_review import held_bucket
from .test_buckets import Env


def test_owner_lookup_is_direct_and_only_no_such_bucket_means_gone(env):
    croit = env.app.state.croit
    assert asyncio.run(croit.bucket_owner("alice-data")) == "alice"
    assert asyncio.run(croit.bucket_owner("acme:reports")) == "acme$carol"
    assert asyncio.run(croit.bucket_owner("nope-bucket")) is None
    env.transport.fail = lambda r: httpx.Response(500, json={"Code": "InternalError",
                                                            "Message": "boom"})
    with pytest.raises(CroitError):
        asyncio.run(croit.bucket_owner("alice-data"))


def test_tenant_bucket_of_someone_else_is_not_mistaken_for_gone(tmp_path, s3_endpoint,
                                                                monkeypatch):
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    try:
        s3 = make(e, "tbk", owner="acme$carol")
        s3.put_object(Bucket="tbk", Key="k", Body=b"1")
        carol = e.session("carol")
        job = delete(e, "tbk", "tbk", carol).json()["job"]
        e.croit_state.buckets["acme:tbk"].owner = "bob"

        async def empty():  # Reef: the global list leaves tenant buckets out
            return []

        monkeypatch.setattr(e.app.state.croit, "list_buckets", empty)
        e.used.clear()
        j = run(e, job["id"])
        assert j["state"] == "failed" and "someone else" in j["last_error"]
        assert e.used == [] and "tbk" in e.moto_buckets()
    finally:
        e.db.close()


def test_admin_api_trouble_during_the_owner_check_fails_for_retry(env, monkeypatch):
    job = delete(env, "alice-data", "alice-data").json()["job"]

    async def down(name):
        raise CroitUnavailable("down")

    monkeypatch.setattr(env.app.state.croit, "bucket_owner", down)
    env.used.clear()
    j = run(env, job["id"])
    assert j["state"] == "failed" and "could not be reached" in j["last_error"]
    assert env.used == [] and "alice-data" in env.moto_buckets()


def test_blocked_message_separates_locks_from_other_errors():
    info = {"locked": 2, "until": None, "hold": True}
    m = bucket_delete._blocked_message(3, 2, info, partial=False)
    assert "3 object versions are under object lock or legal hold" in m
    assert "2 more could not be removed because of storage errors" in m
    assert "All other versions were removed" in m
    assert "more could not" not in bucket_delete._blocked_message(1, 0, info, False)


def test_job_with_mixed_refusals_reports_exact_counts(env, monkeypatch):
    s3 = make(env, "alice-mix", lock=True)
    s3.put_object(Bucket="alice-mix", Key="held", Body=b"1", ObjectLockLegalHoldStatus="ON")
    s3.put_object(Bucket="alice-mix", Key="flaky", Body=b"1")
    job = delete(env, "alice-mix", "alice-mix").json()["job"]
    factory = env.real_factory

    def flaky_client(*a):
        client = factory(*a)
        orig = client.delete_objects

        def delete_objects(**kw):
            out = orig(**kw)
            for o in kw["Delete"]["Objects"]:
                if o["Key"] == "flaky":  # report a transient refusal for it
                    out.setdefault("Errors", []).append(
                        {"Key": "flaky", "VersionId": o["VersionId"],
                         "Code": "InternalError", "Message": "x"})
            return out

        client.delete_objects = delete_objects
        return client

    monkeypatch.setattr(env.app.state.s3, "factory", flaky_client)
    j = run(env, job["id"])
    assert j["state"] == "blocked" and j["remaining_objects"] == 2
    assert "1 object version is under object lock" in j["last_error"]
    assert "1 more could not be removed because of a storage error" in j["last_error"]


def test_finish_revokes_the_owners_rows_and_leaves_the_others(env):
    env.croit_state.create_bucket("alice-big", "alice")
    mine = env.db.bucket_keys.create("alice+k", "alice", "alice-big", "read", "k", "active")
    other = env.db.bucket_keys.create("bob+k", "bob", "alice-big", "read", "k2", "active")
    env.db.shares.create("alice-big", "alice", "A", "carol", "carol", "C", "read", "alice")
    env.db.shares.create("alice-big", "bob", "B", "dave", "dave", "D", "read", "bob")
    job = env.db.delete_jobs.create("alice-big", "alice", "alice", 1, 1)
    asyncio.run(bucket_delete._finish(env.app, job["id"]))
    assert env.db.bucket_keys.get(mine["id"])["revoked_at"] is not None
    assert env.db.bucket_keys.get(other["id"])["revoked_at"] is None
    assert [s["owner_uid"] for s in env.db.shares.list_live("alice-big")] == ["bob"]


def test_superseding_another_owners_job_is_audited(env):
    s3, job = held_bucket(env)
    env.croit_state.buckets["x-shared"].owner = "bob"
    delete(env, "x-shared", "x-shared", env.bob)
    (a,) = [a for a in env.audit("bucket.") if a["action"] == "bucket.delete_superseded"]
    assert a["actor"] == "bob" and a["details"]["job"] == job["id"]
    assert a["details"]["uid"] == "alice" and a["details"]["newOwner"] == "bob"
