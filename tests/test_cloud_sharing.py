"""Sharing cloud (AWS) buckets with people (/api/cloud/buckets/{name}/shares,
/api/cloud/shared-with-me): recipients as on premises (Q8), the recipient's IAM user made on
the spot (decision 17 refusal), one bucket-policy statement per recipient written by the AWS
admin user, list states, update and revoke, bucket delete revokes, the invitation email, the
rebuild. moto `mock_aws` enforces no bucket policies: the documents are asserted."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from bucketeer import bucket_delete, cloud, cloud_delete, rebuild, sharing
from bucketeer.cloud import (
    BUCKET_ACTIONS,
    OBJECT_ACTIONS,
    SHARE_READ_ACTIONS,
    SHARE_WRITE_ACTIONS,
    share_sid,
)

from . import test_cloud_buckets
from .test_admin import H
from .test_auth import USERS_GROUP
from .test_cloud_buckets import NEW, B, acts

base_env = test_cloud_buckets.env  # the fixture (moto mock_aws, cloud the default location)

FULL = "bucketeer-alice-scans"
ROOT = Path(__file__).resolve().parent.parent


class Mail:
    enabled = True

    def __init__(self):
        self.calls = []

    async def send_share_invite(self, to, owner_name, bucket, access, location="onprem"):
        self.calls.append({"to": to, "owner": owner_name, "bucket": bucket, "access": access,
                           "location": location})
        return True


@pytest.fixture
def env(base_env):
    e = base_env
    e.fake.add("erin", groups={USERS_GROUP}, display="Erin E", mail="erin@example.org")
    e.fake.add("dana", groups={USERS_GROUP}, display="Dana D", mail="dana@example.org")
    e.mail = e.app.state.mailer = Mail()
    assert e.user.post(B, json=NEW, headers=H).status_code == 201
    return e


def share(client, login="erin", access="read", name="scans"):
    return client.post(f"{B}/{name}/shares", json={"login": login, "access": access}, headers=H)


def shares(client, name="scans"):
    r = client.get(f"{B}/{name}/shares")
    assert r.status_code == 200, r.text
    return r.json()


def policy(env, bucket=FULL):
    return json.loads(env.s3.get_bucket_policy(Bucket=bucket)["Policy"])


def stmt(env, sid, bucket=FULL):
    return next((s for s in policy(env, bucket)["Statement"] if s.get("Sid") == sid), None)


def arn(env, iam_user):
    return asyncio.run(env.cloud.user_arn(iam_user))


# -- statement shape -----------------------------------------------------------------------


def test_share_read_makes_the_statement_and_the_recipient_account(env):
    assert env.db.cloud_accounts.get("erin") is None
    r = share(env.user, "erin@example.org", "read")
    assert r.status_code == 201, r.text
    v = r.json()
    assert (v["recipient"], v["displayName"], v["iamUser"], v["access"], v["status"]) == (
        "erin", "Erin E", "s3-erin", "read", "active")
    assert v["accountCreated"] is True and v["emailed"] == "queued"
    st = stmt(env, "bucketeer-share-i-s3-erin")
    assert st == {"Sid": "bucketeer-share-i-s3-erin", "Effect": "Allow",
                  "Principal": {"AWS": arn(env, "s3-erin")}, "Action": SHARE_READ_ACTIONS,
                  "Resource": [f"arn:aws:s3:::{FULL}", f"arn:aws:s3:::{FULL}/*"]}
    assert stmt(env, "bucketeer-owner") is not None  # the owner keeps access
    # the recipient's IAM user: Bucketeer's path and owner tag, its list-only policy, no key
    user = env.iam.get_user(UserName="s3-erin")["User"]
    assert user["Path"] == "/bucketeer/"
    tags = {t["Key"]: t["Value"] for t in env.iam.list_user_tags(UserName="s3-erin")["Tags"]}
    assert tags["bucketeer-owner"] == "erin"
    assert env.iam.list_user_policies(UserName="s3-erin")["PolicyNames"] == ["bucketeer-buckets"]
    assert env.iam.list_access_keys(UserName="s3-erin")["AccessKeyMetadata"] == []
    assert env.db.cloud_accounts.get("erin")["iam_user"] == "s3-erin"
    row = env.db.shares.get(v["id"])
    assert (row["location"], row["bucket_name"], row["owner_uid"], row["recipient_ad"],
            row["recipient_uid"], row["recipient_iam"], row["access"], row["created_by"]) == (
        "cloud", FULL, "alice", "erin", "erin", "s3-erin", "read", "alice")
    # audit: the share by the owner, and the recipient's account made by the owner's action
    [a] = acts(env, "cloud.bucket.share")
    assert (a["actor"], a["target_type"], a["target"]) == ("alice", "bucket", FULL)
    assert a["details"]["recipient"] == "erin" and a["details"]["iamUser"] == "s3-erin"
    assert a["details"]["access"] == "read" and a["details"]["accountCreated"] is True
    [made] = [x for x in acts(env, "cloud.user.create") if x["target"] == "s3-erin"]
    assert made["actor"] == "alice" and made["details"]["via"] == "share"
    # the invitation names the full AWS bucket and says it is in Cloud
    assert env.mail.calls == [{"to": "erin@example.org", "owner": env.mail.calls[0]["owner"],
                               "bucket": FULL, "access": "read", "location": "cloud"}]
    assert env.mail.calls[0]["owner"]
    # the on-premises tables and routes never see it
    assert env.db.shares.list_live() == [] and env.db.shares.live_for(FULL, "erin") is None


def test_readwrite_is_a_subset_of_the_owner_and_inside_the_boundary(env):
    assert share(env.user, "bob", "readwrite").status_code == 201
    st = stmt(env, "bucketeer-share-i-s3-bob")
    assert st["Action"] == SHARE_READ_ACTIONS + SHARE_WRITE_ACTIONS
    assert set(st["Action"]) <= set(BUCKET_ACTIONS + OBJECT_ACTIONS)
    forbidden = {"s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketAcl",
                 "s3:PutObjectAcl", "s3:PutLifecycleConfiguration", "s3:DeleteBucket",
                 "s3:PutBucketTagging", "s3:*"}
    assert not set(st["Action"]) & forbidden
    # every action is one the person boundary of tools/aws-setup allows, on the right resource
    script = (ROOT / "tools" / "aws-setup").read_text()
    body = script[script.index("boundary_policy() {"):script.index("admin_policy() {")]

    def allowed(sid):
        part = body[body.index(f'"Sid": "{sid}"'):]
        part = part[:part.index('"Resource"')]
        return set(re.findall(r'"(s3:[A-Za-z]+)"', part))

    bucket_level = {"s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketVersions",
                    "s3:ListBucketMultipartUploads"}
    assert set(st["Action"]) & bucket_level <= allowed("BucketeerBuckets")
    assert set(st["Action"]) - bucket_level <= allowed("BucketeerObjects")


def test_sids_are_unique_and_safe():
    assert share_sid("s3-j.doe_x") == "bucketeer-share-i-s3-j.doe_x"
    odd = share_sid("s3-a@b")
    assert re.fullmatch(r"bucketeer-share-h-[0-9a-f]{32}", odd) and odd != share_sid("s3-a-b")
    assert share_sid("s3-erin") != sharing.s3docs.share_sid("s3-erin")  # never an on-prem Sid


def test_a_fresh_iam_user_is_retried_until_aws_accepts_it(env, monkeypatch):
    monkeypatch.setattr(cloud, "GRANT_WAITS", (1.0, 1.0))
    slept = []

    async def sleep(s):
        slept.append(s)
    monkeypatch.setattr(cloud, "_sleep", sleep)
    real, calls = env.cloud._write_bucket_policy, []

    async def write(bucket, doc):
        calls.append(bucket)
        if len(calls) == 1:
            raise cloud.CloudError(400, "MalformedPolicy", "x", "Invalid principal in policy")
        await real(bucket, doc)
    monkeypatch.setattr(env.cloud, "_write_bucket_policy", write)
    assert share(env.user, "erin").status_code == 201
    assert len(calls) == 2 and slept == [1.0] and stmt(env, "bucketeer-share-i-s3-erin")


# -- refusals -------------------------------------------------------------------------------


def test_a_foreign_iam_user_is_refused(env):
    env.iam.create_user(UserName="s3-erin")  # made outside Bucketeer (decision 17)
    r = share(env.user, "erin")
    assert r.status_code == 409 and "did not make" in r.json()["detail"]
    assert "bucketeer-share-i-s3-erin" not in [s["Sid"] for s in policy(env)["Statement"]]
    assert env.db.shares.list_live(None, None, "cloud") == []
    [a] = acts(env, "cloud.user.refused")
    assert a["actor"] == "alice" and a["target"] == "s3-erin"
    assert acts(env, "cloud.bucket.share") == [] and env.mail.calls == []


@pytest.mark.parametrize("who", ["carol", "ghost", "alice@example.org"])
def test_not_eligible_unknown_and_ambiguous_are_a_uniform_404(env, who):
    r = share(env.user, who)
    assert r.status_code == 404 and r.json()["detail"] == sharing.MSG_NOT_FOUND
    assert len(policy(env)["Statement"]) == 1


def test_yourself_and_a_suspended_recipient_are_refused(env):
    r = share(env.user, "alice")
    assert r.status_code == 409 and "you" in r.json()["detail"]
    env.mock.state.croit.users["bob"].suspended = True
    r = share(env.user, "bob")
    assert r.status_code == 409 and "suspended" in r.json()["detail"]
    assert len(policy(env)["Statement"]) == 1 and env.db.cloud_accounts.get("bob") is None


def test_lookups_share_the_on_prem_rate_limit(env):
    half = sharing.LOOKUP_LIMIT // 2
    for _ in range(half):  # on premises ...
        assert env.user.get("/api/buckets/alice-data/share-lookup",
                            params={"q": "ghost"}).status_code == 404
    for _ in range(sharing.LOOKUP_LIMIT - half):  # ... and in the cloud count together
        assert env.user.get(f"{B}/scans/share-lookup", params={"q": "ghost"}).status_code == 404
    assert env.user.get(f"{B}/scans/share-lookup", params={"q": "erin"}).status_code == 429
    assert share(env.user, "erin").status_code == 429
    assert env.user.get("/api/buckets/alice-data/share-lookup",
                        params={"q": "erin"}).status_code == 429


def test_lookup(env):
    r = env.user.get(f"{B}/scans/share-lookup", params={"q": "erin@example.org"})
    assert r.json() == {"username": "erin", "displayName": "Erin E", "isYou": False}
    assert env.user.get(f"{B}/nope/share-lookup", params={"q": "erin"}).status_code == 404


def test_gating(env):
    assert env.anon.get(f"{B}/scans/shares").status_code == 401
    assert env.user.post(f"{B}/scans/shares", json={"login": "erin", "access": "read"}
                         ).status_code == 403  # CSRF
    env.cloud.last_check = {**env.cloud.last_check, "ok": False}
    assert env.user.get(f"{B}/scans/shares").status_code == 503
    assert share(env.user, "erin").status_code == 503
    assert env.user.get("/api/cloud/shared-with-me").status_code == 503


def test_another_user_cannot_see_or_change_the_shares(env):
    sid = share(env.user, "erin").json()["id"]
    bob = env.admin
    assert bob.get(f"{B}/scans/shares").status_code == 404
    assert share(bob, "dana").status_code == 404
    assert bob.put(f"{B}/scans/shares/{sid}", json={"access": "readwrite"},
                   headers=H).status_code == 404
    assert bob.delete(f"{B}/scans/shares/{sid}", headers=H).status_code == 404
    # bob's own cloud bucket of the same short name does not reach alice's share row
    assert bob.post(B, json=NEW, headers=H).status_code == 201
    assert bob.put(f"{B}/scans/shares/{sid}", json={"access": "readwrite"},
                   headers=H).status_code == 404
    assert bob.delete(f"{B}/scans/shares/{sid}", headers=H).status_code == 404
    assert bob.get(f"{B}/scans/shares").json()["shares"] == []
    assert env.db.shares.get(sid)["revoked_at"] is None
    # the on-prem routes never touch a cloud share row
    assert env.user.delete(f"/api/buckets/{FULL}/shares/{sid}", headers=H).status_code == 404


# -- update, list states, revoke -----------------------------------------------------------


def test_update_changes_the_statement_and_sharing_again_updates(env):
    sid = share(env.user, "erin").json()["id"]
    r = env.user.put(f"{B}/scans/shares/{sid}", json={"access": "readwrite"}, headers=H)
    assert r.status_code == 200 and r.json()["access"] == "readwrite"
    assert stmt(env, "bucketeer-share-i-s3-erin")["Action"] == \
        SHARE_READ_ACTIONS + SHARE_WRITE_ACTIONS
    [a] = acts(env, "cloud.bucket.share_update")
    assert a["details"]["before"] == "read" and a["details"]["after"] == "readwrite"
    r = share(env.user, "erin", "read")  # the same person again: changed, no new row or email
    assert r.status_code == 200 and r.json()["updated"] is True and r.json()["id"] == sid
    assert stmt(env, "bucketeer-share-i-s3-erin")["Action"] == SHARE_READ_ACTIONS
    assert len(env.db.shares.list_live(FULL, None, "cloud")) == 1 and len(env.mail.calls) == 1


def test_list_states_repair_and_unknown_statements(env):
    sid = share(env.user, "erin").json()["id"]
    body = shares(env.user)
    assert body["checked"] is True and [s["status"] for s in body["shares"]] == ["active"]
    # someone widened the statement outside Bucketeer: out of sync until repaired
    doc = policy(env)
    for s in doc["Statement"]:
        if s["Sid"] == "bucketeer-share-i-s3-erin":
            s["Action"] = [*s["Action"], "s3:PutObject"]
    # and a share statement Bucketeer has no row for
    doc["Statement"].append({"Sid": "bucketeer-share-i-s3-zed", "Effect": "Allow",
                             "Principal": {"AWS": arn(env, "s3-zed")},
                             "Action": SHARE_READ_ACTIONS + SHARE_WRITE_ACTIONS,
                             "Resource": [f"arn:aws:s3:::{FULL}/*"]})
    env.s3.put_bucket_policy(Bucket=FULL, Policy=json.dumps(doc))
    by = {s["id"]: s for s in shares(env.user)["shares"]}
    assert by[sid]["status"] == "out_of_sync"
    unknown = by["bucketeer-share-i-s3-zed"]
    assert (unknown["status"], unknown["iamUser"], unknown["access"]) == (
        "unknown", "s3-zed", "readwrite")
    assert env.user.put(f"{B}/scans/shares/{sid}", json={"access": "read"},
                        headers=H).status_code == 200
    assert shares(env.user)["shares"][0]["status"] == "active"
    r = env.user.delete(f"{B}/scans/shares/bucketeer-share-i-s3-zed", headers=H)
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert stmt(env, "bucketeer-share-i-s3-zed") is None
    assert [s["id"] for s in shares(env.user)["shares"]] == [sid]
    # a policy AWS can't give back: the list says so instead of guessing
    async def broken(bucket):
        raise cloud.CloudError(503, "ServiceUnavailable", "x")
    env.cloud._bucket_policy = broken
    body = shares(env.user)
    assert body["checked"] is False and body["shares"][0]["status"] == "unchecked"


def test_update_when_the_recipient_iam_user_is_gone(env):
    sid = share(env.user, "erin").json()["id"]
    env.iam.delete_user_policy(UserName="s3-erin", PolicyName="bucketeer-buckets")
    env.iam.delete_user(UserName="s3-erin")
    r = env.user.put(f"{B}/scans/shares/{sid}", json={"access": "readwrite"}, headers=H)
    assert r.status_code == 409 and "Share the bucket with them again" in r.json()["detail"]
    assert env.db.shares.get(sid)["access"] == "read"


def test_revoke_removes_the_statement_then_the_row(env):
    sid = share(env.user, "erin").json()["id"]
    share(env.user, "bob", "readwrite")
    r = env.user.delete(f"{B}/scans/shares/{sid}", headers=H)
    assert r.status_code == 200 and r.json() == {"id": sid, "revoked": True}
    sids = [s["Sid"] for s in policy(env)["Statement"]]
    assert sids == ["bucketeer-owner", "bucketeer-share-i-s3-bob"]
    assert env.db.shares.get(sid)["revoked_at"] is not None
    [a] = acts(env, "cloud.bucket.unshare")
    assert a["actor"] == "alice" and a["details"]["recipient"] == "erin"
    assert a["details"]["iamUser"] == "s3-erin"
    # again: idempotent, nothing written
    assert env.user.delete(f"{B}/scans/shares/{sid}", headers=H).json()["revoked"] is True
    assert len(acts(env, "cloud.bucket.unshare")) == 1
    assert env.user.delete(f"{B}/scans/shares/other", headers=H).status_code == 404


# -- bucket delete -------------------------------------------------------------------------


def test_deleting_an_empty_bucket_revokes_its_shares(env):
    sid = share(env.user, "erin").json()["id"]
    r = env.user.request("DELETE", f"{B}/scans", headers=H, json={"confirmName": "scans"})
    assert r.status_code == 200
    assert env.db.shares.get(sid)["revoked_at"] is not None
    [a] = acts(env, "cloud.bucket.unshare")
    assert a["details"]["reason"] == "bucket_deleted" and a["target"] == FULL


def test_a_delete_job_blocks_new_shares_and_revokes_them_when_done(env, monkeypatch):
    monkeypatch.setattr(cloud_delete, "KEY_WAITS", ())
    sid = share(env.user, "erin").json()["id"]
    env.s3.put_object(Bucket=FULL, Key="k", Body=b"x")
    r = env.user.request("DELETE", f"{B}/scans", headers=H, json={"confirmName": "scans"})
    assert r.status_code == 202
    job = r.json()["job"]["id"]
    r = share(env.user, "dana")
    assert r.status_code == 409 and "being deleted" in r.json()["detail"]
    assert env.user.put(f"{B}/scans/shares/{sid}", json={"access": "readwrite"},
                        headers=H).status_code == 409
    asyncio.run(bucket_delete.run_job(env.app, job))
    assert env.db.delete_jobs.get(job)["state"] == "done"
    assert env.db.shares.get(sid)["revoked_at"] is not None
    assert [a["details"]["reason"] for a in acts(env, "cloud.bucket.unshare")] == [
        "bucket_deleted"]


# -- recipient side ------------------------------------------------------------------------


def test_shared_with_me_lists_cloud_shares_only_in_cloud(env):
    share(env.user, "erin", "readwrite")
    erin = env.session("erin")
    body = erin.get("/api/cloud/shared-with-me").json()
    assert body["location"] == "cloud" and body["profile"] == "s3-cloud"
    assert body["region"] == "us-west-2"
    [s] = body["shares"]
    assert s["bucket"] == FULL and s["access"] == "readwrite" and s["owner"] and s["sharedAt"]
    assert env.user.get("/api/cloud/shared-with-me").json()["shares"] == []  # not alice's
    # the on-premises list neither shows nor revokes it
    assert erin.get("/api/shared-with-me").json()["shares"] == []
    assert env.db.shares.list_live(FULL, None, "cloud")[0]["revoked_at"] is None
    # on premises: nothing in the cloud list
    env.db.settings.set("location", {"default": "onprem"}, "test")
    body = erin.get("/api/cloud/shared-with-me").json()
    assert body["location"] == "onprem" and body["shares"] == []


def test_shared_with_me_leaves_out_buckets_bucketeer_no_longer_has(env):
    share(env.user, "erin")
    env.db.buckets.delete(FULL, "cloud")  # e.g. removed outside Bucketeer
    erin = env.session("erin")
    assert erin.get("/api/cloud/shared-with-me").json()["shares"] == []
    assert env.db.shares.list_live(FULL, None, "cloud")  # a GET changes nothing


def test_no_email_when_mail_is_off(env):
    env.app.state.mailer = type("Off", (), {"enabled": False})()
    assert share(env.user, "erin").json()["emailed"] is None


def test_invitation_text_for_a_cloud_bucket(tmp_path):
    from bucketeer.mailer import Mailer

    from .test_auth import make_settings
    s = make_settings(tmp_path, smtp_from="portal@example.org",
                      app_url="https://portal.example.org")
    msg = Mailer(s)._build("erin@example.org", "Alice A", FULL, "readwrite", "cloud")
    body = msg.get_content()
    assert msg["Subject"] == f"Alice A shared the cloud bucket {FULL} with you"
    assert "Cloud (AWS)" in body and "Set up this computer" in body and FULL in body
    assert "https://portal.example.org/" in body and "read and write" in body
    onprem = Mailer(s)._build("erin@example.org", "Alice A", "data", "read")
    assert "Cloud" not in onprem.get_content()


# -- rebuild -------------------------------------------------------------------------------


def test_rebuild_restores_cloud_share_rows(env, monkeypatch):
    sid = share(env.user, "erin", "readwrite").json()["id"]
    with env.db.tx() as c:
        c.execute("DELETE FROM shares WHERE id = ?", (sid,))

    async def no_onprem(self, b, sem):  # the on-premises side is tested in test_rebuild.py
        return None
    monkeypatch.setattr(rebuild._Run, "read_bucket", no_onprem)
    report = asyncio.run(rebuild.rebuild_from_ceph(env.app, True))
    assert report["added"]["cloud_shares"] == 1
    [row] = env.db.shares.list_live(FULL, None, "cloud")
    assert (row["recipient_ad"], row["recipient_uid"], row["recipient_iam"], row["access"],
            row["owner_uid"], row["created_by"]) == (
        "erin", "erin", "s3-erin", "readwrite", "alice", "system")
    # fill gaps again: it exists now
    report = asyncio.run(rebuild.rebuild_from_ceph(env.app, True))
    assert report["added"]["cloud_shares"] == 0
    assert any(s["table"] == "cloud_shares" and s["reason"] == "exists"
               for s in report["skipped"])


# -- frontend ------------------------------------------------------------------------------


def test_ui_reuses_the_share_dialog_for_cloud_buckets():
    js = ROOT / "static" / "js"
    view = (js / "views" / "cloud-buckets.js").read_text()
    assert "shareDialog" in view and "cloudShares" in view
    dialog = (js / "components" / "share-dialog.js").read_text()
    assert "api.list(" in dialog and "api.create(" in dialog and "wording" in dialog
    swm = (js / "views" / "shared-with-me.js").read_text()
    assert "cloudSharedWithMe" in swm and "--profile" in swm
    api = (js / "api.js").read_text()
    assert "/cloud/shared-with-me" in api and "/cloud/buckets/" in api
    for text in (view, dialog, swm):
        assert "innerHTML" not in text and "fetch(" not in text
