"""Full lifecycle rules editor under /api/buckets/{name}/lifecycle/rules (Advanced UI only).

The server never looks at `advancedMode`; authorization is the same as for every bucket route
(`require_active_rgw_user`, own bucket verified in croit, the user's OWN S3 key, CSRF on PUT).
The three simple rules of `lifecycle.py` (`bucketeer-*` IDs) are ordinary rules here, flagged
`managed`; this editor writes the WHOLE configuration (`allow_foreign=True`).

- `GET` -> `{rules, etag, warnings, versioning}`. A rule is
  `{id, status: Enabled|Disabled, filter: {prefix?, tags?: [{key, value}], sizeGreaterThan?,
  sizeLessThan?}, expiration: {days?|date?|expiredObjectDeleteMarker?}|null,
  noncurrentExpiration: {days, newerNoncurrentVersions?}|null, abortMultipartDays|null,
  transitions: {current: [raw], noncurrent: [raw]}|null, managed}`. `date` is
  `YYYY-MM-DDT00:00:00Z`. `transitions` is raw S3 data, READ ONLY (Q13: moving objects to other
  storage classes is not offered): it can only be passed back unchanged. No configuration -> `[]`.
  `etag` is a SHA-256 of the canonical rule list. `warnings` are `{rule (1-based), id, message}`.
- `PUT {rules, etag, confirmManaged?}` replaces the whole configuration; an empty list deletes it.
  Answers the GET shape plus `changed`. S3 shapes are built here (one condition -> plain Filter
  element, several -> `And`). Failures:
  * 422 `{detail, errors: [{rule (1-based|null), field, message}]}`: at most 1000 rules, unique
    non-empty IDs up to 255 characters, `bucketeer-` IDs cannot be created and an existing one
    may only be kept as stored, get the builder's canonical form with other days, or be removed
    (all but "kept" need `confirmManaged`; anything else, such as a prefix or Disabled, is 422),
    at least one action,
    days 1..36500, midnight-UTC dates, one of days/date/expiredObjectDeleteMarker, marker and
    unfinished-upload clean-up not combinable with tag or size filters (S3 refuses), tag filter
    keys/values by the S3 tag rules (at most 10), sizes >= 0 with greater < less,
    newerNoncurrentVersions 1..100, and transitions: a rule that has them can only be kept
    exactly as stored or removed (never edited, widened or enabled), none can be created.
    A rule identical to the stored one is accepted as is even if it fails these checks (S3 already
    accepted it) and is written back byte for byte.
  * 409 `{etagMismatch: true, current}`: re-checked INSIDE `s3docs.bucket_lock`.
  * 409 `{requiresConfirmation: true, managedChanges: [ids]}` when a `bucketeer-*` rule would be
    changed or removed and `confirmManaged` is not true. Nothing is written.
- Warnings never block: an enabled expiration without any filter (whole bucket), noncurrent
  rules on a bucket whose versioning is off, an expiration date in the past.
- Audit `bucket.lifecycle_rules` (before/after summaries capped at 8 KB, changed ids,
  `confirm_managed`; only when changed).
"""

import copy
import hashlib
import json
import logging
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from . import s3docs, tags
from .auth import SessionUser, require_user
from .buckets import _audit, _full, _own_buckets, _own_name
from .lifecycle import _versioning
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.lifecycle_rules")

