"""Bucket quotas in whole TiB, the weekly increase limit, cost data and the one-time
unlimited user quota step (P6-4). Runs on both admin backends."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bucketeer import bucket_quota as bq
from bucketeer import catalog

from .backends import admin_backend  # noqa: F401
from .test_buckets import Env, H, create

TIB = 2**40


@pytest.fixture
def env(tmp_path, s3_endpoint):
    e = Env(tmp_path, s3_endpoint)
    yield e
    e.db.close()


def limit(env, tib):
    env.db.settings.set(bq.KEY, {"defaultQuotaTiB": 1, "maxWeeklyQuotaIncreaseTiB": tib})


def bucket_quota_of(env, name):
    return env.croit_state.buckets[name].quota


def changes(env):
    return env.db.quota_changes.list()


def put_quota(client, name, tib):
    return client.put(f"/api/buckets/{name}/quota", json={"quotaTiB": tib}, headers=H)


# -- pure: the rolling window --------------------------------------------------------------

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def row(days_ago, inc):
    return {"ts": bq.iso(NOW - timedelta(days=days_ago)), "increase": inc}


def test_window_counts_only_the_last_seven_days():
    rows = [row(7, 50), row(6.99, 30), row(1, 10)]  # exactly 7 days ago has aged out
    w = bq.window(rows, 100, NOW)
    assert w["usedTiB"] == 40 and w["remainingTiB"] == 60


def test_check_passes_up_to_the_limit_and_names_when_it_frees():
    rows = [row(6, 30), row(2, 50)]
    bq.check(rows, 100, NOW, 20)  # exactly the limit: fine
    with pytest.raises(bq.LimitExceeded) as e:
        bq.check(rows, 100, NOW, 40)
    assert e.value.remaining == 20
    assert e.value.frees_up_at == NOW - timedelta(days=6) + timedelta(days=7)
    body = e.value.body()
    assert body["remainingTiB"] == 20 and body["freesUpAt"].startswith("2026-11-25T12:00")
    assert "You can add 20 TiB more" in body["detail"] and "40 TiB after Nov 25" in body["detail"]
    with pytest.raises(bq.LimitExceeded) as e:  # needs both rows to age out
        bq.check(rows, 100, NOW, 90)
    assert e.value.frees_up_at == NOW - timedelta(days=2) + timedelta(days=7)


def test_check_more_than_the_limit_never_frees_and_decreases_always_pass():
    with pytest.raises(bq.LimitExceeded) as e:
        bq.check([], 100, NOW, 101)
    assert e.value.frees_up_at is None and e.value.body()["freesUpAt"] is None
    bq.check([row(1, 100)], 100, NOW, 0)
    bq.check([row(1, 100)], 100, NOW, -50)


def test_quota_tib_rounds_up_and_ignores_unlimited():
    assert bq.quota_tib({"enabled": True, "maxSize": TIB + 1}) == 2
    assert bq.quota_tib({"enabled": True, "maxSize": 5}) == 1
    assert bq.quota_tib({"enabled": False, "maxSize": TIB}) is None
    assert bq.quota_tib({"enabled": True, "maxSize": -1}) is None


def test_catalog_reads_billings_products_and_price():
    c = catalog.build_catalog({"pricePerTiBMonth": 5, "currency": "EUR",
                               "products": [{"id": "archive", "name": "Deep"}]})
    t3 = catalog.product_for(c, "onprem")
    assert t3["name"] == "Tier-3-S3" and t3["pricePerTiBMonth"] == 5 and t3["currency"] == "EUR"
    assert catalog.product(c, "archive")["name"] == "Deep"
    assert catalog.monthly_cost(t3, 10) == 50 and catalog.monthly_cost(t3, None) is None
    assert [p["name"] for p in catalog.build_catalog(None)] == [
        "Tier-3-S3", "Archive-S3", "Cloud-S3"]


# -- create ---------------------------------------------------------------------------------


def test_create_sets_the_quota_and_records_it(env):
    env.db.settings.set("billing", {"pricePerTiBMonth": 5})
    r = create(env.alice, quotaTiB=10)
    assert r.status_code == 201
    assert r.json()["quotaTiB"] == 10 and r.json()["monthlyCost"] == 50
    q = bucket_quota_of(env, "alice-new")
    assert q["enabled"] and q["maxSize"] == 10 * TIB
    (c,) = changes(env)
    assert (c["person"], c["old_tib"], c["new_tib"], c["increase"], c["counted"]) == (
        "alice", None, 10, 10, 1)
    assert env.audit("bucket.create")[0]["details"]["quotaTiB"] == 10
    assert env.all_tags("alice-new")["bucketeer-quota"] == "10"


def test_create_uses_the_admin_default_and_validates(env):
    env.db.settings.set(bq.KEY, {"defaultQuotaTiB": 3})
    assert create(env.alice).json()["quotaTiB"] == 3
    for bad in (0, -1, 100_001, 1.5, "2"):
        r = create(env.alice, name="alice-bad", quotaTiB=bad)
        assert r.status_code == 422, bad
    assert "alice-bad" not in env.croit_state.buckets


def test_create_over_the_weekly_limit_creates_nothing(env):
    limit(env, 20)
    assert create(env.alice, name="alice-one", quotaTiB=15).status_code == 201
    r = create(env.alice, name="alice-two", quotaTiB=10)
    assert r.status_code == 409
    assert r.json()["remainingTiB"] == 5 and r.json()["freesUpAt"]
    assert "alice-two" not in env.croit_state.buckets
    assert create(env.alice, name="alice-two", quotaTiB=5).status_code == 201


def test_quota_failure_on_create_keeps_the_bucket_with_a_warning(env):
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"code": 500, "message": "boom"})
        if req.method == "PUT" and req.url.path.endswith("/quota") else None)
    r = create(env.alice, quotaTiB=4)
    env.transport.fail = None
    assert r.status_code == 201 and r.json()["quotaApplied"] is False
    assert "Change quota" in r.json()["warning"] and r.json()["quotaTiB"] is None
    assert changes(env) == []  # nothing counted that was not set
    assert env.audit("bucket.create")[0]["details"]["quotaApplied"] is False


# -- change quota -----------------------------------------------------------------------------


def test_owner_changes_quota_increase_counts_decrease_never_gives_back(env):
    limit(env, 20)
    assert create(env.alice, quotaTiB=15).status_code == 201
    r = put_quota(env.alice, "alice-new", 25)  # +10 on top of 15: over 20
    assert r.status_code == 409 and r.json()["remainingTiB"] == 5
    assert bucket_quota_of(env, "alice-new")["maxSize"] == 15 * TIB
    r = put_quota(env.alice, "alice-new", 5)  # a decrease always works
    assert r.status_code == 200 and r.json()["changed"] is True
    assert bucket_quota_of(env, "alice-new")["maxSize"] == 5 * TIB
    assert put_quota(env.alice, "alice-new", 10).status_code == 200  # +5: exactly 20
    assert put_quota(env.alice, "alice-new", 11).status_code == 409  # no credit for the cut
    (a,) = [x for x in env.audit("bucket.quota") if x["details"]["newTiB"] == 5]
    assert a["details"]["oldTiB"] == 15 and a["actor"] == "alice"
    assert len(env.audit("bucket.quota")) == 2
    r = put_quota(env.alice, "alice-new", 10)  # unchanged
    assert r.json()["changed"] is False and len(env.audit("bucket.quota")) == 2


def test_increases_older_than_a_week_no_longer_count(env):
    limit(env, 20)
    old = bq.iso(datetime.now(UTC) - timedelta(days=7, minutes=1))
    env.db.quota_changes.add(person="alice", uid="alice", bucket="alice-data", old_tib=0,
                             new_tib=20, actor="alice", ts=old)
    assert create(env.alice, quotaTiB=20).status_code == 201


def test_limit_is_per_person_across_buckets_and_shown_in_options(env):
    limit(env, 20)
    assert create(env.alice, name="alice-a", quotaTiB=8).status_code == 201
    assert create(env.alice, name="alice-b", quotaTiB=8).status_code == 201
    o = env.alice.get("/api/buckets/options").json()
    assert o["quota"]["remainingTiB"] == 4 and o["quota"]["weeklyLimitTiB"] == 20
    assert o["quota"]["defaultTiB"] == 1 and o["quota"]["exempt"] is False
    assert o["quota"]["nextReleaseAt"]
    assert o["product"]["name"] == "Tier-3-S3"
    assert put_quota(env.alice, "alice-a", 13).status_code == 409


def test_admins_are_exempt_and_their_own_changes_do_not_count(env):
    limit(env, 2)
    assert create(env.bob, name="bob-big", budget=None, quotaTiB=500).status_code == 201
    assert put_quota(env.bob, "bob-big", 900).status_code == 200
    assert all(c["counted"] == 0 for c in changes(env))
    assert env.bob.get("/api/buckets/options").json()["quota"]["remainingTiB"] is None


def test_admin_quota_route_is_recorded_not_counted(env):
    limit(env, 5)
    assert create(env.alice, quotaTiB=5).status_code == 201
    r = env.bob.put("/api/admin/buckets/alice-new/quota", headers=H,
                    json={"enabled": True, "maxSize": 50 * TIB, "maxObjects": None})
    assert r.status_code == 200
    c = changes(env)[0]
    assert (c["actor"], c["old_tib"], c["new_tib"], c["counted"], c["person"]) == (
        "bob", 5, 50, 0, None)
    assert put_quota(env.alice, "alice-new", 10).status_code == 200  # a decrease from 50


def test_quota_route_guards(env):
    assert put_quota(env.alice, "bob-data", 5).status_code == 404  # not hers
    assert put_quota(env.alice, "Alice-Data", 5).status_code == 404
    assert put_quota(env.alice, "alice-data", 0).status_code == 422
    assert env.anon().put("/api/buckets/alice-data/quota", json={"quotaTiB": 2},
                          headers=H).status_code == 401
    assert env.alice.put("/api/buckets/alice-data/quota", json={"quotaTiB": 2}).status_code == 403


def test_parallel_increases_cannot_both_pass(env):
    limit(env, 10)
    assert create(env.alice, name="alice-a", quotaTiB=1).status_code == 201
    assert create(env.alice, name="alice-b", quotaTiB=1).status_code == 201  # 2 of 10 used

    async def both():
        transport = httpx.ASGITransport(app=env.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver",
                                     cookies=dict(env.alice.cookies)) as c:
            return await asyncio.gather(*(
                c.put("/api/buckets/alice-a/quota", json={"quotaTiB": 6}, headers=H),
                c.put("/api/buckets/alice-b/quota", json={"quotaTiB": 6}, headers=H)))

    codes = sorted(r.status_code for r in asyncio.run(both()))
    assert codes == [200, 409]
    assert sum(c["increase"] for c in changes(env) if c["counted"]) <= 10


# -- list: quota and cost ----------------------------------------------------------------------


def test_list_shows_quota_tib_and_monthly_cost(env):
    env.db.settings.set("billing", {"pricePerTiBMonth": 4.5})
    assert create(env.alice, quotaTiB=3).status_code == 201
    body = env.alice.get("/api/buckets").json()
    b = {x["name"]: x for x in body["buckets"]}
    assert b["alice-new"]["quotaTiB"] == 3 and b["alice-new"]["monthlyCost"] == 13.5
    assert b["alice-data"]["quotaTiB"] is None and b["alice-data"]["monthlyCost"] is None
    assert body["product"]["pricePerTiBMonth"] == 4.5 and body["product"]["billedOn"] == "quota"


# -- one-time: unlimited user quotas -------------------------------------------------------------


def test_unlimit_user_quotas_once_for_people_on_the_old_default(env):
    users = env.croit_state.users
    users["alice"].user_quota.update(enabled=True, maxSize=TIB, maxObjects=-1)
    users["bob"].user_quota.update(enabled=True, maxSize=5 * TIB, maxObjects=-1)  # by hand
    env.db.known_accounts.record("alice", "alice", "linked")
    env.db.known_accounts.record("bob", "bob", "linked")
    res = asyncio.run(bq.unlimit_user_quotas(env.app))
    assert res == {"changed": 1, "kept": 1, "failed": 0}
    assert users["alice"].user_quota["enabled"] is False
    assert users["bob"].user_quota["enabled"] is True
    (a,) = env.audit("rgw.user.quota_unlimited")
    assert a["actor"] == "system" and a["target"] == "alice"
    assert env.db.settings.get(bq.DONE_KEY)["changed"] == 1
    users["alice"].user_quota.update(enabled=True, maxSize=TIB)
    assert asyncio.run(bq.unlimit_user_quotas(env.app)) is None  # ran once
    assert users["alice"].user_quota["enabled"] is True


def test_unlimit_retries_until_every_account_was_checked(env):
    env.croit_state.users["alice"].user_quota.update(enabled=True, maxSize=TIB, maxObjects=-1)
    env.db.known_accounts.record("alice", "alice", "linked")
    env.transport.fail = lambda req: (
        httpx.Response(500, json={"code": 500, "message": "boom"})
        if req.method == "PUT" and req.url.path.endswith("/quota") else None)
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    asyncio.run(bq.unlimit_on_start(env.app, sleep=sleep))
    assert len(sleeps) == bq.UNLIMIT_ATTEMPTS - 1
    assert env.db.settings.get(bq.DONE_KEY) is None
    env.transport.fail = None
    asyncio.run(bq.unlimit_on_start(env.app, sleep=sleep))
    assert env.db.settings.get(bq.DONE_KEY)["changed"] == 1
    assert env.croit_state.users["alice"].user_quota["enabled"] is False


def test_unlimit_never_touches_bucket_key_users(env):
    env.croit_state.users["alice"].user_quota.update(enabled=True, maxSize=TIB, maxObjects=-1)
    env.db.known_accounts.record("alice", "alice", "linked")
    env.db.bucket_keys.create("alice", "alice", "alice-data", "read", "x")
    res = asyncio.run(bq.unlimit_user_quotas(env.app))
    assert res["changed"] == 0 and res["kept"] == 1
