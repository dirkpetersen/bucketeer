"""Rebuild SQLite rows from the cluster after the database was lost (PLAN 1 and 6, P6-3).

`python3 app.py --rebuild-from-ceph [--fill-gaps|--full] [--force]` (see `main`), or
`await rebuild_from_ceph(app, fill_gaps)` from code (the restore of P6-7 runs it with
`fill_gaps=True` after a backup is restored).

What comes back, read with each bucket OWNER's own S3 key (the owner always comes from the
admin client's bucket list, never from a tag):
- `buckets`: from the managed tags (`bucket_facts`): budget number, creation date
  (`bucketeer-created`), creator (`bucketeer-creator`). A bucket whose tags hold none of
  these gets no row (it is a bucket older than the portal; nothing would be in the row).
  `creation_known` is 1 only when both creation tags are there, so a row never writes a
  guessed creator or date. Tags are a record, not proof: a `bucketeer-owner` or
  `bucketeer-bucket` tag that disagrees with RGW is reported as a problem and the row is
  left `tag_synced = 0`, so the owner's next reconcile rewrites the tags.
- `bucket_keys`: every `bucketeer-key-<id>` statement in a bucket policy whose principal is
  an existing key user (`<owner>+<label>`, the owner part matching the bucket owner) gives a
  row with the SAME id (so the Sid still matches), owner AD name from the key user's display
  name (`bucketeer key: <owner> / <bucket> / <ro|rw>`, or the older
  `Bucketeer key <label> for <owner> (<bucket>)`), else from the owner uid
  (`rgw_user.ad_names_for_uid`), access from the statement's actions, the public half of its
  access key, state `active`, suspended (as `direct`) when RGW has the key user suspended.
  Key users without a statement are only reported.
- `shares`: every `bucketeer-share-*` statement whose principal is an existing person gives a
  live row (recipient AD name by `ad_names_for_uid`, display names from the admin client,
  access from the actions, `created_by` `system`, `created_at` now).

Modes: `fill_gaps=True` only inserts what is missing and never updates or deletes a row.
`fill_gaps=False` ("full") refuses (`RebuildRefused`) unless the three tables are empty or
`force=True`; it never deletes either, rows that exist are skipped the same way.

One bucket never fails the run: problems are collected. The report is
`{mode, added: {buckets, bucket_keys, shares}, skipped: [{table, item, reason}],
problems: [{table, item, reason, detail?}]}`; audited `system.rebuild` (actor `system`,
target_type `database`, details counts and mode). Only listing users or buckets in the
admin client failing ends the run (an exception, nothing written).

Not recoverable from the cluster: audit log, settings and uid mappings, profiles, revoked
keys and shares (so a deleted key label could be used again), offboarding history, usage
readings, delete jobs, setup tokens.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import sys
import time
from datetime import date
from types import SimpleNamespace
from typing import Any

from botocore.exceptions import ClientError
from fastapi import HTTPException

from . import bucket_facts as bf
from . import s3docs
from .bucket_keys import KEY_BARE
from .croit import CroitClientError
from .rgw_user import ad_names_for_uid, same_uid, split_uid

log = logging.getLogger("bucketeer.rebuild")

TABLES = ("buckets", "bucket_keys", "shares")
CONCURRENCY = 4
KEY_SID = re.compile(r"\Abucketeer-key-(\d{1,18})\Z")
SHARE_PREFIX = s3docs.PREFIX + "share-"
NEW_NAME = re.compile(r"\Abucketeer key: (?P<owner>\S+) / (?P<bucket>\S+) / (?P<access>ro|rw)\Z")
OLD_NAME = re.compile(r"\ABucketeer key (?P<label>\S+) for (?P<owner>\S+) \((?P<bucket>\S+)\)\Z")
DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class RebuildRefused(RuntimeError):
    """Full mode on a database that already has rows (pass force, or use fill-gaps)."""


def is_key_uid(uid: str) -> bool:
    """A bucket-key user by its uid alone (`<owner>+<label>`); the database may be empty."""
    return bool(KEY_BARE.match(split_uid(uid)[1]))


def _short(full: str) -> str:
    return full.partition(":")[2] or full


def _principal_uid(st: dict) -> str | None:
    """`tenant$uid` (or bare uid) named by a statement's single AWS principal."""
    p = st.get("Principal")
    p = p.get("AWS") if isinstance(p, dict) else None
    if isinstance(p, list):
        p = p[0] if len(p) == 1 else None
    if not isinstance(p, str) or ":user/" not in p:
        return None
    head, _, bare = p.partition(":user/")
    tenant = head.rpartition(":")[2]
    return f"{tenant}${bare}" if tenant else bare


