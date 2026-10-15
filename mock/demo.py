#!/usr/bin/env python3
"""Try Bucketeer locally, no AD, no croit, no Ceph: one command.

    .venv/bin/python3 mock/demo.py [--port 8000] [--moto-port 18081] [--admin-ops-port 18082]
                                   [--backend rgw|croit] [--croit-port 18080]
                                   [--keep-data] [--password demo]

Starts, all on 127.0.0.1 and in this one process: a moto S3 server, the RGW Admin Ops mock
(mock/rgw_admin_mock.py) over the mock croit's state (mock/croit_mock.py, mirrored into moto
by mock/s3_bridge.py) and Bucketeer itself. Bucketeer runs unchanged (real login flow,
throttle, sessions, CSRF); only the directory is replaced by `DemoDirectory`, injected
through `create_app(authenticator=...)`. Dev tooling only: nothing in `bucketeer/` imports
this, and no setting or environment variable can switch the demo on. The `app.state.demo`
flag that drives the "Demo mode" banner is set here, on the finished app object.

The default backend is `rgw`, like production (Phase 5 A7): Bucketeer signs Admin Ops
requests with the mock's admin key, creates and versions buckets with the user's own keys
against moto (the bridge mirrors them into the shared state) and deletes non-empty buckets
as a background job. The placement list is entered by hand there, so the demo pre-fills it
with the mock's two placements (change it on the admin Settings tab). `--backend croit`
runs the mock croit API over HTTP instead (`--admin-ops` additionally serves the Admin Ops
mock next to it, for trying the RGW tooling by hand).

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
        self.disabled: set[str] = set()  # sAMAccountNames (lowercase) of disabled accounts
        self.left_group: set[str] = set()  # ... that left the required group
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

    async def lookup_user(self, login: str) -> AuthUser:
        """Recipient lookup for sharing (no password), like `AdAuthenticator.lookup_user`."""
        return self._find(login)

    async def account_statuses(self, usernames) -> dict[str, str]:
        """Offboarding job lookup, like `AdAuthenticator.account_statuses`: `ok`,
        `disabled`, `not_in_group` or `not_found`. Edit `disabled` / `left_group` to demo."""
        out = {}
        for name in usernames:
            key = name.strip().lower()
            if key not in self.users:
                out[name] = "not_found"
            elif key in self.disabled:
                out[name] = "disabled"
            elif key in self.left_group:
                out[name] = "not_in_group"
            else:
                out[name] = "ok"
        return out

    async def verify(self, resolved: Resolved, password: str) -> AuthUser:
        if not secrets.compare_digest(password.encode(), self.password.encode()):
            raise InvalidCredentials("bad_password")
        return resolved.user


# Everything the real environment could set: the demo must not see any of it.
def scrub_environment() -> None:
    for name in Settings.model_fields:
        os.environ.pop(name.upper(), None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


def demo_settings(*, port: int, s3_url: str, data_dir: str, backend: str = "rgw",
                  croit_url: str = "", croit_token: str = "",
                  rgw_admin_url: str = "", rgw_admin_access_key: str = "",
                  rgw_admin_secret_key: str = "",
                  session_secret: str | None = None) -> Settings:
    """The backend is always passed on explicitly. `rgw` (default) needs the `rgw_admin_*`
    values, `croit` the `croit_*` ones; the other backend's are left out."""
    backend_settings = (
        {"rgw_admin_url": rgw_admin_url, "rgw_admin_access_key": rgw_admin_access_key,
         "rgw_admin_secret_key": rgw_admin_secret_key}
        if backend == "rgw" else {"croit_url": croit_url, "croit_api_token": croit_token}
    )
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
        admin_backend=backend,
        s3_endpoint_url=s3_url,
        **backend_settings,
    )


# What an admin would type on the Settings tab: the mock cluster's placements.
DEMO_PLACEMENT = {"default": "default-placement",
                  "allowed": ["default-placement", "nvme-placement"]}


def seed_demo(state) -> None:
    """Extras on top of the mock's normal seed (alice, bob, acme$carol; no erin)."""
    state.put_key("bob", *BOB_EXTRA_KEY)


