"""The storage growth forecast (forecast.py): pure function and the one-query helper."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from bucketeer import forecast
from bucketeer.db import Database

TODAY = date(2026, 11, 28)
GIB = 2**30


def series(days, start=0, step=GIB, end=TODAY, skip=()):
    """`days` daily readings ending at `end`, growing by `step` a day from `start`."""
    out = []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        if i not in skip:
            out.append((d, start + i * step))
    return out


def test_steady_growth():
    # 10 GiB, 11 GiB, ... 39 GiB over 30 days; quota 100 GiB: 61 GiB to go at 1 GiB a day
    f = forecast.forecast(series(30, start=10 * GIB), 100 * GIB, TODAY)
    assert f == {"growthBytesPerDay": GIB, "daysToFull": 61,
                 "fullOn": (TODAY + timedelta(days=61)).isoformat(), "basedOnDays": 30}


def test_only_the_last_30_days_count():
    # 60 days: fast growth long ago, then flat for the last 30
    old = series(30, step=10 * GIB, end=TODAY - timedelta(days=30))
    recent = [(d, 300 * GIB) for d, _ in series(30)]
    f = forecast.forecast(old + recent, 400 * GIB, TODAY)
    assert f["growthBytesPerDay"] == 0 and f["daysToFull"] is None and f["basedOnDays"] == 30
    future = [(TODAY + timedelta(days=3), 999 * GIB)]  # never trusted
    assert forecast.forecast(recent + future, 400 * GIB, TODAY)["growthBytesPerDay"] == 0


def test_gaps_use_the_real_days():
    # one reading a week for a month plus a few daily ones: the slope is still 1 GiB a day
    readings = [r for i, r in enumerate(series(29, start=GIB)) if i % 7 == 0 or i > 24]
    assert len(readings) == 8
    f = forecast.forecast(readings, 100 * GIB, TODAY)
    assert f["growthBytesPerDay"] == GIB and f["basedOnDays"] == 8
    # the projection starts from the latest reading: 29 GiB, 71 GiB to go
    assert f["daysToFull"] == 71
    # the latest reading three days ago: the date counts from it, the days from today
    stale = forecast.forecast(series(20, end=TODAY - timedelta(days=3)), 100 * GIB, TODAY)
    assert stale["fullOn"] == (TODAY - timedelta(days=3) + timedelta(days=81)).isoformat()
    assert stale["daysToFull"] == 78


def test_shrinking_and_flat_buckets_never_fill():
    shrink = forecast.forecast(series(14, start=50 * GIB, step=-GIB), 60 * GIB, TODAY)
    assert shrink["growthBytesPerDay"] == -GIB
    assert shrink["daysToFull"] is None and shrink["fullOn"] is None
    flat = forecast.forecast(series(10, start=5 * GIB, step=0), 60 * GIB, TODAY)
    assert flat["growthBytesPerDay"] == 0 and flat["daysToFull"] is None


def test_noisy_growth_is_fitted():
    readings = [(d, size + (GIB if i % 2 else -GIB)) for i, (d, size)
                in enumerate(series(20, start=100 * GIB, step=2 * GIB))]
    f = forecast.forecast(readings, 200 * GIB, TODAY)
    assert 1.9 * GIB < f["growthBytesPerDay"] < 2.1 * GIB and f["daysToFull"] > 0


@pytest.mark.parametrize("quota", [None, 0, -1])
def test_no_quota_no_date(quota):
    f = forecast.forecast(series(10), quota, TODAY)
    assert f["growthBytesPerDay"] == GIB
    assert f["daysToFull"] is None and f["fullOn"] is None


def test_over_quota_is_full_now():
    f = forecast.forecast(series(10, start=50 * GIB), 40 * GIB, TODAY)
    assert f["daysToFull"] == 0 and f["fullOn"] is None
    # exactly at the quota counts as full, flat or shrinking
    at = forecast.forecast(series(8, start=10 * GIB, step=0), 10 * GIB, TODAY)
    assert at["daysToFull"] == 0 and at["growthBytesPerDay"] == 0
    shrinking = forecast.forecast(series(8, start=50 * GIB, step=-GIB), 40 * GIB, TODAY)
    assert shrinking["daysToFull"] == 0
    # just below and shrinking: never full
    below = forecast.forecast(series(8, start=40 * GIB, step=-GIB), 40 * GIB, TODAY)
    assert below["daysToFull"] is None


def test_too_few_readings():
    assert forecast.forecast([], GIB, TODAY) is None
    assert forecast.forecast(series(6), 100 * GIB, TODAY) is None  # six days
    # seven readings, but squeezed into fewer than seven days is impossible (one a day);
    # seven days with a gap in the middle are not enough readings either
    assert forecast.forecast(series(8, skip=(3,)), 100 * GIB, TODAY)["basedOnDays"] == 7
    assert forecast.forecast(series(7, skip=(3,)), 100 * GIB, TODAY) is None
    # a later reading of the same day replaces the earlier one
    doubled = series(6) + [(TODAY, 0)]
    assert forecast.forecast(doubled, 100 * GIB, TODAY) is None


def test_far_away_dates_are_left_out():
    f = forecast.forecast(series(10, step=1), 2**60, TODAY)  # a byte a day
    assert f["growthBytesPerDay"] == 1 and f["daysToFull"] is None and f["fullOn"] is None


def test_for_buckets_uses_one_query():
    db = Database(":memory:")
    for d, size in series(10):
        db.usage.record(d.isoformat(), [("a", "alice", size, None), ("acme:b", "acme$c", 5, 1),
                                        ("other", "bob", 1, None)])
    db.usage.record((TODAY - timedelta(days=40)).isoformat(), [("a", "alice", 0, None)])
    queries = []
    real = db.usage.readings

    def spy(*a):
        queries.append(a)
        return real(*a)

    db.usage.readings = spy
    app = SimpleNamespace(state=SimpleNamespace(
        db=db, clock=lambda: datetime(2026, 11, 28, 9).timestamp()))
    got = asyncio.run(forecast.for_buckets(app, {"a": 100 * GIB, "acme:b": 3, "nope": None}))
    assert len(queries) == 1 and queries[0][2] == ["a", "acme:b", "nope"]
    assert got["a"]["daysToFull"] == 91 and got["acme:b"]["daysToFull"] == 0
    assert got["nope"] is None
    every = asyncio.run(forecast.for_buckets(app, {"a": None}, all_buckets=True))
    assert queries[-1][2] is None and every["a"]["growthBytesPerDay"] == GIB
    assert asyncio.run(forecast.for_buckets(app, {})) == {}
    db.close()
