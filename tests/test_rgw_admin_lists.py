"""Admin lists on the rgw backend (Phase 5 A6): the short user-list cache, shared in-flight
fetches, invalidation on every mutation, tenant buckets that `stats=true` may drop, bounded
fan-out, and the service user staying out of the lists and the chargeback export."""

import asyncio
import csv
import io
import json

import httpx
import pytest

from bucketeer.croit import CroitUnavailable
from bucketeer.rgw_admin import CONCURRENCY, RgwAdminClient

from .backends import admin_backend, rgw_only  # noqa: F401  (rgw-only module)
from .test_admin import Env, H
from .test_admin_backends import rgw_settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _rgw_only(admin_backend):  # noqa: F811
    rgw_only("the rgw admin plane (these tests build RgwAdminClient themselves)")


class Probe(httpx.AsyncBaseTransport):
    """Counts per-user reads (and the peak running at once); can drop tenant buckets from the
    global `stats=true` answer (the Reef caveat), delay reads, or fail a request."""

    def __init__(self, inner, *, drop_tenant=False, delay=0.0):
        self.inner, self.drop_tenant, self.delay = inner, drop_tenant, delay
        self.user_reads = self.global_buckets = self.user_buckets = self.metadata = 0
        self.running = self.peak = 0
        self.fail = None

    async def handle_async_request(self, request):
        q = request.url.query.decode()
        path, get = request.url.path, request.method == "GET"
        if self.fail and (err := self.fail(request)):
            raise err
        is_user_read = path == "/admin/user" and "uid=" in q and get and "quota" not in q
        if path == "/admin/metadata/user":
            self.metadata += 1
        if path == "/admin/bucket" and "stats=true" in q and get:
            if "uid=" in q:
                self.user_buckets += 1
            else:
                self.global_buckets += 1
        if is_user_read:
            self.user_reads += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            resp = await self.inner.handle_async_request(request)
        finally:
            self.running -= 1
        if self.drop_tenant and path == "/admin/bucket" and "stats=true" in q \
                and "uid=" not in q and get:
            body = json.loads(await resp.aread())
            return httpx.Response(200, json=[b for b in body if not b.get("tenant")])
        return resp


@pytest.fixture
async def probe(admin_ops):
    transport = Probe(httpx.ASGITransport(app=admin_ops.app))
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    client.probe, client.state = transport, admin_ops.state
    yield client
    await client.aclose()


# -- user list cache -----------------------------------------------------------------------


async def test_user_list_is_cached_for_a_short_time(probe):
    first = await probe.list_users()
    reads = probe.probe.user_reads
    assert reads >= 3
    again = await probe.list_users()
    assert probe.probe.user_reads == reads and again == first
    again[0]["name"] = "mutated by a caller"  # callers get copies
    assert (await probe.list_users())[0]["name"] != "mutated by a caller"


async def test_cache_expires(probe, monkeypatch):
    from bucketeer import rgw_admin
    await probe.list_users()
    reads = probe.probe.user_reads
    now = rgw_admin.time.monotonic()
    monkeypatch.setattr(rgw_admin.time, "monotonic", lambda: now + rgw_admin.USERS_TTL + 1)
    await probe.list_users()
    assert probe.probe.user_reads == 2 * reads


async def test_a_uid_filter_is_never_served_from_the_cache(probe):
    await probe.list_users()
    reads = probe.probe.user_reads
    assert [u["uid"] for u in await probe.list_users(["alice"])] == ["alice"]
    assert probe.probe.user_reads == reads + 1


@pytest.mark.parametrize("mutate", [
    lambda c: c.update_user("alice", suspended=True),
    lambda c: c.set_user_quota("alice", enabled=True, max_size=123),
    lambda c: c.set_user_bucket_quota("alice", enabled=True, max_size=123),
    lambda c: c.create_user("zed", "Zed"),
    lambda c: c.delete_user("zed"),
    lambda c: c.put_key("alice", "NEWACCESSKEY0000001", "s" * 40),
    lambda c: c.delete_key("alice", "ALICEACCESSKEY0001"),
    lambda c: c.set_bucket_quota("alice-data", enabled=True, max_size=5),
], ids=["suspend", "user-quota", "bucket-quota-of-user", "create", "delete", "put-key",
        "delete-key", "bucket-quota"])
async def test_every_mutation_drops_the_cache(probe, mutate):
    probe.state.create_user("zed", "Zed")
    await probe.list_users()
    reads = probe.probe.user_reads
    try:
        await mutate(probe)
    except Exception:  # noqa: BLE001  (a failed write may still have changed things)
        pass
    await probe.list_users()
    assert probe.probe.user_reads > reads  # fetched again, not served from the cache


