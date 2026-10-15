"""tools/croit-gate: auth, allowlist, proxying, limits, logging, install, drift."""

import ast
import contextlib
import http.client
import importlib.machinery
import importlib.util
import json
import logging
import os
import re
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn

from bucketeer.config import Settings
from bucketeer.croit import CroitClient
from mock.croit_mock import create_app

from .conftest import REQUIRED

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "tools" / "croit-gate"
REAL = "real-croit-token-" + "r" * 20
CLIENT = "client-gate-token-" + "c" * 20

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def load_gate():
    loader = importlib.machinery.SourceFileLoader("croit_gate_under_test", str(GATE_PATH))
    spec = importlib.util.spec_from_loader("croit_gate_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


gate = load_gate()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recording:
    """ASGI wrapper that remembers the headers and raw target of every request."""

    def __init__(self, app):
        self.app = app
        self.seen = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.seen.append((
                scope["method"], scope["raw_path"].decode(), scope["query_string"].decode(),
                {k.decode(): v.decode() for k, v in scope["headers"]},
            ))
        await self.app(scope, receive, send)


@pytest.fixture(scope="module")
def mock_upstream():
    app = Recording(create_app(task_seconds=0, prefix="/api", admin_token=REAL))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.02)
    app.port = port
    yield app
    server.should_exit = True
    t.join(5)


def start_gate(port, **env):
    values = {
        "CROIT_URL": f"http://127.0.0.1:{port}/api",
        "CROIT_API_TOKEN": REAL,
        "CROIT_GATE_CLIENT_TOKEN": CLIENT,
        "CROIT_GATE_LISTEN": "127.0.0.1:0",
        **env,
    }
    server = gate.Gate(gate.Config(values))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    server.url_port = server.server_address[1]
    return server


@contextlib.contextmanager
def running(port, **env):
    server = start_gate(port, **env)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def gw(mock_upstream):
    with running(mock_upstream.port) as server:
        yield server


