"""Nightly backup of the database and `.env` to an object-locked bucket (admin, PLAN 5).

`BACKUP_BUCKET` (empty = off; the log and the Backup tab then suggest `bucketeer-<random>`,
since the app never writes `.env`) names the bucket. On premises it belongs to the RGW user
`bucketeer-backup` (`croit.BACKUP_UID`), created through the admin client on the first run;
its key is read from the admin client each time (never stored) and, if it has none, one is
generated (`credentials.generate_key_pair`). That user is hidden from the admin lists and
refused as a login, mapping, share or ACL target like the Admin Ops service user.

The bucket is created once (idempotent) with that key: object lock at create, default
retention COMPLIANCE `retentionDays`, and the lifecycle rules `bucketeer-backup-expire`
(current objects after retention + 1 days), `bucketeer-backup-noncurrent` (noncurrent
versions after 1 day) and `bucketeer-backup-markers` (expired delete markers), written through
`s3docs.update_bucket_lifecycle`. Through S3 nobody, not even Bucketeer, can delete or shorten
a backup before it rolls off. Retention can only be lengthened; the lock period applies to
archives written from then on (older ones keep theirs), the lifecycle to all.

Each run uploads `backup/bucketeer-YYYY-MM-DDTHHMMZ.tar.gz` (UTC) holding `bucketeer.sqlite3`
(a consistent snapshot, sqlite backup API), `.env` (the file `load_settings` read: `./.env`;
left out with a note when absent) and `meta.json` (`format`, `createdAt`, `appVersion`,
`schemaVersion`, `tables` {name: rows}, `files` {name: {sha256, bytes}}, `envIncluded`,
`note`). Not encrypted (Q21): the bucket must stay private. A successful run also removes the
database's pre-upgrade copies (`db.remove_upgrade_copies`). One run at a time.

Destinations: `OnPremDestination` (RGW, the backup user's key, `S3_ENDPOINT_URL` or croit's
endpoint discovery). Backups to AWS (P6-12) add another class with the same methods
(`ensure`, `inspect`, `upload`, `download`, `latest`) chosen in `BackupService.destination`.

API (`/api/admin/backup`, admin, CSRF via the parent router):

* `GET` -> `{configured, bucket, suggestedBucket (only while not configured), destination,
  settings: {time, retentionDays}, nextRun, running, bucketState: {exists, objectLock, mode,
  retentionDays, versioning, error, note} | null, lastRun, lastSuccess, runs: [...]}`; never
  changes anything (no user or bucket is created by a GET).
* `PUT /settings {time?, retentionDays?}` -> `{settings, warning?}`; time `HH:MM` (server local),
  retentionDays 1..36500 and never below the current value (422); audit
  `admin.settings.backup` (target_type `setting`) when changed; a longer retention is applied
  to the bucket at once (created if needed; best effort, `warning` on failure, the next run
  applies it).
* `POST /run` -> the finished run `{id, startedAt, finishedAt, trigger manual|schedule, actor,
  outcome ok|failed, key, bytes, sha256, schemaVersion, envIncluded, error}`; 409 while
  backups are off or a run is going. Audit `backup.run` / `backup.failed` (target_type
  `backup`, target the bucket; actor `system` for the schedule). Runs are kept in the
  settings key `backup_runs` (newest first, the last 30).

Restore: `python3 app.py --restore-backup <object key|latest> [--force]` (`restore_main`).
"""

import asyncio
import base64
import contextlib
import hashlib
import importlib
import inspect
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from . import s3docs
from .auth import SessionUser, require_admin
from .config import Settings
from .credentials import generate_key_pair
from .croit import BACKUP_UID, CroitError, CroitUnavailable
from .db import DB_FILENAME, Database, get_db
from .s3client import default_factory

log = logging.getLogger("bucketeer.backup")

