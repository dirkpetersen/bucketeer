"""Object lock (default retention, per-object retention and legal hold) against mock croit and
moto. moto enforces no identities and no governance bypass, so the S3 calls Bucketeer makes are
recorded and asserted (BypassGovernanceRetention only when needed and confirmed)."""

from datetime import UTC, datetime, timedelta

import pytest

from .test_buckets import ALICE_KEY, Env, H, create

B = "alice-lock"
KEY = "data/report.txt"


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


class Spy:
    """Wraps a boto3 client and records the arguments of the lock writes."""

    def __init__(self, inner, calls):
        self._inner, self._calls = inner, calls

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not name.startswith(("put_object_", "put_bucket_")):
            return attr

        def call(**kw):
            self._calls.append((name, kw))
            return attr(**kw)
        return call


@pytest.fixture
def calls(env):
    rec = []
    real = env.app.state.s3.factory
    env.app.state.s3.factory = lambda *a: Spy(real(*a), rec)
    return rec


@pytest.fixture
def lock(env):
    """alice-lock: an object-lock bucket with one object, created through the API."""
    assert create(env.alice, name=B, objectLock=True).status_code == 201
    env.moto().put_object(Bucket=B, Key=KEY, Body=b"x")
    env.used.clear()
    return B


def now():
    return datetime.now(UTC).replace(microsecond=0)


