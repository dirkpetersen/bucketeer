"""User notices (`bucketeer/notices.py`): near-full on-premises buckets, key ages, the daily
schedule and the admin settings. Runs on both admin backends against the mock croit / Admin
Ops and moto; the mailer is a recording fake (a real `Mailer` only for the message texts)."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from bucketeer import notices
from bucketeer.mailer import Mailer

from .backends import admin_backend  # noqa: F401  (fixture: runs each test on both)
from .test_auth import make_settings
from .test_buckets import Env, H

S = "/api/admin/settings/notices"
DAY = timedelta(days=1)


class FakeMailer:
    enabled = True

    def __init__(self, fail=False):
        self.fail, self.near, self.keys = fail, [], []

    async def send_near_full(self, to, bucket, size, quota, percent):
        self.near.append((to, bucket, size, quota, percent))
        return not self.fail

    async def send_key_age(self, to, keys, limit_days, cloud):
        self.keys.append((to, keys, limit_days, cloud))
        return not self.fail


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def tick(env, now):
    return asyncio.run(notices.notice_tick(env.app, now))


def acts(env, action):
    return [a for a in env.db.audit.query(limit=1000) if a["action"] == action]


def fill(env, bucket="alice-data", owner="alice", ratio=1.0, data=b"x" * 1000):
    """Give `bucket` some data and a size quota so that size / quota == `ratio`."""
    if data:
        env.bridge.owner_client(owner).put_object(Bucket=bucket, Key="fill.bin", Body=data)
    b = env.croit_state.buckets[bucket]
    env.croit_state.refresh_usage(b)
    b.quota.update(enabled=True, maxSize=int(b.size_bytes / ratio), maxObjects=-1)
    env.app.state.croit.invalidate_lists()
    return b.size_bytes, b.quota["maxSize"]


# -- admin settings ---------------------------------------------------------------------


def test_settings_defaults_validation_and_audit(env):
    r = env.bob.get(S)
    assert r.status_code == 200
    d = r.json()
    assert (d["nearFullPercent"], d["maxKeyAgeDays"]) == (90, 180)
    assert d["defaults"] == {"nearFullPercent": 90, "maxKeyAgeDays": 180}
    assert d["limits"] == {"nearFullPercent": [50, 100], "maxKeyAgeDays": [0, 3650]}
    assert d["email"] == {"enabled": False} and d["schedule"]["time"] == "07:00"
    assert d["schedule"]["lastRun"] is None
    assert env.alice.get(S).status_code == 403
    assert env.alice.put(S, json={"nearFullPercent": 80}, headers=H).status_code == 403
    assert env.bob.put(S, json={"nearFullPercent": 80}).status_code == 403  # CSRF header
    for bad, field in (({"nearFullPercent": 49}, "nearFullPercent"),
                       ({"nearFullPercent": 101}, "nearFullPercent"),
                       ({"nearFullPercent": "90"}, "nearFullPercent"),
                       ({"nearFullPercent": True}, "nearFullPercent"),
                       ({"nearFullPercent": 9.5}, "nearFullPercent"),
                       ({"nearFullPercent": None}, "nearFullPercent"),
                       ({"maxKeyAgeDays": -1}, "maxKeyAgeDays"),
                       ({"maxKeyAgeDays": 3651}, "maxKeyAgeDays")):
        r = env.bob.put(S, json=bad, headers=H)
        assert r.status_code == 422, bad
        assert r.json()["errors"][0]["field"] == field and r.json()["detail"]
    assert env.bob.put(S, json={}, headers=H).status_code == 422
    assert env.bob.put(S, json={"other": 1}, headers=H).status_code == 422
    assert not acts(env, "admin.settings.notices")

    r = env.bob.put(S, json={"nearFullPercent": 80}, headers=H)
    assert r.status_code == 200
    assert (r.json()["nearFullPercent"], r.json()["maxKeyAgeDays"]) == (80, 180)
    assert env.db.settings.get("notices") == {"nearFullPercent": 80, "maxKeyAgeDays": 180}
    [a] = acts(env, "admin.settings.notices")
    assert (a["actor"], a["target_type"], a["target"]) == ("bob", "setting", "notices")
    assert a["details"] == {"before": {"nearFullPercent": 90, "maxKeyAgeDays": 180},
                            "after": {"nearFullPercent": 80, "maxKeyAgeDays": 180}}
    assert env.bob.put(S, json={"nearFullPercent": 80}, headers=H).status_code == 200
    assert len(acts(env, "admin.settings.notices")) == 1  # unchanged: not audited
    assert env.bob.put(S, json={"maxKeyAgeDays": 0}, headers=H).json()["maxKeyAgeDays"] == 0


def test_bad_stored_values_fall_back_to_defaults(env):
    env.db.settings.set("notices", {"nearFullPercent": 5, "maxKeyAgeDays": "x"})
    assert notices.load_settings(env.db.settings) == notices.DEFAULTS
    env.db.settings.set("notices", ["not", "a", "dict"])
    assert notices.load_settings(env.db.settings) == notices.DEFAULTS


# -- near-full buckets ------------------------------------------------------------------


def test_bucket_list_shows_near_full(env):
    fill(env, ratio=0.95)
    by = {b["name"]: b for b in env.alice.get("/api/buckets").json()["buckets"]}
    assert by["alice-data"]["nearFull"] is True and by["alice-empty"]["nearFull"] is False
    env.bob.put(S, json={"nearFullPercent": 96}, headers=H)
    by = {b["name"]: b for b in env.alice.get("/api/buckets").json()["buckets"]}
    assert by["alice-data"]["nearFull"] is False


def test_near_full_rules():
    q = {"enabled": True, "maxSize": 1000, "maxObjects": -1}
    b = {"bucket_quota": q, "usageSum": {"size": 900}}
    assert notices.near_full(b, 90) and not notices.near_full(b, 91)
    assert not notices.near_full({**b, "bucket_quota": {**q, "enabled": False}}, 50)
    assert not notices.near_full({**b, "bucket_quota": {**q, "maxSize": -1}}, 50)
    assert not notices.near_full({"usageSum": {"size": 10**15}}, 50)  # no quota at all
    assert notices.near_full({**b, "usageSum": {"size": 1200}}, 100)  # over the quota


def test_near_full_email_once_a_week_without_a_row(env):
    mail = env.app.state.mailer = FakeMailer()
    size, quota = fill(env, ratio=0.92)
    now = datetime.now(UTC)
    res = tick(env, now)
    assert res["nearFull"] == 1 and res["bucketNotices"] == 1 and res["errors"] == []
    [(to, bucket, s, q, pct)] = mail.near
    assert (to, bucket, s, q, pct) == ("alice@example.org", "alice-data", size, quota, 92)
    # alice-data has no SQLite row (made outside Bucketeer): the time is kept in settings
    assert env.db.buckets.get("alice-data") is None
    assert "alice-data" in env.db.settings.get("notices_state")["nearFull"]
    tick(env, now + DAY)
    tick(env, now + 6 * DAY)
    assert len(mail.near) == 1  # at most once per 7 days
    tick(env, now + 8 * DAY)
    assert len(mail.near) == 2
    [a, _] = acts(env, "bucket.near_full_notice")
    assert (a["actor"], a["target_type"], a["target"]) == ("system", "bucket", "alice-data")
    assert a["details"] == {"uid": "alice", "bytes": size, "quotaBytes": quota, "percent": 92,
                            "threshold": 90, "emailed": True, "reason": None}


def test_near_full_uses_the_bucket_row_and_retries_a_failed_send(env):
    env.db.buckets.create("alice-data", "alice", "WT-1")
    mail = env.app.state.mailer = FakeMailer(fail=True)
    fill(env, ratio=1.0)
    now = datetime.now(UTC)
    assert tick(env, now)["bucketNotices"] == 0
    assert env.db.buckets.get("alice-data")["over_quota_alert_at"] is None
    assert acts(env, "bucket.near_full_notice")[0]["details"]["reason"] == "send_failed"
    mail.fail = False
    tick(env, now + DAY)  # retried the next day
    assert len(mail.near) == 2
    assert env.db.buckets.get("alice-data")["over_quota_alert_at"].startswith(
        (now + DAY).strftime("%Y-%m-%d"))
    assert "alice-data" not in (env.db.settings.get("notices_state").get("nearFull") or {})
    tick(env, now + 2 * DAY)
    assert len(mail.near) == 2


def test_near_full_with_email_off_is_audited_weekly(env):
    fill(env, ratio=1.0)
    now = datetime.now(UTC)
    tick(env, now)
    tick(env, now + DAY)
    [a] = acts(env, "bucket.near_full_notice")
    assert a["details"]["emailed"] is None and a["details"]["reason"] == "email_off"


def test_near_full_skips_key_users_suspended_owners_and_roomy_buckets(env):
    mail = env.app.state.mailer = FakeMailer()
    fill(env, ratio=0.5)  # alice-data: half full
    fill(env, "bob-shared", "bob", ratio=1.0)
    # a bucket key row naming "bob" makes bob a key user, never a person
    env.db.bucket_keys.create("bob", "alice", "alice-data", "read", "x", "active")
    now = datetime.now(UTC)
    res = tick(env, now)
    assert res["nearFull"] == 0 and res["skipped"] == 1 and mail.near == []
    fill(env, ratio=1.0, data=b"")
    env.croit_state.users["alice"].suspended = True
    env.app.state.croit.invalidate_lists()
    res = tick(env, now)
    assert res["skipped"] == 2 and mail.near == [] and not acts(env, "bucket.near_full_notice")


def test_near_full_admin_trouble_is_an_error_for_a_retry(env):
    fill(env, ratio=1.0)

    async def down():
        from bucketeer.croit import CroitUnavailable
        raise CroitUnavailable("down")

    env.app.state.croit.list_buckets = down
    res = tick(env, datetime.now(UTC))
    assert res["errors"] == ["buckets"]
    assert env.db.settings.get("notices_state")["lastRun"]["errors"] == ["buckets"]


# -- key ages: lists and bookkeeping ----------------------------------------------------


def age_row(env, access_key):
    return next(r for r in env.db.key_ages.list() if r["access_key"] == access_key)


def make_old(env, access_key, days):
    old = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with env.db.tx() as c:
        c.execute("UPDATE key_ages SET first_seen = ? WHERE access_key = ?", (old, access_key))


def test_credentials_list_ages(env):
    d = env.alice.get("/api/credentials").json()
    assert d["maxKeyAgeDays"] == 180
    [k] = d["keys"]
    assert k["ageDays"] == 0 and k["ageExact"] is False and k["rotateSoon"] is False
    assert k["ageKnownSince"].endswith("Z")
    first = age_row(env, k["accessKey"])
    assert (first["uid"], first["kind"], first["created"]) == ("alice", "user", 0)
    env.alice.get("/api/credentials")  # a second look keeps the first sighting
    assert age_row(env, k["accessKey"])["first_seen"] == first["first_seen"]

    new = env.alice.post("/api/credentials/keys", headers=H).json()["accessKey"]
    assert age_row(env, new)["created"] == 1
    by = {x["accessKey"]: x for x in env.alice.get("/api/credentials").json()["keys"]}
    assert by[new]["ageExact"] is True and by[new]["ageDays"] == 0

    make_old(env, k["accessKey"], 200)
    by = {x["accessKey"]: x for x in env.alice.get("/api/credentials").json()["keys"]}
    assert by[k["accessKey"]]["ageDays"] == 200 and by[k["accessKey"]]["rotateSoon"] is True
    env.bob.put(S, json={"maxKeyAgeDays": 0}, headers=H)
    d = env.alice.get("/api/credentials").json()
    assert d["maxKeyAgeDays"] is None and not any(x["rotateSoon"] for x in d["keys"])

    rot = env.alice.post(f"/api/credentials/keys/{k['accessKey']}/rotate", headers=H).json()
    assert rot["oldKeyRemoved"] is True
    keys = {r["access_key"] for r in env.db.key_ages.list()}
    assert k["accessKey"] not in keys and rot["accessKey"] in keys
    assert age_row(env, rot["accessKey"])["created"] == 1
    assert env.alice.delete(f"/api/credentials/keys/{new}", headers=H).status_code == 200
    assert new not in {r["access_key"] for r in env.db.key_ages.list()}


def test_subuser_key_ages(env):
    r = env.alice.post("/api/subusers", json={"name": "ci", "permission": "read"}, headers=H)
    assert r.status_code == 201
    first = r.json()["keys"][0]["accessKey"]
    row = age_row(env, first)
    assert (row["uid"], row["kind"], row["created"]) == ("alice", "subuser", 1)
    second = env.alice.post("/api/subusers/ci/keys", headers=H).json()["accessKey"]
    make_old(env, first, 365)
    d = env.alice.get("/api/subusers").json()
    assert d["maxKeyAgeDays"] == 180
    by = {k["accessKey"]: k for k in d["subusers"][0]["keys"]}
    assert by[first]["rotateSoon"] is True and by[first]["ageDays"] == 365
    assert by[second]["rotateSoon"] is False and by[second]["ageExact"] is True
    # the user's own key list never shows subuser keys
    assert first not in {k["accessKey"] for k in env.alice.get("/api/credentials").json()["keys"]}
    rot = env.alice.post(f"/api/subusers/ci/keys/{first}/rotate", headers=H).json()
    assert first not in {r["access_key"] for r in env.db.key_ages.list()}
    assert env.alice.delete("/api/subusers/ci", headers=H).status_code == 200
    left = {r["access_key"] for r in env.db.key_ages.list()}
    assert second not in left and rot["accessKey"] not in left


class StubCloud:
    available = True

    def __init__(self, keys):
        self.keys = keys

    async def list_access_keys(self, name):
        return [dict(k) for k in self.keys]


def test_cloud_key_list_ages(env):
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    env.app.state.cloud = StubCloud([
        {"accessKey": "AKIAOLD0000000000001", "status": "Active", "createdAt": old},
        {"accessKey": "AKIAOFF0000000000002", "status": "Inactive", "createdAt": old},
        {"accessKey": "AKIANEW0000000000003", "status": "Active",
         "createdAt": datetime.now(UTC).isoformat()}])
    env.db.cloud_accounts.record("alice", "s3-alice", "created")
    d = env.alice.get("/api/cloud/credentials").json()
    assert d["maxKeyAgeDays"] == 180
    by = {k["accessKey"]: k for k in d["keys"]}
    assert by["AKIAOLD0000000000001"]["rotateSoon"] is True
    assert by["AKIAOLD0000000000001"]["ageDays"] == 400
    assert by["AKIAOLD0000000000001"]["ageExact"] is True
    assert by["AKIAOFF0000000000002"]["rotateSoon"] is False  # inactive
    assert by["AKIANEW0000000000003"]["ageDays"] == 0


# -- key ages: the daily reminders ------------------------------------------------------


def test_key_reminder_once_a_month(env):
    env.db.known_accounts.record("alice", "alice", "linked")
    env.alice.post("/api/subusers", json={"name": "ci", "permission": "read"}, headers=H)
    mail = env.app.state.mailer = FakeMailer()
    now = datetime.now(UTC)
    res = tick(env, now)  # first sighting of alice's own key; nothing is old yet
    assert res["keys"] == 2 and res["oldKeys"] == 0 and mail.keys == []
    own = next(r for r in env.db.key_ages.list() if r["kind"] == "user")["access_key"]
    sub = next(r for r in env.db.key_ages.list() if r["kind"] == "subuser")["access_key"]
    make_old(env, own, 200)
    make_old(env, sub, 181)
    res = tick(env, now)
    assert res["oldKeys"] == 2 and res["keyNotices"] == 2
    [(to, keys, limit, cloud)] = mail.keys
    assert (to, limit, cloud) == ("alice@example.org", 180, False)
    assert sorted((k["last4"], k["exact"], k.get("subuser")) for k in keys) == sorted(
        [(own[-4:], False, None), (sub[-4:], True, "ci")])
    tick(env, now + DAY)
    tick(env, now + 29 * DAY)
    assert len(mail.keys) == 1  # at most once per 30 days per key
    tick(env, now + 31 * DAY)
    assert len(mail.keys) == 2
    rows = [a for a in acts(env, "credentials.key.age_notice") if a["target"] == own]
    assert len(rows) == 2
    a = rows[-1]
    assert (a["actor"], a["target_type"]) == ("system", "access_key")
    assert a["details"]["uid"] == "alice" and a["details"]["kind"] == "user"
    assert a["details"]["ageExact"] is False and a["details"]["limitDays"] == 180
    assert a["details"]["emailed"] is True
    s = [a for a in acts(env, "credentials.key.age_notice") if a["target"] == sub][0]
    assert s["details"]["subuser"] == "ci"


def test_key_reminder_retry_off_and_email_off(env):
    env.db.known_accounts.record("alice", "alice", "linked")
    mail = env.app.state.mailer = FakeMailer(fail=True)
    now = datetime.now(UTC)
    tick(env, now)
    own = env.db.key_ages.list()[0]["access_key"]
    make_old(env, own, 300)
    tick(env, now)
    assert age_row(env, own)["reminded_at"] is None  # failed: tomorrow again
    mail.fail = False
    tick(env, now + DAY)
    assert len(mail.keys) == 2 and age_row(env, own)["reminded_at"]
    env.bob.put(S, json={"maxKeyAgeDays": 0}, headers=H)
    assert tick(env, now + 90 * DAY)["oldKeys"] == 0 and len(mail.keys) == 2  # reminders off
    env.bob.put(S, json={"maxKeyAgeDays": 180}, headers=H)
    env.app.state.mailer = Mailer(env.settings)  # SMTP_FROM empty: audit only
    tick(env, now + 90 * DAY)
    last = acts(env, "credentials.key.age_notice")[0]
    assert last["details"]["emailed"] is None and last["details"]["reason"] == "email_off"


def test_key_reminder_skips_suspended_and_key_users(env):
    env.db.known_accounts.record("alice", "alice", "linked")
    env.db.known_accounts.record("bob", "bob", "linked")
    env.db.bucket_keys.create("bob", "alice", "alice-data", "read", "x", "active")
    mail = env.app.state.mailer = FakeMailer()
    now = datetime.now(UTC)
    env.croit_state.users["alice"].suspended = True
    res = tick(env, now)
    assert res["keys"] == 0 and res["skipped"] == 1
    assert env.db.key_ages.list() == []  # neither person's keys were even looked at
    env.croit_state.users["alice"].suspended = False
    tick(env, now)
    make_old(env, env.db.key_ages.list()[0]["access_key"], 999)
    tick(env, now)
    assert [to for to, *_ in mail.keys] == ["alice@example.org"]


def test_key_pass_forgets_vanished_keys(env):
    env.db.known_accounts.record("alice", "alice", "linked")
    env.db.key_ages.seen([("GONEKEY0000000000001", "alice", "user")], "2026-01-01T00:00:00Z")
    tick(env, datetime.now(UTC))
    assert "GONEKEY0000000000001" not in {r["access_key"] for r in env.db.key_ages.list()}


def test_cloud_key_reminders(env):
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    env.app.state.cloud = StubCloud([
        {"accessKey": "AKIAOLD0000000000001", "status": "Active", "createdAt": old},
        {"accessKey": "AKIAOFF0000000000002", "status": "Inactive", "createdAt": old}])
    env.db.cloud_accounts.record("alice", "s3-alice", "created")
    mail = env.app.state.mailer = FakeMailer()
    now = datetime.now(UTC)
    res = tick(env, now)
    assert res["errors"] == []
    [(to, keys, limit, cloud)] = mail.keys
    assert (to, cloud, [k["last4"] for k in keys]) == ("alice@example.org", True, ["0001"])
    assert keys[0]["exact"] is True
    tick(env, now + DAY)
    assert len(mail.keys) == 1
    [a] = [a for a in acts(env, "credentials.key.age_notice") if a["details"]["kind"] == "cloud"]
    assert a["target"] == "AKIAOLD0000000000001" and a["details"]["adUsername"] == "alice"
    env.app.state.cloud = StubCloud([])  # the key was deleted in AWS: forgotten
    tick(env, now + 2 * DAY)
    assert not [r for r in env.db.key_ages.list() if r["kind"] == "cloud"]


# -- schedule ---------------------------------------------------------------------------


def local(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm).astimezone()


def test_due_rules():
    at8 = local(2026, 11, 28, 8)
    now = at8.astimezone(UTC)
    assert notices.due(at8, {}, now)
    assert not notices.due(local(2026, 11, 28, 6, 59), {}, now)
    assert not notices.due(at8, {"lastRunDay": "2026-11-28"}, now)
    assert notices.due(at8, {"lastRunDay": "2026-11-27"}, now)  # catch-up after a restart
    later = (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert not notices.due(at8, {"retryAfter": later}, now)
    assert notices.next_run(local(2026, 11, 28, 6), {}) == "2026-11-28T07:00"
    assert notices.next_run(at8, {"lastRunDay": "2026-11-28"}) == "2026-11-29T07:00"
    assert notices.next_run(at8, {}) == "2026-11-28T08:00"


def test_schedule_runs_once_a_day_and_retries_after_an_error(env):
    at8 = local(2026, 11, 28, 8).astimezone(UTC)
    assert asyncio.run(notices.schedule_tick(env.app, at8)) is not None
    assert asyncio.run(notices.schedule_tick(env.app, at8 + timedelta(hours=2))) is None
    state = env.db.settings.get("notices_state")
    assert state["lastRunDay"] == "2026-11-28" and state["lastRun"]["errors"] == []
    assert env.bob.get(S).json()["schedule"]["lastRun"]["at"].startswith(
        at8.strftime("%Y-%m-%d"))

    async def down():
        from bucketeer.croit import CroitUnavailable
        raise CroitUnavailable("down")

    env.app.state.croit.list_buckets = down
    nxt = at8 + DAY
    assert asyncio.run(notices.schedule_tick(env.app, nxt))["errors"] == ["buckets"]
    assert asyncio.run(notices.schedule_tick(env.app, nxt + timedelta(minutes=30))) is None
    assert asyncio.run(notices.schedule_tick(env.app, nxt + timedelta(minutes=61))) is not None


def test_loop_is_off_in_tests_and_never_raises(env, monkeypatch):
    assert notices.NOTICES_ON_START is False  # conftest switches the lifespan task off
    calls = []

    async def sleep(seconds):
        calls.append(seconds)
        if len(calls) > 2:
            raise asyncio.CancelledError

    async def boom(app, now=None):
        raise RuntimeError("x")

    monkeypatch.setattr(notices, "schedule_tick", boom)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(notices.notice_loop(env.app, sleep=sleep))
    assert calls == [notices.FIRST_DELAY, notices.TICK_SECONDS, notices.TICK_SECONDS]


# -- the messages -----------------------------------------------------------------------


def sent_messages(tmp_path, monkeypatch):
    m = Mailer(make_settings(tmp_path, smtp_from="bucketeer@example.org",
                             app_url="https://bucketeer.example.org"))
    out: list[EmailMessage] = []
    monkeypatch.setattr(m, "_send", out.append)
    return m, out


def test_near_full_message(tmp_path, monkeypatch):
    m, out = sent_messages(tmp_path, monkeypatch)
    assert asyncio.run(m.send_near_full("alice@example.org", "alice-data", 9 * 2**40,
                                        10 * 2**40, 90)) is True
    [msg] = out
    body = msg.get_content()
    assert msg["Subject"] == "Bucket alice-data is 90% full" and msg["To"] == "alice@example.org"
    assert "9.0 TiB of its 10.0 TiB quota (90% full)" in body
    assert "refuses new uploads" in body and "More, Change quota" in body
    assert "https://bucketeer.example.org/" in body and "once a week" in body
    assert asyncio.run(m.send_near_full("bad address", "x", 1, 1, 100)) is False


def test_key_age_message_names_only_the_last_four(tmp_path, monkeypatch):
    m, out = sent_messages(tmp_path, monkeypatch)
    keys = [{"last4": "WXYZ", "ageDays": 200, "exact": False},
            {"last4": "1234", "ageDays": 181, "exact": True, "subuser": "ci"}]
    assert asyncio.run(m.send_key_age("alice@example.org", keys, 180, False)) is True
    body = out[0].get_content()
    assert out[0]["Subject"] == "Time to rotate your S3 access keys"
    assert "ending in WXYZ, at least 200 days old" in body
    assert "subuser ci ending in 1234, 181 days old" in body
    assert "Rotate" in body and "Set up this computer" in body and "once a month" in body
    asyncio.run(m.send_key_age("alice@example.org", keys[:1], 180, True))
    assert out[1]["Subject"] == "Time to rotate your cloud (AWS) access key"
    assert "cloud (AWS) key ending in WXYZ" in out[1].get_content()


def test_email_off_sends_nothing(tmp_path):
    m = Mailer(make_settings(tmp_path))
    assert asyncio.run(m.send_near_full("alice@example.org", "b", 1, 1, 100)) is None
    assert asyncio.run(m.send_key_age("alice@example.org", [], 180, False)) is None


def test_backends_agree_on_the_listing_shape(env):
    """The near-full test reads the admin listing's `bucket_quota` / `usageSum` on both."""
    size, quota = fill(env, ratio=1.0)
    [b] = [b for b in asyncio.run(env.app.state.croit.list_buckets())
           if b["bucket"] == "alice-data"]
    assert notices.size_limit(b["bucket_quota"]) == quota and notices.usage_bytes(b) == size
    assert notices.near_full(b, 100) is True
