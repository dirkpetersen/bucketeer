"""SQLite storage: settings, uid mappings, user profiles, buckets, bucket keys, audit log.

Concurrency: the app is a single process, so there is one connection
(`check_same_thread=False`) guarded by an RLock. Every statement is short.
Async handlers call `await db.run(fn, ...)`, which runs a sync repository
call in a worker thread so the event loop never waits on disk or on the lock.

Migrations: `MIGRATIONS` is an ordered list of SQL scripts or Python steps
(`fn(conn)`, run inside the migration's transaction: plain `execute` calls, never
`executescript` or COMMIT); the schema version is `PRAGMA user_version` (= number
of applied entries). Append a new entry, never edit an applied one. Before an
existing file database is upgraded, a consistent copy is written next to it
(`<file>.pre-v<old>-<UTC time>`, 0600, the newest `KEEP_UPGRADE_COPIES` kept).
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

Migration = str | Callable[[sqlite3.Connection], None]
KEEP_UPGRADE_COPIES = 3
_COPY_NAME = re.compile(r"\.pre-v\d+-\d{8}T\d{6}Z(?:-\d+)?\Z")

MIGRATIONS: list[Migration] = [
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
    # v12: one size reading per bucket and day (the last one of the day wins), for billing.
    """
    CREATE TABLE usage_daily (
        day     TEXT NOT NULL,         -- YYYY-MM-DD, server local date
        bucket  TEXT NOT NULL,         -- tenant-qualified name
        owner   TEXT NOT NULL,         -- RGW uid
        bytes   INTEGER NOT NULL,
        PRIMARY KEY (day, bucket)
    );
    """,
    # v13: the bucket's size quota at that reading (bytes; NULL = no size quota, unlimited),
    # for the chargeback sheet (Tier-3-S3 is billed on the quota).
    """
    ALTER TABLE usage_daily ADD COLUMN quota_bytes INTEGER;
    """,
    # v14: the user's own storage location (Cloud / On Premises switch, Phase 6). NULL = the
    # admin's global default; used only while Advanced mode is on.
    """
    ALTER TABLE user_profiles ADD COLUMN location TEXT
        CHECK (location IS NULL OR location IN ('onprem', 'cloud'));
    """,
    # v15: 0 = created_at / created_by are not the bucket's real creation (the row was made
    # later, e.g. by a budget edit of a bucket older than the portal, or rebuilt from Ceph
    # without a `bucketeer-created` tag): never written as creator/created tags or shown.
    """
    ALTER TABLE buckets ADD COLUMN creation_known INTEGER NOT NULL DEFAULT 1;
    """,
    # v16: bucket quota changes (whole TiB), for the weekly increase limit per person
    # (`bucket_quota.py`). `increase` = max(new - old, 0); only rows with `counted = 1` (a
    # person changing their own bucket) count; admin changes are recorded with 0. Losing
    # this table only resets the rolling week.
    """
    CREATE TABLE quota_changes (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT NOT NULL,          -- UTC ISO time
        person    TEXT,                   -- AD username of the person charged (lowercase)
        uid       TEXT NOT NULL,          -- bucket owner's RGW uid
        bucket    TEXT NOT NULL,          -- tenant-qualified bucket name
        location  TEXT NOT NULL DEFAULT 'onprem',
        old_tib   INTEGER,                -- null: no quota (unlimited) or a new bucket
        new_tib   INTEGER,                -- null: no quota (admin switched it off)
        increase  INTEGER NOT NULL DEFAULT 0,
        counted   INTEGER NOT NULL DEFAULT 1,
        actor     TEXT NOT NULL
    );
    CREATE INDEX quota_changes_person ON quota_changes (person, ts);
    """,
    # v17: cloud (AWS) users, P6-9. `cloud_accounts` links an AD name to the IAM user Bucketeer
    # made for that person (`policy_synced` 0 = the inline policy still has to be written).
    # `cloud_key_suspensions` lists the access keys Bucketeer deactivated because their owner
    # was suspended ('owner' = an admin, 'offboarding' = the job), so waking the owner
    # reactivates exactly those. `setup_tokens.cloud` 1 = a cloud-only link (`[s3-cloud]`),
    # 0 = on premises only.
    """
    CREATE TABLE cloud_accounts (
        ad_username   TEXT PRIMARY KEY COLLATE NOCASE,
        iam_user      TEXT NOT NULL UNIQUE,
        source        TEXT NOT NULL CHECK (source IN ('created', 'recovered')),
        policy_synced INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    );
    CREATE TABLE cloud_key_suspensions (
        access_key   TEXT PRIMARY KEY,
        iam_user     TEXT NOT NULL,
        ad_username  TEXT NOT NULL COLLATE NOCASE,
        suspended_by TEXT NOT NULL CHECK (suspended_by IN ('owner', 'offboarding')),
        suspended_at TEXT NOT NULL
    );
    CREATE INDEX cloud_key_suspensions_ad ON cloud_key_suspensions (ad_username);
    ALTER TABLE setup_tokens ADD COLUMN cloud INTEGER NOT NULL DEFAULT 0;
    """,
    # v18: cloud (AWS) buckets, P6-10, share the `buckets` table. `location` 'cloud' rows are
    # AWS buckets (`name` = the full AWS name); AWS has no bucket quota, so `quota_tib` holds
    # it (mirrored in the `bucketeer-quota` tag); `storage_class` intelligent|deep_archive;
    # `over_quota_alert_at` = the last over-quota email (at most one per 7 days).
    """
    ALTER TABLE buckets ADD COLUMN location TEXT NOT NULL DEFAULT 'onprem'
        CHECK (location IN ('onprem', 'cloud'));
    ALTER TABLE buckets ADD COLUMN storage_class TEXT;
    ALTER TABLE buckets ADD COLUMN quota_tib INTEGER;
    ALTER TABLE buckets ADD COLUMN over_quota_alert_at TEXT;
    CREATE INDEX buckets_location ON buckets (location, owner_uid);
    """,
    # v19: delete jobs for cloud (AWS) buckets share `bucket_delete_jobs` (P6-13).
    # `temp_key` = the access key id (never the secret) of the temporary IAM key a cloud job
    # holds right now, so a crash leftover is deleted on the next run.
    """
    ALTER TABLE bucket_delete_jobs ADD COLUMN location TEXT NOT NULL DEFAULT 'onprem'
        CHECK (location IN ('onprem', 'cloud'));
    ALTER TABLE bucket_delete_jobs ADD COLUMN temp_key TEXT;
    """,
    # User notices (`notices.py`): how old an access key is. RGW keys carry no creation date,
    # so `first_seen` is the creation when Bucketeer made the key (`created` = 1), else the
    # first time Bucketeer saw it (the age is then "at least"). `kind` 'user' = an own key of
    # `uid`, 'subuser' = a key of one of its subusers, 'cloud' = an AWS key (`uid` = the AD
    # name, `first_seen` = IAM's CreateDate). `reminded_at` = the last key age email.
    """
    CREATE TABLE key_ages (
        access_key  TEXT PRIMARY KEY,
        uid         TEXT NOT NULL,
        kind        TEXT NOT NULL CHECK (kind IN ('user', 'subuser', 'cloud')),
        first_seen  TEXT NOT NULL,
        created     INTEGER NOT NULL DEFAULT 0,
        reminded_at TEXT
    );
    CREATE INDEX key_ages_uid ON key_ages (uid);
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


