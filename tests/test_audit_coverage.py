"""Every state-changing route is audited, and the audit log never holds a secret.

`ROUTES` is the source of truth: each POST/PUT/PATCH/DELETE route of the app maps to the
audit action(s) it writes. A new mutating route that is not in the table fails
`test_every_mutating_route_is_in_the_table`; one that is in the table but writes no audit
row fails its own parametrized case. `EVENTS` lists the audited things that are not routes
(login failures, background jobs, first-login RGW user creation).

One shared "coverage run" (module fixture) exercises each route once against the mock croit
and moto, remembers which audit rows each step wrote, and the secret scan runs over all rows.
"""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bucketeer import backup, bucket_delete, bucket_quota, cloud_buckets, notices, rebuild
from bucketeer.bucket_keys import cleanup_pending
from bucketeer.cloud import CloudAdmin, CloudError

from .conftest import REQUIRED
from .test_acl import fake_objects
from .test_alerts import AlertMailer
from .test_auth import SECRET_PW, TEAM_GROUP, USERS_GROUP, make_settings
from .test_billing import FakeMailer
from .test_buckets import Env, H
from .test_cloud_users import BOUNDARY_DOC, boundary_session
from .test_cloud_users import CLOUD as CLOUD_SETTINGS

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
S = "/api/admin/settings"

# route -> audit actions the coverage run must see written by that route's step(s).
# An empty tuple means "deliberately writes nothing" (needs a reason in EXEMPT).
ROUTES: dict[str, tuple[str, ...]] = {
    "POST /api/login": ("login.success", "login.failure"),
    "POST /api/logout": ("logout",),
    "PUT /api/profile": ("profile.update",),
    "POST /api/credentials/reveal": ("credentials.reveal",),
    "POST /api/credentials/keys": ("credentials.key.create",),
    "POST /api/credentials/keys/{access_key}/rotate": ("credentials.key.rotate",),
    "DELETE /api/credentials/keys/{access_key}": ("credentials.key.delete",),
    "POST /api/credentials/setup-link": ("credentials.setup_link",),
    "POST /setup/{token}": ("credentials.setup",),
    "POST /api/cloud/credentials/keys": ("cloud.user.create", "cloud.key.create"),
    "POST /api/cloud/credentials/keys/{access_key}/rotate": ("cloud.key.rotate",),
    "DELETE /api/cloud/credentials/keys/{access_key}": ("cloud.key.delete",),
    "POST /api/cloud/buckets": ("cloud.bucket.create",),
    "PUT /api/cloud/buckets/{name}/quota": ("cloud.bucket.quota",),
    "PUT /api/cloud/buckets/{name}/budget": ("cloud.bucket.budget",),
    "DELETE /api/cloud/buckets/{name}": ("cloud.bucket.delete",),
    "POST /api/cloud/buckets/delete-jobs/{job_id}/retry": ("cloud.bucket.delete_retry",),
    "POST /api/subusers": ("subuser.create",),
    "PATCH /api/subusers/{name}": ("subuser.update",),
    "DELETE /api/subusers/{name}": ("subuser.delete",),
    "POST /api/subusers/{name}/keys": ("subuser.key.create",),
    "POST /api/subusers/{name}/keys/{access_key}/rotate": ("subuser.key.rotate",),
    "DELETE /api/subusers/{name}/keys/{access_key}": ("subuser.key.delete",),
    "POST /api/subusers/{name}/reveal": ("subuser.reveal",),
    "POST /api/buckets": ("bucket.create",),
    "POST /api/buckets/reconcile": ("bucket.tag_sync",),
    "PUT /api/buckets/{name}/versioning": ("bucket.versioning",),
    "PUT /api/buckets/{name}/lifecycle": ("bucket.lifecycle",),
    "PUT /api/buckets/{name}/lifecycle/rules": ("bucket.lifecycle_rules",),
    "PUT /api/buckets/{name}/cors": ("bucket.cors",),
    "PUT /api/buckets/{name}/notifications": ("bucket.notifications",),
    "PUT /api/buckets/{name}/tags": ("bucket.tags",),
    "POST /api/buckets/{name}/multipart/abort": ("bucket.multipart_abort",),
    "PUT /api/buckets/{name}/budget": ("bucket.budget",),
    "PUT /api/buckets/{name}/quota": ("bucket.quota",),
    "PUT /api/buckets/{name}/object-lock": ("bucket.object_lock",),
    "PUT /api/buckets/{name}/objects/retention": ("object.retention",),
    "PUT /api/buckets/{name}/objects/legal-hold": ("object.legal_hold",),
    "PUT /api/buckets/{name}/policy": ("bucket.policy",),
    "DELETE /api/buckets/{name}/policy": ("bucket.policy_delete",),
    "POST /api/buckets/{name}/share-link": ("bucket.share_link",),
    "PUT /api/buckets/{name}/acl": ("bucket.acl",),
    "PUT /api/buckets/{name}/objects/acl": ("object.acl",),
    "POST /api/buckets/{name}/shares": ("bucket.share",),
    "PUT /api/buckets/{name}/shares/{share_id}": ("bucket.share_update",),
    "DELETE /api/buckets/{name}/shares/{share_id}": ("bucket.unshare",),
    "DELETE /api/buckets/{name}": ("bucket.delete",),
    "POST /api/buckets/delete-jobs/{job_id}/retry": ("bucket.delete_retry",),
    "POST /api/buckets/{name}/keys": ("bucket_key.create",),
    "POST /api/bucket-keys/{key_id}/reveal": ("bucket_key.reveal",),
    "DELETE /api/bucket-keys/{key_id}": ("bucket_key.revoke",),
    "DELETE /api/admin/bucket-keys/{key_id}": ("admin.bucket_key.revoke",),
    "PUT /api/admin/users/{uid}/quota": ("admin.user.quota",),
    "PUT /api/admin/buckets/{bucket}/quota": ("admin.bucket.quota",),
    "PUT /api/admin/cloud-buckets/{name}/quota": ("admin.bucket.quota",),
    "POST /api/admin/buckets/write-tags": ("admin.buckets.write_tags",),
    "POST /api/admin/users/{uid}/suspend": ("admin.user.suspend",),
    "POST /api/admin/users/{uid}/unsuspend": ("admin.user.unsuspend",),
    "POST /api/admin/offboarding/run": ("offboarding.run",),
    "PUT /api/admin/offboarding/settings": ("admin.settings.offboarding",),
    "PUT /api/admin/billing/settings": ("admin.settings.billing",),
    "POST /api/admin/billing/report/send": ("billing.report_sent",),
    "PUT /api/admin/backup/settings": ("admin.settings.backup",),
    "POST /api/admin/backup/run": ("backup.run",),
    "POST /api/admin/backup/restore-test": ("backup.restore_test",),
    "PUT /api/admin/alerts": ("admin.settings.alerts",),
    f"PUT {S}/naming": ("admin.settings.naming",),
    f"POST {S}/naming/test": (),
    f"PUT {S}/budget-number": ("admin.settings.budget_number",),
    f"POST {S}/budget-number/test": (),
    f"PUT {S}/placement": ("admin.settings.placement",),
    f"PUT {S}/location": ("admin.settings.location",),
    f"PUT {S}/notices": ("admin.settings.notices",),
    f"PUT {S}/bucket-quota": ("admin.settings.bucket_quota",),
    f"PUT {S}/default-quota": ("admin.settings.bucket_quota",),
    f"DELETE {S}/default-quota": ("admin.settings.bucket_quota",),
    f"PUT {S}/uid-mappings/{{ad_username}}": ("admin.settings.uid_mapping.set",),
    f"DELETE {S}/uid-mappings/{{ad_username}}": ("admin.settings.uid_mapping.delete",),
    f"PUT {S}/notification-endpoints/{{name}}": ("admin.settings.notification_endpoint.set",),
    f"DELETE {S}/notification-endpoints/{{name}}": (
        "admin.settings.notification_endpoint.delete",),
}
EXEMPT = {
    f"POST {S}/naming/test": "dry run of a naming rule; stores nothing",
    f"POST {S}/budget-number/test": "dry run of a budget number rule; stores nothing",
}

