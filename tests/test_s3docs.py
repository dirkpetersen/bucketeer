"""Shared-document helpers: pure logic and read-modify-write against moto."""

import copy
import json
import random
import threading

import pytest
from botocore.exceptions import ClientError

from bucketeer import s3docs as d
from mock.s3_bridge import S3Bridge

FOREIGN = [
    {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"},
    {"Sid": "PublicRead", "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": "*"},
    {"Sid": "bucketeerish", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
     "Resource": "*"},
]


def stmt(sid, **extra):
    return {"Sid": sid, "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam:::user/x"},
            "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*", **extra}


def pol(*statements):
    return {"Version": "2012-10-17", "Statement": list(statements)}


# --------------------------------------------------------------------- policy

@pytest.mark.parametrize("raw", [None, "", "  ", "{}", {}])
def test_missing_policy_is_new_doc(raw):
    assert d.parse_policy(raw) is None
    assert d.upsert_statements(raw, [stmt("bucketeer-a")]) == pol(stmt("bucketeer-a"))
    assert d.remove_statements(raw, ["bucketeer-a"]) is None


def test_parse_errors():
    for bad in ("not json", "[]", '"x"'):
        with pytest.raises(d.S3DocError):
            d.parse_policy(bad)
    with pytest.raises(d.S3DocError):
        d.parse_policy({"Statement": ["x"]})
    with pytest.raises(d.S3DocError):
        d.parse_policy({"Statement": "x"})


def test_statement_as_object_is_normalized_and_foreign_kept():
    doc = {"Version": "2012-10-17", "Id": "keepme", "Statement": FOREIGN[1]}
    out = d.upsert_statements(doc, [stmt("bucketeer-a")])
    assert out == {"Version": "2012-10-17", "Id": "keepme",
                   "Statement": [FOREIGN[1], stmt("bucketeer-a")]}
    removed = d.remove_statements(json.dumps(doc), ["bucketeer-a"])
    assert removed == pol(FOREIGN[1]) | {"Id": "keepme"}


def test_json_string_input_keeps_missing_version():
    out = d.upsert_statements(json.dumps({"Statement": [FOREIGN[0]]}), [stmt("bucketeer-a")])
    assert "Version" not in out
    assert out["Statement"][0] == FOREIGN[0]


def test_input_not_mutated():
    doc = pol(*FOREIGN, stmt("bucketeer-a"))
    before = copy.deepcopy(doc)
    d.upsert_statements(doc, [stmt("bucketeer-a", Effect="Deny"), stmt("bucketeer-b")])
    d.remove_statements(doc, ["bucketeer-a"])
    assert doc == before


def test_replace_in_place_and_append():
    doc = pol(FOREIGN[0], stmt("bucketeer-a"), FOREIGN[1], stmt("bucketeer-b"))
    out = d.upsert_statements(doc, [stmt("bucketeer-b", Effect="Deny"), stmt("bucketeer-c")])
    assert [s.get("Sid") for s in out["Statement"]] == [None, "bucketeer-a", "PublicRead",
                                                         "bucketeer-b", "bucketeer-c"]
    assert out["Statement"][3]["Effect"] == "Deny"


@pytest.mark.parametrize(
    "sid", ["PublicRead", "", None, "bucketeer", "Bucketeer-a", "xbucketeer-a"])
def test_upsert_refuses_foreign_sid(sid):
    with pytest.raises(d.ForeignIdError):
        d.upsert_statements(None, [stmt("bucketeer-ok"), {**stmt("x"), "Sid": sid}])
    with pytest.raises(d.ForeignIdError):
        d.upsert_statements(pol(FOREIGN[1]), [{k: v for k, v in stmt("x").items() if k != "Sid"}])


def test_remove_refuses_foreign_sid_list_but_predicate_is_filtered():
    doc = pol(FOREIGN[1], stmt("bucketeer-a"))
    with pytest.raises(d.ForeignIdError):
        d.remove_statements(doc, ["PublicRead"])
    # a sloppy predicate still cannot remove foreign statements
    assert d.remove_statements(doc, lambda sid: True) == pol(FOREIGN[1])
    assert d.remove_statements(doc, lambda sid: sid.startswith("bucketeer-")) == pol(FOREIGN[1])


