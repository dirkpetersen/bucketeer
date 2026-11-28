"""Nightly backup (bucketeer/backup.py) against moto and both admin backends."""

import asyncio
import base64
import hashlib
import io
import json
import re
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from bucketeer import backup
from bucketeer.config import Settings
from bucketeer.croit import BACKUP_UID
from bucketeer.db import DB_FILENAME, MIGRATIONS, Database

from .backends import admin_backend, admin_client  # noqa: F401
from .conftest import REQUIRED
from .test_auth import USERS_GROUP
from .test_buckets import Env, H

BUCKET = "bucketeer-0123456789ab"
ENV_TEXT = "PORT=8000\nSESSION_SECRET=very-secret-session-value\n"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    p = tmp_path / "repo" / ".env"
    p.parent.mkdir()
    p.write_text(ENV_TEXT)
    monkeypatch.setattr(backup, "ENV_FILE", p)
    return p


@pytest.fixture
def env(tmp_path, s3_endpoint, env_file):
    e = Env(tmp_path, s3_endpoint, backup_bucket=BUCKET)
    e.backup_keys = []
    real = e.app.state.backup.factory

    def spy(endpoint, access, secret, settings):
        e.backup_keys.append(access)
        return real(endpoint, access, secret, settings)

    e.app.state.backup.factory = spy
    yield e
    e.db.close()


def run_now(env):
    r = env.bob.post("/api/admin/backup/run", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def backup_user_keys(env):
    return {k["access_key"] for k in env.croit_state.users[BACKUP_UID].keys}


def fetch(env, key):
    body = env.moto().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    path = env.settings.data_dir + "-dl.tar.gz"
    Path(path).write_bytes(body)
    return backup.read_archive(Path(path))


# -- the run ------------------------------------------------------------------------------


def test_run_creates_user_bucket_and_archive(env):
    rec = run_now(env)
    assert rec["outcome"] == "ok", rec
    assert re.fullmatch(r"backup/bucketeer-\d{4}-\d\d-\d\dT\d{4}Z\.tar\.gz", rec["key"])
    assert rec["trigger"] == "manual" and rec["actor"] == "bob" and rec["envIncluded"] is True
    # the backup user's own key signed every S3 call, never a person's
    assert BACKUP_UID in env.croit_state.users
    assert env.backup_keys and set(env.backup_keys) <= backup_user_keys(env)
    s3 = env.moto()
    lock = s3.get_object_lock_configuration(Bucket=BUCKET)["ObjectLockConfiguration"]
    assert lock["ObjectLockEnabled"] == "Enabled"
    ret = lock["Rule"]["DefaultRetention"]
    assert (ret["Mode"], ret["Days"]) == ("COMPLIANCE", 30)
    assert s3.get_bucket_versioning(Bucket=BUCKET)["Status"] == "Enabled"
    rules = {r["ID"]: r for r in s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]}
    assert set(rules) == set(backup.RULE_IDS)
    assert rules["bucketeer-backup-expire"]["Expiration"]["Days"] == 31
    assert rules["bucketeer-backup-noncurrent"]["NoncurrentVersionExpiration"][
        "NoncurrentDays"] == 1
    assert rules["bucketeer-backup-markers"]["Expiration"]["ExpiredObjectDeleteMarker"] is True
    head = s3.head_object(Bucket=BUCKET, Key=rec["key"])
    assert head["ObjectLockMode"] == "COMPLIANCE" and head["ContentLength"] == rec["bytes"]

    meta, files = fetch(env, rec["key"])
    assert set(files) == {DB_FILENAME, ".env"}
    assert files[".env"].decode() == ENV_TEXT
    assert meta["schemaVersion"] == env.db.version == rec["schemaVersion"]
    assert meta["tables"]["audit_log"] >= 1 and "settings" in meta["tables"]
    assert meta["envIncluded"] is True and meta["format"] == 1
    snap = Path(env.settings.data_dir + "-snap.sqlite3")
    snap.write_bytes(files[DB_FILENAME])
    conn = sqlite3.connect(snap)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'alice'").fetchone()[0]
    conn.close()

    (row,) = [a for a in env.db.audit.query(limit=100) if a["action"] == "backup.run"]
    assert row["actor"] == "bob" and row["target_type"] == "backup" and row["target"] == BUCKET
    assert row["details"]["bucketCreated"] is True and row["details"]["key"] == rec["key"]
    assert "very-secret" not in json.dumps(row) and "very-secret" not in json.dumps(rec)


