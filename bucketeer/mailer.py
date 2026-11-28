"""Share invitation email (CLAUDE.md "Sharing"), the monthly billing report with its
.xlsx attachment (`send_with_attachment`, CLAUDE.md "Billing"), the cloud over-quota alert,
the user notices (`send_near_full`, `send_key_age`, `notices.py`) and the admin alerts
(`send_admin_alert`, `alerts.py`). Optional: with `SMTP_FROM` empty nothing is sent.

With `SMTP_HOST` empty and `SMTP_FROM` set, the mail server is the recipient domain's MX record
(lowest preference first, then the next ones; no MX = the domain's own address), port 25,
STARTTLS when offered. The message holds no secret, so that TLS is not verified.

Plain text only. The message holds the owner's name, the bucket's short name (for a cloud
bucket, `location="cloud"`, its full AWS name and how to set up Cloud), the access level and
the portal's login link. Never a key, secret or presigned URL. Sending never fails a share:
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


def _size(n: int) -> str:
    """Bytes in binary units, as the portal shows them."""
    for unit, f in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if n >= f:
            return f"{n / f:,.1f} {unit}"
    return f"{n:,} bytes"


class Mailer:
    def __init__(self, settings):
        self.s = settings

    @property
    def enabled(self) -> bool:
        return bool(self.s.smtp_from)

    def describe(self) -> str:
        """One line for the startup log: is email on, and through which server."""
        s = self.s
        if not self.enabled:
            return ("share invitation, billing report and admin alert email is OFF "
                    "(SMTP_FROM is empty)")
        via = (f"{s.smtp_host}:{s.smtp_port} security={s.smtp_security}" if s.smtp_host
               else "the recipient domain's MX, port 25")
        return (f"share invitation, billing report and admin alert email is ON, from {s.smtp_from} "
                f"via {via}")

    def _build(self, to: str, owner_name: str, bucket: str, access: str,
               location: str = "onprem") -> EmailMessage:
        s = self.s
        owner = _clean(owner_name) or "A colleague"
        name = _clean(bucket, 63)
        msg = EmailMessage()
        msg["From"] = s.smtp_from
        msg["To"] = to
        if location == "cloud":
            msg["Subject"] = f"{owner} shared the cloud bucket {name} with you"
            msg.set_content(
                f"{owner} gave you {ACCESS.get(access, access)} access to the Cloud (AWS) "
                f"bucket \"{name}\" in {s.app_name}.\n\n"
                f"To use it, log in with your normal account and work in Cloud (the Cloud / "
                f"On Premises switch at the top; turn on Advanced if you do not see it). Then "
                f"choose \"Set up this computer\" to set up your cloud profile. The bucket "
                f"shows under \"Shared with you\":\n\n"
                f"{s.app_url.rstrip('/')}/\n\n"
                f"If you did not expect this message, you can ignore it.\n")
            return msg
        msg["Subject"] = f"{owner} shared the bucket {name} with you"
        msg.set_content(
            f"{owner} gave you {ACCESS.get(access, access)} access to the S3 bucket "
            f"\"{name}\" in {s.app_name}.\n\n"
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

    def _build_report(self, to: str, subject: str, body: str, filename: str,
                      data: bytes, mime: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from
        msg["To"] = to
        msg["Subject"] = _clean(subject, 150)
        msg.set_content(body)
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=_clean(filename, 100))
        return msg

    async def send_with_attachment(self, recipients: list[str], subject: str, body: str,
                                   filename: str, data: bytes, mime: str, what: str
                                   ) -> tuple[list[str], list[str]] | None:
        """One message per recipient (MX delivery is per domain anyway), each with the
        attachment. Returns (sent, failed) addresses, or None while email is off. Never
        raises; `what` names the message in the log lines ("billing report 2026-10")."""
        if not self.enabled:
            log.info("%s not sent: email is off (SMTP_FROM empty)", what)
            return None
        sent, failed = [], []
        for to in recipients:
            to = (to or "").strip()
            if not to or any(c in to for c in "\r\n<>,;\" "):
                log.info("%s not sent to an unusable address", what)
                failed.append(to)
                continue
            try:
                await asyncio.to_thread(
                    self._send, self._build_report(to, subject, body, filename, data, mime))
            except Exception as e:  # noqa: BLE001  logged; the caller audits the outcome
                log.warning("%s email to %s failed: %s: %s", what, to, type(e).__name__,
                            _clean(str(e), 200))
                failed.append(to)
                continue
            log.info("%s email sent to %s", what, to)
            sent.append(to)
        return sent, failed

    def _build_alert(self, to: str, subject: str, body: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from
        msg["To"] = to
        msg["Subject"] = _clean(subject, 150)
        msg.set_content(body)
        return msg

    async def send_admin_alert(self, recipients: list[str], subject: str, body: str
                               ) -> tuple[list[str], list[str]] | None:
        """An alert for the administrators (`alerts.py`): one plain-text message per
        recipient. Returns (sent, failed) addresses, or None while email is off. Never
        raises."""
        if not self.enabled:
            log.info("admin alert not sent: email is off (SMTP_FROM empty)")
            return None
        sent, failed = [], []
        for to in recipients:
            to = (to or "").strip()
            if not to or any(c in to for c in "\r\n<>,;\" "):
                log.info("admin alert not sent to an unusable address")
                failed.append(to)
                continue
            try:
                await asyncio.to_thread(self._send, self._build_alert(to, subject, body))
            except Exception as e:  # noqa: BLE001  logged; the caller audits the outcome
                log.warning("admin alert email to %s failed: %s: %s", to, type(e).__name__,
                            _clean(str(e), 200))
                failed.append(to)
                continue
            log.info("admin alert email sent to %s", to)
            sent.append(to)
        return sent, failed

    def _build_over_quota(self, to: str, bucket: str, size_tib: float, quota_tib: int,
                          as_of: str) -> EmailMessage:
        s = self.s
        msg = EmailMessage()
        msg["From"] = s.smtp_from
        msg["To"] = to
        msg["Subject"] = f"Cloud bucket {_clean(bucket, 63)} is over its quota"
        msg.set_content(
            f"Your cloud (AWS) bucket \"{_clean(bucket, 63)}\" holds about {size_tib:,.2f} TiB "
            f"(as of {_clean(as_of, 10)}), more than its quota of {quota_tib:,} TiB.\n\n"
            "AWS does not stop uploads at the quota, and the real AWS cost is billed. Raise the "
            "quota with \"Change quota\" or delete data you no longer need.\n\n"
            f"{s.app_url}\n\nYou get this reminder at most once a week per bucket.\n")
        return msg

    async def send_over_quota(self, to: str, bucket: str, size_tib: float, quota_tib: int,
                              as_of: str):
        """True (sent), False (failed or no usable address, logged) or None (email off)."""
        if not self.enabled:
            log.info("over-quota alert for %s not sent: email is off (SMTP_FROM empty)",
                     _clean(bucket, 63))
            return None
        to = (to or "").strip()
        if not to or any(c in to for c in "\r\n<>,;\" "):
            log.info("over-quota alert for %s not sent: no usable address", _clean(bucket, 63))
            return False
        try:
            await asyncio.to_thread(
                self._send, self._build_over_quota(to, bucket, size_tib, quota_tib, as_of))
        except Exception as e:  # noqa: BLE001  logged; retried the next day
            log.warning("over-quota alert to %s for %s failed: %s: %s", to, _clean(bucket, 63),
                        type(e).__name__, _clean(str(e), 200))
            return False
        log.info("over-quota alert sent to %s for %s", to, _clean(bucket, 63))
        return True

    # -- user notices (`notices.py`): near-full buckets and old access keys --

    async def _notice(self, to: str, msg_of, what: str):
        """True (sent), False (failed or no usable address, logged) or None (email off)."""
        if not self.enabled:
            log.info("%s not sent: email is off (SMTP_FROM empty)", what)
            return None
        to = (to or "").strip()
        if not to or any(c in to for c in "\r\n<>,;\" "):
            log.info("%s not sent: no usable address", what)
            return False
        try:
            await asyncio.to_thread(self._send, msg_of(to))
        except Exception as e:  # noqa: BLE001  logged; the caller retries the next day
            log.warning("%s email to %s failed: %s: %s", what, to, type(e).__name__,
                        _clean(str(e), 200))
            return False
        log.info("%s email sent to %s", what, to)
        return True

    def _message(self, to: str, subject: str, body: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from
        msg["To"] = to
        msg["Subject"] = _clean(subject, 150)
        msg.set_content(body)
        return msg

    async def send_near_full(self, to: str, bucket: str, size: int, quota: int, percent: int):
        """The on-premises bucket `bucket` (short name) holds `size` of its `quota` bytes."""
        name = _clean(bucket, 63)
        body = (
            f"Your bucket \"{name}\" in {self.s.app_name} holds {_size(size)} of its "
            f"{_size(quota)} quota ({percent}% full).\n\n"
            "When it reaches 100%, the storage refuses new uploads to this bucket until you "
            "make room or raise the quota.\n\n"
            "To raise the quota, open Bucketeer, find the bucket under Your buckets and choose "
            "More, Change quota. You can also delete data you no longer need.\n\n"
            f"{self.s.app_url.rstrip('/')}/\n\n"
            "You get this reminder at most once a week per bucket.\n")
        return await self._notice(
            to, lambda t: self._message(t, f"Bucket {name} is {percent}% full", body),
            f"near-full notice for {name}")

    async def send_key_age(self, to: str, keys: list[dict], limit_days: int, cloud: bool):
        """`keys` = [{last4, ageDays, exact, subuser?}]: the person's keys older than
        `limit_days`. Only the last 4 characters of a key are named."""
        lines = []
        for k in keys:
            what = (f"subuser {_clean(k['subuser'], 20)}" if k.get("subuser")
                    else "cloud (AWS) key" if cloud else "your key")
            age = f"{'' if k.get('exact') else 'at least '}{k['ageDays']:,} days old"
            lines.append(f"  - {what} ending in {_clean(k['last4'], 4)}, {age}")
        many = len(keys) > 1
        if cloud:
            how = ("Make a new key: run \"Set up this computer\" again while you work in Cloud; it "
                   "installs a fresh cloud key on that computer. Or, with Advanced on, choose "
                   "Rotate next to the key on the Credentials card. Then delete the old key on "
                   "the Credentials card once nothing uses it any more.")
        else:
            how = ("Turn on Advanced, choose the key on the Credentials card and click Rotate: "
                   "Bucketeer makes a new key first, then removes the old one. Then update the "
                   "computers and scripts that used it; on a computer you set up with \"Set up "
                   "this computer\", simply run it again to install the new key.")
        body = (
            f"{'These access keys' if many else 'This access key'} of yours in "
            f"{self.s.app_name} {'are' if many else 'is'} older than {limit_days:,} days:\n\n"
            + "\n".join(lines) + "\n\n"
            "Old keys are a risk if they were ever copied somewhere they should not be. "
            f"Please replace {'them' if many else 'it'}.\n\n{how}\n\n"
            f"{self.s.app_url.rstrip('/')}/\n\n"
            "You get this reminder at most once a month per key.\n")
        subject = (f"Time to rotate your {'cloud (AWS)' if cloud else 'S3'} access "
                   f"key{'s' if many else ''}")
        return await self._notice(to, lambda t: self._message(t, subject, body),
                                  f"key age notice ({len(keys)} key(s))")

    async def send_share_invite(self, to: str, owner_name: str, bucket: str, access: str,
                                location: str = "onprem"):
        """`location` 'cloud': a Cloud (AWS) bucket (full AWS name), set up in Cloud."""
        if not self.enabled:
            log.info("share invitation for bucket %s not sent: email is off (SMTP_FROM empty)",
                     _clean(bucket, 63))
            return None
        to = (to or "").strip()
        if not to or any(c in to for c in "\r\n<>,;\" "):
            log.info("share invitation for bucket %s not sent: the recipient has no usable "
                     "email address in AD", _clean(bucket, 63))
            return False
        try:
            await asyncio.to_thread(self._send,
                                    self._build(to, owner_name, bucket, access, location))
        except Exception as e:  # never fail the share
            # SMTP errors carry the server's reply (e.g. "554 relay denied"), never our password
            log.warning("share invitation email to %s for bucket %s failed: %s: %s", to,
                        _clean(bucket, 63), type(e).__name__, _clean(str(e), 200))
            return False
        log.info("share invitation email sent to %s for bucket %s", to, _clean(bucket, 63))
        return True
