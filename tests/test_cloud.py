"""Cloud (AWS) foundation, P6-8: CloudAdmin on moto, the global "Default location"
setting, the per-user location in /api/profile and /api/me. No network: every AWS
call goes to moto's in-process mock (`mock_aws`)."""

import asyncio
import re
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

from bucketeer import cloud
from bucketeer.cloud import CloudAdmin, CloudError, CloudUnavailable
from bucketeer.location import effective_location
from bucketeer.main import create_app

from .conftest import REQUIRED
from .test_admin import Env, H
from .test_auth import make_settings

ACCESS = "AKIAEXAMPLEKEY000001"
SECRET = "cloud-secret-for-tests-only"
CLOUD = {"cloud_user_prefix": "s3-", "cloud_admin_access_key": ACCESS,
         "cloud_admin_secret_key": SECRET}


@pytest.fixture(autouse=True)
def no_cloud_monitor(monkeypatch):
    monkeypatch.setattr(cloud, "CHECK_ON_START", False)


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def admin_client(tmp_path, aws):
    return CloudAdmin(make_settings(tmp_path, **CLOUD))


def run(coro):
    return asyncio.run(coro)


# -- the client ------------------------------------------------------------------------


def test_not_configured_refuses(tmp_path):
    with pytest.raises(cloud.NotConfigured):
        CloudAdmin(make_settings(tmp_path))


def test_repr_has_no_keys(admin_client):
    assert ACCESS not in repr(admin_client) and SECRET not in repr(admin_client)


def test_check_ok(admin_client):
    res = run(admin_client.check())
    assert res["ok"] is True and res["reason"] is None and res["region"] == "us-west-2"
    assert re.fullmatch(r"\d{12}", res["accountId"])
    assert admin_client.available and admin_client.last_check is res


def test_check_failure_is_reported_without_secrets(admin_client, monkeypatch):
    def refuse(service, operation, params):
        if service == "iam":
            raise CloudError(403, "AccessDenied", f"AWS refused {operation} (AccessDenied)")
        return {"Account": "000000000000", "Arn": "x"}

    monkeypatch.setattr(admin_client, "_call_sync", refuse)
    res = run(admin_client.check())
    assert res["ok"] is False and "AccessDenied" in res["reason"]
    assert not admin_client.available


def test_client_error_is_mapped_without_message(admin_client, caplog):
    with pytest.raises(CloudError) as exc:
        run(admin_client.call("iam", "get_user", UserName="nobody-here"))
    assert exc.value.code == "NoSuchEntity" and exc.value.status == 404
    assert "nobody-here" not in str(exc.value)  # AWS's message is only logged
    assert SECRET not in caplog.text and ACCESS not in str(exc.value)


def test_unreachable_is_unavailable(admin_client, monkeypatch):
    c = admin_client.client("sts")

    def boom(**_kw):
        raise EndpointConnectionError(endpoint_url="https://sts.example.invalid/?X=" + SECRET)

    monkeypatch.setattr(c, "get_caller_identity", boom)
    with pytest.raises(CloudUnavailable) as exc:
        run(admin_client.call("sts", "get_caller_identity"))
    assert exc.value.status == 503 and "example.invalid" not in str(exc.value)
    assert SECRET not in str(exc.value)
    res = run(admin_client.check())
    assert res["ok"] is False and "could not be reached" in res["reason"]


def test_unknown_service_refused(admin_client):
    with pytest.raises(ValueError):
        admin_client.client("ec2")


def test_naming_helpers(admin_client):
    assert admin_client.user_name("JDoe") == "s3-jdoe"
    assert admin_client.bucket_name("JDoe", "scans") == "bucketeer-jdoe-scans"


