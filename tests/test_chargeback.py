"""GET /api/admin/export/chargeback.csv against the mock croit."""

import csv
import io
import re

import pytest

from .test_admin import Env, H

URL = "/api/admin/export/chargeback.csv"
S = "/api/admin/settings/budget-number"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


def rows_of(r):
    assert r.text.startswith("﻿")
    return list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))


def test_access_control(env):
    assert env.anon.get(URL).status_code == 401
    assert env.user.get(URL).status_code == 403
    assert env.audit("admin.export") == []


def test_headers(env):
    r = env.admin.get(URL)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    cd = r.headers["content-disposition"]
    assert re.fullmatch(r'attachment; filename="bucketeer-chargeback-\d{4}-\d\d-\d\d\.csv"', cd)


def test_columns_and_rows_match_admin_buckets(env):
    env.db.buckets.create("alice-data", "alice", "IDX-1234")
    env.admin.put("/api/admin/buckets/alice-data/quota", headers=H,
                  json={"enabled": True, "maxSize": 5000, "maxObjects": None})
    r = env.admin.get(URL)
    assert r.text.splitlines()[0].lstrip("﻿") == (
        "bucket,tenant,owner_uid,owner_name,budget_number,budget_status,objects,bytes,"
        "size_gib,quota_bytes,created_at")
    rows = rows_of(r)
    api = env.admin.get("/api/admin/buckets").json()["buckets"]
    assert [x["bucket"] for x in rows] == [b["name"] for b in api]  # same order, one each
    by = {x["bucket"]: x for x in rows}
    for b in api:
        x = by[b["name"]]
        assert (x["tenant"], x["owner_uid"]) == (b["tenant"], b["owner"])
        assert int(x["objects"]) == b["objects"] and int(x["bytes"]) == b["bytes"]
        assert x["size_gib"] == f"{b['bytes'] / 1024**3:.3f}"
    a = by["alice-data"]
    assert (a["owner_name"], a["budget_number"], a["quota_bytes"]) == (
        "Alice Anderson", "IDX-1234", "5000")
    assert a["created_at"]  # from the buckets table
    assert by["alice-empty"]["budget_number"] == "" and by["alice-empty"]["quota_bytes"] == ""
    assert by["alice-empty"]["created_at"] == ""  # not created through the portal
    assert by["acme:reports"]["tenant"] == "acme"
    assert by["acme:reports"]["owner_uid"] == "acme$carol"
    assert by["acme:reports"]["owner_name"]
    assert "secret" not in r.text.lower()


def test_budget_status(env):
    env.db.buckets.create("alice-data", "alice", "IDX-1234")
    env.db.buckets.create("bob-shared", "bob", "wrong")
    st = {x["bucket"]: x["budget_status"] for x in rows_of(env.admin.get(URL))}
    assert st["alice-data"] == "ok"  # no pattern set: any number is ok
    assert st["alice-empty"] == "missing"
    assert env.admin.put(S, json={"pattern": r"IDX-\d{4}"}, headers=H).status_code == 200
    st = {x["bucket"]: x["budget_status"] for x in rows_of(env.admin.get(URL))}
    assert st["alice-data"] == "ok"
    assert st["bob-shared"] == "mismatch"
    assert st["alice-empty"] == "missing"


def test_csv_injection_is_neutralized(env):
    env.db.buckets.create("alice-data", "alice", "=1+2")
    env.db.buckets.create("alice-empty", "alice", "+A")
    env.db.buckets.create("bob-shared", "bob", "-1")
    env.db.buckets.create("acme:reports", "acme$carol", "@x")
    by = {x["bucket"]: x["budget_number"] for x in rows_of(env.admin.get(URL))}
    assert by == {"alice-data": "'=1+2", "alice-empty": "'+A", "bob-shared": "'-1",
                  "acme:reports": "'@x"}


def test_csv_safe_unit():
    from bucketeer.admin import csv_safe

    assert [csv_safe(v) for v in ("=a", "+a", "-a", "@a", "\ta", "\ra", "a=", "", None, 7)] == [
        "'=a", "'+a", "'-a", "'@a", "'\ta", "'\ra", "a=", "", "", "7"]


def test_quoting_of_commas_and_quotes(env):
    env.db.buckets.create("alice-data", "alice", 'A,"B"')
    by = {x["bucket"]: x for x in rows_of(env.admin.get(URL))}
    assert by["alice-data"]["budget_number"] == 'A,"B"'


def test_export_is_audited(env):
    assert env.audit("admin.export") == []
    n = len(env.admin.get("/api/admin/buckets").json()["buckets"])
    assert env.admin.get(URL).status_code == 200
    (row,) = env.audit("admin.export")
    assert row["action"] == "admin.export.chargeback" and row["actor"] == "bob"
    assert row["target_type"] == "report"
    assert row["details"]["rows"] == n


def test_timeout_gives_unknown(env, monkeypatch):
    env.db.buckets.create("alice-data", "alice", "IDX-1234")
    env.admin.put(S, json={"pattern": r"IDX-\d{4}"}, headers=H)

    def boom(value, pattern):
        raise TimeoutError

    monkeypatch.setattr("bucketeer.admin.budget_matches", boom)
    st = {x["bucket"]: x["budget_status"] for x in rows_of(env.admin.get(URL))}
    assert st["alice-data"] == "unknown" and st["alice-empty"] == "missing"
