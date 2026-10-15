"""Subusers and their keys (Q15) on both admin backends, against the in-process mocks."""

import asyncio

import httpx
import pytest

from bucketeer.subusers import MAX_KEYS, MAX_SUBUSERS

from .backends import (
    admin_backend,  # noqa: F401  (fixture: both admin backends)
    on_rgw,
)
from .test_credentials import ALICE, BOB, Env, H

SUB = "/api/subusers"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    yield e
    e.db.close()


def alice(env):
    return env.mock.state.croit.users["alice"]


def sub_keys(env, name, uid="alice"):
    return {k["access_key"]: k["secret_key"]
            for k in env.mock.state.croit.users[uid].keys if k.get("subuser") == name}


def audit(env, prefix="subuser."):
    return [a for a in env.db.audit.query(limit=1000) if a["action"].startswith(prefix)]


def make(env, name="ci", permission="read", **extra):
    r = env.alice.post(SUB, json={"name": name, "permission": permission, **extra}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def every_secret(env) -> set[str]:
    return {k["secret_key"] for u in env.mock.state.croit.users.values() for k in u.keys}


# -- list and create --------------------------------------------------------------------


def test_list_empty(env):
    r = env.alice.get(SUB)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "subusers": [], "permissions": ["read", "write", "read-write", "full-control"],
        "limits": {"maxSubusers": MAX_SUBUSERS, "maxKeys": MAX_KEYS}}


def test_create_makes_exactly_one_key_and_audits(env):
    body = make(env, "ci", "read-write")
    assert body["name"] == "ci" and body["permission"] == "read-write"
    (key,) = body["keys"]
    keys = sub_keys(env, "ci")
    assert set(keys) == {key["accessKey"]} and len(keys) == 1
    assert alice(env).subusers == {"ci": "read-write"}
    assert set(env.mock.state.croit.users["alice"].keys[i]["access_key"] for i in range(2)) \
        >= {key["accessKey"], ALICE[2]}
    assert all(secret not in str(body) for secret in keys.values())
    assert env.db.subusers.used("alice") == ["ci"]
    listed = env.alice.get(SUB).json()["subusers"]
    assert listed == [{"name": "ci", "permission": "read-write",
                       "keys": [{"accessKey": key["accessKey"]}]}]
    (entry,) = audit(env)
    assert entry["action"] == "subuser.create" and entry["actor"] == "alice"
    assert entry["target_type"] == "subuser" and entry["target"] == "alice:ci"
    assert entry["details"]["permission"] == "read-write" and entry["details"]["uid"] == "alice"
    assert entry["details"]["accessKey"] == key["accessKey"]
    assert not any(s in str(env.db.audit.query(limit=1000)) for s in every_secret(env))


@pytest.mark.parametrize("name", ["", "UPPER", "a b", "a:b", "-ab", "ab-", "x" * 21, "a/b",
                                  "a$b", "ä", "a.b"])
def test_bad_names_are_422(env, name):
    r = env.alice.post(SUB, json={"name": name, "permission": "read"}, headers=H)
    assert r.status_code == 422
    assert alice(env).subusers == {} and not env.transport.mutations()


def test_name_alphabet_and_length_accepted(env):
    make(env, "a", "read")
    make(env, "a-1-" + "z" * 16, "write")  # 20 characters
    assert set(alice(env).subusers) == {"a", "a-1-" + "z" * 16}


@pytest.mark.parametrize("permission", ["admin", "readwrite", "full", "NONE", "", "READ"])
def test_bad_permission_is_422(env, permission):
    r = env.alice.post(SUB, json={"name": "ci", "permission": permission}, headers=H)
    assert r.status_code == 422 and alice(env).subusers == {}
    assert env.db.subusers.used("alice") == []


