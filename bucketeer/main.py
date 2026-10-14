import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import Settings

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Settings) -> FastAPI:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None)
    app.state.settings = settings

    # Cheap liveness probe: must never contact LDAP or croit.
    @app.get(settings.health_check_path, include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # JSON endpoints live under /api/... and are registered before this mount,
    # which serves the frontend at / and so must come last.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
