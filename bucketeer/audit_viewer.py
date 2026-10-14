"""Admin audit log viewer: GET /api/admin/audit, /audit/facets, /audit/export.csv.

Reading is not audited (it would spam the log); only the CSV export is, as
`admin.export.audit`. Rows are newest first, paged by an id cursor so inserts
during paging never shift a page. `details` were scrubbed at write time and are
scrubbed again on read."""

import csv
import io
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from .admin import csv_safe
from .auth import SessionUser, require_admin
from .db import Database, get_db

router = APIRouter(prefix="/admin/audit", tags=["admin"], dependencies=[Depends(require_admin)])

Admin = Annotated[SessionUser, Depends(require_admin)]
Db = Annotated[Database, Depends(get_db)]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
EXPORT_MAX_ROWS = 50_000
EXPORT_COLUMNS = ["id", "time", "actor", "action", "target_type", "target", "details"]


def _iso(name: str, value: str | None) -> str | None:
    """ISO 8601 (naive = UTC) -> the stored timestamp format, else 422."""
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        if dt.year < 1970:
            raise ValueError("before 1970")
    except (ValueError, OverflowError):
        raise HTTPException(
            422, f"'{name}' must be an ISO 8601 date or time (1970 or later)."
        ) from None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_Str = Annotated[str | None, Query(max_length=200)]


def _filters(
    actor: _Str = None,
    action: _Str = None,
    target_type: _Str = None,
    target: _Str = None,
    target_contains: _Str = None,
    since: Annotated[str | None, Query(max_length=40)] = None,
    until: Annotated[str | None, Query(max_length=40)] = None,
) -> dict[str, Any]:
    f: dict[str, Any] = {
        "actor": actor or None,
        "action": action or None,
        "target_type": target_type or None,
        "target": target or None,
        "target_contains": target_contains or None,
        "since": _iso("since", since),
        "until": _iso("until", until),
    }
    if f["since"] and f["until"] and f["since"] >= f["until"]:
        raise HTTPException(422, "'since' must be before 'until'.")
    return f


Filters = Annotated[dict[str, Any], Depends(_filters)]


def _entry(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
        "targetType": r["target_type"], "target": r["target"], "details": r["details"],
    }


@router.get("")
async def list_audit(
    db: Db,
    f: Filters,
    limit: Annotated[int, Query(ge=1)] = DEFAULT_LIMIT,
    before_id: Annotated[int | None, Query(ge=1, le=2**62)] = None,
):
    rows, nxt = await db.run(
        lambda: db.audit.search(**f, before_id=before_id, limit=min(limit, MAX_LIMIT)))
    return {"entries": [_entry(r) for r in rows], "nextCursor": nxt}


@router.get("/facets")
async def facets(db: Db):
    return await db.run(db.audit.facets)


@router.get("/export.csv")
async def export_csv(admin: Admin, db: Db, f: Filters):
    """The filtered view, newest first, at most EXPORT_MAX_ROWS rows."""
    rows, nxt = await db.run(lambda: db.audit.search(**f, limit=EXPORT_MAX_ROWS))
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(EXPORT_COLUMNS)
    for r in rows:
        w.writerow([csv_safe(c) for c in (
            r["id"], r["ts"], r["actor"], r["action"], r["target_type"], r["target"],
            json.dumps(r["details"], sort_keys=True, default=str),
        )])
    shown = {k: v for k, v in f.items() if v is not None}
    await db.run(db.audit.add, admin.username, "admin.export.audit", "report", "audit",
                 {"rows": len(rows), "truncated": nxt is not None, "filters": shown})
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="bucketeer-audit-{day}.csv"',
            "Cache-Control": "no-store",
        },
    )