def test_full_control_needs_confirm(env):
    r = env.alice.post(SUB, json={"name": "boss", "permission": "full-control"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["requiresConfirmation"] is True
    assert alice(env).subusers == {} and env.db.subusers.used("alice") == []
    assert not env.transport.mutations()
    ok = make(env, "boss", "full-control", confirm=True)
    assert ok["permission"] == "full-control"


def test_patch_permission_and_full_control_confirm(env):
    make(env, "ci", "read")
    r = env.alice.patch(f"{SUB}/ci", json={"permission": "full-control"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["requiresConfirmation"] is True
    assert alice(env).subusers == {"ci": "read"}
    r = env.alice.patch(f"{SUB}/ci", json={"permission": "full-control", "confirm": True},
                        headers=H)
    assert r.status_code == 200 and r.json()["changed"] is True
    assert alice(env).subusers == {"ci": "full-control"}
    # lowering needs no confirmation
    r = env.alice.patch(f"{SUB}/ci", json={"permission": "write"}, headers=H)
    assert r.status_code == 200 and alice(env).subusers == {"ci": "write"}
    # the same permission again changes nothing and writes no audit row
    n = len(audit(env, "subuser.update"))
    r = env.alice.patch(f"{SUB}/ci", json={"permission": "write"}, headers=H)
    assert r.json()["changed"] is False and len(audit(env, "subuser.update")) == n
    assert {(a["details"]["old"], a["details"]["permission"])
            for a in audit(env, "subuser.update")} == {("read", "full-control"),
                                                       ("full-control", "write")}
    assert env.alice.patch(f"{SUB}/ci", json={"permission": "root"}, headers=H).status_code == 422
    assert env.alice.patch(f"{SUB}/nope", json={"permission": "read"}, headers=H).status_code \
        == 404


# -- limits and never-reused names ---------------------------------------------------------


def test_subuser_limit(env):
    for i in range(MAX_SUBUSERS):
        make(env, f"s{i}")
    r = env.alice.post(SUB, json={"name": "extra", "permission": "read"}, headers=H)
    assert r.status_code == 409 and "at most" in r.json()["detail"]
    assert len(alice(env).subusers) == MAX_SUBUSERS
    assert "extra" not in env.db.subusers.used("alice")


def test_names_are_never_reused(env):
    make(env, "ci")
    assert env.alice.delete(f"{SUB}/ci", headers=H).status_code == 200
    assert alice(env).subusers == {}
    r = env.alice.post(SUB, json={"name": "ci", "permission": "read"}, headers=H)
    assert r.status_code == 409 and "never reused" in r.json()["detail"]
    assert alice(env).subusers == {}
    assert env.db.subusers.used("alice") == ["ci"]
    make(env, "ci2")  # another name is fine


def test_existing_name_is_refused_and_duplicate_create(env):
    env.mock.state.croit.create_subuser("alice", "made-elsewhere", "read")
    r = env.alice.post(SUB, json={"name": "made-elsewhere", "permission": "read"}, headers=H)
    assert r.status_code == 409 and "already have" in r.json()["detail"]
    assert env.db.subusers.used("alice") == []
    make(env, "ci")
    assert env.alice.post(SUB, json={"name": "ci", "permission": "write"},
                          headers=H).status_code == 409
    assert alice(env).subusers["ci"] == "read"


def test_the_same_name_for_two_users_is_fine(env):
    make(env, "ci")
    bob = env.session("bob")
    r = bob.post(SUB, json={"name": "ci", "permission": "read"}, headers=H)
    assert r.status_code == 201
    assert env.mock.state.croit.users["bob"].subusers == {"ci": "read"}


def test_key_limit_per_subuser(env):
    make(env, "ci")
    assert env.alice.post(f"{SUB}/ci/keys", headers=H).status_code == 200
    assert len(sub_keys(env, "ci")) == MAX_KEYS
    r = env.alice.post(f"{SUB}/ci/keys", headers=H)
    assert r.status_code == 409 and "at most" in r.json()["detail"]
    assert len(sub_keys(env, "ci")) == MAX_KEYS


# -- keys ------------------------------------------------------------------------------


def only_key(env, name):
    (access,) = sub_keys(env, name)
    return access


def key_events(env):
    """('put'|'delete', access) of the subuser key writes, and ('get', '') for every read, in
    order, as croit calls (on rgw the reads are the user-info requests)."""
    out = []
    for method, path in env.transport.calls:
        if method == "GET":
            out.append(("get", ""))
        elif "/subusers/" in path and "/keys/" in path:
            out.append((method.lower(), path.split("?")[0].rpartition("/keys/")[2]))
    return out


def test_add_key_returns_only_access_key(env):
    make(env, "ci")
    r = env.alice.post(f"{SUB}/ci/keys", headers=H)
    assert r.status_code == 200 and list(r.json()) == ["accessKey"]
    new = r.json()["accessKey"]
    assert new in sub_keys(env, "ci") and sub_keys(env, "ci")[new] not in r.text
    assert audit(env, "subuser.key.create")[0]["target"] == new


def test_rotate_adds_new_key_before_removing_old(env):
    make(env, "ci")
    old = only_key(env, "ci")
    env.transport.calls.clear()
    r = env.alice.post(f"{SUB}/ci/keys/{old}/rotate", headers=H)
    assert r.status_code == 200 and r.json()["oldKeyRemoved"] is True
    new = r.json()["accessKey"]
    assert new != old and set(sub_keys(env, "ci")) == {new}
    ev = key_events(env)
    put = ev.index(("put", new))
    delete = ev.index(("delete", old))
    assert put < delete and ("get", "") in ev[put + 1:delete]
    entry = audit(env, "subuser.key.rotate")[0]
    assert entry["details"]["old_key_removed"] is True and entry["details"]["newKey"] == new


def test_rotate_at_the_key_limit_works(env):
    make(env, "ci")
    env.alice.post(f"{SUB}/ci/keys", headers=H)
    victim = sorted(sub_keys(env, "ci"))[0]
    r = env.alice.post(f"{SUB}/ci/keys/{victim}/rotate", headers=H)
    assert r.status_code == 200 and len(sub_keys(env, "ci")) == MAX_KEYS
    assert victim not in sub_keys(env, "ci")


def test_rotate_partial_failure_keeps_both(env):
    make(env, "ci")
    old = only_key(env, "ci")
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"message": "boom"}) if req.method == "DELETE" else None)
    r = env.alice.post(f"{SUB}/ci/keys/{old}/rotate", headers=H)
    assert r.status_code == 200 and r.json()["oldKeyRemoved"] is False
    assert old in sub_keys(env, "ci") and r.json()["accessKey"] in sub_keys(env, "ci")
    assert audit(env, "subuser.key.rotate")[0]["details"]["old_key_removed"] is False


def test_rotate_put_failure_keeps_old(env):
    make(env, "ci")
    old = only_key(env, "ci")
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"message": "boom"}) if req.method == "PUT" else None)
    assert env.alice.post(f"{SUB}/ci/keys/{old}/rotate", headers=H).status_code == 502
    assert set(sub_keys(env, "ci")) == {old}
    assert not [c for c in env.transport.calls if c[0] == "DELETE"]


