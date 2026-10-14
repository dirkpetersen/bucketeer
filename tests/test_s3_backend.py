"""The mock croit API and a moto S3 server stay in line (mock/s3_bridge.py)."""

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from mock.croit_mock import ADMIN_TOKEN, MockHooks, create_app
from mock.s3_bridge import S3Bridge

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def bridge(s3_endpoint):
    return S3Bridge(s3_endpoint)


@pytest.fixture
def c(bridge):
    app = create_app(seed=True, task_seconds=0, hooks=bridge.make_hooks(MockHooks),
                     rgw_endpoint=bridge.host_port)
    bridge.seed_objects(app.state.croit)
    return TestClient(app, headers=ADMIN)


def user_s3(c, bridge, uid):
    """An S3 client using the user's own first key, as croit reports it."""
    key = c.get(f"/s3/users/{uid.replace('$', '%24')}/keys").json()[0]
    return bridge.client(key["access_key"], key["secret_key"])


def moto_buckets(bridge):
    s3 = bridge.client("any", "any")
    return {b["Name"] for b in s3.list_buckets()["Buckets"]}


def delete_nonempty(c, name):
    warning = c.request("DELETE", f"/s3/buckets/{name}", json={"name": name}).json()
    sentence = c.app.state.croit.delete_sentence(name)
    return c.request("DELETE", f"/s3/buckets/{name}",
                     params={"ignoreWarning": warning["ignorableWarning"]["token"]},
                     json={"name": name, "deleteData": True, "thisWillDeleteData": sentence})


def test_seeded_buckets_exist_in_moto(c, bridge):
    # tenant bucket acme:reports is addressed as "reports" by its tenant user
    assert moto_buckets(bridge) == {"alice-data", "alice-empty", "bob-shared", "reports"}


def test_seeded_objects_match_croit_usage(c, bridge):
    s3 = user_s3(c, bridge, "alice")
    objs = s3.list_objects_v2(Bucket="alice-data")
    assert objs["KeyCount"] == 42
    assert sum(o["Size"] for o in objs["Contents"]) == 1048576
    usage = next(b for b in c.get("/s3/buckets").json() if b["bucket"] == "alice-data")
    assert usage["usageSum"]["numObjects"] == 42 and usage["usageSum"]["size"] == 1048576
    assert s3.list_objects_v2(Bucket="alice-empty")["KeyCount"] == 0


def test_users_own_keys_work_against_moto(c, bridge):
    s3 = user_s3(c, bridge, "bob")
    s3.put_object(Bucket="bob-shared", Key="hello.txt", Body=b"hi")
    assert s3.get_object(Bucket="bob-shared", Key="hello.txt")["Body"].read() == b"hi"
    assert s3.list_objects_v2(Bucket="bob-shared")["KeyCount"] == 1
    carol = user_s3(c, bridge, "acme$carol")
    assert carol.list_objects_v2(Bucket="reports")["KeyCount"] == 0


def test_croit_create_appears_in_moto(c, bridge):
    r = c.put("/s3/buckets/new-plain", json={"owner": "alice"})
    assert r.status_code == 200
    c.put("/s3/buckets/new-versioned", json={"owner": "bob", "versioning": "ENABLED"})
    c.put("/s3/buckets/new-locked", json={"owner": "alice", "objectLock": True})
    s3 = bridge.client("any", "any")
    assert {"new-plain", "new-versioned", "new-locked"} <= moto_buckets(bridge)
    assert "Status" not in s3.get_bucket_versioning(Bucket="new-plain")
    assert s3.get_bucket_versioning(Bucket="new-versioned")["Status"] == "Enabled"
    assert s3.get_bucket_versioning(Bucket="new-locked")["Status"] == "Enabled"
    lock = s3.get_object_lock_configuration(Bucket="new-locked")
    assert lock["ObjectLockConfiguration"]["ObjectLockEnabled"] == "Enabled"
    with pytest.raises(ClientError):
        s3.get_object_lock_configuration(Bucket="new-plain")


def test_new_user_with_fresh_key_can_own_a_bucket(c, bridge):
    c.post("/s3/users", json={"uid": "zed", "name": "Zed"})
    c.put("/s3/buckets/zed-bucket", json={"owner": "zed"})
    s3 = user_s3(c, bridge, "zed")
    s3.put_object(Bucket="zed-bucket", Key="k", Body=b"v")
    assert "zed-bucket" in moto_buckets(bridge)


