import asyncio
import sqlite3
import stat
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from bucketeer import db as dbmod
from bucketeer.config import Settings
from bucketeer.db import DB_FILENAME, MIGRATIONS, Database, SchemaTooNew, get_db
from bucketeer.main import create_app

from .conftest import REQUIRED


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_migrate_from_empty(tmp_path):
    d = Database(tmp_path / "x.sqlite3")
    assert d.version == len(MIGRATIONS)
    tables = {r["name"] for r in d.query("SELECT name FROM sqlite_master")}
    assert {"settings", "uid_mappings", "user_profiles", "buckets", "bucket_keys",
            "audit_log"} <= tables
    assert d.query_one("PRAGMA foreign_keys")["foreign_keys"] == 1
    assert d.query_one("PRAGMA journal_mode")["journal_mode"] == "wal"
    d.close()


def test_migrate_idempotent(tmp_path):
    p = tmp_path / "x.sqlite3"
    d = Database(p)
    d.settings.set("a", 1)
    d.close()
    d = Database(p)
    assert d.version == len(MIGRATIONS)
    assert d.settings.get("a") == 1
    assert d.migrate() == len(MIGRATIONS)
    d.close()


def test_newer_version_refused(tmp_path):
    p = tmp_path / "x.sqlite3"
    Database(p).close()
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    c.close()
    with pytest.raises(SchemaTooNew):
        Database(p)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    p = tmp_path / "x.sqlite3"
    monkeypatch.setattr(
        dbmod, "MIGRATIONS", [*MIGRATIONS, "CREATE TABLE t (a); BOGUS SQL;"]
    )
    with pytest.raises(sqlite3.Error):
        Database(p)
    monkeypatch.setattr(dbmod, "MIGRATIONS", MIGRATIONS)
    d = Database(p)  # v1 intact, no half-applied v2
    assert d.version == len(MIGRATIONS)
    assert not d.query("SELECT name FROM sqlite_master WHERE name = 't'")
    d.close()


def test_permissions(tmp_path):
    d = Database(tmp_path / "data" / DB_FILENAME)
    d.settings.set("a", 1)
    assert mode(tmp_path / "data") == 0o700
    assert mode(tmp_path / "data" / DB_FILENAME) == 0o600
    d.close()


def test_existing_dir_mode_kept_with_warning(tmp_path, caplog):
    (tmp_path / "data").mkdir()
    (tmp_path / "data").chmod(0o755)
    Database(tmp_path / "data" / DB_FILENAME).close()
    assert mode(tmp_path / "data") == 0o755
    assert "accessible to group/other" in caplog.text
    assert mode(tmp_path / "data" / DB_FILENAME) == 0o600


def test_revoked_uid_can_be_recreated(db):
    k1 = db.bucket_keys.create("alice.backup", "alice", "b1", "read")
    with pytest.raises(sqlite3.IntegrityError):  # two live rows rejected
        db.bucket_keys.create("alice.backup", "alice", "b1", "read")
    db.bucket_keys.revoke(k1["id"])
    k2 = db.bucket_keys.create("alice.backup", "alice", "b1", "readwrite")
    assert k2["id"] != k1["id"]
    assert db.bucket_keys.get_by_uid("alice.backup")["id"] == k2["id"]
    db.bucket_keys.revoke(k2["id"])
    db.bucket_keys.create("alice.backup", "alice", "b1", "read")  # third time
    assert len(db.bucket_keys.list(include_revoked=True)) == 3


def test_scrub_more_secret_shapes():
    from bucketeer.db import REDACTED, scrub

    out = scrub({
        "Authorization": "Bearer x", "api_key": "k", "X-Amz-Signature": "s",
        "access_key_secret": "a", "SessionToken": "t", "session_token": "t",
        "note": "Bearer abc", "url": "https://h/o?X-Amz-Signature=deadbeef&a=1",
        "v2": "https://h/o?Signature=abc", "list": ["bearer zzz", "fine"],
        "fine": "bucket-name",
    })
    assert out.pop("fine") == "bucket-name"
    assert out.pop("list") == [REDACTED, "fine"]
    assert set(out.values()) == {REDACTED}