def test_duplicate_sids_raise_everywhere():
    doc = pol(stmt("A"), stmt("A"))
    for call in (lambda: d.parse_policy(doc),
                 lambda: d.upsert_statements(doc, [stmt("bucketeer-a")]),
                 lambda: d.remove_statements(doc, ["bucketeer-a"])):
        with pytest.raises(d.DuplicateIdError):
            call()
    with pytest.raises(d.DuplicateIdError):
        d.upsert_statements(None, [stmt("bucketeer-a"), stmt("bucketeer-a")])


@pytest.mark.parametrize("sid", [1, 1.5, True, ["a"], {"a": 1}])
def test_non_string_ids_raise_s3doc_error(sid):
    with pytest.raises(d.S3DocError):
        d.parse_policy(pol({"Sid": sid, "Effect": "Allow"}))
    with pytest.raises(d.S3DocError):
        d.parse_lifecycle({"Rules": [{"ID": sid, "Status": "Enabled"}]})
    with pytest.raises(d.S3DocError):
        d.remove_statements(pol({"Sid": sid}), ["bucketeer-a"])


def test_empty_statement_list_is_no_policy():
    assert d.parse_policy({"Version": "2012-10-17", "Statement": []}) is None
    assert d.upsert_statements(None, []) is None
    assert d.upsert_rules(None, []) is None
    assert d.upsert_tags(None, []) is None


def test_statements_without_sid_are_not_duplicates():
    doc = pol(FOREIGN[0], dict(FOREIGN[0]))
    assert d.upsert_statements(doc, [stmt("bucketeer-a")])["Statement"][:2] == [FOREIGN[0]] * 2


def test_last_removal_signals_delete_but_foreign_keeps_policy():
    assert d.remove_statements(pol(stmt("bucketeer-a")), ["bucketeer-a"]) is None
    assert d.remove_statements(pol(stmt("bucketeer-a"), stmt("bucketeer-b")),
                               "bucketeer-a") == pol(stmt("bucketeer-b"))
    mixed = pol(FOREIGN[0], stmt("bucketeer-a"))
    assert d.remove_statements(mixed, ["bucketeer-a"]) == pol(FOREIGN[0])
    # removing something absent leaves the document alone
    assert d.remove_statements(pol(FOREIGN[0]), ["bucketeer-zz"]) == pol(FOREIGN[0])


def test_foreign_preserved_in_order_through_random_ops():
    rng = random.Random(7)
    doc = pol(*FOREIGN)
    expected_foreign = copy.deepcopy(FOREIGN)
    for i in range(300):
        sid = f"bucketeer-{rng.randrange(6)}"
        if rng.random() < 0.6:
            doc = d.upsert_statements(doc, [stmt(sid, N=i)])
        else:
            doc = d.remove_statements(doc, [sid]) or pol()
        foreign = [s for s in doc["Statement"] if not d.is_managed(s.get("Sid"))]
        assert foreign == expected_foreign
        sids = [s["Sid"] for s in doc["Statement"] if "Sid" in s]
        assert len(sids) == len(set(sids))
    assert [s for s in doc["Statement"][:3]] == FOREIGN  # foreign never moved


def test_managed_statements():
    assert d.managed_statements(None) == []
    assert d.managed_statements(pol(FOREIGN[1], stmt("bucketeer-a"))) == [stmt("bucketeer-a")]


# ------------------------------------------------------------------- builders

def test_share_statement_exact():
    assert json.dumps(d.share_statement("data", "alice", None, "read")) == json.dumps({
        "Sid": "bucketeer-share-u-alice",
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam:::user/alice"},
        "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket",
                   "s3:ListBucketVersions", "s3:GetBucketLocation"],
        "Resource": ["arn:aws:s3:::data", "arn:aws:s3:::data/*"],
    })


def test_readwrite_and_tenant_and_key():
    s = d.bucket_key_statement("data", "alice+backup-job", "acme", "readwrite", 12)
    assert s["Sid"] == "bucketeer-key-12"
    assert s["Principal"] == {"AWS": "arn:aws:iam::acme:user/alice+backup-job"}
    assert s["Action"] == [*d.READ_ACTIONS, *d.WRITE_ACTIONS]
    assert d.share_statement("b", "u", "", "read")["Principal"]["AWS"] == "arn:aws:iam:::user/u"


def test_actions_are_never_admin():
    for a in (*d.READ_ACTIONS, *d.WRITE_ACTIONS):
        assert not any(w in a for w in ("Policy", "Lifecycle", "DeleteBucket", "Acl", "*"))
    assert set(d.READ_ACTIONS).isdisjoint(d.WRITE_ACTIONS)


