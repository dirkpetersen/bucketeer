import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    acl,
    activity,
    admin,
    admin_cloud,
    admin_settings,
    alerts,
    audit_viewer,
    auth,
    backup,
    billing,
    billing_report,
    bucket_delete,
    bucket_facts,
    bucket_keys,
    bucket_quota,
    buckets,
    cloud,
    cloud_buckets,
    cloud_sharing,
    cloud_users,
    cors,
    credentials,
    ldapca,
    lifecycle,
    lifecycle_rules,
    location,
    mailer,
    multipart,
    notices,
    notifications,
    object_lock,
    offboarding,
    policy_editor,
    profile,
    rgw_user,
    s3client,
    share_links,
    sharing,
    subusers,
    tags,
)
from .config import Settings
from .croit import CroitClient
from .db import DB_FILENAME, Database
from .ldapauth import AdAuthenticator
from .rgw_admin import RgwAdminClient

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


def warn_renamed_env(settings: Settings) -> None:
    """Log a warning for environment variables that were renamed (Settings ignore them)."""
    if "AWS_PROFILE_NAME" in os.environ:
        logging.getLogger("bucketeer.config").warning(
            "AWS_PROFILE_NAME is no longer read; set S3_PROFILE_NAME instead (now %s)",
            settings.s3_profile_name)


