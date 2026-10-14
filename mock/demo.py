#!/usr/bin/env python3
"""Try Bucketeer locally, no AD, no croit, no Ceph: one command.

    .venv/bin/python3 mock/demo.py [--port 8000] [--croit-port 18080] [--moto-port 18081]
                                   [--keep-data] [--password demo]

Starts, all on 127.0.0.1 and in this one process: a moto S3 server, the mock croit
API (mock/croit_mock.py, mirrored into moto by mock/s3_bridge.py) and Bucketeer
itself. Bucketeer runs unchanged (real login flow, throttle, sessions, CSRF); only
the directory is replaced by `DemoDirectory`, injected through
`create_app(authenticator=...)`. Dev tooling only: nothing in `bucketeer/` imports
this, and no setting or environment variable can switch the demo on. The
`app.state.demo` flag that drives the "Demo mode" banner is set here, on the
finished app object.

The developer's real `.env` and Settings environment variables are ignored on
purpose, so the demo can never reach a real AD or croit.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # run as a script: only mock/ is on sys.path
    sys.path.insert(0, str(ROOT))

from bucketeer.config import Settings  # noqa: E402
from bucketeer.ldapauth import AuthUser, InvalidCredentials, Resolved  # noqa: E402
from bucketeer.main import create_app as create_bucketeer  # noqa: E402

DEMO_PASSWORD = "demo"
DOMAIN = "demo.example"
PLACEHOLDER_LDAP_URL = "ldaps://directory.demo.invalid:636"  # never contacted

# sAMAccountName -> (display name, is_admin). erin has no RGW account yet: the first
# login auto-creates it. alice and bob exist in the mock croit seed.
DEMO_USERS = {
    "alice": ("Alice Anderson", False),
    "bob": ("Bob Brown", False),
    "dana": ("Dana Diaz", True),
    "erin": ("Erin Evans", False),
}
BOB_EXTRA_KEY = ("BOBSECONDKEY0000003", "bob-secret-0003-demo")


class DemoDirectory:
    """A fake AD with the real authenticator interface (authenticate / resolve /
    verify, as in `AdAuthenticator`), so the real /api/login flow, throttle,
    sessions and CSRF run unchanged. Logins match like AD's filter: username,
    email or UPN, case-insensitively."""

    def __init__(self, password: str = DEMO_PASSWORD):
        self.password = password
        self.users = {
            sam: AuthUser(sam, display, f"{sam}@{DOMAIN}", is_admin)
            for sam, (display, is_admin) in DEMO_USERS.items()
        }

    def _find(self, login: str) -> AuthUser:
        key = login.strip().lower()
        if not key:
            raise InvalidCredentials("empty_login")
        for user in self.users.values():
            if key in (user.username.lower(), user.email.lower()):  # UPN == mail here
                return user
        raise InvalidCredentials("unknown_user")

    async def authenticate(self, login: str, password: str) -> AuthUser:
        return await self.verify(await self.resolve(login, password), password)

    async def resolve(self, login: str, password: str) -> Resolved:
        if not password or not password.strip():
            raise InvalidCredentials("empty_password")
        user = self._find(login)
        return Resolved(dn=f"CN={user.username},OU=Demo", url=PLACEHOLDER_LDAP_URL, user=user)

    async def verify(self, resolved: Resolved, password: str) -> AuthUser:
        if not secrets.compare_digest(password.encode(), self.password.encode()):
            raise InvalidCredentials("bad_password")
        return resolved.user


# Everything the real environment could set: the demo must not see any of it.
def scrub_environment() -> None:
    for name in Settings.model_fields:
        os.environ.pop(name.upper(), None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


def demo_settings(*, port: int, croit_url: str, croit_token: str, s3_url: str,
                  data_dir: str, session_secret: str | None = None) -> Settings:
    return Settings(
        _env_file=None,  # never the developer's real .env
        host="127.0.0.1",
        port=port,
        app_name="Bucketeer",
        data_dir=data_dir,
        session_secret=session_secret or secrets.token_urlsafe(32),
        session_cookie_secure=False,  # plain http on localhost
        ldap_url=PLACEHOLDER_LDAP_URL,
        ldap_base_dn="DC=demo,DC=example",
        ldap_bind_dn="CN=svc,OU=Demo",
        ldap_bind_password="unused-in-demo",
        croit_url=croit_url,
        croit_api_token=croit_token,
        s3_endpoint_url=s3_url,
    )


def seed_demo(state) -> None:
    """Extras on top of the mock's normal seed (alice, bob, acme$carol; no erin)."""
    state.put_key("bob", *BOB_EXTRA_KEY)