@pytest.mark.parametrize("args", [
    ("b", "alice", None, "full"), ("b", "", None, "read"), ("b", "a$b", None, "read"),
    ("", "alice", None, "read"), ("a/b", "alice", None, "read"), ("b", "a b", None, "read"),
    ("b", "*", None, "read"), ("b", 'a"b', None, "read"), ("b", "a:b", None, "read"),
    ("b", "a/b", None, "read"), ("b", "alice", "ac me", "read"),
    ("b", "alice", "acme:root", "read"),
    ("b", "alice", "a-b", "read"), ("b", "alice", "a/b", "read"), ("b", "alice", "*", "read"),
    ("b", "alice", 'a"', "read"), ("b", "alice", "acme$", "read"),
])
def test_builder_validation(args):
    with pytest.raises(d.S3DocError):
        d.share_statement(*args)


@pytest.mark.parametrize("uid", ["*", 'a"b', "a b", "a:b", "a/b", "", "a$b"])
@pytest.mark.parametrize("tenant", [None, "acme", "a b", "a:b", "*"])
def test_bucket_key_validation(uid, tenant):
    with pytest.raises(d.S3DocError):
        d.bucket_key_statement("b", uid, tenant, "read", 1)


def test_bucket_key_bad_key_id_and_bucket():
    for kid in ("a b", "", "*", 'x"'):
        with pytest.raises(d.S3DocError):
            d.bucket_key_statement("b", "a+k", None, "read", kid)
    with pytest.raises(d.S3DocError):
        d.share_statement("b b", "k", None, "read")


def test_share_sid_is_unambiguous():
    uids = ["alice", "t-acme-alice", "u-alice", "acme-alice", "a.b", "a_b", "a-b", "t-x", "x"]
    tenants = [None, "", "acme", "t", "u", "a_b", "acme1"]
    seen = {}
    for tenant in tenants:
        for uid in uids:
            sid = d.share_sid(uid, tenant)
            key = (tenant or "", uid)
            assert seen.setdefault(sid, key) == key, (sid, seen[sid], key)
    assert d.share_sid("alice") != d.share_sid("alice", "acme")
    assert d.share_statement("b", "alice", "acme", "read")["Sid"] == "bucketeer-share-t-acme-alice"
    # the Sid and the principal agree
    st = d.share_statement("b", "alice", "acme", "read")
    assert st["Principal"]["AWS"] == "arn:aws:iam::acme:user/alice"


def test_builders_pass_upsert_guard():
    doc = d.upsert_statements(None, [d.share_statement("b", "alice", None, "read"),
                                     d.bucket_key_statement("b", "a+k", None, "read", "7")])
    assert [s["Sid"] for s in doc["Statement"]] == ["bucketeer-share-u-alice", "bucketeer-key-7"]


# ------------------------------------------------------------------ lifecycle

def rule(rid, **extra):
    return {"ID": rid, "Status": "Enabled", "Filter": {"Prefix": ""},
            "Expiration": {"Days": 5}, **extra}


FOREIGN_RULES = [
    {"ID": "mine", "Status": "Enabled", "Filter": {"Prefix": "tmp/"}, "Expiration": {"Days": 1}},
    {"Status": "Disabled", "Filter": {"Prefix": "x/"}, "Expiration": {"Days": 2}},
]


