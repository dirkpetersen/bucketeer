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
* `PUT /api/admin/billing/settings {pricePerTiBMonth?, currency?, fiscalYearStart?,
  products?: [{id: tier3|archive|cloud, name?, pricePerTiBMonth?, advertised?}], contacts?:
  [email] (0-10), sendDay? 1-28, sendEnabled?}` -> the settings; audit
  `admin.settings.billing` (target_type `setting`) when changed. `pricePerTiBMonth` IS the
  `tier3` (Tier-3-S3) price. Switching `sendEnabled` on is 409 while email is off and 422
  without a contact; it records `sendFrom` in `billing_report`, so only send moments after it
  count. `GET` also returns `email: {enabled}` and `report: {lastSentMonth, last, nextSend,
  lastMonth, sendDay (the one in force: at least 5 with cloud billing), cloudBilling}`.
  The sheet and the monthly email are in `billing_report.py`.

`sample()` also stores each bucket's size quota (`quota_bytes`, NULL = none) for the sheet.
"""

import asyncio
import calendar
import csv
import io
import logging
import math
import re
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr

from .auth import SessionUser, require_admin
from .db import Database, get_db

log = logging.getLogger("bucketeer.billing")

SAMPLE_ON_START = True  # tests switch the hourly sampler off and call sample()
REPORT_ON_START = True  # tests switch the monthly email loop off and call report_tick()
SAMPLE_SECONDS = 3600
TIB = 2**40
FY_STARTS = (1, 4, 7, 10)
KEY = "billing"
STATE_KEY = "billing_report"  # {lastSentMonth, sendFrom, last: {...}}; written by the job
# The product catalog (PLAN 0). Ids are fixed; the admin edits name, price, advertised text.
# `tier3` is on premises and billed on the quota; its price IS `pricePerTiBMonth`.
PRODUCT_IDS = ("tier3", "archive", "cloud")
DEFAULT_PRODUCTS = [
    {"id": "tier3", "name": "Tier-3-S3", "pricePerTiBMonth": 0.0,
     "advertised": "On premises (Ceph), billed on the quota you reserve"},
    {"id": "archive", "name": "Archive-S3", "pricePerTiBMonth": 1.0,
     "advertised": "AWS Glacier Deep Archive, about $1 per TiB-month"},
    {"id": "cloud", "name": "Cloud-S3", "pricePerTiBMonth": 18.0,
     "advertised": "AWS S3 Intelligent-Tiering, about $13-18 per TiB-month"},
]
DEFAULTS = {"pricePerTiBMonth": 0.0, "currency": "USD", "fiscalYearStart": 7,
            "contacts": [], "sendDay": 1, "sendEnabled": False}
MAX_CONTACTS = 10
_EMAIL = re.compile(r"[^@\s<>,;\"'()\[\]\\]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
                    r"\.[A-Za-z]{2,63}\Z")
_PRODUCT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}\Z")


def load(db: Database) -> dict[str, Any]:
    stored = db.settings.get(KEY) or {}
    out = {**DEFAULTS, **stored}
    by_id = {p.get("id"): p for p in stored.get("products") or [] if isinstance(p, dict)}
    out["products"] = [{**d, **{k: v for k, v in by_id.get(d["id"], {}).items()
                                if k in d and k != "id"}} for d in DEFAULT_PRODUCTS]
    out["products"][0]["pricePerTiBMonth"] = out["pricePerTiBMonth"]
    return out


def product(settings: dict, product_id: str) -> dict[str, Any]:
    return next(p for p in settings["products"] if p["id"] == product_id)


def load_state(db: Database) -> dict[str, Any]:
    return db.settings.get(STATE_KEY) or {}


def update_state(db: Database, by: str, **changes: Any) -> dict[str, Any]:
    """Read-modify-write of the job state in one transaction (None removes a key)."""
    with db.tx():
        state = {**load_state(db), **changes}
        state = {k: v for k, v in state.items() if v is not None}
        db.settings.set(STATE_KEY, state, by)
    return state


def _bucket_quota(b: dict) -> int | None:
    """Bytes of an enabled size quota, None for none (unlimited). Bucket quotas treat a
    bound at or below zero as no bound (croit and RGW alike)."""
    q = b.get("bucket_quota") or {}
    size = q.get("maxSize")
    if q.get("enabled") and isinstance(size, (int, float)) and size > 0:
        return int(size)
    return None


async def sample(app, today: date | None = None) -> int:
    """Record every bucket's size and quota for today; returns the number of buckets."""
    day = (today or date.today()).isoformat()
    rows = []
    for b in await app.state.croit.list_buckets():
        name = b.get("bucket") or ""
        if name:
            rows.append((name, b.get("owner") or "",
                         int((b.get("usageSum") or {}).get("size") or 0), _bucket_quota(b)))
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
    out = await _report(request, db)
    state = await db.run(load_state, db)
    now = datetime.fromtimestamp(request.app.state.clock())
    out["email"] = {"enabled": request.app.state.mailer.enabled}
    from .billing_report import send_settings
    from .cloud_costs import billing_enabled

    effective = send_settings(request.app, out["settings"])
    out["report"] = {"lastSentMonth": state.get("lastSentMonth"), "last": state.get("last"),
                     "nextSend": next_send(now, effective, state),
                     "lastMonth": previous_month(now), "sendDay": effective["sendDay"],
                     "cloudBilling": billing_enabled(request.app)}
    return out


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


class ProductBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr
    name: StrictStr | None = None
    pricePerTiBMonth: StrictFloat | StrictInt | None = None
    advertised: StrictStr | None = None


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pricePerTiBMonth: StrictFloat | StrictInt | None = None
    currency: StrictStr | None = None
    fiscalYearStart: StrictInt | None = None
    products: list[ProductBody] | None = None
    contacts: list[StrictStr] | None = None
    sendDay: StrictInt | None = None
    sendEnabled: StrictBool | None = None


def _price(value: float | int, what: str) -> float:
    p = float(value)
    if not math.isfinite(p) or not 0 <= p <= 1_000_000:
        raise HTTPException(422, f"The {what} must be a number from 0 to 1,000,000.")
    return round(p, 4)


def normalize_contacts(raw: list[str], what: str = "billing contacts") -> list[str]:
    """Trimmed, de-duplicated (any case) addresses, at most MAX_CONTACTS; 422 otherwise.
    Also used for the admin alert contacts (`alerts.py`)."""
    out: list[str] = []
    for a in raw:
        a = a.strip()
        if not a:
            continue
        if len(a) > 254 or not _EMAIL.match(a):
            raise HTTPException(422, f"\"{a[:80]}\" is not an email address.")
        if a.lower() not in {x.lower() for x in out}:
            out.append(a)
    if len(out) > MAX_CONTACTS:
        raise HTTPException(422, f"At most {MAX_CONTACTS} {what}.")
    return out


def _products(before: list[dict], changes: list[ProductBody]) -> list[dict]:
    out = [dict(p) for p in before]
    by_id = {p["id"]: p for p in out}
    for c in changes:
        if c.id not in by_id:
            raise HTTPException(422, f"Unknown product {c.id[:20]!r}.")
        p = by_id[c.id]
        if c.name is not None:
            name = c.name.strip()
            if not _PRODUCT_NAME.match(name):
                raise HTTPException(422, "A product name is 1-40 letters, digits, spaces, "
                                         "dots, hyphens or underscores.")
            p["name"] = name
        if c.pricePerTiBMonth is not None:
            p["pricePerTiBMonth"] = _price(c.pricePerTiBMonth, "price")
        if c.advertised is not None:
            text = c.advertised.strip()
            if len(text) > 200 or not all(ch.isprintable() for ch in text):
                raise HTTPException(422, "The advertised text is at most 200 printable "
                                         "characters on one line.")
            p["advertised"] = text
    if len({p["name"].lower() for p in out}) != len(out):
        raise HTTPException(422, "Every product needs its own name.")
    return out


