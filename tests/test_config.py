from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from bucketeer.config import Settings, load_settings

from .conftest import REQUIRED

ENV_DEFAULT = Path(__file__).resolve().parent.parent / ".env.default"


def test_env_default_keys_match_fields():
    keys = {k.lower() for k in dotenv_values(ENV_DEFAULT)}
    assert keys == set(Settings.model_fields)


def test_env_default_values_match_code_defaults(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name.upper(), value)
    # Only the REQUIRED (empty in the file) keys are overridden by the env above.
    from_file = Settings(_env_file=ENV_DEFAULT)
    from_code = Settings(_env_file=None)
    assert from_file.model_dump() == from_code.model_dump()


def test_required_are_exactly_the_empty_keys_without_empty_default():
    required = {n for n, f in Settings.model_fields.items() if f.is_required()}
    assert required == set(REQUIRED)
    values = dotenv_values(ENV_DEFAULT)
    for name in required:
        assert values[name.upper()] == ""


def write_env(path, extra=""):
    lines = [f"{k.upper()}={v}" for k, v in REQUIRED.items()]
    path.write_text("\n".join(lines) + "\n" + extra)


def test_missing_required_lists_all(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    with pytest.raises(SystemExit) as exc:
        load_settings()
    msg = str(exc.value)
    for name in REQUIRED:
        assert name.upper() in msg


def test_empty_required_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**REQUIRED, "session_secret": ""})


def test_parsing(monkeypatch):
    monkeypatch.setenv("LDAP_URL", "ldaps://a:636, ldaps://b:636")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("PORT", "9000")
    s = Settings(_env_file=None, **{k: v for k, v in REQUIRED.items() if k != "ldap_url"})
    assert s.ldap_url == ["ldaps://a:636", "ldaps://b:636"]
    assert s.session_cookie_secure is False
    assert s.port == 9000


def test_invalid_log_level():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="loud", **REQUIRED)


def test_secrets_not_in_repr(settings):
    text = repr(settings) + str(settings)
    for secret in ("test-secret", "test-password", "test-token"):
        assert secret not in text


def test_env_in_parent_directory_is_not_loaded(monkeypatch, tmp_path):
    write_env(tmp_path / ".env")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(SystemExit):
        load_settings()


def test_environment_beats_dotenv(monkeypatch, tmp_path):
    write_env(tmp_path / ".env", 'PORT=8123\nAPP_NAME="Bucket Eer"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PORT", "9555")
    s = load_settings()
    assert s.port == 9555
    assert s.app_name == "Bucket Eer"
