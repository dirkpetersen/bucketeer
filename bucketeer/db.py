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
    # v6: the audit viewer's filter dropdown lists distinct actions.
    """
    CREATE INDEX audit_log_action ON audit_log (action);
    """,
    # v7: shares of a bucket with an AD user (bucket policy statement is the source of
    # truth for access; this table gives names for listing and the recipient's view).
    """
    CREATE TABLE shares (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket_name    TEXT NOT NULL,           -- croit name (tenant:bucket)
        owner_uid      TEXT NOT NULL,
        owner_name     TEXT NOT NULL DEFAULT '',
        recipient_ad   TEXT NOT NULL,
        recipient_uid  TEXT NOT NULL,
        recipient_name TEXT NOT NULL DEFAULT '',
        access         TEXT NOT NULL CHECK (access IN ('read', 'readwrite')),
        created_at     TEXT NOT NULL,
        created_by     TEXT NOT NULL,
        revoked_at     TEXT
    );
    CREATE UNIQUE INDEX shares_live ON shares (bucket_name, recipient_uid COLLATE NOCASE)
        WHERE revoked_at IS NULL;
    CREATE INDEX shares_recipient ON shares (recipient_uid COLLATE NOCASE);
    """,
    # v8: offboarding. `known_accounts` durably links an AD name to the RGW uid of a person
    # (the job's scope; the audit log is not the source); `offboarding_suspended_at` is set
    # only while the JOB's suspension stands (cleared by login, any admin suspend/unsuspend).
    # `offboarding_runs` keeps the last runs of the daily job.
    """
    CREATE TABLE known_accounts (
        ad_username              TEXT PRIMARY KEY COLLATE NOCASE,
        rgw_uid                  TEXT NOT NULL,
        source                   TEXT NOT NULL CHECK (source IN ('created', 'linked', 'mapping')),
        first_seen               TEXT NOT NULL,
        last_login               TEXT,
        offboarding_suspended_at TEXT
    );
    CREATE INDEX known_accounts_uid ON known_accounts (rgw_uid COLLATE NOCASE);
    CREATE TABLE offboarding_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        mode        TEXT NOT NULL CHECK (mode IN ('report', 'enforce')),
        trigger     TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual')),
        actor       TEXT NOT NULL,
        outcome     TEXT NOT NULL CHECK (outcome IN ('running', 'ok', 'aborted', 'failed')),
        reason      TEXT,
        counts      TEXT NOT NULL DEFAULT '{}',   -- JSON
        results     TEXT NOT NULL DEFAULT '[]'    -- JSON, per person
    );
    """,
    # v9: background jobs that empty and delete a non-empty bucket on the rgw admin backend
    # (progress is kept so a restart resumes; only one queued/running job per bucket).
    """
    CREATE TABLE bucket_delete_jobs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket            TEXT NOT NULL,            -- croit name (tenant:bucket)
        owner_uid         TEXT NOT NULL,
        owner_ad          TEXT NOT NULL,
        state             TEXT NOT NULL CHECK (state IN
                              ('queued', 'running', 'done', 'blocked', 'failed')),
        deleted_objects   INTEGER NOT NULL DEFAULT 0,
        deleted_bytes     INTEGER NOT NULL DEFAULT 0,
        remaining_objects INTEGER NOT NULL DEFAULT 0,
        objects_hint      INTEGER,                  -- sizes at request time (progress display)
        bytes_hint        INTEGER,
        last_error        TEXT,                     -- user-facing text, never a secret
        attempts          INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        finished_at       TEXT
    );
    CREATE UNIQUE INDEX bucket_delete_jobs_active ON bucket_delete_jobs (bucket)
        WHERE state IN ('queued', 'running');
    CREATE INDEX bucket_delete_jobs_owner ON bucket_delete_jobs (owner_uid);
    """,
    # v10: every subuser name a user ever created (a name is never reused, deleted or not).
    """
    CREATE TABLE subuser_names (
        rgw_uid    TEXT NOT NULL,
        name       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT,
        PRIMARY KEY (rgw_uid, name)
    );
    """,
    # v11: one-time setup tokens ("Set up this computer"); only the SHA-256 of a token is kept.
    """
    CREATE TABLE setup_tokens (
        id          INTEGER PRIMARY KEY,
        token_hash  TEXT NOT NULL UNIQUE,
        ad_username TEXT NOT NULL,
        rgw_uid     TEXT NOT NULL,
        access_key  TEXT NOT NULL,         -- public half only
        os          TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        used_at     TEXT,
        used_ip     TEXT
    );
    CREATE INDEX setup_tokens_user ON setup_tokens (ad_username, created_at);
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
        self.shares = Shares(self)
        self.subusers = SubuserNames(self)
        self.known_accounts = KnownAccounts(self)
        self.offboarding_runs = OffboardingRuns(self)
        self.delete_jobs = BucketDeleteJobs(self)
        self.setup_tokens = SetupTokens(self)
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


class Shares(_Repo):
    """Bucket shares with AD users. Live rows have `revoked_at IS NULL`; at most one
    live row per (bucket, recipient uid)."""

    def create(self, bucket_name: str, owner_uid: str, owner_name: str, recipient_ad: str,
               recipient_uid: str, recipient_name: str, access: str,
               created_by: str) -> dict[str, Any]:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO shares (bucket_name, owner_uid, owner_name, recipient_ad, "
                "recipient_uid, recipient_name, access, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bucket_name, owner_uid, owner_name, recipient_ad, recipient_uid,
                 recipient_name, access, now(), created_by),
            )
            return self.get(cur.lastrowid)  # type: ignore[return-value]

    def get(self, share_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM shares WHERE id = ?", (share_id,))

    def live_for(self, bucket_name: str, recipient_uid: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM shares WHERE bucket_name = ? AND recipient_uid = ? COLLATE NOCASE "
            "AND revoked_at IS NULL", (bucket_name, recipient_uid))

    def list_live(self, bucket_name: str | None = None,
                  recipient_uid: str | None = None) -> list[dict[str, Any]]:
        where, params = ["revoked_at IS NULL"], []
        if bucket_name is not None:
            where.append("bucket_name = ?")
            params.append(bucket_name)
        if recipient_uid is not None:
            where.append("recipient_uid = ? COLLATE NOCASE")
            params.append(recipient_uid)
        return self.db.query(
            f"SELECT * FROM shares WHERE {' AND '.join(where)} ORDER BY id", tuple(params))

    def set_access(self, share_id: int, access: str) -> bool:
        with self.db.tx() as c:
            return c.execute("UPDATE shares SET access = ? WHERE id = ? AND revoked_at IS NULL",
                             (access, share_id)).rowcount > 0

    def revoke(self, share_id: int) -> bool:
        with self.db.tx() as c:
            return c.execute("UPDATE shares SET revoked_at = ? WHERE id = ? "
                             "AND revoked_at IS NULL", (now(), share_id)).rowcount > 0

    def revoke_bucket(self, bucket_name: str,
                      owner_uid: str | None = None) -> list[dict[str, Any]]:
        """Revoke every live share of a (deleted) bucket (only those of `owner_uid` when
        given); returns the rows revoked."""
        extra, args = ("", ()) if owner_uid is None else (" AND owner_uid = ?", (owner_uid,))
        with self.db.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM shares WHERE bucket_name = ? AND revoked_at IS NULL" + extra,
                (bucket_name, *args))]
            c.execute("UPDATE shares SET revoked_at = ? WHERE bucket_name = ? "
                      "AND revoked_at IS NULL" + extra, (now(), bucket_name, *args))
            return rows


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


class SubuserNames(_Repo):
    """Subuser names ever used per RGW uid. RGW is the source of truth for what exists; this
    table only remembers names so a deleted one is never handed out again."""

    def claim(self, uid: str, name: str) -> bool:
        """Record `name` for `uid`; False when it was used before (deleted or not)."""
        with self.db.tx() as c:
            return c.execute(
                "INSERT OR IGNORE INTO subuser_names (rgw_uid, name, created_at) "
                "VALUES (?, ?, ?)", (uid, name, now())).rowcount > 0

    def release(self, uid: str, name: str) -> None:
        """Forget a claim whose create never happened (a clean failure)."""
        with self.db.tx() as c:
            c.execute("DELETE FROM subuser_names WHERE rgw_uid = ? AND name = ?", (uid, name))

    def mark_deleted(self, uid: str, name: str) -> None:
        """Record the deletion; a name Bucketeer never saw (made outside it) is recorded too,
        so it is never reused either."""
        ts = now()
        with self.db.tx() as c:
            c.execute("INSERT INTO subuser_names (rgw_uid, name, created_at, deleted_at) "
                      "VALUES (?, ?, ?, ?) ON CONFLICT (rgw_uid, name) DO UPDATE SET "
                      "deleted_at = COALESCE(deleted_at, excluded.deleted_at)", (uid, name, ts, ts))

    def used(self, uid: str) -> list[str]:
        return [r["name"] for r in self.db.query(
            "SELECT name FROM subuser_names WHERE rgw_uid = ? ORDER BY name", (uid,))]


class KnownAccounts(_Repo):
    """AD name <-> RGW uid links of people (the offboarding job's scope)."""

    def record(self, ad_username: str, uid: str, source: str, *, login: bool = False) -> None:
        """Link (or re-link) `ad_username` to `uid`. The first source is kept while the uid
        stays the same; a login stamps `last_login`."""
        ts = now()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO known_accounts (ad_username, rgw_uid, source, first_seen, last_login)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (ad_username) DO UPDATE SET "
                "source = CASE WHEN rgw_uid = excluded.rgw_uid THEN source "
                "ELSE excluded.source END, "
                "offboarding_suspended_at = CASE WHEN rgw_uid = excluded.rgw_uid "
                "THEN offboarding_suspended_at ELSE NULL END, "
                "rgw_uid = excluded.rgw_uid, "
                "last_login = COALESCE(excluded.last_login, last_login)",
                (ad_username, uid, source, ts, ts if login else None),
            )

    def get(self, ad_username: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM known_accounts WHERE ad_username = ?",
                                 (ad_username,))

    def for_uid(self, uid: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM known_accounts WHERE rgw_uid = ? ORDER BY ad_username",
            (uid,),
        )

    def set_offboarding(self, ad_username: str, suspended: bool) -> bool:
        with self.db.tx() as c:
            return c.execute(
                "UPDATE known_accounts SET offboarding_suspended_at = ? WHERE ad_username = ?",
                (now() if suspended else None, ad_username),
            ).rowcount > 0

    def clear_offboarding_for_uid(self, uid: str) -> list[str]:
        """An admin took over (suspend) or ended (unsuspend) the suspension of `uid`
        (exact uid: `Alice` is not `alice`): the job's claim goes. Returns the AD names whose
        flag was set."""
        with self.db.tx() as c:
            names = [r["ad_username"] for r in c.execute(
                "SELECT ad_username FROM known_accounts WHERE rgw_uid = ? "
                "AND offboarding_suspended_at IS NOT NULL", (uid,))]
            c.execute(
                "UPDATE known_accounts SET offboarding_suspended_at = NULL "
                "WHERE rgw_uid = ?", (uid,))
            return names

    def forget_mapping_rows(self, ad_username: str, keep_uid: str | None = None) -> int:
        """Drop the row of `ad_username` if it came from an admin mapping and does not point
        at `keep_uid` (the mapping was deleted or re-pointed). Exact uid comparison."""
        with self.db.tx() as c:
            return c.execute(
                "DELETE FROM known_accounts WHERE ad_username = ? AND source = 'mapping' "
                "AND (? IS NULL OR rgw_uid != ?)", (ad_username, keep_uid, keep_uid),
            ).rowcount

    def list(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM known_accounts ORDER BY ad_username")


class OffboardingRuns(_Repo):
    KEEP = 30

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        r["counts"] = json.loads(r["counts"] or "{}")
        r["results"] = json.loads(r["results"] or "[]")
        return r

    def start(self, mode: str, trigger: str, actor: str) -> int:
        with self.db.tx() as c:
            run_id = c.execute(
                "INSERT INTO offboarding_runs (started_at, mode, trigger, actor, outcome) "
                "VALUES (?, ?, ?, ?, 'running')", (now(), mode, trigger, actor),
            ).lastrowid
            c.execute(
                "DELETE FROM offboarding_runs WHERE id NOT IN "
                "(SELECT id FROM offboarding_runs ORDER BY id DESC LIMIT ?)", (self.KEEP,))
            return run_id  # type: ignore[return-value]

    def finish(self, run_id: int, outcome: str, reason: str | None,
               counts: dict[str, Any], results: list[dict[str, Any]]) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE offboarding_runs SET finished_at = ?, outcome = ?, reason = ?, "
                "counts = ?, results = ? WHERE id = ?",
                (now(), outcome, reason, json.dumps(scrub(counts)),
                 json.dumps(scrub(results)), run_id),
            )

    def fail_stale(self) -> int:
        """Runs still 'running' after a restart died with the process."""
        with self.db.tx() as c:
            return c.execute(
                "UPDATE offboarding_runs SET outcome = 'failed', finished_at = ?, "
                "reason = 'Bucketeer restarted during the run.' WHERE outcome = 'running'",
                (now(),),
            ).rowcount

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.query(
            "SELECT * FROM offboarding_runs ORDER BY id DESC LIMIT ?", (limit,))]

    def get(self, run_id: int) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT * FROM offboarding_runs WHERE id = ?", (run_id,))
        return self._row(r) if r else None