# Audited outside a plain success path of a route.
EVENTS = {
    "bucket.create_failed": "croit down or task failed while creating a bucket",
    "bucket.delete_failed": "croit down or task failed while deleting a bucket",
    "bucket.versioning_failed": "croit down while changing versioning",
    "bucket.delete_started": "rgw backend: a non-empty bucket's delete job was queued",
    "bucket.delete_superseded": "rgw backend: a job of the bucket's previous owner was ended",
    "bucket.delete_blocked": "rgw backend: object lock or legal hold stopped a delete job",
    "credentials.setup_refused": "a valid setup token whose key, account or endpoint is bad",
    "bucket_key.cleanup": "startup clean-up of stale pending bucket keys (actor system)",
    "bucket.unshare": "deleting a bucket marks its shares revoked (reason bucket_deleted)",
    "offboarding.run_failed": "a run whose croit suspension failed",
    "offboarding.unsuspend_failed": "the login unsuspend could not reach croit (actor system)",
    "offboarding.aborted": "a run aborted by a threshold or an AD error (actor = trigger)",
    "offboarding.suspend": "enforce mode suspends a person who left AD",
    "offboarding.unsuspend": "login undoes the job's suspension (actor system)",
    "backup.failed": "a backup run that could not write its archive",
    "backup.restore_test_failed": "a restore test whose newest archive could not be restored",
    "admin.alert_sent": "an admin alert email (here: the failed backup run; actor system)",
    "admin.alert_failed": "no alert contact's mail server took an alert (actor system)",
    "rgw.user.create": "first S3 use of a new AD user creates the RGW user",
    "rgw.user.quota_unlimited": "one-time step: old default user quota -> unlimited (system)",
    "billing.report_failed": "no billing contact's mail server took the monthly sheet",
    "system.rebuild": "python3 app.py --rebuild-from-ceph (actor system)",
    "cloud.user.refused": "an IAM user with the person's name that Bucketeer did not make",
    "cloud.bucket.create_failed": "a cloud bucket set-up step failed after CreateBucket",
    "cloud.bucket.over_quota": "the daily check found a cloud bucket over its quota (system)",
    "cloud.bucket.delete_started": "a cloud bucket with content: its delete job was queued",
    "cloud.bucket.delete_failed": "a cloud delete job stopped (e.g. the person has two keys)",
    "bucket.near_full_notice": "the daily pass found an on-premises bucket nearly full (system)",
    "credentials.key.age_notice": "the daily pass found an access key older than the limit",
}

ACTION_RE = re.compile(r"^(?:[a-z][a-z_]*)(?:\.[a-z][a-z_]*)+$")
ACTION_EXCEPTIONS = {"logout"}  # pre-dates the scheme; kept so old log rows still match
TARGET_TYPES = {"user", "rgw_user", "bucket", "bucket_key", "access_key", "profile", "setting",
                "uid_mapping", "notification_endpoint", "report", "offboarding_run", "subuser",
                "backup", "database", "cloud_user", "alert"}
ACTORS = {"alice", "bob", "dave", "system", "mallory"}

SECRET_PATTERNS = [
    re.compile(r"Signature=", re.I),
    re.compile(r"X-Amz-(Credential|Security-Token)", re.I),
    re.compile(r"Bearer\s+\S", re.I),
    re.compile(r"(secret|password|passwd|token)[\"']?\s*[:=]\s*[\"'](?!\[redacted\])[^\"']+", re.I),
]


