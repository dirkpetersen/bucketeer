"""Offboarding job: suspend the RGW accounts (and bucket keys) of people who left AD.

CLAUDE.md "Users and access" > Offboarding and the Q10 safeguards. The job NEVER
deletes anything and only suspends.

* **Scope** = people Bucketeer knows (`known_accounts`: recorded by `RgwResolver` when it
  creates or links an account, plus admin uid-mapping targets, added at each run). Other
  RGW users and bucket-key users are never touched; keys follow their owner.
* **AD check**: `authenticator.account_statuses(names)` (service bind only) answers
  `ok | disabled | not_in_group | not_found` for each name, or raises on any LDAP trouble,
  which aborts the whole run.
* **Candidates** are people whose AD status is not `ok` and whose RGW account is not
  suspended already (an admin's suspension is never touched).
* **Abort rule** (Q14, admin settings `maxPercent` 1..100, default 10; `minPeopleForPercent`
  1..1000, default 4; `maxPeople` 1..100000, default 20). With C candidates and K known people
  (all in scope, suspended ones included) the run aborts and suspends nobody when
  `C * 100 > maxPercent * K AND C >= minPeopleForPercent` (strictly more than the percentage,
  at least the minimum), OR `C > maxPeople` (strictly more). So 1 of 4 (25 %) passes with
  minimum 4, 4 of 20 (20 %) aborts at 10 %, exactly 10 % passes, and exactly `maxPeople` passes.
  Any LDAP error aborts regardless. The thresholds in force are stored in the run's `counts`
  (`counts.thresholds`) and quoted in the abort reason.
* **Modes** (`settings` key `offboarding` `{mode: report|enforce, time: "HH:MM", maxPercent,
  minPeopleForPercent, maxPeople}`; stored settings lacking the thresholds use the defaults),
  default report, 03:00 server local time): report records who WOULD be suspended; enforce
  suspends the user in croit and their live bucket keys (`suspended_by = 'offboarding'`), flags the
  known-account row, invalidates the resolver cache and audits per user.
* **Coming back**: `OffboardingService.on_login` (called from the post-login warm-up) unsuspends a
  person whose row is flagged, and only the keys with `suspended_by = 'offboarding'`. Any admin
  suspend/unsuspend clears the flag first (`admin._set_suspended`), so an admin's decision is
  never undone automatically.
* One run at a time; each run is stored in `offboarding_runs` (last 30). A scheduled run is an
  asyncio task started from the lifespan (`SCHEDULE_ON_START`, off in tests).

API (`/api/admin/offboarding`, admin only, CSRF via the parent router):

* `GET` -> `{settings: {mode, time, maxPercent, minPeopleForPercent, maxPeople}, running,
  nextRun (ISO with offset), knownPeople,
  runs: [{id, startedAt, finishedAt, mode, trigger, actor,
  outcome (running|ok|aborted|failed), reason, counts, results: [{uid, adUsernames, reason
  (disabled|not_in_group|not_found), action (would_suspend|not_suspended|suspended|partial|
  failed|unknown|skipped|keys_pending|keys_completed), keys, note?}], thresholds:
  {maxPercent, minPeopleForPercent, maxPeople}|null (those in force for that run)}]}` newest
  first.
* `POST /run` -> the finished run (same shape as one `runs` entry); it runs inline, shielded
  from a dropped connection (`GET` shows `running` meanwhile). 409 while one is running.
* `PUT /settings {mode?, time?, maxPercent?, minPeopleForPercent?, maxPeople?}` -> the new
  settings; 422 `{detail, errors: [{field, message}]}` for bad values; audit
  `admin.settings.offboarding` (target_type `setting`) when something changed.

Audit (actor `system` for scheduled runs and login unsuspends, else the admin):
`offboarding.run` (ok), `offboarding.aborted`, `offboarding.run_failed`,
`offboarding.suspend`, `offboarding.unsuspend`, `offboarding.unsuspend_failed`.
Targets: `offboarding_run` + run id, `rgw_user` + uid.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .admin import Admin, apply_key_suspension
from .auth import require_admin
from .croit import CroitClientError, CroitError
from .db import Database, get_db
from .ldapauth import InvalidCredentials, LdapUnavailable
from .rgw_user import RgwAccount, ad_names_for_uid, is_key_uid

log = logging.getLogger("bucketeer.offboarding")

SCHEDULE_ON_START = True  # tests switch it off (like bucket_keys.CLEANUP_ON_START)
MAX_STORED_RESULTS = 200
DEFAULT_SETTINGS = {"mode": "report", "time": "03:00", "maxPercent": 10,
                    "minPeopleForPercent": 4, "maxPeople": 20}
# name -> (low, high), inclusive
LIMITS = {"maxPercent": (1, 100), "minPeopleForPercent": (1, 1000), "maxPeople": (1, 100000)}
THRESHOLD_KEYS = tuple(LIMITS)
MODES = ("report", "enforce")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\Z")
TICK = 60.0  # the scheduler re-reads the settings at least this often


class AlreadyRunning(Exception):
    pass


# -- settings and schedule (pure) ---------------------------------------------------------


def valid_limit(name: str, value: Any) -> bool:
    lo, hi = LIMITS[name]
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def should_abort(candidates: int, known: int, th: dict[str, Any]) -> bool:
    """The Q14 abort rule (see the module docstring for the exact boundaries)."""
    if not candidates:
        return False
    over_pct = (candidates * 100 > th["maxPercent"] * known
                and candidates >= th["minPeopleForPercent"])
    return over_pct or candidates > th["maxPeople"]


def limits_text(th: dict[str, Any]) -> str:
    n = th["minPeopleForPercent"]
    return (f"more than {th['maxPercent']}% of known people and at least "
            f"{n} {'person' if n == 1 else 'people'}, or more than {th['maxPeople']} people")


def load_settings(store) -> dict[str, Any]:
    saved = store.get("offboarding", None) or {}
    out = dict(DEFAULT_SETTINGS)
    if saved.get("mode") in MODES:
        out["mode"] = saved["mode"]
    if _TIME.match(str(saved.get("time", ""))):
        out["time"] = saved["time"]
    for k in THRESHOLD_KEYS:
        if valid_limit(k, saved.get(k)):
            out[k] = saved[k]
    return out


def next_run(now: datetime, hhmm: str) -> datetime:
    """The first HH:MM strictly after `now` (naive server-local time)."""
    m = _TIME.match(hhmm)
    if not m:
        raise ValueError(f"bad time {hhmm!r}")
    at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


# -- the service -------------------------------------------------------------------------


class OffboardingService:
    def __init__(self, app, now: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.app, self.now, self.sleep = app, now, sleep
        self.running = False
        self.lock = asyncio.Lock()  # every suspend/unsuspend of an RGW user (job, login, admin)
        self._floor = datetime.min  # a settings change: no run is due before this
        self._tasks: set[asyncio.Task] = set()
        self._last: asyncio.Task | None = None

    # state shortcuts
    @property
    def db(self) -> Database:
        return self.app.state.db

    @property
    def croit(self):
        return self.app.state.croit

    @property
    def rgw(self):
        return self.app.state.rgw

    def settings(self) -> dict[str, str]:
        return load_settings(self.db.settings)

    async def _audit(self, actor: str, action: str, kind: str, target: str, details: dict) -> None:
        try:
            await self.db.run(self.db.audit.add, actor, action, kind, target, details)
        except Exception:
            log.exception("audit write failed for %s", action)

    # -- running -----------------------------------------------------------------

    async def start(self, trigger: str, actor: str) -> int:
        """Begin a run in the background; returns its id. Raises AlreadyRunning."""
        if self.running:
            raise AlreadyRunning
        self.running = True
        try:
            cfg = await self.db.run(load_settings, self.db.settings)  # one snapshot
            mode = cfg["mode"]
            th = {k: cfg[k] for k in THRESHOLD_KEYS}
            run_id = await self.db.run(self.db.offboarding_runs.start, mode, trigger, actor)
            task = asyncio.get_running_loop().create_task(
                self._execute(run_id, mode, trigger, actor, th))
        except BaseException:
            self.running = False
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._last = task
        return run_id

    async def run(self, trigger: str, actor: str) -> dict[str, Any]:
        """start() and wait for the finished run (scheduler, tests)."""
        run_id = await self.start(trigger, actor)
        await self._last
        return await self.db.run(self.db.offboarding_runs.get, run_id)

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _execute(self, run_id: int, mode: str, trigger: str, actor: str,
                       th: dict[str, Any]) -> None:
        outcome, reason, counts, results = "failed", None, {}, []
        try:
            outcome, reason, counts, results = await self._run(run_id, mode, actor, th)
        except (LdapUnavailable, InvalidCredentials) as e:
            outcome = "aborted"
            reason = f"AD could not be read ({str(e)[:150]}); nobody was suspended."
        except Exception as e:  # noqa: BLE001 - recorded, never raised into the scheduler
            log.exception("offboarding run %s failed", run_id)
            outcome, reason = "failed", f"{type(e).__name__}: {str(e)[:200]}"
        counts = {**counts, "thresholds": th}
        try:
            await self.db.run(self.db.offboarding_runs.finish, run_id, outcome, reason,
                              counts, results[:MAX_STORED_RESULTS])
        except Exception:
            log.exception("storing offboarding run %s failed", run_id)
        finally:
            self.running = False
        action = {"ok": "offboarding.run", "aborted": "offboarding.aborted"}.get(
            outcome, "offboarding.run_failed")
        await self._audit(actor, action, "offboarding_run", str(run_id), {
            "mode": mode, "trigger": trigger, "outcome": outcome, "reason": reason,
            "counts": counts,
        })

    async def _scope(self) -> tuple[dict[str, dict], int, list[dict]]:
        """People in scope by EXACT croit uid: {uid: {"names": [...], "suspended": bool}}, the
        number of known links whose RGW user is gone, and result entries for people skipped
        because only a differently-cased uid exists in croit (never guessed)."""
        db = self.db
        users = {str(u["uid"]): u for u in await self.croit.list_users(fresh=True)}
        lowered = {u.lower() for u in users}

        def sync_and_load() -> list[dict]:
            maps = {m["ad_username"].lower(): m["rgw_uid"] for m in db.uid_mappings.list()}
            for m in db.uid_mappings.list():  # admin mapping targets count (exact uid)
                if m["rgw_uid"] in users and not is_key_uid(db, m["rgw_uid"]):
                    db.known_accounts.record(m["ad_username"], m["rgw_uid"], "mapping")
            rows = []
            for r in db.known_accounts.list():
                if is_key_uid(db, r["rgw_uid"]):
                    continue
                if r["source"] == "mapping" and maps.get(r["ad_username"].lower()) != r["rgw_uid"]:
                    db.known_accounts.forget_mapping_rows(r["ad_username"], maps.get(
                        r["ad_username"].lower()))  # the mapping is gone or points elsewhere
                    continue
                rows.append(r)
            return rows

        people: dict[str, dict] = {}
        missing = 0
        skipped: dict[str, dict] = {}
        for row in await db.run(sync_and_load):
            uid = row["rgw_uid"]
            user = users.get(uid)
            if user is None:
                if uid.lower() in lowered:
                    e = skipped.setdefault(uid, {
                        "uid": uid, "adUsernames": [], "reason": "uid_case_mismatch",
                        "action": "skipped", "keys": [],
                        "note": "croit only has this uid in a different case; not guessed"})
                    e["adUsernames"].append(row["ad_username"])
                else:
                    missing += 1
                continue
            p = people.setdefault(uid, {"names": [], "rows": [],
                                        "suspended": bool(user.get("suspended"))})
            p["names"].append(row["ad_username"])
            p["rows"].append(row)
        return people, missing, list(skipped.values())

    async def _run(self, run_id: int, mode: str, actor: str, th: dict[str, Any]):
        people, missing, skipped = await self._scope()
        counts: dict[str, Any] = {
            "known": len(people), "knownMissingInCroit": missing, "checked": 0,
            "alreadySuspended": sum(1 for p in people.values() if p["suspended"]),
            "candidates": 0, "suspended": 0, "keysSuspended": 0, "failed": 0,
            "skippedCaseMismatch": len(skipped),
        }
        todo = {uid: p for uid, p in people.items() if not p["suspended"]}
        names = sorted({n for p in todo.values() for n in p["names"]}, key=str.lower)
        statuses = await self.app.state.authenticator.account_statuses(names) if names else {}
        counts["checked"] = len(names)

        # Accounts the job suspended (flag set, suspended in croit) whose bucket keys are still
        # live: an earlier suspend whose outcome was unknown. Enforce finishes the keys.
        pending: list[dict[str, Any]] = []
        for uid, p in sorted(people.items()):
            if p["suspended"] and any(r["offboarding_suspended_at"] for r in p["rows"]):
                live = [k for k in await self._live_keys(uid, p["names"])
                        if not k["suspended_at"]]
                if live:
                    pending.append({"uid": uid, "adUsernames": p["names"], "reason": "keys_pending",
                                    "action": "keys_pending", "keys": [],
                                    "note": "suspended by the job earlier; "
                                            f"{len(live)} bucket key(s) still live"})
        counts["keysPending"] = len(pending)

        candidates: list[tuple[str, dict, str]] = []
        for uid, p in sorted(todo.items()):
            st = [statuses.get(n) for n in p["names"]]
            if any(s is None for s in st):
                raise LdapUnavailable("directory answer incomplete")
            if all(s != "ok" for s in st):  # every AD name of this account has left
                candidates.append((uid, p, st[0]))
        counts["candidates"] = len(candidates)

        results: list[dict[str, Any]] = [
            {"uid": uid, "adUsernames": p["names"], "reason": why,
             "action": "would_suspend", "keys": []}
            for uid, p, why in candidates
        ]
        if should_abort(len(candidates), len(people), th):
            what = (f"{len(candidates)} of {len(people)} known people would be suspended "
                    f"(limits: {limits_text(th)})")
            for r in results:
                r["action"] = "not_suspended"
            return "aborted", f"{what}. AD may be misconfigured; nobody was suspended.", \
                counts, results + pending + skipped
        if mode != "enforce":
            return "ok", None, counts, results + pending + skipped

        started = (await self.db.run(self.db.offboarding_runs.get, run_id))["started_at"]
        for (uid, p, _why), res in zip(candidates, results, strict=True):
            await self._suspend_one(uid, p, res, counts, actor, run_id, started)
        for entry in pending:
            await self._complete_keys(entry, people[entry["uid"]], counts, actor, run_id)
        outcome = "failed" if counts["failed"] else "ok"
        reason = f"{counts['failed']} suspension(s) failed; see the results." if counts["failed"] \
            else None
        return outcome, reason, counts, results + pending + skipped

    async def _suspend_one(self, uid: str, p: dict, res: dict, counts: dict, actor: str,
                           run_id: int, started: str) -> None:
        db, croit = self.db, self.croit
        async with self.lock:
            rows = await db.run(db.known_accounts.for_uid, uid)
            try:
                if any((r["last_login"] or "") > started for r in rows):
                    res.update(action="skipped", note="logged in during the run")
                    return
                fresh = await croit.get_user(uid)
                if fresh is None or fresh.get("suspended"):
                    res.update(action="skipped", note="already suspended or gone")
                    counts["alreadySuspended"] += 1
                    return
            except CroitClientError as e:
                res.update(action="failed", note=f"croit: {str(e)[:150]}")
                counts["failed"] += 1
                return
            # Claim the suspension BEFORE asking croit: if the answer is lost (timeout, 5xx,
            # crash) the account may be suspended anyway, and then it must count as the job's
            # so the person's next login restores it. Only a definite 4xx releases the claim.
            for r in rows:
                await db.run(db.known_accounts.set_offboarding, r["ad_username"], True)
            try:
                await croit.update_user(uid, suspended=True)
            except CroitClientError as e:
                log.warning("offboarding: suspending %s failed: %s", uid, e)
                definite = isinstance(e, CroitError) and 400 <= e.status < 500
                self.rgw.invalidate(uid=uid)  # it may be suspended after all
                if definite:
                    for r in rows:
                        await db.run(db.known_accounts.set_offboarding, r["ad_username"], False)
                res.update(action="failed" if definite else "unknown",
                           note=f"croit: {str(e)[:150]}" + (
                               "" if definite else
                               " (outcome unknown: the account may be suspended and its bucket "
                               "keys may still need suspending; it is recorded as the job's, "
                               "the next enforce run finishes the keys and a login restores "
                               "it)"))
                counts["failed"] += 1
                return
            self.rgw.invalidate(uid=uid)
            keys = await self._live_keys(uid, p["names"])
            done, failed = await apply_key_suspension(
                croit, db, [k for k in keys if not k["suspended_at"]], True,
                "offboarding", uid)
        res.update(action="partial" if failed else "suspended", keys=done)
        if failed:
            res["note"] = ("bucket keys not confirmed suspended (croit error; they may or may "
                           "not be suspended, check them): " + ", ".join(failed))
            counts["failed"] += 1
        counts["suspended"] += 1
        counts["keysSuspended"] += len(done)
        await self._audit(actor, "offboarding.suspend", "rgw_user", uid, {
            "run": run_id, "ad_usernames": p["names"], "reason": res["reason"],
            "keys": done, "keys_failed": failed,
        })

    async def _complete_keys(self, res: dict, p: dict, counts: dict, actor: str,
                             run_id: int) -> None:
        """Suspend the live keys of an account the job already suspended (`keys_only`)."""
        db, uid = self.db, res["uid"]
        async with self.lock:
            rows = await db.run(db.known_accounts.for_uid, uid)
            if not any(r["offboarding_suspended_at"] for r in rows):  # settled meanwhile
                res.update(action="skipped", note="no longer suspended by the job")
                return
            try:
                current = await self.croit.get_user(uid)  # never act on a stale list
            except CroitClientError as e:  # one bad read fails this entry, not the run
                res.update(action="failed", note=f"croit: {str(e)[:150]}")
                counts["failed"] += 1
                return
            if current is None or not current.get("suspended"):
                res.update(action="skipped", note="no longer suspended in RGW", keys=[])
                return
            keys = [k for k in await self._live_keys(uid, p["names"]) if not k["suspended_at"]]
            done, failed = await apply_key_suspension(
                self.croit, db, keys, True, "offboarding", uid)
        res.update(action="partial" if failed else "keys_completed", keys=done, note=None)
        if failed:
            res["note"] = "bucket keys not confirmed suspended: " + ", ".join(failed)
            counts["failed"] += 1
        counts["keysSuspended"] += len(done)
        await self._audit(actor, "offboarding.suspend", "rgw_user", uid, {
            "run": run_id, "ad_usernames": p["names"], "keys_only": True,
            "keys": done, "keys_failed": failed,
        })

    async def _live_keys(self, uid: str, names: list[str]) -> list[dict]:
        db = self.db
        owners = {n.lower(): n for n in names}
        for n in await db.run(ad_names_for_uid, db, uid, self.rgw.tenant):
            owners.setdefault(n.lower(), n)
        return [k for o in owners.values() for k in await db.run(db.bucket_keys.list, o)]

    # -- coming back ---------------------------------------------------------------

    async def on_login(self, username: str, acct: RgwAccount) -> None:
        """After a successful login: undo the JOB's suspension of this person, nothing else."""
        if not acct.resolved or not acct.uid:
            return
        db = self.db
        async with self.lock:  # the flag is read only inside the lock: no stale fast path
            row = await db.run(db.known_accounts.get, username)
            if not row or not row["offboarding_suspended_at"] or row["rgw_uid"] != acct.uid:
                return
            uid = row["rgw_uid"]
            failed: list[str] = []
            try:
                user = await self.croit.get_user(uid)
                if user is not None and user.get("suspended"):
                    await self.croit.update_user(uid, suspended=False)
                names = [r["ad_username"] for r in await db.run(db.known_accounts.for_uid, uid)]
                keys = [k for k in await self._live_keys(uid, names)
                        if k["suspended_by"] == "offboarding"]
                done, failed = await apply_key_suspension(
                    self.croit, db, keys, False, "offboarding", uid)
            except CroitClientError as e:
                log.warning("offboarding: unsuspending %s failed: %s", uid, e)
                await self._audit("system", "offboarding.unsuspend_failed", "rgw_user", uid,
                                  {"ad_username": username, "error": str(e)[:150]})
                return
            self.rgw.invalidate(uid=uid)
            if failed:  # the flag stays: the next login retries the keys
                await self._audit("system", "offboarding.unsuspend_failed", "rgw_user", uid,
                                  {"ad_username": username, "keys_failed": failed})
                return
            await db.run(db.known_accounts.clear_offboarding_for_uid, uid)
        await self._audit("system", "offboarding.unsuspend", "rgw_user", uid,
                          {"ad_username": username, "keys": done})

    # -- schedule ------------------------------------------------------------------

    def note_settings_changed(self) -> None:
        """A new run time counts from now: a time already past today waits for tomorrow."""
        self._floor = self.now()

    def next_run(self) -> datetime:
        return next_run(self.now(), self.settings()["time"])

    async def schedule_loop(self) -> None:
        """Run daily at the configured time. Re-reads the settings every `TICK` seconds,
        so a changed time takes effect without a restart. Never raises."""
        try:
            await self.db.run(self.db.offboarding_runs.fail_stale)
        except Exception:
            log.exception("could not settle stale offboarding runs")
        after = self.now()  # the next run is the first HH:MM strictly after this
        while True:
            try:
                after = max(after, self._floor)  # a changed time never fires a run at once
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
                log.exception("offboarding scheduler")
                after = max(self.now(), target)
                await self.sleep(TICK)


