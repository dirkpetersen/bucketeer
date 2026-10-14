"""Runtime configuration, read from the environment (and `.env`).

Every setting in `.env.default` has a field here. A field without a default is
REQUIRED: the app refuses to start while it is unset or empty. Field defaults
must equal the values in `.env.default` (tests/test_config.py enforces this).
"""

from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOG_LEVELS = ("debug", "info", "warning", "error")


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
    ldap_tls_verify: bool = True
    ldap_timeout: int = Field(5, gt=0)

    # croit API
    croit_url: str = Field(min_length=1)
    croit_api_token: SecretStr = Field(min_length=1)
    croit_ca_cert_file: str = ""
    croit_tls_verify: bool = True
    croit_timeout: int = Field(15, gt=0)

    # S3
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    aws_profile_name: str = "bucketeer"
    s3_tenant: str = ""
    s3_ca_cert_file: str = ""
    s3_tls_verify: bool = True

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


def load_settings() -> Settings:
    """Load ./.env (cwd only, never searched upward; never overriding the environment).

    Exits with one message listing every missing or invalid setting.
    """
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        return Settings()
    except ValidationError as e:
        lines = []
        for err in e.errors():
            name = str(err["loc"][0]).upper() if err["loc"] else "?"
            missing = err["type"] in ("missing", "string_too_short", "too_short")
            lines.append(f"  {name}: {'REQUIRED, not set' if missing else err['msg']}")
        raise SystemExit(
            "Invalid configuration (see .env.default):\n" + "\n".join(sorted(lines))
        ) from None