def _access(st: dict) -> str:
    actions = st.get("Action") or []
    actions = [actions] if isinstance(actions, str) else actions
    return "readwrite" if any(a in ("s3:PutObject", "s3:*") for a in actions) else "read"


def _read(s3, bucket: str) -> dict[str, Any]:
    """Tags and policy of one bucket (sync, owner's client); a refusal of either is kept
    as an error code next to the other."""
    out: dict[str, Any] = {"tags": [], "policy": None}
    try:
        out["tags"] = s3.get_bucket_tagging(Bucket=bucket).get("TagSet") or []
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("NoSuchTagSet", "NoSuchTagSetError"):
            out["tags_error"] = code or "error"
    try:
        out["policy"] = s3.get_bucket_policy(Bucket=bucket).get("Policy")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "NoSuchBucketPolicy":
            out["policy_error"] = code or "error"
    return out


class _Run:
    def __init__(self, state, fill_gaps: bool):
        self.state, self.db, self.admin = state, state.db, state.croit
        rgw = getattr(state, "rgw", None)
        self.tenant = rgw.tenant if rgw is not None else state.settings.s3_tenant
        self.mode = "fill_gaps" if fill_gaps else "full"
        self.added = dict.fromkeys(TABLES, 0)
        self.skipped: list[dict] = []
        self.problems: list[dict] = []

    def skip(self, table: str, item: str, reason: str) -> None:
        self.skipped.append({"table": table, "item": item, "reason": reason})

    def problem(self, table: str, item: str, reason: str, detail: str | None = None) -> None:
        entry = {"table": table, "item": item, "reason": reason}
        if detail:
            entry["detail"] = detail
        self.problems.append(entry)
        log.warning("rebuild: %s %s: %s%s", table, item, reason, f" ({detail})" if detail else "")

    def report(self) -> dict[str, Any]:
        return {"mode": self.mode, "added": self.added, "skipped": self.skipped,
                "problems": self.problems}

    # -- reading -------------------------------------------------------------------------

    async def read_bucket(self, b: dict, sem: asyncio.Semaphore) -> dict | None:
        full, owner = b["bucket"], b["owner"]
        async with sem:
            try:
                return await self.state.s3.call(owner, _read, _short(full))
            except HTTPException as e:
                self.problem("buckets", full, "owner_s3_unavailable", str(e.detail))
            except Exception as e:  # connection trouble, timeouts: this bucket only
                self.problem("buckets", full, "s3_error", type(e).__name__)
        return None

    # -- buckets -------------------------------------------------------------------------

    async def bucket_row(self, full: str, owner: str, tags: list[dict]) -> None:
        db = self.db
        t = {x.get("Key"): x.get("Value") for x in tags if isinstance(x, dict)}
        budget, created, creator = t.get(bf.BUDGET), t.get(bf.CREATED), t.get(bf.CREATOR)
        synced = True
        if t.get(bf.OWNER) and t[bf.OWNER] != bf.owner_value(owner):
            self.problem("buckets", full, "tag_owner_mismatch",
                         f"tag says {t[bf.OWNER]}, RGW says {bf.owner_value(owner)}")
            synced = False
        if t.get(bf.BUCKET) and t[bf.BUCKET] != full:
            self.problem("buckets", full, "tag_bucket_mismatch", f"tag says {t[bf.BUCKET]}")
            synced = False
        if created is not None:
            try:
                if not DATE.match(created):
                    raise ValueError
                date.fromisoformat(created)
            except ValueError:
                self.problem("buckets", full, "bad_created_tag", created[:40])
                created = None
        if not (budget or created or creator):
            return self.skip("buckets", full, "no_record_in_tags")
        if await db.run(db.buckets.get, full) is not None:
            return self.skip("buckets", full, "exists")
        known = bool(created and creator)
        try:
            await db.run(db.buckets.create, full, owner, budget or None, creator or None,
                         synced, creation_known=known,
                         created_at=f"{created}T00:00:00.000000Z" if created else None)
        except sqlite3.Error as e:
            return self.problem("buckets", full, "insert_failed", type(e).__name__)
        self.added["buckets"] += 1

    # -- bucket keys ---------------------------------------------------------------------

    async def key_row(self, full: str, owner: str, st: dict, key_id: int,
                      users: dict[str, dict]) -> None:
        db, item = self.db, f"{full} {st.get('Sid')}"
        uid = _principal_uid(st)
        if not uid or not is_key_uid(uid):
            return self.problem("bucket_keys", item, "bad_principal")
        user = users.get(uid)
        if user is None:
            return self.skip("bucket_keys", item, "key_user_missing")
        o_tenant, o_bare = split_uid(owner)
        k_tenant, k_bare = split_uid(uid)
        k_owner, _, label = k_bare.partition("+")
        if k_tenant != o_tenant or k_owner.lower() != o_bare.lower():
            return self.problem("bucket_keys", item, "key_owner_mismatch",
                                f"key user {uid}, bucket owner {owner}")
        existing = await db.run(db.bucket_keys.get, key_id)
        if existing is not None:
            if same_uid(existing["rgw_uid"], uid):
                return self.skip("bucket_keys", item, "exists")
            return self.problem("bucket_keys", item, "id_taken",
                                f"row {key_id} belongs to {existing['rgw_uid']}")
        live = await db.run(db.bucket_keys.get_by_uid, uid)
        if live is not None and live["revoked_at"] is None:
            return self.skip("bucket_keys", item, "exists")
        access = _access(st)
        name = str(user.get("name") or "")
        m = NEW_NAME.match(name) or OLD_NAME.match(name)
        owner_ad = m["owner"] if m else None
        if m and m["bucket"] != _short(full):
            self.problem("bucket_keys", item, "display_name_bucket_mismatch", m["bucket"])
        if m and "access" in m.groupdict() and m["access"] != ("rw" if access == "readwrite"
                                                               else "ro"):
            self.problem("bucket_keys", item, "display_name_access_mismatch",
                         "the policy statement wins")
        if not owner_ad:
            names = await db.run(ad_names_for_uid, db, owner, self.tenant)
            owner_ad = names[0] if names else None
        if not owner_ad:
            return self.problem("bucket_keys", item, "owner_ad_unknown", owner)
        access_key = None
        try:
            keys = await self.admin.list_keys(uid)
            access_key = next((k["access_key"] for k in keys or []
                               if isinstance(k, dict) and k.get("user") == uid
                               and k.get("access_key")), None)
        except CroitClientError as e:
            self.problem("bucket_keys", item, "keys_unreadable", type(e).__name__)
        try:
            await db.run(db.bucket_keys.restore, key_id, uid, owner_ad, full, access, label,
                         access_key, bool(user.get("suspended")))
        except sqlite3.Error as e:
            return self.problem("bucket_keys", item, "insert_failed", type(e).__name__)
        self.added["bucket_keys"] += 1

    # -- shares --------------------------------------------------------------------------

    async def share_row(self, full: str, owner: str, st: dict, users: dict[str, dict]) -> None:
        db, item = self.db, f"{full} {st.get('Sid')}"
        uid = _principal_uid(st)
        if not uid or is_key_uid(uid):
            return self.problem("shares", item, "bad_principal")
        if same_uid(uid, owner):
            return self.skip("shares", item, "recipient_is_owner")
        rcpt = users.get(uid)
        if rcpt is None:
            return self.skip("shares", item, "recipient_missing")
        if await db.run(db.shares.live_for, full, uid) is not None:
            return self.skip("shares", item, "exists")
        names = await db.run(ad_names_for_uid, db, uid, self.tenant)
        if not names:
            return self.problem("shares", item, "recipient_ad_unknown", uid)
        owner_name = str((users.get(owner) or {}).get("name") or "")
        try:
            await db.run(db.shares.create, full, owner, owner_name, names[0], uid,
                         str(rcpt.get("name") or ""), _access(st), "system")
        except sqlite3.Error as e:
            return self.problem("shares", item, "insert_failed", type(e).__name__)
        self.added["shares"] += 1

    # -- the run -------------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        self.admin.invalidate_lists()
        users = {str(u["uid"]): u for u in await self.admin.list_users() if u.get("uid")}
        rows = sorted((b for b in await self.admin.list_buckets()
                       if isinstance(b, dict) and b.get("bucket")),
                      key=lambda b: str(b["bucket"]))
        todo = []
        for b in rows:
            owner = str(b.get("owner") or "")
            if not owner:
                self.problem("buckets", b["bucket"], "no_owner")
            elif is_key_uid(owner):
                self.problem("buckets", b["bucket"], "owner_is_bucket_key", owner)
            else:
                todo.append(b)
        sem = asyncio.Semaphore(CONCURRENCY)
        reads = await asyncio.gather(*(self.read_bucket(b, sem) for b in todo))
        keys, shares, named = [], [], set()
        for b, got in zip(todo, reads, strict=True):
            full, owner = b["bucket"], b["owner"]
            if got is None:
                continue
            if "tags_error" in got:
                self.problem("buckets", full, "tags_unreadable", got["tags_error"])
            else:
                await self.bucket_row(full, owner, got["tags"])
            if "policy_error" in got:
                self.problem("bucket_keys", full, "policy_unreadable", got["policy_error"])
                continue
            try:
                statements = s3docs.managed_statements(got["policy"])
            except (s3docs.S3DocError, ValueError) as e:
                self.problem("bucket_keys", full, "policy_unparsable", str(e)[:200])
                continue
            for st in statements:
                sid = str(st.get("Sid"))
                if m := KEY_SID.match(sid):
                    keys.append((full, owner, st, int(m[1])))
                    if uid := _principal_uid(st):
                        named.add(uid)
                elif sid.startswith(SHARE_PREFIX):
                    shares.append((full, owner, st))
        for full, owner, st, key_id in keys:
            await self.key_row(full, owner, st, key_id, users)
        for uid in sorted(users):
            if is_key_uid(uid) and uid not in named:
                row = await self.db.run(self.db.bucket_keys.get_by_uid, uid)
                if row is None:
                    self.skip("bucket_keys", uid, "key_user_without_statement")
        for full, owner, st in shares:
            await self.share_row(full, owner, st, users)
        return self.report()


