"""Billing (admin): daily bucket sizes and monthly charges.

Ceph keeps no storage history, so `sample()` records every bucket's size (hourly from the
lifespan; the last reading of a day wins) in `usage_daily`. A bucket's charge for a month
is sum(daily size in TiB) / days in that month x the price per TiB-month: a day without a
reading costs nothing, and `coverage` says how many days of each month were measured. The
current month is "to date". The price in force applies to every month shown.

* `GET /api/admin/billing` -> `{settings: {pricePerTiBMonth, currency, fiscalYearStart},
  months: ["YYYY-MM" x 12, oldest first, the current month last], coverage: {month: days},
  daysInMonth: {month: n}, fiscalYear: {start, label}, firstDay (oldest reading or null),
  buckets: [{bucket, owner, budgetNumber, months: {month: amount}, fytd}],
  users: [{owner, buckets, months, fytd}], totals: {months, fytd}}`; amounts rounded to cents.
* `GET /api/admin/billing/export.csv?view=buckets|users` -> the same figures as CSV (UTF-8
  with BOM, CRLF, `csv_safe` cells): bucket view `bucket, owner, budget_number, <12 months>,
  fytd_<label>`; user view `owner, buckets, <12 months>, fytd_<label>`; a last `days_measured`
  row; audit `admin.export.billing` (target_type `report`).
* `PUT /api/admin/billing/settings {pricePerTiBMonth?, currency?, fiscalYearStart?}` -> the
  settings; audit `admin.settings.billing` (target_type `setting`) when changed.
"""

import asyncio
import calendar
import csv
import io
import logging
import math
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt, StrictStr

from .auth import SessionUser, require_admin
from .db import Database, get_db

log = logging.getLogger("bucketeer.billing")

SAMPLE_ON_START = True  # tests switch the hourly sampler off and call sample()
SAMPLE_SECONDS = 3600
TIB = 2**40
FY_STARTS = (1, 4, 7, 10)
KEY = "billing"
DEFAULTS = {"pricePerTiBMonth": 0.0, "currency": "USD", "fiscalYearStart": 7}


def load(db: Database) -> dict[str, Any]:
    return {**DEFAULTS, **(db.settings.get(KEY) or {})}


async def sample(app, today: date | None = None) -> int:
    """Record every bucket's size for today; returns the number of buckets."""
    day = (today or date.today()).isoformat()
    rows = []
    for b in await app.state.croit.list_buckets():
        name = b.get("bucket") or ""
        if name:
            rows.append((name, b.get("owner") or "",
                         int((b.get("usageSum") or {}).get("size") or 0)))
    await app.state.db.run(app.state.db.usage.record, day, rows)
    return len(rows)


