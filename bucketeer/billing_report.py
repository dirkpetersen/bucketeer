"""Monthly chargeback sheet (.xlsx) and the monthly billing email (PLAN 3; CLAUDE.md "Billing").

Sheet 1 "Chargeback": one row per bucket with readings in the month, exactly the columns
`COLUMNS` (A-J). On-premises buckets are product `tier3` (its catalog name, default
`Tier-3-S3`); Quantity = the month's daily average QUOTA in TiB (2^40), rounded up: the sum
of each day's quota over the days of the month, a day without a reading counts 0. A day
without a size quota (unlimited) counts the measured size instead, and the bucket is listed
on the Problems sheet. Cloud rows (P6-11, `cloud_rows`): one per cloud bucket with a cost
in the month's AWS data export (`cloud_costs.month_costs`), Quantity = that cost in CENTS
(rounded to the nearest cent), product from the bucket's `bucketeer-product` tag
(catalog `archive` / `cloud`, by name or id), A/B from its `bucketeer-owner-email` /
`bucketeer-budget` tags (read with the AWS admin user, `cloud_costs.bucket_facts_for`).
On the Problems sheet, with their amounts: buckets with our prefix but no owner tag
("Unknown cloud bucket", not billed), an unknown product, system costs (billing and backup
buckets), S3 costs without a bucket name, an unreadable export and a month that is not
final yet. E/F are real Excel dates (`m/d/yyyy`), G and I the
tenant-qualified bucket name, H and J empty. A = the owner's AD `mail` (AD names of the uid
from `known_accounts` and `rgw_user.ad_names_for_uid`, looked up without a password; else
the RGW user's email; else the uid, listed on Problems). B = the budget number, empty when
missing or not matching the admin pattern (listed on Problems). Every text cell goes
through `admin.csv_safe` (formula injection works in xlsx too).

* `GET /api/admin/billing/chargeback.xlsx?month=YYYY-MM` (not a future month) -> the file,
  attachment `bucketeer-chargeback-YYYY-MM.xlsx`, `no-store`; audit
  `admin.export.billing_xlsx` (target_type `report`, details month, rows, problems).
* `GET /api/admin/billing/cloud?month=YYYY-MM` -> `{enabled, month, available, final,
  generatedAt, fetchedAt, stale, reason, exportTotal, rowsTotal, rows, unknownTotal,
  systemTotal, unattributed, currency}` (amounts in USD, the export's currency): the
  export's S3 total for our buckets next to the sum of the sheet's cloud rows.
* `GET /api/admin/billing/budgets?month=YYYY-MM` (default the current month; not a future
  one; `no-store`) -> `{month, currency, cloudCurrency, final, cloudEnabled,
  cloudAvailable, cloudNote, rows: [{budgetNumber (null = none or not matching the
  format, like the sheet's empty Index), onprem, cloud, total, buckets, owners: [AD name,
  else uid], mismatched (buckets with a stored number that does not match)}], totals:
  {onprem, cloud, total, buckets}}`: the sheet's rows (`chargeback()`, the same function
  the .xlsx uses) summed per Index. On premises a row costs Quantity x the Tier-3-S3
  price, rounded to the cent (`row_cents`); a cloud row its Quantity in cents. Rows by
  total descending, the null row last. Cloud amounts are 0 while cloud billing is off or
  the export unreadable (`cloudAvailable` false, `cloudNote` says why); they are USD and
  only added to `total` when the billing currency is USD too. `final` = the month is over
  and (no cloud billing, or the cloud costs are final).
* `GET /api/admin/billing/budgets.csv?month=` -> the same as CSV (UTF-8 with BOM, CRLF,
  `csv_safe` cells; `budget_number, buckets, owners, onprem_<cur>, cloud_USD, total_<cur>`,
  `(none)` for the null row, a last `(total)` row); audit `admin.export.billing_budgets`
  (target_type `report`, target `billing`; details month, rows, currency, final).
* `POST /api/admin/billing/report/send {month}` mails that month's sheet to the billing
  contacts now (for testing; does not count as the scheduled send) -> `{month, sent,
  failed, rows, problems}`; 409 while email is off or no contact is set, 502 when no
  address took it.
* `report_loop` (lifespan task, `billing.REPORT_ON_START`) checks every minute: on the send
  day at 06:00 server local time (or at the first tick after it, so a missed day is caught
  up at the next start; with cloud billing set up the send day is at least the 5th,
  `cloud_costs.FINAL_DAY`, so AWS has finished the month) it mails LAST month's sheet,
  then records `lastSentMonth` in the settings key `billing_report`; a failed try is
  retried hourly.
* Audit `billing.report_sent` / `billing.report_failed` (target_type `report`, target
  `billing`; details month, recipients, failed, trigger, reason; never the content);
  actor `system` for the schedule, else the admin.
"""

