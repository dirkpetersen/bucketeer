"""mock/demo.py: the demo app works in-process, and production can never be a demo."""

import os

from fastapi.testclient import TestClient

import mock.demo as demo
from bucketeer.config import Settings
from bucketeer.croit import CroitClient
from bucketeer.db import Database
from bucketeer.main import create_app
from mock.croit_mock import create_app as create_mock

from .conftest import REQUIRED
from .test_admin import Transport

H = {"X-Requested-With": "bucketeer"}
TOKEN = "demo-test-token"


def demo_client(tmp_path):
    mock = create_mock(seed=True, task_seconds=0, prefix="/api", admin_token=TOKEN)
    demo.seed_demo(mock.state.croit)
    settings = demo.demo_settings(
        port=8000, backend="croit", croit_url="http://croit.test/api", croit_token=TOKEN,
        s3_url="http://127.0.0.1:1", data_dir=str(tmp_path),
    )
    croit = CroitClient(settings, poll_interval=0.01, transport=Transport(mock))
    return TestClient(demo.build_demo_app(settings, croit=croit, db=Database(":memory:")))


def test_demo_smoke_login_alice_and_dana(tmp_path):
    c = demo_client(tmp_path)
    assert c.get("/api/demo").json() == {"demo": True}
    r = c.post("/api/login", json={"login": "Alice@demo.example", "password": "demo"}, headers=H)
    assert r.status_code == 200 and r.json()["username"] == "alice" and not r.json()["isAdmin"]
    assert c.get("/api/me").json()["displayName"] == "Alice Anderson"
    c.post("/api/logout", headers=H)
    r = c.post("/api/login", json={"login": "dana", "password": "demo"}, headers=H)
    assert r.status_code == 200 and r.json()["isAdmin"] is True
    bad = c.post("/api/login", json={"login": "dana", "password": "nope"}, headers=H)
    assert bad.status_code == 401


def test_demo_settings_ignore_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CROIT_URL", "https://real-croit.example/api")
    demo.scrub_environment()
    assert "CROIT_URL" not in os.environ
    s = demo.demo_settings(port=1, backend="croit", croit_url="http://127.0.0.1:2/api",
                           croit_token="t", s3_url="http://127.0.0.1:3", data_dir=str(tmp_path))
    assert s.host == "127.0.0.1" and s.session_cookie_secure is False
    assert s.croit_url == "http://127.0.0.1:2/api"


def test_demo_defaults_to_the_rgw_backend(tmp_path):
    s = demo.demo_settings(port=1, s3_url="http://127.0.0.1:3", data_dir=str(tmp_path),
                           rgw_admin_url="http://127.0.0.1:4", rgw_admin_access_key="K",
                           rgw_admin_secret_key="S")
    assert s.admin_backend == "rgw" and s.croit_url == ""
    assert demo.parse_args([]).backend == "rgw"
    assert demo.parse_args(["--backend", "croit"]).backend == "croit"


def test_production_app_is_never_demo(settings, monkeypatch):
    for name in ("DEMO", "BUCKETEER_DEMO", "DEMO_MODE"):
        monkeypatch.setenv(name, "1")
    app = create_app(settings)
    assert app.state.demo is False
    assert TestClient(app).get("/api/demo").json() == {"demo": False}
    assert not any("demo" in f for f in Settings.model_fields)
    assert Settings(_env_file=None, **REQUIRED).model_dump().get("demo") is None


def test_sigterm_cleans_up(tmp_path):
    import re
    import signal
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    def free():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    script = Path(demo.__file__)
    p = subprocess.Popen(
        [sys.executable, str(script), "--port", str(free()), "--croit-port", str(free()),
         "--moto-port", str(free()),
         "--admin-ops-port", str(free())],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp_path,
    )
    data_dir = None
    try:
        deadline = time.time() + 40
        for line in p.stdout:
            m = re.search(r"Data dir:\s+(\S+)", line)
            if m:
                data_dir = Path(m.group(1))
            if "Ctrl+C stops" in line or time.time() > deadline:
                break
        assert data_dir and (data_dir / "aws").is_dir()
        time.sleep(1.5)  # let uvicorn install its handlers
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        assert not data_dir.exists()
    finally:
        if p.poll() is None:
            p.kill()


def test_bucketeer_never_imports_mock():
    import re
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "bucketeer"
    bad = re.compile(r"^\s*(import\s+mock\b|from\s+mock\b)|[\"']mock[./\"']", re.M)
    hits = [str(f) for f in pkg.rglob("*.py") if bad.search(f.read_text())]
    assert hits == []


def test_demo_directory_lookup_user_and_sharing(tmp_path):
    import asyncio

    d = demo.DemoDirectory()
    assert asyncio.run(d.lookup_user("ERIN@demo.example")).username == "erin"
    import pytest

    from bucketeer.ldapauth import InvalidCredentials

    with pytest.raises(InvalidCredentials):
        asyncio.run(d.lookup_user("nobody"))
    with demo_client(tmp_path) as c:
        c.post("/api/login", json={"login": "alice", "password": demo.DEMO_PASSWORD}, headers=H)
        r = c.get("/api/buckets/alice-data/share-lookup", params={"q": "bob"})
        assert r.status_code == 200 and r.json()["displayName"] == "Bob Brown"