def test_users_buckets_and_metrics(admin_client):
    iam = boto3.client("iam", region_name="us-west-2")
    iam.create_user(UserName="s3-alice", Path="/bucketeer/")
    iam.create_user(UserName="someone-else")
    assert [u["UserName"] for u in run(admin_client.list_users())] == ["s3-alice"]
    assert run(admin_client.get_user("s3-alice"))["UserName"] == "s3-alice"
    assert run(admin_client.get_user("s3-nobody")) is None

    s3 = boto3.client("s3", region_name="us-west-2")
    for name in ("bucketeer-alice-data", "other-bucket"):
        s3.create_bucket(Bucket=name,
                         CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    assert [b["Name"] for b in run(admin_client.list_buckets())] == ["bucketeer-alice-data"]
    assert run(admin_client.bucket_status("bucketeer-alice-data")) == "exists"
    assert run(admin_client.bucket_status("bucketeer-alice-free")) == "free"

    now = datetime.now(UTC)
    boto3.client("cloudwatch", region_name="us-west-2").put_metric_data(
        Namespace="AWS/S3", MetricData=[{
            "MetricName": "BucketSizeBytes", "Value": 2048.0, "Timestamp": now,
            "Dimensions": [{"Name": "BucketName", "Value": "bucketeer-alice-metrics"},
                           {"Name": "StorageType", "Value": "StandardStorage"}]}])
    got = run(admin_client.bucket_metric("bucketeer-alice-metrics", "BucketSizeBytes",
                                         "StandardStorage", now - timedelta(days=2),
                                         now + timedelta(hours=1)))
    assert got == 2048.0
    assert run(admin_client.bucket_metric("bucketeer-alice-none", "BucketSizeBytes",
                                          "StandardStorage", now - timedelta(days=2),
                                          now)) is None


def test_monitor_loop_rechecks(admin_client):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(admin_client.monitor_loop(sleep=sleep))
    assert sleeps == [cloud.RECHECK_OK, cloud.RECHECK_OK]


def test_create_app_builds_the_client_only_when_configured(tmp_path):
    from bucketeer.db import Database

    db = Database(":memory:")
    assert create_app(make_settings(tmp_path), db=db).state.cloud is None
    app = create_app(make_settings(tmp_path, **CLOUD), db=db)
    assert isinstance(app.state.cloud, CloudAdmin)
    assert create_app(make_settings(tmp_path, **CLOUD), db=db,
                      cloud_admin=cloud.NO_CLOUD).state.cloud is None
    db.close()


def test_effective_location():
    # the saved choice always applies, whatever Advanced is (it only unlocks the switch)
    assert effective_location(None, "onprem", True) == "onprem"
    assert effective_location("cloud", "onprem", True) == "cloud"
    assert effective_location("cloud", "onprem", False) == "onprem"  # unavailable
    assert effective_location(None, "cloud", True) == "cloud"
    assert effective_location("onprem", "cloud", True) == "onprem"
    assert effective_location(None, "cloud", False) == "onprem"


# -- the admin setting -----------------------------------------------------------------

L = "/api/admin/settings/location"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


@pytest.fixture
def cloud_env(env, tmp_path, aws):
    env.app.state.cloud = CloudAdmin(make_settings(tmp_path, **CLOUD))
    return env


def test_location_setting_gating(env):
    assert env.anon.get(L).status_code == 401
    assert env.user.get(L).status_code == 403
    assert env.user.put(L, json={"default": "onprem"}, headers=H).status_code == 403
    assert env.admin.put(L, json={"default": "onprem"}).status_code == 403  # CSRF


def test_location_setting_without_cloud(env):
    r = env.admin.get(L)
    assert r.status_code == 200
    assert r.json() == {"default": "onprem", "configured": False, "region": "us-west-2",
                        "bucketPrefix": "bucketeer-", "check": None}
    r = env.admin.put(L, json={"default": "cloud"}, headers=H)
    assert r.status_code == 422 and "not set up" in r.json()["detail"]
    for bad in ({"default": "mars"}, {}, {"default": "onprem", "x": 1}):
        assert env.admin.put(L, json=bad, headers=H).status_code == 422
    assert env.audit("admin.settings.location") == []


def test_location_setting_to_cloud_and_back(cloud_env):
    env = cloud_env
    r = env.admin.get(L)
    assert r.json()["configured"] is True and r.json()["check"]["ok"] is True
    r = env.admin.put(L, json={"default": "cloud"}, headers=H)
    assert r.status_code == 200 and r.json()["default"] == "cloud"
    assert env.db.settings.get("location") == {"default": "cloud"}
    # everyone works in Cloud by default now, Advanced or not
    me = env.user.get("/api/me").json()
    assert me["location"] == "cloud" and me["defaultLocation"] == "cloud"
    assert me["cloudAvailable"] is True and me["locationChoice"] is None
    env.admin.put(L, json={"default": "cloud"}, headers=H)  # unchanged: not audited again
    assert env.admin.put(L, json={"default": "onprem"}, headers=H).json()["default"] == "onprem"
    entries = env.audit("admin.settings.location")
    assert [(a["details"]["before"], a["details"]["after"]) for a in reversed(entries)] == [
        ("onprem", "cloud"), ("cloud", "onprem")]
    assert all(a["target_type"] == "setting" and a["target"] == "location" for a in entries)


def test_location_setting_refused_when_check_fails(cloud_env, monkeypatch):
    env = cloud_env

    async def failing():
        return {"ok": False, "accountId": None, "region": "us-west-2",
                "reason": "AWS refused ListUsers (AccessDenied)", "checkedAt": "now"}

    monkeypatch.setattr(env.app.state.cloud, "check", failing)
    r = env.admin.put(L, json={"default": "cloud"}, headers=H)
    assert r.status_code == 422 and "AccessDenied" in r.json()["detail"]
    assert SECRET not in r.text and env.db.settings.get("location") is None


# -- the user's choice -----------------------------------------------------------------


def test_user_location_needs_cloud(env):
    r = env.user.put("/api/profile", json={"location": "cloud"}, headers=H)
    assert r.status_code == 422
    r = env.user.put("/api/profile", json={"location": "moon"}, headers=H)
    assert r.status_code == 422
    # a refused location leaves the other fields of the same request unsaved
    r = env.user.put("/api/profile", json={"advancedMode": True, "location": "cloud"},
                     headers=H)
    assert r.status_code == 422 and env.db.profiles.get("alice")["advanced_mode"] is False
    me = env.user.get("/api/me").json()
    assert me["location"] == "onprem" and me["cloudAvailable"] is False


def test_user_location_is_kept_when_advanced_is_off(cloud_env):
    env = cloud_env
    run(env.app.state.cloud.check())
    r = env.user.put("/api/profile", json={"location": "cloud"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["locationChoice"] == body["location"] == "cloud"  # Advanced off, kept
    r = env.user.put("/api/profile", json={"advancedMode": True}, headers=H)
    assert r.json()["location"] == "cloud"
    assert env.user.get("/api/me").json()["location"] == "cloud"
    # turning Advanced off must not move the location back
    r = env.user.put("/api/profile", json={"advancedMode": False}, headers=H)
    assert r.json()["location"] == "cloud" and r.json()["locationChoice"] == "cloud"
    assert env.user.get("/api/me").json()["location"] == "cloud"
    # cloud goes away: back to on premises, the choice is kept
    env.app.state.cloud.last_check = {**env.app.state.cloud.last_check, "ok": False}
    me = env.user.get("/api/me").json()
    assert me["location"] == "onprem" and me["locationChoice"] == "cloud"
    assert me["cloudAvailable"] is False
    r = env.user.put("/api/profile", json={"location": None}, headers=H)
    assert r.status_code == 200 and r.json()["locationChoice"] is None
    entries = [a for a in env.db.audit.query(limit=100)
               if a["action"] == "profile.update" and a["details"]["field"] == "location"]
    assert [(a["details"]["old"], a["details"]["new"]) for a in reversed(entries)] == [
        (None, "cloud"), ("cloud", None)]


def test_settings_secret_not_in_repr():
    s = make_settings(__import__("pathlib").Path("/nonexistent"), **CLOUD)
    assert SECRET not in repr(s) and SECRET not in str(s)
    assert REQUIRED  # keep the import honest