def iso(days):
    return (now() + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def dget(env, name=B):
    return env.alice.get(f"/api/buckets/{name}/object-lock")


def dput(env, default, etag=None, name=B, headers=H, **kw):
    if etag is None:
        etag = dget(env, name).json()["etag"]
    return env.alice.put(f"/api/buckets/{name}/object-lock", headers=headers,
                         json={"defaultRetention": default, "etag": etag, **kw})


def oget(env, name=B, key=KEY, **params):
    return env.alice.get(f"/api/buckets/{name}/objects/lock", params={"key": key, **params})


def rput(env, body, name=B, key=KEY, headers=H, **params):
    return env.alice.put(f"/api/buckets/{name}/objects/retention", params={"key": key, **params},
                         json=body, headers=headers)


def hput(env, status, name=B, key=KEY, headers=H, **params):
    return env.alice.put(f"/api/buckets/{name}/objects/legal-hold",
                         params={"key": key, **params}, json={"status": status}, headers=headers)


def moto_head(env, key=KEY):
    h = env.moto().head_object(Bucket=B, Key=key)
    return h.get("ObjectLockMode"), h.get("ObjectLockRetainUntilDate"), \
        h.get("ObjectLockLegalHoldStatus")


# -- default retention --

def test_get_on_lock_and_plain_bucket(env, lock):
    j = dget(env).json()
    assert j["enabled"] is True and j["defaultRetention"] is None and j["etag"]
    assert dget(env).headers["cache-control"] == "no-store"
    p = dget(env, "alice-data").json()
    assert p["enabled"] is False and p["defaultRetention"] is None
    assert set(env.used) == {ALICE_KEY}


def test_set_and_clear_default(env):
    assert create(env.alice, name=B, objectLock=True).status_code == 201
    r = dput(env, {"mode": "GOVERNANCE", "days": 30})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    assert r.json()["defaultRetention"] == {"mode": "GOVERNANCE", "days": 30}
    cfg = env.moto().get_object_lock_configuration(Bucket=B)["ObjectLockConfiguration"]
    assert cfg["Rule"]["DefaultRetention"]["Mode"] == "GOVERNANCE"
    assert dget(env).json()["defaultRetention"] == {"mode": "GOVERNANCE", "days": 30}
    assert dput(env, {"mode": "GOVERNANCE", "days": 30}).json()["changed"] is False
    r = dput(env, {"mode": "GOVERNANCE", "years": 2})
    assert r.json()["defaultRetention"] == {"mode": "GOVERNANCE", "years": 2}
    r = dput(env, None)
    assert r.status_code == 200 and r.json()["defaultRetention"] is None
    cfg = env.moto().get_object_lock_configuration(Bucket=B)["ObjectLockConfiguration"]
    assert cfg["ObjectLockEnabled"] == "Enabled" and "Rule" not in cfg
    a = env.audit("bucket.object_lock")
    assert [x["details"]["after"] for x in a][::-1] == [
        {"mode": "GOVERNANCE", "days": 30}, {"mode": "GOVERNANCE", "years": 2}, None]
    assert a[2]["details"]["before"] is None and a[0]["target"] == B
    assert set(env.used) == {ALICE_KEY}


def test_compliance_default_needs_confirm(env, calls):
    create(env.alice, name=B, objectLock=True)
    n = len(calls)
    r = dput(env, {"mode": "COMPLIANCE", "days": 7})
    assert r.status_code == 409 and r.json()["requiresConfirmation"] is True
    assert "administrators" in r.json()["detail"]
    assert len(calls) == n and env.audit("bucket.object_lock") == []
    r = dput(env, {"mode": "COMPLIANCE", "days": 7}, confirm=True)
    assert r.status_code == 200 and r.json()["defaultRetention"]["mode"] == "COMPLIANCE"
    # unchanged compliance default is a no-op without confirm; leaving compliance needs none
    assert dput(env, {"mode": "COMPLIANCE", "days": 7}).json()["changed"] is False
    assert dput(env, {"mode": "GOVERNANCE", "days": 7}).status_code == 200


@pytest.mark.parametrize("bad", [
    {"mode": "GOVERNANCE"}, {"mode": "GOVERNANCE", "days": 1, "years": 1},
    {"mode": "GOVERNANCE", "days": 0}, {"mode": "GOVERNANCE", "days": 36501},
    {"mode": "GOVERNANCE", "years": 101}, {"mode": "GOVERNANCE", "years": 0},
    {"mode": "GOVERNANCE", "days": 1.5}, {"mode": "GOVERNANCE", "days": True},
    {"mode": "OTHER", "days": 1}, {"days": 1}, {"mode": "GOVERNANCE", "days": 1, "x": 1},
])
def test_default_bounds(env, bad):
    create(env.alice, name=B, objectLock=True)
    assert dput(env, bad, confirm=True).status_code == 422
    assert env.audit("bucket.object_lock") == []


def test_default_bounds_edges_ok(env):
    create(env.alice, name=B, objectLock=True)
    assert dput(env, {"mode": "GOVERNANCE", "days": 36500}).status_code == 200
    assert dput(env, {"mode": "GOVERNANCE", "years": 100}).status_code == 200
    assert dput(env, {"mode": "GOVERNANCE", "days": 1}).status_code == 200


def test_default_etag_conflict(env):
    create(env.alice, name=B, objectLock=True)
    old = dget(env).json()["etag"]
    assert dput(env, {"mode": "GOVERNANCE", "days": 5}, old).status_code == 200
    r = dput(env, {"mode": "GOVERNANCE", "days": 9}, old)
    assert r.status_code == 409 and r.json()["etagMismatch"] is True
    assert r.json()["current"]["defaultRetention"] == {"mode": "GOVERNANCE", "days": 5}


def test_default_on_plain_bucket_is_409(env):
    r = dput(env, {"mode": "GOVERNANCE", "days": 5}, etag="x", name="alice-data")
    assert r.status_code == 409 and "can't be added later" in r.json()["detail"]
    assert env.audit("bucket.object_lock") == []


# -- one object --

def test_object_state_and_retention_roundtrip(env, lock, calls):
    j = oget(env).json()
    assert j["retention"] is None and j["legalHold"] == "OFF" and j["key"] == KEY
    until = iso(10)
    r = rput(env, {"mode": "GOVERNANCE", "retainUntil": until})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    assert r.json()["retention"] == {"mode": "GOVERNANCE", "retainUntil": until}
    mode, when, _ = moto_head(env)
    assert mode == "GOVERNANCE" and when.replace(microsecond=0).isoformat().startswith(until[:19])
    assert oget(env).json()["retention"]["retainUntil"] == until
    puts = [kw for n, kw in calls if n == "put_object_retention"]
    assert len(puts) == 1 and "BypassGovernanceRetention" not in puts[0]
    # extending is fine and needs no bypass
    later = iso(20)
    r = rput(env, {"mode": "GOVERNANCE", "retainUntil": later})
    assert r.status_code == 200 and r.json()["retention"]["retainUntil"] == later
    assert all("BypassGovernanceRetention" not in kw for n, kw in calls
               if n == "put_object_retention")
    # identical request: no write, no audit
    n = len(calls)
    assert rput(env, {"mode": "GOVERNANCE", "retainUntil": later}).json()["changed"] is False
    assert len(calls) == n
    a = env.audit("object.retention")
    assert len(a) == 2 and a[0]["target"] == B and a[0]["details"]["key"] == KEY
    assert a[0]["details"]["before"]["retainUntil"] == until
    assert a[0]["details"]["bypassGovernance"] is False
    assert set(env.used) == {ALICE_KEY}


def test_shorten_or_remove_governance_needs_bypass(env, lock, calls):
    rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(20)})
    calls.clear()
    short = {"mode": "GOVERNANCE", "retainUntil": iso(5)}
    r = rput(env, short)
    assert r.status_code == 409 and r.json()["requiresBypass"] is True
    assert "requiresConfirmation" not in r.json() and calls == []
    r = rput(env, {"mode": None})
    assert r.status_code == 409 and r.json()["requiresBypass"] is True and calls == []
    r = rput(env, {**short, "bypassGovernance": True})
    assert r.status_code == 200 and r.json()["retention"]["retainUntil"] == short["retainUntil"]
    (name, kw), = [c for c in calls if c[0] == "put_object_retention"]
    assert kw["BypassGovernanceRetention"] is True
    r = rput(env, {"mode": None, "bypassGovernance": True})
    assert r.status_code == 200 and r.json()["retention"] is None
    assert moto_head(env)[0] is None
    a = env.audit("object.retention")
    assert a[0]["details"]["after"] is None and a[0]["details"]["bypassGovernance"] is True
    assert a[1]["details"]["bypassGovernance"] is True


