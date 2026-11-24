"""Rebuild SQLite rows from Ceph (`bucketeer/rebuild.py`, P6-3) and the budget-edit fix for
buckets older than the portal. Against the mock croit / Admin Ops mock and moto, on both
admin backends."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bucketeer import rebuild

from .backends import admin_backend  # noqa: F401  (fixture: both admin backends)
from .test_auth import USERS_GROUP
from .test_bucket_keys import mk
from .test_buckets import Env, H, create

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    e.fake.add("erin", groups={USERS_GROUP}, display="Erin E", mail="erin@example.org")
    yield e
    e.db.close()


def share(client, login, access, bucket="alice-new"):
    r = client.post(f"/api/buckets/{bucket}/shares", json={"login": login, "access": access},
                    headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def populate(env):
    """A bucket made through the portal, two keys, two shares."""
    assert create(env.alice).status_code == 201
    rw = mk(env.alice, bucket="alice-new", label="backup", access="readwrite")
    ro = mk(env.alice, bucket="alice-data", label="reader", access="read")
    assert rw.status_code == 201 and ro.status_code == 201, (rw.text, ro.text)
    share(env.alice, "bob", "read")
    share(env.alice, "erin", "readwrite")
    return rw.json(), ro.json()


def wipe(env, *tables):
    with env.db.tx() as c:
        for t in tables or rebuild.TABLES:
            c.execute(f"DELETE FROM {t}")


def run(env, fill_gaps=False, **kw):
    return asyncio.run(rebuild.rebuild_from_ceph(env.app, fill_gaps, **kw))


BUCKET_COLS = ("name", "owner_uid", "budget_number", "created_by", "creation_known")
KEY_COLS = ("id", "rgw_uid", "owner_ad_username", "bucket_name", "access", "label",
            "access_key", "state", "revoked_at")
SHARE_COLS = ("bucket_name", "owner_uid", "recipient_ad", "recipient_uid", "access",
              "revoked_at")


def snapshot(env):
    def pick(rows, cols):
        return sorted(tuple(r[c] for c in cols) for r in rows)
    return (pick(env.db.buckets.list(), BUCKET_COLS),
            pick(env.db.bucket_keys.list(include_revoked=True), KEY_COLS),
            pick(env.db.shares.list_live(), SHARE_COLS))


def reasons(report, key):
    return {(e["item"], e["reason"]) for e in report[key]}


def test_full_rebuild_restores_what_the_portal_made(env):
    populate(env)
    before = snapshot(env)
    created = env.db.buckets.get("alice-new")["created_at"][:10]
    wipe(env)
    report = run(env)
    assert report["mode"] == "full"
    assert report["added"] == {"buckets": 1, "bucket_keys": 2, "shares": 2}
    assert report["problems"] == [], report["problems"]
    assert snapshot(env) == before
    row = env.db.buckets.get("alice-new")
    assert row["created_at"][:10] == created and row["tag_synced"] == 1
    # seeded buckets older than the portal carry no record: no row
    assert ("alice-data", "no_record_in_tags") in reasons(report, "skipped")
    # the rebuilt rows work: the owner sees keys and shares again, the recipient too
    assert {k["label"] for k in env.alice.get("/api/bucket-keys").json()["keys"]} == {
        "backup", "reader"}
    st = {s["recipient"]: s["status"] for s in
          env.alice.get("/api/buckets/alice-new/shares").json()["shares"]}
    assert st == {"bob": "active", "erin": "active"}
    assert [s["bucket"] for s in env.bob.get("/api/shared-with-me").json()["shares"]] == [
        "alice-new"]
    (a,) = env.audit("system.")
    assert a["action"] == "system.rebuild" and a["actor"] == "system"
    assert a["target_type"] == "database"
    assert a["details"]["mode"] == "full" and a["details"]["added"]["shares"] == 2


def test_full_refuses_a_database_with_rows_unless_forced(env):
    populate(env)
    before = snapshot(env)
    with pytest.raises(rebuild.RebuildRefused):
        run(env)
    assert env.audit("system.") == []
    report = run(env, force=True)  # never overwrites or duplicates
    assert report["added"] == {"buckets": 0, "bucket_keys": 0, "shares": 0}
    assert snapshot(env) == before
    assert {r for _, r in reasons(report, "skipped")} >= {"exists"}


def test_fill_gaps_adds_only_missing_rows_and_never_overwrites(env):
    rw, _ = populate(env)
    env.db.buckets.set_budget_number("alice-new", "EDITED")
    with env.db.tx() as c:
        c.execute("DELETE FROM bucket_keys WHERE id = ?", (rw["id"],))
        c.execute("DELETE FROM shares WHERE recipient_ad = 'erin'")
    report = run(env, fill_gaps=True)
    assert report["mode"] == "fill_gaps"
    assert report["added"] == {"buckets": 0, "bucket_keys": 1, "shares": 1}
    assert env.db.buckets.get("alice-new")["budget_number"] == "EDITED"
    assert env.db.bucket_keys.get(rw["id"])["label"] == "backup"
    assert env.db.shares.live_for("alice-new", "erin")["access"] == "readwrite"


def test_tag_owner_mismatch_is_reported_not_used(env):
    populate(env)
    tags = env.moto().get_bucket_tagging(Bucket="alice-new")["TagSet"]
    tags = [t if t["Key"] != "bucketeer-owner" else {"Key": t["Key"], "Value": "mallory"}
            for t in tags]
    env.moto().put_bucket_tagging(Bucket="alice-new", Tagging={"TagSet": tags})
    wipe(env)
    report = run(env)
    assert ("alice-new", "tag_owner_mismatch") in reasons(report, "problems")
    row = env.db.buckets.get("alice-new")
    assert row["owner_uid"] == "alice" and row["tag_synced"] == 0  # reconcile rewrites


def test_one_broken_bucket_does_not_stop_the_run(env, monkeypatch):
    populate(env)
    wipe(env)
    real = rebuild._read

    def flaky(s3, bucket):
        if bucket == "alice-new":
            raise ConnectionError("storage gone")
        return real(s3, bucket)
    monkeypatch.setattr(rebuild, "_read", flaky)
    report = run(env)
    assert ("alice-new", "s3_error") in reasons(report, "problems")
    # the key on the other bucket still comes back
    assert report["added"] == {"buckets": 0, "bucket_keys": 1, "shares": 0}
    assert [k["label"] for k in env.db.bucket_keys.list()] == ["reader"]


def test_key_owner_from_display_names_suspension_and_stale_statements(env):
    rw, ro = populate(env)
    users = env.croit_state.users
    users[rw["uid"]].name = "Bucketeer key backup for alice (alice-new)"  # pre-P6-2 format
    users[ro["uid"]].name = "renamed by someone"  # falls back to the owner uid
    users[ro["uid"]].suspended = True
    env.app.state.croit.invalidate_lists()
    wipe(env)
    report = run(env)
    assert report["added"]["bucket_keys"] == 2
    a, b = env.db.bucket_keys.get(rw["id"]), env.db.bucket_keys.get(ro["id"])
    assert a["owner_ad_username"] == "alice" and a["access"] == "readwrite"
    assert b["owner_ad_username"] == "alice" and b["suspended_by"] == "direct"
    assert a["suspended_at"] is None and b["suspended_at"] is not None
    # a statement whose key user is gone is skipped, not restored
    del users[rw["uid"]]
    env.app.state.croit.invalidate_lists()
    wipe(env)
    report = run(env)
    assert any(r == "key_user_missing" for _, r in reasons(report, "skipped"))
    assert [k["id"] for k in env.db.bucket_keys.list()] == [ro["id"]]


def test_budget_edit_of_an_older_bucket_writes_no_creation_tags(env):
    env.moto().put_bucket_tagging(Bucket="alice-data", Tagging={"TagSet": [
        {"Key": "bucketeer-creator", "Value": "olduser"},
        {"Key": "bucketeer-created", "Value": "2020-01-02"}]})
    r = env.alice.put("/api/buckets/alice-data/budget", json={"budgetNumber": "WT-1"},
                      headers=H)
    assert r.status_code == 200 and r.json()["tagSynced"] is True
    row = env.db.buckets.get("alice-data")
    assert row["creation_known"] == 0 and row["created_by"] is None
    t = env.all_tags("alice-data")
    assert t["bucketeer-creator"] == "olduser" and t["bucketeer-created"] == "2020-01-02"
    assert t["bucketeer-budget"] == "WT-1"
    b = next(b for b in env.alice.get("/api/buckets").json()["buckets"]
             if b["name"] == "alice-data")
    assert b["createdAt"] is None
    # and the rebuild then takes the bucket's own record
    wipe(env)
    run(env)
    row = env.db.buckets.get("alice-data")
    assert row["created_by"] == "olduser" and row["created_at"].startswith("2020-01-02")
    assert row["creation_known"] == 1 and row["budget_number"] == "WT-1"


def test_cli(env, capsys):
    populate(env)
    assert rebuild.main(["--rebuild-from-ceph"], app=env.app) == 1
    assert "Refused" in capsys.readouterr().out
    assert rebuild.main(["--rebuild-from-ceph", "--fill-gaps", "--full"], app=env.app) == 1
    assert rebuild.main(["--rebuild-from-ceph", "--bogus"], app=env.app) == 1
    wipe(env, "shares")
    assert rebuild.main(["--rebuild-from-ceph", "--fill-gaps"], app=env.app) == 0
    out = capsys.readouterr().out
    assert "added: buckets 0, bucket_keys 0, shares 2" in out and "no problems" in out


def test_cli_without_configuration_exits_1(tmp_path):
    keep = {k: v for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "SYSTEMROOT", "PYTHONPATH")}
    p = subprocess.run([sys.executable, str(ROOT / "app.py"), "--rebuild-from-ceph"],
                       cwd=tmp_path, env=keep, capture_output=True, text=True, timeout=60)
    assert p.returncode == 1 and "Invalid configuration" in p.stdout


def test_keyword_form_used_by_the_restore_command(env):
    populate(env)
    wipe(env)
    report = asyncio.run(rebuild.rebuild_from_ceph(
        fill_gaps=True, settings=env.settings, db=env.db, client=env.app.state.croit))
    assert report["added"] == {"buckets": 1, "bucket_keys": 2, "shares": 2}
    assert report["problems"] == []