@router.put("/settings")
async def put_settings(request: Request, body: SettingsBody, admin: Admin, db: Db):
    before = await db.run(load, db)
    after = {**before, "products": [dict(p) for p in before["products"]]}
    if body.products is not None:
        after["products"] = _products(after["products"], body.products)
        after["pricePerTiBMonth"] = after["products"][0]["pricePerTiBMonth"]
    if body.pricePerTiBMonth is not None:  # the Tier-3-S3 price; wins over `products`
        after["pricePerTiBMonth"] = _price(body.pricePerTiBMonth, "price")
        after["products"][0]["pricePerTiBMonth"] = after["pricePerTiBMonth"]
    if body.currency is not None:
        c = body.currency.strip().upper()
        if not (len(c) == 3 and c.isascii() and c.isalpha()):
            raise HTTPException(422, "The currency must be a 3-letter code such as USD.")
        after["currency"] = c
    if body.fiscalYearStart is not None:
        if body.fiscalYearStart not in FY_STARTS:
            raise HTTPException(422, "The fiscal year starts on 1/1, 4/1, 7/1 or 10/1.")
        after["fiscalYearStart"] = body.fiscalYearStart
    if body.contacts is not None:
        after["contacts"] = normalize_contacts(body.contacts)
    if body.sendDay is not None:
        if not 1 <= body.sendDay <= 28:
            raise HTTPException(422, "The send day is a day of the month from 1 to 28.")
        after["sendDay"] = body.sendDay
    if body.sendEnabled is not None:
        after["sendEnabled"] = body.sendEnabled
    if after["sendEnabled"] and not before["sendEnabled"] \
            and not request.app.state.mailer.enabled:
        raise HTTPException(409, "Email is off (SMTP_FROM is empty), so the monthly report "
                                 "can't be switched on.")
    if after["sendEnabled"] and not after["contacts"]:
        raise HTTPException(422, "Add at least one billing contact before switching the "
                                 "monthly email on.")
    if after != before:
        await db.run(db.settings.set, KEY, after, admin.username)
        await db.run(db.audit.add, admin.username, "admin.settings.billing", "setting", KEY,
                     {"before": before, "after": after})
        if after["sendEnabled"] and not before["sendEnabled"]:
            # only send moments from now on count: switching on after this month's send
            # day does not mail last month's sheet at once ("Send now" does that)
            start = datetime.fromtimestamp(request.app.state.clock()).isoformat(
                timespec="seconds")
            await db.run(update_state, db, admin.username, sendFrom=start)
    return after


# -- monthly email schedule (pure; the loop is in billing_report.py) ---------------------

REPORT_HOUR = 6


def month_key(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def previous_month(d: date | datetime) -> str:
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def send_moment(now: datetime, send_day: int) -> datetime:
    """This month's send time (server local, naive)."""
    return datetime(now.year, now.month, send_day, REPORT_HOUR)


def due_month(now: datetime, settings: dict, state: dict, retry_seconds: int) -> str | None:
    """The month to send now, or None: sending is on with contacts, this month's send moment
    has passed (so a day missed while the server was down is caught up at the next start),
    last month was not sent yet, the moment is not before sending was switched on, and a
    failed try of that month is at least `retry_seconds` old."""
    if not settings["sendEnabled"] or not settings["contacts"]:
        return None
    moment = send_moment(now, settings["sendDay"])
    if now < moment:
        return None
    month = previous_month(now)
    if (state.get("lastSentMonth") or "") >= month:
        return None
    since = state.get("sendFrom")
    if since and datetime.fromisoformat(since) > moment:
        return None
    failed = state.get("failed") or {}
    if failed.get("month") == month and failed.get("at") and \
            (now - datetime.fromisoformat(failed["at"])).total_seconds() < retry_seconds:
        return None
    return month


def next_send(now: datetime, settings: dict, state: dict) -> str | None:
    """When the next scheduled email goes out (ISO, server local), None while off."""
    if not settings["sendEnabled"] or not settings["contacts"]:
        return None
    if due_month(now, settings, state, retry_seconds=0):
        return now.isoformat(timespec="minutes")
    moment = send_moment(now, settings["sendDay"])
    if moment <= now:
        y, m = (now.year, now.month + 1) if now.month < 12 else (now.year + 1, 1)
        moment = datetime(y, m, settings["sendDay"], REPORT_HOUR)
    return moment.isoformat(timespec="minutes")