def _parts(settings, db, client, s3=None) -> SimpleNamespace:
    """What a run needs, built like `create_app` does (no server, nothing started)."""
    from .credentials import EndpointResolver
    from .s3client import S3Clients

    if s3 is None:
        s3 = S3Clients(client, settings, EndpointResolver(client, settings, time.time))
    return SimpleNamespace(db=db, croit=client, s3=s3, settings=settings, rgw=None)


async def rebuild_from_ceph(app_or_parts=None, fill_gaps: bool = False, *, force: bool = False,
                            actor: str = "system", settings=None, db=None, client=None,
                            croit=None, s3=None) -> dict[str, Any]:
    """Rebuild `buckets`, `bucket_keys` and `shares` from the cluster (module docstring).
    `app_or_parts` is the FastAPI app or anything with `.state` / the same attributes
    (`db`, `croit`, `s3`, `rgw`); without it pass `settings`, `db` and the admin client
    (`client` or `croit`; `s3` optional), e.g. from the restore command. Raises
    RebuildRefused (full mode, rows present, no force) and the admin client's errors when
    users or buckets cannot be listed."""
    if app_or_parts is None:
        if settings is None or db is None or (client or croit) is None:
            raise TypeError("pass an app, or settings, db and client")
        state = _parts(settings, db, client or croit, s3)
    else:
        state = getattr(app_or_parts, "state", app_or_parts)
    db = state.db
    if not fill_gaps and not force:
        counts = {"buckets": await db.run(db.buckets.count),
                  "bucket_keys": await db.run(db.bucket_keys.count),
                  "shares": await db.run(db.shares.count)}
        if any(counts.values()):
            raise RebuildRefused(
                "The database already has rows (" + ", ".join(
                    f"{k}: {v}" for k, v in counts.items()) + "). Use --fill-gaps to add only "
                "what is missing, or --force to run the full rebuild anyway (it never "
                "overwrites or deletes rows).")
    run = _Run(state, fill_gaps)
    report = await run.run()
    reasons: dict[str, int] = {}
    for p in report["problems"]:
        reasons[p["reason"]] = reasons.get(p["reason"], 0) + 1
    await db.run(db.audit.add, actor, "system.rebuild", "database", "bucketeer", {
        "mode": report["mode"], "force": force, "added": report["added"],
        "skipped": len(report["skipped"]), "problems": len(report["problems"]),
        "problem_reasons": reasons,
        "problem_items": [f"{p['table']} {p['item']}" for p in report["problems"]][:50]})
    log.info("rebuild from Ceph (%s): added %s, %d skipped, %d problems", report["mode"],
             report["added"], len(report["skipped"]), len(report["problems"]))
    return report


