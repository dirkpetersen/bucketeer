"""Cloud buckets in the admin views (Q19, P6-14): the admin Buckets list, the admin cloud quota
route, the chargeback CSV, the own-name availability message and the rebuild's cloud step.
moto `mock_aws`, no network."""

import asyncio
import csv
import io
import json

import pytest
from moto import mock_aws

from bucketeer import cloud, rebuild
from bucketeer.cloud import CloudAdmin

from .test_admin import Env, H
from .test_auth import make_settings
from .test_cloud_buckets import NEW, TIB, B, acts, s3, size
from .test_cloud_users import BOUNDARY_DOC, CLOUD, boundary_session, iam

A = "/api/admin/buckets"
Q = "/api/admin/cloud-buckets"
FULL = "bucketeer-alice-scans"


@pytest.fixture
def env(tmp_path, monkeypatch):  # as in tests/test_cloud_buckets.py
    monkeypatch.setattr(cloud, "GRANT_WAITS", ())
    with mock_aws():
        e = Env(tmp_path)
        iam().create_policy(PolicyName="BucketeerUserBoundary", Path="/bucketeer/",
                            PolicyDocument=json.dumps(BOUNDARY_DOC))
        e.cloud = CloudAdmin(make_settings(tmp_path, **CLOUD),
                             session_factory=boundary_session({}))
        asyncio.run(e.cloud.check())
        e.app.state.cloud = e.cloud
        e.db.settings.set("location", {"default": "cloud"}, "test")
        e.s3 = s3()
        yield e
        e.db.close()


def created(env, **extra):
    assert env.user.post(B, json={**NEW, **extra}, headers=H).status_code == 201


def tags(env, bucket):
    return {t["Key"]: t["Value"] for t in env.s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}


