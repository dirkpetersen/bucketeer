"""Backups to AWS (P6-12, changed 2026-11-27): every run writes the archive to the on-premises
bucket AND, while cloud is configured, to AWS. Both buckets have the SAME name (BACKUP_BUCKET),
so the AWS side uses `AwsDestination(admin, bucket)` on moto's in-process AWS (`mock_aws`: IAM
with the boundary policy tools/aws-setup creates, S3 with object lock, STS). The on-premises side
runs against the moto server of `s3_endpoint` (a 127.0.0.1 URL mock_aws lets through)."""

import asyncio
import json
import logging
import re
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from bucketeer import backup, cloud
from bucketeer.cloud import CloudAdmin, CloudError, CloudUnavailable
from bucketeer.db import DB_FILENAME, Database

from .backends import admin_client
from .test_auth import make_settings
from .test_backup import ENV_TEXT, env_file  # noqa: F401  (fixture)
from .test_buckets import Env, H

REGION = "us-west-2"
CLOUD = {"cloud_user_prefix": "s3-", "cloud_admin_access_key": "AKIAEXAMPLEKEY000001",
         "cloud_admin_secret_key": "cloud-admin-secret-for-tests-only"}
# BACKUP_BUCKET with cloud configured: the same name on premises and in AWS
SHARED = "bucketeer-backup-0000000000aa"
NAME_RE = re.compile(r"bucketeer-backup-[0-9a-f]{12}")
USER = "s3-bucketeer-backup"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(cloud, "CHECK_ON_START", False)
    monkeypatch.setattr(backup, "KEY_WAITS", (0.0, 0.0))


@pytest.fixture
def aws():
    with mock_aws():
        boto3.client("iam", region_name=REGION).create_policy(
            PolicyName=cloud.BOUNDARY_POLICY, Path=cloud.USER_PATH,
            PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
                 "Resource": "arn:aws:s3:::bucketeer-*/*"}]}))
        yield


def iam():
    return boto3.client("iam", region_name=REGION)


def s3():
    return boto3.client("s3", region_name=REGION)


class Spy:
    def __init__(self, admin: CloudAdmin):
        self.secrets: list[str] = []
        self.ops: list[str] = []
        self.params: dict[str, dict] = {}
        real = admin._call_sync

        def call(service, operation, params):
            self.ops.append(operation)
            self.params[operation] = params
            res = real(service, operation, params)
            if operation == "create_access_key":
                self.secrets.append(res["AccessKey"]["SecretAccessKey"])
            return res

        admin._call_sync = call


@pytest.fixture
def cenv(tmp_path, s3_endpoint, env_file, aws):  # noqa: F811
    e = Env(tmp_path, s3_endpoint, backup_bucket=SHARED)
    admin = CloudAdmin(make_settings(tmp_path, **CLOUD))
    asyncio.run(admin.check())
    e.app.state.cloud = admin
    e.spy = Spy(admin)
    yield e
    e.db.close()


