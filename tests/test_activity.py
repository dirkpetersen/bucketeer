"""My activity (GET /api/activity): a person sees their own entries and what Bucketeer or an
administrator did to their things, never other people's entries or names.

Against the mock croit (both admin backends), moto and the fake directory, like
`test_sharing.py`. Rows made through the API carry the real detail shapes; a few rows are
written straight into the audit log to reach the edges (an earlier owner, typed logins)."""

import json

import pytest

from bucketeer import activity
from bucketeer.activity import Scope
from bucketeer.croit import CroitUnavailable
from bucketeer.main import STATIC_DIR

from .backends import admin_backend  # noqa: F401  (fixture: every test on both backends)
from .test_auth import SECRET_PW
from .test_buckets import ALICE_KEY, BOB_KEY, Env, H

ENTRY_KEYS = {"id", "ts", "area", "action", "target", "actor", "summary"}
EARLY = "2000-01-01T00:00:00.000000Z"


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def feed(client, **params):
    r = client.get("/api/activity", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def entries(client, **params):
    return feed(client, **params)["entries"]


def by_action(items, action):
    return [e for e in items if e["action"] == action]


def raw(env, actor, action, target_type, target, details=None, ts=None):
    """An audit row written directly (ts in the past when given)."""
    rid = env.db.audit.add(actor, action, target_type, target, details or {})
    if ts:
        with env.db.tx() as c:
            c.execute("UPDATE audit_log SET ts = ? WHERE id = ?", (ts, rid))
    return rid


# -- basics -------------------------------------------------------------------------------


def test_needs_a_session(env):
    assert env.anon().get("/api/activity").status_code == 401


def test_shape_no_store_and_own_login(env):
    r = env.alice.get("/api/activity")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"entries", "nextCursor", "partial"}
    assert body["nextCursor"] is None and body["partial"] is False
    assert all(set(e) == ENTRY_KEYS for e in body["entries"])
    (login,) = by_action(body["entries"], "login.success")
    assert login["actor"] == "you" and login["area"] == "login" and login["target"] is None
    assert login["summary"].startswith("Signed in")


def test_limit_validation_and_cap(env):
    for bad in ({"limit": 0}, {"limit": -1}, {"before_id": 0}, {"limit": "x"}):
        assert env.alice.get("/api/activity", params=bad).status_code == 422
    for _ in range(activity.MAX_LIMIT + 5):
        raw(env, "alice", "bucket.tag_sync", "bucket", "alice-data")
    body = feed(env.alice, limit=1000)
    assert len(body["entries"]) == activity.MAX_LIMIT and body["nextCursor"] is not None
    assert len(entries(env.alice)) == activity.DEFAULT_LIMIT


def test_paging_newest_first_by_id(env):
    ids = [raw(env, "alice", "bucket.tag_sync", "bucket", "alice-data") for _ in range(7)]
    seen, cursor = [], None
    while True:
        params = {"limit": 3, **({"before_id": cursor} if cursor else {})}
        page = feed(env.alice, **params)
        got = [e["id"] for e in page["entries"]]
        assert got == sorted(got, reverse=True)
        if page["nextCursor"] is not None:
            assert len(got) == 3 and page["nextCursor"] == got[-1]
        seen += got
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen))
    assert set(ids) <= set(seen)
    assert seen == sorted(seen, reverse=True)


def test_raw_details_never_returned(env):
    raw(env, "alice", "bucket.create", "bucket", "alice-data",
        {"uid": "alice", "versioning": True, "quotaTiB": 3, "note": "leak@example.org",
         "displayName": "Someone Else"})
    raw(env, "alice", "frobnicate.the_thing", "widget", "w1", {"x": "leak@example.org"})
    body = feed(env.alice)
    text = json.dumps(body)
    assert "leak@example.org" not in text and "Someone Else" not in text
    (create,) = by_action(body["entries"], "bucket.create")
    assert create["summary"] == "Bucket created (versioning on, 3 TiB)"
    assert create["area"] == "bucket" and create["target"] == "alice-data"
    (odd,) = by_action(body["entries"], "frobnicate.the_thing")
    assert odd["summary"] == "Frobnicate the thing" and odd["area"] == "other"
    assert odd["target"] == "w1"  # the person's own action


