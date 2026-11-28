"""Cloud (AWS) users and keys, P6-9: IAM user per person (path, boundary, owner tag), refusal
of users Bucketeer did not make, the inline policy, keys (limit 2, rotate, one-time secret),
audit, offboarding and admin (de)activation, the cloud setup script. moto `mock_aws`, no
network. moto's GetUser omits PermissionsBoundary, so `boundary_session` records the boundary
sent with CreateUser and adds it to GetUser answers, as AWS does."""

import asyncio
import json
import subprocess

import boto3
import pytest
from moto import mock_aws

from bucketeer import cloud, cloud_users, offboarding
from bucketeer.cloud import CloudAdmin, CloudError

from .test_admin import Env, H
from .test_auth import make_settings
from .test_offboarding import DN
from .test_setup_script import V, run_sh

ACCESS = "AKIAEXAMPLEKEY000001"
SECRET = "cloud-secret-for-tests-only"
CLOUD = {"cloud_user_prefix": "s3-", "cloud_admin_access_key": ACCESS,
         "cloud_admin_secret_key": SECRET}
C = "/api/cloud/credentials"
BOUNDARY_DOC = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListAllMyBuckets"], "Resource": "*"}]}


@pytest.fixture(autouse=True)
def no_cloud_monitor(monkeypatch):
    monkeypatch.setattr(cloud, "CHECK_ON_START", False)


def boundary_session(created: dict, endpoint: str | None = None):
    """Session factory for CloudAdmin; `endpoint` points every client at a moto server."""
    def factory(**kw):
        session = boto3.session.Session(**kw)
        real = session.client

        def client(service, **ckw):
            c = real(service, **ckw, **({"endpoint_url": endpoint} if endpoint else {}))
            if service == "iam":
                def remember(params, **_):
                    created[params["UserName"]] = params.get("PermissionsBoundary")

                def add(parsed, **_):
                    u = parsed.get("User") or {}
                    if created.get(u.get("UserName")):
                        u["PermissionsBoundary"] = {
                            "PermissionsBoundaryType": "Policy",
                            "PermissionsBoundaryArn": created[u["UserName"]]}

                c.meta.events.register("provide-client-params.iam.CreateUser", remember)
                c.meta.events.register("after-call.iam.GetUser", add)
            return c

        session.client = client
        return session
    return factory


def iam():
    return boto3.client("iam", region_name="us-west-2", aws_access_key_id="x",
                        aws_secret_access_key="y")


@pytest.fixture
def env(tmp_path):
    with mock_aws():
        e = Env(tmp_path)
        e.created = {}
        e.iam = iam()
        e.iam.create_policy(PolicyName="BucketeerUserBoundary", Path="/bucketeer/",
                            PolicyDocument=json.dumps(BOUNDARY_DOC))
        e.cloud = CloudAdmin(make_settings(tmp_path, **CLOUD),
                             session_factory=boundary_session(e.created))
        asyncio.run(e.cloud.check())
        e.app.state.cloud = e.cloud
        yield e
        e.db.close()


def acts(env, prefix):
    return [a for a in env.db.audit.query(limit=1000) if a["action"].startswith(prefix)]


def statuses(env, name="s3-alice"):
    return {k["AccessKeyId"]: k["Status"]
            for k in env.iam.list_access_keys(UserName=name)["AccessKeyMetadata"]}


# -- the IAM user ----------------------------------------------------------------------


def test_gating(env):
    assert env.anon.get(C).status_code == 401
    assert env.user.post(f"{C}/keys").status_code == 403  # CSRF header missing
    env.app.state.cloud = None
    assert env.user.get(C).status_code == 503
    env.app.state.cloud = env.cloud
    env.cloud.last_check = {**env.cloud.last_check, "ok": False}
    assert env.user.post(f"{C}/keys", headers=H).status_code == 503
    assert env.iam.list_users()["Users"] == []


def test_get_creates_nothing(env):
    r = env.user.get(C)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"profile": "s3-cloud", "region": "us-west-2", "iamUser": None,
                        "keys": [], "maxKeys": 2, "maxKeyAgeDays": 180}
    assert env.iam.list_users()["Users"] == []


