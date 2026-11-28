"""Storage growth forecast: when will a bucket reach its quota? (admin and user bucket lists)

`forecast(readings, quota_bytes, today)` is pure. It takes the daily size readings of ONE
bucket (`usage_daily`, written by `billing.sample`: one row per day and bucket, the last
reading of a day wins), keeps the last `WINDOW_DAYS` (30) calendar days up to `today`, and
fits a least-squares line through them (x = the day, so gaps simply count as missing days).
It needs at least `MIN_READINGS` (7) readings spread over at least `MIN_DAYS` (7) calendar
days, else it returns None. Otherwise:

    {growthBytesPerDay: int (the slope, negative when shrinking),
     daysToFull: int | None, fullOn: "YYYY-MM-DD" | None, basedOnDays: int (readings used)}

The fill date projects from the LATEST reading (the size the lists show) along the slope:
`fullOn` = that day plus ceil((quota - size) / slope) days, `daysToFull` = days from `today`
to it. A bucket already at or over its quota has `daysToFull` 0 and `fullOn` None; without
a quota, with a slope <= 0 (flat or shrinking), or with a fill date more than
`HORIZON_DAYS` (10 years) away, both are None.

`for_buckets(app, quotas)` answers {name: forecast | None} for tenant-qualified bucket names
from ONE query over `usage_daily` (`GET /api/admin/buckets` and `GET /api/buckets` add it to
each row as `forecast`).
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any

log = logging.getLogger("bucketeer.forecast")

WINDOW_DAYS = 30
MIN_READINGS = 7
MIN_DAYS = 7
HORIZON_DAYS = 3650


def forecast(readings: list[tuple[date, int]], quota_bytes: int | None,
             today: date) -> dict[str, Any] | None:
    first = today - timedelta(days=WINDOW_DAYS - 1)
    by_day: dict[date, int] = {}
    for day, size in readings:  # one reading per day; a later one of the same day wins
        if first <= day <= today:
            by_day[day] = max(int(size), 0)
    days = sorted(by_day)
    if len(days) < MIN_READINGS or (days[-1] - days[0]).days + 1 < MIN_DAYS:
        return None
    xs = [(d - days[0]).days for d in days]
    ys = [by_day[d] for d in days]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    out: dict[str, Any] = {"growthBytesPerDay": round(slope), "daysToFull": None,
                           "fullOn": None, "basedOnDays": len(days)}
    if not quota_bytes or quota_bytes <= 0:
        return out
    last, size = days[-1], ys[-1]
    if size >= quota_bytes:
        out["daysToFull"] = 0
        return out
    if slope <= 0:
        return out
    ahead = math.ceil((quota_bytes - size) / slope - 1e-6)  # float noise is no extra day
    if ahead > HORIZON_DAYS:
        return out
    full_on = last + timedelta(days=ahead)
    out["daysToFull"] = max((full_on - today).days, 0)
    out["fullOn"] = full_on.isoformat()
    return out


def window(today: date) -> tuple[str, str]:
    """The first and last day (ISO) of readings a forecast for `today` looks at."""
    return (today - timedelta(days=WINDOW_DAYS - 1)).isoformat(), today.isoformat()


def from_rows(rows: list[dict], quotas: dict[str, int | None],
              today: date) -> dict[str, dict[str, Any] | None]:
    """{name: forecast | None} for every name in `quotas`, from `usage_daily` rows
    (`day`, `bucket`, `bytes`) of any number of buckets."""
    per: dict[str, list[tuple[date, int]]] = {}
    for r in rows:
        if r["bucket"] in quotas:
            per.setdefault(r["bucket"], []).append((date.fromisoformat(r["day"]),
                                                    int(r["bytes"] or 0)))
    return {name: forecast(per[name], quota, today) if name in per else None
            for name, quota in quotas.items()}


async def for_buckets(app, quotas: dict[str, int | None], *, all_buckets: bool = False
                      ) -> dict[str, dict[str, Any] | None]:
    """{name: forecast | None} for the given tenant-qualified names, from ONE query over
    `usage_daily` (`all_buckets`: the query is not narrowed by name, for the admin list).
    Never raises: a database problem only costs the forecast."""
    if not quotas:
        return {}
    db = app.state.db
    today = date.fromtimestamp(app.state.clock())
    first, last = window(today)
    try:
        rows = await db.run(db.usage.readings, first, last,
                            None if all_buckets else sorted(quotas))
    except Exception:  # noqa: BLE001  the lists work without a forecast
        log.exception("usage readings unreadable; no forecast")
        return dict.fromkeys(quotas)
    return from_rows(rows, quotas, today)