router = APIRouter(prefix="/buckets", tags=["lifecycle"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

MAX_RULES = 1000
ID_MAX = 255
PREFIX_MAX = 1024  # bytes
MAX_FILTER_TAGS = 10
SIZE_MAX = 2**62
MAX_NEWER = 100
AUDIT_MAX = 8192
STATUSES = ("Enabled", "Disabled")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
NO_TRANSITIONS = ("Transitions aren't available yet. Rules that already have transitions "
                  "keep them, but they can't be changed or added here.")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[dict]
    etag: str
    confirmManaged: StrictBool = False


class EtagMismatch(Exception):
    def __init__(self, current: list[dict]):
        self.current = current


class Invalid(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors


class NeedConfirm(Exception):
    def __init__(self, ids: list[str]):
        self.ids = ids


# ------------------------------------------------------------- S3 <-> API shape

def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return _iso(value)


def _prune(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _filter_from_s3(raw: dict) -> dict:
    f = raw.get("Filter")
    if f is None:
        f = {"Prefix": raw["Prefix"]} if raw.get("Prefix") else {}  # legacy top-level Prefix
    parts = {**f, **(f.get("And") or {})}
    found = list(parts.get("Tags") or [])
    if parts.get("Tag"):
        found.append(parts["Tag"])
    out: dict[str, Any] = {}
    if parts.get("Prefix"):
        out["prefix"] = parts["Prefix"]
    if found:
        out["tags"] = sorted(({"key": t.get("Key", ""), "value": t.get("Value", "")}
                              for t in found), key=lambda t: (t["key"], t["value"]))
    if parts.get("ObjectSizeGreaterThan") is not None:
        out["sizeGreaterThan"] = parts["ObjectSizeGreaterThan"]
    if parts.get("ObjectSizeLessThan") is not None:
        out["sizeLessThan"] = parts["ObjectSizeLessThan"]
    return out


def rule_from_s3(raw: dict) -> dict:
    exp = raw.get("Expiration") or {}
    expiration = _prune({
        "days": exp.get("Days"), "date": _iso(exp.get("Date")),
        "expiredObjectDeleteMarker": True if exp.get("ExpiredObjectDeleteMarker") else None})
    nc = raw.get("NoncurrentVersionExpiration") or {}
    noncurrent = _prune({"days": nc.get("NoncurrentDays"),
                         "newerNoncurrentVersions": nc.get("NewerNoncurrentVersions")})
    abort = (raw.get("AbortIncompleteMultipartUpload") or {}).get("DaysAfterInitiation")
    trans = {"current": _jsonable(raw.get("Transitions") or []),
             "noncurrent": _jsonable(raw.get("NoncurrentVersionTransitions") or [])}
    rid = raw.get("ID") or ""
    return {"id": rid, "status": raw.get("Status", "Enabled"), "filter": _filter_from_s3(raw),
            "expiration": expiration or None, "noncurrentExpiration": noncurrent or None,
            "abortMultipartDays": abort,
            "transitions": trans if (trans["current"] or trans["noncurrent"]) else None,
            "managed": s3docs.is_managed(rid)}


def _core(rule: dict) -> dict:
    """What an edit can change: everything but the read-only parts."""
    return {k: v for k, v in rule.items() if k not in ("transitions", "managed")}


def etag_of(rules: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
                          ).hexdigest()


def _filter_to_s3(f: dict) -> dict:
    conds: list[tuple[str, Any]] = []
    if f.get("prefix"):
        conds.append(("Prefix", f["prefix"]))
    for t in f.get("tags") or []:
        conds.append(("Tag", {"Key": t["key"], "Value": t["value"]}))
    if f.get("sizeGreaterThan") is not None:
        conds.append(("ObjectSizeGreaterThan", f["sizeGreaterThan"]))
    if f.get("sizeLessThan") is not None:
        conds.append(("ObjectSizeLessThan", f["sizeLessThan"]))
    if not conds:
        return {"Prefix": ""}
    if len(conds) == 1:
        return {conds[0][0]: conds[0][1]}
    and_: dict[str, Any] = {}
    for key, val in conds:
        if key == "Tag":
            and_.setdefault("Tags", []).append(val)
        else:
            and_[key] = val
    return {"And": and_}


def rule_to_s3(rule: dict) -> dict:
    out: dict[str, Any] = {"ID": rule["id"], "Status": rule["status"],
                           "Filter": _filter_to_s3(rule["filter"])}
    e = rule.get("expiration")
    if e:
        out["Expiration"] = (
            {"Days": e["days"]} if "days" in e else {"Date": e["date"]} if "date" in e
            else {"ExpiredObjectDeleteMarker": True})
    n = rule.get("noncurrentExpiration")
    if n:
        out["NoncurrentVersionExpiration"] = _prune({
            "NoncurrentDays": n.get("days"),
            "NewerNoncurrentVersions": n.get("newerNoncurrentVersions")})
    if rule.get("abortMultipartDays") is not None:
        out["AbortIncompleteMultipartUpload"] = {"DaysAfterInitiation": rule["abortMultipartDays"]}
    return out


# ------------------------------------------------------------------ validation

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_date(value: str) -> str | None:
    """Any ISO 8601 date / timestamp as UTC `YYYY-MM-DDTHH:MM:SSZ`; None when unparseable."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return f"{value}T00:00:00Z"
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _iso(dt)


def _shape(r: Any) -> tuple[dict, list[tuple[str, str]]]:
    """Type-level normalization of a submitted rule into the canonical API form."""
    errs: list[tuple[str, str]] = []
    if not isinstance(r, dict):
        return {}, [("rule", "A rule must be an object.")]
    for extra in sorted(set(r) - {"id", "status", "filter", "expiration", "noncurrentExpiration",
                                  "abortMultipartDays", "transitions", "managed"}):
        errs.append(("rule", f"Unknown field {extra!r}."))
    rid = r.get("id")
    if not isinstance(rid, str):
        errs.append(("id", "The name must be text."))
        rid = ""
    status = r.get("status")
    if not isinstance(status, str):
        errs.append(("status", "The status must be Enabled or Disabled."))
        status = ""

    def sub(name: str, allowed: set[str]) -> dict:
        v = r.get(name)
        if v is None:
            return {}
        if not isinstance(v, dict):
            errs.append((name, f"{name} must be an object."))
            return {}
        for k in sorted(set(v) - allowed):
            errs.append((name, f"Unknown field {k!r} in {name}."))
        return v

    f = sub("filter", {"prefix", "tags", "sizeGreaterThan", "sizeLessThan"})
    filt: dict[str, Any] = {}
    if f.get("prefix") is not None:
        if isinstance(f["prefix"], str):
            if f["prefix"]:
                filt["prefix"] = f["prefix"]
        else:
            errs.append(("filter", "The prefix must be text."))
    if f.get("tags") is not None:
        ts = f["tags"]
        if (not isinstance(ts, list) or not all(
                isinstance(t, dict) and set(t) <= {"key", "value"}
                and isinstance(t.get("key"), str) and isinstance(t.get("value", ""), str)
                for t in ts)):
            errs.append(("filter", "Tags must be a list of {key, value} text pairs."))
        elif ts:
            filt["tags"] = sorted(({"key": t["key"], "value": t.get("value", "")} for t in ts),
                                  key=lambda t: (t["key"], t["value"]))
    for k in ("sizeGreaterThan", "sizeLessThan"):
        if f.get(k) is not None:
            if _is_int(f[k]):
                filt[k] = f[k]
            else:
                errs.append(("filter", f"{k} must be a whole number of bytes."))

    e = sub("expiration", {"days", "date", "expiredObjectDeleteMarker"})
    exp: dict[str, Any] = {}
    if e.get("days") is not None:
        if _is_int(e["days"]):
            exp["days"] = e["days"]
        else:
            errs.append(("expiration", "Days must be a whole number."))
    if e.get("date") is not None:
        d = parse_date(e["date"]) if isinstance(e["date"], str) else None
        if d:
            exp["date"] = d
        else:
            errs.append(("expiration", "The date must look like 2027-01-31."))
    if e.get("expiredObjectDeleteMarker") is not None:
        if isinstance(e["expiredObjectDeleteMarker"], bool):
            if e["expiredObjectDeleteMarker"]:
                exp["expiredObjectDeleteMarker"] = True
        else:
            errs.append(("expiration", "expiredObjectDeleteMarker must be true or false."))

    n = sub("noncurrentExpiration", {"days", "newerNoncurrentVersions"})
    nonc: dict[str, Any] = {}
    for k in ("days", "newerNoncurrentVersions"):
        if n.get(k) is not None:
            if _is_int(n[k]):
                nonc[k] = n[k]
            else:
                errs.append(("noncurrentExpiration", f"{k} must be a whole number."))

    abort = r.get("abortMultipartDays")
    if abort is not None and not _is_int(abort):
        errs.append(("abortMultipartDays", "Days must be a whole number."))
        abort = None
    return {"id": rid, "status": status, "filter": filt, "expiration": exp or None,
            "noncurrentExpiration": nonc or None, "abortMultipartDays": abort}, errs


def _strict(rule: dict, has_transitions: bool) -> list[tuple[str, str]]:
    errs: list[tuple[str, str]] = []
    rid = rule["id"]
    if not rid or len(rid) > ID_MAX or _CONTROL.search(rid):
        errs.append(("id", f"Give the rule a name of 1 to {ID_MAX} characters, "
                           "without control characters."))
    if rule["status"] not in STATUSES:
        errs.append(("status", "The status must be Enabled or Disabled."))
    f = rule["filter"]
    if "prefix" in f and (len(f["prefix"].encode()) > PREFIX_MAX or _CONTROL.search(f["prefix"])):
        errs.append(("filter", f"The prefix can be at most {PREFIX_MAX} bytes, "
                               "without control characters."))
    ts = f.get("tags") or []
    if len(ts) > MAX_FILTER_TAGS:
        errs.append(("filter", f"A rule can match at most {MAX_FILTER_TAGS} tags."))
    seen = set()
    for t in ts:
        k, v = t["key"], t["value"]
        if not 1 <= len(k) <= tags.KEY_MAX or not tags.CHARS.match(k):
            errs.append(("filter", f"Tag key {k!r} must be 1 to {tags.KEY_MAX} letters, digits, "
                                   "spaces or + - = . _ : / @."))
        if len(v) > tags.VALUE_MAX or not tags.CHARS.match(v):
            errs.append(("filter", f"Tag value {v!r} must be at most {tags.VALUE_MAX} letters, "
                                   "digits, spaces or + - = . _ : / @."))
        if k in seen:
            errs.append(("filter", f"Tag key {k!r} appears twice."))
        seen.add(k)
    gt, lt = f.get("sizeGreaterThan"), f.get("sizeLessThan")
    for name, v in (("sizeGreaterThan", gt), ("sizeLessThan", lt)):
        if v is not None and not 0 <= v <= SIZE_MAX:
            errs.append(("filter", f"{name} must be 0 or more bytes."))
    if gt is not None and lt is not None and 0 <= gt <= SIZE_MAX and 0 <= lt <= SIZE_MAX \
            and gt >= lt:
        errs.append(("filter", "The minimum size must be smaller than the maximum size."))

    e = rule["expiration"] or {}
    if len(e) > 1:
        errs.append(("expiration", "Choose one of: after a number of days, on a date, or "
                                   "removing leftover delete markers."))
    if "days" in e and not 1 <= e["days"] <= s3docs.MAX_DAYS:
        errs.append(("expiration", f"Days must be from 1 to {s3docs.MAX_DAYS}."))
    if "date" in e and not e["date"].endswith("T00:00:00Z"):
        errs.append(("expiration", "The date must be midnight UTC (a plain date like 2027-01-31)."))
    n = rule["noncurrentExpiration"]
    if n:
        if "days" not in n:
            errs.append(("noncurrentExpiration", "Say after how many days old versions expire."))
        elif not 1 <= n["days"] <= s3docs.MAX_DAYS:
            errs.append(("noncurrentExpiration", f"Days must be from 1 to {s3docs.MAX_DAYS}."))
        if "newerNoncurrentVersions" in n and not 1 <= n["newerNoncurrentVersions"] <= MAX_NEWER:
            errs.append(("noncurrentExpiration",
                         f"Versions to keep must be from 1 to {MAX_NEWER}."))
    a = rule["abortMultipartDays"]
    if a is not None and not 1 <= a <= s3docs.MAX_DAYS:
        errs.append(("abortMultipartDays", f"Days must be from 1 to {s3docs.MAX_DAYS}."))
    if a is not None and (ts or gt is not None or lt is not None):
        errs.append(("abortMultipartDays", "Cleaning up unfinished uploads can only be limited "
                                           "by a prefix, not by tags or size."))
    if e.get("expiredObjectDeleteMarker") and (ts or gt is not None or lt is not None):
        errs.append(("expiration", "Removing leftover delete markers can only be limited by a "
                                   "prefix, not by tags or size."))
    if not (e or n or a is not None or has_transitions):
        errs.append(("rule", "Add at least one action: expire files, expire old versions, "
                             "remove delete markers or clean up unfinished uploads."))
    return errs


MANAGED_ONLY = ("Bucketeer's rules can only change their number of days or be removed; "
                "create your own rule for anything else.")
_BUILDERS = {
    s3docs.EXPIRE_CURRENT_ID: (s3docs.expire_current_rule,
                               lambda r: (r["expiration"] or {}).get("days")),
    s3docs.EXPIRE_NONCURRENT_ID: (s3docs.expire_noncurrent_rule,
                                  lambda r: (r["noncurrentExpiration"] or {}).get("days")),
    s3docs.ABORT_MULTIPART_ID: (s3docs.abort_multipart_rule, lambda r: r["abortMultipartDays"]),
}


def _managed_edit(rule: dict) -> tuple[bool, str]:
    """A managed rule may only become the builder's canonical form with other days."""
    build = _BUILDERS.get(rule["id"])
    if not build:
        return False, MANAGED_ONLY
    try:
        canonical = rule_from_s3(build[0](build[1](rule)))
    except s3docs.S3DocError as exc:
        return False, str(exc)
    return (_core(canonical) == rule), MANAGED_ONLY


def _err(rule: int | None, field: str, message: str) -> dict:
    return {"rule": rule, "field": field, "message": message}


# --------------------------------------------------------------------- planning

def plan(submitted: list[dict], current: dict | None,
         confirm: bool) -> tuple[list[dict], list[str]]:
    """Validate `submitted` against the stored config; returns (new S3 rules, changed ids)."""
    if len(submitted) > MAX_RULES:
        raise Invalid([_err(None, "rules", f"At most {MAX_RULES} rules are allowed.")])
    cur_raw = [r for r in (current or {}).get("Rules") or []]
    cur_by_id = {r["ID"]: r for r in cur_raw if r.get("ID")}
    errs: list[dict] = []
    shaped: list[tuple[dict, Any]] = []
    for i, r in enumerate(submitted, 1):
        rule, e = _shape(r)
        errs += [_err(i, f, m) for f, m in e]
        sub_t = r.get("transitions") if isinstance(r, dict) else None
        if isinstance(sub_t, dict) and not sub_t.get("current") and not sub_t.get("noncurrent"):
            sub_t = None
        shaped.append((rule, sub_t or None))
    seen: set[str] = set()
    out: list[dict] = []
    changed: list[str] = []
    for i, (rule, sub_t) in enumerate(shaped, 1):
        if any(x["rule"] == i for x in errs):
            continue
        rid = rule["id"]
        if rid in seen:
            errs.append(_err(i, "id", f"The name {rid!r} is used twice. Names must be unique."))
            continue
        if rid:
            seen.add(rid)
        raw = cur_by_id.get(rid) if rid else None
        cur_api = rule_from_s3(raw) if raw else None
        if (cur_api["transitions"] if cur_api else None) != _jsonable(sub_t):
            errs.append(_err(i, "transitions", NO_TRANSITIONS))
            continue
        if cur_api and _core(cur_api) == rule:
            out.append(copy.deepcopy(raw))  # unchanged: written back exactly as stored
            continue
        if cur_api and cur_api["transitions"]:
            errs.append(_err(i, "transitions", NO_TRANSITIONS))
            continue
        if cur_api and cur_api["managed"]:
            ok, msg = _managed_edit(rule)
            if not ok:
                errs.append(_err(i, "rule", msg))
            else:
                out.append(rule_to_s3(rule))
                changed.append(rid)
            continue
        if not cur_api and s3docs.is_managed(rid):
            errs.append(_err(i, "id", "Names starting with bucketeer- are reserved for rules "
                                      "Bucketeer manages. Choose another name."))
            continue
        found = _strict(rule, False)
        errs += [_err(i, f, m) for f, m in found]
        if not found:
            out.append(rule_to_s3(rule))
            changed.append(rid)
    if errs:
        raise Invalid(errs)
    kept = {r["ID"] for r in out}
    changed += [rid for rid in cur_by_id if rid not in kept]
    managed = [rid for rid in changed if s3docs.is_managed(rid) and rid in cur_by_id]
    if managed and not confirm:
        raise NeedConfirm(managed)
    return out, changed


# ------------------------------------------------------------------ S3 plumbing

def _read_cfg(s3, bucket: str) -> dict | None:
    try:
        return s3docs.parse_lifecycle(s3.get_bucket_lifecycle_configuration(Bucket=bucket))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchLifecycleConfiguration":
            raise
        return None


def _api_rules(cfg: dict | None) -> list[dict]:
    return [rule_from_s3(r) for r in (cfg or {}).get("Rules") or []]


def warnings_for(rules: list[dict], versioning: str | None) -> list[dict]:
    out = []
    today = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, r in enumerate(rules, 1):
        if r["status"] != "Enabled":
            continue

        def warn(msg, i=i, r=r):
            out.append({"rule": i, "id": r["id"], "message": msg})
        e = r["expiration"] or {}
        deleting = "days" in e or "date" in e or r["noncurrentExpiration"]
        if deleting and not r["filter"]:
            warn("This rule has no filter, so it applies to every file in the bucket.")
        if r["noncurrentExpiration"] and versioning == "off":
            warn("Versioning is off for this bucket, so there are no old versions to expire yet. "
                 "The rule starts to apply once you turn versioning on.")
        if "date" in e and e["date"] < today:
            warn("This date has passed, so matching files are removed as soon as the storage "
                 "service next runs its clean-up.")
    return out


def view(rules: list[dict], versioning: str | None) -> dict[str, Any]:
    return {"rules": rules, "etag": etag_of(rules), "warnings": warnings_for(rules, versioning),
            "versioning": versioning}


def _apply(s3, bucket: str, submitted: list[dict], etag: str, confirm: bool) -> dict:
    seen: dict[str, Any] = {}

    def mutate(cfg):
        before = _api_rules(cfg)
        if etag_of(before) != etag:
            raise EtagMismatch(before)
        seen["before"] = before
        rules, seen["changed"] = plan(submitted, cfg, confirm)
        return {"Rules": rules} if rules else None

    with s3docs.bucket_lock(bucket):
        new = s3docs.update_bucket_lifecycle(s3, bucket, mutate, allow_foreign=True)
        before = seen["before"]
        if _api_rules(new) == before:
            return {"before": before, "after": before, "ids": []}
        try:
            after = _api_rules(_read_cfg(s3, bucket))
        except Exception:  # the write succeeded: never lose the audit entry over the read-back
            log.warning("lifecycle read-back failed after a successful write", exc_info=True)
            after = _api_rules(new)
        return {"before": before, "after": after, "ids": seen["changed"]}


async def _own(request: Request, acct: RgwAccount, name: str) -> tuple[str, str]:
    short = _own_name(name)
    full = _full(acct.uid, short)
    if not any(b.get("bucket") == full
               for b in await _own_buckets(request.app.state.croit, acct.uid)):
        raise HTTPException(404, "Bucket not found.")
    return short, full


def _s3_error(exc: Exception) -> HTTPException:
    err = exc.response.get("Error", {}) if isinstance(exc, ClientError) else {}
    code = err.get("Code")
    log.warning("lifecycle rules S3 call failed: %s", code or type(exc).__name__)
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code == "AccessDenied":
        return HTTPException(403, "You are not allowed to change this bucket's lifecycle rules.")
    if code in ("MalformedXML", "InvalidRequest", "InvalidArgument"):
        return HTTPException(422, "The storage service rejected the rules: "
                                  f"{str(err.get('Message') or code)[:300]}")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


def _summary(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules:
        t = r["transitions"]
        out.append({**{k: v for k, v in r.items() if k not in ("transitions", "managed")},
                    "transitions": len(t["current"]) + len(t["noncurrent"]) if t else 0})
    return out


def _capped(summary: list[dict]) -> Any:
    return summary if len(json.dumps(summary)) <= AUDIT_MAX else {"omitted": "larger than 8 KB"}


@router.get("/{name}/lifecycle/rules")
async def get_rules(name: str, request: Request, acct: Account):
    short, full = await _own(request, acct, name)
    try:
        cfg = await request.app.state.s3.call(acct.uid, _read_cfg, short)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    state = await _versioning(request, acct, full)
    return JSONResponse(view(_api_rules(cfg), state), headers={"Cache-Control": "no-store"})


@router.put("/{name}/lifecycle/rules")
async def put_rules(name: str, body: Body, request: Request, acct: Account, user: User):
    short, full = await _own(request, acct, name)
    try:
        res = await request.app.state.s3.call(
            acct.uid, _apply, short, body.rules, body.etag, body.confirmManaged)
    except EtagMismatch as exc:
        state = await _versioning(request, acct, full)
        return JSONResponse(status_code=409, content={
            "detail": "The lifecycle rules changed since you opened them. "
                      "Reload to see the current rules.",
            "etagMismatch": True, "current": view(exc.current, state)})
    except Invalid as exc:
        first = exc.errors[0]
        return JSONResponse(status_code=422, content={
            "detail": first["message"] if first["rule"] is None
            else f"Rule {first['rule']}: {first['message']}", "errors": exc.errors})
    except NeedConfirm as exc:
        return JSONResponse(status_code=409, content={
            "detail": "This changes rules that Bucketeer manages. Confirm to continue.",
            "requiresConfirmation": True, "managedChanges": exc.ids})
    except HTTPException:
        raise
    except s3docs.S3DocError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:
        raise _s3_error(exc) from None
    before, after = res["before"], res["after"]
    changed = before != after
    state = await _versioning(request, acct, full)
    out = view(after, state)
    if changed:
        managed = [i for i in res["ids"] if s3docs.is_managed(i)]
        await _audit(request, user.username, "bucket.lifecycle_rules", full, uid=acct.uid,
                     rules_before=len(before), rules_after=len(after),
                     before=_capped(_summary(before)), after=_capped(_summary(after)),
                     changed_ids=res["ids"][:100], confirm_managed=bool(managed),
                     warnings=len(out["warnings"]))
    return {**out, "changed": changed}