# -- command line ----------------------------------------------------------------------


def summary(report: dict[str, Any]) -> str:
    added = report["added"]
    lines = [f"Rebuild from Ceph ({report['mode'].replace('_', ' ')}):",
             "  added: " + ", ".join(f"{k} {v}" for k, v in added.items())]
    if report["skipped"]:
        lines.append(f"  skipped ({len(report['skipped'])}):")
        lines += [f"    {s['table']}: {s['item']}: {s['reason']}" for s in report["skipped"]]
    if report["problems"]:
        lines.append(f"  problems ({len(report['problems'])}):")
        lines += [f"    {p['table']}: {p['item']}: {p['reason']}"
                  + (f" ({p['detail']})" if p.get("detail") else "") for p in report["problems"]]
    else:
        lines.append("  no problems")
    return "\n".join(lines)


USAGE = "usage: python3 app.py --rebuild-from-ceph [--fill-gaps|--full] [--force]"


async def _run_cli(app, fill_gaps: bool, force: bool) -> int:
    try:
        report = await rebuild_from_ceph(app, fill_gaps, force=force)
    except RebuildRefused as e:
        print(f"Refused: {e}")
        return 1
    except CroitClientError as e:
        print(f"Failed: the admin plane could not be read ({type(e).__name__}). "
              "Nothing was written.")
        return 1
    print(summary(report))
    return 0