def test_settings(db):
    assert db.settings.get("quota", 42) == 42
    db.settings.set("quota", {"gb": 100}, by="admin")
    db.settings.set("quota", {"gb": 200}, by="bob")
    assert db.settings.get("quota") == {"gb": 200}
    row = db.query_one("SELECT * FROM settings")
    assert row["updated_by"] == "bob"
    db.settings.set("naming", ["a", "b"])
    assert db.settings.all() == {"naming": ["a", "b"], "quota": {"gb": 200}}
    assert db.settings.delete("naming") and not db.settings.delete("naming")


def test_uid_mappings(db):
    db.uid_mappings.set("Alice", "alice-old", by="admin")
    assert db.uid_mappings.get("alice")["rgw_uid"] == "alice-old"  # NOCASE
    db.uid_mappings.set("alice", "alice2")
    assert db.uid_mappings.get("alice")["rgw_uid"] == "alice2"
    with pytest.raises(sqlite3.IntegrityError):
        db.uid_mappings.set("bob", "alice2")
    assert [m["ad_username"] for m in db.uid_mappings.list()] == ["Alice"]
    assert db.uid_mappings.delete("alice") and db.uid_mappings.get("alice") is None


def test_profiles(db):
    p = db.profiles.get("alice")
    assert p["default_budget_number"] is None and p["advanced_mode"] is False
    db.profiles.set_default_budget_number("alice", "IDX-1")
    db.profiles.set_advanced_mode("alice", True)
    p = db.profiles.get("alice")
    assert p["default_budget_number"] == "IDX-1" and p["advanced_mode"] is True
    db.profiles.set_default_budget_number("alice", None)
    assert db.profiles.get("alice")["default_budget_number"] is None
    assert db.profiles.get("alice")["advanced_mode"] is True


def test_buckets(db):
    db.buckets.create("acme:reports", "acme$carol", "X1", by="carol")
    db.buckets.create("legacy", "alice")
    assert db.buckets.get("legacy")["budget_number"] is None
    with pytest.raises(sqlite3.IntegrityError):
        db.buckets.create("legacy", "bob")
    assert [b["name"] for b in db.buckets.list()] == ["acme:reports", "legacy"]
    assert [b["name"] for b in db.buckets.list("alice")] == ["legacy"]
    assert db.buckets.set_budget_number("legacy", "Y")
    assert not db.buckets.set_budget_number("nope", "Y")
    assert db.buckets.get("legacy")["budget_number"] == "Y"
    assert db.buckets.delete("legacy") and db.buckets.get("legacy") is None


def test_bucket_keys(db):
    k = db.bucket_keys.create("alice.job", "alice", "b1", "read", "job")
    assert k["revoked_at"] is None and k["access"] == "read"
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE rgw_uid
        db.bucket_keys.create("alice.job", "bob", "b2", "read")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK access
        db.bucket_keys.create("alice.x", "alice", "b1", "write")
    k2 = db.bucket_keys.create("alice.rw", "alice", "b2", "readwrite")
    db.bucket_keys.create("bob.k", "bob", "b1", "read")
    assert db.bucket_keys.get_by_uid("alice.rw")["id"] == k2["id"]
    assert len(db.bucket_keys.list(owner="alice")) == 2
    assert len(db.bucket_keys.list(bucket_name="b1")) == 2
    assert db.bucket_keys.set_owner_suspended("alice", True) == 2
    assert db.bucket_keys.get(k["id"])["suspended_at"]
    assert db.bucket_keys.set_owner_suspended("alice", False) == 2
    assert db.bucket_keys.get(k["id"])["suspended_at"] is None
    assert db.bucket_keys.set_suspended(k["id"], True)
    assert db.bucket_keys.revoke(k["id"]) and not db.bucket_keys.revoke(k["id"])
    assert len(db.bucket_keys.list(owner="alice")) == 1
    assert len(db.bucket_keys.list(owner="alice", include_revoked=True)) == 2


