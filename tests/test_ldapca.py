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
                        ldap_timeout=3, ldap_tls_verify=True)
    ldapca.ensure_ca_file(s)
    assert out.exists()
    assert "NOT pinned" in caplog.text
    out.write_text("keep me")
    ldapca.ensure_ca_file(s)
    assert out.read_text() == "keep me"


def test_ensure_refetches_when_ldap_url_changes(pki, tmp_path, caplog):
    first, second = pki.serve(), pki.serve()
    out = tmp_path / "ca.pem"

    def settings(url):
        return SimpleNamespace(ldap_ca_cert_file=str(out), ldap_url=[url], ldap_ca_sha256="",
                               ldap_timeout=3, ldap_tls_verify=True)

    ldapca.ensure_ca_file(settings(first))  # missing: fetched, host list recorded
    assert (tmp_path / "ca.pem.hosts").read_text() == ldapca.hosts_hash([first])
    out.write_text("tampered")
    ldapca.ensure_ca_file(settings(first))  # same servers: left alone
    assert out.read_text() == "tampered"
    caplog.clear()
    ldapca.ensure_ca_file(settings(second))  # servers changed: fetched again
    assert out.read_bytes() == _pem(pki.root)
    assert "LDAP_URL changed" in caplog.text
    assert (tmp_path / "ca.pem.hosts").read_text() == ldapca.hosts_hash([second])


def test_changed_servers_that_fail_keep_the_old_file(pki, tmp_path, caplog):
    good = pki.serve()
    out = tmp_path / "ca.pem"
    s = SimpleNamespace(ldap_ca_cert_file=str(out), ldap_url=[good], ldap_ca_sha256="",
                        ldap_timeout=1, ldap_tls_verify=True)
    ldapca.ensure_ca_file(s)
    before = out.read_text()
    s.ldap_url = ["ldaps://localhost:1"]
    ldapca.ensure_ca_file(s)
    assert out.read_text() == before
    assert "the old file stays" in caplog.text


def test_file_supplied_by_hand_is_adopted_not_replaced(pki, tmp_path):
    out = tmp_path / "ca.pem"
    out.write_text("mine")
    s = SimpleNamespace(ldap_ca_cert_file=str(out), ldap_url=[pki.serve()], ldap_ca_sha256="",
                        ldap_timeout=3, ldap_tls_verify=True)
    ldapca.ensure_ca_file(s)
    assert out.read_text() == "mine"
    assert (tmp_path / "ca.pem.hosts").exists()


def test_ensure_failure_only_logs(tmp_path, caplog):
    out = tmp_path / "ca.pem"
    s = SimpleNamespace(ldap_ca_cert_file=str(out), ldap_url=["ldaps://localhost:1"],
                        ldap_ca_sha256="", ldap_timeout=1,
                        ldap_tls_verify=True)
    ldapca.ensure_ca_file(s)
    assert not out.exists()
    assert "the fetch failed" in caplog.text


def test_no_verification_needs_no_ca_file(tmp_path):
    out = tmp_path / "ca.pem"
    s = SimpleNamespace(ldap_ca_cert_file=str(out), ldap_url=["ldaps://localhost:1"],
                        ldap_ca_sha256="", ldap_timeout=1, ldap_tls_verify=False)
    ldapca.ensure_ca_file(s)
    assert not out.exists()


def test_pin_setting_is_validated():
    from bucketeer.config import Settings
    from tests.conftest import REQUIRED

    s = Settings(_env_file=None, **REQUIRED, ldap_ca_sha256="ab:" * 31 + "ab")
    assert s.ldap_ca_sha256 == "AB" * 32
    with pytest.raises(ValueError, match="SHA-256"):
        Settings(_env_file=None, **REQUIRED, ldap_ca_sha256="xyz")
    two = Settings(_env_file=None, **REQUIRED, ldap_ca_sha256="aa" * 32 + ", " + "bb" * 32)
    assert two.ldap_ca_sha256 == "AA" * 32 + "," + "BB" * 32


def _serve(tmp_path, name, certs, key):
    """TLS server on localhost presenting `certs` (leaf first); returns (url, stop)."""
    import socket

    chain, keyf = tmp_path / f"{name}.pem", tmp_path / f"{name}.key"
    chain.write_bytes(b"".join(_pem(c) for c in certs))
    keyf.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain, keyf)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.2)
    done = threading.Event()

    def loop():
        while not done.is_set():
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

    def stop():
        done.set()
        t.join()
        srv.close()

    return f"ldaps://localhost:{srv.getsockname()[1]}", stop


