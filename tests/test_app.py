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


def test_renamed_profile_variable_warns(settings, monkeypatch, caplog):
    monkeypatch.setenv("AWS_PROFILE_NAME", "old-profile")
    with caplog.at_level("WARNING", logger="bucketeer.config"), TestClient(create_app(settings)):
        pass
    msgs = [r.getMessage() for r in caplog.records if r.name == "bucketeer.config"]
    assert any("AWS_PROFILE_NAME is no longer read; set S3_PROFILE_NAME instead (now "
               f"{settings.s3_profile_name})" in m for m in msgs)


def test_no_warning_without_old_variable(settings, monkeypatch, caplog):
    monkeypatch.delenv("AWS_PROFILE_NAME", raising=False)
    with caplog.at_level("WARNING", logger="bucketeer.config"), TestClient(create_app(settings)):
        pass
    assert not [r for r in caplog.records if "AWS_PROFILE_NAME" in r.getMessage()]