def run_now(env):
    r = env.bob.post("/api/admin/backup/run", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def entry(rec, kind):
    (found,) = [d for d in rec["destinations"] if d["kind"] == kind]
    return found


def keys_left():
    return iam().list_access_keys(UserName=USER)["AccessKeyMetadata"]


def read(bucket, key, tmp_path):
    p = tmp_path / "dl.tar.gz"
    p.write_bytes(s3().get_object(Bucket=bucket, Key=key)["Body"].read())
    return backup.read_archive(p)


# -- where backups go ----------------------------------------------------------------------


def test_both_destinations_while_cloud_is_configured(cenv):
    svc = cenv.app.state.backup
    assert [d.kind for d in svc.destinations()] == ["onprem", "cloud"]
    assert all(d.bucket == SHARED for d in svc.destinations())
    # the location no longer picks the place: both copies, always
    cenv.db.settings.set("location", {"default": "onprem"})
    assert [d.kind for d in svc.destinations()] == ["onprem", "cloud"]
    cenv.app.state.cloud = None
    assert [d.kind for d in svc.destinations()] == ["onprem"]


def test_cloud_run_sets_up_bucket_user_and_archive(cenv, tmp_path):
    rec = run_now(cenv)
    assert rec["outcome"] == "ok", rec
    assert rec["bucket"] == SHARED
    assert [d["kind"] for d in rec["destinations"]] == ["onprem", "cloud"]
    assert all(d["outcome"] == "ok" for d in rec["destinations"])
    assert entry(rec, "cloud")["retentionDays"] == 30

    c = s3()
    lock = c.get_object_lock_configuration(Bucket=SHARED)["ObjectLockConfiguration"]
    assert lock["ObjectLockEnabled"] == "Enabled"
    ret = lock["Rule"]["DefaultRetention"]
    assert (ret["Mode"], ret["Days"]) == ("COMPLIANCE", 30)
    assert c.get_bucket_versioning(Bucket=SHARED)["Status"] == "Enabled"
    pab = c.get_public_access_block(Bucket=SHARED)["PublicAccessBlockConfiguration"]
    assert all(pab[k] for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy",
                                "RestrictPublicBuckets"))
    enc = c.get_bucket_encryption(Bucket=SHARED)["ServerSideEncryptionConfiguration"]
    assert enc["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    tags = {t["Key"]: t["Value"] for t in c.get_bucket_tagging(Bucket=SHARED)["TagSet"]}
    assert tags == {"bucketeer-bucket": SHARED, "bucketeer-product": "system",
                    "bucketeer-location": "cloud"}
    rules = {r["ID"]: r for r in c.get_bucket_lifecycle_configuration(Bucket=SHARED)["Rules"]}
    assert set(rules) == set(backup.RULE_IDS)
    assert rules["bucketeer-backup-expire"]["Expiration"]["Days"] == 31

    user = iam().get_user(UserName=USER)["User"]
    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    assert user["Path"] == "/bucketeer/"
    # moto's GetUser leaves the boundary out: check what was sent (aws-setup's condition)
    assert cenv.spy.params["create_user"]["PermissionsBoundary"] == \
        f"arn:aws:iam::{account}:policy/bucketeer/BucketeerUserBoundary"
    doc = iam().get_user_policy(UserName=USER, PolicyName="bucketeer-backup")["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    allowed = {(a, s["Resource"]) for s in doc["Statement"] for a in s["Action"]}
    assert allowed == {
        ("s3:ListBucket", f"arn:aws:s3:::{SHARED}"),
        ("s3:GetBucketLocation", f"arn:aws:s3:::{SHARED}"),
        ("s3:PutObject", f"arn:aws:s3:::{SHARED}/*"),
        ("s3:GetObject", f"arn:aws:s3:::{SHARED}/*"),
        ("s3:GetObjectVersion", f"arn:aws:s3:::{SHARED}/*"),
    }
    assert keys_left() == []  # the run's key is gone
    assert cenv.spy.ops.count("create_access_key") == 1
    assert cenv.spy.ops.count("delete_access_key") == 1

    head = c.head_object(Bucket=SHARED, Key=rec["key"])
    assert head["ObjectLockMode"] == "COMPLIANCE" and head["ContentLength"] == rec["bytes"]
    meta, files = read(SHARED, rec["key"], tmp_path)
    assert files[".env"].decode() == ENV_TEXT and meta["schemaVersion"] == cenv.db.version
    # the on-premises copy went to the same key, in the bucket of the same name
    assert SHARED in cenv.moto_buckets()

    rows = [a for a in cenv.db.audit.query(limit=100) if a["action"] == "backup.run"]
    assert sorted(r["details"]["destination"] for r in rows) == ["cloud", "onprem"]
    (cloud_row,) = [r for r in rows if r["details"]["destination"] == "cloud"]
    assert cloud_row["target"] == SHARED


def test_second_run_reuses_the_bucket_and_clears_leftover_keys(cenv):
    first = run_now(cenv)
    # a crash left two keys behind (the most IAM allows): both are removed first
    iam().create_access_key(UserName=USER)
    iam().create_access_key(UserName=USER)
    second = run_now(cenv)
    assert second["outcome"] == "ok" and second["bucket"] == first["bucket"]
    assert keys_left() == []
    # both runs of this minute share an archive key: each cloud run's row says it reused the bucket
    rows = [a for a in cenv.db.audit.query(limit=100)
            if a["action"] == "backup.run" and a["details"]["destination"] == "cloud"]
    assert len(rows) == 2 and all(r["details"]["bucketCreated"] is False for r in rows)
    assert len([b for b in s3().list_buckets()["Buckets"] if NAME_RE.fullmatch(b["Name"])]) == 1


def test_key_is_deleted_when_the_upload_fails(cenv, monkeypatch):
    def broken(*a):
        raise RuntimeError("upload broke")

    monkeypatch.setattr(backup, "upload_file", broken)
    rec = run_now(cenv)
    assert rec["outcome"] == "failed"
    assert entry(rec, "cloud")["outcome"] == "failed"
    assert keys_left() == []
    (row,) = [a for a in cenv.db.audit.query(limit=100)
              if a["action"] == "backup.failed" and a["details"]["destination"] == "cloud"]
    assert row["target"] == SHARED and row["details"]["error"] == "RuntimeError"


def test_new_key_is_retried_while_iam_propagates(cenv, monkeypatch):
    real, calls = backup.upload_file, []

    def flaky(s3_client, *a):
        if "127.0.0.1" in s3_client.meta.endpoint_url:  # the on-premises copy is not flaky
            return real(s3_client, *a)
        calls.append(1)
        if len(calls) < 3:
            raise ClientError({"Error": {"Code": "InvalidAccessKeyId"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
        return real(s3_client, *a)

    monkeypatch.setattr(backup, "upload_file", flaky)
    rec = run_now(cenv)
    assert rec["outcome"] == "ok" and len(calls) == 3
    monkeypatch.setattr(backup, "KEY_WAITS", ())
    calls.clear()
    rec = run_now(cenv)  # no more waiting: the refusal ends the AWS copy, the key still goes
    assert rec["outcome"] == "failed" and entry(rec, "onprem")["outcome"] == "ok"
    assert "InvalidAccessKeyId" in entry(rec, "cloud")["error"]
    assert keys_left() == []


def test_aws_copy_fails_alone_when_cloud_is_down(cenv, monkeypatch):
    admin = cenv.app.state.cloud

    async def down(name):
        raise CloudUnavailable("AWS could not be reached")

    monkeypatch.setattr(admin, "bucket_status", down)
    rec = run_now(cenv)
    assert rec["outcome"] == "failed" and rec["error"] == "cloud: AWS could not be reached"
    assert entry(rec, "onprem")["outcome"] == "ok" and SHARED in cenv.moto_buckets()


def test_name_taken_by_another_account_fails_the_aws_copy(cenv, monkeypatch):
    admin = cenv.app.state.cloud

    async def taken(name):
        return "taken"

    monkeypatch.setattr(admin, "bucket_status", taken)
    rec = run_now(cenv)
    assert rec["outcome"] == "failed"
    assert "already taken by another AWS account" in entry(rec, "cloud")["error"]
    assert "BACKUP_BUCKET" in entry(rec, "cloud")["error"]
    assert entry(rec, "onprem")["outcome"] == "ok"


def test_existing_bucket_without_object_lock_is_refused(cenv):
    # the test process has one moto store for both sides, so a bucket made here without a lock
    # is seen by the on-premises copy too: both refuse it, with the reason named
    s3().create_bucket(Bucket=SHARED, CreateBucketConfiguration={"LocationConstraint": REGION})
    rec = run_now(cenv)
    assert rec["outcome"] == "failed"
    assert "no object lock" in entry(rec, "cloud")["error"]
    assert "no object lock" in entry(rec, "onprem")["error"]


def test_aws_bucket_is_created_with_object_lock(tmp_path, aws):
    """The create path on its own (in a full run the on-premises copy may create the shared
    name first, in this test process)."""
    name = "bucketeer-backup-0000000000bb"
    admin = CloudAdmin(make_settings(tmp_path, **CLOUD))
    db = Database(":memory:")
    try:
        made = asyncio.run(backup.AwsDestination(admin, name).ensure(30))
        assert made == {"created": True, "retentionDays": 30}
        assert s3().get_bucket_location(Bucket=name)["LocationConstraint"] == REGION
        ret = s3().get_object_lock_configuration(
            Bucket=name)["ObjectLockConfiguration"]["Rule"]["DefaultRetention"]
        assert (ret["Mode"], ret["Days"]) == ("COMPLIANCE", 30)
        assert asyncio.run(backup.AwsDestination(admin, name).ensure(30))["created"] is False
    finally:
        db.close()


def test_no_secret_in_settings_audit_logs_or_answers(cenv, caplog):
    caplog.set_level(logging.DEBUG)
    rec = run_now(cenv)
    state = cenv.bob.get("/api/admin/backup").text
    assert cenv.spy.secrets
    with cenv.db._lock:
        dump = json.dumps([list(r) for t in ("settings", "audit_log")
                           for r in cenv.db._conn.execute(f"SELECT * FROM {t}")])
    for secret in [*cenv.spy.secrets, CLOUD["cloud_admin_secret_key"]]:
        for where in (dump, caplog.text, state, json.dumps(rec)):
            assert secret not in where


def test_state_shows_both_destinations(cenv):
    got = cenv.bob.get("/api/admin/backup").json()
    kinds = [d["kind"] for d in got["destinations"]]
    assert kinds == ["onprem", "cloud"]
    cloud_dest = got["destinations"][1]
    assert cloud_dest["bucket"] == SHARED and cloud_dest["user"] == USER
    assert got["region"] == REGION and got["awsBucket"] == SHARED
    assert got["onpremBucket"] == SHARED and got["bucket"] == SHARED
    assert cloud_dest["state"]["exists"] is False
    assert "create_user" not in cenv.spy.ops and "create_bucket" not in cenv.spy.ops
    rec = run_now(cenv)
    got = cenv.bob.get("/api/admin/backup").json()
    assert got["destinations"][1]["state"] == {
        "exists": True, "objectLock": True, "mode": "COMPLIANCE",
        "retentionDays": 30, "versioning": "enabled"}
    assert got["lastRun"]["outcome"] == rec["outcome"] == "ok"
    # time and retention edits apply to both buckets
    r = cenv.bob.put("/api/admin/backup/settings", json={"time": "02:30", "retentionDays": 40},
                     headers=H)
    assert r.status_code == 200 and "warning" in r.json()  # moto: lock change on a full bucket
    assert backup.load(cenv.db) == {"time": "02:30", "retentionDays": 40, "restoreTestDays": 30}


def test_us_east_1_has_no_location_constraint(tmp_path, aws):
    admin = CloudAdmin(make_settings(tmp_path, cloud_region="us-east-1", **CLOUD))
    db = Database(":memory:")
    try:
        dest = backup.AwsDestination(admin, SHARED)
        made = asyncio.run(dest.ensure(7))
        assert made == {"created": True, "retentionDays": 7}
        loc = boto3.client("s3", region_name="us-east-1").get_bucket_location(Bucket=SHARED)
        assert loc["LocationConstraint"] is None
    finally:
        db.close()


# -- restore test -------------------------------------------------------------------------------


def restore_test(env):
    r = env.bob.post("/api/admin/backup/restore-test", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_restore_test_covers_both_destinations(cenv, caplog):
    caplog.set_level(logging.DEBUG)
    rec = run_now(cenv)
    made = cenv.spy.ops.count("create_access_key")
    got = restore_test(cenv)
    assert got["outcome"] == "ok", got
    assert [d["kind"] for d in got["destinations"]] == ["onprem", "cloud"]
    aws = entry(got, "cloud")
    assert aws["bucket"] == SHARED and aws["key"] == rec["key"] and aws["error"] is None
    assert aws["schemaVersion"] == cenv.db.version and aws["tables"]["audit_log"] >= 1
    # the download used a temporary key of the backup IAM user, gone afterwards
    assert cenv.spy.ops.count("create_access_key") == made + 1 and keys_left() == []
    rows = [a for a in cenv.db.audit.query(limit=200) if a["action"] == "backup.restore_test"]
    assert sorted(r["details"]["destination"] for r in rows) == ["cloud", "onprem"]
    assert all(r["target"] == SHARED for r in rows)
    state = cenv.bob.get("/api/admin/backup").text
    with cenv.db._lock:
        dump = json.dumps([list(r) for t in ("settings", "audit_log")
                           for r in cenv.db._conn.execute(f"SELECT * FROM {t}")])
    for secret in [*cenv.spy.secrets, CLOUD["cloud_admin_secret_key"]]:
        for where in (dump, caplog.text, state, json.dumps(got)):
            assert secret not in where


def test_restore_test_failure_in_aws_alone(cenv, monkeypatch):
    run_now(cenv)

    async def down(self):
        raise CloudUnavailable("AWS could not be reached")

    monkeypatch.setattr(backup.AwsDestination, "latest", down)
    got = restore_test(cenv)
    assert got["outcome"] == "failed" and got["error"] == "cloud: AWS could not be reached"
    assert entry(got, "onprem")["outcome"] == "ok"
    (row,) = [a for a in cenv.db.audit.query(limit=200)
              if a["action"] == "backup.restore_test_failed"]
    assert row["target"] == SHARED and row["details"]["destination"] == "cloud"
    assert row["details"]["error"] == "AWS could not be reached"
    assert keys_left() == []


# -- restore from AWS ---------------------------------------------------------------------------


def test_restore_from_cloud(cenv, tmp_path, monkeypatch):
    cenv.db.settings.set("marker", "in-the-cloud-backup")
    rec = run_now(cenv)
    monkeypatch.setattr(backup, "app_is_running", lambda s: False)
    data_dir = tmp_path / "restore-data"
    settings = cenv.settings.model_copy(update={"data_dir": str(data_dir), **CLOUD})
    client = admin_client(settings, cenv.mock, cenv.transport)
    admin = cenv.app.state.cloud

    # a new machine: no database, the source is named and the bucket is BACKUP_BUCKET
    details = asyncio.run(backup.restore(settings, "latest", source="cloud", client=client,
                                         cloud_admin=admin))
    assert details["source"] == "cloud" and details["bucket"] == SHARED
    assert details["key"] == rec["key"]
    assert keys_left() == []
    restored = Database(data_dir / DB_FILENAME)
    try:
        assert restored.settings.get("marker") == "in-the-cloud-backup"
        (row,) = [a for a in restored.audit.query(limit=500) if a["action"] == "system.restore"]
        assert row["target"] == SHARED and row["details"]["source"] == "cloud"
    finally:
        restored.close()


def test_restore_source_defaults_and_errors(cenv, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "app_is_running", lambda s: False)
    settings = cenv.settings.model_copy(update={"data_dir": str(tmp_path / "d")})
    with pytest.raises(backup.BackupError, match="--from"):
        asyncio.run(backup.restore(settings, "latest", source="mars"))
    with pytest.raises(backup.BackupError, match="CLOUD_"):  # cloud not configured
        asyncio.run(backup.restore(settings, "latest", source="cloud"))
    admin = cenv.app.state.cloud
    client = admin_client(settings, cenv.mock, cenv.transport)
    # without BACKUP_BUCKET the AWS bucket is found: none yet
    nameless = settings.model_copy(update={"backup_bucket": ""})
    with pytest.raises(backup.BackupError, match="No backup bucket found"):
        asyncio.run(backup.restore(nameless, "latest", source="cloud", client=client,
                                   cloud_admin=admin))
    for name in ("bucketeer-backup-00000000000a", "bucketeer-backup-00000000000b"):
        s3().create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
    with pytest.raises(backup.BackupError, match="--aws-bucket"):
        asyncio.run(backup.restore(nameless, "latest", source="cloud", client=client,
                                   cloud_admin=admin))
    with pytest.raises(backup.BackupError, match="No backup found"):
        asyncio.run(backup.restore(nameless, "latest", source="cloud", client=client,
                                   cloud_admin=admin, aws_bucket="bucketeer-backup-00000000000a"))


def test_restore_command_options(monkeypatch):
    seen = {}

    async def fake(settings, which, **kw):
        seen.update(which=which, **kw)

    monkeypatch.setattr(backup, "restore", fake)
    monkeypatch.setattr("bucketeer.config.load_settings", lambda: None)
    assert backup.restore_main(["--restore-backup", "latest", "--from", "mars"]) == 2
    assert backup.restore_main(["--restore-backup", "latest", "--from"]) == 2
    assert backup.restore_main(["--restore-backup", "latest", "--aws-bucket", "--force"]) == 2
    assert backup.restore_main(["--restore-backup", "latest", "--from", "cloud",
                                "--aws-bucket", "bucketeer-backup-x", "--force"]) == 0
    assert seen == {"which": "latest", "force": True, "source": "cloud",
                    "aws_bucket": "bucketeer-backup-x", "backup_bucket": None}


# -- the backup user is never a person --------------------------------------------------------


def test_reserved_usernames(tmp_path, aws):
    # backup, billing and cloud (P6-10): a person's buckets would sit next to the system buckets
    assert cloud.reserved_usernames() == {"bucketeer-admin", "bucketeer-backup", "backup",
                                          "billing", "cloud"}
    admin = CloudAdmin(make_settings(tmp_path, **CLOUD))
    assert admin.backup_user_name() == USER
    for name in ("bucketeer-backup", "Bucketeer-Backup", "bucketeer-admin", "Cloud"):
        with pytest.raises(CloudError) as exc:
            admin.user_name(name)
        assert exc.value.status == 409 and exc.value.code == "reserved"
    assert admin.user_name("alice") == "s3-alice"
    # the owner word `cloud` covers `cloud-*` too: bucketeer-cloud-billing-<hex> stays a system name
    with pytest.raises(CloudError):
        admin.owner_part("cloud-billing")


def test_session_refuses_when_two_keys_are_held(tmp_path, aws, monkeypatch):
    admin = CloudAdmin(make_settings(tmp_path, **CLOUD))
    db = Database(":memory:")
    try:
        dest = backup.AwsDestination(admin, SHARED)
        asyncio.run(dest.ensure(1))
        held = [iam().create_access_key(UserName=USER)["AccessKey"]["AccessKeyId"]
                for _ in range(2)]
        monkeypatch.setattr(backup, "_HELD_KEYS", set(held))
        with pytest.raises(backup.BackupError, match="two keys"):
            asyncio.run(dest.upload("backup/x", Path(tmp_path / "nothing")))
        assert {k["AccessKeyId"] for k in keys_left()} == set(held)  # never another's key
    finally:
        db.close()