def test_first_key_creates_the_user_with_path_boundary_and_tag(env):
    r = env.user.post(f"{C}/keys", headers=H)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["secretKey"] and body["oneTime"] is True and body["status"] == "Active"
    u = env.iam.get_user(UserName="s3-alice")["User"]
    assert u["Path"] == "/bucketeer/"
    account = boto3.client("sts", region_name="us-west-2", aws_access_key_id="x",
                           aws_secret_access_key="y").get_caller_identity()["Account"]
    assert env.created["s3-alice"] == (
        f"arn:aws:iam::{account}:policy/bucketeer/BucketeerUserBoundary")
    tags = {t["Key"]: t["Value"] for t in env.iam.list_user_tags(UserName="s3-alice")["Tags"]}
    assert tags["bucketeer-owner"] == "alice"
    row = env.db.cloud_accounts.get("Alice")
    assert row["iam_user"] == "s3-alice" and row["source"] == "created"
    assert row["policy_synced"] == 1
    doc = env.iam.get_user_policy(UserName="s3-alice", PolicyName="bucketeer-buckets")
    stmts = doc["PolicyDocument"]["Statement"]
    assert [s["Sid"] for s in stmts] == ["ListBuckets"]  # access comes from bucket policies
    [create] = acts(env, "cloud.user.create")
    assert create["target_type"] == "cloud_user" and create["target"] == "s3-alice"
    [key] = acts(env, "cloud.key.create")
    assert key["target"] == body["accessKey"] and key["target_type"] == "access_key"
    # GET lists the key, never a secret
    g = env.user.get(C)
    assert g.json()["iamUser"] == "s3-alice"
    assert [k["accessKey"] for k in g.json()["keys"]] == [body["accessKey"]]
    assert body["secretKey"] not in g.text and "secret" not in g.text.lower()
    assert body["secretKey"] not in json.dumps(env.db.audit.query(limit=1000))


@pytest.mark.parametrize("how", ["outside_path", "no_boundary", "other_owner"])
def test_a_user_bucketeer_did_not_make_is_refused(env, how):
    if how == "outside_path":
        env.iam.create_user(UserName="s3-alice")
    elif how == "no_boundary":
        env.iam.create_user(UserName="s3-alice", Path="/bucketeer/",
                            Tags=[{"Key": "bucketeer-owner", "Value": "alice"}])
    else:
        asyncio.run(env.cloud.create_person_user("s3-alice", "mallory"))
    r = env.user.post(f"{C}/keys", headers=H)
    assert r.status_code == 409 and "administrator" in r.json()["detail"]
    assert env.iam.list_access_keys(UserName="s3-alice")["AccessKeyMetadata"] == []
    assert env.db.cloud_accounts.get("alice") is None
    [refused] = acts(env, "cloud.user.refused")
    assert refused["details"]["reason"] == "not_ours"
    # and the setup link with cloud says so too, before any command exists
    _set_location(env, "cloud")
    r = env.user.post("/api/credentials/setup-link", json={"os": "linux"}, headers=H)
    assert r.status_code == 409


def test_an_unrecorded_user_of_ours_is_recovered(env):
    asyncio.run(cloud_users.ensure_account(env.app, "alice"))
    with env.db.tx() as c:
        c.execute("DELETE FROM cloud_accounts")
    assert env.user.post(f"{C}/keys", headers=H).status_code == 201
    assert env.db.cloud_accounts.get("alice")["source"] == "recovered"


def test_reserved_and_bad_names(env):
    for name in ("bucketeer-admin", "bucketeer-backup", "anonymous", "x" * 70, "Bad Name"):
        with pytest.raises(cloud_users.CloudRefused):
            asyncio.run(cloud_users.check_name(env.cloud, name))
    assert asyncio.run(cloud_users.check_name(env.cloud, "J.Doe")) == "s3-j.doe"


# -- the policy ------------------------------------------------------------------------