class BucketDeleteJobs(_Repo):
    """Jobs that empty and delete a bucket (see `bucket_delete.py`). At most one
    queued/running job per bucket (unique index): `create` raises `sqlite3.IntegrityError`."""

    def create(self, bucket: str, owner_uid: str, owner_ad: str, objects_hint: int | None,
               bytes_hint: int | None) -> dict[str, Any]:
        with self.db.tx() as c:
            t = now()
            cur = c.execute(
                "INSERT INTO bucket_delete_jobs (bucket, owner_uid, owner_ad, state, "
                "objects_hint, bytes_hint, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
                (bucket, owner_uid, owner_ad, objects_hint, bytes_hint, t, t))
            return self.get(cur.lastrowid)  # type: ignore[return-value]

    def get(self, job_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM bucket_delete_jobs WHERE id = ?", (job_id,))

    def latest_for(self, bucket: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM bucket_delete_jobs WHERE bucket = ? ORDER BY id DESC LIMIT 1",
            (bucket,))

    def unfinished(self) -> list[dict[str, Any]]:
        """Queued or running jobs (what a restart resumes)."""
        return self.db.query("SELECT * FROM bucket_delete_jobs WHERE state IN "
                             "('queued', 'running') ORDER BY id")

    def list_for(self, owner_uid: str, done_since: str) -> list[dict[str, Any]]:
        """The owner's jobs: every one not done, plus those done since `done_since`."""
        return self.db.query(
            "SELECT * FROM bucket_delete_jobs WHERE owner_uid = ? AND "
            "(state != 'done' OR finished_at >= ?) ORDER BY id", (owner_uid, done_since))

    def start(self, job_id: int) -> bool:
        with self.db.tx() as c:
            return c.execute(
                "UPDATE bucket_delete_jobs SET state = 'running', attempts = attempts + 1, "
                "last_error = NULL, finished_at = NULL, updated_at = ? WHERE id = ? "
                "AND state IN ('queued', 'running')", (now(), job_id)).rowcount > 0

    def requeue(self, job_id: int) -> bool:
        """blocked/failed -> queued (retry); False if the job is in another state."""
        with self.db.tx() as c:
            return c.execute(
                "UPDATE bucket_delete_jobs SET state = 'queued', last_error = NULL, "
                "finished_at = NULL, updated_at = ? WHERE id = ? AND state IN "
                "('blocked', 'failed')", (now(), job_id)).rowcount > 0

    def supersede(self, job_id: int) -> None:
        """End another owner's job for good (the bucket changed hands)."""
        with self.db.tx() as c:
            t = now()
            c.execute("UPDATE bucket_delete_jobs SET state = 'failed', finished_at = ?, "
                      "updated_at = ?, last_error = 'The bucket now belongs to someone else.' "
                      "WHERE id = ? AND state != 'done'", (t, t, job_id))

    def progress(self, job_id: int, deleted: int, size: int, remaining: int | None) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE bucket_delete_jobs SET deleted_objects = deleted_objects + ?, "
                "deleted_bytes = deleted_bytes + ?, remaining_objects = COALESCE(?, "
                "remaining_objects), updated_at = ? WHERE id = ?",
                (deleted, size, remaining, now(), job_id))

    def finish(self, job_id: int, state: str, message: str | None = None) -> None:
        assert state in ("done", "blocked", "failed")
        with self.db.tx() as c:
            t = now()
            c.execute("UPDATE bucket_delete_jobs SET state = ?, last_error = ?, "
                      "finished_at = ?, updated_at = ? WHERE id = ?",
                      (state, message, t, t, job_id))