class MigrationFailed(RuntimeError):
    """A migration (or the copy taken before it) failed; the app must not start."""


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
        self.usage = UsageDaily(self)
        self.quota_changes = QuotaChanges(self)
        self.cloud_accounts = CloudAccounts(self)
        self.cloud_key_suspensions = CloudKeySuspensions(self)
        self.key_ages = KeyAges(self)
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
            if current == len(MIGRATIONS):
                return current
            copy = None
            if current > 0 and self.path != ":memory:":
                copy = self._upgrade_copy(current)
                log.info("upgrading database %s from schema v%d to v%d; copy of v%d at %s",
                         self.path, current, len(MIGRATIONS), current, copy)
            for n in range(current + 1, len(MIGRATIONS) + 1):
                try:
                    self._apply(n, MIGRATIONS[n - 1])
                except Exception as exc:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    where = (f"the copy taken before the upgrade is {copy}" if copy
                             else "no copy was taken (new database)")
                    raise MigrationFailed(
                        f"database migration {n} failed: {type(exc).__name__}: {exc}; "
                        f"{self.path} stays at schema v{n - 1}; {where}; "
                        f"refusing to start"
                    ) from exc
            return self.version

    def _apply(self, n: int, step: Migration) -> None:
        if isinstance(step, str):
            self._conn.executescript(
                f"BEGIN IMMEDIATE;\n{step}\nPRAGMA user_version = {n};\nCOMMIT;"
            )
            return
        self._conn.execute("BEGIN IMMEDIATE")
        step(self._conn)
        self._conn.execute(f"PRAGMA user_version = {n}")
        self._conn.execute("COMMIT")

    def _upgrade_copy(self, version: int) -> Path:
        """Consistent 0600 copy of the database before an upgrade; keeps the newest few."""
        src = Path(self.path)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = src.with_name(f"{src.name}.pre-v{version}-{stamp}")
        i = 0
        while dest.exists():
            i += 1
            dest = src.with_name(f"{src.name}.pre-v{version}-{stamp}-{i}")
        try:
            os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            dest.chmod(0o600)
            out = sqlite3.connect(dest)
            try:
                self._conn.backup(out)
                out.execute("PRAGMA journal_mode = DELETE")  # one self-contained file
            finally:
                out.close()
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise MigrationFailed(
                f"could not copy {self.path} to {dest} before upgrading it from schema "
                f"v{version}: {type(exc).__name__}: {exc}; refusing to start"
            ) from exc
        copies = sorted(
            (p for p in src.parent.iterdir()
             if p.name.startswith(src.name) and _COPY_NAME.fullmatch(p.name[len(src.name):])),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        )
        for old in copies[:-KEEP_UPGRADE_COPIES]:
            old.unlink(missing_ok=True)
            log.info("removed old pre-upgrade copy %s", old)
        return dest

    def snapshot(self, dest: str | Path) -> None:
        """Consistent copy of the whole database into the new file `dest` (sqlite backup API,
        one self-contained file without WAL), e.g. for the nightly backup."""
        out = sqlite3.connect(dest)
        try:
            with self._lock:
                self._conn.backup(out)
            out.execute("PRAGMA journal_mode = DELETE")
        finally:
            out.close()

    def remove_upgrade_copies(self) -> list[Path]:
        """Delete the pre-upgrade copies next to the file (after a successful backup run,
        which holds the same data). Returns what was removed."""
        if self.path == ":memory:":
            return []
        src = Path(self.path)
        gone = []
        for p in src.parent.iterdir():
            if p.name.startswith(src.name) and _COPY_NAME.fullmatch(p.name[len(src.name):]):
                p.unlink(missing_ok=True)
                gone.append(p)
        return gone

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
                "location": None,
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

    def set_location(self, ad_username: str, location: str | None) -> None:
        self._upsert(ad_username, "location", location)


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
        creation_known: bool = True,
        created_at: str | None = None,
    ) -> None:
        """`creation_known=False`: the bucket existed before this row (its creator and
        creation date are unknown here); `created_at` overrides the current time."""
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO buckets (name, owner_uid, budget_number, created_at, "
                "created_by, tag_synced, creation_known) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, owner_uid, budget_number, created_at or now(), by, int(tag_synced),
                 int(creation_known)),
            )

    # On-premises and cloud rows share this table (`location`, migration v18). `list` and
    # `count` default to on-premises rows; `get` and `delete` take `location` to match only
    # one kind (None = any), so an on-prem path never reads or removes a cloud row.

    def count(self, location: str | None = "onprem") -> int:
        if location is None:
            return int(self.db.query_one("SELECT COUNT(*) AS n FROM buckets")["n"])
        return int(self.db.query_one(
            "SELECT COUNT(*) AS n FROM buckets WHERE location = ?", (location,))["n"])

    def set_tag_synced(self, name: str, synced: bool) -> bool:
        with self.db.tx() as c:
            return c.execute(
                "UPDATE buckets SET tag_synced = ? WHERE name = ?", (int(synced), name)
            ).rowcount > 0

    def get(self, name: str, location: str | None = None) -> dict[str, Any] | None:
        if location is None:
            return self.db.query_one("SELECT * FROM buckets WHERE name = ?", (name,))
        return self.db.query_one("SELECT * FROM buckets WHERE name = ? AND location = ?",
                                 (name, location))

    def list(self, owner_uid: str | None = None,
             location: str | None = "onprem") -> list[dict[str, Any]]:
        where, params = [], []
        if owner_uid is not None:
            where.append("owner_uid = ?")
            params.append(owner_uid)
        if location is not None:
            where.append("location = ?")
            params.append(location)
        cond = f" WHERE {' AND '.join(where)}" if where else ""
        return self.db.query(f"SELECT * FROM buckets{cond} ORDER BY name", tuple(params))

    # -- cloud buckets (P6-10) --

    def create_cloud(self, name: str, owner_uid: str, budget_number: str | None,
                     by: str | None, storage_class: str, quota_tib: int,
                     tag_synced: bool = True, creation_known: bool = True,
                     created_at: str | None = None) -> None:
        """`creation_known` / `created_at` as in `create` (the rebuild restores rows)."""
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO buckets (name, owner_uid, budget_number, created_at, created_by, "
                "tag_synced, creation_known, location, storage_class, quota_tib) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'cloud', ?, ?)",
                (name, owner_uid, budget_number, created_at or now(), by, int(tag_synced),
                 int(creation_known), storage_class, quota_tib))

    def list_cloud(self, owner_uid: str | None = None) -> list[dict[str, Any]]:
        if owner_uid is None:
            return self.db.query("SELECT * FROM buckets WHERE location = 'cloud' ORDER BY name")
        return self.db.query("SELECT * FROM buckets WHERE location = 'cloud' AND owner_uid = ? "
                             "ORDER BY name", (owner_uid,))

    def set_quota_tib(self, name: str, tib: int) -> bool:
        with self.db.tx() as c:
            return c.execute("UPDATE buckets SET quota_tib = ? WHERE name = ?",
                             (tib, name)).rowcount > 0

    def set_alerted(self, name: str, ts: str | None, location: str | None = None) -> bool:
        """The last over-quota (cloud) or near-full (on premises) email of the bucket."""
        with self.db.tx() as c:
            if location is None:
                return c.execute("UPDATE buckets SET over_quota_alert_at = ? WHERE name = ?",
                                 (ts, name)).rowcount > 0
            return c.execute("UPDATE buckets SET over_quota_alert_at = ? WHERE name = ? "
                             "AND location = ?", (ts, name, location)).rowcount > 0

    def set_budget_number(self, name: str, budget_number: str | None) -> bool:
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE buckets SET budget_number = ? WHERE name = ?",
                (budget_number, name),
            )
            return cur.rowcount > 0

    def delete(self, name: str, location: str | None = None) -> bool:
        with self.db.tx() as c:
            if location is None:
                return c.execute("DELETE FROM buckets WHERE name = ?", (name,)).rowcount > 0
            return c.execute("DELETE FROM buckets WHERE name = ? AND location = ?",
                             (name, location)).rowcount > 0


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

    def count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) AS n FROM shares")["n"])

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

    def restore(self, key_id: int, rgw_uid: str, owner_ad_username: str, bucket_name: str,
                access: str, label: str, access_key: str | None,
                suspended: bool = False) -> dict[str, Any]:
        """Insert a key rebuilt from Ceph with its old id (so its Sid `bucketeer-key-<id>`
        still matches), state active; a suspended one counts as suspended directly.
        Raises sqlite3.IntegrityError when the id or the live uid is taken."""
        stamp = now()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO bucket_keys (id, rgw_uid, owner_ad_username, bucket_name, access, "
                "label, created_at, state, access_key, suspended_at, suspended_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (key_id, rgw_uid, owner_ad_username, bucket_name, access, label, stamp,
                 access_key, stamp if suspended else None, "direct" if suspended else None),
            )
        return self.get(key_id)  # type: ignore[return-value]

    def count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) AS n FROM bucket_keys")["n"])

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
               bytes_hint: int | None, location: str = "onprem") -> dict[str, Any]:
        with self.db.tx() as c:
            t = now()
            cur = c.execute(
                "INSERT INTO bucket_delete_jobs (bucket, owner_uid, owner_ad, state, "
                "objects_hint, bytes_hint, created_at, updated_at, location) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                (bucket, owner_uid, owner_ad, objects_hint, bytes_hint, t, t, location))
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

    def list_for(self, owner_uid: str, done_since: str,
                 location: str = "onprem") -> list[dict[str, Any]]:
        """The owner's jobs at `location`: every one not done, plus those done since
        `done_since`."""
        return self.db.query(
            "SELECT * FROM bucket_delete_jobs WHERE owner_uid = ? AND location = ? AND "
            "(state != 'done' OR finished_at >= ?) ORDER BY id",
            (owner_uid, location, done_since))

    def set_temp_key(self, job_id: int, access_key: str | None) -> None:
        """The access key id of a cloud job's temporary IAM key (None = deleted)."""
        with self.db.tx() as c:
            c.execute("UPDATE bucket_delete_jobs SET temp_key = ?, updated_at = ? WHERE id = ?",
                      (access_key, now(), job_id))

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
               created: str, expires: str, purge_before: str, cloud: bool = False) -> int:
        with self.db.tx() as c:
            c.execute("DELETE FROM setup_tokens WHERE created_at < ?", (purge_before,))
            return c.execute(
                "INSERT INTO setup_tokens (token_hash, ad_username, rgw_uid, access_key, os, "
                "created_at, expires_at, cloud) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token_hash, ad, uid, access_key, os_name, created, expires,
                 1 if cloud else 0)).lastrowid  # type: ignore[return-value]

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