def test_inline_policy_has_no_bucket_wildcard(env):
    asyncio.run(cloud_users.ensure_account(env.app, "jdoe"))
    asyncio.run(cloud_users.ensure_account(env.app, "jdoe-x"))
    for name in ("s3-jdoe", "s3-jdoe-x"):
        [stmt] = env.iam.get_user_policy(UserName=name, PolicyName="bucketeer-buckets")[
            "PolicyDocument"]["Statement"]
        assert stmt["Effect"] == "Allow" and stmt["Resource"] == "*"
        assert sorted(stmt["Action"]) == ["s3:GetBucketLocation", "s3:ListAllMyBuckets"]


def test_bucket_owner_statement_keeps_foreign_statements(env):
    c = env.cloud
    s3 = boto3.client("s3", region_name="us-west-2", aws_access_key_id="x",
                      aws_secret_access_key="y")
    bucket = "bucketeer-jdoe-x-data"  # jdoe-x's bucket; nothing about it reaches jdoe
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    asyncio.run(cloud_users.ensure_account(env.app, "jdoe-x"))
    arn = asyncio.run(c.user_arn("s3-jdoe-x"))
    assert arn.endswith(":user/bucketeer/s3-jdoe-x")
    foreign = {"Sid": "team", "Effect": "Allow", "Principal": {"AWS": arn},
               "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"}
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(
        {"Version": "2012-10-17", "Statement": [foreign]}))
    asyncio.run(c.grant_bucket_owner(bucket, arn))
    asyncio.run(c.grant_bucket_owner(bucket, arn))  # idempotent: one owner statement
    doc = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    sids = [s["Sid"] for s in doc["Statement"]]
    assert sorted(sids) == ["bucketeer-owner", "team"]
    owner = next(s for s in doc["Statement"] if s["Sid"] == "bucketeer-owner")
    assert owner["Principal"] == {"AWS": arn}
    assert owner["Resource"] == [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]
    assert "s3:PutObject" in owner["Action"] and "s3:DeleteBucket" not in owner["Action"]
    assert "s3:PutBucketPolicy" not in owner["Action"]
    asyncio.run(c.revoke_bucket_owner(bucket))
    doc = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    assert [s["Sid"] for s in doc["Statement"]] == ["team"]
    s3.delete_bucket_policy(Bucket=bucket)
    asyncio.run(c.grant_bucket_owner(bucket, arn))
    asyncio.run(c.revoke_bucket_owner(bucket))  # the last statement: the policy goes
    with pytest.raises(Exception, match="NoSuchBucketPolicy"):
        s3.get_bucket_policy(Bucket=bucket)
    with pytest.raises(ValueError):
        asyncio.run(c.grant_bucket_owner("someone-elses-bucket", arn))


# -- keys ------------------------------------------------------------------------------


def test_key_limit_rotate_and_delete(env):
    k1 = env.user.post(f"{C}/keys", headers=H).json()["accessKey"]
    r = env.user.post(f"{C}/keys/{k1}/rotate", headers=H)
    assert r.status_code == 200 and r.json()["oldKeyRemoved"] is True and r.json()["secretKey"]
    k2 = r.json()["accessKey"]
    assert set(statuses(env)) == {k2}
    r = env.user.delete(f"{C}/keys/{k2}", headers=H)
    assert r.status_code == 409  # the last key
    k3 = env.user.post(f"{C}/keys", headers=H).json()["accessKey"]
    r = env.user.post(f"{C}/keys", headers=H)
    assert r.status_code == 409 and "2" in r.json()["detail"]  # AWS's limit
    assert env.user.post(f"{C}/keys/{k3}/rotate", headers=H).status_code == 409  # no room
    assert env.user.post(f"{C}/keys/NOTMINE/rotate", headers=H).status_code == 404
    assert env.user.delete(f"{C}/keys/NOTMINE", headers=H).status_code == 404
    assert env.user.delete(f"{C}/keys/{k3}", headers=H).status_code == 200
    assert set(statuses(env)) == {k2}
    # bob can't touch alice's keys
    assert env.admin.delete(f"{C}/keys/{k2}", headers=H).status_code == 404
    assert [a["action"] for a in reversed(acts(env, "cloud.key."))] == [
        "cloud.key.create", "cloud.key.rotate", "cloud.key.create", "cloud.key.delete"]