# -- routes ------------------------------------------------------------------------------

router = APIRouter(prefix="/admin/offboarding", tags=["admin"],
                   dependencies=[Depends(require_admin)])


def get_service(request: Request) -> OffboardingService:
    return request.app.state.offboarding


Service = Annotated[OffboardingService, Depends(get_service)]
Db = Annotated[Database, Depends(get_db)]


def _run_view(r: dict) -> dict[str, Any]:
    return {
        "id": r["id"], "startedAt": r["started_at"], "finishedAt": r["finished_at"],
        "mode": r["mode"], "trigger": r["trigger"], "actor": r["actor"],
        "outcome": r["outcome"], "reason": r["reason"], "counts": r["counts"],
        "thresholds": (r["counts"] or {}).get("thresholds"),
        "results": r["results"],
    }


@router.get("")
async def overview(_a: Admin, svc: Service, db: Db):
    cfg = await db.run(load_settings, db.settings)
    runs = await db.run(db.offboarding_runs.list, db.offboarding_runs.KEEP)
    known = await db.run(db.known_accounts.list)
    return {
        "settings": cfg,
        "running": svc.running,
        "nextRun": next_run(svc.now(), cfg["time"]).astimezone().isoformat(timespec="minutes"),
        "knownPeople": len(known),
        "runs": [_run_view(r) for r in runs],
    }