def build_demo_app(settings: Settings, croit=None, db=None, directory: DemoDirectory | None = None):
    app = create_bucketeer(
        settings, croit=croit, db=db, authenticator=directory or DemoDirectory()
    )
    app.state.demo = True
    return app


# -- process wiring ------------------------------------------------------------


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Run Bucketeer with a fake directory and mock croit.")
    p.add_argument("--port", type=int, default=8000, help="Bucketeer port (default 8000)")
    p.add_argument("--croit-port", type=int, default=18080, help="mock croit port (default 18080)")
    p.add_argument("--moto-port", type=int, default=18081, help="moto S3 port (default 18081)")
    p.add_argument("--keep-data", action="store_true", help="keep the temporary DATA_DIR on exit")
    p.add_argument("--password", default=DEMO_PASSWORD, 
                   help=f"demo password (default {DEMO_PASSWORD})")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    ports = {"Bucketeer": args.port, "mock croit": args.croit_port, "moto": args.moto_port}
    if len(set(ports.values())) != 3:
        sys.exit("error: the three ports must differ")
    for name, port in ports.items():
        if not port_free(port):
            sys.exit(f"error: port {port} ({name}) is already in use; pick another with "
                     f"--port / --croit-port / --moto-port")

    scrub_environment()
    import logging

    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # moto request log
    import uvicorn

    from mock.croit_mock import ADMIN_TOKEN, MockHooks
    from mock.croit_mock import create_app as create_croit
    from mock.s3_bridge import S3Bridge, endpoint_url, start_moto

    data_dir = tempfile.mkdtemp(prefix="bucketeer-demo-")
    aws_dir = Path(data_dir) / "aws"
    aws_dir.mkdir()
    moto = croit_server = None
    try:
        moto = start_moto("127.0.0.1", args.moto_port)
        bridge = S3Bridge(endpoint_url(moto))
        croit_app = create_croit(seed=True, task_seconds=0.3, prefix="/api",
                                 hooks=bridge.make_hooks(MockHooks),
                                 rgw_endpoint=bridge.host_port)
        bridge.seed_objects(croit_app.state.croit)
        seed_demo(croit_app.state.croit)

        croit_server = uvicorn.Server(uvicorn.Config(
            croit_app, host="127.0.0.1", port=args.croit_port, log_level="warning"))
        threading.Thread(target=croit_server.run, name="mock-croit", daemon=True).start()
        deadline = time.time() + 15
        while not croit_server.started:
            if time.time() > deadline:
                sys.exit("error: mock croit did not start")
            time.sleep(0.05)

        croit_url = f"http://127.0.0.1:{args.croit_port}/api"
        settings = demo_settings(port=args.port, croit_url=croit_url, croit_token=ADMIN_TOKEN,
                                 s3_url=bridge.endpoint, data_dir=data_dir)
        app = build_demo_app(settings, directory=DemoDirectory(args.password))

        print(f"""
Bucketeer DEMO  (fake directory, mock croit, moto S3; nothing here is real)

  Open:      http://127.0.0.1:{args.port}/
  Password:  {args.password}   (same for every user)
  Users:     alice  (Alice Anderson, regular, has buckets)
             bob    (Bob Brown, regular, 2 keys)
             dana   (Dana Diaz, ADMIN: admin page)
             erin   (Erin Evans, no S3 account yet: auto-created on first login)
             Log in by username, email (alice@{DOMAIN}) or UPN.

  croit mock:     CROIT_URL={croit_url}
                  admin API token: {ADMIN_TOKEN}
  S3 endpoint:    {bridge.endpoint}  (moto)
  Data dir:       {data_dir} ({'kept' if args.keep_data else 'deleted on exit'})

  Use the keys without touching your real ~/.aws: reveal them in the UI, download
  the files (named config and credentials) into {aws_dir}/ and run:
      AWS_CONFIG_FILE={aws_dir}/config AWS_SHARED_CREDENTIALS_FILE={aws_dir}/credentials \\
        aws --profile bucketeer s3 ls
  (--keep-data keeps that folder.)

Ctrl+C stops everything.
""", flush=True)
        # uvicorn re-raises SIGTERM with the default handler, which would skip `finally`.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
        return 0
    finally:
        if croit_server:
            croit_server.should_exit = True
        if moto:
            moto.stop()
        if args.keep_data:
            print(f"kept data dir: {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
