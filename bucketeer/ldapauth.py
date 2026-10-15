"""Active Directory login over LDAPS (CLAUDE.md "Authentication").

`AdAuthenticator.authenticate(login, password)` runs the six-step flow:
service bind, search with the escaped `{login}`, exactly one match, group
check (nested, via LDAP_MATCHING_RULE_IN_CHAIN), user bind on a fresh
connection, identity from LDAP_USERNAME_ATTRIBUTE. ldap3 is synchronous, so the
whole flow runs in a worker thread.

Failure model: `InvalidCredentials` (wrong password, unknown user, ambiguous
match, not in group, empty input: callers must show one uniform message and may
log `.reason`) versus `LdapUnavailable` (no domain controller usable).

The LDAP layer is injectable: a *connector* is `connector(url, bind_dn, password)
-> LdapSession` and raises `BindRejected` or `LdapUnavailable`. Tests pass a fake;
production uses `Ldap3Connector`.
"""

import asyncio
import logging
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import ldap3
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPException,
    LDAPInvalidCredentialsResult,
    LDAPPasswordIsMandatoryError,
    LDAPSASLPrepError,
    LDAPSizeLimitExceededResult,
)
from ldap3.protocol.sasl.sasl import validate_simple_password
from ldap3.utils.conv import escape_filter_chars

from .config import Settings

log = logging.getLogger(__name__)

# AD "LDAP_MATCHING_RULE_IN_CHAIN": transitive (nested) group membership.
MATCHING_RULE_IN_CHAIN = "1.2.840.113556.1.4.1941"