def test_second_run_is_idempotent_and_history_is_capped(env):
    first = run_now(env)
    keys_before = backup_user_keys(env)
    second = run_now(env)
    assert second["outcome"] == "ok" and second["id"] == first["id"] + 1
    assert backup_user_keys(env) == keys_before  # no second key, no second user
    got = env.bob.get("/api/admin/backup").json()
    assert [r["id"] for r in got["runs"]] == [second["id"], first["id"]]
    assert got["lastRun"]["id"] == got["lastSuccess"]["id"] == second["id"]
    env.db.settings.set(backup.RUNS_KEY, [{"id": i, "outcome": "ok", "actor": "x"}
                                          for i in range(40, 10, -1)])
    rec = run_now(env)
    runs = env.db.settings.get(backup.RUNS_KEY)
    assert len(runs) == backup.KEEP_RUNS and runs[0]["id"] == rec["id"] == 41


def test_missing_env_file_is_noted(env, env_file):
    env_file.unlink()
    rec = run_now(env)
    assert rec["outcome"] == "ok" and rec["envIncluded"] is False and ".env" in rec["note"]
    meta, files = fetch(env, rec["key"])
    assert set(files) == {DB_FILENAME} and meta["note"]


def test_failed_run_is_recorded_and_audited(env):
    def broken(*a):
        raise RuntimeError("no S3 for you")

    env.app.state.backup.factory = broken
    rec = run_now(env)
    assert rec["outcome"] == "failed" and rec["error"] == "onprem: RuntimeError"
    assert rec["destinations"][0]["outcome"] == "failed"
    (row,) = [a for a in env.db.audit.query(limit=100) if a["action"] == "backup.failed"]
    assert row["target_type"] == "backup" and row["details"]["error"] == "RuntimeError"
    assert env.bob.get("/api/admin/backup").json()["lastSuccess"] is None


def test_successful_run_removes_pre_upgrade_copies(env, tmp_path):
    db_file = tmp_path / "file" / DB_FILENAME
    file_db = Database(db_file)
    copy = db_file.with_name(f"{DB_FILENAME}.pre-v3-20260101T000000Z")
    copy.write_bytes(b"x")
    env.app.state.db = file_db  # the route's get_db reads app.state.db
    try:
        assert run_now(env)["outcome"] == "ok"
    finally:
        env.app.state.db = env.db
        file_db.close()
    assert not copy.exists()


def test_scheduled_run_is_by_system(env):
    svc = env.app.state.backup
    clock = [datetime(2026, 3, 1, 2, 58)]

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        if env.db.settings.get(backup.RUNS_KEY):
            raise asyncio.CancelledError

    ran_at = []
    real_run = svc.run

    async def run(trigger, actor):
        ran_at.append(clock[0])
        return await real_run(trigger, actor)

    svc.now, svc.sleep, svc.run = (lambda: clock[0]), sleep, run
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.schedule_loop())
    (rec,) = env.db.settings.get(backup.RUNS_KEY)
    assert rec["trigger"] == "schedule" and rec["actor"] == "system" and rec["outcome"] == "ok"
    assert ran_at == [datetime(2026, 3, 1, 3, 0)]
    (row,) = [a for a in env.db.audit.query(limit=100) if a["action"] == "backup.run"]
    assert row["actor"] == "system"


def test_one_run_at_a_time(env):
    env.app.state.backup.running = True
    r = env.bob.post("/api/admin/backup/run", headers=H)
    assert r.status_code == 409


