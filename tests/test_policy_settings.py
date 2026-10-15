"""Pure helpers in bucketeer/policy_settings.py."""

import time

import pytest

from bucketeer import policy_settings as ps
from bucketeer.db import Database
from bucketeer.policy_settings import SettingsError, validate_bucket_name

NO_RULES = {"prefix": "", "pattern": ""}


@pytest.mark.parametrize("name", [
    "abc", "my-bucket", "a.b.c", "1.2.3", "a" * 63, "bucket-1", "x1y", "a-b.c-d",
    "999.example", "mybucket.s3",
])
def test_valid_dns_names(name):
    assert validate_bucket_name(name, "alice", NO_RULES) == []


@pytest.mark.parametrize("name", [
    "", "ab", "a" * 64, "Abc", "my_bucket", "my bucket", "-abc", "abc-", ".abc", "abc.",
    "a..b", "a.-b", "a-.b", "192.168.5.4", "xn--abc", "sthree-x", "abc-s3alias",
    "abc--ol-s3", "abc--x-s3", "ünï", "a/b", "abc\n", "ab\x00c",
])
def test_invalid_dns_names(name):
    assert validate_bucket_name(name, "alice", NO_RULES), name


def test_prefix_template_and_suggest():
    rules = ps.normalize_naming({"prefix": "{username}-"})
    assert ps.suggest_prefix("Alice", rules) == "alice-"
    assert ps.suggest_prefix("j_doe.x", rules) == "j-doe.x-"
    assert ps.suggest_prefix("alice", NO_RULES) == ""
    assert validate_bucket_name("alice-data", "Alice", rules) == []
    assert any("start with" in e for e in validate_bucket_name("bob-data", "alice", rules))
    # a prefix is not enough: the DNS rules still apply
    assert validate_bucket_name("alice-", "alice", rules)
    assert validate_bucket_name("Alice-data", "alice", rules)


def test_username_that_cannot_be_a_name_fragment():
    rules = ps.normalize_naming({"prefix": "{username}-"})
    errs = validate_bucket_name("x-data", "___", rules)
    assert any("username" in e for e in errs)


def test_static_prefix_and_pattern():
    rules = ps.normalize_naming({"prefix": "lab-", "pattern": r"lab-[a-z]+-\d{4}"})
    assert validate_bucket_name("lab-genomics-2024", "x", rules) == []
    assert validate_bucket_name("lab-genomics", "x", rules)  # fullmatch, not search
    assert validate_bucket_name("xlab-genomics-2024", "x", rules)


def test_pattern_is_fullmatch():
    rules = ps.normalize_naming({"pattern": "[a-z]+"})
    assert validate_bucket_name("abc", "u", rules) == []
    assert validate_bucket_name("abc1", "u", rules)


@pytest.mark.parametrize("raw", [
    {"prefix": "UPPER-"}, {"prefix": "{user}-"}, {"prefix": "a_b"}, {"prefix": "{username"},
    {"prefix": "-{username}"}, {"prefix": "x" * 41}, {"prefix": 5}, {"pattern": 5},
    {"pattern": "("}, {"pattern": "[a-"}, {"pattern": "*abc"}, {"pattern": "x" * 201},
    {"extra": "field"}, [], "text", {"prefix": "a" * 40 + "..", "pattern": ""},
])
def test_bad_naming_rules_rejected(raw):
    with pytest.raises(SettingsError):
        ps.normalize_naming(raw)


def test_catastrophic_pattern_rejected_at_save_and_bounded_at_use():
    evil = "(a|aa)+b"
    t = time.monotonic()
    with pytest.raises(SettingsError, match="too slow"):
        ps.normalize_naming({"pattern": evil})
    assert time.monotonic() - t < 2
    # rules that slipped into the database (hand-edited) still cannot hang a request
    t = time.monotonic()
    errs = validate_bucket_name("a" * 62 + "c", "u", {"prefix": "", "pattern": evil})
    assert time.monotonic() - t < 2
    assert errs


