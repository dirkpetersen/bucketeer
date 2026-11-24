"""Billing: daily samples, monthly charges, fiscal year to date, settings, CSV export,
the chargeback .xlsx (P6-5) and the monthly billing email (P6-6)."""

import asyncio
import io
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from bucketeer import billing, billing_report
from bucketeer.db import Database

from .test_credentials import Env, H

TIB = 2**40


def rows(*items):
    return [{"day": d, "bucket": b, "owner": o, "bytes": n} for d, b, o, n in items]


SETTINGS = {"pricePerTiBMonth": 10.0, "currency": "USD", "fiscalYearStart": 7}


def test_monthly_charge_is_tib_days_over_days_in_month():
    today = date(2026, 11, 20)
    r = billing.report(rows(
        ("2026-11-01", "a", "alice", TIB), ("2026-11-02", "a", "alice", TIB),  # 2 TiB-days
        ("2026-10-15", "a", "alice", 31 * TIB),  # 31 TiB-days in a 31-day month: 1 TiB-month
        ("2026-10-15", "b", "bob", TIB // 2),
        ("2025-11-30", "a", "alice", TIB),  # 13 months ago: out of range
    ), SETTINGS, today, {"a": "DN099999"})
    assert r["months"][0] == "2025-12" and r["months"][-1] == "2026-11" and len(r["months"]) == 12
    a = next(b for b in r["buckets"] if b["bucket"] == "a")
    assert a["months"]["2026-11"] == round(10 * 2 / 30, 2)
    assert a["months"]["2026-10"] == 10.0 and a["budgetNumber"] == "DN099999"
    assert r["coverage"]["2026-10"] == 1 and r["coverage"]["2026-11"] == 2
    assert r["fiscalYear"] == {"start": "2026-07-01", "label": "FY 2026-27"}
    assert a["fytd"] == round(10.0 + 10 * 2 / 30, 2)
    users = {u["owner"]: u for u in r["users"]}
    assert users["bob"]["months"]["2026-10"] == round(10 * 0.5 / 31, 2)
    assert r["totals"]["months"]["2026-10"] == round(10 + 10 * 0.5 / 31, 2)


@pytest.mark.parametrize("start,expect", [(1, "2026-01-01"), (4, "2026-04-01"),
                                          (7, "2026-07-01"), (10, "2026-10-01")])
def test_fiscal_year_start(start, expect):
    assert billing.fiscal_start(date(2026, 11, 20), start).isoformat() == expect
    assert billing.fiscal_start(date(2026, 2, 1), 7).isoformat() == "2025-07-01"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    e.now[0] = datetime(2026, 11, 20, 12).timestamp()
    e.alice = e.session("alice")  # the clock moved: log in again
    yield e
    e.db.close()


def test_sample_records_today_and_the_api_reports_it(env):
    import asyncio

    n = asyncio.run(billing.sample(env.app, today=date(2026, 11, 20)))
    assert n > 0
    stored = env.db.usage.between("2026-11-01", "2026-11-30")
    assert {r["day"] for r in stored} == {"2026-11-20"}
    bob = env.session("bob")
    r = bob.get("/api/admin/billing")
    assert r.status_code == 200 and r.json()["coverage"]["2026-11"] == 1
    assert env.alice.get("/api/admin/billing").status_code == 403


def test_settings_validate_and_audit(env):
    bob = env.session("bob")
    r = bob.put("/api/admin/billing/settings",
                json={"pricePerTiBMonth": 12.5, "fiscalYearStart": 10}, headers=H)
    assert r.status_code == 200 and r.json()["pricePerTiBMonth"] == 12.5
    assert bob.get("/api/admin/billing").json()["fiscalYear"]["start"] == "2026-10-01"
    for bad in ({"fiscalYearStart": 3}, {"pricePerTiBMonth": -1}, {"currency": "US$"}):
        assert bob.put("/api/admin/billing/settings", json=bad, headers=H).status_code == 422
    acts = [a for a in env.db.audit.query(limit=100) if a["action"] == "admin.settings.billing"]
    assert len(acts) == 1


def test_csv_export(env):
    env.db.usage.record("2026-11-01", [("alice-data", "alice", 30 * TIB)])
    bob = env.session("bob")
    bob.put("/api/admin/billing/settings", json={"pricePerTiBMonth": 1}, headers=H)
    r = bob.get("/api/admin/billing/export.csv?view=buckets")
    assert r.status_code == 200 and r.text.startswith("﻿bucket,owner,budget_number,")
    lines = r.text.splitlines()
    assert lines[0].endswith("2026-11,fytd_FY_2026-27")
    assert lines[1].startswith("alice-data,alice,") and lines[1].endswith(",1.00,1.00")
    assert lines[-1].startswith("days_measured")
    u = bob.get("/api/admin/billing/export.csv?view=users")
    assert u.text.splitlines()[1].startswith("alice,1,")
    assert bob.get("/api/admin/billing/export.csv?view=x").status_code == 422
    assert any(a["action"] == "admin.export.billing" for a in env.db.audit.query(limit=100))


# -- P6-5: chargeback .xlsx ---------------------------------------------------------------


def qrows(bucket, owner, days, quota, size=0, month="2026-11", start=1):
    return [{"day": f"{month}-{start + i:02d}", "bucket": bucket, "owner": owner,
             "bytes": size, "quota_bytes": quota} for i in range(days)]


def test_quantity_is_the_daily_average_quota_rounded_up():
    # PLAN 3: 10 TiB for 20 days, then 40 TiB for 10 days of a 30-day month = 20 TiB
    usage = qrows("a", "alice", 20, 10 * TIB) + qrows("a", "alice", 10, 40 * TIB, start=21)
    (a,) = billing_report.tier3_lines(usage, 30)
    assert a["quantity"] == 20 and a["noQuotaDays"] == 0
    usage[0]["quota_bytes"] += 1  # one byte more: rounded up
    assert billing_report.tier3_lines(usage, 30)[0]["quantity"] == 21
    # days without a reading count 0: 10 TiB for 3 of 30 days = 1 TiB (rounded up)
    assert billing_report.tier3_lines(qrows("b", "bob", 3, 10 * TIB), 30)[0]["quantity"] == 1
    # no quota (unlimited): the measured size instead, flagged
    (u,) = billing_report.tier3_lines(qrows("c", "carol", 30, None, size=TIB + 1), 30)
    assert u["quantity"] == 2 and u["noQuotaDays"] == 30


def test_rows_and_problems():
    lines = billing_report.tier3_lines(
        qrows("acme:rep", "acme$carol", 30, TIB) + qrows("a", "alice", 30, TIB)
        + qrows("b", "alice", 30, None, size=5), 30)
    db = Database(":memory:")
    settings = billing.load(db)
    db.close()
    rows, problems = billing_report.build_rows(
        lines, "2026-11", {"a": ("DN1", True), "b": ("bad", False)},
        {"alice": "alice@example.org", "acme$carol": None}, settings)
    by = {r["note"]: r for r in rows}
    assert by["a"]["email"] == "alice@example.org" and by["a"]["index"] == "DN1"
    assert by["b"]["index"] is None and by["acme:rep"]["email"] == "acme$carol"
    assert by["a"]["product"] == "Tier-3-S3" and by["b"]["quantity"] == 1
    kinds = {(p["bucket"], p["problem"]) for p in problems}
    assert kinds == {("acme:rep", "No budget number"),
                     ("acme:rep", "No email address for the owner"),
                     ("b", "Budget number does not match the format"), ("b", "No quota")}


def test_sample_stores_the_quota():
    db = Database(":memory:")

    class Croit:
        async def list_buckets(self):
            return [
                {"bucket": "q", "owner": "alice", "usageSum": {"size": 5},
                 "bucket_quota": {"enabled": True, "maxSize": 3 * TIB, "maxObjects": -1}},
                {"bucket": "off", "owner": "alice", "usageSum": {"size": 7},
                 "bucket_quota": {"enabled": False, "maxSize": 3 * TIB}},
                {"bucket": "neg", "owner": "bob",
                 "bucket_quota": {"enabled": True, "maxSize": -1}},
            ]
    app = SimpleNamespace(state=SimpleNamespace(croit=Croit(), db=db))
    assert asyncio.run(billing.sample(app, today=date(2026, 11, 2))) == 3
    got = {r["bucket"]: r["quota_bytes"] for r in db.usage.between("2026-11-01", "2026-11-30")}
    assert got == {"q": 3 * TIB, "off": None, "neg": None}
    db.close()


def seed_month(env):
    env.db.buckets.create("alice-data", "alice", "=HYPERLINK(\"x\")")
    env.db.buckets.create("acme:reports", "acme$carol", None)
    for d in range(1, 31):
        env.db.usage.record(f"2026-10-{d:02d}", [
            ("alice-data", "alice", TIB, 10 * TIB if d <= 20 else 40 * TIB),
            ("acme:reports", "acme$carol", TIB // 2, None),
            ("ghost-b", "ghost", 0, TIB)])


def test_xlsx_download(env):
    seed_month(env)
    bob = env.session("bob")
    r = bob.get("/api/admin/billing/chargeback.xlsx?month=2026-10")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert 'filename="bucketeer-chargeback-2026-10.xlsx"' in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    ws, prob = wb.worksheets
    assert prob.title == "Problems"
    assert [c.value for c in ws[1]] == billing_report.COLUMNS and ws.max_column == 10
    rows = {row[6].value: row for row in ws.iter_rows(min_row=2)}
    a = rows["alice-data"]
    assert a[0].value == "alice@example.org"
    assert a[1].value == "'=HYPERLINK(\"x\")"  # csv_safe: never a formula
    # 31-day month: (10 x 20 + 40 x 10) / 31 = 19.4 -> 20
    assert a[2].value == "Tier-3-S3" and a[3].value == 20
    assert a[4].value == datetime(2026, 10, 1) and a[5].value == datetime(2026, 10, 31)
    assert a[4].number_format == a[5].number_format == "m/d/yyyy"
    assert a[7].value is None and a[8].value == "alice-data" and a[9].value is None
    c = rows["acme:reports"]
    # no AD name for a tenant uid: the RGW user's email is the fallback
    assert c[0].value == "carol@acme.example" and c[1].value is None and c[3].value == 1
    assert rows["ghost-b"][0].value == "ghost"  # no email anywhere: the uid
    problems = {(p[0].value, p[2].value) for p in prob.iter_rows(min_row=2)}
    assert problems == {("acme:reports", "No quota"), ("acme:reports", "No budget number"),
                        ("ghost-b", "No budget number"),
                        ("ghost-b", "No email address for the owner")}
    (a,) = actions(env, "admin.export.billing_xlsx")
    assert a["target_type"] == "report" and a["details"]["month"] == "2026-10"


def test_xlsx_month_checks(env):
    bob = env.session("bob")
    for bad in ("2026-13", "26-10", "2026-12", "x"):
        assert bob.get(f"/api/admin/billing/chargeback.xlsx?month={bad}").status_code == 422
    assert bob.get("/api/admin/billing/chargeback.xlsx?month=2026-11").status_code == 200
    assert env.alice.get("/api/admin/billing/chargeback.xlsx?month=2026-10").status_code == 403


def test_product_catalog_settings(env):
    bob = env.session("bob")
    s = bob.get("/api/admin/billing").json()["settings"]
    assert [(p["name"], p["pricePerTiBMonth"]) for p in s["products"]] == [
        ("Tier-3-S3", 0.0), ("Archive-S3", 1.0), ("Cloud-S3", 18.0)]
    r = bob.put("/api/admin/billing/settings", headers=H, json={"products": [
        {"id": "tier3", "name": "Tier3 Disk", "pricePerTiBMonth": 5},
        {"id": "cloud", "advertised": "about $15"}]})
    assert r.status_code == 200, r.text
    assert r.json()["pricePerTiBMonth"] == 5.0  # the Tier-3-S3 price is the billing price
    assert r.json()["products"][2]["advertised"] == "about $15"
    r = bob.put("/api/admin/billing/settings", json={"pricePerTiBMonth": 6}, headers=H)
    assert r.json()["products"][0]["pricePerTiBMonth"] == 6.0
    for bad in ([{"id": "nope"}], [{"id": "tier3", "name": "=evil"}],
                [{"id": "archive", "name": "cloud-s3"}],
                [{"id": "cloud", "pricePerTiBMonth": -1}],
                [{"id": "cloud", "advertised": "a\nb"}]):
        assert bob.put("/api/admin/billing/settings", json={"products": bad},
                       headers=H).status_code == 422, bad
    seed_month(env)
    wb = load_workbook(io.BytesIO(bob.get(
        "/api/admin/billing/chargeback.xlsx?month=2026-10").content))
    assert wb.worksheets[0]["C2"].value == "Tier3 Disk"


# -- P6-6: contacts, schedule, email ------------------------------------------------------


class FakeMailer:
    enabled = True

    def __init__(self, fail=()):
        self.s = SimpleNamespace(app_name="Bucketeer", app_url="https://bucketeer.example.org")
        self.fail, self.calls = set(fail), []

    async def send_with_attachment(self, recipients, subject, body, filename, data, mime, what):
        self.calls.append({"to": list(recipients), "subject": subject, "body": body,
                           "filename": filename, "data": data, "mime": mime})
        return ([a for a in recipients if a not in self.fail],
                [a for a in recipients if a in self.fail])


def actions(env, name):
    return [a for a in env.db.audit.query(limit=500) if a["action"] == name]


def test_contacts_send_day_and_switch(env):
    bob = env.session("bob")

    def put(body):
        return bob.put("/api/admin/billing/settings", json=body, headers=H)
    r = put({"contacts": [" Finance@Example.org", "finance@example.org", "", "ap@example.org"],
             "sendDay": 5})
    assert r.status_code == 200
    assert r.json()["contacts"] == ["Finance@Example.org", "ap@example.org"]
    for bad in ({"contacts": ["nope"]}, {"contacts": ["a@b.c\r\nBcc: x@y.z"]},
                {"contacts": [f"u{i}@example.org" for i in range(11)]},
                {"sendDay": 0}, {"sendDay": 29}, {"sendEnabled": "yes"}):
        assert put(bad).status_code == 422, bad
    # email is off in this environment: sending can't be switched on
    assert bob.get("/api/admin/billing").json()["email"] == {"enabled": False}
    assert put({"sendEnabled": True}).status_code == 409
    env.app.state.mailer = FakeMailer()
    assert put({"sendEnabled": True, "contacts": []}).status_code == 422
    r = put({"sendEnabled": True})
    assert r.status_code == 200 and r.json()["sendEnabled"] is True
    assert env.db.settings.get(billing.STATE_KEY)["sendFrom"].startswith("2026-11-20T12:00")
    rep = bob.get("/api/admin/billing").json()["report"]
    assert rep["nextSend"] == "2026-12-05T06:00" and rep["lastMonth"] == "2026-10"


def test_due_month_rules():
    s = {"sendEnabled": True, "contacts": ["a@example.org"], "sendDay": 3}
    due = billing.due_month
    assert due(datetime(2026, 11, 3, 5, 59), s, {}, 3600) is None
    assert due(datetime(2026, 11, 3, 6, 0), s, {}, 3600) == "2026-10"
    assert due(datetime(2026, 11, 20), s, {}, 3600) == "2026-10"  # missed day: caught up
    assert due(datetime(2027, 1, 3, 7), s, {}, 3600) == "2026-12"
    assert due(datetime(2026, 11, 20), s, {"lastSentMonth": "2026-10"}, 3600) is None
    assert due(datetime(2026, 11, 20), s, {"sendFrom": "2026-11-10T00:00:00"}, 3600) is None
    assert due(datetime(2026, 12, 3, 6), s, {"sendFrom": "2026-11-10T00:00:00"}, 3600) \
        == "2026-11"
    failed = {"failed": {"month": "2026-10", "at": "2026-11-20T10:00:00"}}
    assert due(datetime(2026, 11, 20, 10, 30), s, failed, 3600) is None
    assert due(datetime(2026, 11, 20, 11, 0), s, failed, 3600) == "2026-10"
    assert due(datetime(2026, 11, 20), {**s, "sendEnabled": False}, {}, 3600) is None


def test_scheduled_send_once_and_retry(env):
    seed_month(env)
    env.db.settings.set(billing.KEY, {"contacts": ["finance@example.org", "ap@example.org"],
                                      "sendDay": 1, "sendEnabled": True})
    mail = env.app.state.mailer = FakeMailer(fail={"ap@example.org"})
    tick = billing_report.report_tick
    assert asyncio.run(tick(env.app, datetime(2026, 11, 1, 5, 59))) is None
    r = asyncio.run(tick(env.app, datetime(2026, 11, 1, 6, 0)))
    assert r["sent"] == ["finance@example.org"] and r["failed"] == ["ap@example.org"]
    (call,) = mail.calls
    assert call["filename"] == "bucketeer-chargeback-2026-10.xlsx"
    assert call["mime"] == billing_report.XLSX_MIME and "October 2026" in call["subject"]
    assert load_workbook(io.BytesIO(call["data"])).worksheets[0]["G2"].value
    # restart later the same month: never twice
    assert asyncio.run(tick(env.app, datetime(2026, 11, 9))) is None and len(mail.calls) == 1
    state = env.db.settings.get(billing.STATE_KEY)
    assert state["lastSentMonth"] == "2026-10" and state["last"]["ok"] is True
    (sent,) = actions(env, "billing.report_sent")
    assert sent["actor"] == "system" and sent["details"]["failed"] == ["ap@example.org"]
    assert sent["details"]["month"] == "2026-10" and "data" not in sent["details"]
    # next month every address fails: audited, retried after an hour, then sent
    mail.fail = {"finance@example.org", "ap@example.org"}
    r = asyncio.run(tick(env.app, datetime(2026, 12, 1, 6, 0)))
    assert r["sent"] == [] and actions(env, "billing.report_failed")
    assert asyncio.run(tick(env.app, datetime(2026, 12, 1, 6, 30))) is None
    mail.fail = set()
    r = asyncio.run(tick(env.app, datetime(2026, 12, 1, 7, 0)))
    assert r["month"] == "2026-11" and len(r["sent"]) == 2
    assert env.db.settings.get(billing.STATE_KEY).get("failed") is None


def test_tick_does_nothing_while_email_is_off(env):
    env.db.settings.set(billing.KEY, {"contacts": ["f@example.org"], "sendEnabled": True})
    assert asyncio.run(billing_report.report_tick(env.app, datetime(2026, 11, 2))) is None
    assert not actions(env, "billing.report_failed")


def test_send_now(env):
    seed_month(env)
    bob = env.session("bob")

    def send(month):
        return bob.post("/api/admin/billing/report/send", json={"month": month}, headers=H)
    assert send("2026-10").status_code == 409  # email off
    mail = env.app.state.mailer = FakeMailer()
    assert send("2026-10").status_code == 409  # no contact
    bob.put("/api/admin/billing/settings", json={"contacts": ["f@example.org"]}, headers=H)
    assert send("2027-01").status_code == 422 and send("bad").status_code == 422
    r = send("2026-10")
    assert r.status_code == 200 and r.json()["sent"] == ["f@example.org"]
    assert r.json()["rows"] == 3 and len(mail.calls) == 1
    (a,) = actions(env, "billing.report_sent")
    assert a["actor"] == "bob" and a["details"]["trigger"] == "manual"
    assert "lastSentMonth" not in env.db.settings.get(billing.STATE_KEY)  # testing only
    mail.fail = {"f@example.org"}
    assert send("2026-10").status_code == 502 and actions(env, "billing.report_failed")
    assert env.alice.post("/api/admin/billing/report/send", json={"month": "2026-10"},
                          headers=H).status_code == 403
