"""GET /api/admin/audit, /audit/facets, /audit/export.csv."""

import csv
import io
import json

import pytest

from .test_admin import Env

URL = "/api/admin/audit"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    with e.db.tx() as c:  # start without the two logins (raw SQL: the repo is append-only)
        c.execute("DELETE FROM audit_log")
    a = e.db.audit
    a.add("alice", "bucket.create", "bucket", "alice-data", {"budget": "IDX-1"})
    a.add("alice", "bucket.delete", "bucket", "alice-old", {})
    a.add("bob", "admin.user.quota", "rgw_user", "alice", {"before": 1, "after": 2})
    a.add("carol", "credentials.key.create", "access_key", "AKIA123", {})
    a.add("alice", "profile.update", "profile", "alice", {"old": None, "new": "X"})
    yield e
    e.db.close()


def sql(env, stmt, params=()):
    with env.db.tx() as c:
        c.execute(stmt, params)


def get(env, **params):
    r = env.admin.get(URL, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def actions(body):
    return [e["action"] for e in body["entries"]]


def test_access_control(env):
    for path in ("", "/facets", "/export.csv"):
        assert env.anon.get(URL + path).status_code == 401
        assert env.user.get(URL + path).status_code == 403
    assert env.audit("admin.export") == []


def test_shape_newest_first_and_reads_not_audited(env):
    before = env.db.audit.count()
    body = get(env)
    assert body["nextCursor"] is None
    ids = [e["id"] for e in body["entries"]]
    assert ids == sorted(ids, reverse=True) and len(ids) == 5
    assert set(body["entries"][0]) == {
        "id", "ts", "actor", "action", "targetType", "target", "details"}
    assert body["entries"][0]["action"] == "profile.update"
    get(env)
    env.admin.get(URL + "/facets")
    assert env.db.audit.count() == before


def test_filters(env):
    assert actions(get(env, actor="alice")) == ["profile.update", "bucket.delete", "bucket.create"]
    assert actions(get(env, action="bucket.create")) == ["bucket.create"]
    assert actions(get(env, action="bucket")) == []  # no dot: exact
    assert actions(get(env, target_type="access_key")) == ["credentials.key.create"]
    assert actions(get(env, target="alice-data")) == ["bucket.create"]
    assert actions(get(env, target="alice")) == ["profile.update", "admin.user.quota"]
    assert actions(get(env, target_contains="ALICE-")) == ["bucket.delete", "bucket.create"]
    assert actions(get(env, actor="alice", target_type="bucket")) == [
        "bucket.delete", "bucket.create"]
    assert get(env, actor="nobody")["entries"] == []


def test_action_prefix(env):
    assert actions(get(env, action="bucket.")) == ["bucket.delete", "bucket.create"]
    assert actions(get(env, action="admin.")) == ["admin.user.quota"]
    # LIKE wildcards are literal
    assert get(env, action="bucket_")["entries"] == []
    assert get(env, action="%.")["entries"] == []


def test_date_range(env):
    rows = env.db.audit.query(limit=10)  # newest first
    ids = [r["id"] for r in rows]
    stamps = ["2026-01-01T10:00:00.000000Z", "2026-02-01T10:00:00.000000Z",
              "2026-03-01T10:00:00.000000Z", "2026-04-01T10:00:00.000000Z",
              "2026-05-01T10:00:00.000000Z"]
    for i, ts in zip(sorted(ids), stamps, strict=True):
        sql(env, "UPDATE audit_log SET ts = ? WHERE id = ?", (ts, i))
    assert len(get(env, since="2026-03-01T10:00:00Z")["entries"]) == 3  # inclusive
    assert len(get(env, until="2026-03-01T10:00:00Z")["entries"]) == 2  # exclusive
    assert len(get(env, since="2026-02-01", until="2026-04-01")["entries"]) == 2
    # offsets are converted to UTC
    assert len(get(env, since="2026-03-01T12:00:00+02:00")["entries"]) == 3
    assert get(env, since="2027-01-01")["entries"] == []


def test_pagination_stable_under_inserts(env):
    first = get(env, limit=2)
    assert len(first["entries"]) == 2 and first["nextCursor"] == first["entries"][-1]["id"]
    for i in range(3):  # new rows arrive between pages
        env.db.audit.add("zed", "bucket.create", "bucket", f"new-{i}", {})
    second = get(env, limit=2, before_id=first["nextCursor"])
    third = get(env, limit=2, before_id=second["nextCursor"])
    seen = [e["id"] for p in (first, second, third) for e in p["entries"]]
    assert len(seen) == len(set(seen)) == 5  # no duplicates, nothing skipped, no new rows
    assert third["nextCursor"] is None and len(third["entries"]) == 1
    assert max(seen) < first["entries"][0]["id"] + 1
    assert get(env, limit=5)["nextCursor"] is not None  # newest-first includes new rows
    assert get(env, limit=8)["nextCursor"] is None


def test_limit_caps(env):
    for i in range(210):
        env.db.audit.add("bulk", "bucket.create", "bucket", f"b{i}", {})
    assert len(get(env)["entries"]) == 50
    big = get(env, limit=1000)
    assert len(big["entries"]) == 200 and big["nextCursor"] is not None
    assert len(get(env, limit=7)["entries"]) == 7


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": -1}, {"limit": "x"}, {"before_id": 0}, {"before_id": "x"},
    {"since": "yesterday"}, {"until": "2026-13-45"}, {"since": "2026-02-01", "until": "2026-01-01"},
    {"since": "0001-01-01T00:00:00+01:00"}, {"until": "9999-12-31T23:59:59-01:00"},
    {"since": "1969-12-31T23:59:59Z"}, {"since": "1900-01-01"},
    {"actor": "a" * 201}, {"action": "a" * 201}, {"target": "t" * 201},
    {"target_type": "t" * 201}, {"target_contains": "t" * 201}, {"since": "2" * 41},
])
def test_invalid_inputs_422(env, params):
    assert env.admin.get(URL, params=params).status_code == 422
    if not set(params) & {"limit", "before_id"}:  # the export has no paging
        assert env.admin.get(URL + "/export.csv", params=params).status_code == 422
    assert env.audit("admin.export") == []