def test_bypass_is_not_sent_unless_needed(env, lock, calls):
    r = rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(5), "bypassGovernance": True})
    assert r.status_code == 200
    r = rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(9), "bypassGovernance": True})
    assert r.status_code == 200
    assert all("BypassGovernanceRetention" not in kw for n, kw in calls
               if n == "put_object_retention")
    assert [a["details"]["bypassGovernance"] for a in env.audit("object.retention")] == [
        False, False]


def test_compliance_needs_confirm_then_cannot_weaken(env, lock, calls):
    until = iso(10)
    body = {"mode": "COMPLIANCE", "retainUntil": until}
    r = rput(env, body)
    assert r.status_code == 409 and r.json()["requiresConfirmation"] is True
    assert "bypassGovernance" not in str(r.json()) and moto_head(env)[0] is None
    assert rput(env, {**body, "confirm": True}).status_code == 200
    assert moto_head(env)[0] == "COMPLIANCE"
    calls.clear()
    # shorter, removed, or turned into governance: refused, nothing sent, even with every flag
    for weaker in ({"mode": "COMPLIANCE", "retainUntil": iso(3)}, {"mode": None},
                   {"mode": "GOVERNANCE", "retainUntil": iso(30)}):
        r = rput(env, {**weaker, "confirm": True, "bypassGovernance": True})
        assert r.status_code == 409 and r.json()["locked"] is True, weaker
        assert "Compliance" in r.json()["detail"]
    assert calls == []
    # lengthening needs the confirm again but no bypass
    ext = {"mode": "COMPLIANCE", "retainUntil": iso(40)}
    assert rput(env, ext).status_code == 409
    assert rput(env, {**ext, "confirm": True}).json()["retention"]["retainUntil"] == iso(40)
    assert all("BypassGovernanceRetention" not in kw for n, kw in calls)


def test_governance_to_compliance_later_needs_confirm_only(env, lock, calls):
    rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(10)})
    r = rput(env, {"mode": "COMPLIANCE", "retainUntil": iso(15)})
    assert r.status_code == 409 and r.json() == {
        "detail": r.json()["detail"], "requiresConfirmation": True}
    r = rput(env, {"mode": "COMPLIANCE", "retainUntil": iso(15), "confirm": True})
    assert r.status_code == 200 and moto_head(env)[0] == "COMPLIANCE"
    assert all("BypassGovernanceRetention" not in kw for n, kw in calls)


def test_retention_date_validation(env, lock):
    for body in ({"mode": "GOVERNANCE", "retainUntil": iso(-1)},
                 {"mode": "GOVERNANCE", "retainUntil": iso(36501)},
                 {"mode": "GOVERNANCE", "retainUntil": "tomorrow"},
                 {"mode": "GOVERNANCE", "retainUntil": None},
                 {"mode": "GOVERNANCE"},
                 {"mode": None, "retainUntil": iso(3)},
                 {"mode": "OTHER", "retainUntil": iso(3)},
                 {"mode": "GOVERNANCE", "retainUntil": iso(3), "extra": 1}):
        assert rput(env, body).status_code == 422, body
    assert rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(36499)}).status_code == 200


def test_naive_date_is_utc(env, lock):
    naive = (now() + timedelta(days=3)).replace(tzinfo=None).isoformat()
    r = rput(env, {"mode": "GOVERNANCE", "retainUntil": naive})
    assert r.status_code == 200 and r.json()["retention"]["retainUntil"] == iso(3)


