"""SQLite storage: settings, uid mappings, user profiles, buckets, bucket keys, audit log.

Concurrency: the app is a single process, so there is one connection
(`check_same_thread=False`) guarded by an RLock. Every statement is short.
Async handlers call `await db.run(fn, ...)`, which runs a sync repository
call in a worker thread so the event loop never waits on disk or on the lock.

Migrations: `MIGRATIONS` is an ordered list of SQL scripts; the schema version
is `PRAGMA user_version` (= number of applied scripts). Append a new script,
never edit an applied one.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request

log = logging.getLogger(__name__)
DB_FILENAME = "bucketeer.sqlite3"
ACCESS_LEVELS = ("read", "readwrite")

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,              -- JSON
        updated_at TEXT NOT NULL,
        updated_by TEXT
    );
    CREATE TABLE uid_mappings (
        ad_username TEXT PRIMARY KEY COLLATE NOCASE,
        rgw_uid     TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL,
        created_by  TEXT
    );
    CREATE TABLE user_profiles (
        ad_username           TEXT PRIMARY KEY COLLATE NOCASE,
        default_budget_number TEXT,
        advanced_mode         INTEGER NOT NULL DEFAULT 0,
        updated_at            TEXT NOT NULL
    );
    CREATE TABLE buckets (
        name          TEXT PRIMARY KEY,        -- tenant-qualified croit name
        owner_uid     TEXT NOT NULL,
        budget_number TEXT,                    -- NULL for legacy buckets
        created_at    TEXT NOT NULL,
        created_by    TEXT
    );
    CREATE INDEX buckets_owner ON buckets (owner_uid);
    CREATE TABLE bucket_keys (
        id                INTEGER PRIMARY KEY,
        rgw_uid           TEXT NOT NULL,          -- scoped RGW user, e.g. alice.backup-job
        owner_ad_username TEXT NOT NULL COLLATE NOCASE,
        bucket_name       TEXT NOT NULL,
        access            TEXT NOT NULL CHECK (access IN ('read', 'readwrite')),
        label             TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL,
        revoked_at        TEXT,
        suspended_at      TEXT
    );
    -- unique among live keys only, so a revoked uid can be recreated
    CREATE UNIQUE INDEX bucket_keys_live_uid ON bucket_keys (rgw_uid)
        WHERE revoked_at IS NULL;
    CREATE INDEX bucket_keys_owner ON bucket_keys (owner_ad_username);
    CREATE INDEX bucket_keys_bucket ON bucket_keys (bucket_name);
    CREATE TABLE audit_log (
        id          INTEGER PRIMARY KEY,
        ts          TEXT NOT NULL,
        actor       TEXT NOT NULL,             -- AD username or 'system'
        action      TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target      TEXT NOT NULL,
        details     TEXT NOT NULL DEFAULT '{}' -- JSON, never secrets
    );
    CREATE INDEX audit_log_ts ON audit_log (ts);
    CREATE INDEX audit_log_actor ON audit_log (actor);
    CREATE INDEX audit_log_target ON audit_log (target_type, target);
    """,
    # v2: who suspended a bucket key, so waking an owner never revives a key an
    # admin suspended on its own. 'offboarding' is reserved for the daily job.
    # Pre-existing suspensions are backfilled as 'direct': a wrong 'direct' only
    # needs an explicit unsuspend, a wrong 'owner' would silently wake a key.
    """
    ALTER TABLE bucket_keys ADD COLUMN suspended_by TEXT
        CHECK (suspended_by IN ('owner', 'direct', 'offboarding'));
    UPDATE bucket_keys SET suspended_by = 'direct' WHERE suspended_at IS NOT NULL;
    """,
    # v3: 0 = the bucketeer-budget tag still has to be written to the bucket (the
    # S3 call failed after the bucket was created); the bucket list reconciles it.
    """
    ALTER TABLE buckets ADD COLUMN tag_synced INTEGER NOT NULL DEFAULT 1;
    """,
    # v4: bucket keys. `state` tracks creation ('pending' while the multi-step create
    # runs, 'failed' when its rollback could not finish, so the key user may still
    # exist and a revoke retries the cleanup); `access_key` is the key's public half
    # (never a secret), so listing needs no croit call.
    """
    ALTER TABLE bucket_keys ADD COLUMN state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('pending', 'active', 'failed'));
    ALTER TABLE bucket_keys ADD COLUMN access_key TEXT;
    """,
    # v5: 1 = an admin revoked the key while the owner's S3 client could not edit the
    # bucket policy, so the (harmless, principal gone) statement is still in the policy.
    """
    ALTER TABLE bucket_keys ADD COLUMN statement_pending_cleanup INTEGER NOT NULL DEFAULT 0;
    """,
]

