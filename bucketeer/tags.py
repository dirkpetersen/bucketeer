"""Bucket tag editor under /api/buckets/{name}/tags (Advanced UI only).

The server never looks at `advancedMode`; authorization is the same as for every bucket route
(`require_active_rgw_user`, own bucket verified in croit, the user's OWN S3 key, CSRF on PUT).

- `GET` -> `{tags: [{key, value, managed}], etag}`; managed tags (`bucketeer-*`, any case) come
  first and are read only (the budget number lives in `bucketeer-budget`, edit it with
  `PUT /api/buckets/{name}/budget`). No tag set (NoSuchTagSet) -> `[]`. `etag` is a SHA-256 of
  the canonical tag list (managed ones included).
- `PUT {tags: [{key, value}], etag}` replaces the set of NON-managed tags; every managed tag is
  kept as it is. An empty list removes the user tags. Answers the GET shape plus `changed`.
  Validation failures -> 422 `{detail, errors: [{tag (1-based|null), field, message}]}`:
  key 1-128 and value 0-256 characters of letters, digits, space and `+ - = . _ : / @`
  (Unicode letters allowed, no control characters), unique keys (case sensitive), no `aws:`
  prefix, no `bucketeer-` prefix (case-insensitive, managed tags are not editable here), at
  most 50 tags in total counting the managed ones.
- The etag is re-checked INSIDE `s3docs.bucket_lock`: 409 `{etagMismatch: true, current}`.
- Audit `bucket.tags` (before/after user tags only; only when changed).
"""

import hashlib
import json
import logging
import re
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import s3docs
from .auth import SessionUser, require_user
from .buckets import _audit, _full, _own_buckets, _own_name
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.tags")

