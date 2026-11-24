"""Nightly backup (bucketeer/backup.py) against moto and both admin backends."""

import asyncio
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
from bucketeer.db import DB_FILENAME, Database

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
    assert rec["outcome"] == "failed" and rec["error"] == "RuntimeError"
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
    assert got["settings"] == {"time": "03:00", "retentionDays": 30}
    assert got["bucketState"]["exists"] is False and got["runs"] == []
    assert BACKUP_UID not in env.croit_state.users  # a GET creates nothing
    run_now(env)
    st = env.bob.get("/api/admin/backup").json()["bucketState"]
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
    r = put({"time": "01:15", "retentionDays": 45})
    assert r.status_code == 200 and r.json()["settings"] == {"time": "01:15", "retentionDays": 45}
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
    assert run_now(env)["retentionDays"] == 50
    # moto refuses a lock change on a bucket with objects: the save still counts, with a warning
    r = put({"retentionDays": 60})
    assert r.status_code == 200 and "next backup run" in r.json()["warning"]
    assert env.bob.get("/api/admin/backup").json()["settings"]["retentionDays"] == 60


def test_backups_off(tmp_path, s3_endpoint, env_file):
    e = Env(tmp_path, s3_endpoint)
    try:
        got = e.bob.get("/api/admin/backup").json()
        assert got["configured"] is False and got["bucketState"] is None
        assert re.fullmatch(r"bucketeer-[0-9a-f]{12}", got["suggestedBucket"])
        assert e.app.state.backup.describe_off().endswith(got["suggestedBucket"])
        assert e.bob.post("/api/admin/backup/run", headers=H).status_code == 409
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
    assert "Catch-up from Ceph: done" in capsys.readouterr().out
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