_MISSING: Any = object()
_SECRET_KEY = re.compile(
    r"secret|passw|token|credential|private_?key|authorization|api_?key|signature"
    r"|access_?key_?secret|session_?token",
    re.I,
)
# Values that are secrets whatever their key: bearer tokens, presigned URLs.
_SECRET_VALUE = re.compile(r"^\s*Bearer\s|(?:X-Amz-)?Signature=", re.I)
REDACTED = "[redacted]"


class SchemaTooNew(RuntimeError):
    """The database was written by a newer version of Bucketeer."""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def scrub(value: Any) -> Any:
    """Copy of `value` with the value of every secret-looking key redacted."""
    if isinstance(value, dict):
        return {
            str(k): REDACTED if _SECRET_KEY.search(str(k)) else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return REDACTED
    return value


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


class Database:
    """One SQLite connection plus a repository per table."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            p = Path(self.path)
            if not p.parent.exists():
                p.parent.mkdir(parents=True, mode=0o700)
                p.parent.chmod(0o700)  # mkdir's mode is masked by umask
            elif p.parent.stat().st_mode & 0o077:
                log.warning(
                    "%s is accessible to group/other (mode %o); the database "
                    "files themselves stay 0600",
                    p.parent, p.parent.stat().st_mode & 0o777,
                )
            os.close(os.open(p, os.O_CREAT | os.O_RDWR, 0o600))
            p.chmod(0o600)
        self._lock = threading.RLock()
        # isolation_level=None: autocommit; transactions are explicit (tx()).
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()
        self.settings = Settings(self)
        self.uid_mappings = UidMappings(self)
        self.profiles = UserProfiles(self)
        self.buckets = Buckets(self)
        self.bucket_keys = BucketKeys(self)
        self.audit = AuditLog(self)

    # -- plumbing -----------------------------------------------------------

    @property
    def version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        """Apply pending migrations, each atomically. Returns the new version."""
        with self._lock:
            current = self.version
            if current > len(MIGRATIONS):
                raise SchemaTooNew(
                    f"{self.path} is schema version {current}, but this Bucketeer "
                    f"only knows up to {len(MIGRATIONS)}; refusing to start"
                )
            for n in range(current + 1, len(MIGRATIONS) + 1):
                script = (
                    f"BEGIN IMMEDIATE;\n{MIGRATIONS[n - 1]}\n"
                    f"PRAGMA user_version = {n};\nCOMMIT;"
                )
                try:
                    self._conn.executescript(script)
                except Exception:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
            return self.version

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Lock plus a write transaction; rolls back on error."""
        with self._lock:
            if self._conn.in_transaction:  # nested: join the outer transaction
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run a sync repository call in a worker thread:
        `await db.run(db.buckets.get, name)`."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Repo:
    def __init__(self, db: Database):
        self.db = db


class Settings(_Repo):
    """Admin-page runtime policy as JSON values. Callers supply the defaults."""

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def set(self, key: str, value: Any, by: str | None = None) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (key, json.dumps(value), now(), by),
            )

    def delete(self, key: str) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount > 0

    def all(self) -> dict[str, Any]:
        return {
            r["key"]: json.loads(r["value"])
            for r in self.db.query("SELECT key, value FROM settings ORDER BY key")
        }


