"""Admin-editable runtime policy: validation, normalisation and pure helpers.

Everything here is synchronous and free of I/O except the `load_*` functions,
which read the `settings` table. Later features (create bucket, auto-create
users, notifications) call these helpers; nothing in this module applies a
setting to croit or RGW by itself.

Stored in the `settings` table (JSON values, caller-supplied defaults):

* `naming`                 {prefix: str, pattern: str}
* `placement`              {default: str|None, allowed: [str]}
* `default_quota`          absent = the built-in default (1 TiB, `DEFAULT_QUOTA`), else
                           {enabled, maxSize, maxObjects}; `enabled: false` = unlimited
* `budget_number`          {pattern, example} (see `budget.py`)
* `notification_endpoints` [{name, type, url, topic, verifyTls}]

UID mappings live in the `uid_mappings` table. Endpoints carry no credentials
in v1 (see `normalize_endpoint`).

Regex safety: the optional extra bucket-name pattern is admin-supplied. Layers:
length limit; a static pre-check rejecting big counted repeats (`regex` expands
them at compile time, before any timeout applies); compile plus adversarial
probes in a throw-away subprocess with a memory cap and wall-clock limit
(`approve_pattern`); only approved patterns are compiled in-process (bounded
cache) and matched with the `regex` module's `timeout=`. The pattern only sees
names that already passed the DNS rules. `normalize_naming` and
`validate_bucket_name` may block briefly on a new pattern: async callers use
`asyncio.to_thread`.
"""

from __future__ import annotations

import functools
import ipaddress
import re
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import urlsplit

import regex

MAX_QUOTA = 2**62
MAX_PATTERN_LEN = 200
MAX_PREFIX_LEN = 40
REGEX_TIMEOUT = 0.05  # seconds per evaluation
MAX_ENDPOINTS = 50
ENDPOINT_TYPES = ("http", "https", "amqp", "kafka")

NAMING_KEY = "naming"
PLACEMENT_KEY = "placement"
QUOTA_KEY = "default_quota"
ENDPOINTS_KEY = "notification_endpoints"

DEFAULT_NAMING: dict[str, str] = {"prefix": "", "pattern": ""}
DEFAULT_QUOTA: dict[str, Any] = {"enabled": True, "maxSize": 2**40, "maxObjects": None}  # Q4
DEFAULT_PLACEMENT: dict[str, Any] = {"default": None, "allowed": []}


class SettingsError(ValueError):
    """A setting failed validation; the text is safe to show to the admin."""


# -- bucket naming -----------------------------------------------------------------

_DNS_CHARS = re.compile(r"^[a-z0-9.-]+\Z")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}\Z")
_FORBIDDEN_PREFIXES = ("xn--", "sthree-")
_FORBIDDEN_SUFFIXES = ("-s3alias", "--ol-s3", "--x-s3", "--table-s3")
_PREFIX_TEMPLATE = re.compile(r"^(?:[a-z0-9.-]|\{username\})*\Z")


def check_dns_name(name: str) -> list[str]:
    """S3/RGW DNS-compatible bucket naming (AWS rules). Always enforced."""
    errors: list[str] = []
    if not 3 <= len(name) <= 63:
        errors.append("A bucket name must be 3 to 63 characters long.")
    if not name:
        return errors
    if not _DNS_CHARS.match(name):
        errors.append("Use only lowercase letters, digits, hyphens and dots.")
    if not name[0].isalnum() or not name[-1].isalnum() or not name[0].isascii():
        errors.append("The name must start and end with a lowercase letter or a digit.")
    if ".." in name or ".-" in name or "-." in name:
        errors.append("Dots cannot be next to another dot or a hyphen.")
    if _IPV4.match(name):
        errors.append("The name cannot look like an IP address.")
    if name.startswith(_FORBIDDEN_PREFIXES):
        errors.append("The name cannot start with xn-- or sthree-.")
    if name.endswith(_FORBIDDEN_SUFFIXES):
        errors.append("The name cannot end with -s3alias, --ol-s3, --x-s3 or --table-s3.")
    return errors