class SetupTokens(_Repo):
    """One-time setup links. Times are ISO strings in the `now()` format, so they sort."""

    def create(self, token_hash: str, ad: str, uid: str, access_key: str, os_name: str,
               created: str, expires: str, purge_before: str) -> int:
        with self.db.tx() as c:
            c.execute("DELETE FROM setup_tokens WHERE created_at < ?", (purge_before,))
            return c.execute(
                "INSERT INTO setup_tokens (token_hash, ad_username, rgw_uid, access_key, os, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_hash, ad, uid, access_key, os_name, created, expires)).lastrowid  # type: ignore[return-value]

    def count_since(self, ad: str, since: str) -> int:
        return self.db.query_one(
            "SELECT COUNT(*) AS n FROM setup_tokens WHERE ad_username = ? AND created_at >= ?",
            (ad, since))["n"]  # type: ignore[index]

    def by_hash(self, token_hash: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM setup_tokens WHERE token_hash = ?", (token_hash,))

    def claim(self, row_id: int, now_iso: str, ip: str) -> bool:
        """Mark used; True for exactly one caller, and only while not expired."""
        with self.db.tx() as c:
            return c.execute(
                "UPDATE setup_tokens SET used_at = ?, used_ip = ? "
                "WHERE id = ? AND used_at IS NULL AND expires_at > ?",
                (now_iso, ip, row_id, now_iso)).rowcount == 1


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

    def search(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target: str | None = None,
        target_contains: str | None = None,
        since: str | None = None,
        until: str | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Admin viewer: newest first by id (stable under concurrent inserts).
        `action` ending in '.' is a prefix match, otherwise exact; `target` is
        exact, `target_contains` a case-insensitive substring; `since` inclusive,
        `until` exclusive; `before_id` is the cursor. Returns (rows, next cursor
        or None). `details` come back re-scrubbed (defense in depth)."""
        conds: list[str] = []
        params: list[Any] = []
        for col, val in (("actor", actor), ("target_type", target_type), ("target", target)):
            if val is not None:
                conds.append(f"{col} = ?")
                params.append(val)
        if action is not None:
            if action.endswith("."):
                esc = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                conds.append("action LIKE ? ESCAPE '\\'")
                params.append(esc + "%")
            else:
                conds.append("action = ?")
                params.append(action)
        if target_contains:
            conds.append("instr(lower(target), lower(?)) > 0")
            params.append(target_contains)
        if since is not None:
            conds.append("ts >= ?")
            params.append(since)
        if until is not None:
            conds.append("ts < ?")
            params.append(until)
        if before_id is not None:
            conds.append("id < ?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        limit = max(1, limit)
        rows = self.db.query(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", (*params, limit + 1)
        )
        more = len(rows) > limit
        rows = rows[:limit]
        for r in rows:
            try:
                r["details"] = scrub(json.loads(r["details"]))
            except ValueError:
                r["details"] = {}
        return rows, (rows[-1]["id"] if more else None)

    def facets(self) -> dict[str, list[str]]:
        """Distinct actions and target types, for filter dropdowns."""
        return {
            "actions": [r["action"] for r in self.db.query(
                "SELECT DISTINCT action FROM audit_log ORDER BY action")],
            "targetTypes": [r["target_type"] for r in self.db.query(
                "SELECT DISTINCT target_type FROM audit_log ORDER BY target_type")],
        }

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