def test_audit_redacts_secrets(db):
    db.audit.add(
        "alice", "key.create", "key", "alice.job",
        {"access_key": "AK", "secret_key": "S", "nested": {"Password": "p", "ok": 1},
         "items": [{"api_token": "t"}], "ignoreWarning": "t0k"},
    )
    d = db.audit.query()[0]["details"]
    assert d["secret_key"] == d["nested"]["Password"] == "[redacted]"
    assert d["items"][0]["api_token"] == "[redacted]"
    assert d["nested"]["ok"] == 1 and d["access_key"] == "AK"
    raw = db.query_one("SELECT details FROM audit_log")["details"]
    assert '"S"' not in raw and '"p"' not in raw and '"t"' not in raw


def test_audit_query(db):
    ids = []
    for i in range(5):
        ids.append(db.audit.add("alice" if i % 2 == 0 else "bob",
                                "bucket.create" if i < 3 else "bucket.delete",
                                "bucket", f"b{i}", {"i": i}))
    assert [r["id"] for r in db.audit.query()] == ids[::-1]  # newest first
    assert [r["id"] for r in db.audit.query(limit=2, offset=1)] == [ids[3], ids[2]]
    assert {r["actor"] for r in db.audit.query(actor="bob")} == {"bob"}
    assert db.audit.count(action="bucket.delete") == 2
    assert [r["target"] for r in db.audit.query(target="b2")] == ["b2"]
    assert db.audit.count(actor="alice", action="bucket.delete") == 1
    # time range: pin timestamps
    db.query("SELECT 1")
    with db.tx() as c:
        for n, i in enumerate(ids):
            c.execute("UPDATE audit_log SET ts = ? WHERE id = ?",
                      (f"2026-01-0{n + 1}T00:00:00.000000Z", i))
    r = db.audit.query(since="2026-01-02T00:00:00.000000Z",
                       until="2026-01-04T00:00:00.000000Z")
    assert [x["target"] for x in r] == ["b2", "b1"]
    assert db.audit.query(limit=0) and len(db.audit.query(limit=10**6)) == 5
    assert not hasattr(db.audit, "delete") and not hasattr(db.audit, "update")


def test_tx_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO settings VALUES ('k', '1', 't', NULL)")
            raise RuntimeError
    assert db.settings.get("k") is None


def test_run_in_thread(db):
    async def go():
        await db.run(db.settings.set, "k", 1)
        return await db.run(db.settings.get, "k")

    assert asyncio.run(go()) == 1


def test_lifespan_creates_db_under_data_dir(tmp_path):
    s = Settings(_env_file=None, data_dir=str(tmp_path / "d"), **REQUIRED)
    app = create_app(s)
    with TestClient(app) as c:
        assert (tmp_path / "d" / DB_FILENAME).exists()
        assert app.state.db.version == len(MIGRATIONS)
        assert c.get("/health").json() == {"status": "ok"}
    with pytest.raises(sqlite3.ProgrammingError):
        app.state.db.query("SELECT 1")  # closed on shutdown


def test_injected_db_and_dependency(settings, db):
    app = create_app(settings, db=db)
    assert app.state.db is db

    probe = FastAPI()  # the static mount of the real app would shadow new routes
    probe.state.db = db

    @probe.get("/v")
    def version(d: Annotated[Database, Depends(get_db)]):
        return {"v": d.version}

    assert TestClient(probe).get("/v").json() == {"v": len(MIGRATIONS)}
    with TestClient(app):
        pass
    assert db.version  # injected db left open


def test_health_does_not_touch_db(settings):
    app: FastAPI = create_app(settings)  # no lifespan: state.db is None
    assert TestClient(app).get("/health").status_code == 200