MAX_REPEAT = 63  # largest counted repeat bound ({n}, {n,m}, fuzzy {e<=n}) we accept
MAX_NESTED_PRODUCT = 256  # cap on nested counted repeats multiplied together
PROBE_TIMEOUT = 1.0  # wall clock for the compile-and-probe subprocess
PROBE_MEMORY = 256 * 2**20  # address-space limit of that subprocess
PROBE_GROWTH_KB = 16 * 1024  # refuse patterns whose compile grows peak RSS by more than this
_APPROVED_MAX = 64


def check_pattern_static(pattern: str) -> None:
    """Cheap pre-check before any compile: `regex` expands counted repeats when
    it compiles, so `a{10000000}` or `(?:a{3000}){3000}` would eat gigabytes
    before `timeout=` could help. Rejects any number inside `{...}` above
    MAX_REPEAT (this also covers fuzzy `{e<=n}`) and nested counted repeats
    whose product exceeds MAX_NESTED_PRODUCT. Escapes (`\\{`) and character
    classes are skipped; a `{` without digits is a literal. Raises SettingsError."""
    if "#" in pattern:  # comments and verbose mode could hide a `[` from this scanner
        raise SettingsError("The extra pattern cannot contain # (comments are not supported).")
    stack = [1]  # per open group: largest repeat product inside it
    prev = 1  # product of the atom just seen (group content, or 1)
    i, n = 0, len(pattern)
    too_big = SettingsError(
        f"Repeat counts in the extra pattern can be at most {MAX_REPEAT}, "
        f"and nested counts at most {MAX_NESTED_PRODUCT} together."
    )
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            prev = 1
            continue
        if c == "[":  # character class: find its end (a leading ] is literal)
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i, prev = j + 1, 1
            continue
        if c == "(":
            flags = re.match(r"\(\?([A-Za-z-]*)", pattern[i:])
            if flags and "x" in flags.group(1).lower():
                raise SettingsError("Verbose mode (?x) is not supported in the extra pattern.")
            stack.append(1)
            i += 1
            prev = 1
            continue
        if c == ")":
            inner = stack.pop() if len(stack) > 1 else 1
            prev = inner
            stack[-1] = max(stack[-1], inner)
            i += 1
            continue
        if c == "{":
            j = pattern.find("}", i)
            body = pattern[i + 1 : j] if j != -1 else ""
            nums = [int(x) for x in re.findall(r"\d+", body)] if j != -1 else []
            if j != -1 and nums and re.fullmatch(r"[\d,<=eidsEIDS+\-\s]*", body):
                bound = max(nums)
                if bound > MAX_REPEAT:
                    raise too_big
                product = max(prev, 1) * max(bound, 1)
                if product > MAX_NESTED_PRODUCT:
                    raise too_big
                stack[-1] = max(stack[-1], product)
                i, prev = j + 1, product  # stacked quantifiers multiply
                continue
        i += 1
        prev = 1


# Runs in a throw-away interpreter with a memory cap: compiles the pattern and
# matches adversarial strings with a timeout. Prints ok | invalid:<msg> | slow.
_PROBE_SCRIPT = r"""
import sys
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (int(sys.argv[1]), int(sys.argv[1])))
except Exception:
    pass
import regex
def peak():
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return 0
pattern = sys.stdin.read()
before = peak()
probes = ["a" * 63, "a" * 62 + "!", "a-" * 31 + "a", "a." * 31 + "a",
          "0" * 62 + "-", "ab" * 31 + "_",
          "A" * 63, "A1 " * 21, "0" * 63 + "/", "Z" * 64]  # budget-shaped (64 max)
try:
    rx = regex.compile(pattern, cache_pattern=False)
    if peak() - before > int(sys.argv[3]):
        raise MemoryError
    for p in probes:
        rx.fullmatch(p, timeout=float(sys.argv[2]))
    print("ok")
except TimeoutError:
    print("slow")
except MemoryError:
    print("invalid:the pattern needs too much memory")
except Exception as e:
    print("invalid:" + str(e).splitlines()[0][:200])
"""

