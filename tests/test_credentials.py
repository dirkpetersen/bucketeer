"""/api/credentials against the in-process mock croit, with fake-LDAP sessions."""

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bucketeer.config import Settings
from bucketeer.credentials import ENDPOINT_MISS_TTL, ENDPOINT_TTL, endpoint_from_s3cfg
from bucketeer.db import Database
from bucketeer.ldapauth import AdAuthenticator
from bucketeer.main import create_app
from mock.croit_mock import SEED_USERS
from mock.croit_mock import create_app as create_mock

from .backends import (
    admin_backend,  # noqa: F401  (fixture: both admin backends)
    admin_client,
    croit_only,
    on_rgw,
    rgw_only,
)
from .conftest import REQUIRED
from .test_admin import Transport
from .test_auth import SECRET_PW, FakeLdap, make_settings

H = {"X-Requested-With": "bucketeer"}
TOKEN = REQUIRED["croit_api_token"]
ALICE = SEED_USERS["alice"]  # (name, email, access, secret)
BOB = SEED_USERS["bob"]


class Env:
    def __init__(self, tmp_path, **over):
        self.mock = create_mock(seed=True, task_seconds=0, prefix="/api", admin_token=TOKEN)
        self.transport = Transport(self.mock)
        self.settings = make_settings(tmp_path, **over)
        croit = admin_client(self.settings, self.mock, self.transport)
        self.db = Database(":memory:")
        self.now = [1000.0]
        self.app = create_app(
            self.settings, croit=croit, db=self.db,
            authenticator=AdAuthenticator(self.settings, FakeLdap()),
            clock=lambda: self.now[0],
        )
        self.alice = self.session("alice")

    def session(self, login):
        c = TestClient(self.app, base_url="https://testserver")
        r = c.post("/api/login", json={"login": login, "password": SECRET_PW}, headers=H)
        assert r.status_code == 200
        return c

    def anon(self):
        return TestClient(self.app, base_url="https://testserver")

    def audit(self):
        return [a for a in self.db.audit.query(limit=1000) if a["action"] == "credentials.reveal"]

    def s3cfg_calls(self):
        return [c for c in self.transport.calls if c[1].endswith("/cfg-file/download")]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    yield e
    e.db.close()


@pytest.fixture
def make_env(tmp_path):
    made = []

    def make(**over):
        e = Env(tmp_path, **over)
        made.append(e)
        return e

    yield make
    for e in made:
        e.db.close()


# -- list -------------------------------------------------------------------------


def test_list_has_no_secrets(env):
    r = env.alice.get("/api/credentials")
    assert r.status_code == 200
    assert r.json() == {
        "profile": "bucketeer", "region": "us-east-1", "endpointUrl": "https://s3.example.org",
        "addressingStyle": "path", "keys": [{"accessKey": ALICE[2]}],
    }
    assert ALICE[3] not in r.text
    assert r.headers["cache-control"] == "no-store"


def test_unauthenticated_is_401(env):
    anon = env.anon()
    assert anon.get("/api/credentials").status_code == 401
    r = anon.post("/api/credentials/reveal", json={"accessKey": "x"}, headers=H)
    assert r.status_code == 401
    assert env.transport.calls == []