def test_lifecycle_builders_exact():
    assert d.expire_current_rule(30) == {"ID": "bucketeer-expire-current", "Status": "Enabled",
                                         "Filter": {"Prefix": ""}, "Expiration": {"Days": 30}}
    assert d.expire_noncurrent_rule(7)["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}
    assert d.abort_multipart_rule(3) == {
        "ID": "bucketeer-abort-multipart", "Status": "Enabled", "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}


@pytest.mark.parametrize("n", [0, -1, 1.5, "7", None, True, d.MAX_DAYS + 1])
def test_days_validation(n):
    for f in (d.expire_current_rule, d.expire_noncurrent_rule, d.abort_multipart_rule):
        with pytest.raises(d.S3DocError):
            f(n)
    assert d.expire_current_rule(d.MAX_DAYS)["Expiration"]["Days"] == d.MAX_DAYS


def test_lifecycle_upsert_remove_semantics():
    cfg = {"Rules": copy.deepcopy(FOREIGN_RULES), "ResponseMetadata": {"x": 1},
           "TransitionDefaultMinimumObjectSize": "varies_by_storage_class"}
    out = d.upsert_rules(cfg, [d.expire_current_rule(5)])
    assert "ResponseMetadata" not in out
    assert out["Rules"][:2] == FOREIGN_RULES and out["Rules"][2]["ID"] == "bucketeer-expire-current"
    out = d.upsert_rules(out, [d.expire_current_rule(9), d.abort_multipart_rule(2)])
    assert [r.get("ID") for r in out["Rules"]] == [
        "mine", None, "bucketeer-expire-current", "bucketeer-abort-multipart"]
    assert out["Rules"][2]["Expiration"] == {"Days": 9}
    out = d.remove_rules(out, lambda i: True)
    assert out == {"Rules": FOREIGN_RULES}


def test_lifecycle_empty_missing_foreign_duplicate():
    assert d.parse_lifecycle(None) is None and d.parse_lifecycle({"Rules": []}) is None
    assert d.remove_rules(None, ["bucketeer-a"]) is None
    assert d.remove_rules({"Rules": [rule("bucketeer-a")]}, "bucketeer-a") is None
    assert d.upsert_rules(None, [rule("bucketeer-a")]) == {"Rules": [rule("bucketeer-a")]}
    with pytest.raises(d.ForeignIdError):
        d.upsert_rules(None, [rule("mine")])
    with pytest.raises(d.ForeignIdError):
        d.remove_rules({"Rules": [rule("mine")]}, ["mine"])
    with pytest.raises(d.DuplicateIdError):
        d.upsert_rules({"Rules": [rule("a"), rule("a")]}, [rule("bucketeer-a")])
    with pytest.raises(d.DuplicateIdError):
        d.upsert_rules(None, [rule("bucketeer-a"), rule("bucketeer-a")])
    with pytest.raises(d.S3DocError):
        d.parse_lifecycle({"Rules": ["x"]})


def test_lifecycle_foreign_preserved_random():
    rng = random.Random(3)
    cfg = {"Rules": copy.deepcopy(FOREIGN_RULES)}
    for i in range(200):
        rid = f"bucketeer-{rng.randrange(5)}"
        cfg = (d.upsert_rules(cfg, [rule(rid, N=i)]) if rng.random() < 0.6
               else d.remove_rules(cfg, [rid]))
        assert [r for r in cfg["Rules"] if not d.is_managed(r.get("ID"))] == FOREIGN_RULES


# ----------------------------------------------------------------------- tags

def test_tags():
    foreign = [{"Key": "team", "Value": "x"}]
    out = d.upsert_tags(foreign, [{"Key": "bucketeer-budget", "Value": "1"}])
    assert out == foreign + [{"Key": "bucketeer-budget", "Value": "1"}]
    out = d.upsert_tags(out, [{"Key": "bucketeer-budget", "Value": "2"}])
    assert out[1]["Value"] == "2" and len(out) == 2
    assert d.remove_tags(out, ["bucketeer-budget"]) == foreign
    assert d.remove_tags([{"Key": "bucketeer-a", "Value": ""}], ["bucketeer-a"]) is None
    assert d.remove_tags(None, ["bucketeer-a"]) is None
    with pytest.raises(d.ForeignIdError):
        d.upsert_tags(None, [{"Key": "team", "Value": "y"}])
    with pytest.raises(d.ForeignIdError):
        d.remove_tags(foreign, ["team"])
    assert d.remove_tags(foreign, lambda k: True) == foreign
    with pytest.raises(d.DuplicateIdError):
        d.upsert_tags([{"Key": "a", "Value": ""}, {"Key": "a", "Value": ""}], [])


# ------------------------------------------------------------- moto integration

BUCKET = "docs-bucket"


@pytest.fixture
def s3(s3_endpoint):
    client = S3Bridge(s3_endpoint).client("any", "any")
    client.create_bucket(Bucket=BUCKET)
    return client


def drop_share(p):
    return d.remove_statements(p, [d.share_sid("alice"), d.share_sid("alice", "acme")])


def drop_rules(c):
    return d.remove_rules(c, lambda i: True)


def add_noncurrent(c):
    return d.upsert_rules(c, [d.expire_noncurrent_rule(5)])


def raw_policy(s3):
    return json.loads(s3.get_bucket_policy(Bucket=BUCKET)["Policy"])


def test_policy_roundtrip(s3):
    foreign = {"Sid": "Other", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
               "Resource": "arn:aws:s3:::b/*"}
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(pol(foreign)))
    share = d.share_statement(BUCKET, "alice", None, "read")
    d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [share]))
    assert raw_policy(s3)["Statement"] == [foreign, share]
    d.update_bucket_policy(s3, BUCKET, drop_share)
    assert raw_policy(s3)["Statement"] == [foreign]
    d.update_bucket_policy(s3, BUCKET, drop_share)  # no-op
    assert raw_policy(s3)["Statement"] == [foreign]