class InvalidCredentials(Exception):
    """Login refused. `reason` is for server-side logs only, never for users."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LdapUnavailable(Exception):
    """No domain controller could serve the request."""


class BindRejected(Exception):
    """The server refused the bind (wrong DN or password)."""


class SizeLimitExceeded(Exception):
    """The search matched more entries than the size limit asked for."""


@dataclass(frozen=True)
class LdapEntry:
    dn: str
    attrs: dict[str, str]  # requested attributes, first value each, "" if absent


class LdapSession(Protocol):
    def search(
        self,
        base: str,
        flt: str,
        attributes: Sequence[str],
        *,
        scope: str = "subtree",
        size_limit: int = 0,
    ) -> list[LdapEntry]: ...

    def close(self) -> None: ...


Connector = Callable[[str, str, str], LdapSession]


@dataclass(frozen=True)
class AuthUser:
    username: str
    display_name: str
    email: str
    is_admin: bool


@dataclass(frozen=True)
class Resolved:
    """A directory account that passed search and group checks; password not yet verified."""

    dn: str
    url: str  # the domain controller that answered; the user bind goes there too
    user: AuthUser


# -- real ldap3 backend -------------------------------------------------------


def build_server(settings: Settings, url: str) -> ldap3.Server:
    """LDAPS server object with certificate verification per the settings."""
    tls = ldap3.Tls(
        validate=ssl.CERT_REQUIRED if settings.ldap_tls_verify else ssl.CERT_NONE,
        ca_certs_file=settings.ldap_ca_cert_file or None,
    )
    return ldap3.Server(
        url,
        use_ssl=True,
        tls=tls,
        connect_timeout=settings.ldap_timeout,
        get_info=ldap3.NONE,
    )


class _Ldap3Session:
    def __init__(self, conn: ldap3.Connection):
        self._conn = conn

    def search(self, base, flt, attributes, *, scope="subtree", size_limit=0):
        try:
            self._conn.search(
                base,
                flt,
                search_scope=ldap3.BASE if scope == "base" else ldap3.SUBTREE,
                attributes=list(attributes),
                size_limit=size_limit,
            )
        except LDAPSizeLimitExceededResult:
            raise SizeLimitExceeded from None
        except LDAPException as e:
            raise LdapUnavailable(f"search failed: {type(e).__name__}") from e
        out = []
        for r in self._conn.response or []:
            if r.get("type") != "searchResEntry":  # skip referrals
                continue
            lower = {k.lower(): v for k, v in (r.get("attributes") or {}).items()}
            attrs = {}
            for a in attributes:
                v = lower.get(a.lower())
                if isinstance(v, list | tuple):
                    v = v[0] if v else ""
                attrs[a] = "" if v is None else str(v)
            out.append(LdapEntry(dn=r["dn"], attrs=attrs))
        return out

    def close(self) -> None:
        try:
            self._conn.unbind()
        except Exception:  # noqa: BLE001  # best-effort close
            pass


class Ldap3Connector:
    """Opens an LDAPS connection and does a simple bind (a new one per call)."""

    def __init__(
        self, settings: Settings, *, client_strategy=ldap3.SYNC, server_factory=build_server
    ):
        # client_strategy / server_factory exist so tests can run ldap3 itself
        # (MOCK_SYNC) without a network.
        self.settings = settings
        self.client_strategy = client_strategy
        self.server_factory = server_factory

    def __call__(self, url: str, bind_dn: str, password: str) -> LdapSession:
        s = self.settings
        conn = ldap3.Connection(
            self.server_factory(s, url),
            client_strategy=self.client_strategy,
            user=bind_dn,
            password=password,
            authentication=ldap3.SIMPLE,
            raise_exceptions=True,
            receive_timeout=s.ldap_timeout,
            auto_referrals=False,
            read_only=True,
        )
        try:
            conn.bind()
        except (
            LDAPInvalidCredentialsResult,
            LDAPBindError,
            LDAPSASLPrepError,  # ldap3 rejects the password locally: still a bad password
            LDAPPasswordIsMandatoryError,
        ) as e:
            _quiet_close(conn)
            raise BindRejected from e
        except (LDAPException, OSError) as e:  # socket, TLS, timeout, protocol
            _quiet_close(conn)
            raise LdapUnavailable(f"{type(e).__name__}") from e
        return _Ldap3Session(conn)


def _quiet_close(conn: ldap3.Connection) -> None:
    try:
        conn.unbind()
    except Exception:  # noqa: BLE001
        pass


# -- the authenticator --------------------------------------------------------


class AdAuthenticator:
    def __init__(self, settings: Settings, connector: Connector | None = None):
        for u in settings.ldap_url:  # also enforced by Settings; belt and braces
            if not u.lower().startswith("ldaps://"):
                raise ValueError(f"LDAP_URL must be ldaps://, got {u.split('://')[0]}://")
        self.s = settings
        self.connector: Connector = connector or Ldap3Connector(settings)

    async def authenticate(self, login: str, password: str) -> AuthUser:
        """resolve() then verify(). Raises InvalidCredentials or LdapUnavailable."""
        return await self.verify(await self.resolve(login, password), password)

    async def lookup_user(self, login: str) -> AuthUser:
        """Find a person who could log in (same filter, exactly one match, required
        group, nested) WITHOUT a password bind: for sharing with a colleague.
        Raises InvalidCredentials (`.reason` for logs only) or LdapUnavailable."""
        return await asyncio.to_thread(self._lookup, login)

    async def account_statuses(self, usernames: Sequence[str]) -> dict[str, str]:
        """For the offboarding job: the state of each account, found by the username
        attribute (exact, escaped; NOT the login filter): `ok`, `disabled`
        (userAccountControl bit 2), `not_in_group` (nested required group, when set) or
        `not_found`. One service bind, no user password. ANY LDAP trouble (including an
        ambiguous or attribute-less entry) raises `LdapUnavailable` for the whole call."""
        return await asyncio.to_thread(self._account_statuses, list(usernames))

    async def account_status(self, username: str) -> str:
        return (await self.account_statuses([username]))[username]

    async def resolve(self, login: str, password: str) -> Resolved:
        """Service bind, search, exactly-one check, group checks. No user bind.
        `password` is only pre-validated here."""
        return await asyncio.to_thread(self._resolve, login, password)

    async def verify(self, resolved: Resolved, password: str) -> AuthUser:
        """Bind as the resolved DN with the password."""
        await asyncio.to_thread(self._verify_password, resolved.url, resolved.dn, password)
        return resolved.user

    # Everything below is synchronous and runs in a worker thread.

    def _resolve(self, login: str, password: str) -> Resolved:
        s = self.s
        login = login.strip()
        if not login:
            raise InvalidCredentials("empty_login")
        # AD treats an empty password as an unauthenticated bind, which
        # *succeeds*. Refuse before any connection is made.
        if not password or not password.strip():
            raise InvalidCredentials("empty_password")
        # ldap3 SASLpreps the password inside bind() and raises for some inputs
        # (control characters, soft hyphen...). Check it now, before any
        # connection, so that failure is an ordinary bad password and can never
        # be told apart (503 vs 401) from the outside.
        try:
            validate_simple_password(password)
        except (LDAPSASLPrepError, LDAPPasswordIsMandatoryError):
            raise InvalidCredentials("password_not_encodable") from None

        svc, url = self._service_session()
        try:
            entry, username = self._find_entry(svc, login)
            # Only handed out after verify() succeeds.
            is_admin = bool(s.ldap_admin_group_dn) and self._is_member(
                svc, entry.dn, s.ldap_admin_group_dn
            )
            user = AuthUser(
                username=username,
                display_name=entry.attrs.get(s.ldap_display_name_attribute, "").strip(),
                email=entry.attrs.get(s.ldap_email_attribute, "").strip(),
                is_admin=is_admin,
            )
            return Resolved(entry.dn, url, user)
        finally:
            svc.close()

    def _find_entry(self, svc: LdapSession, login: str) -> tuple[LdapEntry, str]:
        """Escaped search, exactly one match, username attribute, required-group check
        (nested). Shared by login (`_resolve`) and recipient lookup (`_lookup`)."""
        s = self.s
        flt = s.ldap_user_filter.replace("{login}", escape_filter_chars(login))
        uattr, dattr, eattr = (
            s.ldap_username_attribute,
            s.ldap_display_name_attribute,
            s.ldap_email_attribute,
        )
        try:
            entries = svc.search(s.ldap_base_dn, flt, [uattr, dattr, eattr], size_limit=2)
        except SizeLimitExceeded:
            raise InvalidCredentials("ambiguous_match") from None
        if not entries:
            raise InvalidCredentials("no_such_user")
        if len(entries) > 1:
            raise InvalidCredentials("ambiguous_match")
        entry = entries[0]
        username = entry.attrs.get(uattr, "").strip()
        if not username:
            log.error("LDAP entry %s has no %s attribute", entry.dn, uattr)
            raise InvalidCredentials("no_username_attribute")
        if s.ldap_required_group_dn and not self._is_member(
            svc, entry.dn, s.ldap_required_group_dn
        ):
            raise InvalidCredentials("not_in_required_group")
        return entry, username

    def _lookup(self, login: str) -> AuthUser:
        login = login.strip()
        if not login:
            raise InvalidCredentials("empty_login")
        svc, _ = self._service_session()
        try:
            entry, username = self._find_entry(svc, login)
        finally:
            svc.close()
        s = self.s
        return AuthUser(
            username=username,
            display_name=entry.attrs.get(s.ldap_display_name_attribute, "").strip(),
            email=entry.attrs.get(s.ldap_email_attribute, "").strip(),
            is_admin=False,  # not looked at: lookups only identify people
        )

    def _account_statuses(self, usernames: list[str]) -> dict[str, str]:
        s = self.s
        uattr = s.ldap_username_attribute
        out: dict[str, str] = {}
        svc, _ = self._service_session()
        try:
            for name in usernames:
                flt = f"({uattr}={escape_filter_chars(name)})"
                try:
                    entries = svc.search(s.ldap_base_dn, flt, [uattr, "userAccountControl"],
                                         size_limit=2)
                except SizeLimitExceeded:
                    raise LdapUnavailable("ambiguous account") from None
                if len(entries) > 1:
                    raise LdapUnavailable("ambiguous account")
                if not entries:
                    out[name] = "not_found"
                    continue
                entry = entries[0]
                if entry.attrs.get(uattr, "").strip().lower() != name.strip().lower():
                    raise LdapUnavailable("unexpected directory entry")
                try:
                    uac = int(entry.attrs.get("userAccountControl", "").strip())
                except ValueError:
                    raise LdapUnavailable("account has no userAccountControl") from None
                if uac & 2:
                    out[name] = "disabled"
                elif s.ldap_required_group_dn and not self._is_member(
                    svc, entry.dn, s.ldap_required_group_dn
                ):
                    out[name] = "not_in_group"
                else:
                    out[name] = "ok"
        finally:
            svc.close()
        return out

    def _service_session(self) -> tuple[LdapSession, str]:
        """First domain controller that accepts the service bind."""
        s = self.s
        password = s.ldap_bind_password.get_secret_value()
        for url in s.ldap_url:
            try:
                return self.connector(url, s.ldap_bind_dn, password), url
            except LdapUnavailable as e:
                log.warning("LDAP server %s unavailable: %s", url, e)
            except BindRejected:
                # A configuration problem (or a locked service account). Do NOT
                # try the other servers: each attempt is another bad bind and
                # would lock the service account. Fail over on connection errors only.
                log.error("LDAP service bind to %s was rejected; check LDAP_BIND_DN/PASSWORD", url)
                raise LdapUnavailable("service bind rejected") from None
        raise LdapUnavailable("no LDAP server usable")

    def _is_member(self, svc: LdapSession, user_dn: str, group_dn: str) -> bool:
        """Nested membership: base-scope search on the user with the chain rule."""
        flt = f"(memberOf:{MATCHING_RULE_IN_CHAIN}:={escape_filter_chars(group_dn)})"
        return bool(svc.search(user_dn, flt, ["distinguishedName"], scope="base", size_limit=1))

    def _verify_password(self, url: str, user_dn: str, password: str) -> None:
        try:
            session = self.connector(url, user_dn, password)  # fresh connection
        except BindRejected:
            raise InvalidCredentials("bad_password") from None
        session.close()