def test_legal_hold_on_off(env, lock):
    r = hput(env, "ON")
    assert r.status_code == 200 and r.json()["legalHold"] == "ON" and r.json()["changed"]
    assert moto_head(env)[2] == "ON"
    assert oget(env).json()["legalHold"] == "ON"
    assert hput(env, "ON").json()["changed"] is False
    assert hput(env, "OFF").json()["legalHold"] == "OFF"
    assert moto_head(env)[2] == "OFF"
    a = env.audit("object.legal_hold")
    assert [(x["details"]["before"], x["details"]["after"]) for x in a][::-1] == [
        ("OFF", "ON"), ("ON", "OFF")]
    assert a[0]["details"]["key"] == KEY and set(env.used) == {ALICE_KEY}
    assert hput(env, "MAYBE").status_code == 422


def test_version_id(env, lock):
    vid = env.moto().put_object(Bucket=B, Key=KEY, Body=b"y")["VersionId"]
    r = rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(5)}, versionId=vid)
    assert r.status_code == 200 and r.json()["versionId"] == vid
    assert oget(env, versionId=vid).json()["retention"] is not None
    assert env.audit("object.retention")[0]["details"]["versionId"] == vid
    assert oget(env, versionId="nope").status_code == 404
    assert hput(env, "ON", versionId="nope").status_code == 404


def test_missing_key_is_404(env, lock):
    assert oget(env, key="nope.txt").status_code == 404
    assert rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(5)}, key="nope.txt"
                ).status_code == 404
    assert hput(env, "ON", key="nope.txt").status_code == 404
    assert oget(env, key="").status_code == 422
    assert oget(env, key="a\nb").status_code == 422
    assert env.audit("object.") == []


def test_object_ops_on_plain_bucket_are_409(env):
    env.moto().put_object(Bucket="alice-data", Key=KEY, Body=b"x")
    assert oget(env, "alice-data").status_code == 409
    assert rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(5)}, "alice-data"
                ).status_code == 409
    assert hput(env, "ON", "alice-data").status_code == 409


def test_other_users_bucket_404(env, lock):
    assert dget(env, "bob-shared").status_code == 404
    assert dput(env, None, "x", "bob-shared").status_code == 404
    assert oget(env, "bob-shared").status_code == 404
    assert rput(env, {"mode": None}, "bob-shared").status_code == 404
    assert hput(env, "ON", "bob-shared").status_code == 404
    assert dget(env, "Bad:Name").status_code == 404
    assert env.audit("object.") == [] and env.audit("bucket.object_lock") == []


def test_suspended_refused(env, lock):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert dget(env).status_code == 403
    assert oget(env).status_code == 403
    assert rput(env, {"mode": None}).status_code == 403
    assert hput(env, "ON").status_code == 403
    assert moto_head(env)[2] is None


def test_csrf_and_anonymous(env, lock):
    assert dput(env, None, headers={}).status_code == 403
    assert rput(env, {"mode": None}, headers={}).status_code == 403
    assert hput(env, "ON", headers={}).status_code == 403
    assert env.anon().get(f"/api/buckets/{B}/object-lock").status_code in (401, 403)
    assert env.anon().get(f"/api/buckets/{B}/objects/lock", params={"key": KEY}
                          ).status_code in (401, 403)
    assert moto_head(env)[2] is None


def test_put_carries_the_version_the_head_returned(env, lock, calls):
    vid = env.moto().put_object(Bucket=B, Key=KEY, Body=b"y")["VersionId"]
    assert rput(env, {"mode": "GOVERNANCE", "retainUntil": iso(5)}).status_code == 200
    assert hput(env, "ON").status_code == 200
    puts = [kw for n, kw in calls if n in ("put_object_retention", "put_object_legal_hold")]
    assert len(puts) == 2 and all(kw["VersionId"] == vid for kw in puts)


def test_date_that_overflows_utc_is_422(env, lock):
    r = rput(env, {"mode": "GOVERNANCE", "retainUntil": "9999-12-31T23:00:00-05:00"})
    assert r.status_code == 422


def test_invalid_request_on_a_lock_bucket_is_not_reported_as_no_lock():
    from botocore.exceptions import ClientError

    from bucketeer.object_lock import _s3_error
    e = _s3_error(ClientError({"Error": {"Code": "InvalidRequest", "Message": "x"}}, "Put"))
    assert e.status_code == 409 and "without object lock" not in e.detail
    assert "InvalidRequest" in e.detail