# -- isolation ---------------------------------------------------------------------------


def test_alice_never_sees_bobs_entries(env):
    mine = raw(env, "alice", "bucket.tag_sync", "bucket", "alice-data")
    theirs = [
        raw(env, "bob", "bucket.tag_sync", "bucket", "bob-shared", {"uid": "bob"}),
        raw(env, "bob", "profile.update", "profile", "bob", {"field": "advanced_mode"}),
        raw(env, "bob", "credentials.key.create", "access_key", BOB_KEY, {"uid": "bob"}),
        # bob is a person here, not an administrator: his row about alice's bucket name
        # (say, from when he owned a bucket of that name) is not alice's business
        raw(env, "bob", "bucket.versioning", "bucket", "alice-data",
            {"uid": "bob", "after": "enabled"}),
        # system and admin rows about bob's things
        raw(env, "system", "rgw.user.quota_unlimited", "rgw_user", "bob", {}),
        raw(env, "bob", "admin.bucket.quota", "bucket", "bob-shared", {}),
        raw(env, "system", "credentials.key.delete", "access_key", BOB_KEY, {}),
    ]
    ids = {e["id"] for e in entries(env.alice)}
    assert mine in ids and not ids & set(theirs)
    bob_ids = {e["id"] for e in entries(env.bob)}
    assert mine not in bob_ids and set(theirs[:3]) <= bob_ids


def test_typed_login_failures_by_others_do_not_show(env):
    anon = env.anon()
    r = anon.post("/api/login", json={"login": "alice", "password": "not-it"}, headers=H)
    assert r.status_code == 401
    assert env.db.audit.query(action="login.failure")[0]["actor"] == "alice"
    raw(env, "alice", "login.throttled", "user", "alice", {"ip": "192.0.2.9"})
    items = entries(env.alice)
    assert not by_action(items, "login.failure") and not by_action(items, "login.throttled")
    # a real sign-in (actor = the AD name, any case) shows, with its address
    raw(env, "ALICE", "login.success", "user", "ALICE", {"ip": "192.0.2.7", "login_as": "x"})
    raw(env, "alice", "logout", "user", "alice", {"ip": "192.0.2.7"})
    items = entries(env.alice)
    assert items[0]["summary"] == "Signed out" and items[0]["actor"] == "you"
    assert items[1]["summary"] == "Signed in from 192.0.2.7" and items[1]["actor"] == "you"


def test_share_received_shows_without_the_owners_name(env):
    r = env.bob.post("/api/buckets/bob-shared/shares", json={"login": "alice", "access": "read"},
                     headers=H)
    assert r.status_code == 201, r.text
    share_id = r.json()["id"]
    # a share of the same bucket with someone else is not alice's business
    assert env.bob.post("/api/buckets/bob-shared/shares", json={"login": "carol", "access": "read"},
                        headers=H).status_code == 201
    assert env.bob.put(f"/api/buckets/bob-shared/shares/{share_id}", json={"access": "readwrite"},
                       headers=H).status_code == 200
    assert env.bob.delete(f"/api/buckets/bob-shared/shares/{share_id}",
                          headers=H).status_code == 200
    body = feed(env.alice)
    text = json.dumps(body)
    assert "Bob B" not in text and "bob@example.org" not in text
    assert "Carol" not in text and "carol" not in text
    shares = [e for e in body["entries"] if e["area"] == "share"]
    assert [(e["action"], e["actor"], e["target"], e["summary"]) for e in shares] == [
        ("bucket.unshare", "owner", "bob-shared", "Your access was removed"),
        ("bucket.share_update", "owner", "bob-shared", "Your access changed to read and write"),
        ("bucket.share", "owner", "bob-shared", "Shared with you, read-only"),
    ]
    assert all(e["actor"] in ("you", "bucketeer", "admin", "owner") for e in body["entries"])
    # the owner sees the same rows as their own work, with the recipient they chose
    mine = [e for e in entries(env.bob) if e["action"] == "bucket.share"]
    assert {e["summary"] for e in mine} == {
        "Shared with alice, read-only",
        "Shared with carol, read-only; their S3 account was created"}  # made on the spot
    assert all(e["actor"] == "you" for e in mine)