def test_croit_versioning_change_reaches_moto(c, bridge):
    s3 = bridge.client("any", "any")
    c.patch("/s3/buckets/alice-empty", json={"versioning": "ENABLED"})
    assert s3.get_bucket_versioning(Bucket="alice-empty")["Status"] == "Enabled"
    c.patch("/s3/buckets/alice-empty", json={"versioning": "OFF"})  # becomes SUSPENDED
    assert s3.get_bucket_versioning(Bucket="alice-empty")["Status"] == "Suspended"


def test_delete_empty_bucket_removes_it_from_moto(c, bridge):
    assert c.request("DELETE", "/s3/buckets/alice-empty",
                     json={"name": "alice-empty"}).status_code == 200
    assert "alice-empty" not in moto_buckets(bridge)


def test_delete_non_empty_bucket_removes_objects_and_bucket(c, bridge):
    s3 = user_s3(c, bridge, "alice")
    s3.put_bucket_versioning(Bucket="alice-data",
                             VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="alice-data", Key="data/file-0000.bin", Body=b"v2")  # a 2nd version
    assert delete_nonempty(c, "alice-data").status_code == 200
    assert "alice-data" not in moto_buckets(bridge)
    # same name again starts empty: no objects or versions survived
    c.put("/s3/buckets/alice-data", json={"owner": "alice"})
    assert s3.list_objects_v2(Bucket="alice-data")["KeyCount"] == 0
    assert s3.list_object_versions(Bucket="alice-data").get("Versions") is None


def test_key_add_and_remove_are_tracked(c, bridge):
    c.put("/s3/users/bob/keys/BOBNEWKEY00000000001", params={"secretKey": "bob-new-secret"})
    assert bridge._keys["bob"]["BOBNEWKEY00000000001"] == "bob-new-secret"
    c.delete("/s3/users/bob/keys/BOBNEWKEY00000000001")
    assert "BOBNEWKEY00000000001" not in bridge._keys["bob"]
    assert "BOBACCESSKEY0000002" in bridge._keys["bob"]


def test_cfg_file_points_at_moto(c, bridge):
    text = c.get("/s3/users/alice/cfg-file/download").json()["fileContents"]
    assert f"host_base = {bridge.host_port}" in text


def test_plain_delete_of_bucket_with_s3_uploaded_objects_is_refused(c, bridge):
    s3 = user_s3(c, bridge, "alice")
    for i in range(5):
        s3.put_object(Bucket="alice-empty", Key=f"k{i}", Body=b"12345")
    r = c.request("DELETE", "/s3/buckets/alice-empty", json={"name": "alice-empty"})
    assert r.status_code == 400 and "ignorableWarning" in r.json()
    assert s3.list_objects_v2(Bucket="alice-empty")["KeyCount"] == 5
    assert c.get("/s3/buckets/alice-empty").status_code == 200
    # the full flow still works and wipes moto
    assert delete_nonempty(c, "alice-empty").status_code == 200
    assert "alice-empty" not in moto_buckets(bridge)


def test_croit_usage_reflects_moto_contents(c, bridge):
    def usage(name):
        return next(b for b in c.get("/s3/buckets").json() if b["bucket"] == name)["usageSum"]

    s3 = user_s3(c, bridge, "alice")
    assert usage("alice-empty")["numObjects"] == 0
    s3.put_object(Bucket="alice-empty", Key="a", Body=b"x" * 100)
    s3.put_object(Bucket="alice-empty", Key="b", Body=b"y" * 28)
    assert usage("alice-empty")["numObjects"] == 2 and usage("alice-empty")["size"] == 128
    assert usage("alice-data")["numObjects"] == 42 and usage("alice-data")["size"] == 1048576
    entry = c.get("/s3/usage/by-bucket/alice-empty", params={"hours": 1}).json()
    assert entry["buckets"]["alice"][0]["bytesUploaded"] == 128


def test_failed_s3_delete_keeps_bucket_in_croit_and_moto(c, bridge):
    from datetime import datetime, timedelta, timezone

    c.put("/s3/buckets/locked-bkt", json={"owner": "alice", "objectLock": True})
    s3 = user_s3(c, bridge, "alice")
    s3.put_object(Bucket="locked-bkt", Key="keep", Body=b"data", ObjectLockMode="COMPLIANCE",
                  ObjectLockRetainUntilDate=datetime.now(timezone.utc) + timedelta(days=1))
    r = delete_nonempty(c, "locked-bkt")
    assert r.status_code >= 400 and "message" in r.json() and r.json()["code"] == r.status_code
    assert c.get("/s3/buckets/locked-bkt").status_code == 200
    assert "locked-bkt" in moto_buckets(bridge)
    assert s3.list_objects_v2(Bucket="locked-bkt")["KeyCount"] == 1
