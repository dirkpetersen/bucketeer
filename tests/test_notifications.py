"""GET/PUT /api/buckets/{name}/notifications against mock croit and moto (S3 + SNS).

moto's SNS accepts RGW's topic attributes but does not keep them, so the attributes Bucketeer
sends are asserted through a spy on the SNS client factory. Whether RGW delivers events is a
cluster check.
"""

import boto3
import pytest

from bucketeer import notifications
from bucketeer.notifications import topic_attributes, topic_name

from .test_buckets import ALICE_KEY, BOB_KEY, Env, H

EP = {"type": "https", "url": "https://hooks.example.org/in", "topic": "", "verifyTls": False}
ADMIN = "/api/admin/settings/notification-endpoints"


class SpySns:
    def __init__(self, real, log, access):
        self.real, self.log, self.access = real, log, access

    def create_topic(self, **kw):
        self.log.append({"access": self.access, **kw})
        return self.real.create_topic(**kw)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.fixture
def env(tmp_path, s3_endpoint, monkeypatch):
    e = Env(tmp_path, s3_endpoint)
    e.topics = []
    real = e.app.state.s3.sns_factory

    def spy(endpoint, access, secret, settings):
        return SpySns(real(endpoint, access, secret, settings), e.topics, access)

    e.app.state.s3.sns_factory = spy
    # moto's ARNs carry its account id where RGW has the tenant ("" without one)
    monkeypatch.setattr(notifications, "_arn_tenant_ok",
                        lambda arn_tenant, tenant: arn_tenant == (tenant or "123456789012"))
    e.sns_url = s3_endpoint
    yield e
    e.db.close()


def url(name="alice-data"):
    return f"/api/buckets/{name}/notifications"


def endpoint(env, name="hook", **over):
    r = env.bob.put(f"{ADMIN}/{name}", json={**EP, **over}, headers=H)
    assert r.status_code == 200, r.text


def get(env):
    return env.alice.get(url()).json()


def put(env, items, etag=None, headers=H):
    return env.alice.put(url(), json={"notifications": items, "etag": etag or get(env)["etag"]},
                         headers=headers)


def item(**over):
    return {"id": "n1", "endpoint": "hook", "events": ["s3:ObjectCreated:*"], **over}


def stored(env):
    return env.moto().get_bucket_notification_configuration(Bucket="alice-data").get(
        "TopicConfigurations", [])


