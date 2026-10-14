"""Simple lifecycle rules under /api/buckets/{name}/lifecycle.

Three rules Bucketeer owns (`bucketeer-expire-current`, `bucketeer-expire-noncurrent`,
`bucketeer-abort-multipart`, built by `s3docs`), written with the USER's own S3 key via
`s3docs.update_bucket_lifecycle`. Every other rule is preserved untouched and only
counted. Both routes use `require_active_rgw_user`, verify ownership in croit (404
otherwise) and, under the parent `/api` router, CSRF applies to the PUT.

- `GET` -> `{expireCurrentDays|null, expireNoncurrentDays|null, abortMultipartDays|null,
  foreignRules: n, versioning: off|enabled|suspended|null}`. Read lazily (one S3 call),
  never part of the bucket list.
- `PUT {expireCurrentDays, expireNoncurrentDays, abortMultipartDays, confirm?}` (all
  three required, null removes the rule; days are integers 1..s3docs.MAX_DAYS) -> the GET
  shape plus `changed` and, when a noncurrent rule is set on a bucket whose versioning is
  off, `hint`. Enabling or shortening `expireCurrentDays` deletes data later, so it needs
  `confirm: true`; otherwise 409 `{requiresConfirmation: true}` and nothing is written.
  Audited `bucket.lifecycle` (before/after of the three values), only when something changed.
"""

import logging
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from . import s3docs
from .auth import SessionUser, require_user
from .buckets import _STATES, _audit, _full, _own_buckets, _own_name
from .croit import CroitClientError
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.lifecycle")

router = APIRouter(prefix="/buckets", tags=["lifecycle"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

FIELDS = ("expireCurrentDays", "expireNoncurrentDays", "abortMultipartDays")
_RULES = {
    "expireCurrentDays": (s3docs.EXPIRE_CURRENT_ID, s3docs.expire_current_rule,
                          lambda r: (r.get("Expiration") or {}).get("Days")),
    "expireNoncurrentDays": (s3docs.EXPIRE_NONCURRENT_ID, s3docs.expire_noncurrent_rule,
                             lambda r: (r.get("NoncurrentVersionExpiration") or {})
                             .get("NoncurrentDays")),
    "abortMultipartDays": (s3docs.ABORT_MULTIPART_ID, s3docs.abort_multipart_rule,
                           lambda r: (r.get("AbortIncompleteMultipartUpload") or {})
                           .get("DaysAfterInitiation")),
}


class LifecycleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expireCurrentDays: StrictInt | None
    expireNoncurrentDays: StrictInt | None
    abortMultipartDays: StrictInt | None
    confirm: StrictBool = False


class ConfirmNeeded(Exception):
    pass


def summarize(config: dict | None) -> dict[str, Any]:
    """The three simple values and the number of rules not managed by Bucketeer."""
    rules = (config or {}).get("Rules") or []
    by_id = {r.get("ID"): r for r in rules}
    out: dict[str, Any] = {}
    for field, (rule_id, _, read) in _RULES.items():
        rule = by_id.get(rule_id)
        days = read(rule) if rule else None
        out[field] = days if isinstance(days, int) else None
    out["foreignRules"] = sum(1 for r in rules if not s3docs.is_managed(r.get("ID")))
    return out


def _read(s3, bucket: str) -> dict | None:
    try:
        return s3docs.parse_lifecycle(s3.get_bucket_lifecycle_configuration(Bucket=bucket))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchLifecycleConfiguration":
            raise
        return None


def _write(s3, bucket: str, wanted: dict[str, int | None], confirm: bool):
    """Apply the three values; returns (summary before, summary after)."""
    seen: dict[str, Any] = {}

    def mutate(config):
        seen["before"] = summarize(config)
        build = {f: _RULES[f][1](d) for f, d in wanted.items() if d is not None}  # validates
        cur = seen["before"]["expireCurrentDays"]
        new = wanted["expireCurrentDays"]
        if new is not None and (cur is None or new < cur) and not confirm:
            raise ConfirmNeeded
        doc = config
        gone = [_RULES[f][0] for f, d in wanted.items() if d is None]
        if gone:
            doc = s3docs.remove_rules(doc, gone)
        if build:
            doc = s3docs.upsert_rules(doc, build.values())
        return doc

    after = s3docs.update_bucket_lifecycle(s3, bucket, mutate)
    return seen["before"], summarize(after)


async def _own(request: Request, acct: RgwAccount, name: str) -> tuple[str, str]:
    short = _own_name(name)
    full = _full(acct.uid, short)
    if not any(b.get("bucket") == full for b in await _own_buckets(request.app.state.croit,
                                                                   acct.uid)):
        raise HTTPException(404, "Bucket not found.")
    return short, full


async def _versioning(request: Request, acct: RgwAccount, full: str) -> str | None:
    try:
        state = (await request.app.state.croit.get_bucket(full, owner=acct.uid)).get("versioning")
    except (CroitClientError, AttributeError):
        return None
    return _STATES.get(state)


def _s3_error(exc: Exception) -> HTTPException:
    code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None
    log.warning("lifecycle S3 call failed: %s", code or type(exc).__name__)
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code == "AccessDenied":
        return HTTPException(403, "You are not allowed to change this bucket's lifecycle.")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


@router.get("/{name}/lifecycle")
async def get_lifecycle(name: str, request: Request, acct: Account):
    short, full = await _own(request, acct, name)
    try:
        config = await request.app.state.s3.call(acct.uid, _read, short)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    return {**summarize(config), "versioning": await _versioning(request, acct, full)}


@router.put("/{name}/lifecycle")
async def put_lifecycle(name: str, body: LifecycleBody, request: Request, acct: Account,
                        user: User):
    short, full = await _own(request, acct, name)
    wanted = {f: getattr(body, f) for f in FIELDS}
    try:
        for f, d in wanted.items():
            if d is not None:
                _RULES[f][1](d)  # bounds check before any S3 call
    except s3docs.S3DocError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        before, after = await request.app.state.s3.call(
            acct.uid, _write, short, wanted, body.confirm)
    except ConfirmNeeded:
        return JSONResponse(status_code=409, content={
            "detail": "Expiring files deletes them for good. Confirm to continue.",
            "requiresConfirmation": True})
    except HTTPException:
        raise
    except s3docs.S3DocError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:
        raise _s3_error(exc) from None
    state = await _versioning(request, acct, full)
    changed = {f: before[f] for f in FIELDS} != {f: after[f] for f in FIELDS}
    if changed:
        await _audit(request, user.username, "bucket.lifecycle", full, uid=acct.uid,
                     before={f: before[f] for f in FIELDS}, after={f: after[f] for f in FIELDS})
    out = {**after, "versioning": state, "changed": changed}
    if after["expireNoncurrentDays"] is not None and state == "off":
        out["hint"] = ("Versioning is off for this bucket, so there are no old versions to "
                       "expire yet. The rule starts to apply once you turn versioning on.")
    return out


