"""CORS rules editor under /api/buckets/{name}/cors (Advanced UI only).

CORS does not grant access by itself: it only lets browser JavaScript on the listed origins
read responses the bucket already allows (public objects, presigned URLs, signed requests).
The server never looks at `advancedMode`; authorization is the same as for every bucket route
(`require_active_rgw_user`, own bucket verified in croit, the user's OWN S3 key, CSRF on PUT).

- `GET` -> `{rules: [{id?, allowedOrigins, allowedMethods, allowedHeaders, exposeHeaders,
  maxAgeSeconds|null}], etag, warnings}`. No configuration (NoSuchCORSConfiguration) -> `[]`.
  `etag` is a SHA-256 of the canonical rule list.
- `PUT {rules, etag}`; an empty list deletes the configuration. Same shape back plus `changed`.
  Validation failures -> 422 `{detail, errors: [{rule (1-based|null), field, message}]}`:
  at most 100 rules; origins `*` or `http(s)://host[:port]` with at most one `*` (other origins
  that S3 accepts, such as a scheme-less host, are saved with a warning);
  methods from GET PUT POST DELETE HEAD; headers are tokens with at most one `*`; exposed
  headers tokens; `maxAgeSeconds` 0..2147483647; id at most 255 chars;
  at most 64 KB as the XML body S3 receives.
- `warnings`: a rule with origin `*` and PUT/POST/DELETE (any website's JavaScript may then
  call the bucket; requests still need credentials or a public bucket) and for origins that
  are not `scheme://host[:port]` (browsers send exactly that form, so such a rule may never
  match). Warns, never blocks.
- The etag is re-checked INSIDE `s3docs.bucket_lock`: 409 `{etagMismatch: true, current}`.
- Audit `bucket.cors` (before/after rule summaries; only when changed).
"""

import hashlib
import json
import logging
import re
from typing import Annotated, Any
from xml.sax.saxutils import escape

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import s3docs
from .auth import SessionUser, require_user
from .buckets import _audit, _full, _own_buckets, _own_name
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.cors")