class UidMappings(_Repo):
    """Exceptions to 'rgw uid = sAMAccountName'."""

    def set(self, ad_username: str, rgw_uid: str, by: str | None = None) -> None:
        """Insert or change a mapping. Raises sqlite3.IntegrityError if
        `rgw_uid` is already mapped from another user."""
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO uid_mappings (ad_username, rgw_uid, created_at, "
                "created_by) VALUES (?, ?, ?, ?) ON CONFLICT (ad_username) DO "
                "UPDATE SET rgw_uid = excluded.rgw_uid",
                (ad_username, rgw_uid, now(), by),
            )

    def get(self, ad_username: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM uid_mappings WHERE ad_username = ?", (ad_username,)
        )

    def list(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM uid_mappings ORDER BY ad_username")

    def delete(self, ad_username: str) -> bool:
        with self.db.tx() as c:
            cur = c.execute(
                "DELETE FROM uid_mappings WHERE ad_username = ?", (ad_username,)
            )
            return cur.rowcount > 0


class UserProfiles(_Repo):
    def get(self, ad_username: str) -> dict[str, Any]:
        """The profile, or the defaults if the user has none yet."""
        row = self.db.query_one(
            "SELECT * FROM user_profiles WHERE ad_username = ?", (ad_username,)
        )
        if row is None:
            return {
                "ad_username": ad_username,
                "default_budget_number": None,
                "advanced_mode": False,
                "updated_at": None,
            }
        row["advanced_mode"] = bool(row["advanced_mode"])
        return row

    def _upsert(self, ad_username: str, column: str, value: Any) -> None:
        with self.db.tx() as c:
            c.execute(
                f"INSERT INTO user_profiles (ad_username, {column}, updated_at) "
                f"VALUES (?, ?, ?) ON CONFLICT (ad_username) DO UPDATE SET "
                f"{column} = excluded.{column}, updated_at = excluded.updated_at",
                (ad_username, value, now()),
            )

    def set_default_budget_number(self, ad_username: str, value: str | None) -> None:
        self._upsert(ad_username, "default_budget_number", value)

    def set_advanced_mode(self, ad_username: str, enabled: bool) -> None:
        self._upsert(ad_username, "advanced_mode", int(enabled))


class Buckets(_Repo):
    """Bucketeer's metadata about buckets. Format of budget_number is not
    validated here (QUESTIONS.md Q2); it is NULL for legacy buckets."""

    def create(
        self,
        name: str,
        owner_uid: str,
        budget_number: str | None = None,
        by: str | None = None,
        tag_synced: bool = True,
    ) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO buckets (name, owner_uid, budget_number, created_at, "
                "created_by, tag_synced) VALUES (?, ?, ?, ?, ?, ?)",
                (name, owner_uid, budget_number, now(), by, int(tag_synced)),
            )

    def set_tag_synced(self, name: str, synced: bool) -> bool:
        with self.db.tx() as c:
            return c.execute(
                "UPDATE buckets SET tag_synced = ? WHERE name = ?", (int(synced), name)
            ).rowcount > 0

    def get(self, name: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM buckets WHERE name = ?", (name,))

    def list(self, owner_uid: str | None = None) -> list[dict[str, Any]]:
        if owner_uid is None:
            return self.db.query("SELECT * FROM buckets ORDER BY name")
        return self.db.query(
            "SELECT * FROM buckets WHERE owner_uid = ? ORDER BY name", (owner_uid,)
        )

    def set_budget_number(self, name: str, budget_number: str | None) -> bool:
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE buckets SET budget_number = ? WHERE name = ?",
                (budget_number, name),
            )
            return cur.rowcount > 0

    def delete(self, name: str) -> bool:
        with self.db.tx() as c:
            return c.execute("DELETE FROM buckets WHERE name = ?", (name,)).rowcount > 0


class BucketKeys(_Repo):
    """Ownership records for scoped RGW users. `bucket_name` has no foreign
    key on purpose: legacy buckets are not in `buckets`."""

    def create(
        self,
        rgw_uid: str,
        owner_ad_username: str,
        bucket_name: str,
        access: str,
        label: str = "",
        state: str = "active",
        access_key: str | None = None,
    ) -> dict[str, Any]:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO bucket_keys (rgw_uid, owner_ad_username, bucket_name, "
                "access, label, created_at, state, access_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rgw_uid, owner_ad_username, bucket_name, access, label, now(), state,
                 access_key),
            )
            return self.get(cur.lastrowid)  # type: ignore[return-value]

    def set_state(self, key_id: int, state: str, access_key: str | None = None) -> bool:
        """pending -> active / failed (creation progress); `access_key` is kept when None."""
        with self.db.tx() as c:
            return c.execute(
                "UPDATE bucket_keys SET state = ?, access_key = COALESCE(?, access_key) "
                "WHERE id = ?",
                (state, access_key, key_id),
            ).rowcount > 0

    def count_live(self, owner: str, bucket_name: str | None = None) -> int:
        """Live (not revoked) keys of an owner, pending and failed ones included."""
        sql = ("SELECT COUNT(*) AS n FROM bucket_keys "
               "WHERE owner_ad_username = ? AND revoked_at IS NULL")
        params: tuple = (owner,)
        if bucket_name is not None:
            sql, params = sql + " AND bucket_name = ?", (owner, bucket_name)
        return int(self.db.query_one(sql, params)["n"])

    def get(self, key_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM bucket_keys WHERE id = ?", (key_id,))

    def get_by_uid(self, rgw_uid: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM bucket_keys WHERE rgw_uid = ? "
            "ORDER BY revoked_at IS NULL DESC, id DESC LIMIT 1",
            (rgw_uid,),
        )

    def list(
        self,
        owner: str | None = None,
        bucket_name: str | None = None,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if owner is not None:
            where.append("owner_ad_username = ?")
            params.append(owner)
        if bucket_name is not None:
            where.append("bucket_name = ?")
            params.append(bucket_name)
        if not include_revoked:
            where.append("revoked_at IS NULL")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.db.query(
            f"SELECT * FROM bucket_keys {clause} ORDER BY id", tuple(params)
        )

    def revoke(self, key_id: int) -> bool:
        """Mark revoked (the row is kept for history). False if absent/already."""
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE bucket_keys SET revoked_at = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (now(), key_id),
            )
            return cur.rowcount > 0

    def set_statement_pending(self, key_id: int, pending: bool) -> bool:
        with self.db.tx() as c:
            return c.execute(
                "UPDATE bucket_keys SET statement_pending_cleanup = ? WHERE id = ?",
                (1 if pending else 0, key_id),
            ).rowcount > 0

    def set_suspended(self, key_id: int, suspended: bool, by: str = "direct") -> bool:
        """`by` is 'owner', 'direct' or 'offboarding'; unsuspending clears both columns."""
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE bucket_keys SET suspended_at = ?, suspended_by = ? WHERE id = ?",
                (now(), by, key_id) if suspended else (None, None, key_id),
            )
            return cur.rowcount > 0

    def set_owner_suspended(self, owner: str, suspended: bool, by: str = "owner") -> int:
        """Cascade from an owner: suspend their live keys that are not suspended yet,
        or wake only the live keys that this same cascade (`by`) suspended. A key
        suspended directly stays as it is. Returns the number of keys changed."""
        with self.db.tx() as c:
            if suspended:
                cur = c.execute(
                    "UPDATE bucket_keys SET suspended_at = ?, suspended_by = ? "
                    "WHERE owner_ad_username = ? AND revoked_at IS NULL "
                    "AND suspended_at IS NULL",
                    (now(), by, owner),
                )
            else:
                cur = c.execute(
                    "UPDATE bucket_keys SET suspended_at = NULL, suspended_by = NULL "
                    "WHERE owner_ad_username = ? AND revoked_at IS NULL "
                    "AND suspended_by = ?",
                    (owner, by),
                )
            return cur.rowcount


