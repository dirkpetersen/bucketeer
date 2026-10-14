"""Key create / rotate / delete against the in-process mock croit."""

import logging
import re

import httpx
import pytest

from bucketeer.credentials import MAX_KEYS, generate_key_pair

from .test_credentials import ALICE, BOB, Env, H


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    yield e
    e.db.close()


def alice_keys(env):
    return {k["access_key"]: k["secret_key"] for k in env.mock.state.croit.users["alice"].keys}


def key_audit(env):
    return [a for a in env.db.audit.query(limit=1000) if a["action"].startswith("credentials.key")]


def key_calls(env):
    return [c for c in env.transport.calls if "/keys" in c[1] and c[0] in ("PUT", "DELETE")]


def test_generated_format():
    for _ in range(50):
        a, s = generate_key_pair()
        assert re.fullmatch(r"[A-Z0-9]{20}", a) and re.fullmatch(r"[A-Za-z0-9]{40}", s)


def test_create_returns_only_access_key_and_audits(env):
    r = env.alice.post("/api/credentials/keys", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert list(body) == ["accessKey"]
    keys = alice_keys(env)
    assert body["accessKey"] in keys and ALICE[2] in keys
    assert keys[body["accessKey"]] not in r.text
    (entry,) = key_audit(env)
    assert entry["action"] == "credentials.key.create" and entry["target"] == body["accessKey"]
    assert all(secret not in str(env.db.audit.query(limit=1000)) for secret in keys.values())
    # the new secret is available through the audited reveal
    rv = env.alice.post("/api/credentials/reveal", json={"accessKey": body["accessKey"]}, headers=H)
    assert rv.json()["secretKey"] == keys[body["accessKey"]]


def test_create_limit(env):
    for _ in range(MAX_KEYS - 1):
        assert env.alice.post("/api/credentials/keys", headers=H).status_code == 200
    r = env.alice.post("/api/credentials/keys", headers=H)
    assert r.status_code == 409 and "at most" in r.json()["detail"]
    assert len(alice_keys(env)) == MAX_KEYS


def test_create_retries_on_other_users_key(env, monkeypatch):
    import bucketeer.credentials as c

    seq = iter([(BOB[2], "S" * 40), ("A" * 20, "T" * 40)])
    monkeypatch.setattr(c, "generate_key_pair", lambda: next(seq))
    r = env.alice.post("/api/credentials/keys", headers=H)
    assert r.status_code == 200 and r.json()["accessKey"] == "A" * 20
    assert BOB[2] not in alice_keys(env)
    assert env.mock.state.croit.access_keys[BOB[2]] == "bob"  # bob's key untouched


def test_rotate_order_new_exists_before_old_removed(env):
    old = ALICE[2]
    r = env.alice.post(f"/api/credentials/keys/{old}/rotate", headers=H)
    assert r.status_code == 200
    new = r.json()["accessKey"]
    assert r.json()["oldKeyRemoved"] is True and new != old
    assert set(alice_keys(env)) == {new}
    calls = [(m, p.split("?")[0].split("/keys")[-1]) for m, p in env.transport.calls
             if m in ("PUT", "DELETE", "GET") and "/users/alice/keys" in p]
    seq = calls
    put = next(i for i, c in enumerate(seq) if c == ("PUT", f"/{new}"))
    delete = next(i for i, c in enumerate(seq) if c == ("DELETE", f"/{old}"))
    gets_between = [c for c in seq[put + 1:delete] if c[0] == "GET"]
    assert put < delete and gets_between  # confirmed via a list between put and delete
    (entry,) = key_audit(env)
    assert entry["action"] == "credentials.key.rotate"
    assert entry["details"]["old_key_removed"] is True


def test_rotate_partial_failure_keeps_both(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"message": "boom"}) if req.method == "DELETE" else None)
    r = env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["oldKeyRemoved"] is False and "old key" in body["message"]
    assert set(alice_keys(env)) == {ALICE[2], body["accessKey"]}
    assert key_audit(env)[0]["details"]["old_key_removed"] is False


def test_rotate_put_failure_leaves_old_key(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"message": "boom"}) if req.method == "PUT" else None)
    r = env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H)
    assert r.status_code == 502
    assert set(alice_keys(env)) == {ALICE[2]}
    assert not [c for c in env.transport.calls if c[0] == "DELETE"]


def test_rotate_unconfirmed_new_key_keeps_old(env):
    n = {"puts": 0}

    def fail(req):
        if req.method == "PUT":
            n["puts"] += 1
            return httpx.Response(204)  # croit says ok but nothing was stored
        return None

    env.transport.fail = fail
    r = env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H)
    assert r.status_code == 502
    assert set(alice_keys(env)) == {ALICE[2]}
    assert not [c for c in env.transport.calls if c[0] == "DELETE"]


def test_delete_own_key(env):
    new = env.alice.post("/api/credentials/keys", headers=H).json()["accessKey"]
    r = env.alice.delete(f"/api/credentials/keys/{ALICE[2]}", headers=H)
    assert r.status_code == 200 and r.json() == {"accessKey": ALICE[2]}
    assert set(alice_keys(env)) == {new}
    assert key_audit(env)[0]["action"] == "credentials.key.delete"


def test_refuse_deleting_last_key(env):
    r = env.alice.delete(f"/api/credentials/keys/{ALICE[2]}", headers=H)
    assert r.status_code == 409 and "Rotate" in r.json()["detail"]
    assert set(alice_keys(env)) == {ALICE[2]}