def test_misconfigured_stored_pattern_is_an_error_not_a_crash():
    errs = validate_bucket_name("abc", "u", {"prefix": "", "pattern": "("})
    assert errs and "misconfigured" in errs[0]


def test_whitespace_trimmed_and_empty_means_off():
    assert ps.normalize_naming({"prefix": "  ", "pattern": " "}) == NO_RULES
    assert ps.normalize_naming({}) == NO_RULES


# -- placement ------------------------------------------------------------------------

ROWS = [{"name": "p1"}, {"name": "p1"}, {"name": "p2"}]


def test_placement_names_and_normalize():
    names = ps.placement_names(ROWS)
    assert names == ["p1", "p2"]
    assert ps.normalize_placement({"default": "p2", "allowed": ["p2", "p1"]}, names) == {
        "default": "p2", "allowed": ["p1", "p2"],
    }
    assert ps.normalize_placement({"default": None, "allowed": []}, names)["default"] is None
    assert ps.normalize_placement({"default": ""}, names)["default"] is None
    for bad in ({"default": "nope"}, {"allowed": ["p1", "nope"]}, {"allowed": "p1"},
                {"default": 3}, {"allowed": [1]}, []):
        with pytest.raises(SettingsError):
            ps.normalize_placement(bad, names)


def test_placement_options():
    avail = ["p1", "p2", "p3"]
    # no choice by default: croit's default, no list
    o = ps.placement_options({"default": None, "allowed": []}, "p1", avail)
    assert o == {"default": "p1", "choices": [], "userChoice": False}
    # admin default wins; the allowed list is shown only when it offers >1 option
    o = ps.placement_options({"default": "p2", "allowed": ["p3"]}, "p1", avail)
    assert o["default"] == "p2" and o["choices"] == ["p2", "p3"] and o["userChoice"]
    o = ps.placement_options({"default": None, "allowed": ["p1"]}, "p1", avail)
    assert o["choices"] == ["p1"] and not o["userChoice"]
    # stale entries are dropped
    o = ps.placement_options({"default": "gone", "allowed": ["gone", "p2"]}, "p1", avail)
    assert o["default"] == "p1" and o["choices"] == ["p1", "p2"]
    assert ps.placement_options({"default": None, "allowed": []}, None, [])["default"] is None


# -- quota ----------------------------------------------------------------------------


def test_quota_normalize():
    n = ps.normalize_quota
    assert n({"enabled": True, "maxSize": 0}) == {"enabled": True, "maxSize": 0, "maxObjects": None}
    assert n({"enabled": True, "maxObjects": 5, "maxSize": 10})["maxObjects"] == 5
    off = {"enabled": False, "maxSize": None, "maxObjects": None}
    assert n({"enabled": False, "maxSize": 7}) == off
    for bad in ({"enabled": True}, {"enabled": True, "maxSize": -1},
                {"enabled": True, "maxSize": 2**62 + 1}, {"enabled": True, "maxSize": 1.5},
                {"enabled": True, "maxSize": True}, {"enabled": "yes", "maxSize": 1},
                {"maxSize": 1}, {"enabled": True, "maxSize": 1, "x": 1}, []):
        with pytest.raises(SettingsError):
            n(bad)


def test_quota_croit_args():
    assert ps.quota_croit_args({"enabled": False}) == {"enabled": False}
    assert ps.quota_croit_args({"enabled": True, "maxSize": 0, "maxObjects": None}) == {
        "enabled": True, "max_size": 0, "max_objects": -1,
    }


# -- uids -----------------------------------------------------------------------------


@pytest.mark.parametrize("uid", ["alice", "acme$carol", "a.b-c_d", "x@y.org", "A1", "u" * 64])
def test_valid_rgw_uid(uid):
    assert ps.validate_rgw_uid(uid) == uid