async def test_suspend_and_quota_show_up_at_once(probe):
    by = lambda users, uid: next(u for u in users if u["uid"] == uid)  # noqa: E731
    assert not by(await probe.list_users(), "alice")["suspended"]
    await probe.update_user("alice", suspended=True)
    assert by(await probe.list_users(), "alice")["suspended"]
    await probe.set_user_quota("alice", enabled=True, max_size=777)
    assert by(await probe.list_users(), "alice")["userQuota"]["maxSize"] == 777
    await probe.create_user("newbie", "New")
    assert "newbie" in {u["uid"] for u in await probe.list_users()}


async def test_a_failure_is_not_cached(probe):
    probe.probe.fail = lambda r: CroitUnavailable("down") if r.url.path == "/admin/metadata/user" \
        else None
    with pytest.raises(CroitUnavailable):
        await probe.list_users()
    probe.probe.fail = None
    users = await probe.list_users()  # the very next call tries again and succeeds
    assert {"alice", "bob"} <= {u["uid"] for u in users}
    reads = probe.probe.user_reads
    probe.probe.fail = lambda r: CroitUnavailable("down")
    assert len(await probe.list_users()) == len(users)  # cached success survives an outage
    assert probe.probe.user_reads == reads


async def test_a_failed_read_in_the_fan_out_is_not_cached(probe):
    probe.probe.fail = lambda r: httpx.ReadTimeout("slow") \
        if r.url.path == "/admin/user" and "uid=bob" in r.url.query.decode() else None
    with pytest.raises(CroitUnavailable):
        await probe.list_users()
    probe.probe.fail = None
    assert "bob" in {u["uid"] for u in await probe.list_users()}


async def test_concurrent_loads_share_one_fetch(admin_ops):
    for i in range(20):
        admin_ops.state.create_user(f"u{i:02d}", "U")
    transport = Probe(httpx.ASGITransport(app=admin_ops.app), delay=0.01)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    results = await asyncio.gather(*(client.list_users() for _ in range(10)))
    assert all(r == results[0] for r in results)
    assert transport.metadata == 1 and transport.user_reads == len(results[0])
    await client.aclose()


async def test_a_write_during_a_fetch_is_not_stored_stale(admin_ops):
    transport = Probe(httpx.ASGITransport(app=admin_ops.app), delay=0.02)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    slow = asyncio.ensure_future(client.list_users())
    await asyncio.sleep(0.01)  # the fan-out is running
    await client.update_user("alice", suspended=True)
    await slow
    fresh = {u["uid"]: u for u in await client.list_users()}
    assert fresh["alice"]["suspended"] is True  # the stale result was not kept
    await client.aclose()


async def test_one_waiter_cancelled_does_not_break_the_others(admin_ops):
    transport = Probe(httpx.ASGITransport(app=admin_ops.app), delay=0.01)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    a = asyncio.ensure_future(client.list_users())
    b = asyncio.ensure_future(client.list_users())
    await asyncio.sleep(0.005)
    a.cancel()
    assert len(await b) >= 3
    await client.aclose()


async def test_300_users_stay_within_the_concurrency_bound(admin_ops):
    for i in range(300):
        admin_ops.state.create_user(f"bulk{i:03d}", "Bulk")
    transport = Probe(httpx.ASGITransport(app=admin_ops.app), delay=0.002)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    users = await asyncio.gather(client.list_users(), client.list_users())
    assert len(users[0]) >= 300
    assert transport.user_reads == len(users[0])  # shared fetch: one read per user
    assert transport.peak <= CONCURRENCY
    await client.aclose()


# -- buckets ---------------------------------------------------------------------------------


async def test_bucket_stats_are_one_call_without_tenant_users(admin_ops):
    state = admin_ops.state
    state.buckets.pop("acme:reports")
    state.users.pop("acme$carol")
    transport = Probe(httpx.ASGITransport(app=admin_ops.app))
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    names = {b["bucket"] for b in await client.list_buckets()}
    assert {"alice-data", "bob-shared"} <= names
    assert (transport.global_buckets, transport.user_buckets) == (1, 0)
    await client.aclose()