async def sample_loop(app) -> None:
    while True:
        try:
            await sample(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  cluster trouble: try again next hour
            log.warning("billing sample failed: %s", type(e).__name__)
        await asyncio.sleep(SAMPLE_SECONDS)


def _months(today: date, n: int = 12) -> list[tuple[int, int]]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def fiscal_start(today: date, start_month: int) -> date:
    year = today.year if today.month >= start_month else today.year - 1
    return date(year, start_month, 1)


def report(rows: list[dict], settings: dict, today: date,
           budgets: dict[str, str | None]) -> dict[str, Any]:
    months = _months(today)
    keys = [f"{y:04d}-{m:02d}" for y, m in months]
    dim = {k: calendar.monthrange(y, m)[1] for k, (y, m) in zip(keys, months, strict=True)}
    price = settings["pricePerTiBMonth"]
    fy = fiscal_start(today, settings["fiscalYearStart"])
    fy_keys = [k for k in keys if k >= f"{fy.year:04d}-{fy.month:02d}"]
    tib_days: dict[str, dict[str, float]] = {}
    owners: dict[str, str] = {}
    days: dict[str, set] = {k: set() for k in keys}
    for r in rows:
        k = r["day"][:7]
        if k not in dim:
            continue
        days[k].add(r["day"])
        tib_days.setdefault(r["bucket"], {}).setdefault(k, 0.0)
        tib_days[r["bucket"]][k] += r["bytes"] / TIB
        owners[r["bucket"]] = r["owner"]  # rows are by day: the latest owner wins

    def money(v: float) -> float:
        return round(v + 0.0, 2)

    buckets, users = [], {}
    for name, per in tib_days.items():
        amounts = {k: price * per.get(k, 0.0) / dim[k] for k in keys}
        fytd = sum(amounts[k] for k in fy_keys)
        buckets.append({"bucket": name, "owner": owners[name], "budgetNumber": budgets.get(name),
                        "months": {k: money(v) for k, v in amounts.items()}, "fytd": money(fytd)})
        u = users.setdefault(owners[name], {"owner": owners[name], "buckets": 0,
                                            "months": dict.fromkeys(keys, 0.0), "fytd": 0.0})
        u["buckets"] += 1
        for k in keys:
            u["months"][k] += amounts[k]
        u["fytd"] += fytd
    for u in users.values():
        u["months"] = {k: money(v) for k, v in u["months"].items()}
        u["fytd"] = money(u["fytd"])
    totals = {"months": {k: money(sum(b["months"][k] for b in buckets)) for k in keys},
              "fytd": money(sum(b["fytd"] for b in buckets))}
    label = (f"FY {fy.year}" if fy.month == 1 else f"FY {fy.year}-{(fy.year + 1) % 100:02d}")
    return {
        "settings": settings, "months": keys, "daysInMonth": dim,
        "coverage": {k: len(days[k]) for k in keys},
        "fiscalYear": {"start": fy.isoformat(), "label": label},
        "firstDay": min((r["day"] for r in rows), default=None),
        "buckets": sorted(buckets, key=lambda b: (-b["fytd"], b["bucket"])),
        "users": sorted(users.values(), key=lambda u: (-u["fytd"], u["owner"])),
        "totals": totals,
    }


router = APIRouter(prefix="/admin/billing", tags=["admin"],
                   dependencies=[Depends(require_admin)])
Db = Annotated[Database, Depends(get_db)]
Admin = Annotated[SessionUser, Depends(require_admin)]


def _today(request: Request) -> date:
    return date.fromtimestamp(request.app.state.clock())


async def _report(request: Request, db: Database) -> dict[str, Any]:
    today = _today(request)
    settings = await db.run(load, db)
    first = f"{_months(today)[0][0]:04d}-{_months(today)[0][1]:02d}-01"
    rows = await db.run(db.usage.between, first, today.isoformat())
    budgets = {b["name"]: b.get("budget_number") for b in await db.run(db.buckets.list)}
    return report(rows, settings, today, budgets)


@router.get("")
async def get_billing(request: Request, db: Db):
    return await _report(request, db)


@router.get("/export.csv")
async def export_billing(request: Request, admin: Admin, db: Db,
                         view: Annotated[str, Query(pattern="^(buckets|users)$")] = "buckets"):
    from .admin import csv_safe

    r = await _report(request, db)
    fy = r["fiscalYear"]["label"].replace(" ", "_")
    head = (["bucket", "owner", "budget_number"] if view == "buckets" else ["owner", "buckets"])
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([*head, *r["months"], f"fytd_{fy}"])
    for row in r[view]:
        lead = ([row["bucket"], row["owner"], row["budgetNumber"] or ""] if view == "buckets"
                else [row["owner"], row["buckets"]])
        w.writerow([csv_safe(c) for c in
                    (*lead, *(f"{row['months'][m]:.2f}" for m in r["months"]),
                     f"{row['fytd']:.2f}")])
    w.writerow(["total", *[""] * (len(head) - 1),
                *(f"{r['totals']['months'][m]:.2f}" for m in r["months"]),
                f"{r['totals']['fytd']:.2f}"])
    w.writerow(["days_measured", *[""] * (len(head) - 1),
                *(r["coverage"][m] for m in r["months"]), ""])
    await db.run(db.audit.add, admin.username, "admin.export.billing", "report", "billing",
                 {"view": view, "rows": len(r[view]), "currency": r["settings"]["currency"]})
    day = _today(request).isoformat()
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="bucketeer-billing-{view}-{day}.csv"',
        "Cache-Control": "no-store"})


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pricePerTiBMonth: StrictFloat | StrictInt | None = None
    currency: StrictStr | None = None
    fiscalYearStart: StrictInt | None = None


@router.put("/settings")
async def put_settings(body: SettingsBody, admin: Admin, db: Db):
    before = await db.run(load, db)
    after = dict(before)
    if body.pricePerTiBMonth is not None:
        p = float(body.pricePerTiBMonth)
        if not math.isfinite(p) or not 0 <= p <= 1_000_000:
            raise HTTPException(422, "The price must be a number from 0 to 1,000,000.")
        after["pricePerTiBMonth"] = round(p, 4)
    if body.currency is not None:
        c = body.currency.strip().upper()
        if not (len(c) == 3 and c.isascii() and c.isalpha()):
            raise HTTPException(422, "The currency must be a 3-letter code such as USD.")
        after["currency"] = c
    if body.fiscalYearStart is not None:
        if body.fiscalYearStart not in FY_STARTS:
            raise HTTPException(422, "The fiscal year starts on 1/1, 4/1, 7/1 or 10/1.")
        after["fiscalYearStart"] = body.fiscalYearStart
    if after != before:
        await db.run(db.settings.set, KEY, after, admin.username)
        await db.run(db.audit.add, admin.username, "admin.settings.billing", "setting", KEY,
                     {"before": before, "after": after})
    return after
