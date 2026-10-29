"""Share invitation email: message content, header safety, SMTP settings."""

import asyncio

import pytest
from pydantic import ValidationError

from bucketeer.config import Settings
from bucketeer.mailer import Mailer

from .conftest import REQUIRED

MAIL = {"smtp_host": "smtp.example.org", "smtp_from": "bucketeer@example.org",
        "app_url": "https://bucketeer.example.org"}


def mailer(**kw):
    return Mailer(Settings(_env_file=None, **{**REQUIRED, **MAIL, **kw}))


def test_message_has_link_and_no_secrets():
    msg = mailer()._build("erin@example.org", "Alice A", "data", "read")
    body = msg.get_content()
    assert "https://bucketeer.example.org/" in body and "read-only" in body
    assert msg["To"] == "erin@example.org" and "data" in msg["Subject"]


def test_names_with_newlines_cannot_inject_headers():
    msg = mailer()._build("erin@example.org", "Evil\r\nBcc: x@y.z", "data", "read")
    assert "\n" not in msg["Subject"] and msg["Bcc"] is None


def test_disabled_without_host_and_bad_address_not_sent(monkeypatch):
    off = Mailer(Settings(_env_file=None, **REQUIRED))
    assert asyncio.run(off.send_share_invite("a@b.c", "A", "d", "read")) is None
    on = mailer()
    monkeypatch.setattr(on, "_send", lambda m: pytest.fail("sent"))
    assert asyncio.run(on.send_share_invite("a@b.c\r\nBcc: x@y.z", "A", "d", "read")) is False


def test_send_failure_returns_false(monkeypatch):
    on = mailer()

    def boom(msg):
        raise OSError("down")
    monkeypatch.setattr(on, "_send", boom)
    assert asyncio.run(on.send_share_invite("a@b.c", "A", "d", "read")) is False


def test_host_requires_sender_and_url():
    with pytest.raises(ValidationError, match="SMTP_FROM, APP_URL"):
        Settings(_env_file=None, **REQUIRED, smtp_host="smtp.example.org")
    with pytest.raises(ValidationError, match="APP_URL"):
        Settings(_env_file=None, **REQUIRED, smtp_from="a@example.org")


def test_mx_mode_tries_mx_hosts_in_order(monkeypatch):
    m = mailer(smtp_host="")
    monkeypatch.setattr(m, "_mx_hosts", lambda a: ["mx1.example.org", "mx2.example.org"])
    tried = []

    class Smtp:
        def __init__(self, host, port, timeout):
            tried.append((host, port))
            if host == "mx1.example.org":
                raise OSError("refused")

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def has_extn(self, n): return False
        def send_message(self, msg): self.sent = msg

    monkeypatch.setattr("bucketeer.mailer.smtplib.SMTP", Smtp)
    assert asyncio.run(m.send_share_invite("e@example.org", "A", "d", "read")) is True
    assert tried == [("mx1.example.org", 25), ("mx2.example.org", 25)]


def test_mx_sorted_by_preference(monkeypatch):
    class R:
        def __init__(self, p, e): self.preference, self.exchange = p, e

    monkeypatch.setattr("bucketeer.mailer.dns.resolver.resolve",
                        lambda d, t, lifetime: [R(20, "b.example.org."), R(10, "a.example.org.")])
    assert mailer()._mx_hosts("x@example.org") == ["a.example.org", "b.example.org"]
