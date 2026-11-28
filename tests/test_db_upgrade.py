"""Upgrading an older database: every past schema version, safety copy, Python steps,
failure message. Databases at each version are built from `MIGRATIONS[:n]`, so a new
migration is covered without editing this file (add SAMPLES rows for its new tables)."""

import sqlite3
import stat

import pytest

from bucketeer import db as dbmod
from bucketeer.db import DB_FILENAME, MIGRATIONS, Database, MigrationFailed

LATEST = len(MIGRATIONS)
T = "2026-01-01T00:00:00+00:00"


def created_in(table: str) -> int:
    """The migration that creates `table`, found by its text (migrations appended by parallel
    work get their number only when merged, so newer samples don't hard-code it)."""
    return next(n for n, m in enumerate(MIGRATIONS, 1)
                if isinstance(m, str) and f"CREATE TABLE {table} (" in m)


# (version that created the table, table, row using only that version's columns)
SAMPLES: list[tuple[int, str, dict]] = [
    (1, "settings", {"key": "naming", "value": '{"prefix": ""}', "updated_at": T}),
    (1, "uid_mappings", {"ad_username": "Alice", "rgw_uid": "alice", "created_at": T}),
    (1, "user_profiles", {"ad_username": "alice", "default_budget_number": "DN099999",
                          "advanced_mode": 1, "updated_at": T}),
    (1, "buckets", {"name": "alice-data", "owner_uid": "alice", "budget_number": "DN1",
                    "created_at": T}),
    (1, "bucket_keys", {"id": 7, "rgw_uid": "alice+backup", "owner_ad_username": "alice",
                        "bucket_name": "alice-data", "access": "read", "label": "backup",
                        "created_at": T, "suspended_at": T}),
    (1, "audit_log", {"id": 3, "ts": T, "actor": "alice", "action": "bucket.create",
                      "target_type": "bucket", "target": "alice-data", "details": "{}"}),
    (7, "shares", {"id": 2, "bucket_name": "alice-data", "owner_uid": "alice",
                   "recipient_ad": "bob", "recipient_uid": "bob", "access": "readwrite",
                   "created_at": T, "created_by": "alice"}),
    (8, "known_accounts", {"ad_username": "alice", "rgw_uid": "alice", "source": "created",
                           "first_seen": T}),
    (8, "offboarding_runs", {"id": 1, "started_at": T, "mode": "report",
                             "trigger": "manual", "actor": "admin", "outcome": "ok"}),
    (9, "bucket_delete_jobs", {"id": 4, "bucket": "alice-old", "owner_uid": "alice",
                               "owner_ad": "alice", "state": "failed", "created_at": T,
                               "updated_at": T}),
    (10, "subuser_names", {"rgw_uid": "alice", "name": "ci", "created_at": T}),
    (11, "setup_tokens", {"id": 5, "token_hash": "ab" * 32, "ad_username": "alice",
                          "rgw_uid": "alice", "access_key": "AKIAEXAMPLE0000000001",
                          "os": "linux", "created_at": T, "expires_at": T}),
    (12, "usage_daily", {"day": "2026-01-01", "bucket": "alice-data", "owner": "alice",
                         "bytes": 1024}),
    (16, "quota_changes", {"id": 6, "ts": T, "person": "alice", "uid": "alice",
                           "bucket": "alice-data", "location": "onprem", "old_tib": 1,
                           "new_tib": 5, "increase": 4, "counted": 1, "actor": "alice"}),
    (17, "cloud_accounts", {"ad_username": "alice", "iam_user": "s3-alice",
                            "source": "created", "created_at": T}),
    (17, "cloud_key_suspensions", {"access_key": "AKIAEXAMPLE0000000002",
                                   "iam_user": "s3-alice", "ad_username": "alice",
                                   "suspended_by": "offboarding", "suspended_at": T}),
    (created_in("key_ages"), "key_ages", {"access_key": "AKIAEXAMPLE0000000003",
                                          "uid": "alice", "kind": "user", "first_seen": T,
                                          "created": 1, "reminded_at": T}),
]