def test_rotate_unconfirmed_new_key_keeps_old(env):
    make(env, "ci")
    old = only_key(env, "ci")
    env.transport.fail = lambda req: (
        httpx.Response(204) if req.method == "PUT" else None)  # says ok, stores nothing
    assert env.alice.post(f"{SUB}/ci/keys/{old}/rotate", headers=H).status_code == 502
    assert set(sub_keys(env, "ci")) == {old}
    assert not [c for c in env.transport.calls if c[0] == "DELETE"]
    assert audit(env, "subuser.key.rotate_failed")


def test_last_key_cannot_be_deleted_but_one_of_two_can(env):
    make(env, "ci")
    only = only_key(env, "ci")
    r = env.alice.delete(f"{SUB}/ci/keys/{only}", headers=H)
    assert r.status_code == 409 and "Rotate" in r.json()["detail"]
    assert set(sub_keys(env, "ci")) == {only}
    extra = env.alice.post(f"{SUB}/ci/keys", headers=H).json()["accessKey"]
    r = env.alice.delete(f"{SUB}/ci/keys/{only}", headers=H)
    assert r.status_code == 200 and set(sub_keys(env, "ci")) == {extra}
    assert audit(env, "subuser.key.delete")[0]["target"] == only


def test_delete_subuser_removes_its_keys(env):
    make(env, "ci")
    env.alice.post(f"{SUB}/ci/keys", headers=H)
    make(env, "other")
    gone = set(sub_keys(env, "ci"))
    kept = set(sub_keys(env, "other"))
    r = env.alice.delete(f"{SUB}/ci", headers=H)
    assert r.status_code == 200
    state = env.mock.state.croit
    assert "ci" not in alice(env).subusers and not sub_keys(env, "ci")
    assert not gone & set(state.access_keys)
    assert set(sub_keys(env, "other")) == kept and ALICE[2] in state.access_keys
    entry = audit(env, "subuser.delete")[0]
    assert entry["target"] == "alice:ci" and set(entry["details"]["accessKeys"]) == gone
    assert env.alice.delete(f"{SUB}/ci", headers=H).status_code == 404


