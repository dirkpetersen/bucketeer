"""Cloud (AWS) buckets, P6-10: names and availability, create (security settings, day-0
storage class rule, owner policy, tags, SQLite row) and rollback, the bucket-policy retry while
IAM propagates a new user, list with CloudWatch sizes and costs, quota (weekly limit shared with
on premises), budget, delete (typed name), the over-quota email. moto `mock_aws`, no network."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from bucketeer import bucket_quota, cloud, cloud_buckets
from bucketeer.cloud import CloudAdmin, CloudError

from .test_admin import Env, H
from .test_auth import make_settings
from .test_cloud_users import BOUNDARY_DOC, CLOUD, boundary_session, iam

B = "/api/cloud/buckets"
TIB = 2**40
NEW = {"name": "scans", "storageClass": "intelligent", "quotaTiB": 2,
       "budgetNumber": "DN099999"}


class Mailer:
    enabled = True

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def send_over_quota(self, to, bucket, size_tib, quota_tib, as_of):
        self.calls.append((to, bucket, quota_tib))
        return not self.fail


def s3():
    return boto3.client("s3", region_name="us-west-2", aws_access_key_id="x",
                        aws_secret_access_key="y")


def cw():
    return boto3.client("cloudwatch", region_name="us-west-2", aws_access_key_id="x",
                        aws_secret_access_key="y")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "GRANT_WAITS", ())
    with mock_aws():
        e = Env(tmp_path)
        e.iam = iam()
        e.iam.create_policy(PolicyName="BucketeerUserBoundary", Path="/bucketeer/",
                            PolicyDocument=json.dumps(BOUNDARY_DOC))
        e.cloud = CloudAdmin(make_settings(tmp_path, **CLOUD),
                             session_factory=boundary_session({}))
        asyncio.run(e.cloud.check())
        e.app.state.cloud = e.cloud
        e.db.settings.set("location", {"default": "cloud"}, "test")
        e.s3 = s3()
        yield e
        e.db.close()


def acts(env, name):
    return [a for a in env.db.audit.query(limit=1000) if a["action"] == name]


def size(bucket, nbytes, storage="IntelligentTieringFAStorage", when=None):
    cw().put_metric_data(Namespace="AWS/S3", MetricData=[{
        "MetricName": "BucketSizeBytes", "Value": float(nbytes), "Unit": "Bytes",
        "Timestamp": when or datetime.now(UTC) - timedelta(hours=30),
        "Dimensions": [{"Name": "BucketName", "Value": bucket},
                       {"Name": "StorageType", "Value": storage}]}])


# -- names ----------------------------------------------------------------------------


def test_names_and_system_words(env):
    c = env.cloud
    assert c.bucket_name("Alice", "scans") == "bucketeer-alice-scans"
    assert c.bucket_name("j.doe_x", "a") == "bucketeer-j-doe-x-a"
    for bad in ("backup", "billing", "backup-x", "Billing.Team"):
        with pytest.raises(CloudError) as e:
            c.owner_part(bad)
        assert e.value.code == "reserved"
    assert c.owner_part("backupx") == "backupx"
    assert {"backup", "billing"} <= cloud.reserved_usernames()
    p = len("bucketeer-alice-")
    assert cloud_buckets.name_problem("scans", p) == ""
    for bad in ("", "Scans", "a.b", "-a", "a-", "a_b", "x" * (64 - p), "data-s3alias"):
        assert cloud_buckets.name_problem(bad, p), bad


def test_gating(env):
    assert env.anon.get(B).status_code == 401
    assert env.user.post(B, json=NEW).status_code == 403  # CSRF
    env.cloud.last_check = {**env.cloud.last_check, "ok": False}
    assert env.user.get(B).status_code == 503
    assert env.user.post(B, json=NEW, headers=H).status_code == 503


def test_create_needs_the_cloud_location(env):
    env.db.settings.set("location", {"default": "onprem"}, "test")
    r = env.user.post(B, json=NEW, headers=H)
    assert r.status_code == 409 and "Cloud" in r.json()["detail"]
    assert env.s3.list_buckets()["Buckets"] == []


# -- availability ---------------------------------------------------------------------


def test_check_free_taken_and_suggestions(env):
    r = env.user.get(f"{B}/check", params={"name": "scans"})
    assert r.json() | {"suggestions": []} == {
        "name": "scans", "fullName": "bucketeer-alice-scans", "available": True,
        "status": "free", "message": "This name is available.", "suggestions": []}
    env.s3.create_bucket(Bucket="bucketeer-alice-scans",
                         CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    env.s3.create_bucket(Bucket="bucketeer-alice-scans-2",
                         CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    body = env.user.get(f"{B}/check", params={"name": "scans"}).json()
    assert body["available"] is False and body["status"] == "exists"
    names = [s["name"] for s in body["suggestions"]]
    year = datetime.now(UTC).year
    assert "scans-2" not in names and f"scans-{year}" in names and len(names) == 2
    assert all(s["fullName"] == "bucketeer-alice-" + s["name"] for s in body["suggestions"])
    bad = env.user.get(f"{B}/check", params={"name": "Bad.Name"}).json()
    assert bad["status"] == "invalid" and not bad["available"]


@pytest.mark.parametrize(("status", "code", "want"), [
    (403, "403", "taken"), (301, "301", "elsewhere"), (404, "404", "free")])
def test_head_bucket_statuses(env, monkeypatch, status, code, want):
    async def head(service, op, **kw):
        raise CloudError(status, code, "x")
    monkeypatch.setattr(env.cloud, "call", head)
    assert asyncio.run(env.cloud.bucket_status("bucketeer-x-y")) == want


# -- create ---------------------------------------------------------------------------


def test_create_sets_everything_up(env):
    r = env.user.post(B, json=NEW, headers=H)
    assert r.status_code == 201, r.text
    body = r.json()
    full = "bucketeer-alice-scans"
    assert body["fullName"] == full and body["quotaTiB"] == 2
    assert body["product"]["name"] == "Cloud-S3" and body["monthlyCost"] == 36
    pab = env.s3.get_public_access_block(Bucket=full)["PublicAccessBlockConfiguration"]
    assert all(pab.values())
    enc = env.s3.get_bucket_encryption(Bucket=full)["ServerSideEncryptionConfiguration"]
    assert enc["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    own = env.s3.get_bucket_ownership_controls(Bucket=full)["OwnershipControls"]
    assert own["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"
    assert env.s3.get_bucket_versioning(Bucket=full)["Status"] == "Enabled"
    rules = {x["ID"]: x for x in env.s3.get_bucket_lifecycle_configuration(Bucket=full)["Rules"]}
    assert rules["bucketeer-class"]["Transitions"] == [
        {"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}]
    assert rules["bucketeer-abort-multipart"]["AbortIncompleteMultipartUpload"] == {
        "DaysAfterInitiation": 7}
    assert rules["bucketeer-expire-noncurrent"]["NoncurrentVersionExpiration"][
        "NoncurrentDays"] == 30
    [stmt] = json.loads(env.s3.get_bucket_policy(Bucket=full)["Policy"])["Statement"]
    account = env.iam.get_user(UserName="s3-alice")["User"]["Arn"].split(":")[4]
    assert stmt["Sid"] == "bucketeer-owner"
    assert stmt["Principal"] == {"AWS": f"arn:aws:iam::{account}:user/bucketeer/s3-alice"}
    tags = {t["Key"]: t["Value"] for t in env.s3.get_bucket_tagging(Bucket=full)["TagSet"]}
    assert tags["bucketeer-owner"] == "alice" and tags["bucketeer-location"] == "cloud"
    assert tags["bucketeer-product"] == "Cloud-S3" and tags["bucketeer-quota"] == "2"
    assert tags["bucketeer-budget"] == "DN099999" and tags["bucketeer-bucket"] == full
    assert tags["bucketeer-owner-email"] == "alice@example.org"
    assert tags["bucketeer-creator"] == "alice" and len(tags["bucketeer-created"]) == 10
    row = env.db.buckets.get(full)
    assert row["location"] == "cloud" and row["quota_tib"] == 2 and row["owner_uid"] == "alice"
    assert row["storage_class"] == "intelligent" and row["tag_synced"] == 1
    [a] = acts(env, "cloud.bucket.create")
    assert a["target"] == full and a["target_type"] == "bucket"
    # the same name again
    assert env.user.post(B, json=NEW, headers=H).status_code == 409


def test_deep_archive_without_versioning(env):
    r = env.user.post(B, json={**NEW, "storageClass": "deep_archive", "versioning": False},
                      headers=H)
    assert r.status_code == 201 and r.json()["product"]["name"] == "Archive-S3"
    full = "bucketeer-alice-scans"
    assert "Status" not in env.s3.get_bucket_versioning(Bucket=full)
    ids = {x["ID"]: x for x in env.s3.get_bucket_lifecycle_configuration(Bucket=full)["Rules"]}
    assert set(ids) == {"bucketeer-class", "bucketeer-abort-multipart"}
    assert ids["bucketeer-class"]["Transitions"][0]["StorageClass"] == "DEEP_ARCHIVE"


@pytest.mark.parametrize("step", ["policy", "tags"])
def test_failure_after_create_rolls_back(env, monkeypatch, step):
    async def boom(*a, **k):
        raise CloudError(500, "InternalError", "x")
    if step == "policy":
        monkeypatch.setattr(env.cloud, "grant_bucket_owner", boom)
    else:
        monkeypatch.setattr(cloud_buckets, "write_tags", boom)
    r = env.user.post(B, json=NEW, headers=H)
    assert r.status_code == 502 and "removed again" in r.json()["detail"]
    assert env.s3.list_buckets()["Buckets"] == []
    assert env.db.buckets.get("bucketeer-alice-scans") is None
    [a] = acts(env, "cloud.bucket.create_failed")
    assert a["details"]["step"] == step and a["details"]["rolledBack"] is True
    assert acts(env, "cloud.bucket.create") == []
    assert env.db.quota_changes.list() == []  # nothing counted against the week


def test_create_denied_by_the_admin_policy(env, monkeypatch, caplog):
    # Real AWS when tools/aws-setup ran with another --prefix than CLOUD_BUCKET_PREFIX.
    async def denied(full):
        raise CloudError(403, "AccessDenied", "AWS refused create_bucket (AccessDenied)")
    monkeypatch.setattr(env.cloud, "create_bucket", denied)
    with caplog.at_level("WARNING", logger="bucketeer.cloud_buckets"):
        r = env.user.post(B, json=NEW, headers=H)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "AccessDenied" in detail and "won't help" in detail and "Try again later" not in detail
    assert "--prefix bucketeer-" in caplog.text and "bucketeer-alice-scans" in caplog.text
    assert acts(env, "cloud.bucket.create_failed") == []  # a refusal changed nothing
    assert env.db.buckets.get("bucketeer-alice-scans") is None


def test_grant_retries_while_the_principal_propagates(env, monkeypatch):
    slept, calls = [], []
    monkeypatch.setattr(cloud, "GRANT_WAITS", (1.0, 2.0, 4.0))

    async def sleep(s):
        slept.append(s)
    monkeypatch.setattr(cloud, "_sleep", sleep)

    async def write(bucket, doc):
        calls.append(bucket)
        if len(calls) <= 2:
            raise CloudError(400, "MalformedPolicy", "x", "Invalid principal in policy")
    monkeypatch.setattr(env.cloud, "_write_bucket_policy", write)

    async def nopolicy(bucket):
        return {"Version": "2012-10-17", "Statement": []}
    monkeypatch.setattr(env.cloud, "_bucket_policy", nopolicy)
    asyncio.run(env.cloud.grant_bucket_owner("bucketeer-a-b", "arn:aws:iam::1:user/x"))
    assert len(calls) == 3 and slept == [1.0, 2.0]

    # another MalformedPolicy fails at once; an endless "Invalid principal" gives up
    calls.clear()

    async def other(bucket, doc):
        calls.append(bucket)
        raise CloudError(400, "MalformedPolicy", "x", "Policy has invalid action")
    monkeypatch.setattr(env.cloud, "_write_bucket_policy", other)
    with pytest.raises(CloudError):
        asyncio.run(env.cloud.grant_bucket_owner("bucketeer-a-b", "arn:aws:iam::1:user/x"))
    assert len(calls) == 1

    async def never(bucket, doc):
        calls.append(bucket)
        raise CloudError(400, "MalformedPolicy", "x", "Invalid principal in policy")
    calls.clear()
    monkeypatch.setattr(env.cloud, "_write_bucket_policy", never)
    with pytest.raises(CloudError):
        asyncio.run(env.cloud.grant_bucket_owner("bucketeer-a-b", "arn:aws:iam::1:user/x"))
    assert len(calls) == 4


def test_cloud_create_refuses_a_name_used_on_premises(env):
    env.db.buckets.create("bucketeer-alice-scans", "bob", "DN099999")
    assert env.user.get(f"{B}/check", params={"name": "scans"}).json()["status"] == "exists"
    r = env.user.post(B, json=NEW, headers=H)
    assert r.status_code == 409
    assert env.s3.list_buckets()["Buckets"] == []
    assert env.db.buckets.get("bucketeer-alice-scans")["location"] == "onprem"


def test_onprem_names_may_not_use_the_cloud_prefix(env, tmp_path):
    env.app.state.settings = make_settings(tmp_path, **CLOUD)
    hint = env.user.get("/api/buckets/options").json()["naming"]["hint"]
    assert '"bucketeer-"' in hint
    r = env.user.post("/api/buckets", json={"name": "bucketeer-alice-x", "budgetNumber":
                                            "DN099999"}, headers=H)
    assert r.status_code == 422 and "reserved for cloud" in r.json()["detail"]


def test_onprem_paths_leave_cloud_rows_alone(env):
    assert env.user.post(B, json=NEW, headers=H).status_code == 201
    full = "bucketeer-alice-scans"
    env.db.buckets.set_tag_synced(full, False)
    assert full not in [b["name"] for b in env.db.buckets.list("alice")]
    assert env.user.post("/api/buckets/reconcile", headers=H).json()["synced"] == []
    r = env.user.put("/api/buckets/bucketeer-alice-scans/budget",
                     json={"budgetNumber": "DN012345"}, headers=H)
    assert r.status_code == 404
    row = env.db.buckets.get(full)
    assert row["location"] == "cloud" and row["budget_number"] == "DN099999"
    # an older on-prem bucket that shares its name with a cloud row: a clear 409, no 500
    env.db.buckets.create_cloud("alice-data", "carl", "DN099999", "carl", "intelligent", 1)
    r = env.user.put("/api/buckets/alice-data/budget", json={"budgetNumber": "DN012345"},
                     headers=H)
    assert r.status_code == 409 and "cloud bucket" in r.json()["detail"]
    assert env.db.buckets.get("alice-data")["budget_number"] == "DN099999"


# -- list -----------------------------------------------------------------------------


def test_list_with_sizes_and_costs(env):
    assert env.user.post(B, json=NEW, headers=H).status_code == 201
    assert env.user.post(B, json={**NEW, "name": "cold", "storageClass": "deep_archive"},
                         headers=H).status_code == 201
    size("bucketeer-alice-scans", TIB, "IntelligentTieringFAStorage")
    size("bucketeer-alice-scans", TIB // 2, "IntelligentTieringIAStorage")
    body = env.user.get(B).json()
    assert body["region"] == "us-west-2"
    by = {b["name"]: b for b in body["buckets"]}
    assert set(by) == {"scans", "cold"}
    scans = by["scans"]
    assert scans["bytes"] == TIB + TIB // 2 and scans["sizeAsOf"]
    assert scans["estimatedCost"] == 27.0 and scans["monthlyCost"] == 36
    assert scans["overQuota"] is False and scans["storageClass"] == "intelligent"
    assert not by["cold"]["bytes"] and by["cold"]["product"]["name"] == "Archive-S3"
    # the on-premises list does not show cloud buckets, nor bob's list alice's
    assert all(not b["name"].startswith("bucketeer-") for b in env.user.get(
        "/api/buckets").json()["buckets"])
    assert env.admin.get(B).json()["buckets"] == []


# -- quota and budget -----------------------------------------------------------------


def test_quota_change_and_weekly_limit_shared_with_onprem(env):
    asyncio.run(bucket_quota.record(env.app, username="alice", uid="alice", full="alice-data",
                                    old=1, new=97, actor="alice"))  # on premises: 96 TiB
    r = env.user.post(B, json={**NEW, "quotaTiB": 5}, headers=H)
    assert r.status_code == 409 and r.json()["remainingTiB"] == 4
    assert env.s3.list_buckets()["Buckets"] == []
    assert env.user.post(B, json=NEW, headers=H).status_code == 201  # 2 TiB fits
    r = env.user.put(f"{B}/scans/quota", json={"quotaTiB": 5}, headers=H)
    assert r.status_code == 409 and r.json()["remainingTiB"] == 2
    r = env.user.put(f"{B}/scans/quota", json={"quotaTiB": 4}, headers=H)
    assert r.status_code == 200 and r.json()["changed"] is True and r.json()["tagSynced"]
    tags = {t["Key"]: t["Value"] for t in env.s3.get_bucket_tagging(
        Bucket="bucketeer-alice-scans")["TagSet"]}
    assert tags["bucketeer-quota"] == "4"
    assert env.db.buckets.get("bucketeer-alice-scans")["quota_tib"] == 4
    # decreases always pass
    assert env.user.put(f"{B}/scans/quota", json={"quotaTiB": 1}, headers=H).status_code == 200
    [a, _] = acts(env, "cloud.bucket.quota")
    assert a["details"]["newTiB"] == 1
    assert env.user.put(f"{B}/nope/quota", json={"quotaTiB": 1}, headers=H).status_code == 404


def test_budget_change(env):
    assert env.user.post(B, json=NEW, headers=H).status_code == 201
    r = env.user.put(f"{B}/scans/budget", json={"budgetNumber": "DN012345"}, headers=H)
    assert r.status_code == 200 and r.json()["changed"] is True
    tags = {t["Key"]: t["Value"] for t in env.s3.get_bucket_tagging(
        Bucket="bucketeer-alice-scans")["TagSet"]}
    assert tags["bucketeer-budget"] == "DN012345"
    assert env.user.put(f"{B}/scans/budget", json={"budgetNumber": None},
                        headers=H).status_code == 422  # only admins may clear it
    [a] = acts(env, "cloud.bucket.budget")
    assert a["details"] == {"uid": "alice", "old": "DN099999", "new": "DN012345",
                            "tag_synced": True}


# -- delete ---------------------------------------------------------------------------


def test_delete_needs_the_typed_name(env):
    # owner 2026-11-25; the content flows are in tests/test_cloud_delete.py
    assert env.user.post(B, json=NEW, headers=H).status_code == 201
    full = "bucketeer-alice-scans"
    r = env.user.delete(f"{B}/scans", headers=H)
    assert r.status_code == 409 and r.json()["requiresConfirmation"] is True
    assert r.json()["objects"] == 0 and env.db.buckets.get(full) is not None
    r = env.user.request("DELETE", f"{B}/scans", headers=H, json={"confirmName": "scans"})
    assert r.status_code == 200
    assert env.s3.list_buckets()["Buckets"] == [] and env.db.buckets.get(full) is None
    [a] = acts(env, "cloud.bucket.delete")
    assert a["target"] == full
    assert env.user.request("DELETE", f"{B}/scans", headers=H,
                            json={"confirmName": "scans"}).status_code == 404


# -- over-quota alert -----------------------------------------------------------------


def test_over_quota_alert_once_a_week(env):
    assert env.user.post(B, json={**NEW, "quotaTiB": 1}, headers=H).status_code == 201
    full = "bucketeer-alice-scans"
    mail = env.app.state.mailer = Mailer()
    now = datetime.now(UTC)
    size(full, TIB // 2)
    assert asyncio.run(cloud_buckets.alert_tick(env.app, now))["over"] == 0
    size(full, 2 * TIB, when=now - timedelta(hours=6))
    assert asyncio.run(cloud_buckets.alert_tick(env.app, now))["alerted"] == 1
    assert mail.calls == [("alice@example.org", full, 1)]
    asyncio.run(cloud_buckets.alert_tick(env.app, now + timedelta(days=1)))
    assert len(mail.calls) == 1  # at most once per 7 days
    asyncio.run(cloud_buckets.alert_tick(env.app, now + timedelta(days=8)))
    assert len(mail.calls) == 2
    [a, _] = acts(env, "cloud.bucket.over_quota")
    assert a["actor"] == "system" and a["details"]["emailed"] is True


def test_failed_alert_is_retried_next_day(env):
    assert env.user.post(B, json={**NEW, "quotaTiB": 1}, headers=H).status_code == 201
    mail = env.app.state.mailer = Mailer(fail=True)
    size("bucketeer-alice-scans", 2 * TIB)
    now = datetime.now(UTC)
    asyncio.run(cloud_buckets.alert_tick(env.app, now))
    mail.fail = False
    asyncio.run(cloud_buckets.alert_tick(env.app, now + timedelta(days=1)))
    assert len(mail.calls) == 2
    assert env.db.buckets.get("bucketeer-alice-scans")["over_quota_alert_at"]


# -- frontend -------------------------------------------------------------------------


def test_cloud_ui_files_follow_the_csp_rules():
    root = Path(__file__).resolve().parent.parent / "static" / "js"
    for rel in ("views/cloud-buckets.js", "components/cloud-bucket-dialog.js"):
        text = (root / rel).read_text()
        assert "innerHTML" not in text and "fetch(" not in text and "http" not in text
        assert 'from "../api.js"' in text and 'from "../dom.js"' in text
    buckets = (root / "views" / "buckets.js").read_text()
    assert "cloudBucketsCard" in buckets and "getLocation" in buckets
    api = (root / "api.js").read_text()
    for path in ('"/cloud/buckets"', "/cloud/buckets/check?name=", "/cloud/buckets/options"):
        assert path in api
