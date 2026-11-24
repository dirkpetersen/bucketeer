"""Monthly chargeback sheet (.xlsx) and the monthly billing email (PLAN 3; CLAUDE.md "Billing").

Sheet 1 "Chargeback": one row per bucket with readings in the month, exactly the columns
`COLUMNS` (A-J). On-premises buckets are product `tier3` (its catalog name, default
`Tier-3-S3`); Quantity = the month's daily average QUOTA in TiB (2^40), rounded up: the sum
of each day's quota over the days of the month, a day without a reading counts 0. A day
without a size quota (unlimited) counts the measured size instead, and the bucket is listed
on the Problems sheet. Cloud rows (P6-11) are other products from the same catalog;
`_lines` is where they join. E/F are real Excel dates (`m/d/yyyy`), G and I the
tenant-qualified bucket name, H and J empty. A = the owner's AD `mail` (AD names of the uid
from `known_accounts` and `rgw_user.ad_names_for_uid`, looked up without a password; else
the RGW user's email; else the uid, listed on Problems). B = the budget number, empty when
missing or not matching the admin pattern (listed on Problems). Every text cell goes
through `admin.csv_safe` (formula injection works in xlsx too).

* `GET /api/admin/billing/chargeback.xlsx?month=YYYY-MM` (not a future month) -> the file,
  attachment `bucketeer-chargeback-YYYY-MM.xlsx`, `no-store`; audit
  `admin.export.billing_xlsx` (target_type `report`, details month, rows, problems).
* `POST /api/admin/billing/report/send {month}` mails that month's sheet to the billing
  contacts now (for testing; does not count as the scheduled send) -> `{month, sent,
  failed, rows, problems}`; 409 while email is off or no contact is set, 502 when no
  address took it.
* `report_loop` (lifespan task, `billing.REPORT_ON_START`) checks every minute: on the send
  day at 06:00 server local time (or at the first tick after it, so a missed day is caught
  up at the next start) it mails LAST month's sheet, then records `lastSentMonth` in the
  settings key `billing_report`; a failed try is retried hourly.
* Audit `billing.report_sent` / `billing.report_failed` (target_type `report`, target
  `billing`; details month, recipients, failed, trigger, reason; never the content);
  actor `system` for the schedule, else the admin.
"""

import asyncio
import calendar
import io
import logging
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictStr

from . import billing
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
               emails: dict[str, str | None], settings: dict) -> tuple[list[dict], list[dict]]:
    """Sheet rows and Problems rows. `budgets[bucket] = (number, usable)`."""
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
        email = emails.get(owner)
        if not email:
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
        })
    rows.sort(key=lambda r: (r["email"].lower(), r["note"]))
    return rows, problems


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


async def owner_emails(app, uids: set[str]) -> dict[str, str | None]:
    """uid -> the owner's email: AD `mail` of an AD name linked to the uid (no password
    bind), else the RGW user's email, else None. Lookup trouble only costs the address."""
    db = app.state.db
    try:
        rgw = {str(u.get("uid")): (u.get("email") or "").strip()
               for u in await app.state.croit.list_users()}
    except Exception as e:  # noqa: BLE001  the AD address is the main source anyway
        log.warning("chargeback: RGW user list unreadable (%s)", type(e).__name__)
        rgw = {}
    tenant = getattr(app.state.rgw, "tenant", "")
    out: dict[str, str | None] = {}
    for uid in sorted(uids):
        names = [r["ad_username"] for r in await db.run(db.known_accounts.for_uid, uid)]
        names += [n for n in await db.run(ad_names_for_uid, db, uid, tenant) if n not in names]
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


async def build(app, month: str) -> tuple[bytes, dict[str, int]]:
    """The month's workbook plus `{rows, problems}`."""
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
    emails = await owner_emails(app, {line["owner"] for line in lines})
    rows, problems = build_rows(lines, month, budgets, emails, settings)
    data = await asyncio.to_thread(to_xlsx, rows, problems)
    return data, {"rows": len(rows), "problems": len(problems)}


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


def _last(result: dict, trigger: str, now: datetime) -> dict[str, Any]:
    return {"month": result["month"], "at": now.isoformat(timespec="seconds"),
            "trigger": trigger, "sent": result["sent"], "failed": result["failed"],
            "ok": bool(result["sent"])}


async def report_tick(app, now: datetime | None = None) -> dict[str, Any] | None:
    """One scheduler check; sends last month's sheet when it is due. Returns the result."""
    db: Database = app.state.db
    now = now or datetime.fromtimestamp(app.state.clock())
    settings = await db.run(billing.load, db)
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