def test_hostile_share_details_leak_nothing(env):
    raw(env, "bob", "bucket.share", "bucket", "bob-shared",
        {"uid": "alice", "recipient": "alice", "access": "read", "ownerEmail": "bob@example.org",
         "displayName": "Bob B", "previousOwner": "bob"})
    raw(env, "bob", "bucket.unshare", "bucket", "bob-shared",
        {"uid": "ALICE", "reason": "bucket_deleted", "owner": "Bob B <bob@example.org>"})
    body = feed(env.alice)
    text = json.dumps(body)
    assert "bob@example.org" not in text and "Bob B" not in text
    (gone,) = by_action(body["entries"], "bucket.unshare")
    assert gone["summary"] == "Your access was removed (the bucket was deleted)"


# -- others acting on the person's things -----------------------------------------------------


def test_admin_actions_show_as_admin_without_a_name(env):
    r = env.bob.put("/api/admin/buckets/alice-data/quota",
                    json={"enabled": True, "maxSize": 5 * 2**40, "maxObjects": None}, headers=H)
    assert r.status_code == 200, r.text
    assert env.bob.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    body = feed(env.alice)  # a suspended person still sees why
    text = json.dumps(body)
    assert "Bob" not in text and "bob@" not in text
    assert not any(e["actor"] == "bob" for e in body["entries"])
    (quota,) = by_action(body["entries"], "admin.bucket.quota")
    assert quota["actor"] == "admin" and quota["target"] == "alice-data"
    assert quota["area"] == "bucket" and quota["summary"].endswith("to 5 TiB")
    (susp,) = by_action(body["entries"], "admin.user.suspend")
    assert susp["actor"] == "admin" and susp["area"] == "account" and susp["target"] is None
    assert susp["summary"].startswith("Account suspended")
    assert env.bob.post("/api/admin/users/alice/unsuspend", headers=H).status_code == 200
    (back,) = by_action(entries(env.alice), "admin.user.unsuspend")
    assert back["summary"].startswith("Account reactivated") and back["actor"] == "admin"
    # the administrator sees them as their own work, naming the account
    (own,) = by_action(entries(env.bob), "admin.user.suspend")
    assert own["actor"] == "you" and own["target"] == "alice"


def test_system_entries_show_as_bucketeer(env):
    raw(env, "system", "rgw.user.quota_unlimited", "rgw_user", "alice", {"before": {}})
    raw(env, "system", "offboarding.suspend", "rgw_user", "alice",
        {"run": 1, "ad_usernames": ["alice"], "reason": "disabled", "keys": []})
    raw(env, "system", "offboarding.unsuspend", "rgw_user", "alice", {"ad_username": "alice"})
    raw(env, "system", "credentials.key.delete", "access_key", ALICE_KEY, {})
    items = entries(env.alice)
    assert [(e["action"], e["actor"]) for e in items[:4]] == [
        ("credentials.key.delete", "bucketeer"), ("offboarding.unsuspend", "bucketeer"),
        ("offboarding.suspend", "bucketeer"), ("rgw.user.quota_unlimited", "bucketeer")]
    assert items[0]["target"] == "…" + ALICE_KEY[-4:] and items[0]["area"] == "key"
    assert items[2]["summary"] == "Account suspended: your directory account is disabled"
    assert items[1]["summary"] == "Account reactivated when you signed in"


def test_rows_from_before_the_bucket_was_created_are_hidden(env):
    env.db.buckets.create("alice-data", "alice", "DN1", by="alice")
    old = raw(env, "otheradmin", "admin.bucket.quota", "bucket", "alice-data",
              {"before": None, "after": None}, ts=EARLY)
    new = raw(env, "otheradmin", "admin.bucket.quota", "bucket", "alice-data", {})
    ids = {e["id"] for e in entries(env.alice)}
    assert new in ids and old not in ids


def test_bucket_key_rows_by_an_admin(env):
    mine = env.db.bucket_keys.create("alice+job", "alice", "alice-data", "read", "job")
    other = env.db.bucket_keys.create("bob+job", "bob", "bob-shared", "read", "nightly")
    raw(env, "bob", "admin.bucket_key.revoke", "bucket_key", str(mine["id"]),
        {"owner": "alice", "bucket": "alice-data"})
    raw(env, "bob", "admin.bucket_key.revoke", "bucket_key", str(other["id"]), {"owner": "bob"})
    (e,) = by_action(entries(env.alice), "admin.bucket_key.revoke")
    assert e["target"] == "job" and e["actor"] == "admin" and e["summary"] == "Bucket key revoked"


