"""One-time setup links: POST /api/credentials/setup-link and POST /setup/{token}."""
# ruff: noqa: E501

import json
import logging

import pytest

from bucketeer.credentials import SETUP_LIMIT, SETUP_TTL, RedactSetupToken
from bucketeer.setup_script import TLS12

from .backends import admin_backend  # noqa: F401  (fixture: both admin backends)
from .test_credentials import ALICE as SEED_ALICE
from .test_credentials import BOB as SEED_BOB
from .test_credentials import H
from .test_credentials import env as base_env  # noqa: F401,F811  (fixture)

# The mock's seed secrets hold "-", which real RGW secrets do not; the script refuses them.
ALICE = (*SEED_ALICE[:3], "AliceSecret0001abcDEF")
BOB = (*SEED_BOB[:3], "BobSecret0002abcDEF")


@pytest.fixture
def env(base_env):  # noqa: F811
    for uid, (_, _, _, secret) in (("alice", ALICE), ("bob", BOB)):
        for k in base_env.mock.state.croit.users[uid].keys:
            k["secret_key"] = secret
    return base_env


LINK = "/api/credentials/setup-link"


def make(env, os_name="linux", client=None):
    r = (client or env.alice).post(LINK, json={"os": os_name}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def token_of(link):
    return link["url"].rsplit("/", 1)[1]


def fetch(env, link, client=None):
    # No session and no CSRF header: the token is the only credential.
    return (client or env.anon()).post("/setup/" + token_of(link))


def actions(env):
    return [a for a in env.db.audit.query(limit=1000) if a["action"].startswith("credentials.setup")]


def test_create_link_shape_and_audit(env):
    r = env.alice.post(LINK, json={"os": "linux"}, headers=H)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    d = r.json()
    token = token_of(d)
    assert d["url"] == f"https://testserver/setup/{token}" and len(token) >= 43
    assert d["command"] == f"curl -fsSL -X POST '{d['url']}' | sh"
    assert d["os"] == "linux" and d["expiresIn"] == SETUP_TTL and d["expiresAt"].endswith("Z")
    assert make(env, "windows")["command"].startswith(TLS12 + "irm -Method Post 'https://testserver/setup/")
    (e,) = [a for a in actions(env) if a["details"]["os"] == "linux"]
    assert e["action"] == "credentials.setup_link" and e["actor"] == "alice" and e["target"] == ALICE[2]
    assert e["target_type"] == "access_key"
    assert e["details"]["uid"] == "alice" and e["details"]["os"] == "linux"
    assert token not in json.dumps(env.db.audit.query(limit=1000))
    row = env.db.query_one("SELECT * FROM setup_tokens WHERE id = ?", (e["details"]["setupId"],))
    assert row["token_hash"] != token and token not in json.dumps(row)


def test_link_needs_csrf_and_session_and_valid_os(env):
    assert env.alice.post(LINK, json={"os": "linux"}).status_code == 403
    assert env.anon().post(LINK, json={"os": "linux"}, headers=H).status_code == 401
    assert env.alice.post(LINK, json={"os": "beos"}, headers=H).status_code == 422


def test_fetch_once_gives_script_with_values(env):
    link = make(env)
    r = fetch(env, link)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("text/plain")
    t = r.text
    assert f"aws_access_key_id = {ALICE[2]}" in t and f"aws_secret_access_key = {ALICE[3]}" in t
    assert "[profile bucketeer]" in t and "endpoint_url = https://s3.example.org" in t
    assert "addressing_style = path" in t and "region = us-east-1" in t
    again = fetch(env, link)
    assert again.status_code == 200 and "exit 1" in again.text and ALICE[3] not in again.text
    assert "already used or has expired" in again.text
    (ok,) = [a for a in actions(env) if a["action"] == "credentials.setup"]
    assert ok["action"] == "credentials.setup" and ok["actor"] == "alice"
    assert ok["details"]["os"] == "linux" and "ip" in ok["details"]


def test_windows_script_and_gone_script_per_os(env):
    link = make(env, "windows")
    assert "WriteAllText" in fetch(env, link).text
    gone = fetch(env, link)
    assert gone.status_code == 200 and gone.text.startswith("Write-Host '")


def test_expired_unknown(env):
    link = make(env)
    env.now[0] += SETUP_TTL + 1
    r = fetch(env, link)
    assert r.status_code == 200 and "expired" in r.text and ALICE[3] not in r.text
    u = env.anon().post("/setup/nope")
    assert u.status_code == 200 and u.text == "echo 'Unknown setup link. Make a new one in Bucketeer.'\n"
    assert env.anon().get("/setup/nope").status_code in (404, 405)


def test_other_session_has_no_effect(env):
    link = make(env)
    bob = env.session("bob")
    assert fetch(env, link, client=bob).status_code == 200  # token holder, not session, decides
    bob_link = make(env, client=bob)
    assert BOB[2] in fetch(env, bob_link).text and ALICE[3] not in fetch(env, make(env, client=bob)).text


def test_suspended_at_fetch_is_refused(env):
    link = make(env)
    env.mock.state.croit.users["alice"].suspended = True
    r = fetch(env, link)
    assert r.status_code == 200 and "suspended" in r.text and ALICE[3] not in r.text
    ev = [a for a in actions(env) if a["action"] == "credentials.setup_refused"]
    assert ev and ev[0]["details"]["reason"] == "suspended"
    assert "already used" in fetch(env, link).text  # the token is spent


def test_key_gone_is_refused(env):
    link = make(env)
    env.alice.post("/api/credentials/keys", headers=H)
    r = env.alice.delete(f"/api/credentials/keys/{ALICE[2]}", headers=H)
    assert r.status_code == 200
    r = fetch(env, link)
    assert r.status_code == 200 and "rotated or deleted" in r.text and ALICE[3] not in r.text
    assert [a["details"]["reason"] for a in actions(env) if a["action"] == "credentials.setup_refused"] == ["key_gone"]


def test_rate_limit(env):
    for _ in range(SETUP_LIMIT):
        make(env)
    r = env.alice.post(LINK, json={"os": "linux"}, headers=H)
    assert r.status_code == 429
    assert make(env, client=env.session("bob"))  # per user
    env.now[0] += 15 * 60 + 1
    assert make(env)


def test_access_log_filter_redacts_token():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                            '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "POST", "/setup/abc_DEF-123", "1.1", 200), None)
    assert RedactSetupToken().filter(rec)
    out = rec.getMessage()
    assert "abc_DEF-123" not in out and "/setup/[redacted]" in out
    other = logging.LogRecord("x", logging.INFO, "", 0, '"%s %s"', ("GET", "/api/me"), None)
    RedactSetupToken().filter(other)
    assert other.getMessage() == '"GET /api/me"'


def test_unsafe_secret_is_refused(env):
    env.mock.state.croit.users["alice"].keys[0]["secret_key"] = "bad'secret"
    r = fetch(env, make(env))
    assert r.status_code == 200 and "cannot be installed" in r.text and "bad" not in r.text
    assert [a["details"]["reason"] for a in actions(env) if a["action"] == "credentials.setup_refused"] == ["unsafe_value"]


def test_audit_never_holds_token_or_secret(env):
    links = [make(env), make(env, "windows")]
    for link in links:
        fetch(env, link)
        fetch(env, link)
    text = json.dumps(env.db.audit.query(limit=1000))
    assert ALICE[3] not in text
    assert not [t for t in map(token_of, links) if t in text]


@pytest.mark.parametrize("path", ["/setup/x"])
def test_setup_route_is_registered_before_static(env, path):
    assert "Unknown setup link" in env.anon().post(path).text


def test_admin_trouble_on_key_list_gets_script_and_audit(env):
    import httpx

    from .backends import on_rgw
    link = make(env)
    env.transport.fail = lambda req: (
        httpx.ConnectError("down") if on_rgw() or "/keys" in str(req.url) else None)
    r = fetch(env, link)
    env.transport.fail = None
    assert r.status_code == 200 and "exit 1" in r.text and ALICE[3] not in r.text
    assert [a["details"]["reason"] for a in actions(env)
            if a["action"] == "credentials.setup_refused"] == ["admin_unavailable"]
