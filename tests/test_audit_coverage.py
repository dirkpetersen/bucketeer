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

from bucketeer.bucket_keys import cleanup_pending

from .conftest import REQUIRED
from .test_auth import SECRET_PW, TEAM_GROUP, USERS_GROUP
from .test_buckets import Env, H

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
    "POST /api/buckets": ("bucket.create",),
    "POST /api/buckets/reconcile": ("bucket.tag_sync",),
    "PUT /api/buckets/{name}/versioning": ("bucket.versioning",),
    "PUT /api/buckets/{name}/lifecycle": ("bucket.lifecycle",),
    "POST /api/buckets/{name}/share-link": ("bucket.share_link",),
    "DELETE /api/buckets/{name}": ("bucket.delete",),
    "POST /api/buckets/{name}/keys": ("bucket_key.create",),
    "POST /api/bucket-keys/{key_id}/reveal": ("bucket_key.reveal",),
    "DELETE /api/bucket-keys/{key_id}": ("bucket_key.revoke",),
    "DELETE /api/admin/bucket-keys/{key_id}": ("admin.bucket_key.revoke",),
    "PUT /api/admin/users/{uid}/quota": ("admin.user.quota",),
    "PUT /api/admin/buckets/{bucket}/quota": ("admin.bucket.quota",),
    "POST /api/admin/users/{uid}/suspend": ("admin.user.suspend",),
    "POST /api/admin/users/{uid}/unsuspend": ("admin.user.unsuspend",),
    f"PUT {S}/naming": ("admin.settings.naming",),
    f"POST {S}/naming/test": (),
    f"PUT {S}/budget-number": ("admin.settings.budget_number",),
    f"POST {S}/budget-number/test": (),
    f"PUT {S}/placement": ("admin.settings.placement",),
    f"PUT {S}/default-quota": ("admin.settings.default_quota",),
    f"DELETE {S}/default-quota": ("admin.settings.default_quota",),
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
    "bucket_key.cleanup": "startup clean-up of stale pending bucket keys (actor system)",
    "rgw.user.create": "first S3 use of a new AD user creates the RGW user",
}

ACTION_RE = re.compile(r"^(?:[a-z][a-z_]*)(?:\.[a-z][a-z_]*)+$")
ACTION_EXCEPTIONS = {"logout"}  # pre-dates the scheme; kept so old log rows still match
TARGET_TYPES = {"user", "rgw_user", "bucket", "bucket_key", "access_key", "profile", "setting",
                "uid_mapping", "notification_endpoint", "report"}
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


@pytest.fixture(scope="module")
def run(tmp_path_factory, moto_server):
    httpx.post(f"{moto_server.endpoint}/moto-api/reset", timeout=10).raise_for_status()
    env = Env(tmp_path_factory.mktemp("cov"), moto_server.endpoint)
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

    # buckets
    r.step("POST /api/buckets", lambda: a.post(
        "/api/buckets", json={"name": "alice-cov", "budgetNumber": "WT-1"}, headers=H))
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
    a.post("/api/buckets", json={"name": "alice-tag", "budgetNumber": "WT-2"}, headers=H)
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
    r.step("event", lambda: a.request("DELETE", "/api/buckets/alice-tag", headers=H))
    env.transport.fail = None
    r.step("DELETE /api/buckets/{name}", lambda: a.request("DELETE", "/api/buckets/alice-cov",
                                                           headers=H))

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
    r.step("POST /api/admin/users/{uid}/suspend", lambda: b.post(
        "/api/admin/users/alice/suspend", headers=H))
    r.step("POST /api/admin/users/{uid}/unsuspend", lambda: b.post(
        "/api/admin/users/alice/unsuspend", headers=H))

    # admin settings (naming last: it changes what bucket names are valid)
    r.step(f"PUT {S}/placement", lambda: b.put(
        f"{S}/placement", json={"default": None, "allowed": []}, headers=H))
    r.step(f"PUT {S}/default-quota", lambda: b.put(f"{S}/default-quota", json=quota, headers=H))
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
    ok(b.get(S))
    yield r
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