def test_next_run():
    assert backup.next_run(datetime(2026, 1, 1, 2, 0), "03:00") == datetime(2026, 1, 1, 3, 0)
    assert backup.next_run(datetime(2026, 1, 1, 3, 0), "03:00") == datetime(2026, 1, 2, 3, 0)


# -- admin API ----------------------------------------------------------------------------


def test_state_before_and_after_the_first_run(env):
    got = env.bob.get("/api/admin/backup").json()
    assert got["configured"] is True and got["bucket"] == BUCKET and got["suggestedBucket"] is None
    assert got["settings"] == {"time": "03:00", "retentionDays": 30, "restoreTestDays": 30}
    assert got["destinations"][0]["state"]["exists"] is False and got["runs"] == []
    assert got["awsBucket"] is None  # no cloud: one destination only
    assert BACKUP_UID not in env.croit_state.users  # a GET creates nothing
    run_now(env)
    st = env.bob.get("/api/admin/backup").json()["destinations"][0]["state"]
    assert st == {"exists": True, "objectLock": True, "mode": "COMPLIANCE",
                  "retentionDays": 30, "versioning": "enabled"}


def test_admin_only(env):
    assert env.alice.get("/api/admin/backup").status_code == 403
    assert env.alice.post("/api/admin/backup/run", headers=H).status_code == 403
    assert env.bob.post("/api/admin/backup/run").status_code == 403  # CSRF header


def test_settings_validation_and_audit(env):
    put = lambda body: env.bob.put("/api/admin/backup/settings", json=body, headers=H)  # noqa: E731
    assert put({"time": "25:00"}).status_code == 422
    assert put({"retentionDays": 0}).status_code == 422
    assert put({"other": 1}).status_code == 422
    assert put({"restoreTestDays": -1}).status_code == 422
    assert put({"restoreTestDays": 366}).status_code == 422
    r = put({"time": "01:15", "retentionDays": 45})
    assert r.status_code == 200 and r.json()["settings"] == {
        "time": "01:15", "retentionDays": 45, "restoreTestDays": 30}
    r = put({"retentionDays": 40})
    assert r.status_code == 422 and "lengthened" in r.json()["detail"]
    rows = [a for a in env.db.audit.query(limit=100) if a["action"] == "admin.settings.backup"]
    assert len(rows) == 1 and rows[0]["target_type"] == "setting"
    assert rows[0]["details"]["before"]["retentionDays"] == 30
    s3 = env.moto()  # applied at once: the bucket is created with the new period
    lock = s3.get_object_lock_configuration(Bucket=BUCKET)["ObjectLockConfiguration"]
    assert lock["Rule"]["DefaultRetention"]["Days"] == 45
    rules = {r["ID"]: r for r in s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]}
    assert rules["bucketeer-backup-expire"]["Expiration"]["Days"] == 46
    assert put({"retentionDays": 50}).status_code == 200
    lock = s3.get_object_lock_configuration(Bucket=BUCKET)["ObjectLockConfiguration"]
    assert lock["Rule"]["DefaultRetention"]["Days"] == 50
    assert run_now(env)["destinations"][0]["retentionDays"] == 50
    # moto refuses a lock change on a bucket with objects: the save still counts, with a warning
    r = put({"retentionDays": 60})
    assert r.status_code == 200 and "next backup run" in r.json()["warning"]
    assert env.bob.get("/api/admin/backup").json()["settings"]["retentionDays"] == 60


def test_backups_off(tmp_path, s3_endpoint, env_file):
    e = Env(tmp_path, s3_endpoint)
    try:
        got = e.bob.get("/api/admin/backup").json()
        assert got["configured"] is False and got["destinations"][0]["state"] is None
        assert re.fullmatch(r"bucketeer-backup-[0-9a-f]{12}", got["suggestedBucket"])
        assert e.app.state.backup.describe_off().endswith(got["suggestedBucket"])
        assert e.bob.post("/api/admin/backup/run", headers=H).status_code == 409
        assert e.bob.post("/api/admin/backup/restore-test", headers=H).status_code == 409
        assert got["nextRestoreTest"] is None and got["restoreTests"] == []
    finally:
        e.db.close()