@pytest.mark.parametrize("uid", [
    "", "$x", "t$", "a$b$c", "a b", "a/b", "a%b", "-x", "ten ant$x", "x\n", "u" * 65, None, 3,
    "a\\b", "..", "t$..",
])
def test_invalid_rgw_uid(uid):
    with pytest.raises(SettingsError):
        ps.validate_rgw_uid(uid)


def test_ad_username():
    assert ps.validate_ad_username("j.doe-2") == "j.doe-2"
    for bad in ("", "a b", "a/b", "a@b", "x" * 65, ".x", None):
        with pytest.raises(SettingsError):
            ps.validate_ad_username(bad)


def test_mapping_uniqueness():
    existing = [{"ad_username": "Bob", "rgw_uid": "legacy-bob"}]
    ps.check_mapping_unique("bob", "legacy-bob", existing)  # same user: update
    with pytest.raises(SettingsError, match="already mapped from Bob"):
        ps.check_mapping_unique("carol", "LEGACY-bob", existing)
    with pytest.raises(SettingsError, match="default"):
        ps.check_mapping_unique("carol", "Carol", existing)


# -- endpoints ------------------------------------------------------------------------


def ep(**kw):
    return {"name": "hook", "type": "https", "url": "https://hooks.example.org/in", **kw}


def test_endpoint_ok_per_type():
    assert ps.normalize_endpoint(ep()) == {
        "name": "hook", "type": "https", "url": "https://hooks.example.org/in",
        "topic": "", "verifyTls": True,
    }
    assert ps.normalize_endpoint(ep(type="http", url="http://10.0.0.5:8080/x"))["type"] == "http"
    assert ps.normalize_endpoint(ep(type="amqp", url="amqps://mq.example.org:5671", topic="ex1"))
    with pytest.raises(SettingsError, match="exchange"):
        ps.normalize_endpoint(ep(type="amqp", url="amqp://mq"))
    assert ps.normalize_endpoint(ep(type="kafka", url="kafka://k1.example.org:9092"))
    with pytest.raises(SettingsError, match="per user"):
        ps.normalize_endpoint(ep(type="kafka", url="kafka://k1.example.org:9092", topic="t"))
    assert ps.normalize_endpoint(ep(url="https://[::1]:8443/x"))
    assert ps.normalize_endpoint(ep(verifyTls=False))["verifyTls"] is False


@pytest.mark.parametrize("kw", [
    {"type": "ftp", "url": "ftp://x"}, {"url": "http://hooks.example.org"},  # scheme vs type
    {"type": "http", "url": "https://x.org"}, {"type": "kafka", "url": "http://x:9092"},
    {"type": "amqp", "url": "kafka://x"}, {"url": "https://user:pw@x.org/"},
    {"url": "https://user@x.org/"}, {"url": "https://x.org/?token=abc"},
    {"url": "https://x.org/#frag"}, {"url": "https:///path"}, {"url": "https://"},
    {"url": "https://x.org:99999/"}, {"url": "https://x.org:abc/"}, {"url": "https://-bad-.org/"},
    {"url": "https://bad host/"}, {"url": ""}, {"url": "   "}, {"url": 5},
    {"type": "kafka", "url": "kafka://k:9092/topic"}, {"topic": "bad topic"},
    {"topic": "x" * 201}, {"verifyTls": "yes"}, {"name": ""}, {"name": "-x"},
    {"name": "a b"}, {"name": "x" * 49}, {"unknown": 1}, {"credentials": "x"},
    {"password": "x"},
])
def test_endpoint_rejected(kw):
    with pytest.raises(SettingsError):
        ps.normalize_endpoint(ep(**kw))


def test_endpoint_choices_hide_addresses():
    out = ps.endpoint_choices([ps.normalize_endpoint(ep())])
    assert out == [{"name": "hook", "type": "https"}]


def test_endpoint_capacity():
    existing = [{"name": f"e{i}"} for i in range(ps.MAX_ENDPOINTS)]
    ps.check_endpoint_capacity({"name": "E3"}, existing)  # replace
    with pytest.raises(SettingsError):
        ps.check_endpoint_capacity({"name": "new"}, existing)