_approved: dict[str, None] = {}  # insertion-ordered, bounded
_approved_lock = threading.Lock()


def approve_pattern(pattern: str) -> None:
    """Accept `pattern` only if it is statically safe and compiles and survives
    the probes inside a memory- and time-limited subprocess. Blocking (up to
    PROBE_TIMEOUT, and ~0.1 s for a first-time pattern): async callers use
    `asyncio.to_thread`. Approved patterns are remembered, so later checks cost
    nothing. Raises SettingsError."""
    with _approved_lock:
        if pattern in _approved:
            return
    check_pattern_static(pattern)
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, pattern goes through stdin
            [sys.executable, "-I", "-c", _PROBE_SCRIPT, str(PROBE_MEMORY), str(REGEX_TIMEOUT),
             str(PROBE_GROWTH_KB)],
            input=pattern, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        out = "slow"
    if out == "slow" or not out:
        raise SettingsError(
            "The extra pattern is too slow or too complex. Simplify it, for example "
            "avoid nested repeats like (a+)+ or large counts."
        )
    if out != "ok":
        msg = out.removeprefix("invalid:")
        raise SettingsError(f"The extra pattern is not a valid regular expression: {msg}")
    with _approved_lock:
        _approved[pattern] = None
        while len(_approved) > _APPROVED_MAX:
            _approved.pop(next(iter(_approved)))


@functools.lru_cache(maxsize=_APPROVED_MAX)
def _compiled(pattern: str):
    """Only ever called for approved patterns (see `approve_pattern`)."""
    return regex.compile(pattern, cache_pattern=False)  # only our bounded cache keeps it


def _pattern_matches(pattern: str, name: str) -> bool:
    """Full-match with a hard time limit. Raises TimeoutError, SettingsError."""
    approve_pattern(pattern)
    return _compiled(pattern).fullmatch(name, timeout=REGEX_TIMEOUT) is not None


def pattern_matches(pattern: str, value: str) -> bool:
    """Full-match `value` against an admin pattern, approved patterns only (see
    `approve_pattern`). Blocking: async callers use `asyncio.to_thread`. Raises
    TimeoutError, SettingsError."""
    return _pattern_matches(pattern, value)


def normalize_naming(raw: Any) -> dict[str, str]:
    """Validate the admin's naming rules; raises SettingsError. Both fields are
    optional (empty = off)."""
    if not isinstance(raw, dict):
        raise SettingsError("Naming rules must be an object.")
    extra = set(raw) - {"prefix", "pattern"}
    if extra:
        raise SettingsError(f"Unknown field: {sorted(extra)[0]}.")
    prefix, pattern = raw.get("prefix", ""), raw.get("pattern", "")
    if not isinstance(prefix, str) or not isinstance(pattern, str):
        raise SettingsError("Prefix and pattern must be text.")
    prefix, pattern = prefix.strip(), pattern.strip()
    if len(prefix) > MAX_PREFIX_LEN:
        raise SettingsError(f"The prefix template can be at most {MAX_PREFIX_LEN} characters.")
    if not _PREFIX_TEMPLATE.match(prefix):
        raise SettingsError(
            "The prefix template can only contain lowercase letters, digits, hyphens, "
            "dots and {username}."
        )
    if prefix:
        # a prefix that can never yield a valid name is a configuration bug
        probe = prefix.replace("{username}", "user") + "xxx"
        if bad := check_dns_name(probe):
            raise SettingsError(f"The prefix template cannot produce valid names: {bad[0]}")
    if len(pattern) > MAX_PATTERN_LEN:
        raise SettingsError(f"The extra pattern can be at most {MAX_PATTERN_LEN} characters.")
    if pattern:
        approve_pattern(pattern)
    return {"prefix": prefix, "pattern": pattern}


