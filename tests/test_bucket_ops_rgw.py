"""Bucket create and versioning on the S3 data plane (ADMIN_BACKEND=rgw, Phase 5 A3).

The user's own key makes the bucket (`bucketeer/bucket_ops.py`); the Admin Ops mock and the
mock croit see it through the moto mirror (`mock/s3_bridge.py`). Every test wraps the S3
client the app builds so the exact calls and the key used can be asserted.
"""

import asyncio

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from .backends import admin_backend, rgw_only  # noqa: F401
from .test_buckets import ALICE_KEY, Env, H, create
from .test_versioning import put


class Recorder:
    """The real client with every call recorded; `fail[method]` is raised instead (before
    the real call), `after[method]` after the real call succeeded."""

    def __init__(self, real, log, fail, after):
        self._real, self.log, self.fail, self.after = real, log, fail, after

    def __getattr__(self, name):
        target = getattr(self._real, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self.log.append((name, kwargs))
            if name in self.fail:
                raise self.fail[name]
            out = target(*args, **kwargs)
            if name in self.after:
                raise self.after[name]
            return out

        return call


def client_error(code, status=400, op="CreateBucket"):
    return ClientError({"Error": {"Code": code, "Message": "x"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, op)


@pytest.fixture
def env(tmp_path, s3_endpoint):
    rgw_only("S3 data-plane create and versioning exist only with ADMIN_BACKEND=rgw")
    e = Env(tmp_path, s3_endpoint)
    e.log, e.fail, e.after = [], {}, {}
    plain = e.app.state.s3.factory
    e.app.state.s3.factory = lambda *a: Recorder(plain(*a), e.log, e.fail, e.after)
    e.calls = lambda name: [kw for n, kw in e.log if n == name]
    yield e
    e.db.close()


def listed(env, name):
    return next(b for b in env.alice.get("/api/buckets").json()["buckets"] if b["name"] == name)


def test_create_uses_the_users_own_key_and_no_admin_call(env):
    r = create(env.alice)
    assert r.status_code == 201 and r.json()["versioning"] == "enabled"
    assert set(env.used) == {ALICE_KEY}
    assert env.mutations() == [] and env.mutations("/s3/") == []
    assert [m for m in env.transport.raw_calls if m[0] == "PUT" and "bucket" in m[1]] == []
    # the mirror made it visible to the admin plane, with the creating key's owner
    assert env.croit_state.buckets["alice-new"].owner == "alice"
    assert env.croit_state.buckets["alice-new"].versioning == "ENABLED"
    assert listed(env, "alice-new")["versioning"] == "enabled"
    (kw,) = env.calls("create_bucket")
    assert kw == {"Bucket": "alice-new"}  # default placement: no LocationConstraint
    assert env.calls("put_bucket_versioning") == [
        {"Bucket": "alice-new", "VersioningConfiguration": {"Status": "Enabled"}}]


def test_default_placement_is_sent_with_an_empty_api_name(env):
    env.db.settings.set("placement", {"default": "nvme-placement", "allowed": []})
    assert create(env.alice).status_code == 201
    (kw,) = env.calls("create_bucket")
    # not <S3_REGION>:<placement>: RGW compares the prefix with the zonegroup api name
    assert kw["CreateBucketConfiguration"] == {"LocationConstraint": ":nvme-placement"}
    assert env.croit_state.buckets["alice-new"].placement == "nvme-placement"
    assert env.audit("bucket.create")[0]["details"]["placement"] == "nvme-placement"


def test_the_users_placement_choice_reaches_the_location_constraint(env):
    env.db.settings.set("placement", {"default": "default-placement",
                                      "allowed": ["nvme-placement"]})
    assert env.alice.get("/api/buckets/options").json()["placements"] == [
        "default-placement", "nvme-placement"]
    r = create(env.alice, placement="nvme-placement")
    assert r.status_code == 201
    (kw,) = env.calls("create_bucket")
    assert kw["CreateBucketConfiguration"] == {"LocationConstraint": ":nvme-placement"}
    assert env.croit_state.buckets["alice-new"].placement == "nvme-placement"


def test_mock_rgw_rejects_a_wrong_api_name_and_unknown_placements(env):
    s3 = boto3.client("s3", endpoint_url=env.bridge.endpoint, region_name="us-east-1",
                      aws_access_key_id=ALICE_KEY, aws_secret_access_key="x")
    for location in ("us-east-1:nvme-placement", ":mystery", "default:nope"):
        with pytest.raises(ClientError) as e:
            s3.create_bucket(Bucket="alice-loc",
                             CreateBucketConfiguration={"LocationConstraint": location})
        assert e.value.response["Error"]["Code"] == "InvalidLocationConstraint"
    s3.create_bucket(Bucket="alice-loc", CreateBucketConfiguration={
        "LocationConstraint": "default:nvme-placement"})
    assert env.croit_state.buckets["alice-loc"].placement == "nvme-placement"


def test_the_mirror_forgets_a_bucket_the_owner_deletes_over_s3(env):
    """No test-side shim: the bridge's middleware removes the bucket from the shared state
    (what the demo relies on), and only for its owner's key."""
    s3 = boto3.client("s3", endpoint_url=env.bridge.endpoint, region_name="us-east-1",
                      aws_access_key_id=ALICE_KEY, aws_secret_access_key="x")
    s3.create_bucket(Bucket="alice-gone")
    assert "alice-gone" in env.croit_state.buckets
    s3.delete_bucket(Bucket="alice-gone")
    assert "alice-gone" not in env.croit_state.buckets


def test_unversioned_create_makes_no_versioning_call(env):
    assert create(env.alice, versioning=False).status_code == 201
    assert env.calls("put_bucket_versioning") == []
    assert listed(env, "alice-new")["versioning"] == "off"


def test_object_lock_is_set_at_create_and_implies_versioning(env):
    r = create(env.alice, name="alice-lock", objectLock=True)
    assert r.status_code == 201 and r.json()["objectLock"] is True
    assert r.json()["versioning"] == "enabled"
    (kw,) = env.calls("create_bucket")
    assert kw["ObjectLockEnabledForBucket"] is True
    assert env.calls("put_bucket_versioning") == []  # object lock enabled it already
    assert env.croit_state.buckets["alice-lock"].object_lock is True
    assert listed(env, "alice-lock")["objectLock"] is True
    assert listed(env, "alice-lock")["versioning"] == "enabled"


def test_list_reads_versioning_and_lock_from_the_stats_without_s3_calls(env):
    env.log.clear()
    rows = {b["name"]: b for b in env.alice.get("/api/buckets").json()["buckets"]}
    assert rows["alice-data"]["versioning"] == "off" and rows["alice-data"]["objectLock"] is False
    assert [n for n, _ in env.log if n in ("get_bucket_versioning",
                                           "get_object_lock_configuration")] == []


def test_bucket_already_owned_by_you_adopts_the_bucket(env):
    # the first attempt reached RGW but the answer was lost; the retry says "owned by you"
    env.after["create_bucket"] = client_error("BucketAlreadyOwnedByYou", 409)
    r = create(env.alice, budget="WT-77")
    assert r.status_code == 201 and r.json()["adopted"] is True and "note" in r.json()
    row = env.db.buckets.get("alice-new")
    assert row["budget_number"] == "WT-77" and row["owner_uid"] == "alice"
    assert env.audit("bucket.create")[0]["details"]["adopted"] is True
    assert env.audit("bucket.create_failed") == []
    assert env.calls("put_bucket_lifecycle_configuration") == []  # adopted: untouched
    assert env.calls("put_bucket_versioning") == []


def test_owned_by_you_without_a_matching_row_is_409_when_not_yours(env):
    env.fail["create_bucket"] = client_error("BucketAlreadyOwnedByYou", 409)
    r = create(env.alice, name="free-name")
    assert r.status_code == 409  # croit/RGW does not list it as the user's own
    assert env.db.buckets.get("free-name") is None


def test_bucket_already_exists_is_409_name_taken(env):
    env.fail["create_bucket"] = client_error("BucketAlreadyExists", 409)
    r = create(env.alice)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    assert env.db.buckets.get("alice-new") is None
    assert env.audit("bucket.") == []


@pytest.mark.parametrize("code,status,words", [
    ("InvalidBucketName", 400, "bucket name"),
    ("TooManyBuckets", 400, "maximum number"),
    ("InvalidLocationConstraint", 400, "placement"),
    ("AccessDenied", 403, "suspended"),
    ("SomethingNew", 400, "SomethingNew"),
])
def test_other_refusals_get_a_clear_message_and_no_audit(env, code, status, words):
    env.fail["create_bucket"] = client_error(code, status)
    r = create(env.alice)
    assert r.status_code == status and words in r.json()["detail"]
    assert "croit" not in r.json()["detail"].lower()
    assert env.db.buckets.get("alice-new") is None
    assert env.audit("bucket.") == []  # a definite refusal changed nothing


def test_server_error_is_audited_as_unknown_outcome(env):
    env.fail["create_bucket"] = client_error("InternalError", 500)
    r = create(env.alice)
    assert r.status_code == 502 and "storage system" in r.json()["detail"]
    (a,) = env.audit("bucket.create_failed")
    assert a["target"] == "alice-new" and a["details"]["error"] == "S3OpError"
    assert env.db.buckets.get("alice-new") is None


def test_timeout_is_audited_as_unknown_outcome(env):
    env.fail["create_bucket"] = ReadTimeoutError(endpoint_url="http://x")
    r = create(env.alice)
    assert r.status_code == 502 and "in time" in r.json()["detail"]
    assert len(env.audit("bucket.create_failed")) == 1


def test_refused_versioning_step_keeps_the_bucket_and_says_off(env):
    env.fail["put_bucket_versioning"] = client_error("AccessDenied", 403, "PutBucketVersioning")
    r = create(env.alice)
    assert r.status_code == 201 and r.json()["versioning"] == "off"
    assert "could not be turned on" in r.json()["warning"]
    assert env.calls("put_bucket_lifecycle_configuration") == []  # no rule without versioning
    d = env.audit("bucket.create")[0]["details"]
    assert d["versioning"] is False and d["noncurrentRuleApplied"] is None
    assert d["versioningApplied"] is False


@pytest.mark.parametrize("failure", [client_error("InternalError", 500, "PutBucketVersioning"),
                                     ReadTimeoutError(endpoint_url="http://x")])
def test_unknown_versioning_step_is_reported_as_unknown(env, failure):
    env.fail["put_bucket_versioning"] = failure
    r = create(env.alice)
    assert r.status_code == 201 and r.json()["versioning"] is None
    assert "could not tell" in r.json()["warning"]
    d = env.audit("bucket.create")[0]["details"]
    assert d["versioningApplied"] is None and d["noncurrentRuleApplied"] is None
    assert env.calls("put_bucket_lifecycle_configuration") == []


def test_reef_answers_200_to_a_repeat_create_of_an_owned_bucket(env):
    # A recorded, paused bucket that the admin listing misses (a Reef tenant bucket, say):
    # CreateBucket would answer 200, so HeadBucket must stop it before anything is touched.
    assert create(env.alice, name="alice-keep", budget="WT-1").status_code == 201
    assert put(env.alice, "alice-keep", False).json()["versioning"] == "suspended"
    row = env.db.buckets.get("alice-keep")
    real = env.app.state.croit.list_buckets

    async def stale():
        return [b for b in await real() if b["bucket"] != "alice-keep"]

    env.app.state.croit.list_buckets = stale
    env.log.clear()
    r = create(env.alice, name="alice-keep", budget="WT-2", objectLock=True)
    assert r.status_code == 409 and "already own" in r.json()["detail"]
    assert env.calls("create_bucket") == [] and env.calls("head_bucket")
    assert env.calls("put_bucket_versioning") == []
    assert env.calls("put_bucket_lifecycle_configuration") == []
    assert env.db.buckets.get("alice-keep") == row  # budget number kept
    assert env.moto().get_bucket_versioning(Bucket="alice-keep")["Status"] == "Suspended"
    assert len(env.audit("bucket.create")) == 1  # only the first, real, create


def test_head_bucket_403_and_404_go_on_to_create_but_other_errors_stop(env):
    env.fail["head_bucket"] = client_error("403", 403, "HeadBucket")
    assert create(env.alice, name="alice-a").status_code == 201
    env.fail["head_bucket"] = client_error("InternalError", 500, "HeadBucket")
    r = create(env.alice, name="alice-b")
    assert r.status_code == 502 and "alice-b" not in env.croit_state.buckets
    assert len(env.audit("bucket.create_failed")) == 1


def test_admin_lists_are_refreshed_after_s3_side_changes(env):
    croit = env.app.state.croit
    calls = []
    real = croit.invalidate_lists
    croit.invalidate_lists = lambda: (calls.append(1), real())[1]
    assert create(env.alice).status_code == 201
    n = len(calls)
    assert n >= 2  # before the ownership check and after the create
    assert put(env.alice, "alice-data", True).status_code == 200
    assert len(calls) > n
    env.fail["put_bucket_versioning"] = client_error("InternalError", 500, "PutBucketVersioning")
    n = len(calls)
    assert put(env.alice, "alice-data", False).status_code == 502
    assert len(calls) > n
    # and a cached admin listing really shows the new bucket
    names = {b["bucket"] for b in asyncio.run(croit.list_buckets())}
    assert "alice-new" in names


def test_create_still_checks_the_name_budget_and_placement_first(env):
    assert create(env.alice, name="Bad_Name").status_code == 422
    assert env.alice.post("/api/buckets", json={"name": "alice-new"}, headers=H).status_code == 422
    assert create(env.alice, placement="mystery").status_code == 422
    assert env.calls("create_bucket") == []


def test_existing_name_of_another_user_is_409_without_an_s3_call(env):
    r = create(env.alice, name="bob-shared")
    assert r.status_code == 409 and env.calls("create_bucket") == []


# -- versioning ---------------------------------------------------------------------


def test_versioning_toggle_uses_put_bucket_versioning_with_the_users_key(env):
    r = put(env.alice, "alice-data", True)
    assert r.status_code == 200 and r.json() == {
        "name": "alice-data", "versioning": "enabled", "changed": True}
    assert env.calls("put_bucket_versioning") == [
        {"Bucket": "alice-data", "VersioningConfiguration": {"Status": "Enabled"}}]
    assert set(env.used) == {ALICE_KEY}
    assert env.croit_state.buckets["alice-data"].versioning == "ENABLED"
    r = put(env.alice, "alice-data", False)
    assert r.json()["versioning"] == "suspended"
    assert env.calls("put_bucket_versioning")[-1]["VersioningConfiguration"] == {
        "Status": "Suspended"}
    assert [a["details"]["after"] for a in reversed(env.audit("bucket.versioning"))] == [
        "enabled", "suspended"]


def test_versioning_is_idempotent_and_off_stays_off(env):
    assert put(env.alice, "alice-data", False).json()["changed"] is False
    assert put(env.alice, "alice-data", True).json()["changed"] is True
    n = len(env.calls("put_bucket_versioning"))
    assert put(env.alice, "alice-data", True).json()["changed"] is False
    assert len(env.calls("put_bucket_versioning")) == n == 1


def test_object_lock_bucket_cannot_be_suspended(env):
    assert create(env.alice, name="alice-lock", objectLock=True).status_code == 201
    r = put(env.alice, "alice-lock", False)
    assert r.status_code == 409 and "object lock" in r.json()["detail"]
    assert env.calls("put_bucket_versioning") == []
    assert env.audit("bucket.versioning") == []


def test_versioning_on_another_users_bucket_is_404_and_never_reaches_s3(env):
    assert put(env.alice, "bob-shared", True).status_code == 404
    assert env.calls("put_bucket_versioning") == []


def test_versioning_server_error_is_audited_and_refusal_is_not(env):
    env.fail["put_bucket_versioning"] = client_error("InternalError", 500, "PutBucketVersioning")
    r = put(env.alice, "alice-data", True)
    assert r.status_code == 502
    (a,) = env.audit("bucket.versioning_failed")
    assert a["details"]["before"] == "off" and a["details"]["wanted"] == "enabled"
    env.fail["put_bucket_versioning"] = client_error("AccessDenied", 403, "PutBucketVersioning")
    r = put(env.alice, "alice-data", True)
    assert r.status_code == 403 and len(env.audit("bucket.versioning_failed")) == 1
    assert [a for a in env.audit() if a["action"] == "bucket.versioning"] == []