def test_two_issuing_cas_pinned_from_all_servers(tmp_path, caplog):
    """OSU style: one root (never sent), two intermediates, each DC sends one."""
    k = [ec.generate_private_key(ec.SECP256R1()) for _ in range(5)]
    mid1 = _cert("Issuing CA 1", "Root", k[1], k[0], ca=True)
    mid2 = _cert("Issuing CA 2", "Root", k[2], k[0], ca=True)
    leaf1 = _cert("localhost", "Issuing CA 1", k[3], k[1], ca=False, san="localhost")
    leaf2 = _cert("localhost", "Issuing CA 2", k[4], k[2], ca=False, san="localhost")
    der = serialization.Encoding.DER
    fp1, fp2 = (ldapca.fingerprint(m.public_bytes(der)) for m in (mid1, mid2))
    (u1, s1), (u2, s2) = _serve(tmp_path, "a", [leaf1, mid1], k[3]), _serve(
        tmp_path, "b", [leaf2, mid2], k[4])
    try:
        out = tmp_path / "ca.pem"
        assert ldapca.fetch_ca_file([u1, u2], str(out), pin=f"{fp1},{fp2}") == [fp1, fp2]
        assert out.read_bytes() == _pem(mid1) + _pem(mid2)
        caplog.clear()
        assert ldapca.fetch_ca_file([u1, u2], str(out), pin=fp1, force=True) == [fp1]
        assert "no CA certificate matches" in caplog.text  # the second server is skipped
        with pytest.raises(ldapca.FetchError, match="matches LDAP_CA_SHA256"):
            ldapca.fetch_ca_file([u2], str(out), pin=fp1, force=True)
        assert out.read_bytes() == _pem(mid1)  # a refused fetch keeps the file
    finally:
        s1()
        s2()


def test_pinned_root_is_taken_from_the_directory(tmp_path, caplog):
    """The pin is the root, which the server never sends: it comes from the directory,
    and only a root the server's chain really leads to is written."""
    k = [ec.generate_private_key(ec.SECP256R1()) for _ in range(4)]
    root = _cert("Root", "Root", k[0], k[0], ca=True)
    mid = _cert("Issuing CA", "Root", k[1], k[0], ca=True)
    leaf = _cert("localhost", "Issuing CA", k[2], k[1], ca=False, san="localhost")
    other_root = _cert("Root", "Root", k[3], k[3], ca=True)  # same name, wrong key
    der = serialization.Encoding.DER
    pin = ldapca.fingerprint(root.public_bytes(der))
    url, stop = _serve(tmp_path, "a", [leaf, mid], k[2])
    asked = []

    def directory(host, port, cafile):
        asked.append(open(cafile).read())
        return [other_root.public_bytes(der), root.public_bytes(der)]

    try:
        out = tmp_path / "ca.pem"
        assert ldapca.fetch_ca_file([url], str(out), pin=pin, directory=directory) == [pin]
        assert out.read_bytes() == _pem(root)
        assert asked == [_pem(mid).decode()]  # the TLS check used what the server sent
        assert "from the directory" in caplog.text
        bad = ldapca.fingerprint(other_root.public_bytes(der))
        with pytest.raises(ldapca.FetchError, match="does not verify"):
            ldapca.fetch_ca_file([url], str(out), pin=bad, directory=directory, force=True)
        assert out.read_bytes() == _pem(root)

        def broken(*a):
            raise OSError("down")

        with pytest.raises(ldapca.FetchError, match="directory lookup failed"):
            ldapca.fetch_ca_file([url], str(out), pin=pin, directory=broken, force=True)
    finally:
        stop()


def test_intermediate_ca_without_root_is_enough(tmp_path):
    """Domain controllers send leaf + intermediate, not the root: the fetch keeps the
    intermediate, and it verifies as a trust anchor (Python 3.12 needs the flag)."""
    import socket

    root_key, mid_key, leaf_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    mid = _cert("Test Issuing CA", "Test Root", mid_key, root_key, ca=True)
    leaf = _cert("localhost", "Test Issuing CA", leaf_key, mid_key, ca=False, san="localhost")
    chain, keyf = tmp_path / "chain.pem", tmp_path / "leaf.key"
    chain.write_bytes(_pem(leaf) + _pem(mid))
    keyf.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain, keyf)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
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
    try:
        out = tmp_path / "ca.pem"
        ldapca.fetch_ca_file([f"ldaps://localhost:{srv.getsockname()[1]}"], str(out))
        assert out.read_bytes() == _pem(mid)
    finally:
        stop.set()
        t.join()
        srv.close()


def test_ldap3_trusts_an_intermediate_anchor():
    import ldap3.core.tls as t

    from bucketeer import ldapauth  # noqa: F401  installs the wrapper

    assert t.create_default_context().verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


def test_startup_report(tmp_path, caplog, pki):
    caplog.set_level("INFO")
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    env.chmod(0o644)
    ca = tmp_path / "ca.pem"
    ca.write_bytes(_pem(pki.root))
    good = ldapca.fingerprint(pki.root.public_bytes(serialization.Encoding.DER))

    def report(**kw):
        caplog.clear()
        s = SimpleNamespace(ldap_ca_cert_file=str(ca), ldap_ca_sha256="", ldap_tls_verify=True)
        for k, v in kw.items():
            setattr(s, k, v)
        ldapca.startup_report(s, cwd=tmp_path)
        return caplog.text

    text = report()
    assert "running as user" in text and str(env) in text
    assert "other users can read it" in text and "NOT pinned" in text and good in text
    env.chmod(0o600)
    assert "other users" not in report(ldap_ca_sha256=good)
    assert "pinned" in caplog.text and "NOT pinned" not in caplog.text
    assert "does not contain a CA pinned" in report(ldap_ca_sha256="00" * 32)
    assert "system trust store" in report(ldap_ca_cert_file="")
    assert "not checked" in report(ldap_tls_verify=False)