def build_demo_app(settings: Settings, croit=None, db=None, directory: DemoDirectory | None = None):
    """With the rgw backend and a database passed in, pre-fill the manual placement list."""
    if db is not None and settings.admin_backend == "rgw":
        from bucketeer.policy_settings import PLACEMENT_KEY

        if db.settings.get(PLACEMENT_KEY) is None:
            db.settings.set(PLACEMENT_KEY, DEMO_PLACEMENT, by="demo")
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
    p = argparse.ArgumentParser(description="Run Bucketeer with a fake directory and mock cluster.")
    p.add_argument("--backend", choices=("rgw", "croit"), default="rgw",
                   help="admin backend Bucketeer uses (default rgw, as in production)")
    p.add_argument("--port", type=int, default=8000, help="Bucketeer port (default 8000)")
    p.add_argument("--croit-port", type=int, default=18080,
                   help="mock croit port, --backend croit only (default 18080)")
    p.add_argument("--moto-port", type=int, default=18081, help="moto S3 port (default 18081)")
    p.add_argument("--admin-ops", action="store_true",
                   help="with --backend croit, also serve the RGW Admin Ops mock "
                        "(always served with --backend rgw)")
    p.add_argument("--admin-ops-port", type=int, default=18082,
                   help="Admin Ops mock port (default 18082)")
    p.add_argument("--keep-data", action="store_true", help="keep the temporary DATA_DIR on exit")
    p.add_argument("--password", default=DEMO_PASSWORD,
                   help=f"demo password (default {DEMO_PASSWORD})")
    p.add_argument("--disabled", default="", metavar="USERS",
                   help="comma-separated demo users that start as disabled in the fake AD, "
                        "to try the offboarding job (e.g. --disabled bob)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    rgw = args.backend == "rgw"
    serve_ops = rgw or args.admin_ops
    ports = {"Bucketeer": args.port, "moto": args.moto_port}
    if not rgw:
        ports["mock croit"] = args.croit_port
    if serve_ops:
        ports["Admin Ops mock"] = args.admin_ops_port
    if len(set(ports.values())) != len(ports):
        sys.exit("error: the ports must differ")
    for name, port in ports.items():
        if not port_free(port):
            sys.exit(f"error: port {port} ({name}) is already in use; pick another with "
                     f"--port / --croit-port / --moto-port / --admin-ops-port")

    scrub_environment()
    import logging

    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # moto request log
    import uvicorn

    from bucketeer.db import DB_FILENAME, Database
    from mock.croit_mock import ADMIN_TOKEN, MockHooks
    from mock.croit_mock import create_app as create_croit
    from mock.s3_bridge import S3Bridge, endpoint_url, start_moto

    data_dir = tempfile.mkdtemp(prefix="bucketeer-demo-")
    aws_dir = Path(data_dir) / "aws"
    aws_dir.mkdir()
    moto = croit_server = ops_server = db = None
    try:
        moto = start_moto("127.0.0.1", args.moto_port)
        bridge = S3Bridge(endpoint_url(moto))
        # The mock croit app always exists: its state is the "cluster" (users, keys, buckets)
        # that both the croit mock API and the Admin Ops mock serve.
        croit_app = create_croit(seed=True, task_seconds=0.3, prefix="/api",
                                 hooks=bridge.make_hooks(MockHooks),
                                 rgw_endpoint=bridge.host_port)
        bridge.seed_objects(croit_app.state.croit)
        seed_demo(croit_app.state.croit)

        if not rgw:
            croit_server = uvicorn.Server(uvicorn.Config(
                croit_app, host="127.0.0.1", port=args.croit_port, log_level="warning"))
            threading.Thread(target=croit_server.run, name="mock-croit", daemon=True).start()
            deadline = time.time() + 15
            while not croit_server.started:
                if time.time() > deadline:
                    sys.exit("error: mock croit did not start")
                time.sleep(0.05)

        ops_app = ops_url = None
        if serve_ops:
            from mock.rgw_admin_mock import create_app as create_ops
            from mock.rgw_admin_mock import default_admins, describe_admins, start_server

            # only the full-cap service user: the read-only mock principal would show up as a user
            ops_app = create_ops(croit_app.state.croit, admins=default_admins()[:1])
            ops_server = start_server(ops_app, "127.0.0.1", args.admin_ops_port)
            ops_url = f"http://127.0.0.1:{args.admin_ops_port}"

        if rgw:
            admin = ops_app.state.admin_ops.admins[0]
            settings = demo_settings(
                port=args.port, s3_url=bridge.endpoint, data_dir=data_dir, backend="rgw",
                rgw_admin_url=ops_url, rgw_admin_access_key=admin.access_key,
                rgw_admin_secret_key=admin.secret_key)
            backend_note = (f"  Admin backend:  rgw (RGW Admin Ops mock)\n"
                            f"                  RGW_ADMIN_URL={ops_url}\n"
                            f"                  service key {admin.access_key} "
                            f"(caps: users, buckets, usage, metadata)\n"
                            f"                  placements pre-filled on the Settings tab: "
                            f"{', '.join(DEMO_PLACEMENT['allowed'])}\n")
        else:
            croit_url = f"http://127.0.0.1:{args.croit_port}/api"
            settings = demo_settings(
                port=args.port, s3_url=bridge.endpoint, data_dir=data_dir, backend="croit",
                croit_url=croit_url, croit_token=ADMIN_TOKEN)
            backend_note = (f"  Admin backend:  croit (mock croit API)\n"
                            f"                  CROIT_URL={croit_url}\n"
                            f"                  admin API token: {ADMIN_TOKEN}\n")
            if ops_url:
                backend_note += (f"  Admin Ops mock: RGW_ADMIN_URL={ops_url} "
                                 f"(not used by Bucketeer)\n"
                                 f"{describe_admins(ops_app)}\n")
        directory = DemoDirectory(args.password)
        directory.disabled |= {n.strip().lower() for n in args.disabled.split(",") if n.strip()}
        db = Database(Path(data_dir) / DB_FILENAME)
        app = build_demo_app(settings, db=db, directory=directory)

        print(f"""
Bucketeer DEMO  (fake directory, mock cluster, moto S3; nothing here is real)

  Open:      http://127.0.0.1:{args.port}/
  Password:  {args.password}   (same for every user)
  Users:     alice  (Alice Anderson, regular, has buckets)
             bob    (Bob Brown, regular, 2 keys)
             dana   (Dana Diaz, ADMIN: admin page)
             erin   (Erin Evans, no S3 account yet: auto-created on first login)
             Log in by username, email (alice@{DOMAIN}) or UPN.

{backend_note}  S3 endpoint:    {bridge.endpoint}  (moto)
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
        if ops_server:
            ops_server.should_exit = True
        if moto:
            moto.stop()
        if db:
            db.close()
        if args.keep_data:
            print(f"kept data dir: {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
