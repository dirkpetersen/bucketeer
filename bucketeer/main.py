import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .croit import CroitClient

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Settings, croit: CroitClient | None = None) -> FastAPI:
    """`croit` lets tests inject a client (e.g. on an ASGI transport)."""
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    client = croit or CroitClient(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.aclose()

    app = FastAPI(
        title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.croit = client  # one client per app, closed on shutdown

    # Cheap liveness probe: must never contact LDAP or croit.
    @app.get(settings.health_check_path, include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # JSON endpoints live under /api/... and are registered before this mount,
    # which serves the frontend at / and so must come last.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
