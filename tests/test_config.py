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


# Settings that are REQUIRED only for one admin backend (a model validator, so they have
# a code default of ""), and the .env.default wording that must introduce each.
RGW_REQUIRED = ("rgw_admin_url", "rgw_admin_access_key", "rgw_admin_secret_key", "s3_endpoint_url")
CROIT_REQUIRED = ("croit_url", "croit_api_token")
MARKER = {"rgw": "REQUIRED (with the default ADMIN_BACKEND=rgw)",
          "croit": "REQUIRED only when ADMIN_BACKEND=croit"}
# Always required, whatever the backend (REQUIRED minus the explicit test backend).
COMMON = {k: v for k, v in REQUIRED.items() if k != "admin_backend" and not k.startswith("croit")}
RGW_VALUES = {"rgw_admin_url": "https://rgw.example.org", "rgw_admin_access_key": "ADMINKEY",
              "rgw_admin_secret_key": "admin-secret", "s3_endpoint_url": "https://s3.example.org"}
CROIT_VALUES = {k: v for k, v in REQUIRED.items() if k.startswith("croit")}


def test_default_backend_is_rgw():
    assert Settings.model_fields["admin_backend"].default == "rgw"
    assert dotenv_values(ENV_DEFAULT)["ADMIN_BACKEND"] == "rgw"


def test_env_default_values_match_code_defaults(monkeypatch):
    # The default backend is rgw, so its REQUIRED values complete the file; ADMIN_BACKEND
    # itself is left to the default so that comparing file and code covers it too.
    for name, value in {**COMMON, **RGW_VALUES}.items():
        monkeypatch.setenv(name.upper(), value)
    from_file = Settings(_env_file=ENV_DEFAULT)
    from_code = Settings(_env_file=None)
    assert from_file.model_dump() == from_code.model_dump()
    assert from_code.admin_backend == "rgw"


def test_env_default_values_match_code_defaults_croit(monkeypatch):
    for name, value in {**COMMON, **CROIT_VALUES, "admin_backend": "croit"}.items():
        monkeypatch.setenv(name.upper(), value)
    from_file = Settings(_env_file=ENV_DEFAULT)
    from_code = Settings(_env_file=None)
    assert from_file.model_dump() == from_code.model_dump()
    assert from_code.admin_backend == "croit"


def test_required_are_exactly_the_empty_keys_without_empty_default():
    required = {n for n, f in Settings.model_fields.items() if f.is_required()}
    conditional = set(RGW_REQUIRED) | set(CROIT_REQUIRED)
    # every always-required setting plus the backend-conditional ones: nothing else
    assert required | conditional == (set(REQUIRED) - {"admin_backend"}) | set(RGW_REQUIRED)
    values = dotenv_values(ENV_DEFAULT)
    for name in required | conditional:
        assert values[name.upper()] == ""
    # ... and each conditional one is introduced by the comment that names its backend
    text = ENV_DEFAULT.read_text()
    for backend, names in (("rgw", RGW_REQUIRED), ("croit", CROIT_REQUIRED)):
        for name in names:
            block = text[: text.index(f"\n{name.upper()}=")].rsplit("\n\n", 1)[-1]
            assert MARKER[backend] in block, name
            other = MARKER["croit" if backend == "rgw" else "rgw"]
            assert other not in block, name


def write_env(path, extra=""):
    lines = [f"{k.upper()}={v}" for k, v in {**REQUIRED, "admin_backend": "croit"}.items()]
    path.write_text("\n".join(lines) + "\n" + extra)


def test_missing_required_lists_all(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here, ADMIN_BACKEND unset: the default rgw applies
    with pytest.raises(SystemExit) as exc:
        load_settings()
    msg = str(exc.value)
    for name in (*COMMON, *RGW_REQUIRED):
        assert name.upper() in msg
    for name in CROIT_REQUIRED:
        assert name.upper() not in msg
    assert "default rgw" in msg and "ADMIN_BACKEND=croit" in msg  # says how to pick croit


def test_missing_croit_settings_named_only_with_croit_backend(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_BACKEND", "croit")
    with pytest.raises(SystemExit) as exc:
        load_settings()
    msg = str(exc.value)
    for name in (*COMMON, *CROIT_REQUIRED):
        assert name.upper() in msg
    for name in RGW_REQUIRED:
        assert name.upper() not in msg


def test_croit_values_do_not_satisfy_the_default_backend(monkeypatch, tmp_path):
    """appmotel copies .env.default; an operator who only fills in the croit settings must
    get a refusal naming the rgw ones, never a silent croit start."""
    for k, v in {**COMMON, **CROIT_VALUES}.items():
        monkeypatch.setenv(k.upper(), v)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        load_settings()
    for name in RGW_REQUIRED:
        assert name.upper() in str(exc.value)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **COMMON, **CROIT_VALUES)


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


def test_missing_ca_file_stops_startup_with_a_clear_message(monkeypatch, tmp_path):
    for k, v in {**REQUIRED, "ldap_ca_cert_file": str(tmp_path / "nope.pem")}.items():
        monkeypatch.setenv(k.upper(), v)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="LDAP_CA_CERT_FILE.*THIS server"):
        load_settings()
    pem = "-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n"
    (tmp_path / "ca.pem").write_text(pem)
    monkeypatch.setenv("LDAP_CA_CERT_FILE", str(tmp_path / "ca.pem"))
    assert load_settings().ldap_ca_cert_file.endswith("ca.pem")
