"""Runtime configuration, read from the environment (and `.env`).

Every setting in `.env.default` has a field here. A field without a default is
REQUIRED: the app refuses to start while it is unset or empty. Field defaults
must equal the values in `.env.default` (tests/test_config.py enforces this).
"""

import ipaddress
import os
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOG_LEVELS = ("debug", "info", "warning", "error")

# Values that end up in the generated ~/.aws files. Anything outside these
# alphabets is refused at startup so a setting can never add INI lines/keys.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
REGION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
ENDPOINT_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?\Z"
)


RGW_KEY_RE = re.compile(r"^[A-Za-z0-9._+-]{1,128}\Z")


def _check_rgw_admin_url(v: str) -> str:
    """https://host[:port][/prefix]; plain http only for a loopback host (the secret key
    never signs a request that travels in the clear). No user info, query or fragment."""
    v = v.strip().rstrip("/")
    if not ENDPOINT_URL_RE.match(v):
        raise ValueError("RGW_ADMIN_URL: must look like https://host[:port] (no spaces or quotes)")
    u = urlsplit(v)
    if u.scheme == "http":
        host = (u.hostname or "").lower()
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValueError("RGW_ADMIN_URL: plain http is only allowed for a loopback host")
    return v


class Settings(BaseSettings):
    # The environment is the source of truth; `.env` is merged in by
    # load_settings() without override. Tests pass explicit values instead.
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=False)

    # Server
    port: int = Field(8000, ge=1, le=65535)
    host: str = "127.0.0.1"
    app_name: str = "Bucketeer"
    health_check_path: str = "/health"
    log_level: str = "info"
    data_dir: str = "data"

    # Sessions
    session_secret: SecretStr = Field(min_length=1)
    session_max_age: int = Field(3600, gt=0)
    session_cookie_secure: bool = True

    # Active Directory login
    ldap_url: Annotated[list[str], NoDecode] = Field(min_length=1)
    ldap_base_dn: str = Field(min_length=1)
    ldap_bind_dn: str = Field(min_length=1)
    ldap_bind_password: SecretStr = Field(min_length=1)
    ldap_user_filter: str = (
        "(&(objectCategory=person)(objectClass=user)"
        "(!(userAccountControl:1.2.840.113556.1.4.803:=2))"
        "(|(sAMAccountName={login})(mail={login})(userPrincipalName={login})))"
    )
    ldap_username_attribute: str = "sAMAccountName"
    ldap_display_name_attribute: str = "displayName"
    ldap_email_attribute: str = "mail"
    ldap_required_group_dn: str = ""
    ldap_admin_group_dn: str = ""
    ldap_ca_cert_file: str = ""
    ldap_ca_sha256: str = ""
    ldap_tls_verify: bool = True
    ldap_timeout: int = Field(5, gt=0)

    # Admin backend: `rgw` (RGW Admin Ops, the default since Phase 5 A7) or `croit` (croit
    # API, kept until the cluster check). The settings of the other backend are ignored.
    # Required-ness is checked in `_backend_settings`; there is never a fallback between them.
    admin_backend: Literal["croit", "rgw"] = "rgw"

    # croit API (REQUIRED only when admin_backend=croit)
    croit_url: str = ""
    croit_api_token: SecretStr = SecretStr("")
    croit_ca_cert_file: str = ""
    croit_tls_verify: bool = True
    croit_timeout: int = Field(15, gt=0)

    # RGW Admin Ops (REQUIRED with the default admin_backend=rgw)
    rgw_admin_url: str = ""
    rgw_admin_access_key: str = ""
    rgw_admin_secret_key: SecretStr = SecretStr("")
    rgw_admin_timeout: int = Field(15, gt=0)

    # Email (optional): share invitations. Empty smtp_host = no email is sent.
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    app_url: str = ""

    # S3
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_addressing_style: Literal["path", "virtual", "auto"] = "path"
    aws_profile_name: str = "bucketeer"
    s3_tenant: str = ""
    s3_ca_cert_file: str = ""
    s3_tls_verify: bool = True

    @field_validator("aws_profile_name")
    @classmethod
    def _check_profile(cls, v: str) -> str:
        if not PROFILE_NAME_RE.match(v):
            raise ValueError(
                "must be 1-63 letters, digits, '.', '_' or '-' (no spaces, brackets or newlines)"
            )
        return v

    @field_validator("s3_region")
    @classmethod
    def _check_region(cls, v: str) -> str:
        if not REGION_RE.match(v):
            raise ValueError("must be letters, digits, '-' or '_' only, e.g. us-east-1")
        return v

    @field_validator("s3_endpoint_url")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not ENDPOINT_URL_RE.match(v):
            raise ValueError("must look like https://host[:port] (no spaces, quotes or newlines)")
        return v

    @model_validator(mode="after")
    def _mail_settings(self):
        if self.smtp_from:  # SMTP_FROM turns email on; SMTP_HOST alone changes nothing
            if not self.app_url:
                raise ValueError("SMTP_FROM is set (email is on), so also set: APP_URL")
            if not re.fullmatch(r"[^\s@<>\"]+@[^\s@<>\"]+", self.smtp_from):
                raise ValueError("SMTP_FROM must be a plain email address")
            if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?(/\S*)?", self.app_url):
                raise ValueError("APP_URL must be an http(s) address")
        return self

    @model_validator(mode="after")
    def _backend_settings(self):
        problems = []
        if self.admin_backend == "croit":
            if not self.croit_url:
                problems.append("CROIT_URL")
            if not self.croit_api_token.get_secret_value():
                problems.append("CROIT_API_TOKEN")
            if problems:
                raise ValueError(
                    f"{', '.join(problems)}: REQUIRED when ADMIN_BACKEND=croit, not set"
                )
        else:
            if not self.rgw_admin_url:
                problems.append("RGW_ADMIN_URL")
            if not self.rgw_admin_access_key:
                problems.append("RGW_ADMIN_ACCESS_KEY")
            if not self.rgw_admin_secret_key.get_secret_value():
                problems.append("RGW_ADMIN_SECRET_KEY")
            if not self.s3_endpoint_url:  # no croit .s3cfg to discover it from
                problems.append("S3_ENDPOINT_URL")
            if problems:
                raise ValueError(
                    f"{', '.join(problems)}: REQUIRED when ADMIN_BACKEND=rgw, not set"
                )
            self.rgw_admin_url = _check_rgw_admin_url(self.rgw_admin_url)
            if not RGW_KEY_RE.match(self.rgw_admin_access_key):
                raise ValueError(
                    "RGW_ADMIN_ACCESS_KEY: must be letters, digits, '.', '_', '-' or '+' only"
                )
        return self

    @field_validator("ldap_url", mode="before")
    @classmethod
    def _split_urls(cls, v):
        if isinstance(v, str):
            return [u.strip() for u in v.split(",") if u.strip()]
        return v

    @field_validator("ldap_url")
    @classmethod
    def _ldaps_only(cls, v: list[str]) -> list[str]:
        # Passwords travel over this connection: plain ldap:// is never allowed.
        for u in v:
            if not u.lower().startswith("ldaps://") or len(u) <= len("ldaps://"):
                raise ValueError("every URL must be ldaps://host[:port] (plain ldap:// is refused)")
        return v

    @field_validator("ldap_user_filter")
    @classmethod
    def _filter_has_login(cls, v: str) -> str:
        if "{login}" not in v:
            raise ValueError("must contain the {login} placeholder")
        return v

    @field_validator("ldap_ca_sha256")
    @classmethod
    def _check_pin(cls, v: str) -> str:
        v = v.replace(":", "").replace(" ", "").strip()
        if v and not re.fullmatch(r"[0-9A-Fa-f]{64}", v):
            raise ValueError("must be a SHA-256 fingerprint: 64 hex digits (colons allowed)")
        return v.upper()

    @field_validator("log_level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}")
        return v

    @field_validator("health_check_path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("must start with /")
        return v


def _backend_problems() -> list[str]:
    """The missing settings of the chosen admin backend, read from the environment, so one
    startup message lists them together with any other missing setting."""
    env = os.environ
    backend = (env.get("ADMIN_BACKEND") or "rgw").strip().lower()
    names = {"croit": ("CROIT_URL", "CROIT_API_TOKEN"),
             "rgw": ("RGW_ADMIN_URL", "RGW_ADMIN_ACCESS_KEY", "RGW_ADMIN_SECRET_KEY",
                   "S3_ENDPOINT_URL")}.get(backend)
    return [f"  {n}: REQUIRED when ADMIN_BACKEND={backend}, not set"
            for n in names or () if not env.get(n)]


def _ca_file_problems(s: Settings) -> list[str]:
    """CA bundle settings that point at nothing usable. Without this the first login or
    request fails with a stack trace instead of a clear message at startup."""
    out = []
    for name in ("ldap_ca_cert_file", "croit_ca_cert_file", "s3_ca_cert_file"):
        path = getattr(s, name)
        if not path:
            continue
        try:
            ok = "BEGIN CERTIFICATE" in Path(path).read_text(errors="replace")
        except OSError:
            ok = False
        if not ok:
            out.append(f"  {name.upper()}: {path} is missing, unreadable or not a PEM "
                       "certificate file (the path must exist on THIS server)")
    return out


def load_settings() -> Settings:
    """Load ./.env (cwd only, never searched upward; never overriding the environment).

    Exits with one message listing every missing or invalid setting.
    """
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        settings = Settings()
        from .ldapca import ensure_ca_file

        ensure_ca_file(settings)  # fetches a missing LDAP CA file (logged, see ldapca.py)
        if problems := _ca_file_problems(settings):
            raise SystemExit("Invalid configuration (see .env.default):\n" + "\n".join(problems))
        return settings
    except ValidationError as e:
        lines = _backend_problems()  # pydantic skips the cross-field check after field errors
        listed = bool(lines)
        for err in e.errors():
            if not err["loc"]:  # the cross-field check; _backend_problems() listed these
                if not listed:
                    lines.append("  " + err["msg"].removeprefix("Value error, "))
                continue
            name = str(err["loc"][0]).upper()
            missing = err["type"] in ("missing", "string_too_short", "too_short")
            lines.append(f"  {name}: {'REQUIRED, not set' if missing else err['msg']}")
        msg = "Invalid configuration (see .env.default):\n" + "\n".join(sorted(lines))
        if listed:
            backend = (os.environ.get("ADMIN_BACKEND") or "rgw").strip().lower()
            how = ("ADMIN_BACKEND is not set, so the default rgw (RGW Admin Ops) applies"
                   if not os.environ.get("ADMIN_BACKEND") else f"ADMIN_BACKEND={backend}")
            msg += (f"\n{how}. Set the variables above; to use the croit API instead, "
                    "set ADMIN_BACKEND=croit explicitly (there is no automatic fallback).")
        raise SystemExit(msg) from None