class CloudAccounts(_Repo):
    """AD name <-> IAM user of a person (cloud, P6-9). AD names compare case-insensitively."""

    def get(self, ad_username: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM cloud_accounts WHERE ad_username = ?",
                                 (ad_username,))

    def record(self, ad_username: str, iam_user: str, source: str) -> dict[str, Any]:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO cloud_accounts (ad_username, iam_user, source, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (ad_username) DO UPDATE SET "
                "iam_user = excluded.iam_user, source = excluded.source, policy_synced = 0",
                (ad_username.lower(), iam_user, source, now()))
        return self.get(ad_username)  # type: ignore[return-value]

    def set_policy_synced(self, ad_username: str, synced: bool) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE cloud_accounts SET policy_synced = ? WHERE ad_username = ?",
                      (1 if synced else 0, ad_username))

    def for_names(self, names: list[str]) -> list[dict[str, Any]]:
        if not names:
            return []
        marks = ", ".join("?" * len(names))
        return self.db.query(
            f"SELECT * FROM cloud_accounts WHERE ad_username IN ({marks}) "  # noqa: S608
            "ORDER BY ad_username", tuple(names))

    def list(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM cloud_accounts ORDER BY ad_username")


class CloudKeySuspensions(_Repo):
    """Access keys Bucketeer deactivated with their suspended owner ('owner'|'offboarding')."""

    def add(self, access_key: str, iam_user: str, ad_username: str, by: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO cloud_key_suspensions (access_key, iam_user, ad_username, "
                "suspended_by, suspended_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (access_key) "
                "DO UPDATE SET suspended_by = excluded.suspended_by",
                (access_key, iam_user, ad_username.lower(), by, now()))

    def remove(self, access_key: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM cloud_key_suspensions WHERE access_key = ?", (access_key,))

    def for_user(self, ad_username: str) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM cloud_key_suspensions WHERE ad_username = ? "
                             "ORDER BY access_key", (ad_username,))

    def relabel(self, ad_username: str, old: str, new: str) -> int:
        with self.db.tx() as c:
            return c.execute(
                "UPDATE cloud_key_suspensions SET suspended_by = ? "
                "WHERE ad_username = ? AND suspended_by = ?", (new, ad_username, old)).rowcount


class KeyAges(_Repo):
    """How old access keys are (`notices.py`). Access keys are unique across RGW (and AWS)."""

    def created(self, access_key: str, uid: str, kind: str, ts: str | None = None) -> None:
        """Bucketeer made `access_key` just now: its age is exact from here."""
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO key_ages (access_key, uid, kind, first_seen, created) "
                "VALUES (?, ?, ?, ?, 1) ON CONFLICT (access_key) DO UPDATE SET "
                "uid = excluded.uid, kind = excluded.kind, first_seen = excluded.first_seen, "
                "created = 1, reminded_at = NULL",
                (access_key, uid, kind, ts or now()))

    def _rows(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            out.update({r["access_key"]: r for r in self.db.query(
                f"SELECT * FROM key_ages WHERE access_key IN ({', '.join('?' * len(part))})",
                tuple(part))})
        return out

    def seen(self, items: list[tuple[str, str, str]], ts: str) -> dict[str, dict[str, Any]]:
        """`items` = [(access_key, uid, kind)]: a key not recorded yet is recorded as first
        seen at `ts`. Returns the rows of all `items` by access key."""
        if items:
            with self.db.tx() as c:
                c.executemany(
                    "INSERT OR IGNORE INTO key_ages (access_key, uid, kind, first_seen) "
                    "VALUES (?, ?, ?, ?)", [(a, u, k, ts) for a, u, k in items])
        return self._rows([a for a, _, _ in items])

    def cloud(self, ad_username: str, keys: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
        """AWS keys `[(access_key, CreateDate)]` of one person: IAM's date is exact."""
        if keys:
            with self.db.tx() as c:
                c.executemany(
                    "INSERT INTO key_ages (access_key, uid, kind, first_seen, created) "
                    "VALUES (?, ?, 'cloud', ?, 1) ON CONFLICT (access_key) DO UPDATE SET "
                    "uid = excluded.uid, kind = 'cloud', first_seen = excluded.first_seen, "
                    "created = 1", [(a, ad_username, ts) for a, ts in keys])
        return self._rows([a for a, _ in keys])

    def set_reminded(self, access_key: str, ts: str) -> bool:
        with self.db.tx() as c:
            return c.execute("UPDATE key_ages SET reminded_at = ? WHERE access_key = ?",
                             (ts, access_key)).rowcount > 0

    def remove(self, *access_keys: str) -> int:
        with self.db.tx() as c:
            return sum(c.execute("DELETE FROM key_ages WHERE access_key = ?", (a,)).rowcount
                       for a in access_keys)

    def prune(self, uid: str, kinds: tuple[str, ...], keep: set[str]) -> int:
        """Forget the keys of `uid` (of these kinds) that no longer exist."""
        rows = self.db.query(
            f"SELECT access_key FROM key_ages WHERE uid = ? AND kind IN "
            f"({', '.join('?' * len(kinds))})", (uid, *kinds))
        gone = [r["access_key"] for r in rows if r["access_key"] not in keep]
        return self.remove(*gone) if gone else 0

    def list(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM key_ages ORDER BY uid, access_key")


class UsageDaily(_Repo):
    """Daily bucket sizes for billing; a later reading of the same day replaces it."""

    def record(self, day: str, rows: list[tuple]) -> None:
        """Rows are `(bucket, owner, bytes)` or `(bucket, owner, bytes, quota_bytes|None)`."""
        with self.db.tx() as c:
            c.executemany(
                "INSERT INTO usage_daily (day, bucket, owner, bytes, quota_bytes) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (day, bucket) DO UPDATE SET owner = excluded.owner, "
                "bytes = excluded.bytes, quota_bytes = excluded.quota_bytes",
                [(day, r[0], r[1], r[2], r[3] if len(r) > 3 else None) for r in rows])

    def between(self, first: str, last: str) -> list[dict[str, Any]]:
        return self.db.query("SELECT day, bucket, owner, bytes, quota_bytes FROM usage_daily "
                             "WHERE day BETWEEN ? AND ? ORDER BY day", (first, last))

    def readings(self, first: str, last: str,
                 buckets: list[str] | None = None) -> list[dict[str, Any]]:
        """`(day, bucket, bytes)` rows of the days `first`..`last`, for the given buckets
        (None: all), in one query; for the growth forecast (`forecast.py`)."""
        if buckets is None or len(buckets) > 500:  # a long IN list: filter afterwards
            rows = self.db.query("SELECT day, bucket, bytes FROM usage_daily "
                                 "WHERE day BETWEEN ? AND ? ORDER BY day", (first, last))
            if buckets is None:
                return rows
            wanted = set(buckets)
            return [r for r in rows if r["bucket"] in wanted]
        if not buckets:
            return []
        marks = ",".join("?" * len(buckets))
        return self.db.query(f"SELECT day, bucket, bytes FROM usage_daily "  # noqa: S608
                             f"WHERE day BETWEEN ? AND ? AND bucket IN ({marks}) ORDER BY day",
                             (first, last, *buckets))


class QuotaChanges(_Repo):
    """Bucket quota changes in whole TiB (the weekly increase limit's record)."""

    def add(self, *, person: str | None, uid: str, bucket: str, old_tib: int | None,
            new_tib: int | None, actor: str, counted: bool = True, location: str = "onprem",
            ts: str | None = None) -> int:
        increase = max(0, (new_tib or 0) - (old_tib or 0)) if new_tib is not None else 0
        with self.db.tx() as c:
            return c.execute(
                "INSERT INTO quota_changes (ts, person, uid, bucket, location, old_tib, "
                "new_tib, increase, counted, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts or now(), person, uid, bucket, location, old_tib, new_tib, increase,
                 1 if counted else 0, actor)).lastrowid

    def increases_since(self, person: str, since: str) -> list[dict[str, Any]]:
        """The counted increases of `person` after `since` (UTC ISO), oldest first."""
        return self.db.query(
            "SELECT ts, bucket, increase FROM quota_changes WHERE person = ? AND counted = 1 "
            "AND increase > 0 AND ts > ? ORDER BY ts", (person, since))

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM quota_changes ORDER BY id DESC LIMIT ?", (limit,))


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
