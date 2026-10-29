"""Share invitation email (CLAUDE.md "Sharing"). Optional: with `SMTP_FROM` empty nothing is sent.

With `SMTP_HOST` empty and `SMTP_FROM` set, the mail server is the recipient domain's MX record
(lowest preference first, then the next ones; no MX = the domain's own address), port 25,
STARTTLS when offered. The message holds no secret, so that TLS is not verified.

Plain text only. The message holds the owner's name, the bucket's short name, the access level
and the portal's login link. Never a key, secret or presigned URL. Sending never fails a share:
`send_share_invite` returns True (sent), False (failed, logged) or None (email is switched off).
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

import dns.exception
import dns.resolver

log = logging.getLogger("bucketeer.mailer")

ACCESS = {"read": "read-only", "readwrite": "read and write"}


def _clean(text: str, limit: int = 80) -> str:
    """One line, no control characters: names come from AD and users."""
    return "".join(c for c in text if c.isprintable()).strip()[:limit]


class Mailer:
    def __init__(self, settings):
        self.s = settings

    @property
    def enabled(self) -> bool:
        return bool(self.s.smtp_from)

    def _build(self, to: str, owner_name: str, bucket: str, access: str) -> EmailMessage:
        s = self.s
        owner = _clean(owner_name) or "A colleague"
        msg = EmailMessage()
        msg["From"] = s.smtp_from
        msg["To"] = to
        msg["Subject"] = f"{owner} shared the bucket {_clean(bucket, 63)} with you"
        msg.set_content(
            f"{owner} gave you {ACCESS.get(access, access)} access to the S3 bucket "
            f"\"{_clean(bucket, 63)}\" in {s.app_name}.\n\n"
            f"To use it, log in with your normal account. This creates your S3 account if you "
            f"do not have one yet, and the bucket then shows under \"Shared with you\":\n\n"
            f"{s.app_url.rstrip('/')}/\n\n"
            f"If you did not expect this message, you can ignore it.\n")
        return msg

    def _mx_hosts(self, address: str) -> list[str]:
        domain = address.rsplit("@", 1)[1]
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        except dns.resolver.NoAnswer:
            return [domain]
        except dns.exception.DNSException as e:
            raise OSError(f"MX lookup failed ({type(e).__name__})") from None
        return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]

    def _send_mx(self, msg: EmailMessage) -> None:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        last: Exception = OSError("no mail server")
        for host in self._mx_hosts(msg["To"])[:3]:
            try:
                with smtplib.SMTP(host, 25, timeout=15) as smtp:
                    smtp.ehlo()
                    if smtp.has_extn("starttls"):
                        smtp.starttls(context=ctx)
                        smtp.ehlo()
                    smtp.send_message(msg)
                return
            except (OSError, smtplib.SMTPException) as e:
                last = e
        raise last

    def _send(self, msg: EmailMessage) -> None:
        s = self.s
        if not s.smtp_host:
            return self._send_mx(msg)
        ctx = ssl.create_default_context()
        if s.smtp_security == "ssl":
            cm = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15, context=ctx)
        else:
            cm = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15)
        with cm as smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=ctx)
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(msg)

    async def send_share_invite(self, to: str, owner_name: str, bucket: str, access: str):
        if not self.enabled:
            return None
        to = (to or "").strip()
        if not to or any(c in to for c in "\r\n<>,;\" "):
            log.info("no usable email address for a share invitation")
            return False
        try:
            await asyncio.to_thread(self._send, self._build(to, owner_name, bucket, access))
            return True
        except Exception as e:  # never fail the share; the text may hold a server name only
            log.warning("share invitation email failed: %s", type(e).__name__)
            return False
