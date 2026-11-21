"""Billing: daily samples, monthly charges, fiscal year to date, settings, CSV export."""

from datetime import date, datetime

import pytest

from bucketeer import billing

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