def test_other_users_and_subuser_keys_are_404(env):
    croit = env.mock.state.croit
    user = croit.users["alice"]
    user.keys.append({"access_key": "SUBKEY0000000000000X", "secret_key": "x" * 40,
                      "user": "alice:sub"})
    croit.access_keys["SUBKEY0000000000000X"] = "alice"
    env.alice.post("/api/credentials/keys", headers=H)  # so delete would otherwise be allowed
    for key in (BOB[2], "SUBKEY0000000000000X", ALICE[2][:-1]):
        assert env.alice.post(f"/api/credentials/keys/{key}/rotate", headers=H).status_code == 404
        assert env.alice.delete(f"/api/credentials/keys/{key}", headers=H).status_code == 404
    assert "bob" in croit.access_keys[BOB[2]]
    assert BOB[2] in {k["access_key"] for k in croit.users["bob"].keys}


def test_suspended_user_refused(env):
    env.mock.state.croit.users["alice"].suspended = True
    assert env.alice.post("/api/credentials/keys", headers=H).status_code == 403
    assert env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H).status_code == 403
    assert env.alice.delete(f"/api/credentials/keys/{ALICE[2]}", headers=H).status_code == 403
    assert not key_calls(env)


def test_csrf_and_unauthenticated(env):
    assert env.alice.post("/api/credentials/keys").status_code == 403
    assert env.alice.delete(f"/api/credentials/keys/{ALICE[2]}").status_code == 403
    anon = env.anon()
    assert anon.post("/api/credentials/keys", headers=H).status_code == 401
    assert anon.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H).status_code == 401
    assert anon.delete(f"/api/credentials/keys/{ALICE[2]}", headers=H).status_code == 401
    assert not key_calls(env) and set(alice_keys(env)) == {ALICE[2]}


def test_put_key_failure_never_leaks_secret(env, caplog):
    seen = []

    def fail(req):
        if req.method == "PUT":
            seen.append(req.url.params.get("secretKey"))
            # worst case: croit echoes the request line, query included
            return httpx.Response(500, json={"message": f"failed {req.url}",
                                             "details": f"secretKey={req.url.params['secretKey']}"})
        return None

    env.transport.fail = fail
    caplog.set_level(logging.DEBUG)
    r = env.alice.post("/api/credentials/keys", headers=H)
    assert r.status_code in (400, 502) and seen
    secret = seen[0]
    assert len(secret) == 40
    assert secret not in r.text and secret not in caplog.text
    assert secret not in str(env.db.audit.query(limit=1000))
    # connection failure path too
    env.transport.fail = lambda req: (
        httpx.ConnectError(f"down {req.url}") if req.method == "PUT" else None)
    seen.clear()
    caplog.clear()
    r = env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H)
    assert r.status_code == 503
    assert not re.search(r"secretKey=", r.text + caplog.text)


def test_never_whole_set_put_and_race_lock(env):
    import asyncio

    env.alice.post("/api/credentials/keys", headers=H)
    env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H)
    assert not [c for c in env.transport.calls
                if c[0] == "PUT" and c[1].rstrip("/").endswith("/users/alice/keys")]
    from bucketeer.credentials import KeyLocks

    locks = KeyLocks()
    assert locks("alice") is locks("alice") and locks("alice") is not locks("bob")
    assert isinstance(locks("alice"), asyncio.Lock)


def test_rotate_confirmation_read_fails_is_audited_and_keeps_old(env):
    gets = {"n": 0}

    def fail(req):
        if req.method == "GET" and req.url.path.endswith("/users/alice/keys"):
            gets["n"] += 1
            if gets["n"] == 2:
                return httpx.Response(500, json={"message": "boom"})
        return None

    env.transport.fail = fail
    r = env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H)
    assert r.status_code == 502
    assert not [c for c in env.transport.calls if c[0] == "DELETE"]
    assert len(alice_keys(env)) == 2  # old kept, new exists
    (entry,) = key_audit(env)
    assert entry["action"] == "credentials.key.rotate_failed"
    assert entry["details"]["old_key_removed"] is False and entry["details"]["confirmed"] is None
    assert entry["details"]["newKey"] in alice_keys(env)


def test_rotate_refused_above_limit_but_allowed_at_limit(env):
    for _ in range(MAX_KEYS - 1):
        env.alice.post("/api/credentials/keys", headers=H)
    r = env.alice.post(f"/api/credentials/keys/{ALICE[2]}/rotate", headers=H)
    assert r.status_code == 200 and len(alice_keys(env)) == MAX_KEYS
    env.mock.state.croit.put_key("alice", "EXTRAKEY000000000006", "x" * 40)
    assert len(alice_keys(env)) == MAX_KEYS + 1
    before = set(alice_keys(env))
    victim = sorted(before)[0]
    r = env.alice.post(f"/api/credentials/keys/{victim}/rotate", headers=H)
    assert r.status_code == 409 and set(alice_keys(env)) == before


def test_put_key_error_body_scrubbed_before_truncation(env, caplog):
    seen = []

    def fail(req):
        if req.method == "PUT":
            secret = req.url.params["secretKey"]
            seen.append(secret)
            # non-JSON body; the secret straddles character 200
            return httpx.Response(502, text="x" * 180 + secret + "tail")
        return None

    env.transport.fail = fail
    r = env.alice.post("/api/credentials/keys", headers=H)
    assert seen and r.status_code == 502
    secret = seen[0]
    blob = r.text + caplog.text + str(env.db.audit.query(limit=1000))
    assert secret[:10] not in blob and secret[-10:] not in blob
