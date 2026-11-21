"""Fetch the CA certificate(s) of the LDAP servers into LDAP_CA_CERT_FILE.

First contact is trust on first use: the certificates come from the domain
controller itself, so they prove nothing unless LDAP_CA_SHA256 pins them or you
compare the printed fingerprints with the directory team. The fetch sends no
password and does no LDAP request. It keeps only the CA certificates of the
chain (never the server's own certificate), and keeps them only if the server's
certificate then verifies against them for its host name.

Run by hand: `python3 app.py --fetch-ldap-ca [--force]`. Run by itself at startup
(`ensure_ca_file`) when LDAP_CA_CERT_FILE is set and the file does not exist, or when
the servers in LDAP_URL changed since the file was fetched (a record `<file>.hosts`
keeps a hash of the host list). A fetch that fails leaves the old file in place.
"""

import hashlib
import logging
import os
import re
import socket
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("bucketeer.ldapca")


class FetchError(Exception):
    pass


def fingerprint(der: bytes) -> str:
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def normalize_pin(value: str) -> str:
    return value.replace(":", "").replace(" ", "").strip().upper()


def hosts_hash(urls: list[str]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(f"{h.lower()}:{p}" for h, p in map(_host_port, urls))).encode()
    ).hexdigest()


def _record(path: str) -> Path:
    return Path(path + ".hosts")


def _file_prints(path: str) -> list[str]:
    try:
        text = Path(path).read_text()
    except OSError:
        return []
    return [fingerprint(ssl.PEM_cert_to_DER_cert(m.group(0))) for m in re.finditer(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)]


def _host_port(url: str) -> tuple[str, int]:
    u = urlsplit(url)
    if not u.hostname:
        raise FetchError(f"{url}: no host name")
    return u.hostname, u.port or 636


def _server_chain(host: str, port: int, timeout: float) -> list[bytes]:
    """The chain the server presents, DER, its own certificate first. No verification,
    and nothing but the TLS handshake is sent."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            get = getattr(tls, "get_unverified_chain", None) or tls._sslobj.get_unverified_chain
            certs = get()
    return [c if isinstance(c, bytes) else ssl.PEM_cert_to_DER_cert(c.public_bytes())
            for c in certs]


def _verifies(path: str, host: str, port: int, timeout: float) -> str | None:
    """None when the server's certificate verifies against the file, else the reason."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # same trust as ldapauth uses
    ctx.load_verify_locations(cafile=path)
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return None
    except ssl.SSLCertVerificationError as e:
        return e.verify_message or str(e)
    except (OSError, ssl.SSLError) as e:
        return type(e).__name__


def pin_set(value: str) -> set[str]:
    """LDAP_CA_SHA256 is a comma-separated list of fingerprints."""
    return {normalize_pin(p) for p in (value or "").split(",") if p.strip()}


def directory_lookup(settings):
    """Read every CA certificate AD publishes (`cACertificate` of the certificationAuthority
    objects under Public Key Services, where the root lives) from one server. Used only
    to find a pinned CA the servers do not send; the caller accepts only a pinned one and
    then checks the chain against it. The service bind runs over TLS checked against the
    CA certificates that server sent (the trust a first fetch has anyway)."""
    def look(host: str, port: int, cafile: str) -> list[bytes]:
        import ldap3

        from . import ldapauth  # noqa: F401  intermediate CAs count as anchors

        server = ldap3.Server(host, port=port, use_ssl=True, get_info=ldap3.NONE,
                              connect_timeout=settings.ldap_timeout,
                              tls=ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=cafile))
        conn = ldap3.Connection(server, user=settings.ldap_bind_dn,
                                password=settings.ldap_bind_password.get_secret_value(),
                                authentication=ldap3.SIMPLE, raise_exceptions=True,
                                receive_timeout=settings.ldap_timeout, read_only=True,
                                auto_referrals=False)
        try:
            conn.bind()
            conn.search("", "(objectClass=*)", search_scope=ldap3.BASE,
                        attributes=["configurationNamingContext"])
            cfg = conn.response[0]["attributes"]["configurationNamingContext"]
            cfg = cfg[0] if isinstance(cfg, list) else cfg
            conn.search(f"CN=Public Key Services,CN=Services,{cfg}",
                        "(objectClass=certificationAuthority)", search_scope=ldap3.SUBTREE,
                        attributes=["cACertificate"])
            return [v for r in conn.response if r.get("type") == "searchResEntry"
                    for v in r["raw_attributes"].get("cACertificate", [])]
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass
    return look


