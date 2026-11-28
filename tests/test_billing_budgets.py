"""Spend per budget number (billing_report.budget_view): `GET /api/admin/billing/budgets` and
its CSV. The figures must be exactly the chargeback sheet's, so most tests build the .xlsx
for the same month and compare the sums per Index. Cloud rows on moto (`mock_aws`), as in
tests/test_cloud_costs.py."""

import csv
import io
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
from moto import mock_aws
from openpyxl import load_workbook

from bucketeer import billing_report, cloud_costs
from bucketeer.cloud import CloudError

from .test_cloud_costs import CLOUD, OCTOBER, seed_buckets, upload_export
from .test_credentials import Env, H

TIB = 2**40
URL = "/api/admin/billing/budgets"
PRICE = 7.25


def seed_onprem(env, month="2026-10", days=31):
    env.db.buckets.create("alice-data", "alice", "DN100001")
    env.db.buckets.create("alice-more", "alice", "DN100001")
    env.db.buckets.create("bob-shared", "bob", "=HYPERLINK(\"x\")")  # must stay text
    env.db.buckets.create("acme:reports", "acme$carol", None)
    for d in range(1, days + 1):
        env.db.usage.record(f"{month}-{d:02d}", [
            ("alice-data", "alice", TIB, 10 * TIB if d <= 20 else 40 * TIB),  # 21 TiB
            ("alice-more", "alice", 0, 3 * TIB),
            ("bob-shared", "bob", 0, 2 * TIB),
            ("acme:reports", "acme$carol", TIB // 2, None),  # no quota: billed on size
            ("ghost-b", "ghost", 0, TIB)])  # no SQLite row: no budget number


def set_price(client, **extra):
    r = client.put("/api/admin/billing/settings", json={"pricePerTiBMonth": PRICE, **extra},
                   headers=H)
    assert r.status_code == 200, r.text


def sheet_sums(client, month):
    """{Index: [on-prem cents, cloud cents, rows]} read back from the .xlsx itself."""
    r = client.get(f"/api/admin/billing/chargeback.xlsx?month={month}")
    assert r.status_code == 200, r.text
    ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
    price = Decimal(str(client.get("/api/admin/billing").json()["settings"]["pricePerTiBMonth"]))
    out: dict = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        index, product, qty = row[1], row[2], row[3]
        if isinstance(index, str) and index.startswith("'"):
            index = index[1:]  # csv_safe's apostrophe
        s = out.setdefault(index, [0, 0, 0])
        if product == "Tier-3-S3":
            s[0] += int((price * qty * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        else:
            s[1] += qty  # cloud rows: cents
        s[2] += 1
    return out


def as_cents(view):
    return {r["budgetNumber"]: [round(r["onprem"] * 100), round(r["cloud"] * 100), r["buckets"]]
            for r in view["rows"]}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    e.now[0] = datetime(2026, 11, 20, 12).timestamp()
    e.alice, e.bob = e.session("alice"), e.session("bob")
    yield e
    e.db.close()


@pytest.fixture
def cloud_env(tmp_path):
    with mock_aws():
        e = Env(tmp_path, **CLOUD)
        e.now[0] = datetime(2026, 11, 20, 12).timestamp()
        seed_buckets()
        e.alice, e.bob = e.session("alice"), e.session("bob")
        yield e
        e.db.close()


def actions(env, name):
    return [a for a in env.db.audit.query(limit=500) if a["action"] == name]


# -- on premises ---------------------------------------------------------------------------


def test_onprem_spend_per_budget_number_is_the_sheets(env):
    seed_onprem(env)
    set_price(env.bob)
    r = env.bob.get(f"{URL}?month=2026-10")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    v = r.json()
    assert as_cents(v) == sheet_sums(env.bob, "2026-10")
    assert [x["budgetNumber"] for x in v["rows"]] == ["DN100001", "=HYPERLINK(\"x\")", None]
    dn, _, none = v["rows"]
    # 31-day month: (10 x 20 + 40 x 11) / 31 = 20.6 -> 21 TiB, plus 3 TiB, at 7.25
    assert dn == {"budgetNumber": "DN100001", "onprem": 174.0, "cloud": 0.0, "total": 174.0,
                  "buckets": 2, "owners": ["alice"], "mismatched": 0}
    # acme:reports (no number, measured 0.5 TiB -> 1) and ghost-b (no row, 1 TiB)
    assert none["onprem"] == 14.5 and none["buckets"] == 2
    assert none["owners"] == ["acme$carol", "ghost"]  # no AD name for these uids
    assert v["totals"] == {"onprem": 203.0, "cloud": 0.0, "total": 203.0, "buckets": 5}
    assert v["month"] == "2026-10" and v["currency"] == "USD" and v["final"] is True
    assert v["cloudEnabled"] is False and v["cloudAvailable"] is False
    assert "not set up" in v["cloudNote"]
    assert actions(env, "admin.export.billing_budgets") == []  # reading is not audited


def test_a_number_not_matching_the_format_is_no_number(env):
    seed_onprem(env)
    set_price(env.bob)
    env.db.settings.set("budget_number", {"pattern": r"DN\d{6}", "example": "DN000001"}, "t")
    v = env.bob.get(f"{URL}?month=2026-10").json()
    assert as_cents(v) == sheet_sums(env.bob, "2026-10")  # the sheet leaves Index empty too
    assert [x["budgetNumber"] for x in v["rows"]] == ["DN100001", None]
    none = v["rows"][-1]
    assert none["buckets"] == 3 and none["mismatched"] == 1 and none["onprem"] == 29.0
    assert "bob" in none["owners"]


def test_price_zero_and_an_empty_month(env):
    seed_onprem(env)
    v = env.bob.get(f"{URL}?month=2026-10").json()  # the default Tier-3-S3 price is 0
    assert v["totals"]["total"] == 0 and v["totals"]["buckets"] == 5
    assert as_cents(v) == sheet_sums(env.bob, "2026-10")
    empty = env.bob.get(f"{URL}?month=2026-09").json()
    assert empty["rows"] == [] and empty["totals"] == {"onprem": 0, "cloud": 0, "total": 0,
                                                       "buckets": 0}


def test_month_defaults_to_the_current_one_and_must_have_started(env):
    seed_onprem(env, month="2026-11", days=20)
    set_price(env.bob)
    v = env.bob.get(URL).json()
    assert v["month"] == "2026-11" and v["final"] is False  # still running
    assert as_cents(v) == sheet_sums(env.bob, "2026-11")
    for bad in ("2026-12", "2027-01", "2026-13", "26-10", "x", "1999-12"):
        assert env.bob.get(f"{URL}?month={bad}").status_code == 422, bad
        assert env.bob.get(f"{URL}.csv?month={bad}").status_code == 422, bad


def test_admin_only(env):
    for path in (URL, f"{URL}.csv"):
        assert env.alice.get(path).status_code == 403
        assert env.anon().get(path).status_code == 401
    assert actions(env, "admin.export.billing_budgets") == []


def test_csv_export(env):
    seed_onprem(env)
    set_price(env.bob)
    r = env.bob.get(f"{URL}.csv?month=2026-10")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="bucketeer-budgets-2026-10.csv"' in r.headers["content-disposition"]
    assert r.text.startswith("﻿budget_number,buckets,owners,onprem_USD,cloud_USD,total_USD\r\n")
    assert "\r\n" in r.text and "\n" not in r.text.replace("\r\n", "")
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[1] == ["DN100001", "2", "alice", "174.00", "0.00", "174.00"]
    assert rows[2][0] == "'=HYPERLINK(\"x\")"  # never a formula
    assert rows[3] == ["(none)", "2", "acme$carol; ghost", "14.50", "0.00", "14.50"]
    assert rows[-1] == ["(total)", "5", "", "203.00", "0.00", "203.00"]
    (a,) = actions(env, "admin.export.billing_budgets")
    assert a["actor"] == "bob" and a["target_type"] == "report" and a["target"] == "billing"
    assert a["details"]["month"] == "2026-10" and a["details"]["rows"] == 3


def test_csv_neutralizes_owner_names(env):
    env.db.buckets.create("evil-b", "=cmd", "+SUM(1)")
    env.db.usage.record("2026-10-01", [("evil-b", "=cmd", 0, TIB)])
    rows = list(csv.reader(io.StringIO(env.bob.get(f"{URL}.csv?month=2026-10")
                                        .text.lstrip("﻿"))))
    assert rows[1][0] == "'+SUM(1)" and rows[1][2] == "'=cmd"


# -- cloud ---------------------------------------------------------------------------------


def test_cloud_costs_join_their_budget_numbers(cloud_env):
    env = cloud_env
    seed_onprem(env)
    set_price(env.bob)
    upload_export("2026-10", [OCTOBER])
    v = env.bob.get(f"{URL}?month=2026-10").json()
    assert as_cents(v) == sheet_sums(env.bob, "2026-10")
    by = {r["budgetNumber"]: r for r in v["rows"]}
    # the cloud rows of the sheet: alice's scans 1.2442 USD -> 124 cents (DN123456), bob's
    # deep archive 0.505 -> 51 cents without a number; unknown buckets are not billed
    assert by["DN123456"] == {"budgetNumber": "DN123456", "onprem": 0.0, "cloud": 1.24,
                              "total": 1.24, "buckets": 1, "owners": ["alice"],
                              "mismatched": 0}
    assert by[None]["cloud"] == 0.51 and by[None]["buckets"] == 3
    assert by[None]["total"] == 15.01 and "bob" in by[None]["owners"]
    assert v["totals"] == {"onprem": 203.0, "cloud": 1.75, "total": 204.75, "buckets": 7}
    assert v["cloudEnabled"] is True and v["cloudAvailable"] is True and v["final"] is True
    # ghost-x (2 USD) and carol-odd (3 USD) are on the Problems sheet, not in any row
    assert "5.00 USD" in v["cloudNote"]
    assert [r["budgetNumber"] for r in v["rows"]][-1] is None


def test_cloud_not_final_yet(cloud_env):
    env = cloud_env
    upload_export("2026-11", [[{**OCTOBER[0], "line_item_usage_start_date":
                                "2026-11-03T00:00:00.000Z",
                                "bill_billing_period_start_date": "2026-11-01T00:00:00.000Z"}]])
    env.now[0] = datetime(2026, 12, 2, 12).timestamp()  # November is over, AWS not done
    bob = env.session("bob")
    v = bob.get(f"{URL}?month=2026-11").json()
    assert v["final"] is False and v["cloudAvailable"] is True
    assert "not final" in v["cloudNote"]
    assert v["totals"]["cloud"] == 1.23


def test_unreadable_export_counts_cloud_as_zero(cloud_env, monkeypatch):
    env = cloud_env
    seed_onprem(env)
    set_price(env.bob)

    def fail(*_a):
        raise CloudError(403, "AccessDenied", "AWS refused ListObjectsV2 (AccessDenied)")

    monkeypatch.setattr(cloud_costs, "read_month", fail)
    v = env.bob.get(f"{URL}?month=2026-10").json()
    assert v["cloudEnabled"] is True and v["cloudAvailable"] is False and v["final"] is False
    assert v["totals"]["cloud"] == 0 and v["totals"]["onprem"] == 203.0
    assert "unavailable" in v["cloudNote"] and "AccessDenied" in v["cloudNote"]
    assert as_cents(v) == sheet_sums(env.bob, "2026-10")


def test_other_currency_keeps_cloud_out_of_the_total(cloud_env):
    env = cloud_env
    seed_onprem(env)
    set_price(env.bob, currency="EUR")
    upload_export("2026-10", [OCTOBER])
    v = env.bob.get(f"{URL}?month=2026-10").json()
    assert v["currency"] == "EUR" and v["cloudCurrency"] == "USD"
    assert v["totals"] == {"onprem": 203.0, "cloud": 1.75, "total": 203.0, "buckets": 7}
    assert "not added" in v["cloudNote"]
    text = env.bob.get(f"{URL}.csv?month=2026-10").text
    assert text.lstrip("﻿").startswith(
        "budget_number,buckets,owners,onprem_EUR,cloud_USD,total_EUR")


def test_the_sheet_is_unchanged_by_the_refactor(cloud_env):
    """The .xlsx keeps exactly its ten columns; the extra row fields stay internal."""
    env = cloud_env
    seed_onprem(env)
    upload_export("2026-10", [OCTOBER])
    ws = load_workbook(io.BytesIO(env.bob.get(
        "/api/admin/billing/chargeback.xlsx?month=2026-10").content)).worksheets[0]
    assert [c.value for c in ws[1]] == billing_report.COLUMNS and ws.max_column == 10
