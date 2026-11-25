"""Backups to AWS (P6-12): AwsDestination on moto's in-process AWS (`mock_aws`: IAM with the
boundary policy tools/aws-setup creates, S3 with object lock, STS). The on-premises side
still runs against the moto server of `s3_endpoint` (a 127.0.0.1 URL mock_aws lets through)."""

import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from bucketeer import backup, cloud
from bucketeer.cloud import CloudAdmin, CloudError
from bucketeer.db import DB_FILENAME, Database

from .backends import admin_client
from .test_auth import make_settings
from .test_backup import BUCKET, ENV_TEXT, env_file  # noqa: F401  (fixture)
from .test_buckets import Env, H

REGION = "us-west-2"
CLOUD = {"cloud_user_prefix": "s3-", "cloud_admin_access_key": "AKIAEXAMPLEKEY000001",
         "cloud_admin_secret_key": "cloud-admin-secret-for-tests-only"}
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
    """Records the secrets AWS hands out so the tests can look for them everywhere."""

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
    e = Env(tmp_path, s3_endpoint, backup_bucket=BUCKET)
    admin = CloudAdmin(make_settings(tmp_path, **CLOUD))
    asyncio.run(admin.check())
    e.app.state.cloud = admin
    e.spy = Spy(admin)
    e.db.settings.set("location", {"default": "cloud"})
    yield e
    e.db.close()


def run_now(env):
    r = env.bob.post("/api/admin/backup/run", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def keys_left():
    return iam().list_access_keys(UserName=USER)["AccessKeyMetadata"]


def read(bucket, key, tmp_path):
    p = tmp_path / "dl.tar.gz"
    p.write_bytes(s3().get_object(Bucket=bucket, Key=key)["Body"].read())
    return backup.read_archive(p)


# -- where backups go ----------------------------------------------------------------------


def test_destination_follows_the_global_location(cenv):
    svc = cenv.app.state.backup
    assert isinstance(svc.destination(), backup.AwsDestination)
    cenv.db.settings.set("location", {"default": "onprem"})
    assert isinstance(svc.destination(), backup.OnPremDestination)
    cenv.db.settings.set("location", {"default": "cloud"})
    admin = cenv.app.state.cloud
    admin.last_check = {**admin.last_check, "ok": False}  # cloud unavailable: on premises
    assert isinstance(svc.destination(), backup.OnPremDestination)
    cenv.app.state.cloud = None
    assert isinstance(svc.destination(), backup.OnPremDestination)


def test_cloud_run_sets_up_bucket_user_and_archive(cenv, tmp_path):
    rec = run_now(cenv)
    assert rec["outcome"] == "ok", rec
    assert rec["destination"] == "cloud" and NAME_RE.fullmatch(rec["bucket"])
    name = rec["bucket"]
    stored = cenv.db.settings.get("backup")
    assert stored["awsBucket"] == name and stored["awsBucketCreated"] is True

    c = s3()
    assert c.get_bucket_location(Bucket=name)["LocationConstraint"] == REGION
    lock = c.get_object_lock_configuration(Bucket=name)["ObjectLockConfiguration"]
    assert lock["ObjectLockEnabled"] == "Enabled"
    ret = lock["Rule"]["DefaultRetention"]
    assert (ret["Mode"], ret["Days"]) == ("COMPLIANCE", 30)
    assert c.get_bucket_versioning(Bucket=name)["Status"] == "Enabled"
    pab = c.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
    assert all(pab[k] for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy",
                                "RestrictPublicBuckets"))
    enc = c.get_bucket_encryption(Bucket=name)["ServerSideEncryptionConfiguration"]
    assert enc["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    tags = {t["Key"]: t["Value"] for t in c.get_bucket_tagging(Bucket=name)["TagSet"]}
    assert tags == {"bucketeer-bucket": name, "bucketeer-product": "system",
                    "bucketeer-location": "cloud"}
    rules = {r["ID"]: r for r in c.get_bucket_lifecycle_configuration(Bucket=name)["Rules"]}
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
        ("s3:ListBucket", f"arn:aws:s3:::{name}"),
        ("s3:GetBucketLocation", f"arn:aws:s3:::{name}"),
        ("s3:PutObject", f"arn:aws:s3:::{name}/*"),
        ("s3:GetObject", f"arn:aws:s3:::{name}/*"),
        ("s3:GetObjectVersion", f"arn:aws:s3:::{name}/*"),
    }
    assert keys_left() == []  # the run's key is gone
    assert cenv.spy.ops.count("create_access_key") == 1
    assert cenv.spy.ops.count("delete_access_key") == 1

    head = c.head_object(Bucket=name, Key=rec["key"])
    assert head["ObjectLockMode"] == "COMPLIANCE" and head["ContentLength"] == rec["bytes"]
    meta, files = read(name, rec["key"], tmp_path)
    assert files[".env"].decode() == ENV_TEXT and meta["schemaVersion"] == cenv.db.version
    assert BUCKET not in cenv.moto_buckets()  # nothing written on premises

    (row,) = [a for a in cenv.db.audit.query(limit=100) if a["action"] == "backup.run"]
    assert row["target"] == name and row["details"]["destination"] == "cloud"
    assert row["details"]["bucketCreated"] is True