def load_naming(settings_repo) -> dict[str, str]:
    stored = settings_repo.get(NAMING_KEY, None) or {}
    return {**DEFAULT_NAMING, **{k: v for k, v in stored.items() if k in DEFAULT_NAMING}}


def _slug(username: str) -> str:
    """AD username as a bucket-name fragment: lowercase, `_` and other
    unsupported characters become hyphens, edges trimmed."""
    s = re.sub(r"[^a-z0-9.-]+", "-", username.lower())
    s = re.sub(r"\.{2,}", ".", s)
    return s.strip("-.")


def suggest_prefix(username: str, rules: dict[str, str]) -> str:
    """The prefix a bucket name must start with for this user ('' = none)."""
    template = (rules or {}).get("prefix", "")
    if not template:
        return ""
    return template.replace("{username}", _slug(username or ""))


def validate_bucket_name(name: str, username: str, rules: dict[str, str]) -> list[str]:
    """All problems with `name` for `username` under `rules` (empty = valid)."""
    errors = check_dns_name(name)
    rules = rules or {}
    prefix = suggest_prefix(username, rules)
    if rules.get("prefix"):
        if "{username}" in rules["prefix"] and not _slug(username or ""):
            errors.append("Your username cannot be used in a bucket name; ask an administrator.")
        elif not name.startswith(prefix):
            errors.append(f"The name must start with \"{prefix}\".")
    pattern = rules.get("pattern", "")
    if pattern and not errors:  # the pattern only sees names that are otherwise valid
        try:
            if not _pattern_matches(pattern, name):
                errors.append("The name does not match the format required by the administrators.")
        except TimeoutError:
            errors.append("The name could not be checked against the administrators' format.")
        except SettingsError:
            errors.append("The administrators' name format is misconfigured.")
    return errors


# -- placement ---------------------------------------------------------------------


def placement_names(rows: list[dict]) -> list[str]:
    """Distinct placement rule names, in croit's order."""
    seen: dict[str, None] = {}
    for r in rows:
        if r.get("name"):
            seen.setdefault(str(r["name"]))
    return list(seen)


def normalize_placement(raw: Any, available: list[str]) -> dict[str, Any]:
    """Validate against croit's current placement names."""
    if not isinstance(raw, dict):
        raise SettingsError("Placement settings must be an object.")
    default, allowed = raw.get("default"), raw.get("allowed", [])
    if default is not None and not isinstance(default, str):
        raise SettingsError("The default placement must be a name or null.")
    default = default or None
    if not isinstance(allowed, list) or not all(isinstance(a, str) for a in allowed):
        raise SettingsError("The allowed placements must be a list of names.")
    for name in ([default] if default else []) + allowed:
        if name not in available:
            raise SettingsError(f"Placement \"{name}\" does not exist on the cluster.")
    return {"default": default, "allowed": [a for a in available if a in set(allowed)]}


def load_placement(settings_repo) -> dict[str, Any]:
    stored = settings_repo.get(PLACEMENT_KEY, None) or {}
    return {**DEFAULT_PLACEMENT, **{k: v for k, v in stored.items() if k in DEFAULT_PLACEMENT}}


def placement_options(rules: dict[str, Any], croit_default: str | None, available: list[str]):
    """What the create-bucket form offers: `{default, choices, userChoice}`.

    `default` is the admin's default when it still exists, else croit's zone
    default (None if neither). `choices` is empty unless an allowed list is set;
    then it holds the allowed placements that still exist plus the default.
    `userChoice` is true only when there is more than one choice, which is the
    "shown only when more than one exists" rule."""
    wanted = rules.get("default")
    default = wanted if wanted in available else croit_default
    choices = [n for n in available if n in set(rules.get("allowed") or [])]
    if choices and default and default not in choices:
        choices.insert(0, default)
    return {"default": default, "choices": choices, "userChoice": len(choices) > 1}


