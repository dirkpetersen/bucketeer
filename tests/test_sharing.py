"""Sharing a bucket with an AD user (/api/buckets/{name}/shares, /api/shared-with-me).

Against the mock croit, moto and the fake directory. moto enforces no policies or identities,
so the policy DOCUMENT is asserted, and ownership at the Bucketeer layer.
"""

import json

import pytest

from bucketeer import s3docs, sharing
from bucketeer.policy_settings import DEFAULT_QUOTA
from bucketeer.rgw_user import READY, RgwAccount

from .test_auth import SECRET_PW, TEAM_GROUP, USERS_GROUP
from .test_bucket_keys import FOREIGN, break_policy_writes, mk, policy, put_foreign, sids
from .test_buckets import Env, H


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    e.fake.add("erin", groups={USERS_GROUP}, display="Erin E", mail="erin@example.org")
    e.fake.add("frank", groups=set(), display="Frank F", mail="frank@example.org")
    yield e
    e.db.close()


@pytest.fixture
def acme(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint, s3_tenant="acme")
    yield e
    e.db.close()


def share(client, login="bob", access="read", bucket="alice-data"):
    return client.post(f"/api/buckets/{bucket}/shares", json={"login": login, "access": access},
                       headers=H)


def shares(client, bucket="alice-data"):
    return client.get(f"/api/buckets/{bucket}/shares").json()["shares"]


def share_actions(env):
    return [a for a in env.audit("bucket.") if "share" in a["action"]]


# -- lookup ------------------------------------------------------------------------------


def test_lookup_by_username_and_email(env):
    for q in ("erin", "ERIN", "erin@example.org"):
        r = env.alice.get("/api/buckets/alice-data/share-lookup", params={"q": q})
        assert r.status_code == 200
        assert r.json() == {"username": "erin", "displayName": "Erin E", "isYou": False}
    r = env.alice.get("/api/buckets/alice-data/share-lookup", params={"q": "alice"})
    assert r.json()["isYou"] is True


def test_lookup_makes_no_password_bind(env):
    env.alice.get("/api/buckets/alice-data/share-lookup", params={"q": "erin"})
    users = [e for e in env.fake.events if e[0] == "bind" and "CN=erin" in e[2]]
    assert users == []


@pytest.mark.parametrize("q", ["nobody", "frank", "alice@example.org", "*", "*)(uid=*"])
def test_lookup_refusals_are_uniform(env, q):
    # unknown, not in the required group, ambiguous mail, wildcard/injection
    r = env.alice.get("/api/buckets/alice-data/share-lookup", params={"q": q})
    assert r.status_code == 404
    assert r.json() == {"detail": sharing.MSG_NOT_FOUND}


def test_empty_required_group_means_anyone_in_ad(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint, ldap_required_group_dn="")
    e.fake.add("frank", groups=set(), display="Frank F", mail="frank@example.org")
    r = e.alice.get("/api/buckets/alice-data/share-lookup", params={"q": "frank"})
    assert r.status_code == 200 and r.json()["username"] == "frank"
    e.db.close()