def secret_hits(rows: list[dict], literals: set[str]) -> list[str]:
    """Why any audit row looks like it holds a secret (empty list = clean)."""
    hits = []
    for r in rows:
        text = json.dumps(r, default=str)
        hits += [f"{r['action']}: pattern {p.pattern}" for p in SECRET_PATTERNS if p.search(text)]
        hits += [f"{r['action']}: literal secret" for lit in literals if lit and lit in text]
    return hits


def mutating_routes(app) -> set[str]:
    # The OpenAPI document lists every route, including the ones in included routers
    # (which `app.routes` hides behind a wrapper). No route hides itself from it.
    return {f"{m.upper()} {path}" for path, ops in app.openapi()["paths"].items()
            for m in ops if m.upper() in UNSAFE}


class Run:
    """The coverage run's record: audit actions seen per step, statuses, and the env."""

    def __init__(self, env):
        self.env = env
        self.seen: dict[str, set[str]] = {}
        self.status: dict[str, list[int]] = {}
        self.secrets: set[str] = set()  # every secret ever seen, even after its key is gone

    def collect(self):
        for u in self.env.croit_state.users.values():
            self.secrets |= {k["secret_key"] for k in u.keys if k.get("secret_key")}

    def step(self, route: str, fn):
        before = max((a["id"] for a in self.env.db.audit.query(limit=1)), default=0)
        self.collect()  # keys that exist now may be gone after the step
        res = fn()
        self.collect()
        if isinstance(res, httpx.Response) and "json" in res.headers.get("content-type", ""):
            body = res.json()
            if isinstance(body, dict) and body.get("secretKey"):
                self.secrets.add(body["secretKey"])
        rows = [a for a in self.env.db.audit.query(limit=1000) if a["id"] > before]
        self.seen.setdefault(route, set()).update(a["action"] for a in rows)
        if isinstance(res, httpx.Response):
            self.status.setdefault(route, []).append(res.status_code)
        return res


def stale_pending_row(env):
    uid = "alice+stale"
    row = env.db.bucket_keys.create(uid, "alice", "alice-data", "read", "stale", "pending")
    old = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with env.db.tx() as c:
        c.execute("UPDATE bucket_keys SET created_at = ? WHERE id = ?", (old, row["id"]))


class FakeCloud:
    available = True

    async def check(self):
        return {"ok": True, "accountId": None, "region": "us-west-2", "reason": None,
                "checkedAt": None}