# Values a later migration fills in for rows that existed before it:
# (migration, table, where, expected)
BACKFILLS: list[tuple[int, str, dict, dict]] = [
    (2, "bucket_keys", {"id": 7}, {"suspended_by": "direct"}),
    (3, "buckets", {"name": "alice-data"}, {"tag_synced": 1}),
    (4, "bucket_keys", {"id": 7}, {"state": "active", "access_key": None}),
    (5, "bucket_keys", {"id": 7}, {"statement_pending_cleanup": 0}),
    (14, "user_profiles", {"ad_username": "alice"}, {"location": None}),
    (15, "buckets", {"name": "alice-data"}, {"creation_known": 1}),
    (18, "buckets", {"name": "alice-data"}, {"location": "onprem", "storage_class": None,
                                             "quota_tib": None, "over_quota_alert_at": None}),
]
# (shares.location / recipient_iam, cloud sharing: the shares sample row exists only from v7
# on, so that backfill is checked in test_shares_location_backfill)
SHARES_LOCATION = next(i + 1 for i, m in enumerate(MIGRATIONS)
                       if isinstance(m, str) and "recipient_iam" in m)  # found, not hard-coded


def test_shares_location_backfill(tmp_path, monkeypatch):
    """Shares made before cloud sharing come through as on-premises rows, and the live index
    stays one row per bucket and uid on premises, per bucket and IAM user in the cloud."""
    p = tmp_path / DB_FILENAME
    build(p, SHARES_LOCATION - 1, monkeypatch)
    d = Database(p)
    assert match(d._conn, "shares", {"id": 2, "location": "onprem", "recipient_iam": None})
    assert d.shares.live_for("alice-data", "bob")["id"] == 2
    with pytest.raises(sqlite3.IntegrityError):
        d.shares.create("alice-data", "alice", "", "bob", "bob", "", "read", "alice")
    # a cloud row of the same name and uid is a different share; two per IAM user are not
    d.shares.create("alice-data", "alice", "", "bob", "bob", "", "read", "alice",
                    location="cloud", recipient_iam="s3-bob")
    with pytest.raises(sqlite3.IntegrityError):
        d.shares.create("alice-data", "alice", "", "bob", "bob", "", "read", "alice",
                        location="cloud", recipient_iam="s3-bob")
    assert [r["id"] for r in d.shares.list_live("alice-data")] == [2]
    d.close()
# (setup_tokens.cloud, v17, defaults to 0; its sample row exists only from v11 on, so the
# backfill is checked in test_setup_token_cloud_backfill)


def test_setup_token_cloud_backfill(tmp_path, monkeypatch):
    p = tmp_path / DB_FILENAME
    build(p, 16, monkeypatch)
    d = Database(p)
    assert match(d._conn, "setup_tokens", {"id": 5, "cloud": 0})
    d.close()


def test_usage_quota_bytes_backfill_from_v12(tmp_path, monkeypatch):
    """Production runs v12: its usage readings come through v13 with no quota (NULL)."""
    p = tmp_path / DB_FILENAME
    build(p, 12, monkeypatch)
    d = Database(p)
    assert d.version == LATEST
    row = d._conn.execute("SELECT bytes, quota_bytes FROM usage_daily WHERE bucket = "
                          "'alice-data'").fetchone()
    assert tuple(row) == (1024, None)
    d.close()


def build(path, version, monkeypatch):
    """A database at `version`, made by the migrations of that time, with sample rows."""
    with monkeypatch.context() as m:
        m.setattr(dbmod, "MIGRATIONS", MIGRATIONS[:version])
        Database(path).close()
    c = sqlite3.connect(path)
    with c:
        for since, table, row in SAMPLES:
            if since <= version:
                c.execute(f"INSERT INTO {table} ({', '.join(row)}) "
                          f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    assert c.execute("PRAGMA user_version").fetchone()[0] == version
    c.close()


def match(conn, table, where):
    sql = " AND ".join(f"{k} IS ?" for k in where)
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE {sql}",
                                          tuple(where.values()))]


def columns(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}


def copies(dirpath):
    return sorted(p for p in dirpath.iterdir() if ".pre-v" in p.name)


def test_samples_cover_every_table():
    d = Database(":memory:")
    tables = set(columns(d._conn))
    d.close()
    assert tables - {"sqlite_sequence"} == {t for _, t, _ in SAMPLES}