SCHEDULE_ON_START = True  # tests switch the nightly scheduler off and call run()
TICK = 60.0
KEY = "backup"
RUNS_KEY = "backup_runs"
KEEP_RUNS = 30
DEFAULTS = {"time": "03:00", "retentionDays": 30}
MAX_RETENTION = 36500
PREFIX = "backup/"
ARCHIVE_RE = re.compile(r"^backup/bucketeer-\d{4}-\d{2}-\d{2}T\d{4}Z\.tar\.gz\Z")
DB_MEMBER, ENV_MEMBER, META_MEMBER = DB_FILENAME, ".env", "meta.json"
RULE_IDS = ("bucketeer-backup-expire", "bucketeer-backup-noncurrent", "bucketeer-backup-markers")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\Z")
ENV_FILE: Path | None = None  # tests point it elsewhere; None = ./.env (what load_settings read)
REPO = Path(__file__).resolve().parent.parent


class BackupError(Exception):
    """A failure with a message fit for the admin page and the audit log (no secrets)."""


class AlreadyRunning(Exception):
    pass


def env_path() -> Path:
    return ENV_FILE or Path.cwd() / ".env"


def suggest_bucket() -> str:
    return f"bucketeer-{secrets.token_hex(6)}"


def archive_key(at: datetime) -> str:
    return f"{PREFIX}bucketeer-{at.astimezone(UTC):%Y-%m-%dT%H%M}Z.tar.gz"


def load(db: Database) -> dict[str, Any]:
    saved = db.settings.get(KEY) or {}
    out = dict(DEFAULTS)
    if _TIME.match(str(saved.get("time", ""))):
        out["time"] = saved["time"]
    days = saved.get("retentionDays")
    if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= MAX_RETENTION:
        out["retentionDays"] = days
    return out


def next_run(now: datetime, hhmm: str) -> datetime:
    """The first HH:MM strictly after `now` (naive server-local time)."""
    m = _TIME.match(hhmm)
    if not m:
        raise ValueError(f"bad time {hhmm!r}")
    at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def describe(exc: BaseException) -> str:
    """A short reason without secrets (admin error texts are scrubbed by the clients)."""
    if isinstance(exc, BackupError):
        return str(exc)
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"S3 refused {exc.operation_name}: {err.get('Code') or 'error'}"
    if isinstance(exc, BotoCoreError):
        return f"S3 not reachable ({type(exc).__name__})"
    if isinstance(exc, CroitError):
        return f"admin API error {exc.status}: {exc.message}"[:300]
    if isinstance(exc, CroitUnavailable):
        return "the admin API is not reachable"
    return f"{type(exc).__name__}"


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code") or "")


def _status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)


# -- the bucket (sync boto3, run in a thread) ----------------------------------------------