@pytest.fixture(scope="module")
def run(tmp_path_factory, moto_server):
    httpx.post(f"{moto_server.endpoint}/moto-api/reset", timeout=10).raise_for_status()
    tmp = tmp_path_factory.mktemp("cov")
    env = Env(tmp, moto_server.endpoint, backup_bucket="bucketeer-cov0123")
    saved_env_file, backup.ENV_FILE = backup.ENV_FILE, tmp / "no-such.env"
    r = Run(env)
    a, b = env.alice, env.bob
    ok = lambda resp: (resp.raise_for_status(), resp)[1]  # noqa: E731

    # auth
    r.step("POST /api/login", lambda: env.session("alice"))
    r.step("POST /api/login", lambda: env.anon().post(
        "/api/login", json={"login": "mallory", "password": "wrong-pw"}, headers=H))
    r.step("POST /api/logout", lambda: env.session("alice").post("/api/logout", headers=H))

    # profile
    r.step("PUT /api/profile", lambda: a.put(
        "/api/profile", json={"defaultBudgetNumber": "WT-9"}, headers=H))

    # own S3 keys: create, reveal, rotate, delete
    new = r.step("POST /api/credentials/keys", lambda: a.post("/api/credentials/keys", headers=H))
    k1 = new.json()["accessKey"]
    r.step("POST /api/credentials/reveal", lambda: a.post(
        "/api/credentials/reveal", json={"accessKey": k1}, headers=H))
    rot = r.step("POST /api/credentials/keys/{access_key}/rotate", lambda: a.post(
        f"/api/credentials/keys/{k1}/rotate", headers=H))
    k2 = rot.json()["accessKey"]
    r.step("DELETE /api/credentials/keys/{access_key}", lambda: a.delete(
        f"/api/credentials/keys/{k2}", headers=H))

    # one-time setup command (seed secrets look like RGW's, so the script accepts them)
    run_link = r.step("POST /api/credentials/setup-link", lambda: a.post(
        "/api/credentials/setup-link", json={"os": "linux"}, headers=H)).json()["url"]
    path = run_link.replace("https://testserver", "")
    r.step("POST /setup/{token}", lambda: env.anon().post(path))
    bad_link = a.post("/api/credentials/setup-link", json={"os": "linux"}, headers=H).json()["url"]
    alice_keys = env.croit_state.users["alice"].keys
    saved = [k["secret_key"] for k in alice_keys]
    for k in alice_keys:
        k["secret_key"] = "bad'secret"
    r.step("event", lambda: env.anon().post(bad_link.replace("https://testserver", "")))
    for k, secret in zip(alice_keys, saved, strict=True):
        k["secret_key"] = secret

    # cloud keys on the moto server (the AWS side is tested in tests/test_cloud_users.py);
    # the client stays until the location step, so admin suspend/unsuspend reach it too
    cl = CloudAdmin(make_settings(tmp, **CLOUD_SETTINGS),
                    session_factory=boundary_session({}, moto_server.endpoint))
    cl.client("iam").create_policy(PolicyName="BucketeerUserBoundary", Path="/bucketeer/",
                                   PolicyDocument=json.dumps(BOUNDARY_DOC))
    asyncio.run(cl.check())
    env.app.state.cloud = cl
    ck = r.step("POST /api/cloud/credentials/keys", lambda: a.post(
        "/api/cloud/credentials/keys", headers=H)).json()["accessKey"]
    r.step("POST /api/cloud/credentials/keys/{access_key}/rotate", lambda: a.post(
        f"/api/cloud/credentials/keys/{ck}/rotate", headers=H))
    extra = ok(a.post("/api/cloud/credentials/keys", headers=H)).json()["accessKey"]
    r.step("DELETE /api/cloud/credentials/keys/{access_key}", lambda: a.delete(
        f"/api/cloud/credentials/keys/{extra}", headers=H))
    cl.client("iam").create_user(UserName="s3-bob")  # made outside Bucketeer: refused
    r.step("event", lambda: b.post("/api/cloud/credentials/keys", headers=H))

    # cloud buckets on the same moto server (tests/test_cloud_buckets.py has the details)
    env.db.settings.set("location", {"default": "cloud"}, "test")
    cb = {"name": "cov", "storageClass": "intelligent", "quotaTiB": 1,
          "budgetNumber": "DN099999"}
    r.step("POST /api/cloud/buckets", lambda: a.post("/api/cloud/buckets", json=cb, headers=H))
    r.step("PUT /api/cloud/buckets/{name}/quota", lambda: a.put(
        "/api/cloud/buckets/cov/quota", json={"quotaTiB": 2}, headers=H))
    r.step("PUT /api/cloud/buckets/{name}/budget", lambda: a.put(
        "/api/cloud/buckets/cov/budget", json={"budgetNumber": "DN012345"}, headers=H))
    r.step("PUT /api/admin/cloud-buckets/{name}/quota", lambda: b.put(
        "/api/admin/cloud-buckets/bucketeer-alice-cov/quota", json={"quotaTiB": 1}, headers=H))
    cl.client("cloudwatch").put_metric_data(Namespace="AWS/S3", MetricData=[{
        "MetricName": "BucketSizeBytes", "Value": float(3 * 2**40), "Unit": "Bytes",
        "Timestamp": datetime.now(UTC) - timedelta(hours=30),
        "Dimensions": [{"Name": "BucketName", "Value": "bucketeer-alice-cov"},
                       {"Name": "StorageType", "Value": "IntelligentTieringFAStorage"}]}])
    r.step("event", lambda: asyncio.run(cloud_buckets.alert_tick(env.app)))  # over quota
    r.step("DELETE /api/cloud/buckets/{name}", lambda: a.request(
        "DELETE", "/api/cloud/buckets/cov", headers=H, json={"confirmName": "cov"}))
    # a cloud bucket with content: a job with a temporary key of the owner's IAM user; it
    # fails while the person has two keys, then is retried (tests/test_cloud_delete.py)
    ok(a.post("/api/cloud/buckets", json={**cb, "name": "full"}, headers=H))
    cl.client("s3").put_object(Bucket="bucketeer-alice-full", Key="k", Body=b"x")
    owner_iam = env.db.cloud_accounts.get("alice")["iam_user"]
    while len(cl.client("iam").list_access_keys(UserName=owner_iam)["AccessKeyMetadata"]) < 2:
        cl.client("iam").create_access_key(UserName=owner_iam)
    cjob = r.step("event", lambda: a.request(  # cloud.bucket.delete_started
        "DELETE", "/api/cloud/buckets/full", headers=H, json={"confirmName": "full"}))
    cjob = cjob.json()["job"]["id"]
    r.step("event", lambda: asyncio.run(bucket_delete.run_job(env.app, cjob)))  # _failed
    spare = cl.client("iam").list_access_keys(UserName=owner_iam)["AccessKeyMetadata"][-1]
    cl.client("iam").delete_access_key(UserName=owner_iam, AccessKeyId=spare["AccessKeyId"])
    r.step("POST /api/cloud/buckets/delete-jobs/{job_id}/retry", lambda: a.post(
        f"/api/cloud/buckets/delete-jobs/{cjob}/retry", headers=H))
    r.step("event", lambda: asyncio.run(bucket_delete.run_job(env.app, cjob)))  # .delete
    assert env.db.delete_jobs.get(cjob)["state"] == "done"
    real_grant = cl.grant_bucket_owner

    async def broken_grant(*_a):
        raise CloudError(500, "InternalError", "AWS refused PutBucketPolicy (InternalError)")
    cl.grant_bucket_owner = broken_grant
    r.step("event", lambda: a.post("/api/cloud/buckets", json={**cb, "name": "cov2"},
                                   headers=H))  # rolled back: cloud.bucket.create_failed
    cl.grant_bucket_owner = real_grant
    env.db.settings.set("location", {"default": "onprem"}, "test")

    # subusers (subuser.create_failed is covered in tests/test_subusers.py)
    sub = r.step("POST /api/subusers", lambda: a.post(
        "/api/subusers", headers=H,
        json={"name": "cov", "permission": "full-control", "confirm": True}))
    s1 = sub.json()["keys"][0]["accessKey"]
    r.step("PATCH /api/subusers/{name}", lambda: a.patch(
        "/api/subusers/cov", json={"permission": "read"}, headers=H))
    s2 = r.step("POST /api/subusers/{name}/keys", lambda: a.post(
        "/api/subusers/cov/keys", headers=H)).json()["accessKey"]
    r.step("POST /api/subusers/{name}/reveal", lambda: a.post(
        "/api/subusers/cov/reveal", json={"accessKey": s2}, headers=H))
    s3 = r.step("POST /api/subusers/{name}/keys/{access_key}/rotate", lambda: a.post(
        f"/api/subusers/cov/keys/{s1}/rotate", headers=H)).json()["accessKey"]
    r.step("DELETE /api/subusers/{name}/keys/{access_key}", lambda: a.delete(
        f"/api/subusers/cov/keys/{s3}", headers=H))
    r.step("DELETE /api/subusers/{name}", lambda: a.delete("/api/subusers/cov", headers=H))

    # buckets
    r.step("POST /api/buckets", lambda: a.post(
        "/api/buckets", headers=H,
        json={"name": "alice-cov", "budgetNumber": "WT-1", "versioning": False}))
    r.step("PUT /api/buckets/{name}/versioning", lambda: a.put(
        "/api/buckets/alice-cov/versioning", json={"enabled": True}, headers=H))
    r.step("PUT /api/buckets/{name}/lifecycle", lambda: a.put(
        "/api/buckets/alice-cov/lifecycle", headers=H, json={
            "expireCurrentDays": None, "expireNoncurrentDays": 30, "abortMultipartDays": 7}))
    r.step("POST /api/buckets/{name}/share-link", lambda: a.post(
        "/api/buckets/alice-data/share-link", headers=H,
        json={"key": "data/file-0001.bin", "expiresIn": 3600}))
    # a bucket whose tag write fails, then reconcile
    env.break_tagging()
    a.post("/api/buckets", headers=H,
           json={"name": "alice-tag", "budgetNumber": "WT-2", "versioning": False})
    env.fix_tagging()
    r.step("POST /api/buckets/reconcile", lambda: a.post("/api/buckets/reconcile", headers=H))

    # bucket keys
    bk = r.step("POST /api/buckets/{name}/keys", lambda: a.post(
        "/api/buckets/alice-cov/keys", json={"label": "ci", "access": "read"}, headers=H))
    kid = bk.json()["id"]
    r.step("POST /api/bucket-keys/{key_id}/reveal", lambda: a.post(
        f"/api/bucket-keys/{kid}/reveal", headers=H))
    r.step("DELETE /api/bucket-keys/{key_id}", lambda: a.delete(f"/api/bucket-keys/{kid}",
                                                                  headers=H))
    # sharing: share, change access, revoke; a second share is left for the bucket delete
    sh = r.step("POST /api/buckets/{name}/shares", lambda: a.post(
        "/api/buckets/alice-cov/shares", json={"login": "bob", "access": "read"}, headers=H))
    sid = sh.json()["id"]
    r.step("PUT /api/buckets/{name}/shares/{share_id}", lambda: a.put(
        f"/api/buckets/alice-cov/shares/{sid}", json={"access": "readwrite"}, headers=H))
    r.step("DELETE /api/buckets/{name}/shares/{share_id}", lambda: a.delete(
        f"/api/buckets/alice-cov/shares/{sid}", headers=H))
    ok(a.post("/api/buckets/alice-cov/shares", json={"login": "bob", "access": "read"},
              headers=H))
    # raw policy editor: write a foreign statement, then delete the policy
    et = a.get("/api/buckets/alice-tag/policy").json()["etag"]
    doc = {"Version": "2012-10-17", "Statement": [{
        "Sid": "team-read", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam:::user/bob"},
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::alice-tag/*"}]}
    put_p = r.step("PUT /api/buckets/{name}/policy", lambda: a.put(
        "/api/buckets/alice-tag/policy", json={"policy": doc, "etag": et}, headers=H))
    r.step("DELETE /api/buckets/{name}/policy", lambda: a.request(
        "DELETE", "/api/buckets/alice-tag/policy", json={"etag": put_p.json()["etag"]},
        headers=H))
    # full lifecycle editor
    et = a.get("/api/buckets/alice-tag/lifecycle/rules").json()["etag"]
    r.step("PUT /api/buckets/{name}/lifecycle/rules", lambda: a.put(
        "/api/buckets/alice-tag/lifecycle/rules", headers=H, json={"etag": et, "rules": [{
            "id": "tmp", "status": "Enabled", "filter": {"prefix": "tmp/"},
            "expiration": {"days": 7}}]}))
    # CORS editor
    et = a.get("/api/buckets/alice-tag/cors").json()["etag"]
    r.step("PUT /api/buckets/{name}/cors", lambda: a.put(
        "/api/buckets/alice-tag/cors", headers=H, json={"etag": et, "rules": [{
            "allowedOrigins": ["https://example.org"], "allowedMethods": ["GET"]}]}))
    # notifications (endpoint from the admin allowlist)
    ok(b.put(f"{S}/notification-endpoints/covhook", headers=H, json={
        "type": "https", "url": "https://hooks.example.org/s3"}))
    et = a.get("/api/buckets/alice-tag/notifications").json()["etag"]
    r.step("PUT /api/buckets/{name}/notifications", lambda: a.put(
        "/api/buckets/alice-tag/notifications", headers=H, json={"etag": et, "notifications": [{
            "id": "n1", "endpoint": "covhook", "events": ["s3:ObjectCreated:*"]}]}))
    # unfinished uploads
    up = env.moto().create_multipart_upload(Bucket="alice-tag", Key="big.bin")["UploadId"]
    r.step("POST /api/buckets/{name}/multipart/abort", lambda: a.post(
        "/api/buckets/alice-tag/multipart/abort", headers=H,
        json={"uploads": [{"key": "big.bin", "uploadId": up}]}))
    # tags and budget number
    et = a.get("/api/buckets/alice-tag/tags").json()["etag"]
    r.step("PUT /api/buckets/{name}/tags", lambda: a.put(
        "/api/buckets/alice-tag/tags", headers=H, json={"etag": et, "tags": [
            {"key": "team", "value": "genomics"}]}))
    r.step("PUT /api/buckets/{name}/budget", lambda: a.put(
        "/api/buckets/alice-tag/budget", headers=H, json={"budgetNumber": "WT-999"}))
    r.step("PUT /api/buckets/{name}/quota", lambda: a.put(
        "/api/buckets/alice-tag/quota", headers=H, json={"quotaTiB": 3}))
    # object lock: default retention, then retention and legal hold of one object
    ok(a.post("/api/buckets", headers=H, json={
        "name": "alice-olock", "budgetNumber": "WT-4", "objectLock": True}))
    et = a.get("/api/buckets/alice-olock/object-lock").json()["etag"]
    r.step("PUT /api/buckets/{name}/object-lock", lambda: a.put(
        "/api/buckets/alice-olock/object-lock", headers=H, json={
            "etag": et, "defaultRetention": {"mode": "GOVERNANCE", "days": 1}}))
    env.moto().put_object(Bucket="alice-olock", Key="k.txt", Body=b"x")
    until = (datetime.now(UTC) + timedelta(days=9)).isoformat()
    r.step("PUT /api/buckets/{name}/objects/retention", lambda: a.put(
        "/api/buckets/alice-olock/objects/retention", params={"key": "k.txt"}, headers=H,
        json={"mode": "GOVERNANCE", "retainUntil": until}))
    r.step("PUT /api/buckets/{name}/objects/legal-hold", lambda: a.put(
        "/api/buckets/alice-olock/objects/legal-hold", params={"key": "k.txt"}, headers=H,
        json={"status": "ON"}))
    # ACLs: bucket (moto keeps these) and object (moto ignores object ACL documents)
    et = a.get("/api/buckets/alice-data/acl").json()["etag"]
    r.step("PUT /api/buckets/{name}/acl", lambda: a.put(
        "/api/buckets/alice-data/acl", headers=H, json={
            "etag": et, "keep": [], "add": [{"login": "bob", "permission": "READ"}]}))
    plain_factory = env.app.state.s3.factory
    fake_objects(env)
    oacl = "/api/buckets/alice-data/objects/acl"
    key = {"key": "data/file-0001.bin"}
    et = a.get(oacl, params=key).json()["etag"]
    r.step("PUT /api/buckets/{name}/objects/acl", lambda: a.put(
        oacl, params=key, headers=H, json={
            "etag": et, "keep": [], "add": [{"login": "bob", "permission": "READ"}]}))
    env.app.state.s3.factory = plain_factory
    # failures with an unknown outcome (croit 5xx) are audited too
    def fail(method, needle):
        env.transport.fail = lambda q: (httpx.Response(500, json={"message": "boom"})
                                        if q.method == method and needle in q.url.path else None)

    fail("PUT", "/s3/buckets")
    r.step("event", lambda: a.post("/api/buckets", headers=H,
                                   json={"name": "alice-bad", "budgetNumber": "WT-3"}))
    fail("PATCH", "/s3/buckets")
    r.step("event", lambda: a.put("/api/buckets/alice-tag/versioning", json={"enabled": True},
                                  headers=H))
    fail("DELETE", "/s3/buckets")
    r.step("event", lambda: a.request("DELETE", "/api/buckets/alice-tag", headers=H,
                                      json={"confirmName": "alice-tag"}))
    env.transport.fail = None
    r.step("DELETE /api/buckets/{name}", lambda: a.request("DELETE", "/api/buckets/alice-cov",
                                                           headers=H,
                                                           json={"confirmName": "alice-cov"}))

    # rgw backend: delete job queued, blocked by a legal hold, then retried by the owner
    # (the run uses the croit backend, so the job path is switched on for these steps)
    env.croit_state.create_bucket("alice-held", "alice", versioning="ENABLED", object_lock=True)
    env.bridge.owner_client("alice").put_object(
        Bucket="alice-held", Key="k", Body=b"1", ObjectLockLegalHoldStatus="ON")
    saved = (bucket_delete.uses_jobs, bucket_delete.AUTOSTART)
    bucket_delete.uses_jobs, bucket_delete.AUTOSTART = (lambda app: True), False
    try:
        queued = r.step("event", lambda: a.request(
            "DELETE", "/api/buckets/alice-held", headers=H, json={"confirmName": "alice-held"}))
        job_id = queued.json()["job"]["id"]
        r.step("event", lambda: asyncio.run(bucket_delete.run_job(env.app, job_id)))
        r.step("POST /api/buckets/delete-jobs/{job_id}/retry", lambda: a.post(
            f"/api/buckets/delete-jobs/{job_id}/retry", headers=H))
        # the bucket changes hands: the new owner's delete supersedes the old job
        env.croit_state.buckets["alice-held"].owner = "bob"
        r.step("event", lambda: b.request(
            "DELETE", "/api/buckets/alice-held", headers=H, json={"confirmName": "alice-held"}))
    finally:
        bucket_delete.uses_jobs, bucket_delete.AUTOSTART = saved

    # startup clean-up (background job) and first-use RGW user creation
    stale_pending_row(env)
    r.step("event", lambda: asyncio.run(cleanup_pending(env.app, delays=(0.01,))))
    env.fake.add("dave", groups={USERS_GROUP, TEAM_GROUP}, display="Dave D",
                 mail="dave@example.org")
    r.step("event", lambda: env.session("dave").get("/api/credentials"))

    # admin
    abk = ok(a.post("/api/buckets/alice-data/keys", json={"label": "adm", "access": "read"},
                    headers=H)).json()
    r.step("DELETE /api/admin/bucket-keys/{key_id}", lambda: b.delete(
        f"/api/admin/bucket-keys/{abk['id']}", headers=H))
    quota = {"enabled": True, "maxSize": 5 * 2**30, "maxObjects": None}
    r.step("PUT /api/admin/users/{uid}/quota", lambda: b.put(
        "/api/admin/users/alice/quota", json=quota, headers=H))
    r.step("PUT /api/admin/buckets/{bucket}/quota", lambda: b.put(
        "/api/admin/buckets/alice-data/quota", json=quota, headers=H))
    r.step("POST /api/admin/buckets/write-tags", lambda: b.post(
        "/api/admin/buckets/write-tags", headers=H))
    r.step("POST /api/admin/users/{uid}/suspend", lambda: b.post(
        "/api/admin/users/alice/suspend", headers=H))
    r.step("POST /api/admin/users/{uid}/unsuspend", lambda: b.post(
        "/api/admin/users/alice/unsuspend", headers=H))

    # offboarding: a clean report run, then dave leaves AD: aborted by the 10% limit, then
    # (limit lifted) enforced; his next login undoes it
    r.step("POST /api/admin/offboarding/run", lambda: b.post(
        "/api/admin/offboarding/run", headers=H))
    r.step("PUT /api/admin/billing/settings", lambda: b.put(
        "/api/admin/billing/settings", json={"pricePerTiBMonth": 9.5}, headers=H))
    ok(b.put("/api/admin/billing/settings", json={"contacts": ["finance@example.org"]},
             headers=H))
    real_mailer = env.app.state.mailer
    env.app.state.mailer = mail = FakeMailer()
    r.step("POST /api/admin/billing/report/send", lambda: b.post(
        "/api/admin/billing/report/send", json={"month": "2026-01"}, headers=H))
    mail.fail = {"finance@example.org"}
    r.step("event", lambda: b.post(
        "/api/admin/billing/report/send", json={"month": "2026-01"}, headers=H))
    env.app.state.mailer = real_mailer
    r.step("PUT /api/admin/offboarding/settings", lambda: b.put(
        "/api/admin/offboarding/settings",
        json={"mode": "enforce", "time": "04:30", "minPeopleForPercent": 1}, headers=H))
    dave = env.fake.users["CN=dave,OU=Users,DC=example,DC=org"]
    dave["userAccountControl"] = "514"
    r.step("event", lambda: b.post("/api/admin/offboarding/run", headers=H))  # aborted
    b.put("/api/admin/offboarding/settings", json={"maxPercent": 100}, headers=H)
    fail("PATCH", "/s3/users")  # croit down for the suspension: the run fails
    r.step("event", lambda: b.post("/api/admin/offboarding/run", headers=H))
    env.transport.fail = None
    r.step("event", lambda: b.post("/api/admin/offboarding/run", headers=H))  # suspends
    dave["userAccountControl"] = "512"

    async def dave_returns():
        acct = await env.app.state.rgw.resolve("dave")
        await env.app.state.offboarding.on_login("dave", acct)
    fail("PATCH", "/s3/users")  # ... and the login unsuspend fails once
    r.step("event", lambda: asyncio.run(dave_returns()))
    env.transport.fail = None
    r.step("event", lambda: asyncio.run(dave_returns()))

    # admin settings (naming last: it changes what bucket names are valid)
    r.step(f"PUT {S}/placement", lambda: b.put(
        f"{S}/placement", json={"default": None, "allowed": []}, headers=H))
    r.step(f"PUT {S}/bucket-quota", lambda: b.put(
        f"{S}/bucket-quota", json={"maxWeeklyQuotaIncreaseTiB": 50}, headers=H))
    r.step(f"PUT {S}/default-quota", lambda: b.put(
        f"{S}/default-quota", json={"quotaTiB": 2}, headers=H))
    r.step(f"DELETE {S}/default-quota", lambda: b.delete(f"{S}/default-quota", headers=H))
    r.step(f"PUT {S}/uid-mappings/{{ad_username}}", lambda: b.put(
        f"{S}/uid-mappings/erin", json={"rgwUid": "alice"}, headers=H))
    r.step(f"DELETE {S}/uid-mappings/{{ad_username}}", lambda: b.delete(
        f"{S}/uid-mappings/erin", headers=H))
    ep = {"type": "https", "url": "https://hooks.example.org/in", "topic": "", "verifyTls": True}
    r.step(f"PUT {S}/notification-endpoints/{{name}}", lambda: b.put(
        f"{S}/notification-endpoints/hook", json=ep, headers=H))
    r.step(f"DELETE {S}/notification-endpoints/{{name}}", lambda: b.delete(
        f"{S}/notification-endpoints/hook", headers=H))
    r.step(f"PUT {S}/budget-number", lambda: b.put(
        f"{S}/budget-number", json={"pattern": "[0-9]{6}", "example": "123456"}, headers=H))
    r.step(f"POST {S}/budget-number/test", lambda: b.post(
        f"{S}/budget-number/test", headers=H,
        json={"rules": {"pattern": "[0-9]{6}", "example": ""}, "value": "123456"}))
    r.step(f"POST {S}/naming/test", lambda: b.post(
        f"{S}/naming/test", headers=H,
        json={"rules": {"prefix": "", "pattern": ""}, "name": "x", "username": "alice"}))
    r.step(f"PUT {S}/naming", lambda: b.put(
        f"{S}/naming", json={"prefix": "{username}-", "pattern": ""}, headers=H))
    env.app.state.cloud = FakeCloud()  # the AWS side is tested on moto in test_cloud.py
    r.step(f"PUT {S}/location", lambda: b.put(f"{S}/location", json={"default": "cloud"},
                                              headers=H))
    env.app.state.cloud = None
    ok(b.get(S))

    # admin alert contacts; the failed backup run below mails them (a fake mailer)
    r.step("PUT /api/admin/alerts", lambda: b.put(
        "/api/admin/alerts", json={"contacts": ["ops@example.org"]}, headers=H))
    # backups: settings, a failed run (alert sent) and restore test (alert failed), good ones
    r.step("PUT /api/admin/backup/settings", lambda: b.put(
        "/api/admin/backup/settings", json={"time": "02:30", "restoreTestDays": 14}, headers=H))
    real_factory = env.app.state.backup.factory

    def broken(*_a):
        raise RuntimeError("S3 down")

    env.app.state.backup.factory = broken
    real_mailer = env.app.state.mailer
    env.app.state.mailer = AlertMailer()
    r.step("event", lambda: b.post("/api/admin/backup/run", headers=H))  # + admin.alert_sent
    env.app.state.mailer = AlertMailer(fail={"ops@example.org"})
    r.step("event", lambda: b.post("/api/admin/backup/restore-test", headers=H))  # alert_failed
    env.app.state.mailer = real_mailer
    env.app.state.backup.factory = real_factory
    r.step("POST /api/admin/backup/run", lambda: ok(b.post("/api/admin/backup/run", headers=H)))
    r.step("POST /api/admin/backup/restore-test", lambda: ok(b.post(
        "/api/admin/backup/restore-test", headers=H)))
    # rebuilding rows from Ceph (command line; fill-gaps adds nothing here)
    r.step("event", lambda: asyncio.run(rebuild.rebuild_from_ceph(env.app, True)))
    # the one-time step: a person still on the old 1 TiB user quota becomes unlimited
    env.croit_state.users["alice"].user_quota.update(enabled=True, maxSize=2**40, maxObjects=-1)
    r.step("event", lambda: asyncio.run(bucket_quota.unlimit_user_quotas(env.app)))
    # user notices: the settings, then a daily pass with a full bucket and old keys
    r.step(f"PUT {S}/notices", lambda: b.put(f"{S}/notices", json={"maxKeyAgeDays": 30},
                                             headers=H))
    full = env.croit_state.buckets["alice-data"]
    env.croit_state.refresh_usage(full)
    full.quota.update(enabled=True, maxSize=max(1, full.size_bytes), maxObjects=-1)
    asyncio.run(notices.notice_tick(env.app))  # first sightings of the keys
    r.step("event", lambda: asyncio.run(notices.notice_tick(
        env.app, datetime.now(UTC) + timedelta(days=60))))
    yield r
    backup.ENV_FILE = saved_env_file
    env.db.close()


# -- the table is complete --------------------------------------------------------------


def test_every_mutating_route_is_in_the_table(run):
    found = mutating_routes(run.env.app)
    assert found - set(ROUTES) == set(), "new mutating route(s): audit them, then list here"
    assert set(ROUTES) - found == set(), "table lists a route that no longer exists"
    assert all(k in ROUTES for k in EXEMPT) and all(ROUTES[k] == () for k in EXEMPT)
    assert all(v or k in EXEMPT for k, v in ROUTES.items()), "an empty entry needs an EXEMPT reason"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_route_writes_its_audit_actions(run, route):
    expected = set(ROUTES[route])
    if not expected:
        assert route in EXEMPT
        return
    assert route in run.seen, f"the coverage run never called {route}"
    assert expected <= run.seen[route], f"{route}: wanted {expected}, saw {run.seen[route]}"
    if route != "POST /api/login":
        assert any(s < 400 for s in run.status[route]), f"{route} never succeeded in the run"


def test_events_are_written(run):
    seen = {a["action"] for a in run.env.db.audit.query(limit=1000)}
    assert set(EVENTS) <= seen, set(EVENTS) - seen


def test_cleanup_is_by_system(run):
    (row,) = [a for a in run.env.db.audit.query(limit=1000) if a["action"] == "bucket_key.cleanup"]
    assert row["actor"] == "system" and row["target_type"] == "bucket_key"


# -- consistency ------------------------------------------------------------------------


def test_naming_actor_and_target_scheme(run):
    rows = run.env.db.audit.query(limit=1000)
    assert len(rows) > 35
    for a in rows:
        assert ACTION_RE.match(a["action"]) or a["action"] in ACTION_EXCEPTIONS, a["action"]
        assert a["actor"] in ACTORS, a
        assert a["target_type"] in TARGET_TYPES and a["target"], a
    by = {}
    for a in rows:
        by.setdefault(a["action"].split(".")[0], set()).add(a["target_type"])
    assert by["bucket"] == {"bucket"} and by["bucket_key"] == {"bucket_key"}
    assert by["profile"] == {"profile"} and by["credentials"] == {"access_key"}
    assert by["rgw"] == {"rgw_user"}
    users = {a["target"] for a in rows if a["action"].startswith("admin.user.")}
    assert users == {"alice"} and {a["target_type"] for a in rows
                                   if a["action"].startswith("admin.user.")} == {"rgw_user"}
    assert {a["target_type"] for a in rows if a["action"].startswith("admin.settings.naming")} \
        == {"setting"}


# -- no secrets -------------------------------------------------------------------------


def known_secrets(run) -> set[str]:
    run.collect()
    return {SECRET_PW, "wrong-pw", REQUIRED["croit_api_token"], REQUIRED["session_secret"],
            REQUIRED["ldap_bind_password"]} | run.secrets


def test_no_secret_in_any_audit_row(run):
    rows = run.env.db.audit.query(limit=10000)
    assert rows and not secret_hits(rows, known_secrets(run))


def test_secret_scan_catches_a_planted_secret():
    row = {"action": "x.y", "details": {"url": "https://s3/b?X-Amz-Signature=abc"}}
    assert secret_hits([row], set())
    assert secret_hits([{"action": "x.y", "details": {"h": "Bearer abcdef"}}], set())
    assert secret_hits([{"action": "x.y", "details": {"n": "hunter2-key"}}], {"hunter2-key"})
    assert not secret_hits([{"action": "x.y", "details": {"password": "[redacted]"}}], set())


def test_login_failure_rows_carry_no_password(run):
    fails = [a for a in run.env.db.audit.query(limit=1000) if a["action"] == "login.failure"]
    assert fails and all("wrong-pw" not in json.dumps(a) for a in fails)


def test_accumulated_secrets_include_deleted_keys(run):
    """The rotated/deleted/revoked keys' secrets were collected while they still existed."""
    live = {k["secret_key"] for u in run.env.croit_state.users.values() for k in u.keys}
    assert len(run.secrets - live) >= 3


def test_scan_fails_when_a_deleted_keys_secret_leaks_into_an_audit_row(run):
    gone = sorted(run.secrets - {k["secret_key"] for u in run.env.croit_state.users.values()
                                 for k in u.keys})[0]
    rows = run.env.db.audit.query(limit=10000)
    leaky = [*rows, {"action": "credentials.key.rotate", "details": {"note": gone}}]
    assert secret_hits(leaky, known_secrets(run))
    assert not secret_hits(rows, known_secrets(run))
