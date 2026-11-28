"""Admin alert emails (bucketeer/alerts.py): the contacts setting, sending, and the jobs that
send them (backup runs, restore tests, no backup for 48 hours, offboarding runs).

Sends are captured with `AlertMailer` (like the billing tests' FakeMailer); the real
`Mailer.send_admin_alert` is checked with its SMTP call replaced. croit admin backend only:
nothing here depends on the backend (tests/test_backup.py runs the backup jobs on both).
"""

import asyncio
import logging
import smtplib
from datetime import datetime, timedelta

import httpx
import pytest

from bucketeer import alerts, backup, offboarding
from bucketeer.config import Settings
from bucketeer.mailer import Mailer

from .conftest import REQUIRED
from .test_admin import Env as AdminEnv
from .test_backup import BUCKET, env_file  # noqa: F401  (fixture)
from .test_buckets import Env, H
from .test_offboarding import go, leave, patch_fails

OPS = "ops@example.org"


class AlertMailer:
    """Records `send_admin_alert` calls; addresses in `fail` are refused."""

    enabled = True

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    async def send_admin_alert(self, recipients, subject, body):
        self.calls.append({"to": list(recipients), "subject": subject, "body": body})
        return ([a for a in recipients if a not in self.fail],
                [a for a in recipients if a in self.fail])