def test_backup_bucket_setting_is_validated():
    for bad in ("Bucketeer-X", "a.b.c", "ab", "-bad-"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, backup_bucket=bad, **REQUIRED)
    assert Settings(_env_file=None, backup_bucket=" bucketeer-ok ", **REQUIRED).backup_bucket \
        == "bucketeer-ok"


# -- the backup user is not a person --------------------------------------------------------


def test_backup_user_hidden_and_refused(env):
    run_now(env)
    raw = env.croit_state.buckets
    raw = raw.values() if isinstance(raw, dict) else raw
    assert any(b.owner == BACKUP_UID for b in raw)  # the cluster has it; the lists don't
    users = env.bob.get("/api/admin/users").json()
    users = users["users"] if isinstance(users, dict) else users
    assert BACKUP_UID not in {u["uid"] for u in users}
    buckets = env.bob.get("/api/admin/buckets").json()
    buckets = buckets["buckets"] if isinstance(buckets, dict) else buckets
    assert all(BUCKET not in b["name"] and b.get("owner") != BACKUP_UID for b in buckets)
    names = asyncio.run(env.app.state.croit.list_user_names())
    assert BACKUP_UID not in names and "alice" in names
    listed = asyncio.run(env.app.state.croit.list_buckets())
    assert all(b.get("owner") != BACKUP_UID for b in listed)
    # an AD account of that name never reaches it
    env.fake.add(BACKUP_UID, groups={USERS_GROUP}, display="Backup Person",
                 mail="bp@example.org")
    acct = env.session(BACKUP_UID).get("/api/account").json()
    assert acct["status"] == "creation_failed" and acct["reason"] == "invalid_username"
    # nor an admin mapping, a quota change or a suspension
    r = env.bob.put("/api/admin/settings/uid-mappings/erin", json={"rgwUid": BACKUP_UID},
                    headers=H)
    assert r.status_code == 422
    r = env.bob.post(f"/api/admin/users/{BACKUP_UID}/suspend", headers=H)
    assert r.status_code == 409


# -- archive checks and restore ---------------------------------------------------------------


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_read_archive_refuses_damage(tmp_path):
    db, env_ = b"SQLite", b"A=1\n"
    meta = {"files": {DB_FILENAME: {"sha256": backup._sha256(db)},
                      ".env": {"sha256": backup._sha256(b"other")}}}
    p = tmp_path / "a.tar.gz"
    p.write_bytes(_tar({"meta.json": json.dumps(meta).encode(), DB_FILENAME: db, ".env": env_}))
    with pytest.raises(backup.BackupError, match="SHA-256 mismatch for .env"):
        backup.read_archive(p)
    p.write_bytes(_tar({"meta.json": b"{}", DB_FILENAME: db, "../evil": b"x"}))
    with pytest.raises(backup.BackupError, match="Unexpected"):
        backup.read_archive(p)
    p.write_bytes(b"not a tar")
    with pytest.raises(backup.BackupError):
        backup.read_archive(p)


