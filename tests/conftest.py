import pytest

REQUIRED = {
    "session_secret": "test-secret",
    "ldap_url": "ldaps://dc.example.org:636",
    "ldap_base_dn": "DC=example,DC=org",
    "ldap_bind_dn": "svc@example.org",
    "ldap_bind_password": "test-password",
    "croit_url": "https://croit.example.org/api",
    "croit_api_token": "test-token",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep the developer's real environment and .env out of the tests."""
    from bucketeer.config import Settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def settings(tmp_path):
    from bucketeer.config import Settings

    return Settings(_env_file=None, data_dir=str(tmp_path / "data"), **REQUIRED)


@pytest.fixture(scope="session", autouse=True)
def repo_db_untouched():
    """Fail the session if any test creates or changes the repo's real database."""
    from pathlib import Path

    db = Path(__file__).resolve().parent.parent / "data" / "bucketeer.sqlite3"

    def stamp():
        return (db.stat().st_mtime_ns, db.stat().st_size) if db.exists() else None

    before = stamp()
    yield
    assert stamp() == before, f"tests created or modified {db}"


@pytest.fixture(scope="session")
def moto_server():
    """A moto S3 server on a free localhost port for the whole test session."""
    import os

    from mock.s3_bridge import endpoint_url, start_moto

    saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    server = start_moto()
    server.endpoint = endpoint_url(server)
    yield server
    server.stop()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def s3_endpoint(moto_server):
    """The moto endpoint URL, emptied of buckets before each test."""
    import httpx

    httpx.post(f"{moto_server.endpoint}/moto-api/reset", timeout=10).raise_for_status()
    return moto_server.endpoint
