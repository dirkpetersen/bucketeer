"""Presigned share links and the read-only key autocomplete under /api/buckets/{name}/.

Both routes use `require_active_rgw_user`, verify ownership in croit (404 otherwise) and
act with the USER's own S3 key (`app.state.s3`). Responses are `Cache-Control: no-store`.
A presigned URL carries a signature made with the user's key: it is never logged or
audited (the audit scrubber would redact `Signature=` values anyway), only bucket, key
and expiry are. The link stops working if that key is rotated or deleted, or the account
is suspended.

- `GET /objects?prefix=&limit=` -> `{prefix, entries: [{key, size, lastModified}],
  prefixes: [{prefix}], truncated}`: one `list_objects_v2` page with Delimiter "/" and
  `MaxKeys=limit` (default 20, max 50), so keys plus prefixes never exceed `limit`. It is
  an autocomplete aid, NOT a browser: no pagination, download, upload or delete.
  `prefix` is at most 1024 bytes of UTF-8 without control characters (else 422).
- `POST /share-link {key, expiresIn}` (CSRF) -> `{url, expiresAt}`. `expiresIn` is
  60..604800 seconds (the UI offers 1 hour, 1 day, 7 days). The object must exist
  (`head_object`, else 404). The URL is a SigV4 presigned GET for the public S3 endpoint
  and the configured addressing style (the same client every S3 call uses). Audited as
  `bucket.share_link` with `uid`, `key`, `expiresAt`, `expiresIn`.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from .auth import SessionUser, require_user
from .buckets import _audit, _own_buckets, _own_name
from .buckets import _full as _full_name
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.share_links")

router = APIRouter(prefix="/buckets", tags=["share-links"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

MAX_KEY_BYTES = 1024
MIN_EXPIRY, MAX_EXPIRY = 60, 7 * 24 * 3600  # seconds; SigV4 itself caps at 7 days
DEFAULT_LIMIT, MAX_LIMIT = 20, 50


class ShareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: StrictStr
    expiresIn: StrictInt


def _check_text(value: str, what: str, allow_empty: bool) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise HTTPException(422, f"The {what} contains characters that are not allowed.") from None
    if size > MAX_KEY_BYTES or (size == 0 and not allow_empty):
        raise HTTPException(422, f"The {what} must be {'at most' if allow_empty else '1 to'} "
                                 f"{MAX_KEY_BYTES} bytes long.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise HTTPException(422, f"The {what} contains characters that are not allowed.")
    return value


async def _own(request: Request, acct: RgwAccount, name: str) -> tuple[str, str]:
    short = _own_name(name)
    full = _full_name(acct.uid, short)
    if not any(b.get("bucket") == full for b in await _own_buckets(request.app.state.croit,
                                                                   acct.uid)):
        raise HTTPException(404, "Bucket not found.")
    return short, full


def _code(exc: Exception) -> str | None:
    return exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None


def _s3_error(exc: Exception) -> HTTPException:
    code = _code(exc)
    log.warning("share-link S3 call failed: %s", code or type(exc).__name__)
    if code in ("NoSuchBucket",):
        return HTTPException(404, "Bucket not found.")
    if code in ("AccessDenied", "403"):  # HEAD responses have no body, so the code is "403"
        return HTTPException(403, "You don't have access to this object, it may have been "
                                  "uploaded by a bucket key or another user.")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


def _list(s3, bucket: str, prefix: str, limit: int) -> dict:
    return s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/", MaxKeys=limit)


def _head_and_sign(s3, bucket: str, key: str, expires_in: int) -> str | None:
    """The presigned URL, or None when the object does not exist."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _code(exc) in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in,
        HttpMethod="GET")


@router.get("/{name}/objects")
async def list_keys(name: str, request: Request, acct: Account, response: Response,
                    prefix: str = "",
                    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT):
    response.headers["Cache-Control"] = "no-store"
    _check_text(prefix, "prefix", allow_empty=True)
    short, _ = await _own(request, acct, name)
    try:
        page = await request.app.state.s3.call(acct.uid, _list, short, prefix, limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    entries = []
    for o in page.get("Contents") or []:
        when = o.get("LastModified")
        entries.append({
            "key": o["Key"], "size": int(o.get("Size") or 0),
            "lastModified": when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(when, datetime) else None,
        })
    prefixes = [{"prefix": p["Prefix"]} for p in page.get("CommonPrefixes") or []]
    # MaxKeys already bounds keys + prefixes; slice anyway so the promise holds on any backend
    entries, prefixes = entries[:limit], prefixes[:max(0, limit - len(entries))]
    return {"prefix": prefix, "entries": entries, "prefixes": prefixes,
            "truncated": bool(page.get("IsTruncated"))}


@router.post("/{name}/share-link")
async def create_share_link(name: str, body: ShareBody, request: Request, acct: Account,
                            user: User, response: Response):
    response.headers["Cache-Control"] = "no-store"
    key = _check_text(body.key, "object key", allow_empty=False)
    if not MIN_EXPIRY <= body.expiresIn <= MAX_EXPIRY:
        raise HTTPException(422, "A link must last between 1 minute and 7 days.")
    short, full = await _own(request, acct, name)
    expires_at = (datetime.now(UTC) + timedelta(seconds=body.expiresIn)).replace(microsecond=0)
    try:
        url = await request.app.state.s3.call(
            acct.uid, _head_and_sign, short, key, body.expiresIn)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    if url is None:
        raise HTTPException(404, "There is no file with that name in this bucket.")
    stamp = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    await _audit(request, user.username, "bucket.share_link", full, uid=acct.uid, key=key,
                 expiresAt=stamp, expiresIn=body.expiresIn)
    return {"url": url, "expiresAt": stamp}