def test_restore_roundtrip(env, env_file, tmp_path, monkeypatch, capsys):
    env.db.settings.set("marker", "from-the-backup")
    rec = run_now(env)
    run_now(env)  # a second archive in the same minute: "latest" still finds one
    data_dir = tmp_path / "restore-data"
    live = Database(data_dir / DB_FILENAME)
    live.settings.set("marker", "live-before-restore")
    live.close()
    settings = env.settings.model_copy(update={"data_dir": str(data_dir)})
    monkeypatch.setattr(backup, "app_is_running", lambda s: True)
    client = admin_client(settings, env.mock, env.transport)
    with pytest.raises(backup.BackupError, match="stop it first"):
        asyncio.run(backup.restore(settings, "latest", client=client))
    monkeypatch.setattr(backup, "app_is_running", lambda s: False)
    with pytest.raises(backup.BackupError, match="object key"):
        asyncio.run(backup.restore(settings, "../etc/passwd", client=client))

    details = asyncio.run(backup.restore(settings, rec["key"], client=client))
    # the real catch-up (bucketeer.rebuild, fill-gaps) ran against the restored database
    assert details["catchUp"] == "done", details
    assert details["catchUpResult"]["mode"] == "fill_gaps"
    assert set(details["catchUpResult"]) == {"mode", "added", "skipped", "problems",
                                             "problemReasons", "problemItems"}
    assert "Rebuild from Ceph (fill gaps):" in capsys.readouterr().out  # rebuild.summary
    restored = Database(data_dir / DB_FILENAME)
    try:
        assert restored.settings.get("marker") == "from-the-backup"
        (row,) = [a for a in restored.audit.query(limit=500) if a["action"] == "system.restore"]
        assert row["actor"] == "system" and row["details"]["key"] == rec["key"]
    finally:
        restored.close()
    (kept,) = data_dir.glob(f"{DB_FILENAME}.replaced-*")
    old = sqlite3.connect(kept)
    assert "live-before-restore" in old.execute(
        "SELECT value FROM settings WHERE key = 'marker'").fetchone()[0]
    old.close()
    (written,) = env_file.parent.glob(".env.restored-*")
    assert written.read_text() == ENV_TEXT and written.stat().st_mode & 0o777 == 0o600
    assert env_file.read_text() == ENV_TEXT  # never written over

    # "latest" picks the newest archive and a catch-up hook is called when one exists
    import types

    calls = []
    mod = types.ModuleType("bucketeer.rebuild")

    def rebuild_from_ceph(fill_gaps, db):
        calls.append((fill_gaps, db.version))
        return {"buckets": 0}

    mod.rebuild_from_ceph = rebuild_from_ceph
    monkeypatch.setitem(__import__("sys").modules, "bucketeer.rebuild", mod)
    details = asyncio.run(backup.restore(settings, "latest", client=client))
    assert details["catchUp"] == "done" and calls and calls[0][0] is True
    assert details["catchUpResult"] == {"buckets": 0}


def test_restore_command_usage():
    assert backup.restore_main(["--restore-backup"]) == 2
    assert backup.restore_main(["--restore-backup", "--force"]) == 2


def test_catch_up_digest_keeps_counts_and_fifty_problems():
    report = {"mode": "fill_gaps", "added": {"buckets": 3}, "skipped": [{}] * 7,
              "problems": [{"table": "buckets", "item": f"b{i}", "reason": "no_owner",
                            "detail": "x" * 100} for i in range(500)]}
    got = backup.catch_up_digest(report)
    assert got["problems"] == 500 and got["skipped"] == 7 and got["added"] == {"buckets": 3}
    assert got["problemReasons"] == {"no_owner": 500} and len(got["problemItems"]) == 50
    assert got["problemItems"][0] == "buckets b0" and len(json.dumps(got)) < 2000
    assert backup.catch_up_digest({"buckets": 0}) == {"buckets": 0}
    assert len(backup.catch_up_digest(["y" * 50] * 100)) == 2000


# -- restore from another on-premises bucket (--backup-bucket) ----------------------------------

OLD = "bucketeer-old-backups"


def put_archive(s3, bucket, key, body):
    md5 = base64.b64encode(hashlib.md5(body).digest()).decode()  # object lock wants it
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentMD5=md5)


