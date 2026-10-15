"""The offboarding job: scope, report/enforce, safeguards, coming back, schedule, API.

Fake LDAP (tests/test_auth.py), the in-process mock croit (tests/test_admin.py).
`env.user` is alice's session, `env.admin` bob's (an admin). The job's AD view of a person
is changed with `leave(env, name, how)` on the fake directory.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from bucketeer import offboarding
from bucketeer.ldapauth import AdAuthenticator, LdapUnavailable
from bucketeer.offboarding import AlreadyRunning, next_run
from bucketeer.rgw_user import RgwResolver

from .backends import (
    admin_backend,  # noqa: F401  (fixture: both admin backends)
    admin_secret,
    error_response,
)
from .test_admin import TOKEN, Env, H
from .test_auth import SECRET_PW, USERS_GROUP, FakeLdap, make_settings

DN = "CN={},OU=Users,DC=example,DC=org"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.svc = e.app.state.offboarding
    e.croit_users = e.mock.state.croit.users
    # alice and bob are people Bucketeer knows (their account is resolved once)
    for c in (e.user, e.admin):
        assert c.get("/api/account").json()["status"] == "ready"
    yield e
    e.db.close()


@pytest.fixture
def lenient(monkeypatch):
    """Lift the 10% limit so one person can be offboarded among a few."""
    monkeypatch.setattr(offboarding, "MAX_PERCENT", 100)


def go(env, trigger="manual", actor="bob"):
    return asyncio.run(env.svc.run(trigger, actor))


def set_mode(env, mode):
    r = env.admin.put("/api/admin/offboarding/settings", json={"mode": mode}, headers=H)
    assert r.status_code == 200


def leave(env, name, how="disabled"):
    u = env.fake.users[DN.format(name)]
    if how == "disabled":
        u["userAccountControl"] = "514"  # ACCOUNTDISABLE | NORMAL_ACCOUNT
    elif how == "group":
        u["groups"] = set()
    else:
        del env.fake.users[DN.format(name)]


def suspended(env, uid):
    return env.croit_users[uid].suspended


def acts(env, prefix):
    return [a for a in env.db.audit.query(limit=1000) if a["action"].startswith(prefix)]


def results(run):
    return {r["uid"]: r for r in run["results"]}


# -- scope -----------------------------------------------------------------------------


def test_logins_record_known_accounts(env):
    rows = {r["ad_username"]: r for r in env.db.known_accounts.list()}
    assert set(rows) == {"alice", "bob"}
    assert rows["alice"]["rgw_uid"] == "alice" and rows["alice"]["source"] == "linked"
    assert rows["alice"]["first_seen"] and rows["alice"]["offboarding_suspended_at"] is None
    # a first login that creates the RGW user records source 'created'
    env.fake.add("dave", groups={USERS_GROUP}, display="Dave D", mail="dave@example.org")
    assert env.session("dave").get("/api/account").json()["created"] is True
    assert env.db.known_accounts.get("dave")["source"] == "created"


def test_only_known_people_are_in_scope(env, lenient):
    env.mock.state.croit.create_user("legacy", "Legacy Service")  # not in AD, never linked
    set_mode(env, "enforce")
    leave(env, "alice")
    run = go(env)
    assert run["outcome"] == "ok" and set(results(run)) == {"alice"}
    assert suspended(env, "alice") and not suspended(env, "legacy")
    assert not suspended(env, "acme$carol") and not suspended(env, "bob")
    assert run["counts"]["known"] == 2


def test_mapping_targets_are_in_scope(env, lenient):
    env.db.uid_mappings.set("erin", "acme$carol", "bob")  # erin is not in the fake AD
    set_mode(env, "enforce")
    run = go(env)
    assert results(run)["acme$carol"]["reason"] == "not_found"
    assert results(run)["acme$carol"]["adUsernames"] == ["erin"]
    assert suspended(env, "acme$carol")
    assert env.db.known_accounts.get("erin")["source"] == "mapping"


def test_mapping_to_a_missing_user_is_skipped(env, lenient):
    env.db.uid_mappings.set("erin", "ghost", "bob")
    run = go(env)
    assert run["outcome"] == "ok" and run["results"] == []


def test_bucket_key_users_are_never_people(env, lenient):
    env.mock.state.croit.create_user("alice+ci", "alice key")
    env.db.bucket_keys.create("alice+ci", "alice", "alice-data", "read")
    env.db.known_accounts.record("alicekey", "alice+ci", "linked")  # even if linked by mistake
    set_mode(env, "enforce")
    run = go(env)
    assert run["results"] == [] and run["counts"]["known"] == 2
    assert not suspended(env, "alice+ci")
    # the resolver does not record a key user either
    acct = SimpleNamespace(resolved=True, uid="alice+ci", created=False, source="existing")
    asyncio.run(env.app.state.rgw.record_link("bob.key", acct))
    assert env.db.known_accounts.get("bob.key") is None


# -- modes -----------------------------------------------------------------------------


def test_report_mode_suspends_nothing(env, lenient):
    leave(env, "alice", "group")
    env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    env.transport.calls.clear()
    run = go(env)
    assert run["mode"] == "report" and run["outcome"] == "ok"
    assert run["results"] == [{"uid": "alice", "adUsernames": ["alice"],
                               "reason": "not_in_group", "action": "would_suspend", "keys": []}]
    assert env.transport.mutations() == []
    assert not suspended(env, "alice")
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None
    assert env.db.bucket_keys.get_by_uid("acme$carol")["suspended_at"] is None
    assert [a["action"] for a in acts(env, "offboarding.")] == ["offboarding.run"]


def test_enforce_suspends_user_and_keys_by_offboarding(env, lenient):
    k = env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    direct = env.db.bucket_keys.create("bob", "alice", "alice-data", "read")
    env.db.bucket_keys.set_suspended(direct["id"], True, "direct")
    assert env.user.get("/api/account").json()["status"] == "ready"  # cached as ready
    set_mode(env, "enforce")
    leave(env, "alice")
    run = go(env, "schedule", "system")
    assert run["outcome"] == "ok" and run["counts"]["suspended"] == 1
    assert results(run)["alice"]["action"] == "suspended"
    assert results(run)["alice"]["keys"] == ["acme$carol"]
    assert suspended(env, "alice") and suspended(env, "acme$carol")
    row = env.db.bucket_keys.get(k["id"])
    assert row["suspended_at"] and row["suspended_by"] == "offboarding"
    assert env.db.bucket_keys.get(direct["id"])["suspended_by"] == "direct"  # left alone
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"]
    # the resolver cache was invalidated: the user sees the suspension at once
    assert env.user.get("/api/account").json()["status"] == "suspended"
    assert not suspended(env, "bob")
    # nothing is deleted
    assert [m for m in env.transport.mutations() if m[0] == "DELETE"] == []
    a = [x for x in acts(env, "offboarding.suspend")]
    assert len(a) == 1 and a[0]["actor"] == "system" and a[0]["target"] == "alice"
    assert a[0]["target_type"] == "rgw_user" and a[0]["details"]["keys"] == ["acme$carol"]
    (done,) = acts(env, "offboarding.run")
    assert done["actor"] == "system" and done["target_type"] == "offboarding_run"


def test_manual_run_actor_is_the_admin(env, lenient):
    set_mode(env, "enforce")
    leave(env, "alice")
    r = env.admin.post("/api/admin/offboarding/run", headers=H)
    assert r.status_code == 200 and r.json()["trigger"] == "manual" and r.json()["actor"] == "bob"
    assert {a["actor"] for a in acts(env, "offboarding.")} == {"bob"}


def test_account_that_is_fine_in_ad_is_untouched(env, lenient):
    set_mode(env, "enforce")
    run = go(env)
    assert run["outcome"] == "ok" and run["results"] == [] and run["counts"]["candidates"] == 0
    assert [m for m in env.transport.mutations() if m[0] == "PATCH"] == []


# -- safeguards ------------------------------------------------------------------------


def test_ldap_error_aborts_the_run(env, lenient):
    set_mode(env, "enforce")
    leave(env, "alice")
    env.fake.fail_search = True
    run = go(env)
    assert run["outcome"] == "aborted" and "AD could not be read" in run["reason"]
    assert not suspended(env, "alice")
    (a,) = acts(env, "offboarding.aborted")
    assert a["details"]["reason"] == run["reason"]
    # a domain controller that is down aborts as well
    env.fake.fail_search = None
    env.fake.down.add("ldaps://dc.example.org:636")
    assert go(env)["outcome"] == "aborted" and not suspended(env, "alice")


def test_percent_threshold_aborts(env):
    set_mode(env, "enforce")
    leave(env, "alice")  # 1 of 2 known people = 50% > 10%
    run = go(env)
    assert run["outcome"] == "aborted" and "1 of 2" in run["reason"] and "10%" in run["reason"]
    assert results(run)["alice"]["action"] == "not_suspended"  # shown, not acted on
    assert not suspended(env, "alice") and not env.db.known_accounts.get(
        "alice")["offboarding_suspended_at"]
    assert acts(env, "offboarding.suspend") == []


def test_exactly_ten_percent_is_allowed(env):
    for i in range(8):
        env.fake.add(f"u{i}", groups={USERS_GROUP}, display=f"U{i}", mail=f"u{i}@example.org")
        env.mock.state.croit.create_user(f"u{i}", f"U{i}")
        env.db.known_accounts.record(f"u{i}", f"u{i}", "linked")
    set_mode(env, "enforce")
    leave(env, "alice")  # 1 of 10
    run = go(env)
    assert run["outcome"] == "ok" and suspended(env, "alice")


def test_more_than_twenty_people_abort(env, lenient):
    for i in range(21):
        env.mock.state.croit.create_user(f"gone{i}", f"G{i}")
        env.db.known_accounts.record(f"gone{i}", f"gone{i}", "linked")  # not in AD
    set_mode(env, "enforce")
    run = go(env)
    assert run["outcome"] == "aborted" and "21 of 23" in run["reason"]
    assert "20 people" in run["reason"]
    assert not any(suspended(env, f"gone{i}") for i in range(21))
    with env.db.tx() as c:
        c.execute("DELETE FROM known_accounts WHERE ad_username = 'gone0'")
    set_mode(env, "report")
    run = go(env)
    assert run["outcome"] == "ok" and len(run["results"]) == 20


def test_admin_suspended_account_is_not_touched(env, lenient):
    assert env.admin.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    set_mode(env, "enforce")
    leave(env, "alice")
    env.transport.calls.clear()
    run = go(env)
    assert run["outcome"] == "ok" and run["results"] == []
    assert run["counts"]["alreadySuspended"] == 1
    assert [m for m in env.transport.mutations() if m[1].startswith("/api/s3/users")] == []
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None
    # ... and a login does not undo the admin's decision
    leave_back(env)
    acct = asyncio.run(env.app.state.rgw.resolve("alice"))
    asyncio.run(env.svc.on_login("alice", acct))
    assert suspended(env, "alice")


def leave_back(env):
    env.fake.users[DN.format("alice")]["userAccountControl"] = "512"


def test_person_who_logged_in_during_the_run_is_skipped(env, lenient, monkeypatch):
    set_mode(env, "enforce")
    leave(env, "alice")
    real = env.svc._suspend_one

    async def late_login(uid, p, res, counts, actor, run_id, started):
        env.db.known_accounts.record("alice", "alice", "linked", login=True)
        await real(uid, p, res, counts, actor, run_id, started)

    monkeypatch.setattr(env.svc, "_suspend_one", late_login)
    run = go(env)
    assert results(run)["alice"]["action"] == "skipped" and not suspended(env, "alice")


def patch_fails(env, make):
    env.transport.fail = lambda req: make() if req.method == "PATCH" else None


def test_definite_croit_refusal_releases_the_claim(env, lenient):
    secret = admin_secret()  # croit's token, or the Admin Ops secret key
    set_mode(env, "enforce")
    leave(env, "alice")
    patch_fails(env, lambda: error_response(400, f"no {secret}"))
    run = go(env)
    assert run["outcome"] == "failed" and run["counts"]["failed"] == 1
    assert results(run)["alice"]["action"] == "failed" and TOKEN not in str(run)
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None


@pytest.mark.parametrize("kind", ["5xx", "timeout"])
def test_unknown_outcome_keeps_the_claim_so_login_restores(env, lenient, kind):
    import httpx

    set_mode(env, "enforce")
    leave(env, "alice")
    # RGW will apply the suspension, but the answer is lost
    patch_fails(env, (lambda: httpx.Response(502, json={"code": 502, "message": "bad gateway"}))
                if kind == "5xx" else (lambda: httpx.ReadTimeout("slow")))
    run = go(env)
    assert run["outcome"] == "failed" and run["counts"]["failed"] == 1
    r = results(run)["alice"]
    assert r["action"] == "unknown" and "outcome unknown" in r["note"]
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"]
    env.mock.state.croit.update_user("alice", {"suspended": True})  # ... it had been applied
    env.transport.fail = None
    leave_back(env)
    acct = asyncio.run(env.app.state.rgw.resolve("alice"))
    assert acct.status == "suspended"
    asyncio.run(env.svc.on_login("alice", acct))
    assert not suspended(env, "alice")
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None


def test_key_cascade_failure_is_noted_as_uncertain(env, lenient):
    import httpx

    env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    set_mode(env, "enforce")
    leave(env, "alice")
    env.transport.fail = lambda req: (
        httpx.Response(502, json={"code": 502, "message": "x"})
        if req.method == "PATCH" and "carol" in req.url.path else None)
    run = go(env)
    r = results(run)["alice"]
    assert r["action"] == "partial" and "may or may not" in r["note"]
    assert suspended(env, "alice") and run["outcome"] == "failed"


def partial_alice(env):
    """alice offboarded, but her bucket key could not be confirmed suspended."""
    import httpx

    env.k_off = env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    set_mode(env, "enforce")
    leave(env, "alice")
    env.transport.fail = lambda req: (
        httpx.Response(502, json={"code": 502, "message": "x"})
        if req.method == "PATCH" and "carol" in req.url.path else None)
    assert results(go(env))["alice"]["action"] == "partial"
    env.transport.fail = None
    assert suspended(env, "alice") and not suspended(env, "acme$carol")


def test_restored_outside_bucketeer_is_not_judged_from_a_cached_list(env, lenient):
    partial_alice(env)
    assert env.admin.get("/api/admin/users").status_code == 200  # fills any list cache
    leave_back(env)
    env.mock.state.croit.update_user("alice", {"suspended": False})  # an admin, outside us
    run = go(env)
    assert "alice" not in results(run) or results(run)["alice"]["action"] != "keys_completed"
    assert not suspended(env, "alice") and not suspended(env, "acme$carol")
    assert env.db.bucket_keys.get(env.k_off["id"])["suspended_at"] is None


def test_complete_keys_skips_an_account_that_is_no_longer_suspended(env, lenient):
    partial_alice(env)
    env.mock.state.croit.update_user("alice", {"suspended": False})
    res, counts = {"uid": "alice"}, {"keysSuspended": 0, "failed": 0}
    asyncio.run(env.svc._complete_keys(res, {"names": ["alice"]}, counts, "bob", 1))
    assert res["action"] == "skipped" and "no longer suspended" in res["note"]
    assert counts["keysSuspended"] == 0 and not suspended(env, "acme$carol")
    assert env.db.bucket_keys.get(env.k_off["id"])["suspended_at"] is None


# -- coming back -----------------------------------------------------------------------


def offboard_alice(env):
    env.k_off = env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    env.mock.state.croit.create_user("alice-k2", "k2")
    env.k_direct = env.db.bucket_keys.create("alice-k2", "alice", "alice-data", "read")
    env.mock.state.croit.update_user("alice-k2", {"suspended": True})
    env.db.bucket_keys.set_suspended(env.k_direct["id"], True, "direct")
    set_mode(env, "enforce")
    leave(env, "alice")
    assert go(env)["outcome"] == "ok" and suspended(env, "alice")


def test_login_unsuspends_only_what_the_job_suspended(env, lenient):
    offboard_alice(env)
    leave_back(env)
    acct = asyncio.run(env.app.state.rgw.resolve("alice"))
    assert acct.status == "suspended"
    asyncio.run(env.svc.on_login("alice", acct))
    assert not suspended(env, "alice") and not suspended(env, "acme$carol")
    assert env.db.bucket_keys.get(env.k_off["id"])["suspended_at"] is None
    assert env.db.bucket_keys.get(env.k_direct["id"])["suspended_by"] == "direct"
    assert suspended(env, "alice-k2")
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None
    assert env.user.get("/api/account").json()["status"] == "ready"  # cache invalidated
    (a,) = acts(env, "offboarding.unsuspend")
    assert a["actor"] == "system" and a["target"] == "alice" and a["details"]["keys"] == [
        "acme$carol"]
    # a second login changes nothing
    asyncio.run(env.svc.on_login("alice", acct))
    assert len(acts(env, "offboarding.unsuspend")) == 1


def test_warm_up_after_login_unsuspends(env, lenient, monkeypatch):
    offboard_alice(env)
    monkeypatch.setattr(RgwResolver, "warm_on_login", True)
    rgw = env.app.state.rgw

    async def login():
        rgw.warm(SimpleNamespace(username="alice", display_name="Alice A"))
        await asyncio.gather(*rgw._tasks)

    asyncio.run(login())
    assert not suspended(env, "alice")
    assert env.db.known_accounts.get("alice")["last_login"]


def test_admin_suspend_takes_over_from_the_job(env, lenient):
    offboard_alice(env)
    r = env.admin.post("/api/admin/users/alice/suspend", headers=H)
    assert r.status_code == 200
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None
    assert env.db.bucket_keys.get(env.k_off["id"])["suspended_by"] == "owner"
    leave_back(env)
    acct = asyncio.run(env.app.state.rgw.resolve("alice"))
    asyncio.run(env.svc.on_login("alice", acct))  # the login does nothing
    assert suspended(env, "alice") and suspended(env, "acme$carol")
    assert acts(env, "offboarding.unsuspend") == []
    # the admin's own unsuspend wakes the keys that were suspended with the account
    assert env.admin.post("/api/admin/users/alice/unsuspend", headers=H).status_code == 200
    assert not suspended(env, "acme$carol") and suspended(env, "alice-k2")


def test_manual_unsuspend_clears_the_flag_and_wakes_keys(env, lenient):
    offboard_alice(env)
    r = env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert r.status_code == 200 and r.json()["keys"] == ["acme$carol"]
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None
    assert env.db.bucket_keys.get(env.k_off["id"])["suspended_at"] is None
    assert env.db.bucket_keys.get(env.k_direct["id"])["suspended_at"]


def test_relinking_to_another_uid_drops_the_flag(env):
    env.db.known_accounts.record("zed", "zed", "linked")
    env.db.known_accounts.set_offboarding("zed", True)
    env.db.known_accounts.record("zed", "zed", "linked", login=True)
    assert env.db.known_accounts.get("zed")["offboarding_suspended_at"]
    env.db.known_accounts.record("zed", "zed2", "mapping")
    row = env.db.known_accounts.get("zed")
    assert row["offboarding_suspended_at"] is None and row["source"] == "mapping"


# -- one run at a time, history --------------------------------------------------------


def test_one_run_at_a_time(env):
    async def two():
        first = await env.svc.start("manual", "bob")
        with pytest.raises(AlreadyRunning):
            await env.svc.start("manual", "bob")
        await env.svc._last
        return first

    first = asyncio.run(two())
    assert env.db.offboarding_runs.get(first)["outcome"] in ("ok", "aborted")
    assert env.svc.running is False
    env.svc.running = True
    r = env.admin.post("/api/admin/offboarding/run", headers=H)
    assert r.status_code == 409
    assert env.admin.get("/api/admin/offboarding").json()["running"] is True


def test_only_the_last_30_runs_are_kept(env):
    for _ in range(32):
        env.db.offboarding_runs.start("report", "manual", "bob")
    runs = env.db.offboarding_runs.list(100)
    assert len(runs) == 30 and runs[0]["id"] == 32
    assert env.db.offboarding_runs.fail_stale() == 30
    assert all(r["outcome"] == "failed" for r in env.db.offboarding_runs.list(100))


# -- schedule --------------------------------------------------------------------------


def test_next_run_computation():
    d = datetime
    assert next_run(d(2026, 10, 14, 2, 0), "03:00") == d(2026, 10, 14, 3, 0)
    assert next_run(d(2026, 10, 14, 3, 0), "03:00") == d(2026, 10, 15, 3, 0)  # strictly after
    assert next_run(d(2026, 10, 14, 3, 0, 1), "03:00") == d(2026, 10, 15, 3, 0)
    assert next_run(d(2026, 10, 14, 23, 59, 30), "00:00") == d(2026, 10, 15, 0, 0)
    assert next_run(d(2026, 10, 31, 12, 0), "03:30") == d(2026, 11, 1, 3, 30)
    assert next_run(d(2026, 12, 31, 12, 0), "03:30") == d(2027, 1, 1, 3, 30)
    with pytest.raises(ValueError):
        next_run(d(2026, 1, 1), "3:00")


def test_scheduler_runs_daily_at_the_configured_time(env):
    clock = [datetime(2026, 10, 14, 2, 0)]
    fired = []

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def fake_run(trigger, actor):
        fired.append((clock[0], trigger, actor))

    env.svc.now, env.svc.sleep, env.svc.run = lambda: clock[0], sleep, fake_run

    async def main():
        task = asyncio.get_running_loop().create_task(env.svc.schedule_loop())
        while len(fired) < 2:
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert fired[0] == (datetime(2026, 10, 14, 3, 0), "schedule", "system")
    assert fired[1] == (datetime(2026, 10, 15, 3, 0), "schedule", "system")


def test_scheduler_follows_a_changed_time(env):
    clock = [datetime(2026, 10, 14, 2, 0)]
    fired = []

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def fake_run(trigger, actor):
        fired.append(clock[0])

    env.svc.now, env.svc.sleep, env.svc.run = lambda: clock[0], sleep, fake_run
    env.db.settings.set("offboarding", {"mode": "report", "time": "02:30"})

    async def main():
        task = asyncio.get_running_loop().create_task(env.svc.schedule_loop())
        while not fired:
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert fired == [datetime(2026, 10, 14, 2, 30)]


def test_lifespan_starts_and_stops_the_scheduler(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from bucketeer.db import Database
    from bucketeer.main import create_app

    monkeypatch.setattr(offboarding, "SCHEDULE_ON_START", True)
    db = Database(":memory:")
    app = create_app(make_settings(tmp_path), db=db,
                     authenticator=AdAuthenticator(make_settings(tmp_path), FakeLdap()))
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}  # startup never waits for the job
    db.close()


# -- settings and API ------------------------------------------------------------------


def test_overview(env, lenient):
    leave(env, "alice")
    go(env)
    body = env.admin.get("/api/admin/offboarding").json()
    assert body["settings"] == {"mode": "report", "time": "03:00"}
    assert body["running"] is False and body["knownPeople"] == 2
    assert body["thresholds"] == {"percent": offboarding.MAX_PERCENT, "max": 20}
    assert datetime.fromisoformat(body["nextRun"]).minute == 0
    (run,) = body["runs"]
    assert run["outcome"] == "ok" and run["trigger"] == "manual" and run["mode"] == "report"
    assert run["results"][0]["uid"] == "alice" and run["finishedAt"]


def test_admin_only_and_csrf(env):
    for method, path in (("get", "/api/admin/offboarding"), ("post", "/api/admin/offboarding/run"),
                         ("put", "/api/admin/offboarding/settings")):
        assert getattr(env.anon, method)(path, headers=H).status_code == 401
        assert getattr(env.user, method)(path, headers=H).status_code == 403
    assert env.admin.post("/api/admin/offboarding/run").status_code == 403  # no CSRF header
    assert env.admin.put("/api/admin/offboarding/settings", json={"mode": "enforce"}
                         ).status_code == 403


@pytest.mark.parametrize("body", [
    {}, {"mode": "delete"}, {"mode": "ENFORCE"}, {"time": "24:00"}, {"time": "3:00"},
    {"time": "03:60"}, {"time": "03:00\n"}, {"time": ""}, {"time": 300}, {"mode": "report", "x": 1},
])
def test_settings_validation(env, body):
    r = env.admin.put("/api/admin/offboarding/settings", json=body, headers=H)
    assert r.status_code == 422
    assert env.db.settings.get("offboarding") is None and acts(env, "admin.settings") == []


def test_settings_save_and_audit(env):
    r = env.admin.put("/api/admin/offboarding/settings", json={"mode": "enforce"}, headers=H)
    assert r.json() == {"mode": "enforce", "time": "03:00"}
    r = env.admin.put("/api/admin/offboarding/settings", json={"time": "04:15"}, headers=H)
    assert r.json() == {"mode": "enforce", "time": "04:15"}
    env.admin.put("/api/admin/offboarding/settings", json={"time": "04:15"}, headers=H)  # same
    rows = sorted(acts(env, "admin.settings.offboarding"), key=lambda a: a["id"])
    assert len(rows) == 2 and {a["actor"] for a in rows} == {"bob"}
    assert rows[0]["target_type"] == "setting" and rows[0]["target"] == "offboarding"
    assert rows[1]["details"] == {"before": {"mode": "enforce", "time": "03:00"},
                                  "after": {"mode": "enforce", "time": "04:15"}}
    assert env.svc.settings() == {"mode": "enforce", "time": "04:15"}


def test_garbage_in_the_settings_table_falls_back_to_the_defaults(env):
    env.db.settings.set("offboarding", {"mode": "nuke", "time": "9pm"})
    assert env.svc.settings() == {"mode": "report", "time": "03:00"}


# -- the AD side -----------------------------------------------------------------------


@pytest.fixture
def ad(tmp_path):
    fake = FakeLdap()
    return fake, AdAuthenticator(make_settings(tmp_path), fake)


def test_account_statuses(ad):
    fake, auth = ad
    fake.users[DN.format("bob")]["userAccountControl"] = "514"
    fake.users[DN.format("alice2")]["userAccountControl"] = "66050"  # disabled + other flags
    fake.users[DN.format("bob")]["groups"] = {USERS_GROUP}
    out = asyncio.run(auth.account_statuses(
        ["alice", "bob", "carol", "alice2", "nobody", "alice@example.org"]))
    assert out == {"alice": "ok", "bob": "disabled", "carol": "not_in_group",
                   "alice2": "disabled", "nobody": "not_found",
                   "alice@example.org": "not_found"}  # exact username attribute, not mail/UPN
    assert asyncio.run(auth.account_status("ALICE")) == "ok"  # AD names are case-insensitive


def test_account_statuses_use_the_service_bind_only_and_escape(ad):
    fake, auth = ad
    out = asyncio.run(auth.account_statuses(["*)(|(objectClass=*", "alice"]))
    assert out["*)(|(objectClass=*"] == "not_found" and out["alice"] == "ok"
    assert {e[2] for e in fake.binds()} == {"CN=svc,OU=Service,DC=example,DC=org"}
    flts = [e[2] for e in fake.events if e[0] == "search" and "objectClass" in e[2]]
    assert flts == [r"(sAMAccountName=\2a\29\28|\28objectClass=\2a)"]


def test_account_statuses_raise_on_any_trouble(ad):
    fake, auth = ad
    fake.fail_search = True
    with pytest.raises(LdapUnavailable):
        asyncio.run(auth.account_statuses(["alice"]))
    fake.fail_search = None
    fake.down.add("ldaps://dc.example.org:636")
    with pytest.raises(LdapUnavailable):
        asyncio.run(auth.account_statuses(["alice"]))
    fake.down.clear()
    del fake.users[DN.format("alice2")]
    fake.add("twin", groups=set(), display="T", mail="t@example.org")
    fake.users[DN.format("twin")]["sAMAccountName"] = "alice"  # two entries for one name
    with pytest.raises(LdapUnavailable):
        asyncio.run(auth.account_statuses(["alice"]))
    fake.users.pop(DN.format("twin"))
    fake.users[DN.format("alice")]["userAccountControl"] = "garbage"
    with pytest.raises(LdapUnavailable):
        asyncio.run(auth.account_statuses(["alice"]))


def test_demo_directory_has_the_same_interface():
    from mock.demo import DemoDirectory

    d = DemoDirectory()
    d.disabled.add("bob")
    d.left_group.add("erin")
    out = asyncio.run(d.account_statuses(["alice", "bob", "erin", "zed"]))
    assert out == {"alice": "ok", "bob": "disabled", "erin": "not_in_group", "zed": "not_found"}


def test_session_password_is_not_needed(env):
    # the job works with no user session or password at all
    assert SECRET_PW not in str(go(env))


# -- exact uid matching ------------------------------------------------------------------


def test_case_variants_in_croit_are_never_confused(env, lenient):
    env.mock.state.croit.create_user("Alice", "Legacy Alice")  # another RGW user
    env.db.known_accounts.record("alice", "alice", "linked")
    set_mode(env, "enforce")
    leave(env, "alice")
    run = go(env)
    assert set(results(run)) == {"alice"} and suspended(env, "alice")
    assert not suspended(env, "Alice")
    assert env.db.known_accounts.for_uid("alice")[0]["offboarding_suspended_at"]
    assert env.db.known_accounts.for_uid("Alice") == []
    # an admin touching `Alice` does not clear alice's claim; the login still restores alice
    assert env.admin.post("/api/admin/users/Alice/suspend", headers=H).status_code == 200
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"]
    leave_back(env)
    acct = asyncio.run(env.app.state.rgw.resolve("alice"))
    asyncio.run(env.svc.on_login("alice", acct))
    assert not suspended(env, "alice") and suspended(env, "Alice")


def test_known_uid_that_only_matches_in_another_case_is_skipped(env, lenient):
    env.mock.state.croit.create_user("Zed", "Zed")
    env.db.known_accounts.record("zed", "zed", "linked")  # croit has only `Zed`
    set_mode(env, "enforce")
    run = go(env)
    (r,) = [x for x in run["results"] if x["uid"] == "zed"]
    assert r["action"] == "skipped" and "different case" in r["note"]
    assert not suspended(env, "Zed") and run["counts"]["skippedCaseMismatch"] == 1


def test_mapping_target_in_another_case_counts_as_missing(env, lenient):
    env.mock.state.croit.create_user("lab", "Lab")
    env.db.uid_mappings.set("erin", "Lab", "bob")  # the resolver's exact get_user: missing
    set_mode(env, "enforce")
    run = go(env)
    assert not suspended(env, "lab") and run["counts"]["known"] == 2
    assert env.db.known_accounts.get("erin") is None


# -- mapping changes ---------------------------------------------------------------------


def test_deleted_mapping_leaves_the_scope(env, lenient):
    env.mock.state.croit.create_user("lab-shared", "Lab")
    assert env.admin.put("/api/admin/settings/uid-mappings/jdoe", json={"rgwUid": "lab-shared"},
                         headers=H).status_code == 200
    set_mode(env, "enforce")
    run = go(env)  # jdoe is not in AD: the mapping target is a candidate
    assert results(run)["lab-shared"]["reason"] == "not_found" and suspended(env, "lab-shared")
    env.mock.state.croit.update_user("lab-shared", {"suspended": False})
    assert env.admin.delete("/api/admin/settings/uid-mappings/jdoe", headers=H).status_code == 200
    assert env.db.known_accounts.get("jdoe") is None
    assert "lab-shared" not in results(go(env)) and not suspended(env, "lab-shared")


def test_stale_mapping_row_is_ignored_even_without_the_route(env, lenient):
    env.mock.state.croit.create_user("lab-shared", "Lab")
    env.db.known_accounts.record("jdoe", "lab-shared", "mapping")  # mapping long gone
    run = go(env)
    assert "lab-shared" not in results(run) and env.db.known_accounts.get("jdoe") is None


def test_repointed_mapping_moves_the_scope(env, lenient):
    env.mock.state.croit.create_user("lab-a", "A")
    env.mock.state.croit.create_user("lab-b", "B")
    for uid in ("lab-a", "lab-b"):
        assert env.admin.put("/api/admin/settings/uid-mappings/jdoe", json={"rgwUid": uid},
                             headers=H).status_code == 200
    assert env.db.known_accounts.get("jdoe") is None  # re-recorded by the next run
    set_mode(env, "enforce")
    run = go(env)
    assert set(results(run)) == {"lab-b"} and not suspended(env, "lab-a")


# -- one lock for every suspend/unsuspend -------------------------------------------------


def admin_user():
    from bucketeer.auth import SessionUser

    return SessionUser("bob", "Bob B", "bob@example.org", True, 0)


async def settle(*ticks):
    for _ in range(20):
        await asyncio.sleep(0)


def test_admin_suspend_waits_for_a_login_unsuspend(env, lenient):
    from bucketeer.admin import _set_suspended

    offboard_alice(env)
    leave_back(env)
    croit = env.app.state.croit

    async def main():
        acct = await env.app.state.rgw.resolve("alice")
        entered, release = asyncio.Event(), asyncio.Event()
        real = croit.get_user

        async def slow_get_user(uid):
            out = await real(uid)
            entered.set()
            await release.wait()  # on_login has seen flag and suspension, holds the lock
            return out

        croit.get_user = slow_get_user
        t1 = asyncio.create_task(env.svc.on_login("alice", acct))
        await entered.wait()
        croit.get_user = real
        t2 = asyncio.create_task(_set_suspended(
            "alice", True, admin_user(), croit, env.db, env.app.state.rgw, env.svc.lock))
        await settle()
        assert not t2.done()  # the admin waits instead of racing
        release.set()
        await asyncio.gather(t1, t2)

    asyncio.run(main())
    # the admin's decision came last and stands
    assert suspended(env, "alice") and not env.db.known_accounts.get(
        "alice")["offboarding_suspended_at"]
    assert acts(env, "admin.user.suspend")


def test_login_waits_for_a_job_suspension_in_progress(env, lenient):
    set_mode(env, "enforce")
    leave(env, "alice")
    croit = env.app.state.croit

    async def main():
        people, _missing, _skipped = await env.svc._scope()
        acct = await env.app.state.rgw.resolve("alice")
        entered, release = asyncio.Event(), asyncio.Event()
        real = croit.get_user

        async def slow_get_user(uid):
            out = await real(uid)
            entered.set()
            await release.wait()  # the job has read last_login and holds the lock
            return out

        croit.get_user = slow_get_user
        res = {"uid": "alice", "adUsernames": ["alice"], "reason": "disabled", "keys": [],
               "action": "would_suspend"}
        counts = {"suspended": 0, "keysSuspended": 0, "failed": 0, "alreadySuspended": 0}
        t1 = asyncio.create_task(env.svc._suspend_one(
            "alice", people["alice"], res, counts, "system", 1, "9999"))
        await entered.wait()
        croit.get_user = real
        env.db.known_accounts.record("alice", "alice", "linked", login=True)  # login just now
        t2 = asyncio.create_task(env.svc.on_login("alice", acct))
        await settle()
        assert not t2.done()  # no unlocked fast path that sees "no flag" and gives up
        release.set()
        await asyncio.gather(t1, t2)
        return res

    res = asyncio.run(main())
    assert res["action"] == "suspended"
    assert not suspended(env, "alice")  # the login restored the person afterwards
    assert env.db.known_accounts.get("alice")["offboarding_suspended_at"] is None


def test_admin_unsuspend_wakes_job_keys_even_if_croit_says_awake(env, lenient):
    offboard_alice(env)
    env.mock.state.croit.update_user("alice", {"suspended": False})  # croit already awake
    r = env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert r.status_code == 200 and r.json()["keys"] == ["acme$carol"]
    assert env.db.bucket_keys.get(env.k_off["id"])["suspended_at"] is None


# -- changing the time ---------------------------------------------------------------------


def test_changing_the_time_does_not_start_a_run_at_once(env):
    clock = [datetime(2026, 10, 14, 2, 0)]
    fired, changed = [], []

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        if clock[0] == datetime(2026, 10, 14, 10, 0) and not changed:
            changed.append(1)  # the admin moves the run to 09:00, which is already past
            env.db.settings.set("offboarding", {"mode": "report", "time": "09:00"})
            env.svc.note_settings_changed()
            assert env.svc.next_run() == datetime(2026, 10, 15, 9, 0)  # what GET shows
        await asyncio.sleep(0)

    async def fake_run(trigger, actor):
        fired.append(clock[0])

    env.svc.now, env.svc.sleep, env.svc.run = lambda: clock[0], sleep, fake_run

    async def main():
        task = asyncio.get_running_loop().create_task(env.svc.schedule_loop())
        while len(fired) < 2:
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert fired == [datetime(2026, 10, 14, 3, 0), datetime(2026, 10, 15, 9, 0)]


def test_unknown_suspend_leaves_keys_live_until_the_next_enforce_run(env, lenient):
    import httpx

    k = env.db.bucket_keys.create("acme$carol", "alice", "alice-data", "read")
    set_mode(env, "enforce")
    leave(env, "alice")
    patch_fails(env, lambda: httpx.ReadTimeout("slow"))
    run = go(env)
    assert results(run)["alice"]["action"] == "unknown"
    assert "bucket keys may still need suspending" in results(run)["alice"]["note"]
    env.transport.fail = None
    env.mock.state.croit.update_user("alice", {"suspended": True})  # croit had applied it
    assert env.db.bucket_keys.get(k["id"])["suspended_at"] is None and not suspended(
        env, "acme$carol")
    # report mode only lists the pending keys
    set_mode(env, "report")
    run = go(env)
    assert results(run)["alice"]["action"] == "keys_pending" and run["counts"]["keysPending"] == 1
    assert env.db.bucket_keys.get(k["id"])["suspended_at"] is None
    # the next enforce run finishes them (not a candidate: already suspended)
    set_mode(env, "enforce")
    run = go(env, "schedule", "system")
    r = results(run)["alice"]
    assert r["action"] == "keys_completed" and r["keys"] == ["acme$carol"]
    assert run["outcome"] == "ok" and run["counts"]["keysSuspended"] == 1
    row = env.db.bucket_keys.get(k["id"])
    assert row["suspended_by"] == "offboarding" and suspended(env, "acme$carol")
    (a,) = [x for x in acts(env, "offboarding.suspend") if x["details"].get("keys_only")]
    assert a["actor"] == "system" and a["target"] == "alice" and a["details"]["keys"] == [
        "acme$carol"]
    # a third run has nothing left to do
    assert "alice" not in results(go(env))
    # the person's next login restores the user and those keys
    leave_back(env)
    acct = asyncio.run(env.app.state.rgw.resolve("alice"))
    asyncio.run(env.svc.on_login("alice", acct))
    assert not suspended(env, "alice") and not suspended(env, "acme$carol")
    assert env.db.bucket_keys.get(k["id"])["suspended_at"] is None