# -- default quota -----------------------------------------------------------------


def normalize_quota(raw: Any) -> dict[str, Any]:
    """{enabled, maxSize, maxObjects}; null = unlimited, 0 is a real limit for
    users (as in the admin user quota). `enabled: false` means explicitly
    unlimited. Raises SettingsError."""
    if not isinstance(raw, dict):
        raise SettingsError("The quota must be an object.")
    if set(raw) - {"enabled", "maxSize", "maxObjects"}:
        raise SettingsError("Unknown quota field.")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise SettingsError("\"enabled\" must be true or false.")
    out: dict[str, Any] = {"enabled": enabled, "maxSize": None, "maxObjects": None}
    for name in ("maxSize", "maxObjects"):
        v = raw.get(name)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_QUOTA:
            raise SettingsError(f"{name} must be a whole number from 0 to {MAX_QUOTA}, or null.")
        out[name] = v
    if not enabled:
        return {"enabled": False, "maxSize": None, "maxObjects": None}
    if out["maxSize"] is None and out["maxObjects"] is None:
        raise SettingsError("An enforced quota needs a size or an object limit.")
    return out


def load_default_quota(settings_repo) -> dict[str, Any] | None:
    """The admin's stored value; None = not set."""
    return settings_repo.get(QUOTA_KEY, None)


def quota_or_default(stored: dict[str, Any] | None) -> dict[str, Any]:
    """`stored` (the admin's value) or a copy of the built-in `DEFAULT_QUOTA`."""
    return stored or dict(DEFAULT_QUOTA)


def effective_default_quota(settings_repo) -> dict[str, Any]:
    """The quota new users get: the admin's value, else `DEFAULT_QUOTA` (1 TiB,
    objects unlimited). The one source of truth; an explicit unlimited is stored
    (`enabled: false`) and is not the same as unset."""
    return quota_or_default(load_default_quota(settings_repo))


def quota_croit_args(quota: dict[str, Any]) -> dict[str, Any]:
    """Keyword arguments for `CroitClient.set_user_quota`."""
    if not quota.get("enabled"):
        return {"enabled": False}
    return {
        "enabled": True,
        "max_size": -1 if quota.get("maxSize") is None else quota["maxSize"],
        "max_objects": -1 if quota.get("maxObjects") is None else quota["maxObjects"],
    }


# -- uid mappings ------------------------------------------------------------------

_AD_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_UID_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}\Z")


def validate_ad_username(value: Any) -> str:
    if not isinstance(value, str) or not _AD_NAME.match(value):
        raise SettingsError(
            "The AD username may use letters, digits, dot, hyphen and underscore (up to 64)."
        )
    return value


def validate_rgw_uid(value: Any) -> str:
    """`uid` or `tenant$uid`."""
    if not isinstance(value, str):
        raise SettingsError("The RGW user id must be text.")
    tenant, sep, uid = value.rpartition("$")
    ok = _UID_PART.match(uid) and (not sep or _TENANT.match(tenant)) and value.count("$") <= 1
    if not ok:
        raise SettingsError(
            "The RGW user id may use letters, digits and . _ @ + - (optionally as tenant$uid)."
        )
    return value


def check_mapping_unique(
    ad_username: str, rgw_uid: str, existing: list[dict[str, Any]]
) -> None:
    """Both directions: an AD user has one RGW uid, an RGW uid belongs to one AD
    user (compared case-insensitively to avoid look-alike duplicates)."""
    if ad_username.lower() == rgw_uid.lower():
        raise SettingsError("That mapping is the default (same name); no mapping is needed.")
    for m in existing:
        same_user = m["ad_username"].lower() == ad_username.lower()
        if not same_user and m["rgw_uid"].lower() == rgw_uid.lower():
            raise SettingsError(f"{rgw_uid} is already mapped from {m['ad_username']}.")


