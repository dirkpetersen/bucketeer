import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from . import (
    admin,
    admin_settings,
    audit_viewer,
    auth,
    bucket_keys,
    buckets,
    credentials,
    lifecycle,
    profile,
    rgw_user,
    s3client,
    share_links,
)
from .config import Settings
from .croit import CroitClient
from .db import DB_FILENAME, Database
from .ldapauth import AdAuthenticator

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# No inline script/style anywhere in the frontend, so the policy can be strict.
# (CSSOM changes from JS, e.g. element.style.x = ..., are allowed by style-src 'self'.)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
ASSET_CACHE = "public, max-age=86400"  # fonts and images: stable, not fingerprinted


def create_app(
    settings: Settings,
    croit: CroitClient | None = None,
    db: Database | None = None,
    authenticator: AdAuthenticator | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """`croit` lets tests inject a client (e.g. on an ASGI transport); `db` an
    already-open database (e.g. in-memory), which the caller then closes;
    `authenticator` a fake-LDAP one; `clock` drives session expiry and the
    login throttle."""
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    client = croit or CroitClient(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        owned = db is None
        if owned:
            # DATA_DIR is relative to the working directory (repo root).
            _app.state.db = Database(Path(settings.data_dir) / DB_FILENAME)
        cleanup = None
        if bucket_keys.CLEANUP_ON_START:  # background: never blocks startup or /health
            cleanup = asyncio.get_running_loop().create_task(
                bucket_keys.cleanup_pending(_app))
        try:
            yield
        finally:
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
            await _app.state.rgw.aclose()
            await client.aclose()
            if owned:
                _app.state.db.close()

    app = FastAPI(
        title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.croit = client  # one client per app, closed on shutdown
    app.state.db = db  # else opened in the lifespan; /health never touches it

    app.state.authenticator = authenticator or AdAuthenticator(settings)
    app.state.sessions = auth.SessionManager(settings, clock)
    app.state.throttle = auth.LoginThrottle(clock)
    app.state.rgw = rgw_user.RgwResolver(client, lambda: app.state.db, settings.s3_tenant)

    app.state.endpoints = credentials.EndpointResolver(client, settings, clock)
    app.state.s3 = s3client.S3Clients(client, settings, app.state.endpoints)
    # Always False here. Only mock/demo.py flips it on the finished app object;
    # no setting or environment variable can (tests/test_demo.py).
    app.state.demo = False

    demo_router = APIRouter()

    @demo_router.get("/demo")
    def demo_flag() -> dict[str, bool]:
        """Public: lets the frontend show a "Demo mode" banner, also before login."""
        return {"demo": app.state.demo is True}

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        path = request.url.path
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif path.startswith(("/fonts/", "/img/")):
            response.headers.setdefault("Cache-Control", ASSET_CACHE)
        elif response.status_code in (200, 304) and path != settings.health_check_path:
            # No build step means no fingerprinted file names: always revalidate
            # (StaticFiles sends ETag/Last-Modified, so this costs a 304).
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # Cheap liveness probe: must never contact LDAP or croit.
    @app.get(settings.health_check_path, include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # JSON endpoints live under /api/... and are registered before the static
    # mount below, which serves the frontend at / and so must come last. Every
    # /api router is included into `api`, which enforces the CSRF header on
    # state-changing methods; never add a route to `app` under /api directly.
    api = APIRouter(prefix="/api", dependencies=[Depends(auth.csrf_protect)])
    api.include_router(auth.router)
    api.include_router(demo_router)
    api.include_router(rgw_user.router)
    api.include_router(credentials.router)
    api.include_router(profile.router)
    api.include_router(buckets.router)
    api.include_router(bucket_keys.router)
    api.include_router(bucket_keys.admin_router)
    api.include_router(audit_viewer.router)
    api.include_router(lifecycle.router)
    api.include_router(share_links.router)
    api.include_router(admin.router)
    api.include_router(admin_settings.router)
    app.include_router(api)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