def test_aws_down_is_503(env, monkeypatch):
    env.user.post(f"{C}/keys", headers=H)

    async def down(*_a, **_k):
        raise cloud.CloudUnavailable()

    monkeypatch.setattr(env.cloud, "list_access_keys", down)
    r = env.user.get(C)
    assert r.status_code == 503 and SECRET not in r.text


# -- suspension ------------------------------------------------------------------------


def test_admin_suspend_deactivates_and_unsuspend_reactivates(env):
    k = env.user.post(f"{C}/keys", headers=H).json()["accessKey"]
    assert env.admin.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    assert statuses(env) == {k: "Inactive"}
    assert env.db.cloud_key_suspensions.for_user("alice")[0]["suspended_by"] == "owner"
    [a] = acts(env, "admin.user.suspend")
    assert f"cloud:{k}" in a["details"]["keys"]
    assert env.admin.post("/api/admin/users/alice/unsuspend", headers=H).status_code == 200
    assert statuses(env) == {k: "Active"}
    assert env.db.cloud_key_suspensions.for_user("alice") == []


def test_admin_suspend_reports_aws_failure(env, monkeypatch):
    env.user.post(f"{C}/keys", headers=H)

    async def refuse(*_a, **_k):
        raise CloudError(500, "ServiceFailure", "AWS refused UpdateAccessKey (ServiceFailure)")

    monkeypatch.setattr(env.cloud, "set_access_key_status", refuse)
    r = env.admin.post("/api/admin/users/alice/suspend", headers=H)
    assert r.status_code == 502 and "cloud:" in r.json()["detail"]
    assert env.mock.state.croit.users["alice"].suspended  # the account itself is suspended


def test_offboarding_deactivates_and_login_reactivates(env):
    k = env.user.post(f"{C}/keys", headers=H).json()["accessKey"]
    env.user.get("/api/account")
    env.admin.get("/api/account")
    env.db.settings.set("offboarding", {**offboarding.DEFAULT_SETTINGS, "maxPercent": 100,
                                        "mode": "enforce"})
    alice = env.fake.users[DN.format("alice")]
    alice["userAccountControl"] = "514"
    run = asyncio.run(env.app.state.offboarding.run("manual", "bob"))
    res = {r["uid"]: r for r in run["results"]}["alice"]
    assert res["action"] == "suspended" and f"cloud:{k}" in res["keys"]
    assert statuses(env) == {k: "Inactive"}
    assert env.db.cloud_key_suspensions.for_user("alice")[0]["suspended_by"] == "offboarding"
    # a key deactivated by hand outside the job's record is never woken by the login
    alice["userAccountControl"] = "512"

    async def back():
        acct = await env.app.state.rgw.resolve("alice")
        await env.app.state.offboarding.on_login("alice", acct)

    asyncio.run(back())
    assert statuses(env) == {k: "Active"}
    assert env.db.cloud_key_suspensions.for_user("alice") == []
    assert acts(env, "offboarding.unsuspend")


def test_offboarding_cloud_failure_is_reported_and_finished_later(env, monkeypatch):
    env.user.post(f"{C}/keys", headers=H)
    env.admin.get("/api/account")
    env.db.settings.set("offboarding", {**offboarding.DEFAULT_SETTINGS, "maxPercent": 100,
                                        "mode": "enforce"})
    env.fake.users[DN.format("alice")]["userAccountControl"] = "514"
    real = env.cloud.set_access_key_status

    async def down(*_a, **_k):
        raise cloud.CloudUnavailable()

    monkeypatch.setattr(env.cloud, "set_access_key_status", down)
    run = asyncio.run(env.app.state.offboarding.run("manual", "bob"))
    res = {r["uid"]: r for r in run["results"]}["alice"]
    assert res["action"] == "partial" and "cloud:" in res["note"]
    assert run["outcome"] == "failed" and env.mock.state.croit.users["alice"].suspended
    monkeypatch.setattr(env.cloud, "set_access_key_status", real)
    run = asyncio.run(env.app.state.offboarding.run("manual", "bob"))
    res = {r["uid"]: r for r in run["results"]}["alice"]
    assert res["action"] == "keys_completed" and set(statuses(env).values()) == {"Inactive"}