def main(argv: list[str], app=None) -> int:
    """`--rebuild-from-ceph [--fill-gaps|--full] [--force]`; `app` lets tests pass a built
    app (with its database open). Otherwise Settings, the database (migrated), the admin
    client and the S3 clients are built exactly as `create_app` does, without serving."""
    args = [a for a in argv if a != "--rebuild-from-ceph"]
    unknown = [a for a in args if a not in ("--fill-gaps", "--full", "--force")]
    if unknown or ("--fill-gaps" in args and "--full" in args):
        print(USAGE)
        return 1
    fill_gaps, force = "--fill-gaps" in args, "--force" in args
    if app is not None:
        return asyncio.run(_run_cli(app, fill_gaps, force))

    from pathlib import Path

    from dotenv import load_dotenv
    from pydantic import ValidationError

    from .config import Settings
    from .db import DB_FILENAME, Database
    from .main import create_app

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        settings = Settings()
    except ValidationError as e:
        print(f"Invalid configuration: {len(e.errors())} problem(s); run the app to see them.")
        return 1
    try:
        db = Database(Path(settings.data_dir) / DB_FILENAME)
    except Exception as e:
        print(f"Failed: the database could not be opened ({e}).")
        return 1
    try:
        built = create_app(settings, db=db)

        async def go() -> int:
            try:
                return await _run_cli(built, fill_gaps, force)
            finally:
                await built.state.rgw.aclose()
                await built.state.croit.aclose()

        return asyncio.run(go())
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