def test_policy_created_and_deleted_when_last_managed_removed(s3):
    share = d.share_statement(BUCKET, "alice", "acme", "readwrite")
    out = d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [share]))
    assert out["Statement"] == [share] and raw_policy(s3)["Statement"] == [share]
    assert d.update_bucket_policy(s3, BUCKET, drop_share) is None
    with pytest.raises(ClientError) as e:
        s3.get_bucket_policy(Bucket=BUCKET)
    assert e.value.response["Error"]["Code"] == "NoSuchBucketPolicy"
    # removing from a bucket without any policy is fine
    assert d.update_bucket_policy(s3, BUCKET, drop_share) is None


def test_policy_other_errors_propagate(s3):
    with pytest.raises(ClientError):
        d.update_bucket_policy(s3, "nope-bucket", lambda p: p)


def test_mutation_error_writes_nothing(s3):
    d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [stmt("bucketeer-a")]))
    with pytest.raises(d.ForeignIdError):
        d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [stmt("Evil")]))
    assert [s["Sid"] for s in raw_policy(s3)["Statement"]] == ["bucketeer-a"]


def test_lifecycle_roundtrip(s3):
    s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": [
        {"ID": "mine", "Status": "Enabled", "Filter": {"Prefix": "tmp/"},
         "Expiration": {"Days": 1}}]})
    d.update_bucket_lifecycle(s3, BUCKET, lambda c: d.upsert_rules(
        c, [d.expire_current_rule(30), d.abort_multipart_rule(7)]))
    rules = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    assert [r["ID"] for r in rules] == [
        "mine", "bucketeer-expire-current", "bucketeer-abort-multipart"]
    d.update_bucket_lifecycle(s3, BUCKET, drop_rules)
    rules = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
    assert [r["ID"] for r in rules] == ["mine"]


def test_lifecycle_deleted_when_last_managed_removed(s3):
    d.update_bucket_lifecycle(s3, BUCKET, add_noncurrent)
    assert d.update_bucket_lifecycle(s3, BUCKET, drop_rules) is None
    with pytest.raises(ClientError) as e:
        s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)
    assert e.value.response["Error"]["Code"] == "NoSuchLifecycleConfiguration"
    assert d.update_bucket_lifecycle(s3, BUCKET, drop_rules) is None


def test_tagging_roundtrip(s3):
    def add_tag(t):
        return d.upsert_tags(t, [{"Key": "bucketeer-b", "Value": "1"}])

    s3.put_bucket_tagging(Bucket=BUCKET, Tagging={"TagSet": [{"Key": "team", "Value": "x"}]})
    d.update_bucket_tagging(s3, BUCKET, add_tag)
    assert len(s3.get_bucket_tagging(Bucket=BUCKET)["TagSet"]) == 2
    d.update_bucket_tagging(s3, BUCKET, lambda t: d.remove_tags(t, lambda k: True))
    assert s3.get_bucket_tagging(Bucket=BUCKET)["TagSet"] == [{"Key": "team", "Value": "x"}]
    s3.delete_bucket_tagging(Bucket=BUCKET)
    d.update_bucket_tagging(s3, BUCKET, add_tag)
    assert d.update_bucket_tagging(s3, BUCKET, lambda t: d.remove_tags(t, lambda k: True)) is None
    with pytest.raises(ClientError):
        s3.get_bucket_tagging(Bucket=BUCKET)


def test_concurrent_updates_do_not_lose_writes(s3_endpoint, s3):
    n = 12
    errors = []

    def worker(i):
        try:
            c = S3Bridge(s3_endpoint).client("any", "any")
            d.update_bucket_policy(c, BUCKET, lambda p: d.upsert_statements(
                p, [d.share_statement(BUCKET, f"user{i}", None, "read")]))
            d.update_bucket_lifecycle(c, BUCKET, lambda cfg: d.upsert_rules(
                cfg, [{**rule(f"bucketeer-r{i}")}]))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(raw_policy(s3)["Statement"]) == n
    assert len(s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]) == n


def test_bucket_lock_is_per_bucket_and_reentrant():
    assert d.bucket_lock("x") is d.bucket_lock("x")
    assert d.bucket_lock("x") is not d.bucket_lock("y")
    with d.bucket_lock("x"), d.bucket_lock("x"):
        pass