def external(env, topic="their-topic", nid="ext1"):
    sns = boto3.client("sns", endpoint_url=env.sns_url, region_name="us-east-1",
                       aws_access_key_id="x", aws_secret_access_key="y")
    arn = sns.create_topic(Name=topic)["TopicArn"]
    cfg = {"Id": nid, "TopicArn": arn, "Events": ["s3:ObjectRemoved:*"],
           "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "x/"}]}}}
    env.moto().put_bucket_notification_configuration(
        Bucket="alice-data", NotificationConfiguration={"TopicConfigurations": [cfg]})
    return arn


def test_get_empty_and_no_endpoints(env):
    j = get(env)
    assert j["notifications"] == [] and j["endpoints"] == [] and j["etag"]
    assert env.alice.get(url()).headers["cache-control"] == "no-store"
    assert env.used and set(env.used) == {ALICE_KEY}


def test_add_notification_topic_from_allowlist(env):
    endpoint(env)
    j = get(env)
    assert j["endpoints"] == [{"name": "hook", "type": "https"}]  # no address for users
    assert "hooks.example.org" not in str(j)
    r = put(env, [item(prefix="in/", suffix=".csv",
                       events=["s3:ObjectCreated:Put", "s3:ObjectCreated:Copy"])])
    assert r.status_code == 200, r.text
    n = r.json()["notifications"][0]
    assert n["managed"] is True and n["endpoint"] == "hook" and n["topic"] == "bucketeer-hook-alice"
    assert n["prefix"] == "in/" and n["suffix"] == ".csv" and r.json()["changed"] is True
    assert "hooks.example.org" not in r.text
    assert env.topics == [{"access": ALICE_KEY, "Name": "bucketeer-hook-alice", "Attributes": {
        "push-endpoint": "https://hooks.example.org/in", "verify-ssl": "false"}}]
    cfg = stored(env)[0]
    assert cfg["TopicArn"].endswith(":bucketeer-hook-alice") and cfg["Id"] == "n1"
    assert {x["Name"]: x["Value"] for x in cfg["Filter"]["Key"]["FilterRules"]} == {
        "prefix": "in/", "suffix": ".csv"}
    assert get(env)["notifications"] == r.json()["notifications"]
    again = put(env, [item(prefix="in/", suffix=".csv",
                           events=["s3:ObjectCreated:Put", "s3:ObjectCreated:Copy"])],
                r.json()["etag"])
    assert again.json()["changed"] is False and len(env.topics) == 1
    assert set(env.used) == {ALICE_KEY}


def test_user_cannot_inject_url_or_attributes(env):
    endpoint(env)
    for extra in ({"url": "http://evil.example"}, {"pushEndpoint": "http://evil"},
                  {"attributes": {"push-endpoint": "http://evil"}}):
        r = put(env, [{**item(), **extra}])
        assert r.status_code == 422, extra
    r = put(env, [item(endpoint="http://evil.example")])
    assert r.status_code == 422 and stored(env) == [] and env.topics == []


def test_topic_attributes_per_type(env):
    a = topic_attributes({"name": "q", "type": "amqp", "url": "amqps://b:5671", "topic": "ex",
                          "verifyTls": True})
    assert a == {"push-endpoint": "amqps://b:5671", "amqp-exchange": "ex", "verify-ssl": "true"}
    assert topic_attributes({"name": "k", "type": "kafka", "url": "kafka://b:9092",
                             "topic": "t"}) == {"push-endpoint": "kafka://b:9092"}
    assert topic_attributes({"name": "h", "type": "http", "url": "http://h:1",
                             "topic": ""}) == {"push-endpoint": "http://h:1"}
    assert topic_name("alice", "ci-hook") == "bucketeer-ci_2dhook-alice"


def test_amqp_without_exchange_refused(env):
    # the admin API refuses it now; a stored one (older data) still fails safely for users
    r = env.bob.put(f"{ADMIN}/bus", json={**EP, "type": "amqp", "url": "amqp://b:5672"},
                    headers=H)
    assert r.status_code == 422
    env.db.settings.set("notification_endpoints", [
        {"name": "bus", "type": "amqp", "url": "amqp://broker:5672", "topic": "",
         "verifyTls": True}])
    r = put(env, [item(endpoint="bus")])
    assert r.status_code == 422 and stored(env) == []


def test_resync_attributes_after_admin_change(env):
    endpoint(env)
    j = put(env, [item()]).json()
    endpoint(env, url="https://new.example.org/in", verifyTls=True)
    r = put(env, [item(), item(id="n2", events=["s3:ObjectRemoved:Delete"])], j["etag"])
    assert r.status_code == 200
    assert env.topics[-1]["Attributes"] == {"push-endpoint": "https://new.example.org/in",
                                            "verify-ssl": "true"}
    assert len(env.topics) == 2  # one CreateTopic per save, not per notification


def test_unknown_or_removed_endpoint_422(env):
    r = put(env, [item(endpoint="nope")])
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "endpoint"
    endpoint(env)
    j = put(env, [item()]).json()
    assert env.bob.delete(f"{ADMIN}/hook", headers=H).status_code == 200
    # the view changed with the allowlist (409 for the old etag); the notification is now
    # external: kept verbatim, but a managed item for the removed endpoint is refused
    assert put(env, [item()], j["etag"]).status_code == 409
    got = get(env)["notifications"][0]
    assert got["managed"] is False and got["endpoint"] is None
    assert put(env, [item()]).status_code == 422
    r = put(env, [{"id": "n1", "external": True}])
    assert r.status_code == 200 and r.json()["changed"] is False


@pytest.mark.parametrize("bad", [
    {"events": []}, {"events": ["s3:ObjectCreated:Nope"]}, {"events": ["s3:*"]},
    {"events": ["s3:ObjectCreated:*", "s3:ObjectCreated:Put"]}, {"events": "s3:ObjectCreated:*"},
    {"id": ""}, {"id": "x" * 65}, {"id": "a\nb"}, {"prefix": "a" * 1025}, {"suffix": "a\x00"},
    {"prefix": 5}, {"events": [3]},
])
def test_validation(env, bad):
    endpoint(env)
    r = put(env, [item(**bad)])
    assert r.status_code == 422, r.text
    assert r.json()["errors"] and stored(env) == []


def test_duplicate_ids_and_limits(env):
    endpoint(env)
    assert put(env, [item(), item()]).status_code == 422
    assert put(env, [item(id=f"n{i}") for i in range(101)]).status_code == 422
    ok = put(env, [item(prefix="é" * 512)])  # 1024 bytes exactly
    assert ok.status_code == 200
    assert put(env, [item(prefix="é" * 513)], ok.json()["etag"]).status_code == 422


def test_external_notification_preserved_and_removable(env):
    endpoint(env)
    arn = external(env)
    j = get(env)
    ext = j["notifications"][0]
    assert ext["managed"] is False and ext["endpoint"] is None and ext["topic"] == "their-topic"
    r = put(env, [{"id": "ext1", "external": True}, item()], j["etag"])
    assert r.status_code == 200, r.text
    ids = {c["Id"]: c for c in stored(env)}
    assert ids["ext1"]["TopicArn"] == arn and ids["ext1"]["Events"] == ["s3:ObjectRemoved:*"]
    assert ids["ext1"]["Filter"]["Key"]["FilterRules"][0]["Value"] == "x/"
    assert [n["managed"] for n in r.json()["notifications"]] == [False, True]
    # an unknown external id, or an external flag on a managed item, is refused
    assert put(env, [{"id": "ghost", "external": True}], r.json()["etag"]).status_code == 422
    assert put(env, [{"id": "n1", "external": True}], r.json()["etag"]).status_code == 422
    # removing: omit it
    r2 = put(env, [item()], r.json()["etag"])
    assert [n["id"] for n in r2.json()["notifications"]] == ["n1"] and len(stored(env)) == 1
    r3 = put(env, [], r2.json()["etag"])
    assert r3.status_code == 200 and r3.json()["notifications"] == [] and stored(env) == []


def test_etag_conflict_keeps_current(env):
    endpoint(env)
    stale = get(env)["etag"]
    external(env)
    r = put(env, [item()], stale)
    assert r.status_code == 409 and r.json()["etagMismatch"] is True
    cur = r.json()["current"]
    assert cur["notifications"][0]["id"] == "ext1" and cur["etag"] == get(env)["etag"]
    assert env.audit("bucket.notifications") == [] and env.topics == []


def test_other_users_bucket_404(env):
    assert env.alice.get(url("bob-shared")).status_code == 404
    r = env.alice.put(url("bob-shared"), json={"notifications": [], "etag": "x"}, headers=H)
    assert r.status_code == 404 and env.audit("bucket.notifications") == []


def test_suspended_refused(env):
    env.croit_state.users["alice"].suspended = True
    env.app.state.rgw.invalidate(username="alice")
    assert env.alice.get(url()).status_code == 403
    assert put(env, [], "x").status_code == 403


def test_csrf_and_login(env):
    endpoint(env)
    assert put(env, [item()], get(env)["etag"], headers={}).status_code == 403
    assert env.anon().get(url()).status_code in (401, 403)
    assert stored(env) == [] and env.topics == []


def test_audit_rows_names_only(env):
    endpoint(env)
    j = put(env, [item(prefix="a/")]).json()
    put(env, [], j["etag"])
    rows = list(reversed(env.audit("bucket.notifications")))
    assert len(rows) == 2 and rows[0]["target"] == "alice-data" and rows[0]["actor"] == "alice"
    d = rows[0]["details"]
    assert d["count_before"] == 0 and d["count_after"] == 1
    assert d["after"] == [{"id": "n1", "endpoint": "hook", "events": ["s3:ObjectCreated:*"],
                           "prefix": "a/", "suffix": None}]
    assert "example.org" not in str(rows)
    assert rows[1]["details"]["count_after"] == 0


def test_topic_name_injective():
    pairs = [("c", "a-b"), ("b-c", "a"), ("b", "a_2db-c"), ("a.b", "c"), ("a_b", "c"),
             ("a-b", "c"), ("a+b", "c"), ("c", "a.b"), ("c", "a_b")]
    names = [topic_name(u, e) for u, e in pairs]
    assert len(set(names)) == len(names)
    # same bare uid in a tenant and out of it shares a name: topics are per tenant anyway
    assert topic_name("acme$carol", "x") == topic_name("carol", "x")
    assert topic_name("c", "a-b") != topic_name("b-c", "a")
    assert topic_name("a.b", "c") != topic_name("a_b", "c") != topic_name("a-b", "c")
    long = topic_name("u" * 64 + "@+" * 20, "e" * 48 + "._" * 30)
    assert len(long) <= 256 and set(long) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    assert topic_name("x", "h" * 200) != topic_name("x", "i" * 200)
    worst = topic_name("u" * 118, "e" * 118)
    assert len(worst) == 247 and len(topic_name("u" * 119, "e" * 119)) <= 249


def test_own_topic_with_rgw_arns():
    """The real tenant comparison (the env fixture patches it for moto's account id)."""
    eps = [{"name": "hook"}]
    assert notifications._own_topic(
        "arn:aws:sns:default::bucketeer-hook-alice", "alice", eps) == "hook"
    assert notifications._own_topic(
        "arn:aws:sns:default:acme:bucketeer-hook-carol", "acme$carol", eps) == "hook"
    for arn, uid in [
        ("arn:aws:sns:default:acme:bucketeer-hook-alice", "alice"),
        ("arn:aws:sns:default::bucketeer-hook-carol", "acme$carol"),
        ("arn:aws:sns:default:other:bucketeer-hook-carol", "acme$carol"),
        ("arn:aws:sns:default::bucketeer-hook-bob", "alice"),
        ("arn:aws:sns:default::bucketeer-gone-alice", "alice"),
        ("arn:aws:sqs:default::bucketeer-hook-alice", "alice"),
        ("arn:aws:sns:default::bucketeer-hook-alice:extra", "alice"),
    ]:
        assert notifications._own_topic(arn, uid, eps) is None, arn


def test_two_users_get_two_topics(env):
    endpoint(env)
    assert put(env, [item()]).status_code == 200
    r = env.bob.get("/api/buckets/bob-shared/notifications")
    assert r.status_code == 200, r.text
    r = env.bob.put("/api/buckets/bob-shared/notifications", headers=H, json={
        "notifications": [item()], "etag": r.json()["etag"]})
    assert r.status_code == 200, r.text
    assert [(t["access"], t["Name"]) for t in env.topics] == [
        (ALICE_KEY, "bucketeer-hook-alice"), (BOB_KEY, "bucketeer-hook-bob")]
    assert r.json()["notifications"][0]["topic"] == "bucketeer-hook-bob"


def test_other_users_topic_is_external(env):
    endpoint(env)
    external(env, topic="bucketeer-hook-bob", nid="n1")
    n = get(env)["notifications"][0]
    assert n["managed"] is False and n["endpoint"] is None
    # sending the managed form for that id replaces it only by the user's own topic
    r = put(env, [{"id": "n1", "external": True}])
    assert r.status_code == 200 and r.json()["changed"] is False and env.topics == []
    assert stored(env)[0]["TopicArn"].endswith(":bucketeer-hook-bob")
    r = put(env, [item()])
    assert r.status_code == 200 and r.json()["changed"] is True
    assert stored(env)[0]["TopicArn"].endswith(":bucketeer-hook-alice")


def test_other_tenant_topic_is_external(env):
    endpoint(env)
    external(env, topic="bucketeer-hook-alice", nid="n1")
    assert get(env)["notifications"][0]["managed"] is True
    cfg = env.moto().get_bucket_notification_configuration(Bucket="alice-data")
    arn = cfg["TopicConfigurations"][0]["TopicArn"]
    parts = arn.split(":")
    assert notifications._own_topic(arn, "alice", [{"name": "hook"}]) == "hook"
    parts[4] = "othertenant"
    assert notifications._own_topic(":".join(parts), "alice", [{"name": "hook"}]) is None
    assert notifications._own_topic(arn, "acme$alice", [{"name": "hook"}]) is None


def test_create_topic_error_not_relayed(env):
    from botocore.exceptions import ClientError
    endpoint(env)

    class Boom:
        def create_topic(self, **kw):
            raise ClientError({"Error": {"Code": "InvalidParameter",
                                          "Message": "bad push-endpoint https://hooks.example.org/in"}},
                              "CreateTopic")

    env.app.state.s3.sns_factory = lambda *a: Boom()
    r = put(env, [item()])
    assert r.status_code == 422 and "hooks.example.org" not in r.text
