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
def settings():
    from bucketeer.config import Settings

    return Settings(_env_file=None, **REQUIRED)
