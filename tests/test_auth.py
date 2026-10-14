"""Login, sessions, throttle and CSRF, against a fake LDAP layer (no network)."""

import json
import re
import ssl
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bucketeer import auth, ldapauth
from bucketeer.config import Settings
from bucketeer.db import Database
from bucketeer.ldapauth import (
    MATCHING_RULE_IN_CHAIN,
    AdAuthenticator,
    BindRejected,
    LdapEntry,
    LdapUnavailable,
    SizeLimitExceeded,
)
from bucketeer.main import create_app

from .conftest import REQUIRED

SVC_DN = "CN=svc,OU=Service,DC=example,DC=org"
SVC_PW = "test-password"
USERS_GROUP = "CN=S3-Users,OU=Groups,DC=example,DC=org"
ADMINS_GROUP = "CN=S3-Admins,OU=Groups,DC=example,DC=org"
TEAM_GROUP = "CN=Team,OU=Groups,DC=example,DC=org"  # member of S3-Users
H = {"X-Requested-With": "bucketeer"}
SECRET_PW = "Sup3r-S3cret-pw!"


def _unescape(v: str) -> str:
    return re.sub(r"\\([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), v)


class FakeLdap:
    """A tiny AD: users, group nesting, unreachable servers. Understands only
    the leaf assertions of the default user filter and the chain-rule filter."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.parents: dict[str, set[str]] = {TEAM_GROUP: {USERS_GROUP}}  # group -> parent groups
        self.down: set[str] = set()
        self.events: list[tuple] = []  # ("bind", url, dn) / ("search", base, flt, scope)
        self.add("alice", groups={TEAM_GROUP}, display="Alice A", mail="alice@example.org")
        self.add("bob", groups={USERS_GROUP, ADMINS_GROUP}, display="Bob B", mail="bob@example.org")
        self.add("carol", groups=set(), display="Carol C", mail="carol@example.org")
        # same mail as alice: ambiguous when logging in by mail
        self.add("alice2", groups={USERS_GROUP}, display="Alice Two", mail="alice@example.org")

    def add(self, sam, groups, display, mail, password=SECRET_PW):
        dn = f"CN={sam},OU=Users,DC=example,DC=org"
        self.users[dn] = {
            "sAMAccountName": sam,
            "mail": mail,
            "userPrincipalName": f"{sam}@example.org",
            "displayName": display,
            "password": password,
            "groups": groups,
        }

    def binds(self):
        return [e for e in self.events if e[0] == "bind"]

    def member_of(self, dn, group):
        seen, todo = set(), list(self.users[dn]["groups"])
        while todo:
            g = todo.pop()
            if g in seen:
                continue
            seen.add(g)
            todo.extend(self.parents.get(g, ()))
        return group in seen

    # connector(url, dn, password) -> session
    def __call__(self, url, dn, password):
        self.events.append(("bind", url, dn))
        if url in self.down:
            raise LdapUnavailable("down")
        if dn == SVC_DN:
            if password != SVC_PW:
                raise BindRejected
        elif dn in self.users:
            # AD accepts an empty password as an anonymous bind: model that trap.
            if password != "" and password != self.users[dn]["password"]:
                raise BindRejected
        else:
            raise BindRejected
        return _FakeSession(self)


class _FakeSession:
    def __init__(self, fake):
        self.fake = fake

    def close(self):
        pass

    def search(self, base, flt, attributes, *, scope="subtree", size_limit=0):
        f = self.fake
        f.events.append(("search", base, flt, scope))
        chain = re.fullmatch(rf"\(memberOf:{re.escape(MATCHING_RULE_IN_CHAIN)}:=(.*)\)", flt)
        if chain:
            assert scope == "base" and base in f.users
            ok = f.member_of(base, _unescape(chain.group(1)))
            return [LdapEntry(base, {"distinguishedName": base})] if ok else []
        values = [
            _unescape(m.group(2)).lower()
            for m in re.finditer(
                r"\((sAMAccountName|mail|userPrincipalName)=((?:[^()\\]|\\[0-9a-fA-F]{2})*)\)", flt
            )
        ]
        found = [
            LdapEntry(dn, {a: u.get(a, "") for a in attributes})
            for dn, u in f.users.items()
            if any(u[k].lower() in values for k in ("sAMAccountName", "mail", "userPrincipalName"))
        ]
        if size_limit and len(found) > size_limit:
            raise SizeLimitExceeded
        return found


def make_settings(tmp_path, **over):
    return Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        **{
            **REQUIRED,
            "ldap_bind_dn": SVC_DN,
            "ldap_required_group_dn": USERS_GROUP,
            "ldap_admin_group_dn": ADMINS_GROUP,
            **over,
        },
    )


class Env:
    def __init__(self, tmp_path, **over):
        self.now = [1_000_000.0]
        self.fake = FakeLdap()
        self.settings = make_settings(tmp_path, **over)
        self.db = Database(":memory:")
        self.app = create_app(
            self.settings,
            db=self.db,
            authenticator=AdAuthenticator(self.settings, self.fake),
            clock=lambda: self.now[0],
        )
        self.client = TestClient(self.app, base_url="https://testserver")

    def login(self, login="alice", password=SECRET_PW, headers=H):
        return self.client.post(
            "/api/login", json={"login": login, "password": password}, headers=headers
        )

    @property
    def sessions(self):
        return self.app.state.sessions

    def audit(self):
        return self.db.audit.query(limit=1000)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


# -- login flow ---------------------------------------------------------------


@pytest.mark.parametrize("login", ["alice", "ALICE", " alice "])
def test_login_by_username_any_case(env, login):
    r = env.login(login)
    assert r.status_code == 200
    assert r.json() == {
        "username": "alice",
        "displayName": "Alice A",
        "email": "alice@example.org",
        "isAdmin": False,
    }


def test_login_by_mail_and_upn_same_username(env):
    for login in ("bob", "bob@example.org"):  # bob's mail and UPN are the same string
        assert env.login(login).json()["username"] == "bob"
    env.fake.users[next(d for d in env.fake.users if "CN=bob" in d)]["mail"] = "robert@example.org"
    assert env.login("robert@example.org").json()["username"] == "bob"  # mail
    assert env.login("bob@example.org").json()["username"] == "bob"  # UPN


def test_filter_injection_is_escaped(env):
    evil = "*)(|(objectClass=*"
    r = env.login(evil)
    assert r.status_code == 401
    (flt,) = [e[2] for e in env.fake.events if e[0] == "search"]
    assert evil not in flt and "*)(|(" not in flt
    assert "\\2a\\29\\28|\\28objectClass=\\2a" in flt
    assert flt.count("(") == env.settings.ldap_user_filter.count("(")  # structure unchanged
    # a wildcard alone matches nobody
    assert env.login("*").status_code == 401


@pytest.mark.parametrize("pw", ["", "   ", "\t"])
def test_empty_password_rejected_before_any_bind(env, pw):
    r = env.login("alice", pw)
    assert r.status_code == 401
    assert env.fake.events == []  # not even a service bind
    assert env.login("", SECRET_PW).status_code == 401
    assert env.fake.events == []


def test_empty_password_rejected_by_authenticator_itself(env):
    import asyncio

    with pytest.raises(ldapauth.InvalidCredentials) as e:
        asyncio.run(AdAuthenticator(env.settings, env.fake).authenticate("alice", ""))
    assert e.value.reason == "empty_password" and env.fake.events == []


def test_ambiguous_mail_refused(env):
    r = env.login("alice@example.org")  # alice and alice2 share it
    assert r.status_code == 401
    assert not env.fake.binds()[1:]  # only the service bind: no user bind attempted


def test_not_in_required_group_refused_without_user_bind(env):
    r = env.login("carol")
    assert r.status_code == 401
    assert len(env.fake.binds()) == 1  # service bind only
    assert any(MATCHING_RULE_IN_CHAIN in e[2] for e in env.fake.events if e[0] == "search")


def test_nested_group_membership_accepted(env):
    # alice is only in Team, which is nested in S3-Users
    r = env.login("alice")
    assert r.status_code == 200
    chain = [e for e in env.fake.events if e[0] == "search" and MATCHING_RULE_IN_CHAIN in e[2]]
    assert chain and chain[0][2] == f"(memberOf:{MATCHING_RULE_IN_CHAIN}:={USERS_GROUP})"
    assert chain[0][3] == "base"


def test_group_dn_is_escaped_in_filter(tmp_path):
    e = Env(tmp_path, ldap_required_group_dn="CN=Evil)(uid=*,DC=x")
    e.login("alice")
    flt = [x[2] for x in e.fake.events if x[0] == "search" and MATCHING_RULE_IN_CHAIN in x[2]][0]
    assert "\\29\\28" in flt and ")(uid" not in flt


def test_empty_required_group_allows_everyone(tmp_path):
    e = Env(tmp_path, ldap_required_group_dn="")
    assert e.login("carol").status_code == 200
    assert not any(USERS_GROUP in x[2] for x in e.fake.events if x[0] == "search")


def test_admin_flag(env):
    assert env.login("bob").json()["isAdmin"] is True
    assert env.login("alice").json()["isAdmin"] is False


def test_empty_admin_group_means_no_admins(tmp_path):
    e = Env(tmp_path, ldap_admin_group_dn="")
    assert e.login("bob").json()["isAdmin"] is False


def test_wrong_password_and_unknown_user_look_identical(env):
    wrong = env.login("alice", "nope")
    unknown = env.login("nobody", "nope")
    ambiguous = env.login("alice@example.org")
    carol = env.login("carol")
    assert {r.status_code for r in (wrong, unknown, ambiguous, carol)} == {401}
    assert len({r.text for r in (wrong, unknown, ambiguous, carol)}) == 1
    assert wrong.json() == {"detail": auth.MSG_INVALID}
    assert "auth" not in wrong.headers.get("set-cookie", "")
    assert env.client.get("/api/me").status_code == 401


def test_username_taken_from_directory_not_typed_login(env):
    assert env.login("ALICE").json()["username"] == "alice"


def test_ldap_unreachable_is_503(env):
    env.fake.down = set(env.settings.ldap_url)
    r = env.login("alice")
    assert r.status_code == 503
    assert r.json() == {"detail": auth.MSG_UNAVAILABLE} and r.json() != {"detail": auth.MSG_INVALID}


def test_service_bind_rejected_is_503(tmp_path):
    e = Env(tmp_path)
    e.settings.ldap_bind_password = type(e.settings.ldap_bind_password)("wrong")
    assert e.login("alice").status_code == 503


def test_failover_to_second_url(tmp_path):
    e = Env(tmp_path, ldap_url="ldaps://dc1:636,ldaps://dc2:636")
    e.fake.down = {"ldaps://dc1:636"}
    assert e.login("alice").status_code == 200
    urls = [b[1] for b in e.fake.binds()]
    assert urls[0] == "ldaps://dc1:636" and set(urls[1:]) == {"ldaps://dc2:636"}
    assert len(urls) == 3  # dc1 fail, dc2 service, dc2 user


def test_ldap_url_must_be_ldaps(tmp_path):
    with pytest.raises(ValidationError, match="ldaps://"):
        make_settings(tmp_path, ldap_url="ldap://dc.example.org")
    with pytest.raises(ValidationError, match="ldaps://"):
        make_settings(tmp_path, ldap_url="ldaps://ok:636,ldap://bad")
    s = make_settings(tmp_path).model_copy(update={"ldap_url": ["ldap://dc"]})
    with pytest.raises(ValueError, match="ldaps"):
        AdAuthenticator(s, FakeLdap())


def test_filter_needs_placeholder(tmp_path):
    with pytest.raises(ValidationError, match="login"):
        make_settings(tmp_path, ldap_user_filter="(sAMAccountName=x)")


def test_bad_request_bodies_do_not_echo_input(env):
    for body in ({"login": "a", "password": 5}, [1], {"login": "a"}):
        r = env.client.post("/api/login", json=body, headers=H)
        assert r.status_code == 400
        assert "5" not in r.text.replace("400", "")
    r = env.client.post("/api/login", content=b"not json", headers=H)
    assert r.status_code == 400
    r = env.login("alice", "x" * 1000)
    assert r.status_code == 400 and env.fake.events == []


# -- real ldap3 plumbing (no network) -------------------------------------------


def test_build_server_tls_settings(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("")
    s = make_settings(tmp_path, ldap_ca_cert_file=str(ca), ldap_timeout=7)
    srv = ldapauth.build_server(s, "ldaps://dc:636")
    assert srv.ssl and srv.tls.validate == ssl.CERT_REQUIRED
    assert srv.tls.ca_certs_file == str(ca) and srv.connect_timeout == 7
    insecure = ldapauth.build_server(make_settings(tmp_path, ldap_tls_verify=False), "ldaps://dc")
    assert insecure.tls.validate == ssl.CERT_NONE


def test_ldap3_session_wrapper_extracts_entries():
    import ldap3

    server = ldap3.Server("fake")
    conn = ldap3.Connection(server, user="cn=x", password="p", client_strategy=ldap3.MOCK_SYNC)
    conn.strategy.add_entry(
        "cn=alice,dc=example,dc=org",
        {"sAMAccountName": "alice", "mail": ["alice@example.org"], "userPassword": "p",
         "objectClass": "person"},
    )
    conn.strategy.add_entry("cn=x", {"userPassword": "p", "objectClass": "person"})
    conn.bind()
    sess = ldapauth._Ldap3Session(conn)
    out = sess.search(
        "dc=example,dc=org", "(sAMAccountName=alice)", ["sAMAccountName", "mail", "displayName"]
    )
    assert out == [
        LdapEntry("cn=alice,dc=example,dc=org",
                  {"sAMAccountName": "alice", "mail": "alice@example.org", "displayName": ""})
    ]


# -- sessions -----------------------------------------------------------------------


def test_cookie_flags_and_contents(env):
    r = env.login("bob")
    cookie = r.headers["set-cookie"]
    low = cookie.lower()
    assert cookie.startswith("__Host-bucketeer=")
    assert "httponly" in low and "secure" in low
    assert "samesite=strict" in low and "path=/" in low and "max-age=3600" in low
    assert "domain" not in low
    assert r.headers["cache-control"] == "no-store"
    token = cookie.split("=", 1)[1].split(";")[0]
    payload = env.sessions._ser.loads(token)
    assert set(payload) == {"username", "display_name", "email", "is_admin", "issued_at"}
    assert payload["username"] == "bob" and payload["is_admin"] is True
    assert SECRET_PW not in token and SECRET_PW not in json.dumps(payload)


def test_insecure_cookie_setting_for_local_dev(tmp_path):
    e = Env(tmp_path, session_cookie_secure=False)
    c = e.login("alice").headers["set-cookie"]
    assert c.startswith("bucketeer=") and "secure" not in c.lower() and "httponly" in c.lower()


def test_me_returns_session_user_or_401(env):
    assert env.client.get("/api/me").status_code == 401
    env.login("bob")
    r = env.client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {
        "username": "bob", "displayName": "Bob B", "email": "bob@example.org", "isAdmin": True
    }


def test_tampered_cookie_rejected(env):
    env.login("alice")
    name = env.sessions.cookie_name
    tok = env.client.cookies.get(name)
    for bad in (tok[:-2] + ("AA" if not tok.endswith("AA") else "BB"), "garbage", tok + "x", ""):
        r = env.client.get("/api/me", headers={"Cookie": f"{name}={bad}"})
        assert r.status_code == 401
    # right payload shape, wrong key
    from itsdangerous import URLSafeSerializer
    forged = URLSafeSerializer("other-secret", salt="bucketeer.session.v1").dumps(
        {"username": "root", "display_name": "", "email": "", "is_admin": True,
         "issued_at": 1_000_000}
    )
    assert env.client.get("/api/me", headers={"Cookie": f"{name}={forged}"}).status_code == 401


def test_session_expires_server_side(env):
    env.login("alice")
    env.now[0] += env.settings.session_max_age - 1
    assert env.client.get("/api/me").status_code == 200
    env.now[0] += 1  # exactly max_age: expired, though the browser still sends the cookie
    assert env.client.get("/api/me").status_code == 401


def test_future_issued_at_rejected(env):
    env.login("alice")
    env.now[0] -= 3600  # clock went back: token appears to come from the future
    assert env.client.get("/api/me").status_code == 401


def test_login_issues_fresh_session(env):
    env.login("alice")
    first = env.client.cookies.get(env.sessions.cookie_name)
    env.now[0] += 100
    env.login("bob")
    second = env.client.cookies.get(env.sessions.cookie_name)
    assert first != second
    assert env.client.get("/api/me").json()["username"] == "bob"


def test_logout_clears_cookie(env):
    env.login("alice")
    r = env.client.post("/api/logout", headers=H)
    assert r.status_code == 200
    sc = r.headers["set-cookie"].lower()
    assert "max-age=0" in sc or "expires=" in sc
    assert env.client.get("/api/me").status_code == 401


def test_require_user_and_admin_dependencies(env):
    mini = FastAPI()
    mini.state.sessions = env.sessions

    @mini.get("/u")
    def u(user: Annotated[auth.SessionUser, Depends(auth.require_user)]):
        return user.username

    @mini.get("/a")
    def a(user: Annotated[auth.SessionUser, Depends(auth.require_admin)]):
        return user.username

    c = TestClient(mini, base_url="https://testserver")
    assert c.get("/u").status_code == 401 and c.get("/a").status_code == 401
    env.login("alice")
    name = env.sessions.cookie_name
    ck = {"Cookie": f"{name}={env.client.cookies.get(name)}"}
    assert c.get("/u", headers=ck).status_code == 200
    assert c.get("/a", headers=ck).status_code == 403
    env.login("bob")
    ck = {"Cookie": f"{name}={env.client.cookies.get(name)}"}
    assert c.get("/a", headers=ck).json() == "bob"


# -- CSRF ----------------------------------------------------------------------------


def test_csrf_header_required_for_login(env):
    r = env.login("alice", headers={})
    assert r.status_code == 403
    assert env.fake.events == []  # rejected before LDAP
    r = env.login("alice", headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 403
    assert "set-cookie" not in r.headers


def test_csrf_header_required_for_logout(env):
    env.login("alice")
    assert env.client.post("/api/logout").status_code == 403
    assert env.client.get("/api/me").status_code == 200  # still logged in


def test_all_unsafe_api_routes_are_csrf_protected(env):
    paths = [
        (m.upper(), re.sub(r"\{[^}]+\}", "x", path))
        for path, ops in env.app.openapi()["paths"].items()
        if path.startswith("/api")
        for m in ops
        if m.upper() not in auth.SAFE_METHODS
    ]
    assert ("POST", "/api/login") in paths
    for method, path in paths:
        assert env.client.request(method, path).status_code == 403, (method, path)


# -- throttle ------------------------------------------------------------------------


def test_throttle_blocks_after_five_failures_without_contacting_ldap(env):
    for _ in range(5):
        assert env.login("alice", "bad").status_code == 401
    n = len(env.fake.events)
    r = env.login("alice", SECRET_PW)  # even the right password
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert len(env.fake.events) == n
    assert env.login("ALICE ", SECRET_PW).status_code == 429  # same normalized login
    assert env.login("bob").status_code == 200  # other logins unaffected


def test_throttle_window_expires(env):
    for _ in range(5):
        env.login("alice", "bad")
    assert env.login("alice").status_code == 429
    env.now[0] += auth.THROTTLE_WINDOW - 1
    assert env.login("alice").status_code == 429  # blocked requests don't extend the window
    env.now[0] += 2
    assert env.login("alice").status_code == 200


def test_success_resets_login_counter(env):
    for _ in range(4):
        env.login("alice", "bad")
    assert env.login("alice").status_code == 200
    for _ in range(4):
        assert env.login("alice", "bad").status_code == 401


def test_throttle_per_ip_across_logins(env):
    for i in range(auth.THROTTLE_IP_LIMIT):
        assert env.login(f"user{i}", "bad").status_code == 401
    assert env.login("bob").status_code == 429
    env.now[0] += auth.THROTTLE_WINDOW + 1
    assert env.login("bob").status_code == 200


def test_ldap_outage_is_not_counted_against_the_user(env):
    env.fake.down = set(env.settings.ldap_url)
    for _ in range(10):
        assert env.login("alice").status_code == 503
    env.fake.down = set()
    assert env.login("alice").status_code == 200


def test_throttle_unit():
    t = [0.0]
    th = auth.LoginThrottle(lambda: t[0], window=10, login_limit=2, ip_limit=3)
    assert th.acquire("a", "1") is None and th.acquire("a", "1") is None
    blocked = th.acquire("a", "1")
    assert blocked and blocked[1] is True
    assert th.acquire("a", "1")[1] is False  # reported only once per block
    t[0] = 11
    assert th.acquire("a", "1") is None


# -- audit ---------------------------------------------------------------------------


def test_audit_entries_and_no_password(env):
    env.login("Alice", "wrong-" + SECRET_PW)
    env.login("alice")
    env.client.post("/api/logout", headers=H)
    for _ in range(6):
        env.login("mallory", "x" + SECRET_PW)
    rows = env.audit()
    actions = [(r["actor"], r["action"]) for r in reversed(rows)]
    assert actions[:3] == [
        ("alice", "login.failure"), ("alice", "login.success"), ("alice", "logout")
    ]
    assert ("mallory", "login.throttled") in actions
    assert actions.count(("mallory", "login.throttled")) == 1
    fail = rows[-1]
    assert fail["details"]["reason"] == "bad_password"
    blob = json.dumps(rows)
    assert SECRET_PW not in blob and "wrong-" not in blob and "xSup3r" not in blob


def test_passwords_never_logged(env, caplog):
    caplog.set_level("DEBUG")
    env.login("alice", "wrong-" + SECRET_PW)
    env.login("alice")
    env.fake.down = set(env.settings.ldap_url)
    env.login("alice")
    assert SECRET_PW not in caplog.text
    assert "reason=bad_password" in caplog.text


# -- review fixes: SASLprep oracle, per-account throttle, service-bind lockout ------------


WEIRD_PASSWORDS = ["\x00", "­", "a\x01b", "a​b\x7f"]  # ldap3's SASLprep raises on these


def _mock_ldap3_connector(settings):
    """The real Ldap3Connector, with ldap3's own MOCK_SYNC strategy as the 'server'."""
    import ldap3

    server = ldap3.Server("mock-dc")
    seed = ldap3.Connection(server, user="cn=seed", password="x", client_strategy=ldap3.MOCK_SYNC)
    for dn, attrs in {
        SVC_DN: {"userPassword": SVC_PW},
        "cn=alice,ou=users,dc=example,dc=org": {
            "userPassword": SECRET_PW, "sAMAccountName": "alice",
            "mail": "alice@example.org", "displayName": "Alice A",
        },
    }.items():
        seed.strategy.add_entry(dn, {"objectClass": "person", **attrs})
    calls = []

    def factory(_s, url):
        calls.append(url)
        return server

    return ldap3, calls, ldapauth.Ldap3Connector(
        settings, client_strategy=ldap3.MOCK_SYNC, server_factory=factory
    )


@pytest.fixture
def real_env(tmp_path):
    s = make_settings(
        tmp_path,
        ldap_user_filter="(|(sAMAccountName={login})(mail={login}))",
        ldap_required_group_dn="",
        ldap_admin_group_dn="",
        ldap_base_dn="dc=example,dc=org",
    )
    ldap3, calls, conn = _mock_ldap3_connector(s)
    db = Database(":memory:")
    app = create_app(s, db=db, authenticator=AdAuthenticator(s, conn))
    yield TestClient(app, base_url="https://testserver"), calls, conn
    db.close()


def test_real_connector_login_works_through_ldap3_mock(real_env):
    client, calls, _ = real_env
    r = client.post("/api/login", json={"login": "alice", "password": SECRET_PW}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "alice" and r.json()["email"] == "alice@example.org"
    r = client.post("/api/login", json={"login": "alice", "password": "wrong"}, headers=H)
    assert r.status_code == 401


@pytest.mark.parametrize("pw", WEIRD_PASSWORDS)
def test_unencodable_password_is_401_not_503_and_never_connects(real_env, pw):
    client, calls, _ = real_env
    for login in ("alice", "nobody"):  # existing and unknown account: identical answer
        r = client.post("/api/login", json={"login": login, "password": pw}, headers=H)
        assert r.status_code == 401, (login, r.status_code)
        assert r.json() == {"detail": auth.MSG_INVALID}
    assert calls == []  # rejected before even the service bind


def test_unencodable_passwords_count_against_throttle(real_env):
    client, _, _ = real_env
    body = {"login": "alice", "password": "\x00"}
    codes = [client.post("/api/login", json=body, headers=H).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


@pytest.mark.parametrize("pw", WEIRD_PASSWORDS + [""])
def test_connector_maps_ldap3_password_errors_to_bind_rejected(real_env, pw):
    _, _, conn = real_env
    with pytest.raises(BindRejected):
        conn("ldaps://dc", "cn=alice,ou=users,dc=example,dc=org", pw)


def _by_spelling_env(tmp_path):
    e = Env(tmp_path)
    next(u for d, u in e.fake.users.items() if "CN=bob" in d)["mail"] = "robert@example.org"
    return e


def test_account_throttle_counts_all_spellings_together(tmp_path):
    e = _by_spelling_env(tmp_path)
    spellings = ["bob", "bob@example.org", "robert@example.org"]  # sam, UPN, mail
    for i in range(auth.THROTTLE_USER_LIMIT):
        assert e.login(spellings[i % 3], "bad").status_code == 401, i
    before = len(e.fake.events)
    r = e.login("robert@example.org", SECRET_PW)  # even the right password
    assert r.status_code == 429 and r.headers["retry-after"]
    # no user bind was made; only the service bind + search to resolve the account
    assert [x for x in e.fake.events[before:] if x[0] == "bind" and x[2] != SVC_DN] == []
    # that spelling is now blocked up front (typed-login counter saturated)...
    n = len(e.fake.events)
    assert e.login("robert@example.org").status_code == 429 and len(e.fake.events) == n
    # ...and so is every other spelling of the account
    assert e.login("bob").status_code == 429
    assert e.login("alice").status_code == 200  # others unaffected
    e.now[0] += auth.THROTTLE_WINDOW + 1
    assert e.login("bob").status_code == 200


def test_unknown_login_throttles_at_the_same_attempt_as_a_known_one(tmp_path):
    e = _by_spelling_env(tmp_path)
    known = [e.login("bob", "bad").status_code for _ in range(6)]
    unknown = [e.login("nobody", "bad").status_code for _ in range(6)]
    assert known == unknown == [401] * 5 + [429]
    assert e.login("bob").text.count("Too many") == e.login("nobody").text.count("Too many") == 1


def test_account_throttle_cleared_by_success_and_not_charged_for_non_members(tmp_path):
    e = Env(tmp_path)
    for _ in range(4):
        e.login("bob", "bad")
    assert e.login("bob").status_code == 200
    for _ in range(4):
        assert e.login("bob", "bad").status_code == 401
    # carol is not in the group: refused before any password check, so no account charge
    for _ in range(4):
        e.login("carol", "bad")
    assert ("user", "carol") not in e.app.state.throttle._hits


def test_rejected_service_bind_does_not_try_other_servers(tmp_path):
    e = Env(tmp_path, ldap_url="ldaps://dc1:636,ldaps://dc2:636,ldaps://dc3:636")
    e.settings.ldap_bind_password = type(e.settings.ldap_bind_password)("stale")
    assert e.login("alice").status_code == 503
    assert [b[1] for b in e.fake.binds()] == ["ldaps://dc1:636"]


def test_connection_error_still_fails_over_to_next_server(tmp_path):
    e = Env(tmp_path, ldap_url="ldaps://dc1:636,ldaps://dc2:636,ldaps://dc3:636")
    e.fake.down = {"ldaps://dc1:636", "ldaps://dc2:636"}
    assert e.login("alice").status_code == 200
    urls = [b[1] for b in e.fake.binds()]
    assert urls[:3] == ["ldaps://dc1:636", "ldaps://dc2:636", "ldaps://dc3:636"]