# -- loading ---------------------------------------------------------------------------


def test_load_defaults_and_roundtrip():
    db = Database(":memory:")
    s = db.settings
    assert ps.load_naming(s) == NO_RULES
    assert ps.load_placement(s) == {"default": None, "allowed": []}
    assert ps.load_default_quota(s) is None
    assert ps.load_endpoints(s) == []
    s.set(ps.NAMING_KEY, {"prefix": "x-", "junk": 1})
    assert ps.load_naming(s) == {"prefix": "x-", "pattern": ""}
    db.close()


# -- regex compile bounds ---------------------------------------------------------------


@pytest.mark.parametrize("pattern", [
    "a{10000000}", "(?:a{3000}){3000}", "(?:(?:(?:a{63}){63}){63}){63}", "a{64}", "a{1,64}",
    "a{100000000}", "(?:a{20}){20}", "(?#[)a{500000}(?#])", "(?x)#[\na{500000}#]",
    "(?x)a", "(?ix)a", "(?i-x:a)", "(?x:a{500000})", "a#b", "a{e<=1000}", "[a-z]{64}",
    r"\d{70}", "(a{9}){9}{4}",
])
def test_huge_counted_repeats_rejected_fast_without_compiling(pattern, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must be rejected before any compile or subprocess")

    monkeypatch.setattr(ps.regex, "compile", boom)
    monkeypatch.setattr(ps.subprocess, "run", boom)
    t = time.monotonic()
    with pytest.raises(SettingsError):
        ps.normalize_naming({"pattern": pattern})
    assert time.monotonic() - t < 0.5


@pytest.mark.parametrize("pattern", [
    r"[a-z]{1,63}", r"a{2}b{3}", r"(?:ab){10}", r"(?:a{8}){8}", r"\{99999\}", "[{99999}]",
    "x{,5}", "{username}-[a-z]+", r"[a-z0-9]{3,20}-(data|scratch)", "a{2}(b{20}c{20})",
])
def test_static_check_accepts_normal_patterns(pattern):
    ps.check_pattern_static(pattern)


def test_subprocess_backstop_limits_memory_and_time(monkeypatch):
    # bypass the static check: the memory-capped subprocess must still refuse
    monkeypatch.setattr(ps, "check_pattern_static", lambda p: None)
    monkeypatch.setattr(ps, "PROBE_MEMORY", 128 * 2**20)
    t = time.monotonic()
    with pytest.raises(SettingsError):
        ps.approve_pattern("a{100000000}")
    assert time.monotonic() - t < 5
    monkeypatch.setattr(ps, "PROBE_TIMEOUT", 0.001)  # wall-clock kill path
    with pytest.raises(SettingsError, match="too slow or too complex"):
        ps.approve_pattern("never-seen-pattern-[a-z]+")


def test_approved_patterns_are_memoized(monkeypatch):
    ps.approve_pattern("memo-[a-z]+")
    monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: 1 / 0)
    ps.approve_pattern("memo-[a-z]+")  # no second subprocess
    assert validate_bucket_name("memo-abc", "u", {"prefix": "", "pattern": "memo-[a-z]+"}) == []


def test_scoped_flags_without_x_still_work():
    ps.check_pattern_static("(?i:abc)-[a-z]+")
    ps.check_pattern_static("(?P<x>abc)(?:def)")
    ps.check_pattern_static("(?V1)abc")
    assert ps.normalize_naming({"pattern": "(?i:abc)-[a-z]+"})["pattern"]


def test_compile_memory_growth_check_refuses(monkeypatch):
    monkeypatch.setattr(ps, "PROBE_GROWTH_KB", -1)  # stub: even zero growth is refused
    with pytest.raises(SettingsError, match="too much memory"):
        ps.approve_pattern("grow-" + "[a-z0-9-]{1,40}" * 3 + "-x")


def test_real_small_pattern_approved_within_growth_limit():
    ps.approve_pattern("small-[a-z]{1,10}")