# -- reveal -------------------------------------------------------------------------------


def test_reveal_audits_first_and_is_not_cached(env):
    make(env, "ci", "write")
    access = only_key(env, "ci")
    secret = sub_keys(env, "ci")[access]
    r = env.alice.post(f"{SUB}/ci/reveal", json={"accessKey": access}, headers=H)
    assert r.status_code == 200 and r.json() == {"accessKey": access, "secretKey": secret}
    assert r.headers["cache-control"] == "no-store"
    (entry,) = audit(env, "subuser.reveal")
    assert entry["target"] == access and entry["target_type"] == "access_key"
    assert entry["details"] == {"uid": "alice", "subuser": "ci", "permission": "write"}
    assert secret not in str(env.db.audit.query(limit=1000))


def test_reveal_is_refused_when_the_audit_cannot_be_written(env, monkeypatch):
    make(env, "ci")
    access = only_key(env, "ci")
    secret = sub_keys(env, "ci")[access]

    def broken(*_a, **_k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(env.db.audit, "add", broken)
    with pytest.raises(RuntimeError):  # no answer was built, so no body carried the secret
        env.alice.post(f"{SUB}/ci/reveal", json={"accessKey": access}, headers=H)
    assert secret not in str(audit(env, ""))


def test_reveal_only_own_subuser_keys(env):
    make(env, "ci")
    make(env, "other")
    for key in (ALICE[2], BOB[2], only_key(env, "other"), "NOPE"):
        r = env.alice.post(f"{SUB}/ci/reveal", json={"accessKey": key}, headers=H)
        assert r.status_code == 404, key
        assert not any(s in r.text for s in every_secret(env))
    assert not audit(env, "subuser.reveal")


def test_other_users_cannot_touch_my_subuser(env):
    make(env, "ci")
    access = only_key(env, "ci")
    bob = env.session("bob")
    assert bob.get(SUB).json()["subusers"] == []
    calls = [
        bob.patch(f"{SUB}/ci", json={"permission": "write"}, headers=H),
        bob.delete(f"{SUB}/ci", headers=H),
        bob.post(f"{SUB}/ci/keys", headers=H),
        bob.post(f"{SUB}/ci/keys/{access}/rotate", headers=H),
        bob.delete(f"{SUB}/ci/keys/{access}", headers=H),
        bob.post(f"{SUB}/ci/reveal", json={"accessKey": access}, headers=H),
    ]
    assert [r.status_code for r in calls] == [404] * 6
    assert alice(env).subusers == {"ci": "read"} and set(sub_keys(env, "ci")) == {access}
    assert env.mock.state.croit.users["bob"].subusers == {}
    # the uid never comes from the request
    r = bob.post(f"{SUB}/ci/reveal?uid=alice", json={"accessKey": access, "uid": "alice"},
                 headers=H)
    assert r.status_code == 404


# -- interactions --------------------------------------------------------------------------


def test_subuser_keys_are_not_the_users_own_keys(env):
    make(env, "ci")
    make(env, "other")
    sub_access = set(sub_keys(env, "ci")) | set(sub_keys(env, "other"))
    creds = env.alice.get("/api/credentials").json()
    assert [k["accessKey"] for k in creds["keys"]] == [ALICE[2]]
    # the limit of own keys counts only own keys: 4 more fit next to the subuser keys
    for _ in range(4):
        assert env.alice.post("/api/credentials/keys", headers=H).status_code == 200
    assert env.alice.post("/api/credentials/keys", headers=H).status_code == 409
    # a subuser key is not an own key for rotate, delete or reveal
    for access in sub_access:
        assert env.alice.post(f"/api/credentials/keys/{access}/rotate",
                              headers=H).status_code == 404
        assert env.alice.delete(f"/api/credentials/keys/{access}", headers=H).status_code == 404
        assert env.alice.post("/api/credentials/reveal", json={"accessKey": access},
                              headers=H).status_code == 404
    assert sub_access <= {k["access_key"] for k in alice(env).keys}


def test_s3_calls_use_the_users_own_key_not_a_subuser_key(env):
    make(env, "ci")
    assert asyncio.run(env.app.state.s3._creds("alice"))[1] == ALICE[2]


def test_suspended_user_is_refused_everywhere(env):
    make(env, "ci")
    access = only_key(env, "ci")
    alice(env).suspended = True
    env.app.state.rgw.invalidate(username="alice")
    env.transport.calls.clear()
    before = len(audit(env))
    calls = [
        env.alice.get(SUB),
        env.alice.post(SUB, json={"name": "x", "permission": "read"}, headers=H),
        env.alice.patch(f"{SUB}/ci", json={"permission": "write"}, headers=H),
        env.alice.delete(f"{SUB}/ci", headers=H),
        env.alice.post(f"{SUB}/ci/keys", headers=H),
        env.alice.post(f"{SUB}/ci/keys/{access}/rotate", headers=H),
        env.alice.delete(f"{SUB}/ci/keys/{access}", headers=H),
        env.alice.post(f"{SUB}/ci/reveal", json={"accessKey": access}, headers=H),
    ]
    assert [r.status_code for r in calls] == [403] * 8
    assert len(audit(env)) == before
    assert not [c for c in env.transport.calls if c[0] != "GET"]
    assert not any(s in r.text for r in calls for s in every_secret(env))
    assert alice(env).subusers == {"ci": "read"}


def test_csrf_and_unauthenticated(env):
    make(env, "ci")
    access = only_key(env, "ci")
    assert env.alice.post(SUB, json={"name": "x", "permission": "read"}).status_code == 403
    assert env.alice.delete(f"{SUB}/ci").status_code == 403
    assert env.alice.post(f"{SUB}/ci/reveal", json={"accessKey": access}).status_code == 403
    anon = env.anon()
    assert anon.get(SUB).status_code == 401
    assert anon.post(SUB, json={"name": "x", "permission": "read"}, headers=H).status_code == 401
    assert anon.post(f"{SUB}/ci/reveal", json={"accessKey": access},
                     headers=H).status_code == 401
    assert alice(env).subusers == {"ci": "read"}


# -- failures ------------------------------------------------------------------------------


def test_key_failure_rolls_the_subuser_back(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"message": "boom"}) if req.method == "PUT"
        and "key" in str(req.url) else None)
    r = env.alice.post(SUB, json={"name": "ci", "permission": "read"}, headers=H)
    assert r.status_code == 502
    assert alice(env).subusers == {} and not sub_keys(env, "ci")
    assert [a["action"] for a in audit(env)] == ["subuser.create_failed"]
    env.transport.fail = None
    # the name stays recorded: it existed for a moment and is never handed out again
    r = env.alice.post(SUB, json={"name": "ci", "permission": "read"}, headers=H)
    assert r.status_code == 409