def test_second_run_reuses_the_bucket_and_clears_leftover_keys(cenv):
    first = run_now(cenv)
    # a crash left two keys behind (the most IAM allows): both are removed first
    iam().create_access_key(UserName=USER)
    iam().create_access_key(UserName=USER)
    second = run_now(cenv)
    assert second["outcome"] == "ok" and second["bucket"] == first["bucket"]
    assert keys_left() == []
    rows = [a for a in cenv.db.audit.query(limit=100) if a["action"] == "backup.run"]
    assert rows[0]["details"]["bucketCreated"] is False
    assert len([b for b in s3().list_buckets()["Buckets"] if NAME_RE.fullmatch(b["Name"])]) == 1


def test_key_is_deleted_when_the_upload_fails(cenv, monkeypatch):
    def broken(*a):
        raise RuntimeError("upload broke")

    monkeypatch.setattr(backup, "upload_file", broken)
    rec = run_now(cenv)
    assert rec["outcome"] == "failed" and rec["destination"] == "cloud"
    assert keys_left() == []
    (row,) = [a for a in cenv.db.audit.query(limit=100) if a["action"] == "backup.failed"]
    assert row["details"]["destination"] == "cloud" and NAME_RE.fullmatch(row["target"])


def test_new_key_is_retried_while_iam_propagates(cenv, monkeypatch):
    real, calls = backup.upload_file, []

    def flaky(*a):
        calls.append(1)
        if len(calls) < 3:
            raise ClientError({"Error": {"Code": "InvalidAccessKeyId"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
        return real(*a)

    monkeypatch.setattr(backup, "upload_file", flaky)
    assert run_now(cenv)["outcome"] == "ok" and len(calls) == 3
    monkeypatch.setattr(backup, "KEY_WAITS", ())
    calls.clear()
    rec = run_now(cenv)  # no more waiting: the refusal ends the run, the key still goes
    assert rec["outcome"] == "failed" and "InvalidAccessKeyId" in rec["error"]
    assert keys_left() == []


def test_taken_name_is_replaced(cenv, monkeypatch):
    taken = "bucketeer-backup-000000000000"
    s3().create_bucket(Bucket=taken, CreateBucketConfiguration={"LocationConstraint": REGION})
    names = iter([taken, "bucketeer-backup-0000000000ff"])
    monkeypatch.setattr(CloudAdmin, "new_backup_bucket_name", lambda self: next(names))
    rec = run_now(cenv)
    assert rec["outcome"] == "ok" and rec["bucket"] == "bucketeer-backup-0000000000ff"
    assert cenv.db.settings.get("backup")["awsBucket"] == rec["bucket"]


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


def test_state_shows_the_cloud_destination(cenv):
    got = cenv.bob.get("/api/admin/backup").json()
    assert got["destination"] == "cloud" and got["bucket"] is None and got["awsBucket"] is None
    assert got["region"] == REGION and got["user"] == USER and got["onpremBucket"] == BUCKET
    assert got["bucketState"]["exists"] is False and "AWS" in got["bucketState"]["note"]
    assert "create_user" not in cenv.spy.ops and "create_bucket" not in cenv.spy.ops
    rec = run_now(cenv)
    got = cenv.bob.get("/api/admin/backup").json()
    assert got["bucket"] == got["awsBucket"] == rec["bucket"]
    assert got["bucketState"] == {"exists": True, "objectLock": True, "mode": "COMPLIANCE",
                                  "retentionDays": 30, "versioning": "enabled"}
    # time and retention edits keep the stored AWS bucket
    r = cenv.bob.put("/api/admin/backup/settings", json={"time": "02:30", "retentionDays": 40},
                     headers=H)
    assert r.status_code == 200 and "warning" in r.json()  # moto: lock change on a full bucket
    assert cenv.db.settings.get("backup")["awsBucket"] == rec["bucket"]
    assert backup.load(cenv.db) == {"time": "02:30", "retentionDays": 40}


def test_switching_back_writes_on_premises_and_keeps_the_aws_bucket(cenv):
    first = run_now(cenv)
    cenv.db.settings.set("location", {"default": "onprem"})
    rec = run_now(cenv)
    assert rec["outcome"] == "ok" and rec["destination"] == "onprem" and rec["bucket"] == BUCKET
    assert BUCKET in cenv.moto_buckets()
    got = cenv.bob.get("/api/admin/backup").json()
    assert got["destination"] == "onprem" and got["bucket"] == BUCKET
    assert got["awsBucket"] == first["bucket"]
    # the cloud archive is still there
    assert s3().head_object(Bucket=first["bucket"], Key=first["key"])


def test_us_east_1_has_no_location_constraint(tmp_path, aws):
    admin = CloudAdmin(make_settings(tmp_path, cloud_region="us-east-1", **CLOUD))
    db = Database(":memory:")
    try:
        dest = backup.AwsDestination(admin, db)
        made = asyncio.run(dest.ensure(7))
        assert made == {"created": True, "retentionDays": 7}
        loc = boto3.client("s3", region_name="us-east-1").get_bucket_location(Bucket=dest.bucket)
        assert loc["LocationConstraint"] is None
    finally:
        db.close()


# -- restore from AWS ---------------------------------------------------------------------------


def test_restore_from_cloud(cenv, tmp_path, monkeypatch):
    cenv.db.settings.set("marker", "in-the-cloud-backup")
    rec = run_now(cenv)
    monkeypatch.setattr(backup, "app_is_running", lambda s: False)
    data_dir = tmp_path / "restore-data"
    settings = cenv.settings.model_copy(update={"data_dir": str(data_dir), **CLOUD})
    client = admin_client(settings, cenv.mock, cenv.transport)
    admin = cenv.app.state.cloud

    # a new machine: no database, so the source must be named and the bucket is found
    details = asyncio.run(backup.restore(settings, "latest", source="cloud", client=client,
                                         cloud_admin=admin))
    assert details["source"] == "cloud" and details["bucket"] == rec["bucket"]
    assert details["key"] == rec["key"]
    assert keys_left() == []
    restored = Database(data_dir / DB_FILENAME)
    try:
        assert restored.settings.get("marker") == "in-the-cloud-backup"
        (row,) = [a for a in restored.audit.query(limit=500) if a["action"] == "system.restore"]
        assert row["target"] == rec["bucket"] and row["details"]["source"] == "cloud"
        # the restored database records where its newest backup went
        restored.settings.set("backup_runs", [{"id": 1, "outcome": "ok",
                                               "destination": "cloud"}])
        restored.settings.set("backup", {"awsBucket": rec["bucket"]})
    finally:
        restored.close()

    # without --from: the live database says cloud and names the bucket
    assert backup.live_backup_state(data_dir / DB_FILENAME) == {
        "awsBucket": rec["bucket"], "lastDestination": "cloud"}
    details = asyncio.run(backup.restore(settings, rec["key"], client=client,
                                         cloud_admin=admin))
    assert details["source"] == "cloud" and keys_left() == []


def test_restore_source_defaults_and_errors(cenv, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "app_is_running", lambda s: False)
    assert backup.live_backup_state(tmp_path / "none.sqlite3") == {
        "awsBucket": None, "lastDestination": None}
    broken = tmp_path / "broken.sqlite3"
    sqlite3.connect(broken).close()
    assert backup.live_backup_state(broken)["lastDestination"] is None
    settings = cenv.settings.model_copy(update={"data_dir": str(tmp_path / "d")})
    with pytest.raises(backup.BackupError, match="--from"):
        asyncio.run(backup.restore(settings, "latest", source="mars"))
    with pytest.raises(backup.BackupError, match="CLOUD_"):  # cloud not configured
        asyncio.run(backup.restore(settings, "latest", source="cloud"))
    admin = cenv.app.state.cloud
    client = admin_client(settings, cenv.mock, cenv.transport)
    with pytest.raises(backup.BackupError, match="No backup bucket found"):
        asyncio.run(backup.restore(settings, "latest", source="cloud", client=client,
                                   cloud_admin=admin))
    for name in ("bucketeer-backup-00000000000a", "bucketeer-backup-00000000000b"):
        s3().create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
    with pytest.raises(backup.BackupError, match="--aws-bucket"):
        asyncio.run(backup.restore(settings, "latest", source="cloud", client=client,
                                   cloud_admin=admin))
    with pytest.raises(backup.BackupError, match="No backup found"):
        asyncio.run(backup.restore(settings, "latest", source="cloud", client=client,
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
                    "aws_bucket": "bucketeer-backup-x"}


# -- the backup user is never a person --------------------------------------------------------


def test_reserved_usernames(tmp_path, aws):
    # backup/billing (P6-10): a person's buckets would sit next to the system buckets
    assert cloud.reserved_usernames() == {"bucketeer-admin", "bucketeer-backup", "backup",
                                          "billing"}
    admin = CloudAdmin(make_settings(tmp_path, **CLOUD))
    assert admin.backup_user_name() == USER
    for name in ("bucketeer-backup", "Bucketeer-Backup", "bucketeer-admin"):
        with pytest.raises(CloudError) as exc:
            admin.user_name(name)
        assert exc.value.status == 409 and exc.value.code == "reserved"
    assert admin.user_name("alice") == "s3-alice"
    assert NAME_RE.fullmatch(admin.new_backup_bucket_name())


def test_session_refuses_when_two_keys_are_held(tmp_path, aws, monkeypatch):
    admin = CloudAdmin(make_settings(tmp_path, **CLOUD))
    db = Database(":memory:")
    try:
        dest = backup.AwsDestination(admin, db)
        asyncio.run(dest.ensure(1))
        held = [iam().create_access_key(UserName=USER)["AccessKey"]["AccessKeyId"]
                for _ in range(2)]
        monkeypatch.setattr(backup, "_HELD_KEYS", set(held))
        with pytest.raises(backup.BackupError, match="two keys"):
            asyncio.run(dest.upload("backup/x", Path(tmp_path / "nothing")))
        assert {k["AccessKeyId"] for k in keys_left()} == set(held)  # never another's key
    finally:
        db.close()