def create_app(
    settings: Settings,
    croit: CroitClient | RgwAdminClient | None = None,
    db: Database | None = None,
    authenticator: AdAuthenticator | None = None,
    clock: Callable[[], float] = time.time,
    cloud_admin: "cloud.CloudAdmin | None | object" = None,
) -> FastAPI:
    """`croit` (the admin-plane client: `CroitClient`, or `RgwAdminClient` when
    `ADMIN_BACKEND=rgw`) lets tests inject a client (e.g. on an ASGI transport); `db` an
    already-open database (e.g. in-memory), which the caller then closes;
    `authenticator` a fake-LDAP one; `clock` drives session expiry and the
    login throttle; `cloud_admin` a cloud client (tests; default: one built from the
    CLOUD_* settings when cloud is configured, else none)."""
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(settings.log_level.upper())  # app.py may have set it up
    client = croit or (
        RgwAdminClient(settings) if settings.admin_backend == "rgw" else CroitClient(settings)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        ldapca.startup_report(settings)
        warn_renamed_env(settings)
        logging.getLogger("bucketeer.mailer").info("%s", _app.state.mailer.describe())
        owned = db is None
        if owned:
            # DATA_DIR is relative to the working directory (repo root).
            _app.state.db = Database(Path(settings.data_dir) / DB_FILENAME)
        cleanup = None
        if bucket_keys.CLEANUP_ON_START:  # background: never blocks startup or /health
            cleanup = asyncio.get_running_loop().create_task(
                bucket_keys.cleanup_pending(_app))
        resume = None
        if bucket_delete.RESUME_ON_START:  # unfinished delete jobs; never blocks startup
            resume = asyncio.get_running_loop().create_task(bucket_delete.resume(_app))
        sampler = None
        if billing.SAMPLE_ON_START:  # hourly bucket sizes for billing; never blocks startup
            sampler = asyncio.get_running_loop().create_task(billing.sample_loop(_app))
        reporter = None
        if billing.REPORT_ON_START:  # monthly chargeback email; never blocks startup
            reporter = asyncio.get_running_loop().create_task(billing_report.report_loop(_app))
        cloud_check = None
        cloud_alerts = None
        if _app.state.cloud is not None and cloud_buckets.ALERT_ON_START:  # daily, background
            cloud_alerts = asyncio.get_running_loop().create_task(cloud_buckets.alert_loop(_app))
        if _app.state.cloud is not None and cloud.CHECK_ON_START:  # never blocks startup
            cloud_check = asyncio.get_running_loop().create_task(
                _app.state.cloud.monitor_loop())
        if not settings.backup_bucket:
            logging.getLogger("bucketeer.backup").warning("%s", _app.state.backup.describe_off())
        backups = None
        if backup.SCHEDULE_ON_START:  # nightly backup; never blocks startup or /health
            backups = asyncio.get_running_loop().create_task(_app.state.backup.schedule_loop())
        unlimit = None
        if bucket_quota.UNLIMIT_ON_START:  # one-time: unlimited user quotas; in background
            unlimit = asyncio.get_running_loop().create_task(
                bucket_quota.unlimit_on_start(_app))
        notice = None
        if notices.NOTICES_ON_START:  # daily near-full and key age emails; in background
            notice = asyncio.get_running_loop().create_task(notices.notice_loop(_app))
        schedule = None
        if offboarding.SCHEDULE_ON_START:  # background: never blocks startup or /health
            schedule = asyncio.get_running_loop().create_task(
                _app.state.offboarding.schedule_loop())
        try:
            yield
        finally:
            for task in (cloud_check, cloud_alerts, sampler, reporter, unlimit, notice):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if schedule is not None:
                schedule.cancel()
                await asyncio.gather(schedule, return_exceptions=True)
            if backups is not None:
                backups.cancel()
                await asyncio.gather(backups, return_exceptions=True)
            await _app.state.backup.aclose()
            await _app.state.offboarding.aclose()
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
            if resume is not None:
                resume.cancel()
                await asyncio.gather(resume, return_exceptions=True)
            await bucket_delete.shutdown(_app)
            await _app.state.rgw.aclose()
            await client.aclose()
            if owned:
                _app.state.db.close()

    app = FastAPI(
        title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan
    )

    @app.exception_handler(profile.BudgetMissing)
    async def _budget_missing(_request, _exc):
        return JSONResponse(status_code=409, content={
            "detail": profile.BUDGET_MISSING_TEXT, "budgetMissing": True})
    app.state.settings = settings
    app.state.clock = clock
    credentials.install_access_log_filter()
    app.state.croit = client  # one client per app, closed on shutdown
    # Cloud (AWS) admin client, None when the CLOUD_* settings are not set (cloud.py).
    app.state.cloud = (cloud_admin if cloud_admin is not None
                       else cloud.CloudAdmin(settings) if settings.cloud_configured else None)
    if app.state.cloud is cloud.NO_CLOUD:
        app.state.cloud = None
    app.state.db = db  # else opened in the lifespan; /health never touches it

    app.state.authenticator = authenticator or AdAuthenticator(settings)
    app.state.sessions = auth.SessionManager(settings, clock)
    app.state.throttle = auth.LoginThrottle(clock)
    app.state.rgw = rgw_user.RgwResolver(client, lambda: app.state.db, settings.s3_tenant)

    app.state.offboarding = offboarding.OffboardingService(app)
    app.state.rgw.on_login = app.state.offboarding.on_login
    app.state.backup = backup.BackupService(app)

    app.state.endpoints = credentials.EndpointResolver(client, settings, clock)
    app.state.mailer = mailer.Mailer(settings)
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
    api.include_router(cloud_users.router)
    api.include_router(cloud_buckets.router)
    api.include_router(cloud_sharing.router)
    api.include_router(subusers.router)
    api.include_router(profile.router)
    api.include_router(activity.router)
    api.include_router(bucket_delete.router)  # before /buckets/{name}
    api.include_router(buckets.router)
    api.include_router(bucket_keys.router)
    api.include_router(bucket_keys.admin_router)
    api.include_router(bucket_facts.admin_router)
    api.include_router(audit_viewer.router)
    api.include_router(lifecycle.router)
    api.include_router(lifecycle_rules.router)
    api.include_router(policy_editor.router)
    api.include_router(cors.router)
    api.include_router(notifications.router)
    api.include_router(tags.router)
    api.include_router(object_lock.router)
    api.include_router(share_links.router)
    api.include_router(multipart.router)
    api.include_router(acl.router)
    api.include_router(sharing.router)
    api.include_router(admin.router)
    api.include_router(admin_cloud.router)
    api.include_router(location.router)  # /admin/settings/location
    api.include_router(notices.router)  # /admin/settings/notices
    api.include_router(admin_settings.router)
    api.include_router(offboarding.router)
    api.include_router(billing.router)
    api.include_router(billing_report.router)
    api.include_router(backup.router)
    api.include_router(alerts.router)
    app.include_router(api)
    app.include_router(credentials.setup_router)  # POST /setup/{token}: the token is the credential

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