@pytest.mark.parametrize("version", range(1, LATEST))
def test_upgrade_from_every_past_version(tmp_path, monkeypatch, version):
    p = tmp_path / DB_FILENAME
    build(p, version, monkeypatch)
    d = Database(p)
    assert d.version == LATEST
    for since, table, row in SAMPLES:
        if since <= version:
            assert match(d._conn, table, row), (table, row)
    for n, table, where, expected in BACKFILLS:
        if version < n:
            assert match(d._conn, table, {**where, **expected}), (n, table, expected)
    fresh = Database(":memory:")
    assert columns(d._conn) == columns(fresh._conn)  # same schema as a new install
    fresh.close()
    d.settings.set("after", 1)  # repositories work on the upgraded file
    d.close()
    (copy,) = copies(tmp_path)
    assert copy.name.startswith(f"{DB_FILENAME}.pre-v{version}-")
    c = sqlite3.connect(copy)
    assert c.execute("PRAGMA user_version").fetchone()[0] == version
    assert c.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
    c.close()


def test_copy_mode_and_limit(tmp_path, monkeypatch):
    p = tmp_path / DB_FILENAME
    build(p, 1, monkeypatch)
    (tmp_path / "unrelated.pre-v1-x").write_text("")
    for i in range(1, 6):  # five upgrades, one new (empty) step each time
        monkeypatch.setattr(dbmod, "MIGRATIONS", [*MIGRATIONS, *[lambda conn: None] * i])
        Database(p).close()
    left = [c for c in copies(tmp_path) if c.name.startswith(DB_FILENAME)]
    assert sorted(c.name.split(".pre-v")[1].split("-")[0] for c in left) == [
        str(LATEST + i) for i in (2, 3, 4)]  # the newest three
    assert all(stat.S_IMODE(c.stat().st_mode) == 0o600 for c in left)
    assert not any(c.name.endswith(("-wal", "-shm")) for c in tmp_path.iterdir()
                   if ".pre-v" in c.name)
    assert (tmp_path / "unrelated.pre-v1-x").exists()


def test_no_copy_for_new_or_current_database(tmp_path):
    p = tmp_path / DB_FILENAME
    Database(p).close()  # new: v0 -> latest
    Database(p).close()  # already latest
    assert not copies(tmp_path)


def test_python_step(tmp_path, monkeypatch):
    p = tmp_path / DB_FILENAME
    build(p, LATEST, monkeypatch)

    def step(conn):
        conn.execute("ALTER TABLE buckets ADD COLUMN product TEXT")
        conn.execute("UPDATE buckets SET product = upper(owner_uid)")
        assert conn.in_transaction

    monkeypatch.setattr(dbmod, "MIGRATIONS", [*MIGRATIONS, step])
    d = Database(p)
    assert d.version == LATEST + 1
    assert d.query_one("SELECT product FROM buckets")["product"] == "ALICE"
    d.close()
    assert len(copies(tmp_path)) == 1


def test_failing_python_step_rolls_back_and_names_copy(tmp_path, monkeypatch):
    p = tmp_path / DB_FILENAME
    build(p, LATEST, monkeypatch)

    def step(conn):
        conn.execute("DELETE FROM buckets")
        raise ValueError("bad data in row 3")

    monkeypatch.setattr(dbmod, "MIGRATIONS", [*MIGRATIONS, step])
    with pytest.raises(MigrationFailed) as err:
        Database(p)
    (copy,) = copies(tmp_path)
    msg = str(err.value)
    assert f"migration {LATEST + 1} failed" in msg
    assert "ValueError: bad data in row 3" in msg
    assert str(copy) in msg
    assert f"stays at schema v{LATEST}" in msg
    c = sqlite3.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert c.execute("SELECT count(*) FROM buckets").fetchone()[0] == 1
    c.close()


def test_failing_sql_step_message(tmp_path, monkeypatch):
    p = tmp_path / DB_FILENAME
    build(p, LATEST, monkeypatch)
    monkeypatch.setattr(dbmod, "MIGRATIONS", [*MIGRATIONS, "CREATE TABLE t (a); BOGUS;"])
    with pytest.raises(MigrationFailed, match=rf"migration {LATEST + 1} failed: "
                                              r"OperationalError: .*pre-v"):
        Database(p)
    c = sqlite3.connect(p)
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 't'").fetchall()
    c.close()


def test_memory_database_takes_no_copy(monkeypatch):
    monkeypatch.setattr(dbmod, "MIGRATIONS", MIGRATIONS[:3])
    d = Database(":memory:")
    monkeypatch.setattr(dbmod, "MIGRATIONS", MIGRATIONS)
    assert d.migrate() == LATEST
    d.close()