def fetch_ca_file(urls: list[str], path: str, *, pin: str = "", force: bool = False,
                  timeout: float = 5.0, directory=None) -> list[str]:
    """Ask every server in `urls` for its CA certificates, keep the pinned ones (all of
    them without a pin), write them to `path` once every kept server verifies against
    the file, and return their SHA-256 fingerprints. A server that is down, sends no
    pinned CA or fails the check is skipped and logged; at least one must pass.
    When a server sends no pinned CA (usually: the pin is the root, which servers never
    send) and `directory(host, port, cafile)` is given, the pinned CA is looked up there.
    Refuses to replace an existing file without `force`."""
    target = Path(path)
    if target.exists() and not force:
        raise FetchError(f"{path} exists; use --force to replace it")
    wanted = pin_set(pin)
    errors, found, servers = [], {}, []
    for url in urls:
        host, port = _host_port(url)
        try:
            cas = _server_chain(host, port, timeout)[1:]
        except (OSError, ssl.SSLError) as e:
            errors.append(f"{host}: {type(e).__name__}")
            continue
        if not cas:
            errors.append(f"{host}: it sent no CA certificate (only its own)")
            continue
        keep = [c for c in cas if not wanted or normalize_pin(fingerprint(c)) in wanted]
        if not keep and directory:
            keep = _pinned_from_directory(directory, host, port, cas, wanted, errors)
        if not keep:
            errors.append(f"{host}: no CA certificate matches LDAP_CA_SHA256; it sent: "
                          + ", ".join(fingerprint(c) for c in cas))
            continue
        for c in keep:
            found.setdefault(fingerprint(c), c)
        servers.append((host, port))
    if not servers:
        raise FetchError("; ".join(errors) or "no LDAP_URL")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".ldap-ca-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("".join(ssl.DER_cert_to_PEM_cert(c) for c in found.values()))
        os.chmod(tmp, 0o644)  # a public certificate, no secret
        ok = 0
        for host, port in servers:
            bad = _verifies(tmp, host, port, timeout)
            if bad:
                errors.append(f"{host}: its certificate does not verify against the CA "
                              f"certificates it sent ({bad})")
            else:
                ok += 1
        if not ok:
            raise FetchError("; ".join(errors))
        os.replace(tmp, target)
        try:
            _record(path).write_text(hosts_hash(urls))
        except OSError:
            log.warning("could not write %s", _record(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if errors:
        log.warning("LDAP CA fetch skipped: %s", "; ".join(errors))
    return list(found)


def _pinned_from_directory(directory, host, port, cas, wanted, errors) -> list[bytes]:
    fd, tmp = tempfile.mkstemp(prefix=".ldap-ca-sent-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("".join(ssl.DER_cert_to_PEM_cert(c) for c in cas))
        found = directory(host, port, tmp)
    except Exception as e:  # noqa: BLE001  LDAP, TLS or network trouble: report, go on
        errors.append(f"{host}: directory lookup failed ({type(e).__name__})")
        return []
    finally:
        os.unlink(tmp)
    keep = [c for c in found if normalize_pin(fingerprint(c)) in wanted]
    if keep:
        log.warning("LDAP CA: %s sent no pinned CA; took the pinned CA from the directory",
                    host)
    return keep


def ensure_ca_file(settings) -> None:
    """Startup: fetch the CA file when it is missing or LDAP_URL changed since it was
    fetched. Logs the fingerprints. A failure only logs (an old file stays in place);
    the normal startup check then stops the app if the file is still missing."""
    path = settings.ldap_ca_cert_file
    if not path or not settings.ldap_tls_verify:  # no verification: no CA file needed
        return
    urls, pin = settings.ldap_url, settings.ldap_ca_sha256
    exists, rec = Path(path).exists(), _record(path)
    if exists:
        try:
            old = rec.read_text().strip()
        except OSError:
            old = ""
        if not old:  # a file supplied by hand: adopt it, fetch only on a later change
            try:
                rec.write_text(hosts_hash(urls))
            except OSError:
                pass
            return
        if old == hosts_hash(urls):
            return
        why, before = "LDAP_URL changed", _file_prints(path)
    else:
        why, before = "it was missing", []
    try:
        prints = fetch_ca_file(urls, path, pin=pin, force=True, timeout=settings.ldap_timeout,
                               directory=_directory_for(settings))
    except FetchError as e:
        log.error("LDAP_CA_CERT_FILE %s: %s, but the fetch failed (%s)%s", path, why, e,
                  "; the old file stays" if exists else "")
        return
    log.warning("LDAP_CA_CERT_FILE %s: %s, so it was fetched from the directory server. %s "
                "Before: %s. Now: %s.", path, why,
                "It matches LDAP_CA_SHA256." if pin else
                "NOT pinned: set LDAP_CA_SHA256 to one of the new fingerprints.",
                ", ".join(before) or "none", ", ".join(prints))


def _directory_for(settings):
    """Only with a pin: without one there is nothing to look for."""
    if getattr(settings, "ldap_ca_sha256", "") and getattr(settings, "ldap_bind_dn", ""):
        return directory_lookup(settings)
    return None


def startup_report(settings, cwd: Path | None = None) -> None:
    """One look in the log tells an operator who runs the app, from where, with which
    settings file and which LDAP trust. Warns about a .env other users can read."""
    import getpass

    cwd = cwd or Path.cwd()
    env = cwd / ".env"
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001  no user name in the password database
        user = str(os.getuid())
    log.info("running as user %s in %s, settings file %s", user, cwd,
             env.resolve() if env.exists() else "none (environment only)")
    if env.exists() and env.stat().st_mode & 0o077:
        log.warning("%s holds secrets and other users can read it: run chmod 600 %s",
                    env.resolve(), env.resolve())
    ca, pin = settings.ldap_ca_cert_file, settings.ldap_ca_sha256
    if not settings.ldap_tls_verify:
        log.warning("LDAP_TLS_VERIFY=false: domain controller certificates are not checked")
        return
    if not ca:
        log.info("LDAP CA: system trust store (LDAP_CA_CERT_FILE is empty)")
        return
    prints = _file_prints(ca)
    if pin and not pin_set(pin) & {normalize_pin(p) for p in prints}:
        log.warning("LDAP CA: %s does not contain a CA pinned by LDAP_CA_SHA256", ca)
    else:
        log.info("LDAP CA: %s, SHA-256 %s, %s", ca, ", ".join(prints) or "none",
                 "pinned" if pin else "NOT pinned (set LDAP_CA_SHA256)")


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv
    from pydantic import ValidationError

    from .config import Settings

    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        s = Settings()
    except ValidationError as e:
        print(f"Invalid configuration: {len(e.errors())} problem(s); run the app to see them.")
        return 1
    if not s.ldap_ca_cert_file:
        print("Set LDAP_CA_CERT_FILE to the path where the file should be written.")
        return 1
    try:
        prints = fetch_ca_file(s.ldap_url, s.ldap_ca_cert_file, pin=s.ldap_ca_sha256,
                               force="--force" in argv, timeout=s.ldap_timeout,
                               directory=_directory_for(s))
    except FetchError as e:
        print(f"Failed: {e}")
        return 1
    print(f"Wrote {s.ldap_ca_cert_file}. CA certificate fingerprints (SHA-256):")
    for p in prints:
        print(f"  {p}")
    if not s.ldap_ca_sha256:
        print("Compare them with the directory team, then set LDAP_CA_SHA256 to pin one.")
    return 0