def test_restore_from_another_onprem_bucket(env, tmp_path, monkeypatch):
    env.db.settings.set("marker", "in-the-old-bucket")
    rec = run_now(env)
    s3 = env.moto()
    s3.create_bucket(Bucket=OLD)
    s3.put_object(Bucket=OLD, Key=rec["key"],
                  Body=s3.get_object(Bucket=BUCKET, Key=rec["key"])["Body"].read())
    env.db.settings.set("marker", "in-the-current-bucket")
    run_now(env)  # BACKUP_BUCKET now holds a newer archive
    monkeypatch.setattr(backup, "app_is_running", lambda s: False)
    data_dir = tmp_path / "restore-data"
    # BACKUP_BUCKET was renamed (here: emptied): the option still reaches the older bucket
    settings = env.settings.model_copy(update={"data_dir": str(data_dir), "backup_bucket": ""})
    client = admin_client(settings, env.mock, env.transport)
    with pytest.raises(backup.BackupError, match="--backup-bucket NAME"):
        asyncio.run(backup.restore(settings, "latest", client=client))
    with pytest.raises(backup.BackupError, match="--aws-bucket"):
        asyncio.run(backup.restore(settings, "latest", source="cloud", backup_bucket=OLD,
                                   client=client))
    with pytest.raises(backup.BackupError, match="lowercase"):
        asyncio.run(backup.restore(settings, "latest", backup_bucket="Old.Backups",
                                   client=client))
    details = asyncio.run(backup.restore(settings, "latest", source="onprem",
                                         backup_bucket=OLD, client=client))
    assert (details["bucket"], details["source"], details["key"]) == (OLD, "onprem", rec["key"])
    restored = Database(data_dir / DB_FILENAME)
    try:
        assert restored.settings.get("marker") == "in-the-old-bucket"
        (row,) = [a for a in restored.audit.query(limit=500) if a["action"] == "system.restore"]
        assert row["target"] == OLD
    finally:
        restored.close()


def test_restore_command_backup_bucket_option(monkeypatch, capsys):
    seen = {}

    async def fake(settings, which, **kw):
        seen.update(which=which, **kw)

    monkeypatch.setattr(backup, "restore", fake)
    monkeypatch.setattr("bucketeer.config.load_settings", lambda: None)
    main = backup.restore_main
    assert main(["--restore-backup", "latest", "--backup-bucket"]) == 2
    assert main(["--restore-backup", "latest", "--backup-bucket", "--force"]) == 2
    assert main(["--restore-backup", "latest", "--backup-bucket", "Old.Backups"]) == 2
    assert main(["--restore-backup", "latest", "--from", "cloud", "--backup-bucket", OLD]) == 2
    out = capsys.readouterr().out
    assert "--aws-bucket" in out and "lowercase" in out and "--backup-bucket NAME" in out
    assert seen == {}  # refused before the settings are even read
    assert main(["--restore-backup", "latest", "--from", "onprem", "--backup-bucket", OLD]) == 0
    assert seen == {"which": "latest", "force": False, "source": "onprem", "aws_bucket": None,
                    "backup_bucket": OLD}
    assert main(["--restore-backup", "latest", "--backup-bucket", OLD, "--force"]) == 0
    assert seen["source"] is None and seen["backup_bucket"] == OLD and seen["force"] is True


# -- restore test ------------------------------------------------------------------------------


