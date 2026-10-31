"""LDAP CA fetch (bucketeer/ldapca.py) against a fake TLS server on localhost."""

import datetime
import ssl
import threading
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from bucketeer import ldapca


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject, issuer_name, key, signer, *, ca, san=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(_name(issuer_name))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if san:
        b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
    return b.sign(signer, hashes.SHA256())


def _pem(c):
    return c.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def pki(tmp_path):
    """Root CA -> leaf for localhost. Returns a function that serves a chain."""
    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    root = _cert("Test Root", "Test Root", root_key, root_key, ca=True)
    leaf = _cert("localhost", "Test Root", leaf_key, root_key, ca=False, san="localhost")
    key_pem = leaf_key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
    servers = []

    def serve(send_ca=True):
        chain = tmp_path / f"chain{len(servers)}.pem"
        chain.write_bytes(_pem(leaf) + (_pem(root) if send_ca else b""))
        keyf = tmp_path / "leaf.key"
        keyf.write_bytes(key_pem)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(chain, keyf)
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        srv.settimeout(0.2)
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    c, _ = srv.accept()
                except OSError:
                    continue
                try:
                    ctx.wrap_socket(c, server_side=True).close()
                except (OSError, ssl.SSLError):
                    pass

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        servers.append((srv, stop, t))
        return f"ldaps://localhost:{srv.getsockname()[1]}"

    yield SimpleNamespace(serve=serve, root=root)
    for srv, stop, t in servers:
        stop.set()
        t.join()
        srv.close()


def test_fetch_writes_only_the_ca_and_it_verifies(pki, tmp_path):
    url = pki.serve()
    out = tmp_path / "sub" / "ca.pem"
    prints = ldapca.fetch_ca_file([url], str(out))
    assert prints == [ldapca.fingerprint(pki.root.public_bytes(serialization.Encoding.DER))]
    text = out.read_text()
    assert text.count("BEGIN CERTIFICATE") == 1  # the root, not the leaf
    assert text.encode() == _pem(pki.root)


def test_existing_file_is_kept_without_force(pki, tmp_path):
    url = pki.serve()
    out = tmp_path / "ca.pem"
    out.write_text("old")
    with pytest.raises(ldapca.FetchError, match="--force"):
        ldapca.fetch_ca_file([url], str(out))
    assert out.read_text() == "old"
    ldapca.fetch_ca_file([url], str(out), force=True)
    assert "BEGIN CERTIFICATE" in out.read_text()


def test_pin_must_match(pki, tmp_path):
    url = pki.serve()
    good = ldapca.fingerprint(pki.root.public_bytes(serialization.Encoding.DER))
    out = tmp_path / "ca.pem"
    with pytest.raises(ldapca.FetchError, match="LDAP_CA_SHA256"):
        ldapca.fetch_ca_file([url], str(out), pin="00" * 32)
    assert not out.exists()
    ldapca.fetch_ca_file([url], str(out), pin=good.lower())  # colons and case do not matter
    assert out.exists()


def test_server_without_ca_certificate_is_refused(pki, tmp_path):
    url = pki.serve(send_ca=False)
    out = tmp_path / "ca.pem"
    with pytest.raises(ldapca.FetchError, match="no CA certificate"):
        ldapca.fetch_ca_file([url], str(out))
    assert not out.exists()


def test_unreachable_first_server_falls_over_to_the_next(pki, tmp_path):
    good = pki.serve()
    out = tmp_path / "ca.pem"
    ldapca.fetch_ca_file(["ldaps://localhost:1", good], str(out), timeout=2)
    assert out.exists()


def test_ensure_fetches_a_missing_file_only(pki, tmp_path, caplog):
    url = pki.serve()
    out = tmp_path / "ca.pem"
    s = SimpleNamespace(ldap_ca_cert_file=str(out), ldap_url=[url], ldap_ca_sha256="",
                        ldap_timeout=3)
    ldapca.ensure_ca_file(s)
    assert out.exists()
    assert "NOT pinned" in caplog.text
    out.write_text("keep me")
    ldapca.ensure_ca_file(s)
    assert out.read_text() == "keep me"


def test_ensure_failure_only_logs(tmp_path, caplog):
    out = tmp_path / "ca.pem"
    s = SimpleNamespace(ldap_ca_cert_file=str(out), ldap_url=["ldaps://localhost:1"],
                        ldap_ca_sha256="", ldap_timeout=1)
    ldapca.ensure_ca_file(s)
    assert not out.exists()
    assert "could not be fetched" in caplog.text


def test_pin_setting_is_validated():
    from bucketeer.config import Settings
    from tests.conftest import REQUIRED

    s = Settings(_env_file=None, **REQUIRED, ldap_ca_sha256="ab:" * 31 + "ab")
    assert s.ldap_ca_sha256 == "AB" * 32
    with pytest.raises(ValueError, match="SHA-256"):
        Settings(_env_file=None, **REQUIRED, ldap_ca_sha256="xyz")