@router.post("/run")
async def run_now(admin: Admin, svc: Service, db: Db):
    """Runs now and answers with the finished run (the job itself is a task that a dropped
    connection does not cancel; `GET` shows `running` meanwhile). 409 while one is running."""
    try:
        run_id = await svc.start("manual", admin.username)
    except AlreadyRunning:
        raise HTTPException(409, "An offboarding run is already in progress.") from None
    await asyncio.shield(svc._last)
    return _run_view(await db.run(db.offboarding_runs.get, run_id))


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str | None = None
    time: str | None = None
    maxPercent: Any = None
    minPeopleForPercent: Any = None
    maxPeople: Any = None


@router.put("/settings")
async def put_settings(body: SettingsBody, admin: Admin, db: Db, svc: Service):
    given = body.model_fields_set & {"mode", "time", *THRESHOLD_KEYS}
    if not given or any(getattr(body, k) is None for k in given):
        raise HTTPException(422, "Give a mode, a time and/or thresholds.")
    errors: list[dict[str, str]] = []
    if body.mode is not None and body.mode not in MODES:
        errors.append({"field": "mode", "message": "mode must be 'report' or 'enforce'."})
    if body.time is not None and not _TIME.match(body.time):
        errors.append({"field": "time",
                       "message": "time must be HH:MM (24 hours, server local time)."})
    for k in THRESHOLD_KEYS:
        v = getattr(body, k)
        if v is not None and not valid_limit(k, v):
            lo, hi = LIMITS[k]
            errors.append({"field": k, "message": f"Enter a whole number from {lo} to {hi}."})
    if errors:
        return JSONResponse(status_code=422,
                            content={"detail": errors[0]["message"], "errors": errors})
    before = await db.run(load_settings, db.settings)
    after = dict(before)
    for k in ("mode", "time", *THRESHOLD_KEYS):
        if getattr(body, k) is not None:
            after[k] = getattr(body, k)
    if after != before:
        await db.run(db.settings.set, "offboarding", after, admin.username)
        svc.note_settings_changed()
        await db.run(db.audit.add, admin.username, "admin.settings.offboarding", "setting",
                     "offboarding", {"before": before, "after": after})
    return after
