"""Share invitation email: message content, header safety, SMTP settings."""

import asyncio
import logging
import smtplib

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


def test_every_outcome_is_logged(monkeypatch, caplog):
    off = Mailer(Settings(_env_file=None, **REQUIRED))
    on = mailer(smtp_password="hunter2-secret")
    sent = []
    with caplog.at_level(logging.INFO, logger="bucketeer.mailer"):
        asyncio.run(off.send_share_invite("a@b.c", "A", "data", "read"))
        monkeypatch.setattr(on, "_send", sent.append)
        asyncio.run(on.send_share_invite("erin@example.org", "A", "data", "read"))

        def refused(msg):
            raise smtplib.SMTPRecipientsRefused({"x@example.org": (554, b"relay denied")})
        monkeypatch.setattr(on, "_send", refused)
        asyncio.run(on.send_share_invite("x@example.org", "A", "data", "read"))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("not sent: email is off (SMTP_FROM empty)" in m for m in msgs), msgs
    assert any("email sent to erin@example.org for bucket data" in m for m in msgs), msgs
    (fail,) = [m for m in msgs if "failed" in m]
    assert "SMTPRecipientsRefused" in fail and "relay denied" in fail
    assert "hunter2" not in " ".join(msgs) and len(sent) == 1


def test_describe_for_the_startup_log():
    assert "OFF" in Mailer(Settings(_env_file=None, **REQUIRED)).describe()
    line = mailer(smtp_port=25, smtp_security="starttls").describe()
    assert "ON" in line and "smtp.example.org:25 security=starttls" in line
    assert "MX" in mailer(smtp_host="").describe()


def test_sender_turns_email_on_and_needs_url():
    with pytest.raises(ValidationError, match="APP_URL"):
        Settings(_env_file=None, **REQUIRED, smtp_from="a@example.org")
    relay_only = Settings(_env_file=None, **REQUIRED, smtp_host="smtp.example.org")
    assert Mailer(relay_only).enabled is False


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


def test_report_with_attachment(monkeypatch, caplog):
    on = mailer()
    sent = []

    def send(msg):
        if msg["To"] == "bad@example.org":
            raise smtplib.SMTPRecipientsRefused({"bad@example.org": (550, b"no such user")})
        sent.append(msg)
    monkeypatch.setattr(on, "_send", send)
    with caplog.at_level(logging.INFO, logger="bucketeer.mailer"):
        res = asyncio.run(on.send_with_attachment(
            ["f@example.org", "bad@example.org", "x@y.z\r\nBcc: e@f.g"], "Chargeback\nOctober",
            "Attached.", "sheet.xlsx", b"PK\x03\x04data",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "billing report"))
    assert res == (["f@example.org"], ["bad@example.org", "x@y.z\r\nBcc: e@f.g"])
    (msg,) = sent
    assert "\n" not in msg["Subject"] and msg["To"] == "f@example.org"
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == "sheet.xlsx" and att.get_content() == b"PK\x03\x04data"
    assert att.get_content_type().endswith("spreadsheetml.sheet")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("billing report email sent to f@example.org" in m for m in msgs)
    assert any("no such user" in m for m in msgs)
    off = Mailer(Settings(_env_file=None, **REQUIRED))
    assert asyncio.run(off.send_with_attachment(["a@b.c"], "s", "b", "f", b"", "a/b", "x")) is None