class AuditLog(_Repo):
    """Append-only: only `add` and the query helpers exist. Never put secrets
    in `details`; keys that look secret are redacted as a safety net."""

    def add(
        self,
        actor: str,
        action: str,
        target_type: str,
        target: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        payload = json.dumps(scrub(details or {}), default=str)
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO audit_log (ts, actor, action, target_type, target, "
                "details) VALUES (?, ?, ?, ?, ?, ?)",
                (now(), actor, action, target_type, target, payload),
            )
            return cur.lastrowid  # type: ignore[return-value]

    @staticmethod
    def _where(
        actor: str | None,
        action: str | None,
        target_type: str | None,
        target: str | None,
        since: str | None,
        until: str | None,
    ) -> tuple[str, list[Any]]:
        conds, params = [], []
        for col, val in (
            ("actor", actor),
            ("action", action),
            ("target_type", target_type),
            ("target", target),
        ):
            if val is not None:
                conds.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            conds.append("ts >= ?")
            params.append(since)
        if until is not None:
            conds.append("ts < ?")
            params.append(until)
        return (f"WHERE {' AND '.join(conds)}" if conds else ""), params

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Newest first. `since` inclusive, `until` exclusive (ISO timestamps)."""
        where, params = self._where(actor, action, target_type, target, since, until)
        limit = max(1, min(limit, 1000))
        rows = self.db.query(
            f"SELECT * FROM audit_log {where} ORDER BY ts DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (*params, limit, max(0, offset)),
        )
        for r in rows:
            r["details"] = json.loads(r["details"])
        return rows

    def count(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        where, params = self._where(actor, action, target_type, target, since, until)
        row = self.db.query_one(f"SELECT COUNT(*) AS n FROM audit_log {where}", tuple(params))
        return row["n"]  # type: ignore[index]


def get_db(request: Request) -> Database:
    """FastAPI dependency: `db: Database = Depends(get_db)`."""
    return request.app.state.db
