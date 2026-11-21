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
    ctx.load_verify_locations(cafile=path)
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return None
    except ssl.SSLCertVerificationError as e:
        return e.verify_message or str(e)
    except (OSError, ssl.SSLError) as e:
        return type(e).__name__


def fetch_ca_file(urls: list[str], path: str, *, pin: str = "", force: bool = False,
                  timeout: float = 5.0) -> list[str]:
    """Write the CA certificates of the first reachable server to `path`; return their
    SHA-256 fingerprints. Refuses to replace an existing file without `force`."""
    target = Path(path)
    if target.exists() and not force:
        raise FetchError(f"{path} exists; use --force to replace it")
    pin = normalize_pin(pin)
    errors = []
    for url in urls:
        host, port = _host_port(url)
        try:
            chain = _server_chain(host, port, timeout)
        except (OSError, ssl.SSLError) as e:
            errors.append(f"{host}: {type(e).__name__}")
            continue
        cas = chain[1:]
        if not cas:
            errors.append(f"{host}: it sent no CA certificate (only its own)")
            continue
        prints = [fingerprint(c) for c in cas]
        if pin and not any(normalize_pin(p) == pin for p in prints):
            raise FetchError(f"{host}: no CA certificate matches LDAP_CA_SHA256; "
                             f"it sent: {', '.join(prints)}")
        pem = "".join(ssl.DER_cert_to_PEM_cert(c) for c in cas)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".ldap-ca-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pem)
            os.chmod(tmp, 0o644)  # a public certificate, no secret
            bad = _verifies(tmp, host, port, timeout)
            if bad:
                raise FetchError(f"{host}: its certificate does not verify against the CA "
                                 f"certificates it sent ({bad}); the root CA may be missing")
            os.replace(tmp, target)
            try:
                _record(path).write_text(hosts_hash(urls))
            except OSError:
                log.warning("could not write %s", _record(path))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return prints
    raise FetchError("; ".join(errors) or "no LDAP_URL")


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
        prints = fetch_ca_file(urls, path, pin=pin, force=True, timeout=settings.ldap_timeout)
    except FetchError as e:
        log.error("LDAP_CA_CERT_FILE %s: %s, but the fetch failed (%s)%s", path, why, e,
                  "; the old file stays" if exists else "")
        return
    log.warning("LDAP_CA_CERT_FILE %s: %s, so it was fetched from the directory server. %s "
                "Before: %s. Now: %s.", path, why,
                "It matches LDAP_CA_SHA256." if pin else
                "NOT pinned: set LDAP_CA_SHA256 to one of the new fingerprints.",
                ", ".join(before) or "none", ", ".join(prints))


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
                               force="--force" in argv, timeout=s.ldap_timeout)
    except FetchError as e:
        print(f"Failed: {e}")
        return 1
    print(f"Wrote {s.ldap_ca_cert_file}. CA certificate fingerprints (SHA-256):")
    for p in prints:
        print(f"  {p}")
    if not s.ldap_ca_sha256:
        print("Compare them with the directory team, then set LDAP_CA_SHA256 to pin one.")
    return 0