router = APIRouter(prefix="/buckets", tags=["tags"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

MAX_TAGS = 50
KEY_MAX = 128
VALUE_MAX = 256
AUDIT_MAX = 8192
# \w is Unicode (letters, digits, underscore) and excludes control characters.
CHARS = re.compile(r"\A[\w +\-=.:/@]*\Z")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[dict]
    etag: str


class EtagMismatch(Exception):
    def __init__(self, current: list[dict]):
        self.current = current


class Invalid(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors


def is_managed(key: str) -> bool:
    return key.lower().startswith(s3docs.PREFIX)


def _canon(tagset: list[dict]) -> list[dict]:
    """[{key, value, managed}] with managed first, each group in stored order."""
    rows = [{"key": t["Key"], "value": t.get("Value", ""), "managed": is_managed(t["Key"])}
            for t in tagset]
    return [r for r in rows if r["managed"]] + [r for r in rows if not r["managed"]]


def etag_of(rows: list[dict]) -> str:
    flat = sorted([r["key"], r["value"]] for r in rows)
    return hashlib.sha256(json.dumps(flat, separators=(",", ":")).encode()).hexdigest()


def view(rows: list[dict]) -> dict[str, Any]:
    return {"tags": rows, "etag": etag_of(rows)}


def normalize(raw: list[dict], managed_count: int = 0,
              stored: set[tuple[str, str]] = frozenset()) -> list[dict]:
    """Validate submitted user tags; raises Invalid with per-tag messages. A tag exactly equal
    to one already stored skips the charset, aws: and length rules (RGW accepts tags outside
    AWS's rules, and those must not block saving unrelated changes); uniqueness, the
    bucketeer- prefix and the total count always apply."""
    errs: list[dict] = []
    if len(raw) + managed_count > MAX_TAGS:
        room = MAX_TAGS - managed_count
        raise Invalid([{"tag": None, "field": "tags", "message": (
            f"A bucket can have at most {MAX_TAGS} tags"
            + (f", {managed_count} of them managed by Bucketeer, so at most {room} of yours."
               if managed_count else "."))}])
    out, seen = [], set()
    for i, t in enumerate(raw, 1):
        def err(field, message, i=i):
            errs.append({"tag": i, "field": field, "message": message})
        extra = sorted(set(t) - {"key", "value"})
        if extra:
            err("tag", f"Unknown field {extra[0]!r}.")
        key, value = t.get("key"), t.get("value", "")
        if value is None:
            value = ""
        if not isinstance(key, str) or not isinstance(value, str):
            err("key", "Key and value must be text.")
            continue
        keep = (key, value) in stored
        if not key:
            err("key", "Enter a key.")
        elif keep:
            pass
        elif len(key) > KEY_MAX:
            err("key", f"A key can have at most {KEY_MAX} characters.")
        elif not CHARS.match(key):
            err("key", "Keys can contain letters, digits, spaces and + - = . _ : / @ only.")
        elif key.lower().startswith("aws:"):
            err("key", "Keys starting with aws: are reserved by AWS.")
        if key and is_managed(key):
            err("key", "Keys starting with bucketeer- are managed by Bucketeer and cannot be "
                       "edited here.")
        elif key and key in seen:
            err("key", f"The key {key!r} is used twice.")
        if keep:
            pass
        elif len(value) > VALUE_MAX:
            err("value", f"A value can have at most {VALUE_MAX} characters.")
        elif not CHARS.match(value):
            err("value", "Values can contain letters, digits, spaces and + - = . _ : / @ only.")
        seen.add(key)
        out.append({"Key": key, "Value": value})
    if errs:
        raise Invalid(errs)
    return out


def _read(s3, bucket: str) -> list[dict]:
    try:
        return _canon(s3.get_bucket_tagging(Bucket=bucket)["TagSet"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchTagSet":
            raise
        return []


def _user(rows: list[dict]) -> list[dict]:
    return [{"key": r["key"], "value": r["value"]} for r in rows if not r["managed"]]


def _capped(tags: list[dict]) -> Any:
    return tags if len(json.dumps(tags)) <= AUDIT_MAX else {"omitted": "larger than 8 KB"}


def _apply(s3, bucket: str, raw: list[dict], etag: str) -> dict:
    with s3docs.bucket_lock(bucket):
        current = _read(s3, bucket)
        if etag_of(current) != etag:
            raise EtagMismatch(current)
        managed = [r for r in current if r["managed"]]
        new = normalize(raw, len(managed),
                        {(r["key"], r["value"]) for r in current if not r["managed"]})
        keep = [{"Key": r["key"], "Value": r["value"]} for r in managed]
        if _user(current) != [{"key": t["Key"], "value": t["Value"]} for t in new]:
            # allow_foreign: these ARE the user's own tags; the managed ones are kept above.
            s3docs.update_bucket_tagging(s3, bucket, lambda _old: keep + new or None,
                                         allow_foreign=True)
        else:
            return {"before": current, "after": current}
        try:
            after = _read(s3, bucket)
        except Exception:  # the write succeeded: never lose the audit entry over the read-back
            log.warning("tag read-back failed after a successful write", exc_info=True)
            after = _canon(keep + new)
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
    log.warning("tags S3 call failed: %s", code or type(exc).__name__)
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code == "AccessDenied":
        return HTTPException(403, "You are not allowed to change this bucket's tags.")
    if code in ("MalformedXML", "InvalidTag", "InvalidRequest", "InvalidArgument"):
        return HTTPException(422, "The storage service rejected the tags: "
                                  f"{str(err.get('Message') or code)[:300]}")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


def _invalid(exc: Invalid) -> JSONResponse:
    first = exc.errors[0]
    return JSONResponse(status_code=422, content={
        "detail": first["message"] if first["tag"] is None
        else f"Tag {first['tag']}: {first['message']}", "errors": exc.errors})


@router.get("/{name}/tags")
async def get_tags(name: str, request: Request, acct: Account):
    short, _ = await _own(request, acct, name)
    try:
        rows = await request.app.state.s3.call(acct.uid, _read, short)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    return JSONResponse(view(rows), headers={"Cache-Control": "no-store"})


@router.put("/{name}/tags")
async def put_tags(name: str, body: Body, request: Request, acct: Account, user: User):
    short, full = await _own(request, acct, name)
    try:
        res = await request.app.state.s3.call(acct.uid, _apply, short, body.tags, body.etag)
    except Invalid as exc:
        return _invalid(exc)
    except EtagMismatch as exc:
        return JSONResponse(status_code=409, content={
            "detail": "The tags changed since you opened them. Reload to see the current tags.",
            "etagMismatch": True, "current": view(exc.current)})
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    before, after = _user(res["before"]), _user(res["after"])
    changed = before != after
    if changed:
        await _audit(request, user.username, "bucket.tags", full, uid=acct.uid,
                     tags_before=len(before), tags_after=len(after),
                     before=_capped(before), after=_capped(after))
    return {**view(res["after"]), "changed": changed}