def set_contacts(client, *addresses):
    r = client.put("/api/admin/alerts", json={"contacts": list(addresses)}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def alert_rows(env):
    return [a for a in env.db.audit.query(limit=500) if a["action"].startswith("admin.alert")]


@pytest.fixture
def aenv(tmp_path):
    e = AdminEnv(tmp_path)
    e.app.state.mailer = AlertMailer()
    e.svc = e.app.state.offboarding  # what test_offboarding.go() runs
    yield e
    e.db.close()


@pytest.fixture
def benv(tmp_path, s3_endpoint, env_file):  # noqa: F811
    e = Env(tmp_path, s3_endpoint, backup_bucket=BUCKET)
    e.app.state.mailer = AlertMailer()
    set_contacts(e.bob, OPS)
    yield e
    e.db.close()


# -- the setting --------------------------------------------------------------------------


def test_contacts_setting_validation_and_audit(aenv):
    got = aenv.admin.get("/api/admin/alerts").json()
    assert got["contacts"] == [] and got["maxContacts"] == 10 and got["last"] is None
    assert got["email"] == {"enabled": True}
    assert [k["kind"] for k in got["kinds"]] == list(alerts.KINDS)

    def put(body):
        return aenv.admin.put("/api/admin/alerts", json=body, headers=H)

    for bad in ({"contacts": ["nope"]}, {"contacts": ["a@b.c\r\nBcc: x@y.z"]},
                {"contacts": [f"u{i}@example.org" for i in range(11)]},
                {"contacts": OPS}, {}, {"contacts": [], "other": 1}):
        assert put(bad).status_code == 422, bad
    assert "alert contacts" in put({"contacts": [f"u{i}@example.org" for i in range(11)]}
                                   ).json()["detail"]
    r = put({"contacts": [" Ops@Example.org", OPS, "", "oncall@example.org"]})
    assert r.status_code == 200
    assert r.json()["contacts"] == ["Ops@Example.org", "oncall@example.org"]
    assert put({"contacts": ["Ops@Example.org", "oncall@example.org"]}).status_code == 200
    rows = [a for a in aenv.db.audit.query(limit=100) if a["action"] == "admin.settings.alerts"]
    assert len(rows) == 1  # the unchanged save writes nothing
    assert (rows[0]["actor"], rows[0]["target_type"], rows[0]["target"]) == (
        "bob", "setting", "alerts")
    assert rows[0]["details"] == {"before": {"contacts": []},
                                  "after": {"contacts": ["Ops@Example.org", "oncall@example.org"]}}
    assert put({"contacts": []}).json()["contacts"] == []


def test_admin_only(aenv):
    assert aenv.user.get("/api/admin/alerts").status_code == 403
    assert aenv.user.put("/api/admin/alerts", json={"contacts": []}, headers=H).status_code == 403
    assert aenv.admin.put("/api/admin/alerts", json={"contacts": []}).status_code == 403  # CSRF


# -- sending --------------------------------------------------------------------------------


def test_send_alert_mails_every_contact_and_audits(aenv):
    set_contacts(aenv.admin, OPS, "oncall@example.org")
    entry = asyncio.run(alerts.send_alert(aenv.app, "backup_failed", "Backup failed (AWS)",
                                          "What went wrong.\n"))
    (call,) = aenv.app.state.mailer.calls
    assert call["to"] == [OPS, "oncall@example.org"]
    assert call["subject"] == "[Bucketeer] Backup failed (AWS)"
    assert call["body"].startswith("What went wrong.") and "Settings tab" in call["body"]
    assert entry["emailed"] is True and entry["sent"] == [OPS, "oncall@example.org"]
    (row,) = alert_rows(aenv)
    assert (row["action"], row["actor"], row["target_type"], row["target"]) == (
        "admin.alert_sent", "system", "alert", "backup_failed")
    assert row["details"] == {"subject": "Backup failed (AWS)",
                              "sent": [OPS, "oncall@example.org"], "failed": []}
    assert aenv.admin.get("/api/admin/alerts").json()["last"]["kind"] == "backup_failed"
    # no contact's mail server takes it: admin.alert_failed
    aenv.app.state.mailer = AlertMailer(fail={OPS, "oncall@example.org"})
    entry = asyncio.run(alerts.send_alert(aenv.app, "backup_stale", "No backup", "x"))
    assert entry["emailed"] is False and entry["reason"] == "send_failed"
    row = alert_rows(aenv)[0]
    assert (row["action"], row["target"]) == ("admin.alert_failed", "backup_stale")
    assert row["details"]["failed"] == [OPS, "oncall@example.org"]


def test_without_email_or_contacts_it_only_logs(aenv, caplog):
    with caplog.at_level(logging.WARNING, logger="bucketeer.alerts"):
        entry = asyncio.run(alerts.send_alert(aenv.app, "backup_failed", "Backup failed", "x"))
        assert entry["reason"] == "no_contacts" and aenv.app.state.mailer.calls == []
        set_contacts(aenv.admin, OPS)
        aenv.app.state.mailer = Mailer(aenv.app.state.settings)  # SMTP_FROM empty: off
        entry = asyncio.run(alerts.send_alert(aenv.app, "backup_failed", "Backup failed", "x"))
        assert entry["reason"] == "email_off" and entry["emailed"] is False
    assert alert_rows(aenv) == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("not emailed (no alert contacts are set)" in m for m in msgs), msgs
    assert any("not emailed (email is off" in m for m in msgs), msgs
    got = aenv.admin.get("/api/admin/alerts").json()
    assert got["email"] == {"enabled": False} and got["last"]["reason"] == "email_off"


def test_send_alert_never_raises(aenv, caplog):
    class Broken(AlertMailer):
        async def send_admin_alert(self, *a):
            raise RuntimeError("smtp exploded")

    set_contacts(aenv.admin, OPS)
    aenv.app.state.mailer = Broken()
    with caplog.at_level(logging.ERROR, logger="bucketeer.alerts"):
        assert asyncio.run(alerts.send_alert(aenv.app, "backup_failed", "s", "b")) is None
    assert "could not be sent" in caplog.text and alert_rows(aenv) == []


def test_mailer_sends_one_plain_message_per_contact(monkeypatch):
    m = Mailer(Settings(_env_file=None, **{**REQUIRED, "smtp_host": "smtp.example.org",
                                           "smtp_from": "bucketeer@example.org",
                                           "app_url": "https://bucketeer.example.org"}))
    sent = []

    def send(msg):
        if msg["To"] == "bad@example.org":
            raise smtplib.SMTPRecipientsRefused({"bad@example.org": (550, b"no such user")})
        sent.append(msg)

    monkeypatch.setattr(m, "_send", send)
    res = asyncio.run(m.send_admin_alert([OPS, "bad@example.org", "x@y.z\r\nBcc: e@f.g"],
                                         "Backup\r\nBcc: evil@example.org failed", "Body"))
    assert res == ([OPS], ["bad@example.org", "x@y.z\r\nBcc: e@f.g"])
    (msg,) = sent
    assert msg["To"] == OPS and msg["Bcc"] is None and "\n" not in msg["Subject"]
    assert msg.get_content().strip() == "Body" and not list(msg.iter_attachments())
    off = Mailer(Settings(_env_file=None, **REQUIRED))
    assert asyncio.run(off.send_admin_alert([OPS], "s", "b")) is None


# -- backup runs and restore tests ------------------------------------------------------------


def test_failed_backup_run_and_restore_test_alert(benv):
    mail = benv.app.state.mailer
    assert benv.bob.post("/api/admin/backup/run", headers=H).json()["outcome"] == "ok"
    assert benv.bob.post("/api/admin/backup/restore-test", headers=H).json()["outcome"] == "ok"
    assert mail.calls == []  # good news is not mailed
    real = benv.app.state.backup.factory

    def broken(*a):
        raise RuntimeError("no S3 for you")

    benv.app.state.backup.factory = broken
    rec = benv.bob.post("/api/admin/backup/run", headers=H).json()
    assert rec["outcome"] == "failed"
    (call,) = mail.calls
    assert call["to"] == [OPS] and call["subject"] == "[Bucketeer] Backup failed (on premises)"
    assert f"bucket {BUCKET}: RuntimeError" in call["body"] and rec["key"] in call["body"]
    assert "by bob" in call["body"]
    test = benv.bob.post("/api/admin/backup/restore-test", headers=H).json()
    assert test["outcome"] == "failed"
    call = mail.calls[-1]
    assert call["subject"] == "[Bucketeer] Restore test failed (on premises)"
    assert "RuntimeError" in call["body"] and "live database was not touched" in call["body"]
    benv.app.state.backup.factory = real
    assert {r["target"] for r in alert_rows(benv)} == {"backup_failed", "restore_test_failed"}
    assert all("very-secret" not in c["body"] for c in mail.calls)


def iso(at):
    return at.astimezone().isoformat(timespec="seconds")


def a_run(rid, outcome, at, error=None):
    return {"id": rid, "outcome": outcome, "trigger": "schedule", "actor": "system",
            "startedAt": iso(at), "finishedAt": iso(at), "error": error, "destinations": []}


def test_no_successful_backup_for_48_hours_alerts_once_a_day(benv):
    svc, mail = benv.app.state.backup, benv.app.state.mailer
    now = [datetime(2026, 3, 10, 12, 0)]
    svc.now = lambda: now[0]
    start = now[0] - timedelta(hours=1)
    good = a_run(1, "ok", now[0] - timedelta(hours=47))
    benv.db.settings.set(backup.RUNS_KEY, [good])
    assert asyncio.run(svc.check_stale(start)) is False  # 47 hours
    bad = a_run(2, "failed", now[0] - timedelta(hours=23), "onprem: RuntimeError")
    benv.db.settings.set(backup.RUNS_KEY, [bad, good])
    now[0] += timedelta(hours=2)
    assert asyncio.run(svc.check_stale(start)) is True
    (call,) = mail.calls
    assert call["subject"] == "[Bucketeer] No successful backup for 49 hours"
    assert "onprem: RuntimeError" in call["body"] and BUCKET in call["body"]
    now[0] += timedelta(hours=23)
    assert asyncio.run(svc.check_stale(start)) is False and len(mail.calls) == 1  # once a day
    now[0] += timedelta(hours=1, minutes=1)
    assert asyncio.run(svc.check_stale(start)) is True and len(mail.calls) == 2
    assert backup.local_time(benv.db.settings.get(backup.STALE_KEY)["at"]) == now[0]
    # never a success: counted from the oldest kept run, else from the loop's start
    benv.db.settings.set(backup.STALE_KEY, {})
    benv.db.settings.set(backup.RUNS_KEY, [])
    assert asyncio.run(svc.check_stale(now[0] - timedelta(hours=40))) is False
    assert asyncio.run(svc.check_stale(now[0] - timedelta(hours=50))) is True
    assert "No backup run succeeded since" in mail.calls[-1]["body"]
    benv.db.settings.set(backup.STALE_KEY, {})
    benv.db.settings.set(backup.RUNS_KEY, [a_run(3, "failed", now[0] - timedelta(hours=60))])
    assert asyncio.run(svc.check_stale(now[0])) is True
    rows = alert_rows(benv)
    assert rows and {(r["action"], r["actor"], r["target"]) for r in rows} == {
        ("admin.alert_sent", "system", "backup_stale")}


def test_schedule_loop_checks_for_stale_backups(benv):
    svc, mail = benv.app.state.backup, benv.app.state.mailer
    clock = datetime(2026, 3, 10, 12, 0)
    svc.now = lambda: clock
    benv.db.settings.set(backup.RUNS_KEY, [a_run(1, "ok", clock - timedelta(days=3))])

    async def sleep(seconds):
        raise asyncio.CancelledError

    svc.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.schedule_loop())
    assert [c["subject"] for c in mail.calls] == ["[Bucketeer] No successful backup for 72 hours"]