# ----------------------------------------------------- foreign-entry guard (moto)

def put_foreign(s3):
    foreign = {"Sid": "Other", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
               "Resource": "arn:aws:s3:::docs-bucket/*"}
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps({**pol(foreign), "Id": "pid"}))
    return foreign


def test_forgotten_return_does_not_delete_foreign_policy(s3):
    foreign = put_foreign(s3)

    def forgetful(p):
        d.upsert_statements(p, [stmt("bucketeer-a")])  # no return

    with pytest.raises(d.ForeignIdError):
        d.update_bucket_policy(s3, BUCKET, forgetful)
    assert raw_policy(s3)["Statement"] == [foreign]


def test_edited_foreign_statement_refused_unless_allowed(s3):
    foreign = put_foreign(s3)

    def edit(p):
        p = copy.deepcopy(p)
        p["Statement"][0]["Effect"] = "Deny"
        return p

    with pytest.raises(d.ForeignIdError):
        d.update_bucket_policy(s3, BUCKET, edit)
    assert raw_policy(s3)["Statement"] == [foreign]
    d.update_bucket_policy(s3, BUCKET, edit, allow_foreign=True)
    assert raw_policy(s3)["Statement"][0]["Effect"] == "Deny"
    d.update_bucket_policy(s3, BUCKET, lambda p: None, allow_foreign=True)
    with pytest.raises(ClientError):
        s3.get_bucket_policy(Bucket=BUCKET)


def test_top_level_policy_fields_and_reorder_guarded(s3):
    put_foreign(s3)

    def change_id(p):
        return {**p, "Id": "other"}

    with pytest.raises(d.ForeignIdError):
        d.update_bucket_policy(s3, BUCKET, change_id)
    d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [stmt("bucketeer-a")]))
    assert raw_policy(s3)["Id"] == "pid"

    def rename_foreign(p):
        return {**p, "Statement": [{**p["Statement"][0], "Sid": "Renamed"}, *p["Statement"][1:]]}

    with pytest.raises(d.ForeignIdError):
        d.update_bucket_policy(s3, BUCKET, rename_foreign)


def test_empty_statement_result_deletes_not_puts(s3):
    d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [stmt("bucketeer-a")]))
    d.update_bucket_policy(s3, BUCKET, lambda p: {"Version": "2012-10-17", "Statement": []})
    with pytest.raises(ClientError):
        s3.get_bucket_policy(Bucket=BUCKET)
    assert d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [])) is None


def test_lifecycle_and_tag_guards(s3):
    s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": [
        {"ID": "mine", "Status": "Enabled", "Filter": {"Prefix": "t/"},
         "Expiration": {"Days": 1}}]})
    with pytest.raises(d.ForeignIdError):
        d.update_bucket_lifecycle(s3, BUCKET, lambda c: None)
    assert len(s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]) == 1
    d.update_bucket_lifecycle(s3, BUCKET, lambda c: None, allow_foreign=True)
    with pytest.raises(ClientError):
        s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)
    s3.put_bucket_tagging(Bucket=BUCKET, Tagging={"TagSet": [{"Key": "team", "Value": "x"}]})
    with pytest.raises(d.ForeignIdError):
        d.update_bucket_tagging(s3, BUCKET, lambda t: [{"Key": "team", "Value": "y"}])
    assert s3.get_bucket_tagging(Bucket=BUCKET)["TagSet"][0]["Value"] == "x"
    d.update_bucket_tagging(s3, BUCKET, lambda t: None, allow_foreign=True)


def test_policy_without_version_stays_without_version(s3):
    foreign = put_foreign(s3)
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps({"Statement": [foreign]}))
    share = d.share_statement(BUCKET, "alice", None, "read")
    d.update_bucket_policy(s3, BUCKET, lambda p: d.upsert_statements(p, [share]))
    assert "Version" not in raw_policy(s3)
    assert raw_policy(s3)["Statement"] == [foreign, share]
    d.update_bucket_policy(s3, BUCKET, lambda p: d.remove_statements(p, [share["Sid"]]))
    assert "Version" not in raw_policy(s3)
    assert raw_policy(s3)["Statement"] == [foreign]
    assert "Version" not in d.upsert_statements({"Statement": [foreign]}, [share])
    assert "Version" not in d.remove_statements({"Statement": [foreign, share]}, [share["Sid"]])