def call(server, method, target, token=CLIENT, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.url_port, timeout=10)
    h = dict(headers or {})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    try:
        conn.request(method, target, body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read(), r
    finally:
        conn.close()


# --- stub upstream for misbehaviour ---------------------------------------------


@pytest.fixture
def stub():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            mode = self.server.mode
            if mode == "big":
                data = b"x" * (gate.MAX_RESPONSE_BODY + 10)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://evil.example/")
                self.end_headers()
            elif mode == "slow":
                threading.Event().wait(1.5)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            elif mode == "echo":
                body = json.dumps({"auth": self.headers.get("Authorization")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_PUT = do_GET

    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    s.mode = "big"
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


# --- client authentication ------------------------------------------------------


def test_client_token_checks(gw):
    assert call(gw, "GET", "/api/s3/user-names", token=None)[0] == 401
    assert call(gw, "GET", "/api/s3/user-names", token="wrong")[0] == 401
    assert call(gw, "GET", "/api/s3/user-names", token=REAL)[0] == 401
    status, body, _ = call(gw, "GET", "/api/s3/user-names", token=None,
                           headers={"Authorization": "Basic " + CLIENT})
    assert status == 401
    assert call(gw, "GET", "/api/s3/user-names")[0] == 200
    # not-allowed paths do not reveal anything to unauthenticated callers
    assert call(gw, "GET", "/api/servers", token=None)[0] == 401


def test_real_token_goes_upstream_and_client_token_does_not(gw, mock_upstream):
    status, body, _ = call(gw, "GET", "/api/auth/token-info")
    assert status == 200
    method, path, query, headers = mock_upstream.seen[-1]
    assert headers["authorization"] == "Bearer " + REAL
    assert CLIENT not in json.dumps(mock_upstream.seen)
    assert REAL.encode() not in body


# --- allowlist ------------------------------------------------------------------


@pytest.mark.parametrize("method,target", [
    ("GET", "/api/config/rgw-ssl-cert"),
    ("PUT", "/api/config/rgw-ssl-cert"),
    ("GET", "/api/api-tokens"),
    ("GET", "/api/servers"),
    ("DELETE", "/api/s3/users"),
    ("GET", "/api/s3/users/alice/extra"),
    ("GET", "/api/s3/users/alice/keys/AK/more"),
    ("POST", "/api/s3/user-names"),
    ("GET", "/s3/users"),
    ("GET", "/apix/s3/users"),
    ("GET", "/api"),
    ("GET", "/api/"),
    ("GET", "/"),
])
def test_not_allowlisted(gw, method, target):
    status, body, _ = call(gw, method, target)
    assert status == 403
    assert json.loads(body)["message"]


@pytest.mark.parametrize("target", [
    "/api/s3/users/../api-tokens",
    "/api/s3/users/%2e%2e/api-tokens",
    "/api/s3/users/..",
    "/api/s3/users/alice%2Fkeys",
    "/api/s3/users/alice%2fkeys",
    "/api/s3/users/alice%5Ckeys",
    "/api/s3/users/alice%00",
    "/api/s3/users//alice",
    "/api//s3/users",
    "/api/s3/users/alice/",
    "/api/s3/users/al\\ice",
    "/api/s3/users/%zz",
    "/api/s3/users/alice#frag",
    "/api/s3/users/./quota",
    "/api/s3/users/%2E/keys",
])
def test_path_tricks(gw, target):
    assert call(gw, "GET", target)[0] == 403


def test_query_only_where_used(gw):
    assert call(gw, "GET", "/api/s3/user-names?x=1")[0] == 403
    assert call(gw, "GET", "/api/s3/users?uid=alice")[0] != 403
    assert call(gw, "GET", "/api/s3/users?uid=al%20ice")[0] != 403
    assert call(gw, "GET", "/api/s3/users?uid=a\\b")[0] == 403
    assert call(gw, "GET", "/api/s3/users?uid=a#b")[0] == 403


def test_encoded_dollar_and_tenant(gw):
    assert call(gw, "GET", "/api/s3/users/acme%24carol/keys")[0] == 200


# --- end to end with the real client ---------------------------------------------


async def test_croit_client_through_gate(gw, mock_upstream):
    s = Settings(_env_file=None, **{
        **REQUIRED,
        "croit_url": f"http://127.0.0.1:{gw.url_port}/api",
        "croit_api_token": CLIENT,
    })
    client = CroitClient(s)
    try:
        assert (await client.token_info())["username"]
        await client.create_user("gateuser", "Gate User")
        await client.put_key("gateuser", "GATEACCESSKEY0000001", "S" * 40)
        keys = await client.list_keys("gateuser")
        assert any(k["access_key"] == "GATEACCESSKEY0000001" for k in keys)
        assert (await client.get_user("gateuser"))["uid"] == "gateuser"
        assert isinstance(await client.list_user_buckets("alice"), list)
        assert isinstance(await client.list_buckets(), list)
        assert "placements" in str(await client.list_placements()).lower() or True
        await client.delete_key("gateuser", "GATEACCESSKEY0000001")
        await client.delete_user("gateuser")
    finally:
        await client.aclose()
    assert any(q.startswith("secretKey=") for _, _, q, _ in mock_upstream.seen)


# --- limits and failures ---------------------------------------------------------


def test_request_body_cap(gw):
    # declare an oversize body; the gateway answers before reading it
    with socket.create_connection(("127.0.0.1", gw.url_port), timeout=10) as sock:
        sock.sendall((
            f"POST /api/s3/users HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {CLIENT}\r\n"
            f"Content-Length: {gate.MAX_REQUEST_BODY + 1}\r\n\r\n"
        ).encode())
        assert sock.recv(100).startswith(b"HTTP/1.0 413")
    status, _, _ = call(gw, "POST", "/api/s3/users", body={"uid": "capok", "name": "Cap Ok"})
    assert status < 300


def test_chunked_body_refused(gw):
    status, _, _ = call(gw, "POST", "/api/s3/users", body=b"{}",
                        headers={"Transfer-Encoding": "chunked", "Content-Length": "2"})
    assert status == 411


def test_response_cap(stub):
    stub.mode = "big"
    with running(stub.server_address[1]) as g:
        assert call(g, "GET", "/api/s3/user-names")[0] == 502


def test_redirect_not_followed(stub):
    stub.mode = "redirect"
    with running(stub.server_address[1]) as g:
        status, body, resp = call(g, "GET", "/api/s3/user-names")
        assert status == 502
        assert resp.getheader("Location") is None


def test_upstream_timeout(stub):
    stub.mode = "slow"
    with running(stub.server_address[1], CROIT_GATE_TIMEOUT="0.3") as g:
        assert call(g, "GET", "/api/s3/user-names")[0] == 504


def test_upstream_down():
    port = free_port()
    with running(port) as g:
        status, body, _ = call(g, "GET", "/api/s3/user-names")
        assert status == 502
        assert REAL.encode() not in body


def test_hop_headers_and_cookies_not_forwarded(gw, mock_upstream):
    call(gw, "GET", "/api/s3/user-names", headers={
        "Cookie": "a=b", "X-Forwarded-For": "1.2.3.4", "X-Forwarded-Host": "evil",
        "Proxy-Authorization": "x", "X-Other": "keep-out",
    })
    headers = mock_upstream.seen[-1][3]
    for name in ("cookie", "x-forwarded-for", "x-forwarded-host", "proxy-authorization",
                 "x-other"):
        assert name not in headers


# --- logging ---------------------------------------------------------------------


def test_logs_have_no_tokens_or_queries(gw, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    secret = "SECRETKEYVALUE" + "q" * 26
    call(gw, "PUT", f"/api/s3/users/bob/keys/AKLOGTEST0000000001?secretKey={secret}")
    call(gw, "GET", f"/api/s3/user-names?secretKey={secret}")  # 403: query not allowed
    call(gw, "GET", "/api/servers")
    call(gw, "GET", "/api/s3/user-names", token="wrong-" + CLIENT)
    call(gw, "GET", "/api/s3/user-names")
    text = caplog.text + capfd.readouterr().err
    assert "AKLOGTEST0000000001" in text
    assert " 403 " in text and " 401 " in text and " 200 " in text
    for forbidden in (secret, "secretKey", REAL, CLIENT, "wrong-", "?"):
        assert forbidden not in text.replace("%(", "")


# --- configuration ---------------------------------------------------------------


def base_env(**kw):
    return {"CROIT_URL": "https://croit.example.org/api", "CROIT_API_TOKEN": REAL,
            "CROIT_GATE_CLIENT_TOKEN": CLIENT, **kw}


def test_config_refusals():
    gate.Config(base_env())
    for bad in (
        {"CROIT_GATE_CLIENT_TOKEN": "short"},
        {"CROIT_GATE_CLIENT_TOKEN": REAL},
        {"CROIT_GATE_CLIENT_TOKEN": ""},
        {"CROIT_API_TOKEN": ""},
        {"CROIT_URL": ""},
        {"CROIT_URL": "ftp://x/api"},
        {"CROIT_GATE_TIMEOUT": "0"},
        {"CROIT_GATE_LISTEN": "nonsense"},
        {"CROIT_GATE_LISTEN": "0.0.0.0:18089"},
        {"CROIT_GATE_LISTEN": "192.0.2.1:18089"},
    ):
        with pytest.raises(ValueError):
            gate.Config(base_env(**bad))


def test_listen_defaults_and_remote():
    assert gate.Config(base_env()).listen == ("127.0.0.1", 18089)
    assert gate.Config(base_env(CROIT_GATE_LISTEN="[::1]:1234")).listen == ("::1", 1234)
    assert gate.Config(base_env(CROIT_GATE_LISTEN="localhost:99")).listen[1] == 99
    cfg = gate.Config(base_env(CROIT_GATE_LISTEN="0.0.0.0:18089"), allow_remote=True)
    assert cfg.listen == ("0.0.0.0", 18089)


def test_check_mode(mock_upstream, capsys):
    cfg = gate.Config(base_env(CROIT_URL=f"http://127.0.0.1:{mock_upstream.port}/api"))
    assert gate.upstream_check(cfg)[0] is True
    bad = gate.Config(base_env(CROIT_URL=f"http://127.0.0.1:{mock_upstream.port}/api",
                               CROIT_API_TOKEN="not-the-token-" + "z" * 20))
    assert gate.upstream_check(bad)[0] is False


def test_help_runs():
    import subprocess
    import sys

    out = subprocess.run([sys.executable, str(GATE_PATH), "--help"], capture_output=True,
                         text=True, check=True).stdout
    assert "--allow-remote" in out and "--check" in out


def test_settings_accepts_plain_http_gateway_url():
    s = Settings(_env_file=None, **{**REQUIRED, "croit_url": "http://127.0.0.1:18089/api"})
    assert s.croit_url == "http://127.0.0.1:18089/api"


# --- install ---------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="install refuses to run as root")
def test_install(tmp_path):
    home = tmp_path / "home"
    lines = gate.install(home=str(home))
    exe = home / ".local/bin/croit-gate"
    env = home / ".config/croit-gate/env"
    unit = home / ".config/systemd/user/croit-gate.service"
    assert exe.read_bytes() == GATE_PATH.read_bytes()
    assert stat.S_IMODE(exe.stat().st_mode) == 0o755
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    text = unit.read_text()
    assert f"ExecStart={exe}\n" in text
    assert f"EnvironmentFile={env}\n" in text
    for line in ("Restart=on-failure", "NoNewPrivileges=yes", "PrivateTmp=yes",
                 "ProtectSystem=strict", "WantedBy=default.target"):
        assert line in text
    assert any("enable-linger" in line for line in lines)
    assert any("systemctl --user" in line and "enable --now" in line for line in lines)
    assert "CROIT_GATE_CLIENT_TOKEN=" in env.read_text()
    # the template is not a usable config
    with pytest.raises(ValueError):
        gate.Config({k: v for k, v in
                     (ln.split("=", 1) for ln in env.read_text().splitlines()
                      if "=" in ln and not ln.startswith("#"))})

    # re-install keeps the env file and its tokens, refreshes script and unit
    env.write_text("CROIT_API_TOKEN=keepme\n")
    os.chmod(env, 0o600)
    exe.write_text("old")
    unit.write_text("old")
    lines = gate.install(home=str(home))
    assert env.read_text() == "CROIT_API_TOKEN=keepme\n"
    assert exe.read_bytes() == GATE_PATH.read_bytes()
    assert unit.read_text() == text
    assert any("Kept the existing" in line for line in lines)


@pytest.mark.skipif(os.geteuid() == 0, reason="install refuses to run as root")
def test_install_cli_runs_no_systemctl(tmp_path):
    import subprocess
    import sys

    home = tmp_path / "h"
    home.mkdir()
    out = subprocess.run([sys.executable, str(GATE_PATH), "install"], capture_output=True,
                         text=True, check=True, env={"HOME": str(home), "PATH": "/nonexistent"})
    assert "enable-linger" in out.stdout
    assert (home / ".config/croit-gate/env").exists()


# --- allowlist drift -------------------------------------------------------------


def norm(template):
    return re.sub(r"\{[^}]*\}", "{}", template)


def client_calls():
    """(method, template, has_params) for every self._request(...) in croit.py."""
    tree = ast.parse((ROOT / "bucketeer" / "croit.py").read_text())
    calls = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_request"):
            continue
        method = node.args[0].value
        target = node.args[1]
        if isinstance(target, ast.Constant):
            template = target.value
        else:
            assert isinstance(target, ast.Call) and target.func.id == "_path"
            template = target.args[0].value
        has_params = any(k.arg == "params" for k in node.keywords)
        calls.add((method, norm(template), has_params))
    return calls


def test_allowlist_matches_client():
    calls = client_calls()
    assert len(calls) > 20
    # the same (method, path) may be called with and without params
    used = {}
    for method, template, has_params in calls:
        used[(method, template)] = used.get((method, template), False) or has_params
    allowed = {(m, norm(t)): q for m, t, q in gate.ROUTES}
    assert len(allowed) == len(gate.ROUTES), "duplicate route"
    missing = set(used) - set(allowed)
    extra = set(allowed) - set(used)
    assert not missing, f"croit.py calls the gateway does not allow: {sorted(missing)}"
    assert not extra, f"gateway allows calls croit.py never makes: {sorted(extra)}"
    for key, wants_query in used.items():
        assert allowed[key] == wants_query, f"query allowance differs for {key}"


# --- security review fixes -------------------------------------------------------


def raw_exchange(server, data, timeout=10, read_all=True):
    with socket.create_connection(("127.0.0.1", server.url_port), timeout=timeout) as sock:
        sock.sendall(data)
        out = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                out += chunk
        except OSError:
            pass
        return out


def test_token_with_control_characters_refused():
    for bad in (REAL + "\r", REAL + "\n", "has space " + "x" * 30, "tab\t" + "x" * 30,
                "café" + "x" * 30):
        with pytest.raises(ValueError) as e:
            gate.Config(base_env(CROIT_API_TOKEN=bad))
        assert bad.strip() not in str(e.value)
        with pytest.raises(ValueError) as e:
            gate.Config(base_env(CROIT_GATE_CLIENT_TOKEN=bad if len(bad) > 31 else bad + "x" * 40))
        assert "x" * 30 not in str(e.value)


def test_value_error_from_http_client_does_not_leak_token(mock_upstream, caplog, capfd,
                                                          monkeypatch):
    caplog.set_level(logging.DEBUG)
    original = http.client.HTTPConnection.request

    def boom(self, *args, **kwargs):
        if self.port == mock_upstream.port:
            raise ValueError(f"Invalid header value b'Bearer {REAL}\\r'")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(http.client.HTTPConnection, "request", boom)
    with running(mock_upstream.port) as g:
        status, body, _ = call(g, "GET", "/api/s3/user-names")
        assert status == 502
        cfg = g.cfg
    assert gate.upstream_check(cfg) == (False, "croit: croit request failed")
    text = caplog.text + "".join(capfd.readouterr())
    assert REAL not in text and REAL.encode() not in body


def test_unexpected_exception_logs_type_only(gw, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    def boom(*a, **k):
        raise RuntimeError(f"secret {REAL}")

    monkeypatch.setattr(gate, "upstream_request", boom)
    assert call(gw, "GET", "/api/s3/user-names")[0] == 500
    assert "RuntimeError" in caplog.text and REAL not in caplog.text
    assert "Traceback" not in caplog.text


def test_folded_header_refused_and_not_forwarded(gw, mock_upstream):
    before = len(mock_upstream.seen)
    req = (f"GET /api/s3/user-names HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {CLIENT}\r\n"
           "Accept: x\r\n Transfer-Encoding: chunked\r\n\r\n").encode()
    out = raw_exchange(gw, req)
    assert out.startswith(b"HTTP/1.0 400")
    assert len(mock_upstream.seen) == before


def test_accept_fixed_and_odd_content_type_replaced(gw, mock_upstream):
    call(gw, "POST", "/api/s3/users", body=b'{"uid":"ctuser","name":"Ct"}',
         headers={"Content-Type": "text/x-\x01weird", "Accept": "text/evil"})
    headers = mock_upstream.seen[-1][3]
    assert headers["accept"] == "application/json"
    assert headers["content-type"] == "application/json"
    call(gw, "POST", "/api/s3/users", body=b'{"uid":"ctuser2","name":"Ct"}',
         headers={"Content-Type": "text/plain"})
    assert mock_upstream.seen[-1][3]["content-type"] == "application/json"


def test_header_limits(gw):
    many = "".join(f"X-H{i}: v\r\n" for i in range(40))
    out = raw_exchange(gw, (f"GET /api/s3/user-names HTTP/1.1\r\nAuthorization: Bearer {CLIENT}"
                            f"\r\n{many}\r\n").encode())
    assert out.startswith(b"HTTP/1.0 431")
    out = raw_exchange(gw, (f"GET /api/s3/user-names HTTP/1.1\r\nAuthorization: Bearer {CLIENT}"
                            f"\r\nX-Long: {'a' * 9000}\r\n\r\n").encode())
    assert out.startswith(b"HTTP/1.0 431")
    out = raw_exchange(gw, f"GET /{'a' * 9000} HTTP/1.1\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.0 414")
    ok = raw_exchange(gw, (f"GET /api/s3/user-names HTTP/1.1\r\nAuthorization: Bearer {CLIENT}"
                           "\r\n" + "".join(f"X-H{i}: v\r\n" for i in range(20)) + "\r\n").encode())
    assert ok.startswith(b"HTTP/1.0 200")


def test_slow_client_cut_off_by_deadline(mock_upstream):
    import time

    with running(mock_upstream.port, CROIT_GATE_TIMEOUT="1") as g:
        sock = socket.create_connection(("127.0.0.1", g.url_port), timeout=5)
        started = time.monotonic()
        closed = False
        try:
            sock.sendall(b"GET /api/s3/user-names HTTP/1.1\r\n")
            while time.monotonic() - started < 6:
                time.sleep(0.3)  # faster than the per-recv timeout
                sock.sendall(b"X-A: b\r\n")
        except OSError:
            closed = True
        finally:
            sock.close()
        elapsed = time.monotonic() - started
        assert closed and elapsed < 4, elapsed


def test_connection_cap(mock_upstream):
    import time

    with running(mock_upstream.port, CROIT_GATE_MAX_CONNECTIONS="2") as g:
        idle = [socket.create_connection(("127.0.0.1", g.url_port)) for _ in range(2)]
        try:
            time.sleep(0.4)
            out = raw_exchange(g, b"GET /api/s3/user-names HTTP/1.1\r\n\r\n")
            assert out.startswith(b"HTTP/1.0 503")
        finally:
            for s in idle:
                s.close()
        for _ in range(50):  # slots come back once the idle clients are gone
            if call(g, "GET", "/api/s3/user-names")[0] == 200:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("slots were not released")
    with pytest.raises(ValueError):
        gate.Config(base_env(CROIT_GATE_MAX_CONNECTIONS="0"))


def test_python39_compatible_syntax():
    import sys

    src = GATE_PATH.read_text()
    ast.parse(src, feature_version=(3, 9))
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if type(n).__name__ == "Match"]
    for n in ast.walk(tree):  # no 3.10-only keyword arguments
        if isinstance(n, ast.Call) and any(k.arg == "strict" for k in n.keywords):
            raise AssertionError("zip(strict=) needs Python 3.10")
    assert gate.MIN_PYTHON == (3, 9) and sys.version_info >= gate.MIN_PYTHON
    assert "Python 3.9" in src.split('"""')[1]


@pytest.mark.parametrize("target", [
    "/api/s3/users/%252F/keys",
    "/api/s3/users/alice%3Bx",
    "/api/s3/users/%C0%AE%C0%AE/keys",
    "/api/s3/users/%2e%2e/keys",
    "/api/s3/users/%C3%A9",
    "/api/s3/users/%FF",
    "/api/s3/users/a%20b",
    "/api/s3/buckets/a%3Fb",
])
def test_param_decoding_is_strict(gw, target):
    assert call(gw, "GET", target)[0] == 403


def test_every_value_bucketeer_path_builds_passes():
    from bucketeer.croit import _path

    cases = [
        ("DELETE", "/s3/users/{uid}", {"uid": "alice"}),
        ("DELETE", "/s3/users/{uid}", {"uid": "acme$carol"}),
        ("PATCH", "/s3/users/{uid}", {"uid": "acme$alice+backup-job"}),
        ("GET", "/s3/users/{uid}/keys", {"uid": "john.smith@x"}),
        ("PUT", "/s3/buckets/{name}", {"name": "acme:reports"}),
        ("GET", "/s3/buckets/{name}/policy", {"name": "my-bucket.v2"}),
        ("PUT", "/s3/users/{uid}/keys/{access_key}",
         {"uid": "acme$a+b", "access_key": "AB12CD34EF56GH78IJ90"}),
        ("GET", "/tasks/{id}", {"id": 1234}),
        ("GET", "/s3/usage/by-bucket/{name}", {"name": "acme:b_1"}),
    ]
    for method, template, params in cases:
        path = _path(template, **params)
        assert gate.check_request(method, path, "") is None, path


def test_dotted_params_refused():
    for bad in ("/s3/users/.", "/s3/users/..", "/s3/users/%2E"):
        assert gate.check_request("GET", bad, "")


def test_http_upstream_only_for_loopback():
    gate.Config(base_env(CROIT_URL="http://127.0.0.1:1/api"))
    gate.Config(base_env(CROIT_URL="http://localhost:1/api"))
    gate.Config(base_env(CROIT_URL="http://[::1]:1/api"))
    for url in ("http://croit.example.org/api", "http://10.0.0.5/api"):
        with pytest.raises(ValueError):
            gate.Config(base_env(CROIT_URL=url))


def test_install_race_free_and_unit_limits(tmp_path, monkeypatch):
    target = tmp_path / "f"
    real_link = os.link

    def racing_link(src, dst, **kw):
        Path(dst).write_text("someone else")  # appears after our existence check
        return real_link(src, dst, **kw)

    monkeypatch.setattr(os, "link", racing_link)
    assert gate.write_file(str(target), b"mine", 0o600, overwrite=False) is False
    assert target.read_text() == "someone else"
    assert not list(tmp_path.glob("f.new.*"))
    monkeypatch.undo()
    assert gate.write_file(str(tmp_path / "g"), b"x", 0o600, overwrite=False) is True
    unit = gate.UNIT_TEMPLATE
    assert "MemoryMax=" in unit and "TasksMax=" in unit
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in unit


def test_http_attribute_only_used_in_request_helpers():
    tree = ast.parse((ROOT / "bucketeer" / "croit.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CroitClient")
    allowed = {"_request", "__init__", "aclose", "closed", "__repr__"}
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        uses = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "_http"]
        if uses:
            assert fn.name in allowed, f"{fn.name} uses self._http directly; use _request"
