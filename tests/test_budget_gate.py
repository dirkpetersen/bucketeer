"""No new bucket or credential before a default budget number is saved (admins exempt)."""

import pytest

from bucketeer import profile

from .test_credentials import ALICE, Env, H

CREATES = [
    ("post", "/api/buckets", {"name": "alice-new", "budgetNumber": "DN099999"}),
    ("post", "/api/credentials/keys", None),
    ("post", "/api/credentials/setup-link", {"os": "linux"}),
    ("post", "/api/subusers", {"name": "job", "permission": "read"}),
    ("post", "/api/subusers/job/keys", None),
    ("post", "/api/buckets/alice-data/keys", {"label": "job", "access": "read"}),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "REQUIRE_DEFAULT_BUDGET", True)
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    yield e
    e.db.close()


@pytest.mark.parametrize("method,path,body", CREATES)
def test_creates_refused_until_a_default_is_saved(env, method, path, body):
    r = getattr(env.alice, method)(path, json=body, headers=H)
    assert r.status_code == 409
    assert r.json() == {"detail": profile.BUDGET_MISSING_TEXT, "budgetMissing": True}
    env.alice.put("/api/profile", json={"defaultBudgetNumber": "DN099999"}, headers=H)
    r = getattr(env.alice, method)(path, json=body, headers=H)
    assert not r.json().get("budgetMissing")  # past the gate (the route decides the rest)


def test_reading_and_reveal_stay_open(env):
    assert env.alice.get("/api/buckets").status_code == 200
    assert env.alice.get("/api/credentials").status_code == 200
    r = env.alice.post("/api/credentials/reveal", json={"accessKey": ALICE[2]}, headers=H)
    assert r.status_code == 200 and r.json()["secretKey"] == ALICE[3]


def test_admins_are_exempt(env):
    bob = env.session("bob")
    assert bob.get("/api/me").json()["isAdmin"] is True
    r = bob.post("/api/credentials/keys", headers=H)
    assert not (r.json() or {}).get("budgetMissing")