# -- notification endpoints --------------------------------------------------------

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}\Z")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"^(?=.{{1,253}}\Z){_LABEL}(?:\.{_LABEL})*\Z")
_TOPIC = re.compile(r"^[A-Za-z0-9._-]{1,200}\Z")
_SCHEMES = {
    "http": ("http",), "https": ("https",), "amqp": ("amqp", "amqps"), "kafka": ("kafka",),
}


def normalize_endpoint(raw: Any) -> dict[str, Any]:
    """Validate one allowlist entry; raises SettingsError.

    Fields: name, type (http|https|amqp|kafka), url, topic (the AMQP exchange
    or Kafka topic; optional), verifyTls (default true; applies to https and
    amqps). **No credentials in v1**: a URL with user info, a query string or a
    fragment is refused so no secret can sit in plain view in the database,
    audit log or browser. Brokers that need a login are out of scope until
    the owner asks for it (then add write-only fields, never returned)."""
    if not isinstance(raw, dict):
        raise SettingsError("An endpoint must be an object.")
    if set(raw) - {"name", "type", "url", "topic", "verifyTls"}:
        raise SettingsError("Unknown endpoint field.")
    name, typ, url = raw.get("name"), raw.get("type"), raw.get("url")
    topic, verify = raw.get("topic") or "", raw.get("verifyTls", True)
    if not isinstance(name, str) or not _NAME.match(name):
        raise SettingsError("The name may use letters, digits, . _ - (up to 48 characters).")
    if typ not in ENDPOINT_TYPES:
        raise SettingsError("The type must be http, https, amqp or kafka.")
    if not isinstance(url, str) or not url.strip():
        raise SettingsError("Enter the endpoint URL.")
    if not isinstance(topic, str) or (topic and not _TOPIC.match(topic)):
        raise SettingsError("The topic or exchange may use letters, digits and . _ - only.")
    if not isinstance(verify, bool):
        raise SettingsError("verifyTls must be true or false.")
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise SettingsError("That is not a valid URL.") from e
    allowed = _SCHEMES[typ]
    if parts.scheme not in allowed:
        starts = " or ".join(s + "://" for s in allowed)
        raise SettingsError(f"A {typ} endpoint must start with {starts}.")
    host = parts.hostname or ""
    if not host:
        raise SettingsError("The URL needs a host name.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SettingsError("Credentials in the URL are not supported; remove the user info.")
    if parts.query or parts.fragment:
        raise SettingsError("Query strings and fragments are not supported (could hold secrets).")
    if port is not None and not 1 <= port <= 65535:
        raise SettingsError("The port must be between 1 and 65535.")
    if not _HOST.match(host):
        try:
            ipaddress.ip_address(host)
        except ValueError as e:
            raise SettingsError("The host name is not valid.") from e
    if typ == "kafka" and parts.path not in ("", "/"):
        raise SettingsError("A Kafka endpoint is host and port only; use the topic field.")
    return {"name": name, "type": typ, "url": url, "topic": topic, "verifyTls": verify}


def check_endpoint_capacity(endpoint: dict[str, Any], existing: list[dict[str, Any]]) -> None:
    """Names are unique case-insensitively (an upsert replaces the same name)."""
    others = [e for e in existing if e["name"].lower() != endpoint["name"].lower()]
    if len(others) + 1 > MAX_ENDPOINTS:
        raise SettingsError(f"At most {MAX_ENDPOINTS} endpoints are allowed.")


def load_endpoints(settings_repo) -> list[dict[str, Any]]:
    return list(settings_repo.get(ENDPOINTS_KEY, None) or [])


def endpoint_choices(endpoints: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What users may see of the allowlist: name and type, never the address."""
    return [{"name": e["name"], "type": e["type"]} for e in endpoints]
