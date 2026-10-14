from fastapi.testclient import TestClient

from bucketeer.config import Settings
from bucketeer.main import create_app

from .conftest import REQUIRED


def test_health(settings):
    r = TestClient(create_app(settings)).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_path_configurable():
    s = Settings(_env_file=None, health_check_path="/up", **REQUIRED)  # no lifespan
    c = TestClient(create_app(s))
    assert c.get("/up").status_code == 200


def test_static_index_at_root(settings):
    r = TestClient(create_app(settings)).get("/")
    assert r.status_code == 200
    assert "<title>" in r.text


def test_unknown_path_is_404(settings):
    assert TestClient(create_app(settings)).get("/api/nope").status_code == 404