def test_bucket_list_trouble_falls_back_and_says_partial(env, monkeypatch):
    env.db.buckets.create("alice-data", "alice", "DN1", by="alice", created_at=EARLY)
    rid = raw(env, "bob", "admin.bucket.quota", "bucket", "alice-data", {})

    async def down(uid):
        raise CroitUnavailable("down")

    monkeypatch.setattr(env.app.state.croit, "list_user_buckets", down)
    body = feed(env.alice)
    assert body["partial"] is True
    assert rid in {e["id"] for e in body["entries"]}  # from the recorded bucket row


def test_unresolvable_account_still_shows_own_entries(env, monkeypatch):
    rid = raw(env, "alice", "profile.update", "profile", "alice",
              {"field": "advanced_mode", "old": False, "new": True})
    raw(env, "system", "rgw.user.quota_unlimited", "rgw_user", "alice", {})
    env.app.state.rgw.invalidate(username="alice")

    async def down(*a, **k):
        raise CroitUnavailable("down")

    monkeypatch.setattr(env.app.state.croit, "get_user", down)
    monkeypatch.setattr(env.app.state.croit, "list_user_names", down)
    monkeypatch.setattr(env.app.state.croit, "service_uid", down, raising=False)
    body = feed(env.alice)
    assert body["partial"] is True
    (e,) = [e for e in body["entries"] if e["id"] == rid]
    assert e["summary"] == "Advanced mode turned on" and e["area"] == "profile"
    assert not by_action(body["entries"], "rgw.user.quota_unlimited")


# -- summaries and targets (units) ---------------------------------------------------------


def one(row, scope=None):
    scope = scope or Scope("alice", "alice")
    base = {"id": 1, "ts": "2026-11-28T10:00:00.000000Z", "actor": "alice",
            "target_type": "bucket", "target": "alice-data", "details": "{}"}
    base.update(row)
    if isinstance(base["details"], dict):
        base["details"] = json.dumps(base["details"])
    return activity.entry(base, scope)


@pytest.mark.parametrize(("action", "details", "summary"), [
    ("bucket.quota", {"oldTiB": 1, "newTiB": 5}, "Quota changed from 1 TiB to 5 TiB"),
    ("admin.bucket.quota", {"location": "cloud", "oldTiB": 3, "newTiB": 3}, "Quota set to 3 TiB"),
    ("bucket.budget", {"old": "DN1", "new": "DN2"}, "Budget number changed from DN1 to DN2"),
    ("bucket.budget", {"old": "DN1", "new": None}, "Budget number removed"),
    ("bucket.versioning", {"before": "enabled", "after": "suspended"}, "Versioning paused"),
    ("bucket.delete", {"objects": 3, "bytes": 3 * 2**20, "dataDeleted": True},
     "Bucket deleted with 3 objects (3 MiB)"),
    ("bucket.share_link", {"expiresIn": 86400}, "Share link made, valid for 1 day"),
    ("bucket.lifecycle", {"after": {"expireCurrentDays": None, "expireNoncurrentDays": 30,
                                    "abortMultipartDays": 7}},
     "Lifecycle: old versions expire after 30 days; unfinished uploads end after 7 days"),
    ("bucket.cors", {"rules_after": 0}, "CORS rules removed"),
    ("bucket.multipart_abort", {"aborted": 2, "failed": 0}, "2 unfinished uploads cleared"),
    ("object.legal_hold", {"after": "ON"}, "Legal hold placed on an object"),
    ("bucket.object_lock", {"after": {"mode": "GOVERNANCE", "days": 30}},
     "Default retention set to 30 days, governance mode"),
    ("bucket.quota", {"oldTiB": "lots", "newTiB": [1]}, "Quota changed"),  # odd details
])
def test_bucket_summaries(action, details, summary):
    assert one({"action": action, "details": details})["summary"] == summary