import asyncio
import calendar
import csv
import io
import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictStr

from . import billing, cloud_costs
from .auth import SessionUser, require_admin
from .budget import budget_matches, load_budget_rules
from .db import Database, get_db
from .rgw_user import ad_names_for_uid

log = logging.getLogger("bucketeer.billing")

COLUMNS = ["Username / Email", "Index", "Product Name", "Quantity", "Order Date",
           "Fulfillment Date", "Note", "Order", "Reference ID", "Project Name"]
PROBLEM_COLUMNS = ["Bucket", "Owner", "Problem", "Details"]
DATE_FORMAT = "m/d/yyyy"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TICK_SECONDS = 60
RETRY_SECONDS = 3600
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_bounds(month: str) -> tuple[date, date, int]:
    y, m = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, days), days


def tier3_lines(usage: list[dict], days: int) -> list[dict[str, Any]]:
    """Per bucket: quantity (whole TiB, rounded up) from the daily quota readings, plus
    how many readings had no quota (billed on the measured size instead)."""
    per: dict[str, dict[str, Any]] = {}
    for r in usage:  # ordered by day: the latest owner wins
        b = per.setdefault(r["bucket"], {"bucket": r["bucket"], "owner": r["owner"],
                                          "byteDays": 0, "readings": 0, "noQuotaDays": 0})
        b["owner"] = r["owner"]
        b["readings"] += 1
        quota = r.get("quota_bytes")
        if quota is None:
            b["noQuotaDays"] += 1
            b["byteDays"] += int(r["bytes"])
        else:
            b["byteDays"] += int(quota)
    for b in per.values():
        b["quantity"] = -(-b["byteDays"] // (billing.TIB * days))  # integer ceil
        b["product"] = "tier3"
    return sorted(per.values(), key=lambda b: b["bucket"])


def build_rows(lines: list[dict], month: str, budgets: dict[str, tuple[str | None, bool]],
               emails: dict[str, str | None] | None, settings: dict
               ) -> tuple[list[dict], list[dict]]:
    """Sheet rows and Problems rows. `budgets[bucket] = (number, usable)`; `emails` None =
    not looked up (column A shows the uid, no email problem). Besides the sheet's columns
    each row keeps `location`, `owner`, `productId` and the stored `budget` (`row_cents`,
    `budget_view`)."""
    first, last, _ = month_bounds(month)
    rows, problems = [], []
    for line in lines:
        bucket, owner = line["bucket"], line["owner"]
        number, usable = budgets.get(bucket, (None, False))
        if not number:
            problems.append({"bucket": bucket, "owner": owner, "problem": "No budget number",
                             "details": "Index left empty"})
        elif not usable:
            problems.append({"bucket": bucket, "owner": owner,
                             "problem": "Budget number does not match the format",
                             "details": f"Stored value {number}; Index left empty"})
        email = emails.get(owner) if emails is not None else None
        if not email and emails is not None:
            problems.append({"bucket": bucket, "owner": owner,
                             "problem": "No email address for the owner",
                             "details": "Column A shows the S3 user id"})
        if line["noQuotaDays"]:
            problems.append({
                "bucket": bucket, "owner": owner, "problem": "No quota",
                "details": f"No size quota on {line['noQuotaDays']} of {line['readings']} "
                           f"days measured; billed on the measured size for those days"})
        rows.append({
            "email": email or owner, "index": number if number and usable else None,
            "product": billing.product(settings, line["product"])["name"],
            "quantity": line["quantity"], "orderDate": first, "fulfillmentDate": last,
            "note": bucket, "referenceId": bucket,
            "location": "onprem", "owner": owner, "productId": line["product"],
            "budget": number or None,
        })
    rows.sort(key=lambda r: (r["email"].lower(), r["note"]))
    return rows, problems


CLOUD_PRODUCTS = ("archive", "cloud")
EXPORT_CURRENCY = "USD"


def cloud_product(settings: dict, value: str | None) -> str | None:
    """The catalog id (`archive` / `cloud`) a `bucketeer-product` tag names: the current
    product name, its default name (renamed since) or the id, any case; else None."""
    v = (value or "").strip().lower()
    if not v:
        return None
    for p in [*settings["products"], *billing.DEFAULT_PRODUCTS]:
        if p["id"] in CLOUD_PRODUCTS and v in (p["id"], p["name"].strip().lower()):
            return p["id"]
    return None


def cents(cost: float) -> int:
    return int((Decimal(str(cost)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _usd(cost: float) -> str:
    amount = Decimal(str(cost)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount} {EXPORT_CURRENCY}"


def cloud_rows(costs: dict, facts: dict[str, dict], month: str, settings: dict,
               usable: dict[str | None, bool], billing_bucket: str = ""
               ) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Cloud sheet rows, Problems rows and the totals for the Billing tab. `usable[number]`
    says whether a budget number matches the admin pattern."""
    first, last, _ = month_bounds(month)
    rows: list[dict] = []
    problems: list[dict] = []
    summary: dict[str, Any] = {"rowsTotal": 0.0, "rows": 0, "unknownTotal": 0.0}

    def problem(bucket, owner, what, details):
        problems.append({"bucket": bucket, "owner": owner, "problem": what,
                         "details": details})

    if not costs.get("available"):
        problem("(cloud)", "", "Cloud costs unavailable",
                costs.get("reason") or "The billing export is unreadable")
        return rows, problems, summary
    if costs.get("stale"):
        problem("(cloud)", "", "Cloud costs not current",
                f"Export read {costs.get('fetchedAt')}; reading it again failed: "
                f"{costs.get('reason')}")
    if not costs.get("final"):
        problem("(cloud)", "", "Cloud costs not final",
                f"AWS finishes a month by the {cloud_costs.FINAL_DAY}th of the next one; "
                f"export generated {costs.get('generatedAt') or 'never'}")
    unknown = Decimal(0)
    row_cents = 0
    for bucket, cost in costs["buckets"].items():
        f = facts.get(bucket) or {}
        owner = (f.get("owner") or "").strip()
        if not owner:
            unknown += Decimal(str(cost))
            problem(bucket, "", "Unknown cloud bucket",
                    f"{_usd(cost)} not billed: no bucketeer-owner tag"
                    + (f" ({f['reason']})" if f.get("reason") else ""))
            continue
        pid = cloud_product(settings, f.get("product"))
        if pid is None:
            unknown += Decimal(str(cost))
            problem(bucket, owner, "Unknown product",
                    f"{_usd(cost)} not billed: the bucketeer-product tag is "
                    f"{f.get('product') or 'missing'}")
            continue
        quantity = cents(cost)
        if quantity == 0:
            continue
        number = (f.get("budget") or "").strip() or None
        ok = bool(number) and usable.get(number, False)
        if not number:
            problem(bucket, owner, "No budget number", "Index left empty")
        elif not ok:
            problem(bucket, owner, "Budget number does not match the format",
                    f"Stored value {number}; Index left empty")
        email = (f.get("email") or "").strip()
        if "@" not in email:
            email = ""
            problem(bucket, owner, "No email address for the owner",
                    "Column A shows the owner tag")
        row_cents += quantity
        rows.append({
            "email": email or owner, "index": number if ok else None,
            "product": billing.product(settings, pid)["name"], "quantity": quantity,
            "orderDate": first, "fulfillmentDate": last, "note": bucket, "referenceId": bucket,
            "location": "cloud", "owner": owner, "productId": pid, "budget": number,
        })
    for name, cost in (costs.get("system") or {}).items():
        if cost:
            kind = "billing export" if name == billing_bucket else "backups"
            problem(name, "Bucketeer", "System cost",
                    f"{_usd(cost)} for Bucketeer's {kind}; not billed")
    if costs.get("unattributed"):
        problem("(no bucket)", "", "S3 cost without a bucket name",
                f"{_usd(costs['unattributed'])} (taxes or account-level S3 charges); "
                f"not billed")
    summary.update(rowsTotal=row_cents / 100, rows=len(rows), unknownTotal=float(unknown))
    return rows, problems, summary


async def cloud_part(app, month: str, pattern: str | None
                     ) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """The month's cloud rows, problems and Billing-tab summary; nothing when cloud
    billing is not set up."""
    summary: dict[str, Any] = {"enabled": cloud_costs.billing_enabled(app), "month": month,
                               "currency": EXPORT_CURRENCY}
    if not summary["enabled"]:
        return [], [], summary
    db: Database = app.state.db
    costs = await cloud_costs.month_costs(app, month)
    facts = (await cloud_costs.bucket_facts_for(app, list(costs["buckets"]))
             if costs.get("available") else {})
    numbers = {(f.get("budget") or "").strip() or None for f in facts.values()}

    def judge():
        out = {}
        for n in numbers:
            try:
                out[n] = budget_matches(n, pattern) is not False
            except TimeoutError:
                out[n] = True  # unjudged: keep it, as the admin list does
        return out

    usable = await asyncio.to_thread(judge)
    settings = await db.run(billing.load, db)
    rows, problems, part = cloud_rows(costs, facts, month, settings, usable,
                                      app.state.settings.cloud_billing_bucket)
    summary.update(part)
    summary.update({k: costs.get(k) for k in ("available", "final", "generatedAt",
                                               "fetchedAt", "reason", "exportTotal",
                                               "unattributed")})
    summary["stale"] = bool(costs.get("stale"))
    summary["systemTotal"] = round(sum((costs.get("system") or {}).values()), 8)
    return rows, problems, summary


def _text(value: Any) -> str | None:
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    from .admin import csv_safe

    text = ILLEGAL_CHARACTERS_RE.sub("", csv_safe(value))
    return text or None


def to_xlsx(rows: list[dict], problems: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    wb.properties.creator = "Bucketeer"
    ws = wb.active
    ws.title = "Chargeback"
    ws.append(COLUMNS)
    for r in rows:
        ws.append([_text(r["email"]), _text(r["index"]), _text(r["product"]), r["quantity"],
                   r["orderDate"], r["fulfillmentDate"], _text(r["note"]), None,
                   _text(r["referenceId"]), None])
        for cell in ws[ws.max_row][4:6]:
            cell.number_format = DATE_FORMAT
    p = wb.create_sheet("Problems")
    p.append(PROBLEM_COLUMNS)
    for r in problems:
        p.append([_text(r["bucket"]), _text(r["owner"]), _text(r["problem"]),
                  _text(r["details"])])
    for sheet, widths in ((ws, (32, 14, 14, 10, 12, 16, 32, 10, 32, 14)), (p, (32, 24, 40, 70))):
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for i, w in enumerate(widths):
            sheet.column_dimensions[chr(ord("A") + i)].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def owner_names(app, uids: set[str]) -> dict[str, list[str]]:
    """uid -> the AD names linked to it (`known_accounts`, then `rgw_user.ad_names_for_uid`);
    SQLite only, no directory lookup."""
    db = app.state.db
    tenant = getattr(app.state.rgw, "tenant", "")
    out: dict[str, list[str]] = {}
    for uid in sorted(uids):
        names = [r["ad_username"] for r in await db.run(db.known_accounts.for_uid, uid)]
        names += [n for n in await db.run(ad_names_for_uid, db, uid, tenant) if n not in names]
        out[uid] = names
    return out


async def owner_emails(app, uids: set[str]) -> dict[str, str | None]:
    """uid -> the owner's email: AD `mail` of an AD name linked to the uid (no password
    bind), else the RGW user's email, else None. Lookup trouble only costs the address."""
    try:
        rgw = {str(u.get("uid")): (u.get("email") or "").strip()
               for u in await app.state.croit.list_users()}
    except Exception as e:  # noqa: BLE001  the AD address is the main source anyway
        log.warning("chargeback: RGW user list unreadable (%s)", type(e).__name__)
        rgw = {}
    linked = await owner_names(app, uids)
    out: dict[str, str | None] = {}
    for uid in sorted(uids):
        names = linked[uid]
        email = None
        for name in names[:3]:
            try:
                user = await app.state.authenticator.lookup_user(name)
            except Exception:  # noqa: BLE001  not found, left the group, LDAP down
                continue
            if user.email and "@" in user.email:
                email = user.email.strip()
                break
        out[uid] = email or rgw.get(uid) or None
    return out


async def chargeback(app, month: str, *, emails: bool = True) -> dict[str, Any]:
    """The month's chargeback: `{rows, problems, settings, cloud}` (sheet rows on premises
    and cloud, Problems rows, the billing settings, the cloud summary). The ONE computation
    behind the .xlsx, the monthly email and the spend per budget number (`budget_view`), so
    their figures can never differ. `emails=False` skips the AD address lookups: only
    column A (and its Problems entries) depend on them, never a quantity or an Index."""
    db: Database = app.state.db
    first, last, days = month_bounds(month)
    usage = await db.run(db.usage.between, first.isoformat(), last.isoformat())
    lines = tier3_lines(usage, days)
    settings = await db.run(billing.load, db)
    numbers = {b["name"]: b.get("budget_number") for b in await db.run(db.buckets.list)}
    pattern = (await db.run(load_budget_rules, db.settings))["pattern"]

    def judge():
        out, seen = {}, {}
        for line in lines:
            n = numbers.get(line["bucket"])
            if n not in seen:
                try:
                    seen[n] = budget_matches(n, pattern) is not False
                except TimeoutError:
                    seen[n] = True  # unjudged: keep it, as the admin list does
            out[line["bucket"]] = (n, seen[n])
        return out

    budgets = await asyncio.to_thread(judge)
    addresses = (await owner_emails(app, {line["owner"] for line in lines})
                 if emails else None)
    rows, problems = build_rows(lines, month, budgets, addresses, settings)
    c_rows, c_problems, summary = await cloud_part(app, month, pattern)
    rows = sorted(rows + c_rows, key=lambda r: (r["email"].lower(), r["note"]))
    return {"rows": rows, "problems": problems + c_problems, "settings": settings,
            "cloud": summary}


async def build(app, month: str) -> tuple[bytes, dict[str, int]]:
    """The month's workbook plus `{rows, problems}`."""
    c = await chargeback(app, month)
    data = await asyncio.to_thread(to_xlsx, c["rows"], c["problems"])
    return data, {"rows": len(c["rows"]), "problems": len(c["problems"])}


# -- spend per budget number -------------------------------------------------------------


def row_cents(row: dict, settings: dict) -> int:
    """What one sheet row bills, in cents: a Tier-3-S3 row is its Quantity (whole TiB) x the
    product's price per TiB-month (billing currency), rounded half up to the cent; a cloud
    row's Quantity already is cents (USD, the export's currency)."""
    if row["location"] == "cloud":
        return int(row["quantity"])
    price = Decimal(str(billing.product(settings, row["productId"])["pricePerTiBMonth"]))
    return int((price * row["quantity"] * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _owner_uid(row: dict) -> str:
    """The RGW uid behind a row's owner (a cloud owner tag names a tenant as `tenant:uid`)."""
    owner = row["owner"]
    return owner.replace(":", "$", 1) if row["location"] == "cloud" else owner


def budget_view(c: dict, month: str, today: date,
                names: dict[str, list[str]]) -> dict[str, Any]:
    """The chargeback (`chargeback()`) summed per budget number (the sheet's Index): see
    `GET /api/admin/billing/budgets`. Pure; `names` = uid -> AD names (`owner_names`)."""
    settings, cloud = c["settings"], c["cloud"]
    currency = settings["currency"]
    mixed = currency != EXPORT_CURRENCY
    groups: dict[str | None, dict[str, Any]] = {}
    for row in c["rows"]:
        g = groups.setdefault(row["index"], {"onprem": 0, "cloud": 0, "buckets": 0,
                                             "owners": set(), "mismatched": 0})
        g[row["location"]] += row_cents(row, settings)
        g["buckets"] += 1
        uid = _owner_uid(row)
        g["owners"].add((names.get(uid) or [row["owner"]])[0])
        if row["index"] is None and row.get("budget"):
            g["mismatched"] += 1  # a stored number that does not match the format

    def total(g: dict) -> int:
        return g["onprem"] + (0 if mixed else g["cloud"])

    rows = [{"budgetNumber": number, "onprem": g["onprem"] / 100, "cloud": g["cloud"] / 100,
             "total": total(g) / 100, "buckets": g["buckets"],
             "owners": sorted(g["owners"], key=str.lower), "mismatched": g["mismatched"]}
            for number, g in groups.items()]
    rows.sort(key=lambda r: (r["budgetNumber"] is None, -r["total"],
                             (r["budgetNumber"] or "").lower()))
    sums = {k: sum(g[k] for g in groups.values()) for k in ("onprem", "cloud", "buckets")}
    enabled = bool(cloud.get("enabled"))
    available = enabled and bool(cloud.get("available"))
    notes = []
    if not enabled:
        notes.append("Cloud billing is not set up (CLOUD_BILLING_BUCKET is empty), so cloud "
                     "buckets are not included.")
    elif not available:
        reason = cloud.get("reason") or "the billing export could not be read"
        notes.append(f"Cloud costs are unavailable ({reason}), so cloud amounts are 0.")
    else:
        if cloud.get("stale"):
            notes.append("Cloud costs are from an earlier read of the AWS billing export; "
                         "reading it again failed.")
        if not cloud.get("final"):
            notes.append(f"Cloud costs for {month} are not final yet: AWS finishes a month "
                         f"by the {cloud_costs.FINAL_DAY}th of the next one.")
        if cloud.get("unknownTotal"):
            notes.append(f"{_usd(cloud['unknownTotal'])} of cloud costs could not be assigned "
                         f"to a bucket owner or product and are not included (see the "
                         f"Problems sheet).")
    if mixed and available:
        notes.append(f"Cloud costs are in {EXPORT_CURRENCY} and are not added to the totals "
                     f"in {currency}.")
    cloud_done = not enabled or (available and bool(cloud.get("final")))
    return {
        "month": month, "currency": currency, "cloudCurrency": EXPORT_CURRENCY,
        "final": month < billing.month_key(today) and cloud_done,
        "cloudEnabled": enabled, "cloudAvailable": available,
        "cloudNote": " ".join(notes) or None,
        "rows": rows,
        "totals": {"onprem": sums["onprem"] / 100, "cloud": sums["cloud"] / 100,
                   "total": (sums["onprem"] + (0 if mixed else sums["cloud"])) / 100,
                   "buckets": sums["buckets"]},
    }


async def budget_report(app, month: str) -> dict[str, Any]:
    """`GET /api/admin/billing/budgets`: the month's chargeback per budget number."""
    c = await chargeback(app, month, emails=False)
    names = await owner_names(app, {_owner_uid(r) for r in c["rows"]})
    return budget_view(c, month, date.fromtimestamp(app.state.clock()), names)


def filename(month: str) -> str:
    return f"bucketeer-chargeback-{month}.xlsx"


def _lock(app) -> asyncio.Lock:
    lock = getattr(app.state, "billing_report_lock", None)
    if lock is None:
        lock = app.state.billing_report_lock = asyncio.Lock()
    return lock


async def send_report(app, month: str, *, actor: str, trigger: str) -> dict[str, Any]:
    """Build and mail one month's sheet to the billing contacts; audited either way."""
    db: Database = app.state.db
    async with _lock(app):
        settings = await db.run(billing.load, db)
        recipients = list(settings["contacts"])
        base = {"month": month, "trigger": trigger}
        result: dict[str, Any] = {"month": month, "sent": [], "failed": recipients,
                                  "rows": 0, "problems": 0}
        try:
            data, counts = await build(app, month)
        except Exception as e:  # noqa: BLE001  audited and retried; never kill the loop
            log.warning("billing report %s could not be built: %s", month, type(e).__name__)
            await db.run(db.audit.add, actor, "billing.report_failed", "report", "billing",
                         {**base, "recipients": recipients,
                          "reason": f"build_failed: {type(e).__name__}"})
            return {**result, "reason": "build_failed"}
        result.update(counts)
        s = app.state.mailer.s
        first, _, _ = month_bounds(month)
        label = first.strftime("%B %Y")
        body = (f"Attached is the storage chargeback sheet for {label} from {s.app_name}: "
                f"{counts['rows']} rows. ")
        body += (f"{counts['problems']} problems are listed on the Problems sheet.\n\n"
                 if counts["problems"] else "No problems found.\n\n")
        body += (f"The sheet can also be downloaded on the admin page, Billing tab:\n"
                 f"{(s.app_url or '').rstrip('/')}/\n")
        res = await app.state.mailer.send_with_attachment(
            recipients, f"{s.app_name} chargeback {label}", body, filename(month), data,
            XLSX_MIME, f"billing report {month}")
        if res is None:
            await db.run(db.audit.add, actor, "billing.report_failed", "report", "billing",
                         {**base, "recipients": recipients, "reason": "email_off"})
            return {**result, "reason": "email_off"}
        sent, failed = res
        result.update(sent=sent, failed=failed)
        if sent:
            await db.run(db.audit.add, actor, "billing.report_sent", "report", "billing",
                         {**base, "recipients": sent, "failed": failed, **counts})
        else:
            await db.run(db.audit.add, actor, "billing.report_failed", "report", "billing",
                         {**base, "recipients": recipients, "reason": "smtp"})
            result["reason"] = "smtp"
        return result


def send_settings(app, settings: dict) -> dict:
    """The billing settings as the scheduler uses them: with cloud billing set up, the
    send day is at least `cloud_costs.FINAL_DAY` (AWS finishes a month by then)."""
    if cloud_costs.billing_enabled(app) and settings["sendDay"] < cloud_costs.FINAL_DAY:
        return {**settings, "sendDay": cloud_costs.FINAL_DAY}
    return settings


def _last(result: dict, trigger: str, now: datetime) -> dict[str, Any]:
    return {"month": result["month"], "at": now.isoformat(timespec="seconds"),
            "trigger": trigger, "sent": result["sent"], "failed": result["failed"],
            "ok": bool(result["sent"])}


async def report_tick(app, now: datetime | None = None) -> dict[str, Any] | None:
    """One scheduler check; sends last month's sheet when it is due. Returns the result."""
    db: Database = app.state.db
    now = now or datetime.fromtimestamp(app.state.clock())
    settings = send_settings(app, await db.run(billing.load, db))
    state = await db.run(billing.load_state, db)
    month = billing.due_month(now, settings, state, RETRY_SECONDS)
    if month is None or not app.state.mailer.enabled:
        return None
    log.info("billing report %s is due; sending to %d contact(s)", month,
             len(settings["contacts"]))
    result = await send_report(app, month, actor="system", trigger="schedule")
    if result["sent"]:
        await db.run(billing.update_state, db, "system", lastSentMonth=month, failed=None,
                     last=_last(result, "schedule", now))
    else:
        await db.run(billing.update_state, db, "system",
                     failed={"month": month, "at": now.isoformat(timespec="seconds")},
                     last=_last(result, "schedule", now))
    return result


async def report_loop(app, sleep=asyncio.sleep) -> None:
    while True:
        try:
            await report_tick(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  try again at the next tick
            log.warning("billing report check failed: %s", type(e).__name__)
        await sleep(TICK_SECONDS)


router = APIRouter(prefix="/admin/billing", tags=["admin"],
                   dependencies=[Depends(require_admin)])
Db = Annotated[Database, Depends(get_db)]
Admin = Annotated[SessionUser, Depends(require_admin)]


def _check_month(request: Request, month: str) -> str:
    today = date.fromtimestamp(request.app.state.clock())
    if month > billing.month_key(today) or month < "2000-01":
        raise HTTPException(422, "Pick a month that has started.")
    return month


@router.get("/chargeback.xlsx")
async def chargeback_xlsx(request: Request, admin: Admin, db: Db,
                          month: Annotated[str, Query(pattern=MONTH_PATTERN)]):
    month = _check_month(request, month)
    data, counts = await build(request.app, month)
    await db.run(db.audit.add, admin.username, "admin.export.billing_xlsx", "report",
                 "billing", {"month": month, **counts})
    return Response(data, media_type=XLSX_MIME, headers={
        "Content-Disposition": f'attachment; filename="{filename(month)}"',
        "Cache-Control": "no-store"})


def _month_or_current(request: Request, month: str | None) -> str:
    return _check_month(request, month or billing.month_key(
        date.fromtimestamp(request.app.state.clock())))


@router.get("/budgets")
async def budgets(request: Request, response: Response,
                  month: Annotated[str | None, Query(pattern=MONTH_PATTERN)] = None):
    month = _month_or_current(request, month)
    response.headers["Cache-Control"] = "no-store"
    return await budget_report(request.app, month)


BUDGET_COLUMNS = ["budget_number", "buckets", "owners"]


@router.get("/budgets.csv")
async def budgets_csv(request: Request, admin: Admin, db: Db,
                      month: Annotated[str | None, Query(pattern=MONTH_PATTERN)] = None):
    """The budget view as CSV (UTF-8 with BOM, CRLF, `csv_safe` cells): one row per budget
    number (`(none)` for buckets without a usable one), then a `(total)` row."""
    from .admin import csv_safe

    month = _month_or_current(request, month)
    r = await budget_report(request.app, month)
    cur, ccur = r["currency"], r["cloudCurrency"]
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([*BUDGET_COLUMNS, f"onprem_{cur}", f"cloud_{ccur}", f"total_{cur}"])
    for row in r["rows"]:
        w.writerow([csv_safe(c) for c in (
            row["budgetNumber"] if row["budgetNumber"] is not None else "(none)",
            row["buckets"], "; ".join(row["owners"]), f"{row['onprem']:.2f}",
            f"{row['cloud']:.2f}", f"{row['total']:.2f}")])
    t = r["totals"]
    w.writerow(["(total)", t["buckets"], "", f"{t['onprem']:.2f}", f"{t['cloud']:.2f}",
                f"{t['total']:.2f}"])
    await db.run(db.audit.add, admin.username, "admin.export.billing_budgets", "report",
                 "billing", {"month": month, "rows": len(r["rows"]), "currency": cur,
                             "final": r["final"]})
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="bucketeer-budgets-{month}.csv"',
        "Cache-Control": "no-store"})


@router.get("/cloud")
async def cloud_summary(request: Request, db: Db,
                        month: Annotated[str, Query(pattern=MONTH_PATTERN)]):
    month = _check_month(request, month)
    pattern = (await db.run(load_budget_rules, db.settings))["pattern"]
    _, _, summary = await cloud_part(request.app, month, pattern)
    return summary


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: StrictStr


@router.post("/report/send")
async def send_now(request: Request, body: SendBody, admin: Admin, db: Db):
    import re

    if not re.match(MONTH_PATTERN, body.month):
        raise HTTPException(422, "The month is YYYY-MM.")
    month = _check_month(request, body.month)
    if not request.app.state.mailer.enabled:
        raise HTTPException(409, "Email is off (SMTP_FROM is empty).")
    settings = await db.run(billing.load, db)
    if not settings["contacts"]:
        raise HTTPException(409, "Add a billing contact first.")
    result = await send_report(request.app, month, actor=admin.username, trigger="manual")
    now = datetime.fromtimestamp(request.app.state.clock())
    await db.run(billing.update_state, db, admin.username, last=_last(result, "manual", now))
    if not result["sent"]:
        raise HTTPException(502, "The report could not be sent; the server log has the "
                                 "mail server's answer.")
    return result