def test_backend_refusal_changes_nothing_and_frees_the_name(env):
    def refuse(req):
        creating = (req.method == "POST" and req.url.path.endswith("/users/alice")) or (
            req.method == "PUT" and "subuser" in str(req.url) and "key" not in str(req.url))
        return httpx.Response(400, json={"code": 400, "message": "no"}) if creating else None

    env.transport.fail = refuse
    r = env.alice.post(SUB, json={"name": "ci", "permission": "read"}, headers=H)
    assert r.status_code == 400
    assert alice(env).subusers == {} and not audit(env)
    assert env.db.subusers.used("alice") == []  # nothing happened, so the name is free
    env.transport.fail = None
    make(env, "ci")


def test_backend_down_is_503(env):
    make(env, "ci")
    env.transport.fail = lambda req: httpx.ConnectError("down")
    assert env.alice.get(SUB).status_code == 503
    assert env.alice.post(f"{SUB}/ci/keys", headers=H).status_code == 503


def test_no_secret_in_any_response_or_audit_row(env):
    first = env.alice.post(SUB, json={"name": "ci", "permission": "read"}, headers=H)
    access = first.json()["keys"][0]["accessKey"]
    responses = [first, env.alice.get(SUB),
                 env.alice.post(f"{SUB}/ci/keys", headers=H),
                 env.alice.post(f"{SUB}/ci/keys/{access}/rotate", headers=H),
                 env.alice.patch(f"{SUB}/ci", json={"permission": "write"}, headers=H),
                 env.alice.get("/api/credentials")]
    assert all(r.status_code in (200, 201) for r in responses)
    secrets = every_secret(env)
    assert not any(s in r.text for r in responses for s in secrets)
    assert not any(s in str(env.db.audit.query(limit=1000)) for s in secrets)
    # only the reveal carries one
    new = next(iter(sub_keys(env, "ci")))
    rev = env.alice.post(f"{SUB}/ci/reveal", json={"accessKey": new}, headers=H)
    assert rev.json()["secretKey"] == sub_keys(env, "ci")[new]