def restore_test(env):
    r = env.bob.post("/api/admin/backup/restore-test", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_restore_test_proves_the_newest_archive(env, env_file):
    first = restore_test(env)  # nothing written yet
    assert first["outcome"] == "failed" and first["trigger"] == "manual"
    (d,) = first["destinations"]
    assert d["kind"] == "onprem" and d["bucket"] == BUCKET and "No backup found" in d["error"]
    env.db.settings.set("marker", "tested")
    rec = run_now(env)
    got = restore_test(env)
    assert got["outcome"] == "ok" and got["actor"] == "bob" and got["id"] == first["id"] + 1
    (d,) = got["destinations"]
    assert d["outcome"] == "ok" and d["error"] is None and d["key"] == rec["key"]
    assert d["schemaVersion"] == env.db.version == len(MIGRATIONS)
    assert d["tables"]["settings"] >= 1 and d["tables"]["audit_log"] >= 1
    assert d["envIncluded"] is True and d["checkedAt"] and d["createdAt"]
    # nothing live was touched, and the temporary folder is gone
    assert env.db.settings.get("marker") == "tested"
    assert env_file.read_text() == ENV_TEXT and not list(env_file.parent.glob(".env.restored-*"))
    data = Path(env.settings.data_dir)
    assert data.is_dir() and not list(data.iterdir())
    st = env.bob.get("/api/admin/backup").json()
    assert [t["id"] for t in st["restoreTests"]] == [got["id"], first["id"]]
    assert st["testing"] is False and st["nextRestoreTest"]
    rows = [a for a in env.db.audit.query(limit=200)
            if a["action"].startswith("backup.restore_test")]
    assert [r["action"] for r in rows] == ["backup.restore_test", "backup.restore_test_failed"]
    assert rows[0]["actor"] == "bob" and rows[0]["target_type"] == "backup"
    assert rows[0]["target"] == BUCKET and rows[0]["details"]["destination"] == "onprem"
    assert rows[0]["details"]["key"] == rec["key"]
    assert "very-secret" not in json.dumps(rows) and "very-secret" not in json.dumps(st)


def test_restore_test_catches_a_damaged_newest_archive(env):
    rec = run_now(env)
    put_archive(env.moto(), BUCKET, rec["key"], b"not a backup archive")  # a newer version
    got = restore_test(env)
    (d,) = got["destinations"]
    assert got["outcome"] == "failed" and "Not a readable backup archive" in d["error"]
    assert got["error"] == f"onprem: {d['error']}" and d["key"] == rec["key"]


def test_restore_test_one_at_a_time_and_admin_only(env):
    svc = env.app.state.backup
    assert env.alice.post("/api/admin/backup/restore-test", headers=H).status_code == 403
    assert env.bob.post("/api/admin/backup/restore-test").status_code == 403  # CSRF header
    svc.running = True
    r = env.bob.post("/api/admin/backup/restore-test", headers=H)
    assert r.status_code == 409 and "backup is running" in r.json()["detail"]
    svc.running, svc.testing = False, True
    r = env.bob.post("/api/admin/backup/restore-test", headers=H)
    assert r.status_code == 409 and "restore test is running" in r.json()["detail"]
    assert env.db.settings.get(backup.TESTS_KEY) is None


def repack(src, out, db=None, meta=None):
    """`src` with the database bytes and meta.json changed; the SHA-256 values match again,
    so only the database checks can catch it."""
    m, files = backup.read_archive(src)
    if db:
        files[DB_FILENAME] = db(files[DB_FILENAME])
    m["files"] = {n: {"sha256": backup._sha256(b), "bytes": len(b)} for n, b in files.items()}
    if meta:
        meta(m)
    out.write_bytes(_tar({"meta.json": json.dumps(m).encode(), **files}))
    return out


def newer_schema(data, where):
    p = where / "newer.sqlite3"
    p.write_bytes(data)
    conn = sqlite3.connect(p)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.commit()
    conn.close()
    out = p.read_bytes()
    p.unlink()
    return out


def test_check_archive_refuses_what_could_not_be_restored(tmp_path):
    src_db = Database(":memory:")
    src_db.settings.set("marker", "x")
    env_ = tmp_path / ".env"
    env_.write_text(ENV_TEXT)
    work = tmp_path / "w"
    work.mkdir()
    path, meta = backup.build_archive(src_db, env_, work, datetime(2026, 3, 1))
    src_db.close()
    got = backup.check_archive(path, work)
    assert got["schemaVersion"] == len(MIGRATIONS) == meta["schemaVersion"]
    assert got["tables"] == meta["tables"] and got["envIncluded"] is True
    bad = tmp_path / "bad.tar.gz"
    cases = [
        ({"db": lambda b: b[:4096] + bytes(len(b) - 4096)}, "integrity check|can't be"),
        ({"db": lambda b: b"definitely not sqlite " * 300}, "can't be opened|integrity"),
        ({"db": lambda b: newer_schema(b, tmp_path),
          "meta": lambda m: m.update(schemaVersion=len(MIGRATIONS) + 1)},
         "newer than this version"),
        ({"meta": lambda m: m.update(schemaVersion=1)}, "meta.json says schema v1"),
        ({"meta": lambda m: m["tables"].update(settings=999)}, "row counts"),
    ]
    for change, message in cases:
        repack(path, bad, **change)
        with pytest.raises(backup.BackupError, match=message):
            backup.check_archive(bad, work)


def run_schedule_once(env, start):
    """The schedule loop on a fake clock until the first scheduled run is recorded."""
    svc = env.app.state.backup
    clock = [start]

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        if env.db.settings.get(backup.RUNS_KEY):
            raise asyncio.CancelledError

    svc.now, svc.sleep = (lambda: clock[0]), sleep
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.schedule_loop())