router = APIRouter(prefix="/buckets", tags=["cors"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

MAX_RULES = 100
MAX_BYTES = 64 * 1024
MAX_AGE = 2_147_483_647  # S3/RGW accept any non-negative 32-bit value
AUDIT_MAX = 8192
METHODS = ("GET", "PUT", "POST", "DELETE", "HEAD")
WRITE_METHODS = {"PUT", "POST", "DELETE"}
ORIGIN_RE = re.compile(r"\A(https?)://([A-Za-z0-9*]([A-Za-z0-9.*-]*[A-Za-z0-9*])?)(:[0-9]{1,5})?\Z")
TOKEN_RE = re.compile(r"\A[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
LIST_MAX = 50


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[dict]
    etag: str


class EtagMismatch(Exception):
    def __init__(self, current: list[dict]):
        self.current = current


class Invalid(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors


def etag_of(rules: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
                          ).hexdigest()


def warnings_for(rules: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rules, 1):
        bad = [m for m in r["allowedMethods"] if m in WRITE_METHODS]
        if "*" in r["allowedOrigins"] and bad:
            out.append({"rule": i, "message": (
                f"Rule {i} lets JavaScript on any website send {', '.join(bad)} requests to this "
                "bucket from a visitor's browser. They still need valid credentials (or a public "
                "bucket), but consider listing only the websites you trust.")})
        odd = [o for o in r["allowedOrigins"] if o != "*" and not ORIGIN_RE.match(o)]
        if odd:
            out.append({"rule": i, "message": (
                f"Rule {i}: {', '.join(odd)} is not in the form https://example.com. Browsers "
                "send the scheme and host, so this origin may never match.")})
    return out


def view(rules: list[dict]) -> dict[str, Any]:
    return {"rules": rules, "etag": etag_of(rules), "warnings": warnings_for(rules)}


def _norm_s3(rule: dict) -> dict:
    out = {"allowedOrigins": list(rule.get("AllowedOrigins", [])),
           "allowedMethods": list(rule.get("AllowedMethods", [])),
           "allowedHeaders": list(rule.get("AllowedHeaders", [])),
           "exposeHeaders": list(rule.get("ExposeHeaders", [])),
           "maxAgeSeconds": rule.get("MaxAgeSeconds")}
    if rule.get("ID"):
        out = {"id": rule["ID"], **out}
    return out


def _to_s3(rule: dict) -> dict:
    out: dict[str, Any] = {"AllowedOrigins": rule["allowedOrigins"],
                           "AllowedMethods": rule["allowedMethods"]}
    if rule.get("id"):
        out["ID"] = rule["id"]
    if rule["allowedHeaders"]:
        out["AllowedHeaders"] = rule["allowedHeaders"]
    if rule["exposeHeaders"]:
        out["ExposeHeaders"] = rule["exposeHeaders"]
    if rule["maxAgeSeconds"] is not None:
        out["MaxAgeSeconds"] = rule["maxAgeSeconds"]
    return out


def _strings(value: Any, field: str, i: int, errs: list[dict]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errs.append({"rule": i, "field": field, "message": f"{field} must be a list of text."})
        return []
    if len(value) > LIST_MAX:
        errs.append({"rule": i, "field": field,
                     "message": f"At most {LIST_MAX} entries in {field}."})
        return []
    seen, out = set(), []
    for v in (x.strip() for x in value):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # boto3 sends these unescaped: invalid XML


def check_origin(o: str) -> str | None:
    """Hard errors only; odd-but-accepted origins get a warning (see warnings_for)."""
    if o == "*":
        return None
    if _CONTROL.search(o):
        return f"{o!r} contains a control character."
    if len(o) > 255 or re.search(r"\s", o):
        return f"{o!r} is not an origin (no spaces, at most 255 characters)."
    if o.count("*") > 1:
        return f"{o!r} has more than one * wildcard; S3 allows one."
    return None


def _xml_body(rules: list[dict]) -> str:
    """Approximation of the XML body S3 receives (its 64 KB limit is on that body)."""
    def tags(name, values):
        return "".join(f"<{name}>{escape(str(v))}</{name}>" for v in values)
    out = ["<CORSConfiguration>"]
    for r in rules:
        out.append("<CORSRule>")
        if r.get("id"):
            out.append(tags("ID", [r["id"]]))
        out.append(tags("AllowedOrigin", r["allowedOrigins"]))
        out.append(tags("AllowedMethod", r["allowedMethods"]))
        out.append(tags("AllowedHeader", r["allowedHeaders"]))
        out.append(tags("ExposeHeader", r["exposeHeaders"]))
        if r["maxAgeSeconds"] is not None:
            out.append(tags("MaxAgeSeconds", [r["maxAgeSeconds"]]))
        out.append("</CORSRule>")
    out.append("</CORSConfiguration>")
    return "".join(out)


def normalize(raw: list[dict]) -> list[dict]:
    """Validate and canonicalize submitted rules; raises Invalid with per-rule messages."""
    errs: list[dict] = []
    if len(raw) > MAX_RULES:
        raise Invalid([{"rule": None, "field": "rules",
                        "message": f"At most {MAX_RULES} rules are allowed."}])
    rules = []
    for i, r in enumerate(raw, 1):
        def err(field, message, i=i):
            errs.append({"rule": i, "field": field, "message": message})
        extra = sorted(set(r) - {"id", "allowedOrigins", "allowedMethods", "allowedHeaders",
                                 "exposeHeaders", "maxAgeSeconds"})
        if extra:
            err("rule", f"Unknown field {extra[0]!r}.")
        rid = r.get("id")
        if rid is not None and (not isinstance(rid, str) or len(rid) > 255
                                or _CONTROL.search(rid)):
            err("id", "The name must be text of at most 255 characters, without control "
                      "characters.")
            rid = None
        origins = _strings(r.get("allowedOrigins"), "allowedOrigins", i, errs)
        methods = _strings(r.get("allowedMethods"), "allowedMethods", i, errs)
        headers = _strings(r.get("allowedHeaders"), "allowedHeaders", i, errs)
        expose = _strings(r.get("exposeHeaders"), "exposeHeaders", i, errs)
        if not origins:
            err("allowedOrigins", "Add at least one origin, such as https://example.com.")
        for o in origins:
            if (msg := check_origin(o)):
                err("allowedOrigins", msg)
        if not methods:
            err("allowedMethods", "Choose at least one method.")
        for m in methods:
            if m not in METHODS:
                err("allowedMethods", f"{m!r} is not allowed. Use {', '.join(METHODS)}.")
        for h in headers:
            if not TOKEN_RE.match(h) or h.count("*") > 1:
                err("allowedHeaders", f"{h!r} is not a header name (one * wildcard at most).")
        for h in expose:
            if not TOKEN_RE.match(h) or "*" in h:
                err("exposeHeaders", f"{h!r} is not a header name.")
        age = r.get("maxAgeSeconds")
        if age is not None and (isinstance(age, bool) or not isinstance(age, int)
                                or not 0 <= age <= MAX_AGE):
            err("maxAgeSeconds", "Max age must be a whole number of seconds, 0 to 2147483647.")
            age = None
        rule = {"allowedOrigins": origins, "allowedMethods": [m for m in METHODS if m in methods],
                "allowedHeaders": headers, "exposeHeaders": expose, "maxAgeSeconds": age}
        rules.append({"id": rid, **rule} if rid else rule)
    if errs:
        raise Invalid(errs)
    if len(_xml_body(rules).encode()) > MAX_BYTES:
        raise Invalid([{"rule": None, "field": "rules",
                        "message": "The CORS configuration can be at most 64 KB."}])
    return rules


def _read(s3, bucket: str) -> list[dict]:
    try:
        return [_norm_s3(r) for r in s3.get_bucket_cors(Bucket=bucket)["CORSRules"]]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchCORSConfiguration":
            raise
        return []


def _capped(summary: list[dict]) -> Any:
    return summary if len(json.dumps(summary)) <= AUDIT_MAX else {"omitted": "larger than 8 KB"}


def _apply(s3, bucket: str, new: list[dict], etag: str) -> dict:
    with s3docs.bucket_lock(bucket):
        current = _read(s3, bucket)
        if etag_of(current) != etag:
            raise EtagMismatch(current)
        if new != current:
            if new:
                s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={
                    "CORSRules": [_to_s3(r) for r in new]})
            else:
                s3.delete_bucket_cors(Bucket=bucket)
        if new == current:
            return {"before": current, "after": current}
        try:
            after = _read(s3, bucket)
        except Exception:  # the write succeeded: never lose the audit entry over the read-back
            log.warning("cors read-back failed after a successful write", exc_info=True)
            after = new
        return {"before": current, "after": after}


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
    log.warning("cors S3 call failed: %s", code or type(exc).__name__)
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code == "AccessDenied":
        return HTTPException(403, "You are not allowed to change this bucket's CORS rules.")
    if code in ("MalformedXML", "InvalidRequest", "InvalidArgument"):
        return HTTPException(422, "The storage service rejected the rules: "
                                  f"{str(err.get('Message') or code)[:300]}")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


def _summary(rules: list[dict]) -> list[dict]:
    return [{"id": r.get("id"), "origins": r["allowedOrigins"], "methods": r["allowedMethods"],
             "headers": r["allowedHeaders"], "expose": r["exposeHeaders"],
             "maxAge": r["maxAgeSeconds"]} for r in rules]


@router.get("/{name}/cors")
async def get_cors(name: str, request: Request, acct: Account):
    short, _ = await _own(request, acct, name)
    try:
        rules = await request.app.state.s3.call(acct.uid, _read, short)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    return JSONResponse(view(rules), headers={"Cache-Control": "no-store"})


@router.put("/{name}/cors")
async def put_cors(name: str, body: Body, request: Request, acct: Account, user: User):
    short, full = await _own(request, acct, name)
    try:
        new = normalize(body.rules)
    except Invalid as exc:
        first = exc.errors[0]
        return JSONResponse(status_code=422, content={
            "detail": first["message"] if first["rule"] is None
            else f"Rule {first['rule']}: {first['message']}", "errors": exc.errors})
    try:
        res = await request.app.state.s3.call(acct.uid, _apply, short, new, body.etag)
    except EtagMismatch as exc:
        return JSONResponse(status_code=409, content={
            "detail": "The CORS rules changed since you opened them. "
                      "Reload to see the current rules.",
            "etagMismatch": True, "current": view(exc.current)})
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    before, after = res["before"], res["after"]
    changed = before != after
    if changed:
        await _audit(request, user.username, "bucket.cors", full, uid=acct.uid,
                     rules_before=len(before), rules_after=len(after),
                     before=_capped(_summary(before)), after=_capped(_summary(after)),
                     warnings=len(warnings_for(after)))
    return {**view(after), "changed": changed}