# -- setup script ----------------------------------------------------------------------


def setup_secret(env):
    for k in env.mock.state.croit.users["alice"].keys:
        k["secret_key"] = "AliceSetupSecret0001"


def _set_location(env, location):
    r = env.user.put("/api/profile", json={"location": location}, headers=H)
    assert r.status_code == 200 and r.json()["location"] == location


def test_setup_on_cloud_installs_only_the_cloud_profile(env, tmp_path):
    setup_secret(env)
    _set_location(env, "cloud")
    r = env.user.post("/api/credentials/setup-link", json={"os": "linux"}, headers=H)
    assert r.status_code == 200 and r.json()["location"] == "cloud"
    path = r.json()["url"].replace("https://testserver", "")
    text = env.anon.post(path).text
    [key] = env.iam.list_access_keys(UserName="s3-alice")["AccessKeyMetadata"]
    # cloud only: no on-premises profile, no endpoint, no on-premises key
    assert "[profile s3-cloud]" in text and "[profile s3-onprem]" not in text
    assert "endpoint_url" not in text and "AliceSetupSecret0001" not in text
    assert key["AccessKeyId"] in text
    [setup] = [a for a in acts(env, "credentials.setup") if a["action"] == "credentials.setup"]
    assert setup["details"]["cloudKey"] == key["AccessKeyId"]
    assert setup["details"]["location"] == "cloud"
    # the script really writes the cloud section only
    script = tmp_path / "s.sh"
    script.write_text(text)
    out = subprocess.run(["/bin/sh", str(script)], env={"HOME": str(tmp_path),
                         "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    cred = (tmp_path / ".aws/credentials").read_text()
    assert "[s3-cloud]" in cred and "[s3-onprem]" not in cred and key["AccessKeyId"] in cred
    cfg = (tmp_path / ".aws/config").read_text()
    assert "endpoint_url" not in cfg and "when_required" in cfg
    assert "The AWS profile 's3-cloud' is installed." in out.stdout


def test_setup_on_premises_installs_only_the_on_premises_profile(env):
    setup_secret(env)
    _set_location(env, "onprem")
    r = env.user.post("/api/credentials/setup-link", json={"os": "linux"}, headers=H)
    assert r.status_code == 200 and r.json()["location"] == "onprem"
    text = env.anon.post(r.json()["url"].replace("https://testserver", "")).text
    assert "[profile s3-onprem]" in text and "[profile s3-cloud]" not in text
    assert env.db.cloud_accounts.get("alice") is None  # no cloud account made


def test_old_cloud_flag_is_ignored_on_premises(env):
    setup_secret(env)
    _set_location(env, "onprem")
    r = env.user.post("/api/credentials/setup-link", json={"os": "linux", "cloud": True},
                      headers=H)
    assert r.status_code == 200 and r.json()["location"] == "onprem"
    text = env.anon.post(r.json()["url"].replace("https://testserver", "")).text
    assert "[profile s3-onprem]" in text and "[profile s3-cloud]" not in text


def test_refused_cloud_setup_audits_the_rgw_user(env):
    setup_secret(env)
    _set_location(env, "cloud")
    r = env.user.post("/api/credentials/setup-link", json={"os": "linux"}, headers=H)
    assert r.status_code == 200 and r.json()["location"] == "cloud"
    env.cloud.last_check = {**env.cloud.last_check, "ok": False}  # cloud goes away
    text = env.anon.post(r.json()["url"].replace("https://testserver", "")).text
    assert "Cloud storage is not available" in text
    [refused] = acts(env, "credentials.setup_refused")
    assert refused["target_type"] == "rgw_user" and refused["target"] == "alice"
    assert refused["details"]["location"] == "cloud"
    assert refused["details"]["reason"] == "cloud_unavailable"


def test_setup_with_cloud_makes_room_at_two_keys(env):
    setup_secret(env)
    env.user.post(f"{C}/keys", headers=H)
    env.user.post(f"{C}/keys", headers=H)
    first = sorted(statuses(env))
    _set_location(env, "cloud")
    r = env.user.post("/api/credentials/setup-link", json={"os": "macos"}, headers=H)
    env.anon.post(r.json()["url"].replace("https://testserver", ""))
    keys = statuses(env)
    assert len(keys) == 2 and len(set(keys) & set(first)) == 1
    assert any(a["details"].get("reason") == "make_room" for a in acts(env, "cloud.key.delete"))


def test_setup_without_cloud_is_unchanged(env):
    setup_secret(env)
    r = env.user.post("/api/credentials/setup-link", json={"os": "linux"}, headers=H)
    text = env.anon.post(r.json()["url"].replace("https://testserver", "")).text
    assert "s3-cloud" not in text and env.iam.list_users()["Users"] == []


def test_sh_script_cloud_section(tmp_path):
    from bucketeer.setup_script import CloudValues, sh_script
    cv = CloudValues("s3-cloud", "us-west-2", "AKIACLOUD0000000001", "cl0ud/Secret+Value=")
    assert cv.valid() and not CloudValues("s3-cloud", "us-west-2", "AK", "x'y").valid()
    text = sh_script(V, cv)
    assert "[profile s3-cloud]" in text and "AWS is still activating the new key" in text
    assert run_sh(tmp_path).returncode == 0  # on premises alone still works


def test_cloud_panel_keeps_the_secret_in_memory_only():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "static/js/views/cloud-keys.js").read_text()
    for bad in ("localStorage", "sessionStorage", "innerHTML", "console.log", "console.error",
                "setAttribute"):
        assert bad not in js
    assert "You won't see it again" in js


# -- real IAM: GetUser on a name outside /bucketeer/ (or missing) is AccessDenied ---------

def _real_iam_get_user(env, monkeypatch):
    """Make moto answer GetUser like real IAM does under the admin policy (GetUser only on
    user/bucketeer/<prefix>*): a missing user or one under another path is AccessDenied."""
    orig = env.cloud.call

    async def call(service, op, **kw):
        if service == "iam" and op == "get_user":
            try:
                path = env.iam.get_user(UserName=kw["UserName"])["User"]["Path"]
            except env.iam.exceptions.NoSuchEntityException:
                path = None
            if path != "/bucketeer/":
                raise CloudError(403, "AccessDenied", "AWS refused get_user (AccessDenied)")
        return await orig(service, op, **kw)

    monkeypatch.setattr(env.cloud, "call", call)


def test_first_cloud_use_works_when_iam_denies_get_user(env, monkeypatch):
    _real_iam_get_user(env, monkeypatch)
    r = env.user.post(f"{C}/keys", headers=H)
    assert r.status_code == 201, r.text
    user = env.iam.get_user(UserName="s3-alice")["User"]
    assert user["Path"] == "/bucketeer/"
    assert env.db.cloud_accounts.get("alice")["iam_user"] == "s3-alice"


def test_first_cloud_setup_link_works_when_iam_denies_get_user(env, monkeypatch):
    _real_iam_get_user(env, monkeypatch)
    _set_location(env, "cloud")
    r = env.user.post("/api/credentials/setup-link", json={"os": "linux"}, headers=H)
    assert r.status_code == 200 and r.json()["location"] == "cloud", r.text
    assert env.db.cloud_accounts.get("alice") is not None


def test_a_name_taken_outside_our_path_is_refused_when_iam_denies_get_user(env, monkeypatch):
    env.iam.create_user(UserName="s3-alice")  # someone else's, at the root path
    _real_iam_get_user(env, monkeypatch)
    r = env.user.post(f"{C}/keys", headers=H)
    assert r.status_code == 409 and "administrator" in r.json()["detail"]
    assert env.db.cloud_accounts.get("alice") is None
    [refused] = acts(env, "cloud.user.refused")
    assert refused["details"]["reason"] == "not_ours"