# -- both backends, tenant uids -------------------------------------------------------------


def test_client_methods_encode_a_tenant_uid(env):
    croit = env.app.state.croit
    state = env.mock.state.croit

    async def go():
        await croit.create_subuser("acme$carol", "ci", "read-write")
        await croit.put_subuser_key("acme$carol", "ci", "TENANTKEY00000000001", "s" * 40)
        subs = await croit.list_subusers("acme$carol")
        keys = await croit.list_subuser_keys("acme$carol", "ci")
        own = await croit.list_keys("acme$carol")
        await croit.set_subuser_permission("acme$carol", "ci", "full-control")
        after = await croit.list_subusers("acme$carol")
        await croit.delete_subuser_key("acme$carol", "ci", "TENANTKEY00000000001")
        await croit.delete_subuser("acme$carol", "ci")
        return subs, keys, own, after

    subs, keys, own, after = asyncio.run(go())
    assert subs == [{"name": "ci", "permission": "read-write"}]
    assert [(k["user"], k["access_key"]) for k in keys] == [
        ("acme$carol:ci", "TENANTKEY00000000001")]
    assert {k["user"] for k in own} == {"acme$carol", "acme$carol:ci"}
    assert after == [{"name": "ci", "permission": "full-control"}]
    assert state.users["acme$carol"].subusers == {}
    assert "TENANTKEY00000000001" not in state.access_keys
    path = [p for _m, p in env.transport.raw_calls]
    assert any("acme%24carol" in p for p in path) or on_rgw()
    assert not any("acme$carol" in p for p in path)


def test_delete_subuser_via_client_removes_keys_on_both_backends(env):
    croit = env.app.state.croit

    async def go():
        await croit.create_subuser("alice", "ci", "read")
        await croit.put_subuser_key("alice", "ci", "KEYAAAAAAAAAAAAAAAAA", "a" * 40)
        await croit.put_subuser_key("alice", "ci", "KEYBBBBBBBBBBBBBBBBB", "b" * 40)
        await croit.delete_subuser("alice", "ci")
        return await croit.list_keys("alice")

    keys = asyncio.run(go())
    assert {k["user"] for k in keys} == {"alice"}
    assert not {"KEYAAAAAAAAAAAAAAAAA", "KEYBBBBBBBBBBBBBBBBB"} & set(
        env.mock.state.croit.access_keys)