# -- offboarding --------------------------------------------------------------------------------


def test_offboarding_aborted_and_failed_runs_alert(aenv):
    set_contacts(aenv.admin, OPS)
    mail = aenv.app.state.mailer
    for c in (aenv.user, aenv.admin):  # alice and bob are people Bucketeer knows
        assert c.get("/api/account").json()["status"] == "ready"
    assert go(aenv)["outcome"] == "ok" and mail.calls == []
    aenv.fake.fail_search = True
    run = go(aenv)
    assert run["outcome"] == "aborted"
    (call,) = mail.calls
    assert call["subject"] == "[Bucketeer] Offboarding run aborted"
    assert "AD could not be read" in call["body"] and "Nobody was suspended" in call["body"]
    assert f"#{run['id']}" in call["body"] and "started by bob" in call["body"]
    aenv.fake.fail_search = None
    aenv.db.settings.set("offboarding", {**offboarding.DEFAULT_SETTINGS, "maxPercent": 100,
                                         "mode": "enforce"})
    leave(aenv, "alice")
    patch_fails(aenv, lambda: httpx.Response(400, json={"code": 400, "message": "refused"}))
    run = go(aenv, trigger="schedule", actor="system")
    assert run["outcome"] == "failed"
    call = mail.calls[-1]
    assert call["subject"] == "[Bucketeer] Offboarding run failed"
    assert "1 suspension(s) failed" in call["body"] and "the daily schedule" in call["body"]
    aenv.transport.fail = None
    assert {r["target"] for r in alert_rows(aenv)} == {"offboarding_aborted",
                                                        "offboarding_failed"}
    assert all(r["actor"] == "system" for r in alert_rows(aenv))