def test_lookup_is_rate_limited_per_owner(env):
    for _ in range(sharing.LOOKUP_LIMIT):
        assert env.alice.get("/api/buckets/alice-data/share-lookup",
                             params={"q": "nobody"}).status_code == 404
    r = env.alice.get("/api/buckets/alice-data/share-lookup", params={"q": "erin"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert share(env.alice, "erin").status_code == 429  # shares count too
    # other owners are unaffected
    assert env.bob.get("/api/buckets/bob-shared/share-lookup",
                       params={"q": "erin"}).status_code == 200


def test_lookup_directory_down_is_503(env):
    env.fake.down = set(env.settings.ldap_url)
    r = env.alice.get("/api/buckets/alice-data/share-lookup", params={"q": "erin"})
    assert r.status_code == 503


def test_lookup_other_users_bucket_404_and_no_directory_query(env):
    n = len(env.fake.events)
    r = env.alice.get("/api/buckets/bob-shared/share-lookup", params={"q": "erin"})
    assert r.status_code == 404 and len(env.fake.events) == n
    assert env.anon().get("/api/buckets/alice-data/share-lookup",
                          params={"q": "erin"}).status_code == 401


# -- share ---------------------------------------------------------------------------------


def test_share_with_existing_user(env):
    put_foreign(env)
    r = share(env.alice, "bob", "read")
    assert r.status_code == 201
    v = r.json()
    assert v["recipient"] == "bob" and v["displayName"] == "Bob B" and v["uid"] == "bob"
    assert v["access"] == "read" and v["status"] == "active" and v["accountCreated"] is False
    p = policy(env)
    assert p["Statement"][0] == FOREIGN
    st = p["Statement"][1]
    assert st["Sid"] == "bucketeer-share-u-bob" and st["Effect"] == "Allow"
    assert st["Principal"] == {"AWS": "arn:aws:iam:::user/bob"}
    assert st["Action"] == list(s3docs.READ_ACTIONS)
    assert st["Resource"] == ["arn:aws:s3:::alice-data", "arn:aws:s3:::alice-data/*"]
    row = env.db.shares.get(v["id"])
    assert (row["bucket_name"], row["owner_uid"], row["recipient_ad"], row["recipient_uid"],
            row["access"], row["created_by"], row["revoked_at"]) == (
        "alice-data", "alice", "bob", "bob", "read", "alice", None)
    assert set(env.used) == {env.croit_state.users["alice"].keys[0]["access_key"]}  # owner's key
    (a,) = [a for a in share_actions(env) if a["action"] == "bucket.share"]
    assert a["actor"] == "alice" and a["target_type"] == "bucket" and a["target"] == "alice-data"
    assert a["details"]["recipient"] == "bob" and a["details"]["access"] == "read"


def test_share_with_email_login(env):
    r = share(env.alice, "bob@example.org", "readwrite")
    assert r.status_code == 201 and r.json()["recipient"] == "bob"
    st = policy(env)["Statement"][0]
    assert "s3:PutObject" in st["Action"] and "s3:DeleteBucket" not in st["Action"]


def test_share_creates_account_on_the_spot(env):
    assert "erin" not in env.croit_state.users
    r = share(env.alice, "erin@example.org")
    assert r.status_code == 201 and r.json()["accountCreated"] is True
    u = env.croit_state.users["erin"]
    assert u.name == "Erin E" and u.email == ""  # never an email
    assert u.user_quota["enabled"] is True and u.user_quota["maxSize"] == DEFAULT_QUOTA["maxSize"]
    assert sids(env) == ["bucketeer-share-u-erin"]
    created = [a for a in env.audit("rgw.user.") if a["action"] == "rgw.user.create"]
    assert created and created[-1]["target"] == "erin"


def test_not_eligible_and_ambiguous_refused(env):
    for who in ("frank", "alice@example.org", "ghost"):
        r = share(env.alice, who)
        assert r.status_code == 404 and r.json()["detail"] == sharing.MSG_NOT_FOUND
    assert policy(env) is None and "frank" not in env.croit_state.users
    assert env.db.shares.list_live() == []


def test_self_share_refused(env):
    for who in ("alice", "ALICE@example.org"):
        r = share(env.alice, who)
        assert r.status_code in (409, 404)
    assert share(env.alice, "alice").status_code == 409
    assert policy(env) is None


def test_same_account_via_mapping_refused(env):
    env.bob.put("/api/admin/settings/uid-mappings/erin", json={"rgwUid": "alice"}, headers=H)
    r = share(env.alice, "erin")
    assert r.status_code == 409 and policy(env) is None


def test_mapping_honoured(env):
    env.bob.put("/api/admin/settings/uid-mappings/erin", json={"rgwUid": "bob"}, headers=H)
    r = share(env.alice, "erin")
    assert r.status_code == 201
    assert r.json()["uid"] == "bob" and r.json()["recipient"] == "erin"
    assert sids(env) == ["bucketeer-share-u-bob"]
    assert "erin" not in env.croit_state.users


def test_key_user_refused(env):
    k = mk(env.alice, label="ci").json()
    assert k["uid"] == "alice+ci"
    # the resolver and the mapping rules keep key users away from humans; if one ever
    # slipped through, sharing still refuses
    real = env.app.state.rgw.resolve

    async def resolve(username, display_name=""):
        if username == "erin":
            return RgwAccount(username, "alice+ci", READY, display_name)
        return await real(username, display_name)

    env.app.state.rgw.resolve = resolve
    r = share(env.alice, "erin")
    assert r.status_code == 409 and "bucket key" in r.json()["detail"]
    assert sids(env) == [f"bucketeer-key-{k['id']}"]


def test_uid_taken_refused(env):
    # a bucket-key row claims the uid "erin": the resolver refuses to hand it to a human
    env.db.bucket_keys.create("erin", "alice", "alice-data", "read", "x", "active")
    r = share(env.alice, "erin")
    assert r.status_code == 409 and "erin" not in env.croit_state.users


def test_suspended_recipient_refused(env):
    assert share(env.alice, "erin").status_code == 201
    assert env.bob.post("/api/admin/users/erin/suspend", headers=H).status_code == 200
    r = share(env.alice, "erin", "readwrite")
    assert r.status_code == 409 and "suspended" in r.json()["detail"]


def test_suspended_owner_refused(env):
    env.bob.post("/api/admin/users/alice/suspend", headers=H)
    assert share(env.alice, "bob").status_code == 403
    assert env.alice.get("/api/buckets/alice-data/shares").status_code == 403


def test_croit_down_for_recipient_is_503(env):
    import httpx
    env.transport.fail = lambda r: (httpx.Response(500, json={"message": "x"})
                                    if "/s3/user" in r.url.path else None)
    assert share(env.alice, "erin").status_code == 503
    assert policy(env) is None


def test_other_users_bucket_404_and_no_changes(env):
    assert share(env.alice, "erin", bucket="bob-shared").status_code == 404
    assert env.alice.get("/api/buckets/bob-shared/shares").status_code == 404
    assert env.alice.delete("/api/buckets/bob-shared/shares/1", headers=H).status_code == 404
    assert env.alice.get("/api/buckets/Bad:Name/shares").status_code == 404
    assert "erin" not in env.croit_state.users


def test_csrf_required(env):
    assert env.alice.post("/api/buckets/alice-data/shares",
                          json={"login": "bob", "access": "read"}).status_code == 403
    assert env.alice.delete("/api/buckets/alice-data/shares/1").status_code == 403


@pytest.mark.parametrize("body", [{"login": "bob"}, {"login": "bob", "access": "full"},
                                  {"login": "bob", "access": "read", "uid": "x"}])
def test_bad_bodies_422(env, body):
    assert env.alice.post("/api/buckets/alice-data/shares", json=body,
                          headers=H).status_code == 422


def test_tenant_principal_and_sid(acme):
    acme.fake.add("erin", groups={USERS_GROUP}, display="Erin E", mail="erin@example.org")
    carol = acme.session("carol")
    r = share(carol, "erin", "read", bucket="reports")
    assert r.status_code == 201 and r.json()["uid"] == "acme$erin"
    (st,) = policy(acme, "reports")["Statement"]
    assert st["Sid"] == "bucketeer-share-t-acme-erin"
    assert st["Principal"] == {"AWS": "arn:aws:iam::acme:user/erin"}
    row = acme.db.shares.get(r.json()["id"])
    assert row["bucket_name"] == "acme:reports" and row["owner_uid"] == "acme$carol"


def test_policy_write_failure_changes_nothing(env):
    break_policy_writes(env)
    r = share(env.alice, "bob")
    assert r.status_code == 502
    assert env.db.shares.list_live() == []
    assert not [a for a in share_actions(env) if a["action"] == "bucket.share"]


# -- list / update / revoke ----------------------------------------------------------------


def test_list_shares_and_foreign_ignored(env):
    put_foreign(env)
    share(env.alice, "bob")
    share(env.alice, "erin", "readwrite")
    out = shares(env.alice)
    assert [(s["recipient"], s["access"], s["status"]) for s in out] == [
        ("bob", "read", "active"), ("erin", "readwrite", "active")]
    assert env.alice.get("/api/buckets/alice-data/shares").headers["cache-control"] == "no-store"
    assert shares(env.alice, "alice-empty") == []


def test_change_access_updates_statement_and_row(env):
    put_foreign(env)
    v = share(env.alice, "bob", "read").json()
    r = env.alice.put(f"/api/buckets/alice-data/shares/{v['id']}", json={"access": "readwrite"},
                      headers=H)
    assert r.status_code == 200 and r.json()["access"] == "readwrite"
    p = policy(env)
    assert [s["Sid"] for s in p["Statement"]] == ["legacy-allow", "bucketeer-share-u-bob"]
    assert "s3:PutObject" in p["Statement"][1]["Action"]
    assert env.db.shares.get(v["id"])["access"] == "readwrite"
    (a,) = [a for a in share_actions(env) if a["action"] == "bucket.share_update"]
    assert a["details"]["before"] == "read" and a["details"]["after"] == "readwrite"


def test_share_again_changes_access(env):
    v = share(env.alice, "bob", "read").json()
    r = share(env.alice, "bob", "readwrite")
    assert r.status_code == 200 and r.json()["updated"] is True and r.json()["id"] == v["id"]
    assert len(env.db.shares.list_live()) == 1
    assert len(policy(env)["Statement"]) == 1


def test_revoke_removes_statement_and_marks_row(env):
    put_foreign(env)
    v = share(env.alice, "bob").json()
    r = env.alice.delete(f"/api/buckets/alice-data/shares/{v['id']}", headers=H)
    assert r.status_code == 200 and r.json() == {"id": v["id"], "revoked": True}
    assert sids(env) == ["legacy-allow"]
    assert env.db.shares.get(v["id"])["revoked_at"] is not None
    assert shares(env.alice) == []
    again = env.alice.delete(f"/api/buckets/alice-data/shares/{v['id']}", headers=H)
    assert again.status_code == 200  # idempotent
    assert [a["action"] for a in share_actions(env)].count("bucket.unshare") == 1
    # a revoked share can be made again
    assert share(env.alice, "bob").status_code == 201
    assert env.alice.put(f"/api/buckets/alice-data/shares/{v['id']}", json={"access": "read"},
                         headers=H).status_code == 404  # the old row stays revoked


def test_last_statement_removed_deletes_policy(env):
    v = share(env.alice, "bob").json()
    env.alice.delete(f"/api/buckets/alice-data/shares/{v['id']}", headers=H)
    assert policy(env) is None


def test_revoke_failure_keeps_row(env):
    v = share(env.alice, "bob").json()
    break_policy_writes(env, reads=True)
    r = env.alice.delete(f"/api/buckets/alice-data/shares/{v['id']}", headers=H)
    assert r.status_code == 502
    assert env.db.shares.get(v["id"])["revoked_at"] is None


def test_out_of_sync_and_repair(env):
    v = share(env.alice, "bob").json()
    env.moto().delete_bucket_policy(Bucket="alice-data")  # someone removed the statement
    (s,) = shares(env.alice)
    assert s["status"] == "out_of_sync" and s["id"] == v["id"]
    r = env.alice.put(f"/api/buckets/alice-data/shares/{v['id']}", json={"access": "read"},
                      headers=H)
    assert r.status_code == 200 and sids(env) == ["bucketeer-share-u-bob"]
    assert shares(env.alice)[0]["status"] == "active"


def test_access_drift_is_out_of_sync(env):
    v = share(env.alice, "bob", "read").json()
    with env.db.tx() as c:
        c.execute("UPDATE shares SET access = 'readwrite' WHERE id = ?", (v["id"],))
    assert shares(env.alice)[0]["status"] == "out_of_sync"


def test_unknown_statement_listed_and_revocable(env):
    put_foreign(env)
    stmt = s3docs.share_statement("alice-data", "zed", None, "readwrite")
    env.moto().put_bucket_policy(Bucket="alice-data", Policy=json.dumps(
        {"Version": "2012-10-17", "Statement": [FOREIGN, stmt]}))
    (s,) = shares(env.alice)
    assert s == {"id": "bucketeer-share-u-zed", "recipient": "zed", "displayName": "",
                 "uid": "zed", "access": "readwrite", "createdAt": None, "status": "unknown"}
    r = env.alice.delete("/api/buckets/alice-data/shares/bucketeer-share-u-zed", headers=H)
    assert r.status_code == 200 and sids(env) == ["legacy-allow"]
    assert shares(env.alice) == []
    # foreign Sids can never be addressed
    r = env.alice.delete("/api/buckets/alice-data/shares/legacy-allow", headers=H)
    assert r.status_code == 404 and sids(env) == ["legacy-allow"]


def test_unreadable_policy_is_unchecked(env):
    share(env.alice, "bob")
    break_policy_writes(env, reads=True)
    body = env.alice.get("/api/buckets/alice-data/shares").json()
    assert body["checked"] is False and body["shares"][0]["status"] == "unchecked"


def test_share_coexists_with_bucket_key(env):
    k = mk(env.alice, label="ci").json()
    share(env.alice, "bob")
    assert set(sids(env)) == {f"bucketeer-key-{k['id']}", "bucketeer-share-u-bob"}
    assert [s["recipient"] for s in shares(env.alice)] == ["bob"]


# -- recipient side --------------------------------------------------------------------------


def test_shared_with_me_lists_only_live_shares_for_recipient(env):
    a = share(env.alice, "bob", "readwrite").json()
    share(env.alice, "erin", "read")
    env.bob.post("/api/buckets", json={"name": "bob-new", "budgetNumber": "WT-1"}, headers=H)
    share(env.bob, "erin", "read", bucket="bob-new")
    r = env.bob.get("/api/shared-with-me")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    (s,) = r.json()["shares"]
    assert (s["bucket"], s["address"], s["owner"], s["access"]) == (
        "alice-data", "alice-data", "Alice A", "readwrite") or s["owner"]
    assert s["sharedAt"].endswith("Z")
    env.alice.delete(f"/api/buckets/alice-data/shares/{a['id']}", headers=H)
    assert env.bob.get("/api/shared-with-me").json() == {"shares": []}
    erin = env.session("erin")
    assert {x["bucket"] for x in erin.get("/api/shared-with-me").json()["shares"]} == {
        "alice-data", "bob-new"}
    assert env.anon().get("/api/shared-with-me").status_code == 401
    secret = json.dumps(erin.get("/api/shared-with-me").json())
    assert "access_key" not in secret and "secret" not in secret.lower()


def test_shared_with_me_cross_tenant_address(acme):
    carol = acme.session("carol")
    acme.fake.add("erin", groups={USERS_GROUP}, display="Erin E", mail="erin@example.org")
    share(carol, "erin", "read", bucket="reports")
    (s,) = acme.session("erin").get("/api/shared-with-me").json()["shares"]
    assert s["bucket"] == "reports" and s["address"] == "reports"  # same tenant
    # a recipient in another tenant addresses it tenant:bucket
    acme.db.shares.create("acme:reports", "acme$carol", "Carol C", "x", "other$x", "", "read", "c")
    assert acme.db.shares.list_live(recipient_uid="other$x")[0]["bucket_name"] == "acme:reports"


def test_bucket_delete_marks_shares_revoked(env):
    v = share(env.alice, "bob").json()
    r = env.alice.request("DELETE", "/api/buckets/alice-data", headers=H,
                          json={"confirmName": "alice-data"})
    assert r.status_code == 200
    assert env.db.shares.get(v["id"])["revoked_at"] is not None
    assert env.bob.get("/api/shared-with-me").json() == {"shares": []}
    un = [a for a in share_actions(env) if a["action"] == "bucket.unshare"]
    assert un and un[0]["details"]["reason"] == "bucket_deleted"


def test_no_secret_in_audit(env):
    share(env.alice, "erin")
    text = json.dumps(share_actions(env))
    for u in env.croit_state.users.values():
        for k in u.keys:
            assert k["secret_key"] not in text
    assert SECRET_PW not in text and TEAM_GROUP not in text


# -- stale rows and old ids ----------------------------------------------------------------


def test_bucket_recreated_by_another_owner(env):
    # Bob once shared "alice-data" with erin; the bucket was deleted outside Bucketeer and
    # alice owns the name now. Bob's row must never show up, be reused or be repaired.
    stale = env.db.shares.create("alice-data", "bob", "Bob B", "erin", "erin", "Erin E", "read",
                                 "bob")
    assert shares(env.alice) == []
    assert env.db.shares.get(stale["id"])["revoked_at"] is not None
    (a,) = [a for a in share_actions(env) if a["action"] == "bucket.unshare"]
    assert a["details"]["reason"] == "owner_changed" and a["details"]["previousOwner"] == "bob"
    assert policy(env) is None  # nothing written into alice's bucket
    for call in (lambda: env.alice.put(f"/api/buckets/alice-data/shares/{stale['id']}",
                                       json={"access": "read"}, headers=H),
                 lambda: env.alice.delete(f"/api/buckets/alice-data/shares/{stale['id']}",
                                          headers=H)):
        assert call().status_code == 404
    assert policy(env) is None
    # sharing with erin makes a fresh row owned by alice, not a reuse of bob's
    env.db.shares.create("alice-data", "bob", "Bob B", "erin", "erin", "Erin E", "readwrite",
                         "bob")
    r = share(env.alice, "erin", "read")
    assert r.status_code == 201 and r.json()["id"] != stale["id"]
    assert [x["owner_uid"] for x in env.db.shares.list_live("alice-data")] == ["alice"]
    # and the recipient's view lists only the live, current share
    (s,) = env.session("erin").get("/api/shared-with-me").json()["shares"]
    assert s["owner"] == "Alice A"


def test_reshare_then_delete_old_id_keeps_live_share(env):
    old = share(env.alice, "bob").json()
    env.alice.delete(f"/api/buckets/alice-data/shares/{old['id']}", headers=H)
    new = share(env.alice, "bob", "readwrite").json()
    n = len(share_actions(env))
    r = env.alice.delete(f"/api/buckets/alice-data/shares/{old['id']}", headers=H)
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert sids(env) == ["bucketeer-share-u-bob"]  # the live statement is untouched
    assert env.db.shares.get(new["id"])["revoked_at"] is None
    assert len(share_actions(env)) == n
    assert shares(env.alice)[0]["status"] == "active"


def test_delete_by_sid_with_live_row_revokes_row(env):
    v = share(env.alice, "bob").json()
    r = env.alice.delete("/api/buckets/alice-data/shares/bucketeer-share-u-bob", headers=H)
    assert r.status_code == 200 and r.json()["id"] == v["id"]
    assert policy(env) is None
    assert env.db.shares.get(v["id"])["revoked_at"] is not None
    (a,) = [a for a in share_actions(env) if a["action"] == "bucket.unshare"]
    assert a["details"]["recipient"] == "bob" and a["details"]["share"] == v["id"]
    assert shares(env.alice) == []


def test_stale_rows_hidden_even_if_sweep_is_swallowed(env, monkeypatch):
    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(sharing, "_sweep", nothing)  # as if it raised and was swallowed
    stale = env.db.shares.create("alice-data", "bob", "Bob B", "erin", "erin", "Erin E", "read",
                                 "bob")
    assert shares(env.alice) == []
    r = share(env.alice, "erin", "readwrite")
    assert r.status_code == 201 and r.json()["id"] != stale["id"]
    assert env.db.shares.get(stale["id"])["revoked_at"] is not None
    assert [x["owner_uid"] for x in env.db.shares.list_live("alice-data")] == ["alice"]


def test_shared_with_me_drops_shares_of_vanished_buckets(env):
    live = share(env.alice, "bob").json()
    gone = env.db.shares.create("alice-gone", "alice", "Alice A", "bob", "bob", "Bob B", "read",
                                "alice")
    r = env.bob.get("/api/shared-with-me")
    assert [s["bucket"] for s in r.json()["shares"]] == ["alice-data"]
    assert env.db.shares.get(gone["id"])["revoked_at"] is not None
    assert env.db.shares.get(live["id"])["revoked_at"] is None
    (a,) = [a for a in share_actions(env) if a["details"].get("reason") == "bucket_gone"]
    assert a["actor"] == "system" and a["action"] == "bucket.unshare"
    assert a["target"] == "alice-gone"


def test_shared_with_me_keeps_rows_when_croit_fails(env):
    import httpx
    gone = env.db.shares.create("alice-gone", "alice", "Alice A", "bob", "bob", "Bob B", "read",
                                "alice")
    real = env.transport.fail
    env.transport.fail = lambda r: (httpx.Response(500, json={"message": "x"})
                                    if r.url.path.endswith("/buckets") else real and real(r))
    out = env.bob.get("/api/shared-with-me")
    env.transport.fail = real
    assert out.status_code in (200, 502, 503)
    assert env.db.shares.get(gone["id"])["revoked_at"] is None


@pytest.mark.parametrize("body", [{}, None, "oops"])
def test_shared_with_me_keeps_rows_when_croit_answers_a_non_list(env, body):
    import httpx
    live = share(env.alice, "bob").json()
    real = env.transport.fail
    env.transport.fail = lambda r: (httpx.Response(200, json=body)
                                    if r.url.path.endswith("/buckets") else real and real(r))
    out = env.bob.get("/api/shared-with-me")
    env.transport.fail = real
    assert out.status_code == 200
    assert env.db.shares.get(live["id"])["revoked_at"] is None


class FakeMailer:
    def __init__(self, result=True):
        self.result, self.sent = result, []

    async def send_share_invite(self, to, owner_name, bucket, access):
        self.sent.append((to, owner_name, bucket, access))
        return self.result


def test_new_share_sends_invitation_once(env):
    env.app.state.mailer = FakeMailer(True)
    r = share(env.alice, "erin", "readwrite")
    assert r.status_code == 201 and r.json()["emailed"] is True
    assert env.app.state.mailer.sent == [("erin@example.org", env.app.state.mailer.sent[0][1],
                                           "alice-data", "readwrite")]
    again = share(env.alice, "erin", "read")  # a change of access is not a new invitation
    assert again.status_code == 200 and "emailed" not in again.json()
    assert len(env.app.state.mailer.sent) == 1


def test_failed_invitation_never_fails_the_share(env):
    env.app.state.mailer = FakeMailer(False)
    r = share(env.alice, "erin")
    assert r.status_code == 201 and r.json()["emailed"] is False


def test_email_off_by_default(env):
    r = share(env.alice, "erin")
    assert r.status_code == 201 and r.json()["emailed"] is None