def test_key_and_account_summaries():
    key = {"target_type": "access_key", "target": "AKIAEXAMPLE00001234"}
    e = one({**key, "action": "credentials.key.rotate",
             "details": {"newKey": "AKIAEXAMPLE0000WXYZ", "old_key_removed": False}})
    assert e["target"] == "…1234" and e["area"] == "key"
    assert e["summary"] == "Access key rotated, new key …WXYZ; the old key could not be deleted"
    e = one({**key, "action": "credentials.setup",
             "details": {"os": "macos", "ip": "198.51.100.4"}})
    assert e["summary"] == "Computer set up (macOS, from 198.51.100.4)"
    e = one({"target_type": "subuser", "target": "alice:ci", "action": "subuser.update",
             "details": {"old": "read", "permission": "full-control"}})
    assert e["target"] == "ci"
    assert e["summary"] == "Subuser access changed from read to full control"
    e = one({"target_type": "rgw_user", "target": "alice", "actor": "system",
             "action": "rgw.user.create", "details": {"quota_applied": True}})
    assert e["actor"] == "bucketeer" and e["target"] is None
    assert e["summary"] == "S3 account created"


def test_cloud_bucket_names_are_shown_short():
    scope = Scope("alice", "alice", ("bucketeer-alice-photos",),
                  frozenset({"bucketeer-alice-photos"}), cloud_prefix="bucketeer-alice-")
    e = one({"target": "bucketeer-alice-photos", "action": "cloud.bucket.create",
             "details": {"storageClass": "deep_archive", "quotaTiB": 2}}, scope)
    assert e["target"] == "photos" and e["area"] == "cloud"
    assert e["summary"] == "Cloud bucket created (Deep Archive, 2 TiB)"
    # an on-prem bucket that happens to start with the prefix keeps its name
    e = one({"target": "bucketeer-alice-x", "action": "bucket.create"}, scope)
    assert e["target"] == "bucketeer-alice-x"


def test_tenant_buckets_drop_the_own_tenant_only():
    scope = Scope("carol", "acme$carol", ("acme:reports",))
    assert one({"actor": "carol", "target": "acme:reports", "action": "bucket.tags",
                "details": {"tags_after": 2}}, scope)["target"] == "reports"
    other = one({"actor": "dave", "target": "beta:data", "action": "bucket.share",
                 "details": {"uid": "acme$carol", "access": "readwrite"}}, scope)
    assert other["target"] == "beta:data" and other["actor"] == "owner"
    assert other["summary"] == "Shared with you, read and write"


def test_others_names_never_reach_a_summary():
    # not the person's own action: no recipient, budget number or label from the details
    e = one({"actor": "system", "action": "bucket.share",
             "details": {"recipient": "zed", "uid": "zed", "access": "read"}})
    assert e["summary"] == "Bucket shared, read-only"
    e = one({"actor": "someadmin", "action": "bucket.budget", "details": {"old": "A", "new": "B"}})
    assert e["summary"] == "Budget number changed" and e["actor"] == "admin"


# -- query plan, migration, frontend ------------------------------------------------------------


def test_one_indexed_query(env):
    scope = Scope("alice", "alice", ("alice-data",), keys=(ALICE_KEY,))
    sql, params = activity._sql(scope, None, 50)
    plan = [r["detail"] for r in env.db.query("EXPLAIN QUERY PLAN " + sql, params)]
    assert any("audit_log_actor_nocase" in p for p in plan), plan
    assert any("audit_log_action" in p for p in plan), plan
    assert not any(p.startswith("SCAN audit_log") for p in plan), plan  # no full log scan
    idx = env.db.query_one("SELECT sql FROM sqlite_master WHERE name = 'audit_log_actor_nocase'")
    assert idx and "COLLATE NOCASE" in idx["sql"]


def test_frontend_route_menu_and_styles():
    js = STATIC_DIR / "js"
    assert '"#/activity": "activity"' in (js / "app.js").read_text()
    dash = (js / "views" / "dashboard.js").read_text()
    assert 'href: "#/activity"' in dash and '"My activity"' in dash and "renderActivity()" in dash
    view = (js / "views" / "activity.js").read_text()
    assert 'from "../api.js"' in view and "fetch(" not in view
    assert "Load more" in view and "Back to my workspace" in view
    assert 'href="css/activity.css"' in (STATIC_DIR / "index.html").read_text()


def test_password_never_in_feed(env):
    assert SECRET_PW not in json.dumps(feed(env.alice))