def test_suspended_user_gets_no_keys(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    try:
        e.mock.state.croit.users["alice"].suspended = True
        assert e.alice.get("/api/credentials").status_code == 403
        r = e.alice.post("/api/credentials/reveal", json={"accessKey": ALICE[2]}, headers=H)
        assert r.status_code == 403 and ALICE[3] not in r.text
        assert e.audit() == []
        # no key read on croit; on Admin Ops user and keys are one read, so: no write at all
        assert not [c for c in e.transport.raw_calls if c[0] != "GET"]
        assert not [c for c in e.transport.calls if c[1].endswith("/keys")]
    finally:
        e.db.close()


def test_croit_down_is_503(env):
    # croit: only the key list is down; Admin Ops reads keys with the user, so all of it is
    env.transport.fail = lambda req: (
        httpx.ConnectError("down") if on_rgw() or "/keys" in str(req.url) else None)
    assert env.alice.get("/api/credentials").status_code == 503


# -- reveal -----------------------------------------------------------------------


def test_reveal_own_key_and_audit(env):
    r = env.alice.post("/api/credentials/reveal", json={"accessKey": ALICE[2]}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"accessKey": ALICE[2], "secretKey": ALICE[3]}
    assert r.headers["cache-control"] == "no-store"
    (entry,) = env.audit()
    assert entry["actor"] == "alice" and entry["target"] == ALICE[2]
    assert entry["details"] == {"uid": "alice"}
    assert ALICE[3] not in str(env.db.audit.query(limit=1000))


def test_reveal_another_users_key_is_404(env):
    r = env.alice.post("/api/credentials/reveal", json={"accessKey": BOB[2]}, headers=H)
    assert r.status_code == 404 and BOB[3] not in r.text
    r = env.alice.post("/api/credentials/reveal", json={"accessKey": ALICE[2][:-1]}, headers=H)
    assert r.status_code == 404  # exact match only
    assert env.audit() == []


def test_reveal_ignores_a_uid_sent_by_the_client(env):
    r = env.alice.post("/api/credentials/reveal?uid=bob",
                       json={"accessKey": BOB[2], "uid": "bob"}, headers=H)
    assert r.status_code == 404
    assert not [c for c in env.transport.calls if "/users/bob/" in c[1]]
    # the croit view above maps every Admin Ops read to /users?uid=: check the real requests
    assert not [c for c in env.transport.raw_calls if "uid=bob" in c[1] or "/users/bob" in c[1]]


def test_reveal_requires_csrf_header(env):
    r = env.alice.post("/api/credentials/reveal", json={"accessKey": ALICE[2]})
    assert r.status_code == 403 and ALICE[3] not in r.text
    assert env.audit() == []


BAD_BODIES = [{}, {"accessKey": ""}, {"accessKey": 5}, [], {"accessKey": "x" * 300}]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_reveal_rejects_malformed_bodies(env, body):
    assert env.alice.post("/api/credentials/reveal", json=body, headers=H).status_code == 422


def test_reveal_fails_closed_when_the_audit_cannot_be_written(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.db.audit, "add", boom)
    with pytest.raises(RuntimeError, match="disk full"):  # no response, so no secret
        env.alice.post("/api/credentials/reveal", json={"accessKey": ALICE[2]}, headers=H)


def _keys_with_subuser(req):
    if str(req.url).endswith("/users/alice/keys"):
        return httpx.Response(200, json=[
            {"user": "alice", "access_key": ALICE[2], "secret_key": ALICE[3]},
            {"user": "alice:sub", "access_key": "SUBKEY1", "secret_key": "subsecret"},
        ])


def test_subuser_keys_are_not_listed_or_revealed(env):
    env.transport.fail = _keys_with_subuser
    r = env.alice.get("/api/credentials")
    assert r.json()["keys"] == [{"accessKey": ALICE[2]}]
    r = env.alice.post("/api/credentials/reveal", json={"accessKey": "SUBKEY1"}, headers=H)
    assert r.status_code == 404 and "subsecret" not in r.text


# -- endpoint ---------------------------------------------------------------------


def test_endpoint_from_env_never_asks_croit(env):
    env.alice.get("/api/credentials")
    assert env.s3cfg_calls() == []


def test_endpoint_from_s3cfg_and_cache(make_env):
    croit_only("rgw never reads the .s3cfg; see test_rgw_endpoint_is_configured_never_discovered")
    e = make_env()  # no S3_ENDPOINT_URL
    assert e.alice.get("/api/credentials").json()["endpointUrl"] == "http://127.0.0.1:7480"
    e.alice.get("/api/credentials")
    assert len(e.s3cfg_calls()) == 1  # cached
    e.now[0] += ENDPOINT_TTL + 1
    e.alice.get("/api/credentials")
    assert len(e.s3cfg_calls()) == 2


def test_rgw_endpoint_is_configured_never_discovered(make_env):
    rgw_only("croit discovers the endpoint from the .s3cfg instead")
    e = make_env(s3_endpoint_url="https://s3.example.org")
    assert e.alice.get("/api/credentials").json()["endpointUrl"] == "https://s3.example.org"
    # without a configured endpoint the resolver answers null and never asks the backend
    bare = make_env()
    assert bare.alice.get("/api/credentials").json()["endpointUrl"] is None
    bare.now[0] += ENDPOINT_MISS_TTL + 1
    bare.alice.get("/api/credentials")
    assert not any("cfg" in c[1] for c in bare.transport.calls)
    assert not any("cfg" in c[1] for c in bare.transport.raw_calls)


def test_endpoint_none_when_unavailable_and_retried_soon(make_env):
    croit_only("rgw never reads the .s3cfg; see test_rgw_endpoint_is_configured_never_discovered")
    e = make_env()
    e.transport.fail = lambda req: (
        httpx.Response(500, json={}) if "cfg-file" in str(req.url) else None
    )
    r = e.alice.get("/api/credentials")
    assert r.status_code == 200 and r.json()["endpointUrl"] is None
    e.alice.get("/api/credentials")
    assert len(e.s3cfg_calls()) == 1
    e.transport.fail = None
    e.now[0] += ENDPOINT_MISS_TTL + 1
    assert e.alice.get("/api/credentials").json()["endpointUrl"] == "http://127.0.0.1:7480"


@pytest.mark.parametrize("payload", [["x"], "text", None, {"fileContents": 5}, {}])
def test_non_dict_cfg_file_answer_gives_null_endpoint(make_env, payload):
    e = make_env()
    e.transport.fail = lambda req: (
        httpx.Response(200, json=payload) if "cfg-file" in str(req.url) else None
    )
    r = e.alice.get("/api/credentials")
    assert r.status_code == 200 and r.json()["endpointUrl"] is None


def test_s3cfg_parsing():
    text = "[default]\nhost_base = s3.example.org\nuse_https = True\n# host_base = other\n"
    assert endpoint_from_s3cfg(text) == "https://s3.example.org"
    assert endpoint_from_s3cfg("host_base = 10.0.0.1:7480\nuse_https = False") == "http://10.0.0.1:7480"
    assert endpoint_from_s3cfg("# host_base = x\n") is None
    assert endpoint_from_s3cfg("host_base = evil.org\nfoo\n") == "http://evil.org"
    for bad in ("a b", "a/b", "a;b", "http://x", "-x"):
        assert endpoint_from_s3cfg(f"host_base = {bad}") is None


# -- settings ---------------------------------------------------------------------


def test_addressing_style_setting(make_env):
    assert make_env(s3_endpoint_url="https://s3.example.org").alice.get(
        "/api/credentials").json()["addressingStyle"] == "path"
    e = make_env(s3_endpoint_url="https://s3.example.org", s3_addressing_style="virtual")
    assert e.alice.get("/api/credentials").json()["addressingStyle"] == "virtual"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **REQUIRED, s3_addressing_style="bogus")


@pytest.mark.parametrize("name", ["bucketeer", "default", "my-lab_2.s3", "A1"])
def test_profile_name_accepts_safe_names(name):
    assert Settings(_env_file=None, **REQUIRED, aws_profile_name=name).aws_profile_name == name


BAD_PROFILES = ["", "a b", "a]\nb", "x]\n[default", "-x", "a=b", "a#b", "a\n", "é", "x" * 64]


@pytest.mark.parametrize("name", BAD_PROFILES)
def test_profile_name_rejects_ini_injection(name):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **REQUIRED, aws_profile_name=name)


@pytest.mark.parametrize("region", ["", "us east", "us-east-1\nfoo = bar", "a=b"])
def test_region_rejects_unsafe_values(region):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **REQUIRED, s3_region=region)


def test_endpoint_setting_validation():
    ok = Settings(_env_file=None, **REQUIRED, s3_endpoint_url="https://s3.example.org/")
    assert ok.s3_endpoint_url == "https://s3.example.org"
    assert Settings(_env_file=None, **REQUIRED).s3_endpoint_url == ""
    for bad in ("s3.example.org", "ftp://x", "https://x y", "https://x\nfoo = bar", "https://x;rm", "http://"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **REQUIRED, s3_endpoint_url=bad)