def lifecycle_rules(days: int) -> list[dict]:
    every = {"Status": "Enabled", "Filter": {"Prefix": ""}}
    return [
        {"ID": RULE_IDS[0], **every, "Expiration": {"Days": days + 1}},
        {"ID": RULE_IDS[1], **every, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
        {"ID": RULE_IDS[2], **every, "Expiration": {"ExpiredObjectDeleteMarker": True}},
    ]


def _lock_state(s3, bucket: str) -> tuple[bool, str | None, int | None]:
    try:
        cfg = s3.get_object_lock_configuration(Bucket=bucket).get("ObjectLockConfiguration") or {}
    except ClientError as e:
        if "ObjectLockConfiguration" in _code(e) or _status(e) == 404:
            return False, None, None
        raise
    ret = (cfg.get("Rule") or {}).get("DefaultRetention") or {}
    days = ret.get("Days") or (ret["Years"] * 365 if ret.get("Years") else None)
    return cfg.get("ObjectLockEnabled") == "Enabled", ret.get("Mode"), days


def ensure_bucket(s3, bucket: str, days: int) -> dict[str, Any]:
    """Create the bucket if needed and (re)apply lock and lifecycle. Never shortens."""
    created = False
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if _status(e) in (401, 403) or _code(e) in ("403", "AccessDenied", "Forbidden"):
            raise BackupError(f"The bucket {bucket} exists but belongs to another account. "
                              "Choose another BACKUP_BUCKET name.") from None
        if _status(e) != 404 and _code(e) not in ("404", "NoSuchBucket", "NotFound"):
            raise
        try:
            s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
            created = True
            log.info("created backup bucket %s with object lock", bucket)
        except ClientError as e2:
            if _code(e2) == "BucketAlreadyExists":
                raise BackupError(f"The bucket name {bucket} is taken by another account. "
                                  "Choose another BACKUP_BUCKET name.") from None
            if _code(e2) != "BucketAlreadyOwnedByYou":
                raise
    enabled, mode, current = _lock_state(s3, bucket)
    if not enabled:
        raise BackupError(f"The bucket {bucket} has no object lock (it was not created by "
                          "Bucketeer). Choose a new BACKUP_BUCKET name.")
    effective = max(days, current or 0)
    if mode != "COMPLIANCE" or current != effective:
        s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration={
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": effective}}})
    s3docs.update_bucket_lifecycle(
        s3, bucket, lambda cur: s3docs.upsert_rules(cur, lifecycle_rules(effective)))
    return {"created": created, "retentionDays": effective}


def inspect_bucket(s3, bucket: str) -> dict[str, Any]:
    out: dict[str, Any] = {"exists": False}
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if _status(e) == 404 or _code(e) in ("404", "NoSuchBucket", "NotFound"):
            out["note"] = "The first backup run creates it."
            return out
        if _status(e) in (401, 403):
            out["error"] = "The bucket exists but belongs to another account."
            return out
        raise
    enabled, mode, days = _lock_state(s3, bucket)
    versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status") or "Off"
    out.update(exists=True, objectLock=enabled, mode=mode, retentionDays=days,
               versioning=versioning.lower())
    return out


def upload_file(s3, bucket: str, key: str, path: Path) -> None:
    data = path.read_bytes()
    md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
    # object lock with a default retention: S3 wants Content-MD5 (or a checksum) on PUT
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentMD5=md5,
                  ContentType="application/gzip")
    head = s3.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength") or -1) != len(data):
        raise BackupError("The uploaded archive has the wrong size.")


def latest_key(s3, bucket: str) -> str | None:
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        keys += [o["Key"] for o in page.get("Contents") or [] if ARCHIVE_RE.match(o["Key"])]
    return max(keys) if keys else None


# -- destinations ---------------------------------------------------------------------------


class OnPremDestination:
    """The backup bucket on the Ceph cluster, owned by the RGW user `bucketeer-backup`."""

    kind = "onprem"

    def __init__(self, client, settings: Settings, endpoints,
                 factory: Callable[..., Any] = default_factory):
        self.client, self.settings, self.endpoints = client, settings, endpoints
        self.factory = factory
        self.bucket = settings.backup_bucket

    async def _key(self, create: bool) -> tuple[str, str] | None:
        """The backup user's first key (creating user and key when `create`)."""
        c = self.client
        user = await c.get_user(BACKUP_UID)
        if user is None:
            if not create:
                return None
            try:
                await c.create_user(BACKUP_UID, "Bucketeer backups")
            except CroitError as e:
                if e.status not in (400, 409, 422) or await c.get_user(BACKUP_UID) is None:
                    raise
            log.info("created the RGW user %s for backups", BACKUP_UID)
            user = await c.get_user(BACKUP_UID) or {}
        if user.get("suspended"):
            raise BackupError(f"The RGW user {BACKUP_UID} is suspended in the cluster.")

        def own(keys):
            return [k for k in keys or [] if isinstance(k, dict) and k.get("user") == BACKUP_UID
                    and k.get("access_key") and k.get("secret_key")]

        keys = own(await c.list_keys(BACKUP_UID))
        if not keys:
            if not create:
                return None
            access, secret = generate_key_pair()
            await c.put_key(BACKUP_UID, access, secret)
            keys = own(await c.list_keys(BACKUP_UID))
            if not keys:
                raise BackupError(f"Could not give the RGW user {BACKUP_UID} an access key.")
        return keys[0]["access_key"], keys[0]["secret_key"]

    async def _s3(self, create: bool = True):
        key = await self._key(create)
        if key is None:
            return None
        endpoint = await self.endpoints.resolve(BACKUP_UID)
        if not endpoint:
            raise BackupError("The S3 endpoint is not known (set S3_ENDPOINT_URL).")
        return self.factory(endpoint, key[0], key[1], self.settings)

    async def ensure(self, retention_days: int) -> dict[str, Any]:
        s3 = await self._s3()
        return await asyncio.to_thread(ensure_bucket, s3, self.bucket, retention_days)

    async def inspect(self) -> dict[str, Any]:
        s3 = await self._s3(create=False)
        if s3 is None:
            return {"exists": False, "note": "The first backup run creates the backup "
                                             "account and the bucket."}
        return await asyncio.to_thread(inspect_bucket, s3, self.bucket)

    async def upload(self, key: str, path: Path) -> None:
        s3 = await self._s3()
        await asyncio.to_thread(upload_file, s3, self.bucket, key, path)

    async def latest(self) -> str | None:
        s3 = await self._s3(create=False)
        if s3 is None:
            return None
        return await asyncio.to_thread(latest_key, s3, self.bucket)

    async def download(self, key: str, path: Path) -> None:
        s3 = await self._s3(create=False)
        if s3 is None:
            raise BackupError(f"The RGW user {BACKUP_UID} or its key does not exist.")
        await asyncio.to_thread(s3.download_file, self.bucket, key, str(path))


# -- the archive ----------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def app_version() -> str | None:
    try:
        out = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=REPO, timeout=3,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001  no git, not a checkout: the version is optional
        return None


def table_counts(path: Path) -> tuple[int, dict[str, int]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        return version, {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                         for n in names}
    finally:
        conn.close()


def build_archive(db: Database, env_file: Path, workdir: Path,
                  at: datetime) -> tuple[Path, dict[str, Any]]:
    """Write the tar.gz into `workdir`; returns its path and the meta document."""
    snap = workdir / DB_MEMBER
    db.snapshot(snap)
    files: dict[str, bytes] = {DB_MEMBER: snap.read_bytes()}
    note = None
    try:
        files[ENV_MEMBER] = env_file.read_bytes()
    except FileNotFoundError:
        note = f"{env_file} does not exist: the archive holds no .env"
    except OSError as e:
        note = f"{env_file} could not be read ({type(e).__name__}): the archive holds no .env"
    if note:
        log.warning("backup: %s", note)
    version, tables = table_counts(snap)
    meta = {
        "format": 1, "createdAt": at.astimezone(UTC).isoformat(timespec="seconds"),
        "appVersion": app_version(), "schemaVersion": version, "tables": tables,
        "files": {n: {"sha256": _sha256(b), "bytes": len(b)} for n, b in files.items()},
        "envIncluded": ENV_MEMBER in files, "note": note,
    }
    files[META_MEMBER] = json.dumps(meta, indent=2, sort_keys=True).encode()
    out = workdir / "archive.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for name in (META_MEMBER, DB_MEMBER, ENV_MEMBER):
            if name not in files:
                continue
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(files[name]), 0o600, int(at.timestamp())
            tar.addfile(info, io.BytesIO(files[name]))
    snap.unlink()
    return out, meta


def read_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Members of a backup archive, checked against meta.json. Raises BackupError."""
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name not in (META_MEMBER, DB_MEMBER, ENV_MEMBER) or not m.isfile():
                    raise BackupError(f"Unexpected archive member {m.name!r}.")
                f = tar.extractfile(m)
                files[m.name] = f.read() if f else b""
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BackupError(f"Not a readable backup archive ({type(e).__name__}).") from None
    if META_MEMBER not in files or DB_MEMBER not in files:
        raise BackupError("The archive has no meta.json or no database.")
    try:
        meta = json.loads(files.pop(META_MEMBER))
        listed = meta["files"]
    except (ValueError, KeyError, TypeError):
        raise BackupError("meta.json is not valid.") from None
    if set(listed) != set(files):
        raise BackupError("The files in the archive do not match meta.json.")
    for name, data in files.items():
        if _sha256(data) != listed[name].get("sha256"):
            raise BackupError(f"SHA-256 mismatch for {name}: the archive is damaged.")
    return meta, files


# -- the service ----------------------------------------------------------------------------


class BackupService:
    def __init__(self, app, now: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.app, self.now, self.sleep = app, now, sleep
        self.running = False
        self.suggestion = suggest_bucket()
        self._floor = datetime.min
        self._tasks: set[asyncio.Task] = set()
        self.factory: Callable[..., Any] = default_factory  # tests spy on the backup key

    @property
    def db(self) -> Database:
        return self.app.state.db

    @property
    def bucket(self) -> str:
        return self.app.state.settings.backup_bucket

    def destination(self) -> OnPremDestination:
        # P6-12: when the global location is Cloud, return the AWS destination here.
        s = self.app.state
        return OnPremDestination(s.croit, s.settings, s.endpoints, self.factory)

    def settings(self) -> dict[str, Any]:
        return load(self.db)

    def runs(self) -> list[dict[str, Any]]:
        return list(self.db.settings.get(RUNS_KEY) or [])

    def describe_off(self) -> str:
        return (f"BACKUP_BUCKET is not set: backups are off. Suggested: "
                f"BACKUP_BUCKET={self.suggestion}")

    def _save_run(self, rec: dict[str, Any]) -> None:
        with self.db.tx():
            runs = [r for r in self.runs() if r.get("id") != rec["id"]]
            self.db.settings.set(RUNS_KEY, [rec, *runs][:KEEP_RUNS], rec["actor"])

    async def run(self, trigger: str, actor: str) -> dict[str, Any]:
        """One backup run, shielded so a dropped request does not cancel it."""
        if not self.bucket:
            raise BackupError("Backups are off: BACKUP_BUCKET is not set.")
        if self.running:
            raise AlreadyRunning
        self.running = True
        task = asyncio.get_running_loop().create_task(self._run(trigger, actor))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def _run(self, trigger: str, actor: str) -> dict[str, Any]:
        db = self.db
        at = datetime.now(UTC)
        runs = await db.run(self.runs)
        rec: dict[str, Any] = {
            "id": max((r.get("id", 0) for r in runs), default=0) + 1,
            "startedAt": at.isoformat(timespec="seconds"), "finishedAt": None,
            "trigger": trigger, "actor": actor, "destination": "onprem",
            "bucket": self.bucket, "key": archive_key(at), "outcome": "running",
            "bytes": None, "sha256": None, "schemaVersion": None, "envIncluded": None,
            "error": None,
        }
        try:
            dest = self.destination()
            rec["destination"] = dest.kind
            s = await db.run(self.settings)
            made = await dest.ensure(s["retentionDays"])
            data_dir = Path(self.app.state.settings.data_dir)
            with tempfile.TemporaryDirectory(
                    prefix=".backup-", dir=data_dir if data_dir.is_dir() else None) as tmp:
                path, meta = await asyncio.to_thread(build_archive, db, env_path(), Path(tmp), at)
                body = path.read_bytes()
                await dest.upload(rec["key"], path)
            rec.update(outcome="ok", bytes=len(body), sha256=_sha256(body),
                       schemaVersion=meta["schemaVersion"], envIncluded=meta["envIncluded"],
                       note=meta["note"], retentionDays=made["retentionDays"])
            await db.run(db.audit.add, actor, "backup.run", "backup", self.bucket, {
                "trigger": trigger, "key": rec["key"], "bytes": rec["bytes"],
                "sha256": rec["sha256"], "schemaVersion": rec["schemaVersion"],
                "envIncluded": rec["envIncluded"], "bucketCreated": made["created"],
                "retentionDays": made["retentionDays"], "destination": dest.kind})
            for p in await db.run(db.remove_upgrade_copies):
                log.info("backup done: removed pre-upgrade copy %s", p)
            log.info("backup %s written to %s (%d bytes)", rec["key"], self.bucket, rec["bytes"])
        except asyncio.CancelledError:
            rec.update(outcome="failed", error="Stopped by a shutdown.")
            raise
        except Exception as e:  # noqa: BLE001  every failure is recorded and audited
            rec.update(outcome="failed", error=describe(e))
            log.warning("backup to %s failed: %s", self.bucket, rec["error"])
            await db.run(db.audit.add, actor, "backup.failed", "backup", self.bucket or "-", {
                "trigger": trigger, "key": rec["key"], "error": rec["error"]})
        finally:
            self.running = False
            rec["finishedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
            with contextlib.suppress(Exception):
                await db.run(self._save_run, rec)
        return rec

    async def apply_retention(self) -> None:
        dest = self.destination()
        await dest.ensure((await self.db.run(self.settings))["retentionDays"])

    async def state(self) -> dict[str, Any]:
        s = await self.db.run(self.settings)
        runs = await self.db.run(self.runs)
        bucket_state = None
        if self.bucket:
            try:
                bucket_state = await self.destination().inspect()
            except Exception as e:  # noqa: BLE001  shown on the page, not raised
                bucket_state = {"exists": None, "error": describe(e)}
        return {
            "configured": bool(self.bucket), "bucket": self.bucket or None,
            "suggestedBucket": None if self.bucket else self.suggestion,
            "destination": "onprem", "settings": s,
            "nextRun": (self.next_run().isoformat(timespec="minutes") if self.bucket
                        else None),
            "running": self.running, "bucketState": bucket_state,
            "lastRun": runs[0] if runs else None,
            "lastSuccess": next((r for r in runs if r.get("outcome") == "ok"), None),
            "runs": runs, "user": BACKUP_UID,
        }

    def note_settings_changed(self) -> None:
        self._floor = self.now()

    def next_run(self) -> datetime:
        return next_run(max(self.now(), self._floor), self.settings()["time"])

    async def schedule_loop(self) -> None:
        """Back up daily at the configured time; re-reads the settings every `TICK`."""
        if not self.bucket:  # the lifespan logged the suggestion
            return
        after = self.now()
        while True:
            target = None
            try:
                after = max(after, self._floor)
                target = next_run(after, self.settings()["time"])
                wait = (target - self.now()).total_seconds()
                if wait > 0:
                    await self.sleep(min(wait, TICK))
                    continue
                try:
                    await self.run("schedule", "system")
                except AlreadyRunning:
                    pass
                after = max(self.now(), target)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("backup scheduler")
                after = max(self.now(), target or self.now())
                await self.sleep(TICK)

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


# -- API --------------------------------------------------------------------------------------


router = APIRouter(prefix="/admin/backup", tags=["admin"], dependencies=[Depends(require_admin)])
Db = Annotated[Database, Depends(get_db)]
Admin = Annotated[SessionUser, Depends(require_admin)]


def get_service(request: Request) -> BackupService:
    return request.app.state.backup


Svc = Annotated[BackupService, Depends(get_service)]


@router.get("")
async def get_backup(svc: Svc):
    return await svc.state()


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: StrictStr | None = None
    retentionDays: StrictInt | None = None


@router.put("/settings")
async def put_settings(body: SettingsBody, admin: Admin, db: Db, svc: Svc):
    before = await db.run(load, db)
    errors = []
    if body.time is not None and not _TIME.match(body.time):
        errors.append({"field": "time", "message": "Time must be HH:MM (24 hours, server time)."})
    if body.retentionDays is not None:
        if not 1 <= body.retentionDays <= MAX_RETENTION:
            errors.append({"field": "retentionDays",
                           "message": f"Enter a whole number of days from 1 to {MAX_RETENTION}."})
        elif body.retentionDays < before["retentionDays"]:
            errors.append({"field": "retentionDays", "message": (
                f"Retention can only be lengthened (it is {before['retentionDays']} days): "
                "backups already written stay locked for their period.")})
    if errors:
        return JSONResponse(status_code=422,
                            content={"detail": errors[0]["message"], "errors": errors})
    after = dict(before)
    if body.time is not None:
        after["time"] = body.time
    if body.retentionDays is not None:
        after["retentionDays"] = body.retentionDays
    out: dict[str, Any] = {"settings": after}
    if after != before:
        await db.run(db.settings.set, KEY, after, admin.username)
        svc.note_settings_changed()
        await db.run(db.audit.add, admin.username, "admin.settings.backup", "setting", KEY,
                     {"before": before, "after": after})
        if after["retentionDays"] > before["retentionDays"] and svc.bucket:
            try:
                await svc.apply_retention()
            except Exception as e:  # noqa: BLE001  the next run applies it
                out["warning"] = (f"Saved. The bucket could not be updated now "
                                  f"({describe(e)}); the next backup run applies it.")
    return out


@router.post("/run")
async def run_now(admin: Admin, svc: Svc):
    if not svc.bucket:
        raise HTTPException(409, "Backups are off: set BACKUP_BUCKET in .env first.")
    try:
        return await svc.run("manual", admin.username)
    except AlreadyRunning:
        raise HTTPException(409, "A backup is running already.") from None


# -- restore (command line) -------------------------------------------------------------------


CATCH_UP_MODULES = ("bucketeer.rebuild", "bucketeer.ceph_rebuild", "bucketeer.rebuild_from_ceph")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def app_is_running(settings: Settings) -> bool:
    import httpx

    host = "127.0.0.1" if settings.host in ("0.0.0.0", "", "::") else settings.host
    try:
        r = httpx.get(f"http://{host}:{settings.port}{settings.health_check_path}", timeout=2)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _unique(path: Path) -> Path:
    """`path`, or `path-1`, `path-2`... when it exists (two restores in one second)."""
    out, i = path, 0
    while out.exists():
        i += 1
        out = path.with_name(f"{path.name}-{i}")
    return out


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(0o600)


def replace_database(target: Path, data: bytes) -> Path | None:
    """Put `data` in place of the database at `target`; the old one (WAL folded in) is kept
    as `<file>.replaced-<time>`. Returns that copy's path, or None when there was none."""
    stamp = _stamp()
    new = _unique(target.with_name(f"{target.name}.restoring-{stamp}"))
    _write_private(new, data)
    conn = sqlite3.connect(f"file:{new}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if ok != "ok":
        new.unlink()
        raise BackupError(f"The database in the archive fails the integrity check ({ok}).")
    kept = None
    if target.exists():
        kept = _unique(target.with_name(f"{target.name}.replaced-{stamp}"))
        os.close(os.open(kept, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        src, out = sqlite3.connect(target), sqlite3.connect(kept)
        try:
            src.backup(out)  # includes what is still in the WAL
            out.execute("PRAGMA journal_mode = DELETE")
        finally:
            out.close()
            src.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
    os.replace(new, target)
    return kept


async def _catch_up(settings: Settings, db: Database, client) -> tuple[str, Any]:
    for name in CATCH_UP_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        fn = getattr(mod, "rebuild_from_ceph", None)
        if fn is None:
            continue
        offered = {"fill_gaps": True, "settings": settings, "db": db, "client": client,
                   "croit": client}
        params = inspect.signature(fn).parameters
        result = fn(**{k: v for k, v in offered.items() if k in params})
        if inspect.isawaitable(result):
            result = await result
        return "done", result
    return "unavailable", None


async def restore(settings: Settings, which: str, *, force: bool = False,
                  client=None, factory: Callable[..., Any] = default_factory,
                  out: Callable[[str], None] = print) -> dict[str, Any]:
    """Download, verify and install a backup. Raises BackupError with a clear message."""
    from .credentials import EndpointResolver
    from .croit import CroitClient
    from .rgw_admin import RgwAdminClient

    if not settings.backup_bucket:
        raise BackupError("BACKUP_BUCKET is not set in .env.")
    if not force and app_is_running(settings):
        raise BackupError(f"Bucketeer answers on port {settings.port}: stop it first "
                          "(or pass --force).")
    own = client is None
    if own:
        client = (RgwAdminClient(settings) if settings.admin_backend == "rgw"
                  else CroitClient(settings))
    try:
        import time

        dest = OnPremDestination(client, settings, EndpointResolver(client, settings, time.time),
                                 factory)
        key = which
        if which == "latest":
            key = await dest.latest()
            if not key:
                raise BackupError(f"No backup found in {settings.backup_bucket}.")
        elif not ARCHIVE_RE.match(which):
            raise BackupError("Give an object key like backup/bucketeer-2026-01-31T0300Z.tar.gz "
                              "or 'latest'.")
        out(f"Downloading {key} from {settings.backup_bucket} ...")
        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=data_dir) as tmp:
            path = Path(tmp) / "archive.tar.gz"
            try:
                await dest.download(key, path)
            except ClientError as e:
                raise BackupError(describe(e)) from None
            meta, files = read_archive(path)
        out(f"Checked SHA-256 of {', '.join(sorted(files))} (schema v{meta.get('schemaVersion')},"
            f" written {meta.get('createdAt')}).")
        target = data_dir / DB_FILENAME
        kept = replace_database(target, files[DB_MEMBER])
        if kept:
            out(f"The previous database is kept as {kept}")
        db = Database(target)  # runs the migrations (with a pre-upgrade copy) when older
        try:
            out(f"Database installed at {target} (schema v{db.version}).")
            env_written = None
            if ENV_MEMBER in files:
                live = env_path()
                base = live.resolve() if live.exists() else live
                env_written = _unique(base.with_name(f".env.restored-{_stamp()}"))
                _write_private(env_written, files[ENV_MEMBER])
                out(f"The backed-up .env is at {env_written} (0600). Compare it with "
                    f"{live} and copy what you need by hand.")
            else:
                out("The archive holds no .env.")
            try:
                status, result = await _catch_up(settings, db, client)
            except Exception as e:  # noqa: BLE001  the restore itself is done
                status, result = "failed", describe(e)
            if status == "unavailable":
                out("Catch-up from Ceph (rebuild_from_ceph) isn't available in this version: "
                    "buckets, keys and shares made after the backup are not added.")
            else:
                out(f"Catch-up from Ceph: {status}: {result}")
            details = {"key": key, "bucket": settings.backup_bucket,
                       "createdAt": meta.get("createdAt"),
                       "schemaFrom": meta.get("schemaVersion"), "schemaTo": db.version,
                       "replacedCopy": str(kept) if kept else None,
                       "envRestored": str(env_written) if env_written else None,
                       "catchUp": status,
                       "catchUpResult": result if isinstance(result, dict | list | str | int)
                       else (str(result) if result is not None else None)}
            db.audit.add("system", "system.restore", "backup", settings.backup_bucket, details)
            return details
        finally:
            db.close()
    finally:
        if own:
            await client.aclose()


def restore_main(argv: list[str]) -> int:
    """`app.py --restore-backup <object key|latest> [--force]`."""
    from .config import load_settings

    args = list(argv)
    force = "--force" in args
    try:
        which = args[args.index("--restore-backup") + 1]
    except (ValueError, IndexError):
        print("usage: python3 app.py --restore-backup <object key|latest> [--force]")
        return 2
    if which.startswith("--"):
        print("usage: python3 app.py --restore-backup <object key|latest> [--force]")
        return 2
    settings = load_settings()
    try:
        asyncio.run(restore(settings, which, force=force))
    except BackupError as e:
        print(f"Restore failed: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"Restore failed: {describe(e)}")
        return 1
    print("Restore finished. Start Bucketeer again.")
    return 0
