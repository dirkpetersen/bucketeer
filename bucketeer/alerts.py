"""Alert emails for the administrators: trouble that needs a person (backup safety).

Admin setting `alerts` `{contacts: [0..10 addresses]}` (validated like the billing contacts,
`billing.normalize_contacts`). `send_alert(app, kind, subject, body)` mails every contact
through `app.state.mailer.send_admin_alert` (one plain-text message each, subject prefixed
with `[APP_NAME]`). It never raises and never carries a secret: callers pass texts already fit
for the admin page (backup errors from `backup.describe`, offboarding reasons). With email off
(`SMTP_FROM` empty) or no contacts it only logs a warning, and nothing is audited.

Kinds (the audit target):

* `backup_failed`: a backup run ended `failed` (each failed destination and its error);
* `restore_test_failed`: a restore test found an archive that can't be restored;
* `backup_stale`: no backup succeeded for more than 48 hours while backups are on (the backup
  schedule loop checks it, at most one email per 24 hours, settings key `backup_stale_alert`);
* `offboarding_aborted`, `offboarding_failed`: an offboarding run ended that way (its reason).

Audit (actor `system`, target_type `alert`, target the kind): `admin.alert_sent` (subject,
`sent` and `failed` addresses) when at least one contact's mail server took it, else
`admin.alert_failed`. The last alert (emailed or not) is kept in the settings key
`alert_state` (`last: {kind, subject, at, emailed, reason, sent, failed}`) for the Settings card.

API (`/api/admin/alerts`, admin, CSRF via the parent router):

* `GET` -> `{contacts, maxContacts, email: {enabled}, kinds: [{kind, text}], last | null}`.
* `PUT {contacts}` -> the same; 422 for an address that is not one or more than 10; audit
  `admin.settings.alerts` (target_type `setting`, target `alerts`, before/after) when changed.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, StrictStr

from .auth import SessionUser, require_admin
from .billing import MAX_CONTACTS, normalize_contacts
from .db import Database, get_db

log = logging.getLogger("bucketeer.alerts")

KEY = "alerts"
STATE_KEY = "alert_state"
KINDS = {
    "backup_failed": "A backup run failed in at least one place.",
    "restore_test_failed": "A restore test found a backup that can't be restored.",
    "backup_stale": "No backup succeeded for more than 48 hours (at most one email a day).",
    "offboarding_aborted": "An offboarding run was aborted (AD looked wrong, nobody suspended).",
    "offboarding_failed": "An offboarding run failed.",
}


def load(db: Database) -> dict[str, Any]:
    saved = db.settings.get(KEY) or {}
    return {"contacts": [c for c in saved.get("contacts") or [] if isinstance(c, str)]}


def _remember(db: Database, entry: dict[str, Any]) -> None:
    with db.tx():
        state = dict(db.settings.get(STATE_KEY) or {})
        state["last"] = entry
        db.settings.set(STATE_KEY, state, "system")


def _footer(app) -> str:
    s = app.state.settings
    where = f"{s.app_url.rstrip('/')}/#/admin" if s.app_url else "the administration page"
    return (f"\n\n--\nSent by {s.app_name} to its alert contacts. Change who gets these on the "
            f"Settings tab of {where}.\n")


async def send_alert(app, kind: str, subject: str, body: str) -> dict[str, Any] | None:
    """Mail `subject`/`body` to every alert contact. Returns the remembered entry, or None
    when nothing could be recorded. Never raises."""
    try:
        return await _send(app, kind, subject, body)
    except Exception:  # noqa: BLE001  an alert must never break the job that raised it
        log.exception("admin alert %s could not be sent", kind)
        return None


async def _send(app, kind: str, subject: str, body: str) -> dict[str, Any]:
    db, mailer = app.state.db, app.state.mailer
    contacts = (await db.run(load, db))["contacts"]
    entry: dict[str, Any] = {"kind": kind, "subject": subject,
                             "at": datetime.now(UTC).isoformat(timespec="seconds"),
                             "emailed": False, "reason": None, "sent": [], "failed": []}
    result = None
    if not mailer.enabled:
        entry["reason"] = "email_off"
    elif not contacts:
        entry["reason"] = "no_contacts"
    else:
        result = await mailer.send_admin_alert(
            contacts, f"[{app.state.settings.app_name}] {subject}", body.rstrip() + _footer(app))
        if result is None:  # email switched off meanwhile
            entry["reason"] = "email_off"
    if result is None:
        log.warning("admin alert %s not emailed (%s): %s", kind,
                    "email is off, SMTP_FROM is empty" if entry["reason"] == "email_off"
                    else "no alert contacts are set", subject)
        await db.run(_remember, db, entry)
        return entry
    sent, failed = result
    entry.update(emailed=bool(sent), reason=None if sent else "send_failed", sent=sent,
                 failed=failed)
    await db.run(_remember, db, entry)
    await db.run(db.audit.add, "system", "admin.alert_sent" if sent else "admin.alert_failed",
                 "alert", kind, {"subject": subject, "sent": sent, "failed": failed})
    if not sent:
        log.warning("admin alert %s reached no contact: %s", kind, subject)
    return entry


# -- API --------------------------------------------------------------------------------------

router = APIRouter(prefix="/admin/alerts", tags=["admin"], dependencies=[Depends(require_admin)])
Db = Annotated[Database, Depends(get_db)]
Admin = Annotated[SessionUser, Depends(require_admin)]


async def _view(request: Request, db: Database) -> dict[str, Any]:
    cfg = await db.run(load, db)
    state = await db.run(db.settings.get, STATE_KEY) or {}
    return {**cfg, "maxContacts": MAX_CONTACTS,
            "email": {"enabled": request.app.state.mailer.enabled},
            "kinds": [{"kind": k, "text": t} for k, t in KINDS.items()],
            "last": state.get("last")}


@router.get("")
async def get_alerts(request: Request, db: Db):
    return await _view(request, db)


class AlertsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contacts: list[StrictStr]


@router.put("")
async def put_alerts(request: Request, body: AlertsBody, admin: Admin, db: Db):
    before = await db.run(load, db)
    after = {"contacts": normalize_contacts(body.contacts, "alert contacts")}
    if after != before:
        await db.run(db.settings.set, KEY, after, admin.username)
        await db.run(db.audit.add, admin.username, "admin.settings.alerts", "setting", KEY,
                     {"before": before, "after": after})
    return await _view(request, db)