async def test_tenant_buckets_the_global_call_drops_are_added(admin_ops):
    admin_ops.state.create_user("acme$dave", "Dave")
    admin_ops.state.create_bucket("dave-files", "acme$dave")
    transport = Probe(httpx.ASGITransport(app=admin_ops.app), drop_tenant=True)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    rows = await client.list_buckets()
    names = {b["bucket"] for b in rows}
    assert {"acme:reports", "acme:dave-files", "alice-data"} <= names
    assert len(names) == len(rows)  # no duplicates
    assert transport.global_buckets == 1 and transport.user_buckets == 2  # per tenant user
    # usage and owner survive the detour
    reports = next(b for b in rows if b["bucket"] == "acme:reports")
    assert reports["owner"] == "acme$carol" and reports["tenant"] == "acme"
    await client.aclose()


async def test_tenant_buckets_are_not_duplicated_when_the_global_call_has_them(probe):
    rows = await probe.list_buckets()
    assert [b["bucket"] for b in rows].count("acme:reports") == 1


async def test_many_tenant_users_stay_bounded(admin_ops):
    for i in range(60):
        admin_ops.state.create_user(f"t{i % 3}$user{i}", "T")
    transport = Probe(httpx.ASGITransport(app=admin_ops.app), delay=0.002, drop_tenant=True)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    await asyncio.gather(client.list_buckets(), client.list_buckets())
    assert transport.global_buckets == 1  # concurrent callers share the fetch
    assert transport.user_buckets == 61
    assert transport.peak <= CONCURRENCY
    await client.aclose()


async def test_a_tenant_query_failure_fails_the_bucket_list_and_is_not_cached(admin_ops):
    transport = Probe(httpx.ASGITransport(app=admin_ops.app), drop_tenant=True)
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    transport.fail = lambda r: httpx.ReadTimeout("slow") \
        if r.url.path == "/admin/bucket" and "uid=" in r.url.query.decode() else None
    with pytest.raises(CroitUnavailable):
        await client.list_buckets()
    transport.fail = None
    assert "acme:reports" in {b["bucket"] for b in await client.list_buckets()}
    await client.aclose()


async def test_bucket_list_is_not_cached_but_in_flight_calls_share(admin_ops):
    transport = Probe(httpx.ASGITransport(app=admin_ops.app))
    client = RgwAdminClient(rgw_settings(admin_ops), transport=transport)
    await client.list_buckets()
    await client.list_buckets()
    assert transport.global_buckets == 2
    await client.aclose()


# -- app level -------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    rgw_only("the rgw admin plane")
    e = Env(tmp_path)
    e.transport.inner = Probe(e.transport.inner, drop_tenant=True)
    yield e
    e.db.close()


def test_admin_buckets_and_chargeback_include_tenant_buckets(env):
    names = {b["name"] for b in env.admin.get("/api/admin/buckets").json()["buckets"]}
    assert "acme:reports" in names
    r = env.admin.get("/api/admin/export/chargeback.csv")
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assert "acme:reports" in {x["bucket"] for x in rows}


def test_service_user_and_its_buckets_stay_out_of_lists_and_chargeback(env):
    env.mock.state.croit.create_bucket("svc-secret-bucket", "bucketeer-admin")
    users = {u["uid"] for u in env.admin.get("/api/admin/users").json()["users"]}
    assert "bucketeer-admin" not in users and "alice" in users
    buckets = env.admin.get("/api/admin/buckets").text
    assert "svc-secret-bucket" not in buckets
    csv_text = env.admin.get("/api/admin/export/chargeback.csv").text
    assert "svc-secret-bucket" not in csv_text and "bucketeer-admin" not in csv_text


def test_admin_actions_show_up_in_the_next_list(env):
    def alice():
        return next(u for u in env.admin.get("/api/admin/users").json()["users"]
                    if u["uid"] == "alice")
    assert alice()["suspended"] is False
    assert env.admin.post("/api/admin/users/alice/suspend", headers=H).status_code == 200
    assert alice()["suspended"] is True
    r = env.admin.put("/api/admin/users/alice/quota", headers=H,
                      json={"enabled": True, "maxSize": 4096, "maxObjects": None})
    assert r.status_code == 200 and alice()["quota"]["maxSize"] == 4096
    env.admin.post("/api/admin/users/alice/unsuspend", headers=H)
    assert alice()["suspended"] is False


def test_a_login_created_account_appears_in_the_admin_list(env):
    # the resolver creates the user through the same client, which drops the cached list
    assert "newcomer" not in {u["uid"] for u in env.admin.get("/api/admin/users").json()["users"]}
    from .test_auth import USERS_GROUP
    env.fake.add("newcomer", groups={USERS_GROUP}, display="New Comer", mail="n@example.org")
    env.session("newcomer")
    assert env.session("newcomer").get("/api/account").json()["status"] == "ready"
    assert "newcomer" in {u["uid"] for u in env.admin.get("/api/admin/users").json()["users"]}