def old_test(at, outcome="ok"):
    stamp = at.astimezone().isoformat(timespec="seconds")
    return {"id": 1, "startedAt": stamp, "finishedAt": stamp, "trigger": "manual",
            "actor": "bob", "outcome": outcome, "destinations": [], "error": None}


@pytest.mark.parametrize("days, age, outcome, expect", [
    (30, None, None, True),  # never tested
    (30, 30, "ok", True),  # 30 calendar days ago
    (30, 29, "ok", False),
    (30, 1, "failed", True),  # a failed test is tried again after the next good backup
    (0, None, None, False),  # automatic tests off
])
def test_restore_test_after_a_scheduled_backup(env, days, age, outcome, expect):
    start = datetime(2026, 3, 1, 2, 58)
    env.db.settings.set(backup.KEY, {"restoreTestDays": days})
    if age is not None:
        env.db.settings.set(backup.TESTS_KEY, [old_test(start - timedelta(days=age), outcome)])
    run_schedule_once(env, start)
    tests = env.db.settings.get(backup.TESTS_KEY) or []
    if expect:
        assert (tests[0]["trigger"], tests[0]["actor"], tests[0]["outcome"]) == (
            "schedule", "system", "ok")
        (row,) = [a for a in env.db.audit.query(limit=200) if a["action"] == "backup.restore_test"]
        assert row["actor"] == "system"
    else:
        assert len(tests) == (0 if age is None else 1)


def test_no_restore_test_after_a_failed_scheduled_backup(env):
    def broken(*a):
        raise RuntimeError("no S3 for you")

    env.app.state.backup.factory = broken
    run_schedule_once(env, datetime(2026, 3, 1, 2, 58))
    assert env.db.settings.get(backup.RUNS_KEY)[0]["outcome"] == "failed"
    assert env.db.settings.get(backup.TESTS_KEY) is None


def test_next_restore_test(env):
    svc = env.app.state.backup
    now = datetime(2026, 3, 1, 12, 0)
    svc.now = lambda: now
    put = lambda tests: env.db.settings.set(backup.TESTS_KEY, tests)  # noqa: E731
    assert svc.next_restore_test() == datetime(2026, 3, 2, 3, 0)  # never tested: next backup
    put([old_test(now - timedelta(days=10))])
    assert svc.next_restore_test() == datetime(2026, 3, 21, 3, 0)  # 30 days after that one
    assert svc.restore_test_due() is False
    put([old_test(now - timedelta(days=40))])
    assert svc.next_restore_test() == datetime(2026, 3, 2, 3, 0)  # overdue: the next backup
    put([old_test(now - timedelta(days=1), "failed")])
    assert svc.next_restore_test() == datetime(2026, 3, 2, 3, 0) and svc.restore_test_due()
    env.db.settings.set(backup.KEY, {"restoreTestDays": 7, "time": "01:30"})
    put([old_test(now)])
    assert svc.next_restore_test() == datetime(2026, 3, 8, 1, 30)
    env.db.settings.set(backup.KEY, {"restoreTestDays": 0})
    assert svc.next_restore_test() is None and svc.restore_test_due() is False