def test_rescrub_on_read(env):
    # bypass the write scrubber with raw SQL
    sql(env, 
        "INSERT INTO audit_log (ts, actor, action, target_type, target, details) "
        "VALUES (?, 'x', 'evil.raw', 'bucket', 'b', ?)",
        ("2026-01-01T00:00:00.000000Z", json.dumps({
            "secret_key": "TOPSECRET", "nested": {"api_token": "TOPSECRET2"},
            "url": "https://s3/x?X-Amz-Signature=abc", "ok": "fine"})))
    body = get(env, action="evil.raw")
    assert "TOPSECRET" not in json.dumps(body) and "Signature=abc" not in json.dumps(body)
    d = body["entries"][0]["details"]
    assert d["ok"] == "fine" and d["secret_key"] == "[redacted]" and d["url"] == "[redacted]"
    sql(env, "UPDATE audit_log SET details = 'not json' WHERE action = 'evil.raw'")
    assert get(env, action="evil.raw")["entries"][0]["details"] == {}
    csv_text = env.admin.get(URL + "/export.csv", params={"action": "evil.raw"}).text
    assert "TOPSECRET" not in csv_text


def test_facets(env):
    body = env.admin.get(URL + "/facets").json()
    assert body["actions"] == sorted({
        "bucket.create", "bucket.delete", "admin.user.quota", "credentials.key.create",
        "profile.update"})
    assert body["targetTypes"] == ["access_key", "bucket", "profile", "rgw_user"]


def rows_of(r):
    assert r.text.startswith("﻿")
    return list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))


def test_csv_export(env):
    env.db.audit.add("mallory", "bucket.create", "bucket", "=HYPERLINK(1)", {"label": "+x"})
    r = env.admin.get(URL + "/export.csv", params={"actor": "mallory"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].startswith(
        'attachment; filename="bucketeer-audit-')
    assert r.text.splitlines()[0].lstrip("﻿") == "id,time,actor,action,target_type,target,details"
    rows = rows_of(r)
    assert len(rows) == 1
    assert rows[0]["target"] == "'=HYPERLINK(1)"  # csv_safe
    assert json.loads(rows[0]["details"]) == {"label": "+x"}
    (entry,) = env.audit("admin.export")
    assert (entry["action"], entry["actor"], entry["target_type"], entry["target"]) == (
        "admin.export.audit", "bob", "report", "audit")
    assert entry["details"]["rows"] == 1
    assert entry["details"]["filters"] == {"actor": "mallory"}
    # unfiltered export lists all rows incl. the earlier export entry, newest first
    all_rows = rows_of(env.admin.get(URL + "/export.csv"))
    assert len(all_rows) == 7  # 5 seeded + mallory + first export


def test_csv_cap(env, monkeypatch):
    import bucketeer.audit_viewer as av
    monkeypatch.setattr(av, "EXPORT_MAX_ROWS", 3)
    r = env.admin.get(URL + "/export.csv")
    assert len(rows_of(r)) == 3
    (entry,) = env.audit("admin.export")
    assert entry["details"]["truncated"] is True