def test_admin_list_has_cloud_rows(env):
    created(env)
    size(FULL, TIB // 2)
    body = env.admin.get(A).json()
    assert body["cloud"] == {"configured": True, "available": True, "maxTiB": 100_000}
    by = {b["name"]: b for b in body["buckets"]}
    assert by["alice-data"]["location"] == "onprem"
    c = by[FULL]
    assert c["location"] == "cloud" and c["owner"] == "alice" and c["ownerAd"] == "alice"
    assert c["bytes"] == TIB // 2 and c["objects"] is None and c["sizeAsOf"]
    assert c["quotaTiB"] == 2 and c["quota"] == {"enabled": True, "maxSize": 2 * TIB,
                                                  "maxObjects": None}
    assert c["storageClass"] == "intelligent" and c["product"]["name"] == "Cloud-S3"
    assert c["budgetNumber"] == "DN099999" and c["budgetMatches"] is None
    assert c["overQuota"] is False and c["tagPending"] is False
    names = [b["name"] for b in body["buckets"]]
    assert names == sorted(names, key=str.lower)
    assert env.user.get(A).status_code == 403


def test_admin_list_without_cloud_still_lists_rows(env):
    created(env)
    env.app.state.cloud_sizes = {}
    env.cloud.last_check = {**env.cloud.last_check, "ok": False}
    body = env.admin.get(A).json()
    c = next(b for b in body["buckets"] if b["name"] == FULL)
    assert c["bytes"] is None and c["quotaTiB"] == 2
    assert body["cloud"]["available"] is False
    env.app.state.cloud = None
    c = next(b for b in env.admin.get(A).json()["buckets"] if b["name"] == FULL)
    assert c["location"] == "cloud" and c["bytes"] is None


def test_admin_cloud_quota(env):
    created(env)
    url = f"{Q}/{FULL}/quota"
    assert env.user.put(url, json={"quotaTiB": 5}, headers=H).status_code == 403
    assert env.admin.put(url, json={"quotaTiB": 5}).status_code == 403  # CSRF
    assert env.admin.put(url, json={"quotaTiB": 0}, headers=H).status_code == 422
    assert env.admin.put(f"{Q}/alice-data/quota", json={"quotaTiB": 5},
                         headers=H).status_code == 404  # on premises: not here
    assert env.admin.put(f"{Q}/bucketeer-nope/quota", json={"quotaTiB": 5},
                         headers=H).status_code == 404
    r = env.admin.put(url, json={"quotaTiB": 500}, headers=H)  # far above the weekly limit
    assert r.status_code == 200
    assert r.json() == {"bucket": FULL, "quotaTiB": 500, "changed": True,
                        "monthlyCost": r.json()["monthlyCost"], "tagSynced": True}
    assert env.db.buckets.get(FULL)["quota_tib"] == 500
    assert tags(env, FULL)["bucketeer-quota"] == "500"
    [a] = acts(env, "admin.bucket.quota")
    assert a["actor"] == "bob" and a["target"] == FULL
    assert a["details"] == {"location": "cloud", "owner": "alice", "oldTiB": 2, "newTiB": 500,
                            "tagSynced": True}
    rec = [x for x in env.db.quota_changes.list() if x["bucket"] == FULL]
    assert any(x["new_tib"] == 500 and not x["counted"] and x["actor"] == "bob" for x in rec)
    # the owner's weekly allowance is untouched by the admin's change
    assert env.user.get(f"{B}/options").json()["quota"]["remainingTiB"] == \
        env.user.get("/api/buckets/options").json()["quota"]["remainingTiB"]
    r = env.admin.put(url, json={"quotaTiB": 500}, headers=H)
    assert r.json()["changed"] is False and len(acts(env, "admin.bucket.quota")) == 1


def test_chargeback_has_cloud_rows(env):
    created(env)
    size(FULL, 3 * 2**30)
    r = env.admin.get("/api/admin/export/chargeback.csv")
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assert list(rows[0])[-2:] == ["location", "product"]
    by = {x["bucket"]: x for x in rows}
    c = by[FULL]
    assert (c["location"], c["product"], c["owner_uid"]) == ("cloud", "Cloud-S3", "alice")
    assert c["bytes"] == str(3 * 2**30) and c["size_gib"] == "3.000"
    assert c["quota_bytes"] == str(2 * TIB) and c["objects"] == "" and c["created_at"]
    assert c["budget_number"] == "DN099999" and c["budget_status"] == "ok"
    assert c["owner_name"] == "Alice Anderson"
    assert (by["alice-data"]["location"], by["alice-data"]["product"]) == ("onprem", "Tier-3-S3")


def test_check_says_when_the_name_is_your_own(env):
    created(env)
    body = env.user.get(f"{B}/check", params={"name": "scans"}).json()
    assert body["status"] == "exists"
    assert body["message"] == "You already have a bucket with this name."
    env.s3.create_bucket(Bucket="bucketeer-alice-other",  # in the account, no row
                         CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    body = env.user.get(f"{B}/check", params={"name": "other"}).json()
    assert body["message"] == "A bucket with this name already exists."


# -- rebuild ------------------------------------------------------------------------------


def run_rebuild(env, monkeypatch, fill_gaps=True):
    async def no_onprem(self, b, sem):  # the on-premises side is tested in test_rebuild.py
        return None
    monkeypatch.setattr(rebuild._Run, "read_bucket", no_onprem)
    return asyncio.run(rebuild.rebuild_from_ceph(env.app, fill_gaps))


def test_rebuild_restores_cloud_rows(env, monkeypatch):
    created(env)
    created(env, name="cold", storageClass="deep_archive", quotaTiB=3, budgetNumber="DN012345")
    before = {n: env.db.buckets.get(n) for n in (FULL, "bucketeer-alice-cold")}
    for n in before:
        env.db.buckets.delete(n, "cloud")
    # a system bucket and a prefix bucket without Bucketeer tags
    loc = {"LocationConstraint": "us-west-2"}
    env.s3.create_bucket(Bucket="bucketeer-backup-0123456789ab", CreateBucketConfiguration=loc)
    env.s3.create_bucket(Bucket="bucketeer-stray", CreateBucketConfiguration=loc)
    report = run_rebuild(env, monkeypatch)
    assert report["added"]["cloud_buckets"] == 2
    for n, old in before.items():
        row = env.db.buckets.get(n, "cloud")
        for k in ("owner_uid", "budget_number", "created_by", "storage_class", "quota_tib"):
            assert row[k] == old[k], (n, k)
        assert row["created_at"][:10] == old["created_at"][:10] and row["creation_known"] == 1
    assert env.db.buckets.get("bucketeer-alice-cold")["storage_class"] == "deep_archive"
    assert {"table": "cloud_buckets", "item": "bucketeer-backup-0123456789ab",
            "reason": "system_bucket"} in report["skipped"]
    assert any(p["item"] == "bucketeer-stray" and p["reason"] == "no_owner_tag"
               for p in report["problems"])
    assert env.db.buckets.get("bucketeer-stray") is None

    # fill gaps never overwrites: a changed row stays as it is
    env.db.buckets.set_quota_tib(FULL, 9)
    report = run_rebuild(env, monkeypatch)
    assert report["added"]["cloud_buckets"] == 0
    assert {"table": "cloud_buckets", "item": FULL, "reason": "exists"} in report["skipped"]
    assert env.db.buckets.get(FULL)["quota_tib"] == 9


def test_rebuild_storage_class_from_product_and_unknown_quota(env, monkeypatch):
    created(env, storageClass="deep_archive")
    env.db.buckets.delete(FULL, "cloud")
    env.s3.delete_bucket_lifecycle(Bucket=FULL)
    t = tags(env, FULL)
    t["bucketeer-quota"] = "unlimited"
    env.s3.put_bucket_tagging(Bucket=FULL, Tagging={"TagSet": [
        {"Key": k, "Value": v} for k, v in t.items()]})
    report = run_rebuild(env, monkeypatch)
    row = env.db.buckets.get(FULL, "cloud")
    assert row["storage_class"] == "deep_archive"  # from the bucketeer-product tag
    assert row["quota_tib"] == 1  # the default quota
    assert any(p["reason"] == "quota_unknown" for p in report["problems"])


def test_rebuild_without_cloud_has_no_cloud_step(env, monkeypatch):
    env.app.state.cloud = None
    assert "cloud_buckets" not in run_rebuild(env, monkeypatch)["added"]
