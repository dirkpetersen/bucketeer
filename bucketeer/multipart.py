"""Unfinished (multipart) uploads under /api/buckets/{name}/multipart (Advanced UI only).

The manual counterpart of the simple lifecycle rule "abort incomplete multipart uploads after N
days". It lists and aborts unfinished uploads only; it is NOT an object browser. The server never
looks at `advancedMode`; authorization is the same as for every bucket route
(`require_active_rgw_user`, own bucket verified in croit -> 404, the user's OWN S3 key through
`app.state.s3`, CSRF on POST). Responses are `Cache-Control: no-store`.

- `GET ?prefix=&keyMarker=&uploadIdMarker=&limit=` -> `{uploads: [{key, uploadId, initiated,
  initiator?, storageClass?}], truncated, nextKeyMarker, nextUploadIdMarker, totalShown}`: ONE
  ListMultipartUploads page (`limit` 1..100, default 50), so one GET is one S3 call. Pass the two
  `next*` values back as the markers to continue. Part counts and sizes are deliberately not
  offered: they would need one ListParts call per upload. `prefix`, `keyMarker` are at most
  1024 bytes without control characters (else 422), like share-link keys.
- `POST /abort` takes exactly one of two forms:
  - `{uploads: [{key, uploadId}]}` (1..100): aborts those.
  - `{olderThanDays: 1..36500, prefix?, confirm: true}`: lists the bucket's unfinished uploads
    started more than N days ago and aborts them. At most 1000 are aborted per request and at
    most 10,000 uploads are scanned (10 list pages), so one request does bounded work; the
    answer has `truncated: true` when more may remain and the UI repeats. WITHOUT
    `confirm: true` nothing is written: 409 `{requiresConfirmation, count, truncated}` where
    `count` is how many it would abort now (at least `count` when `truncated`).
  Each abort is independent. Answer `{aborted: [{key, uploadId}], failed: [{key, uploadId,
  error}], notFound: [{key, uploadId}], truncated}`; NoSuchUpload means it is already gone and
  counts as `notFound`. Other S3 errors are per-upload `failed` entries (error = S3 code).
  If the bucket vanishes midway, the rest are reported as `failed` (NoSuchBucket) and what was
  already aborted is kept in the answer and the audit. An upload start time without a time
  zone fails closed: 502 and nothing aborted (missing start times are never aborted).
  A whole-request failure (no key, S3 unreachable before anything ran) is the usual 4xx/5xx.
- Audit `bucket.multipart_abort` (target = the bucket) only when something was aborted or
  failed: `uid`, `mode` (selected|older_than), counts `aborted`/`failed`/`notFound`,
  `olderThanDays`, `prefix`, `truncated` and at most 50 distinct keys, 8 KB in all (never
  upload IDs).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from .auth import SessionUser, require_user
from .buckets import _audit, _full, _own_buckets, _own_name
from .rgw_user import RgwAccount, require_active_rgw_user
from .share_links import _check_text

log = logging.getLogger("bucketeer.multipart")

router = APIRouter(prefix="/buckets", tags=["multipart"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

DEFAULT_LIMIT, MAX_LIMIT = 50, 100
MAX_SELECTED = 100
MAX_ABORT = 1000  # aborts per older-than request
SCAN_PAGE, SCAN_PAGES = 1000, 10  # at most 10,000 uploads looked at per request
MAX_DAYS = 36500
AUDIT_KEYS = 50
AUDIT_BYTES = 8192  # total size of the audited key list


class NaiveTimestamp(Exception):
    """S3 sent a start time without a time zone; age cannot be judged, so nothing is aborted."""


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: StrictStr
    uploadId: StrictStr


class AbortBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uploads: list[Item] | None = None
    olderThanDays: StrictInt | None = None
    prefix: StrictStr | None = None
    confirm: StrictBool | None = None


def _code(exc: Exception) -> str | None:
    return exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None


def _s3_error(exc: Exception) -> HTTPException:
    code = _code(exc)
    log.warning("multipart S3 call failed: %s", code or type(exc).__name__)
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code == "AccessDenied":
        return HTTPException(403, "You are not allowed to list or abort uploads in this bucket.")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


async def _own(request: Request, acct: RgwAccount, name: str) -> tuple[str, str]:
    short = _own_name(name)
    full = _full(acct.uid, short)
    if not any(b.get("bucket") == full
               for b in await _own_buckets(request.app.state.croit, acct.uid)):
        raise HTTPException(404, "Bucket not found.")
    return short, full


def _stamp(when: Any) -> str | None:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(when, datetime) \
        else None


def _row(u: dict) -> dict:
    ini = u.get("Initiator") or {}
    out = {"key": u["Key"], "uploadId": u["UploadId"], "initiated": _stamp(u.get("Initiated"))}
    who = ini.get("DisplayName") or ini.get("ID")
    if who:
        out["initiator"] = who
    if u.get("StorageClass"):
        out["storageClass"] = u["StorageClass"]
    return out


def _list_page(s3, bucket: str, prefix: str, key_marker: str, upload_marker: str,
               limit: int) -> dict:
    args: dict[str, Any] = {"Bucket": bucket, "MaxUploads": limit}
    if prefix:
        args["Prefix"] = prefix
    if key_marker:
        args["KeyMarker"] = key_marker
        if upload_marker:
            args["UploadIdMarker"] = upload_marker
    return s3.list_multipart_uploads(**args)


def _older(s3, bucket: str, prefix: str, cutoff: datetime) -> tuple[list[dict], bool]:
    """Uploads started before `cutoff`, at most MAX_ABORT (+ whether more may remain)."""
    found: list[dict] = []
    key_m = up_m = ""
    for _ in range(SCAN_PAGES):
        page = _list_page(s3, bucket, prefix, key_m, up_m, SCAN_PAGE)
        for u in page.get("Uploads") or []:
            when = u.get("Initiated")
            if not isinstance(when, datetime):
                continue  # no usable start time: never aborted
            if when.tzinfo is None:
                raise NaiveTimestamp
            if when < cutoff:
                if len(found) >= MAX_ABORT:
                    return found, True
                found.append({"key": u["Key"], "uploadId": u["UploadId"]})
        if not page.get("IsTruncated"):
            return found, False
        key_m, up_m = page.get("NextKeyMarker") or "", page.get("NextUploadIdMarker") or ""
        if not key_m:
            return found, False
    return found, True  # scan budget used up


def _abort_all(s3, bucket: str, items: list[dict]) -> dict:
    res: dict[str, list] = {"aborted": [], "failed": [], "notFound": []}
    for pos, it in enumerate(items):
        ref = {"key": it["key"], "uploadId": it["uploadId"]}
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=it["key"], UploadId=it["uploadId"])
            res["aborted"].append(ref)
        except ClientError as exc:
            code = _code(exc) or "Error"
            if code == "NoSuchUpload":
                res["notFound"].append(ref)
            elif code == "NoSuchBucket":  # the bucket is gone: nothing else can work
                res["failed"].append({**ref, "error": code})
                res["failed"].extend({"key": r["key"], "uploadId": r["uploadId"], "error": code}
                                     for r in items[pos + 1:])
                break
            else:
                res["failed"].append({**ref, "error": code})
        except Exception as exc:  # network trouble for this one upload
            log.warning("multipart abort failed: %s", type(exc).__name__)
            res["failed"].append({**ref, "error": "Unavailable"})
    return res


@router.get("/{name}/multipart")
async def list_uploads(name: str, request: Request, acct: Account, response: Response,
                       prefix: str = "", keyMarker: str = "", uploadIdMarker: str = "",
                       limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT):
    response.headers["Cache-Control"] = "no-store"
    _check_text(prefix, "prefix", allow_empty=True)
    _check_text(keyMarker, "key marker", allow_empty=True)
    _check_text(uploadIdMarker, "upload marker", allow_empty=True)
    short, _ = await _own(request, acct, name)
    try:
        page = await request.app.state.s3.call(
            acct.uid, _list_page, short, prefix, keyMarker, uploadIdMarker, limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    uploads = [_row(u) for u in (page.get("Uploads") or [])][:limit]
    more = bool(page.get("IsTruncated"))
    return {"uploads": uploads, "truncated": more,
            "nextKeyMarker": (page.get("NextKeyMarker") or None) if more else None,
            "nextUploadIdMarker": (page.get("NextUploadIdMarker") or None) if more else None,
            "totalShown": len(uploads)}


@router.post("/{name}/multipart/abort")
async def abort_uploads(name: str, body: AbortBody, request: Request, acct: Account,
                        user: User, response: Response):
    response.headers["Cache-Control"] = "no-store"
    selected = body.uploads is not None
    if selected == (body.olderThanDays is not None):
        raise HTTPException(422, "Send either a list of uploads or olderThanDays, not both.")
    prefix = ""
    if selected:
        if body.prefix is not None or body.confirm is not None:
            raise HTTPException(422, "prefix and confirm belong to the older-than form.")
        if not 1 <= len(body.uploads) <= MAX_SELECTED:
            raise HTTPException(422, f"Choose 1 to {MAX_SELECTED} uploads per request.")
        for it in body.uploads:
            _check_text(it.key, "object key", allow_empty=False)
            _check_text(it.uploadId, "upload ID", allow_empty=False)
    else:
        if not 1 <= body.olderThanDays <= MAX_DAYS:
            raise HTTPException(422, f"Days must be between 1 and {MAX_DAYS}.")
        prefix = _check_text(body.prefix or "", "prefix", allow_empty=True)
    short, full = await _own(request, acct, name)
    s3 = request.app.state.s3
    truncated = False
    try:
        if selected:
            items = [{"key": i.key, "uploadId": i.uploadId} for i in body.uploads]
        else:
            cutoff = datetime.now(UTC) - timedelta(days=body.olderThanDays)
            items, truncated = await s3.call(acct.uid, _older, short, prefix, cutoff)
            if body.confirm is not True:
                return JSONResponse(status_code=409, content={
                    "detail": f"This would abort {len(items)}"
                              f"{'+' if truncated else ''} unfinished uploads. Confirm to go on.",
                    "requiresConfirmation": True, "count": len(items), "truncated": truncated})
        res = await s3.call(acct.uid, _abort_all, short, items) if items else \
            {"aborted": [], "failed": [], "notFound": []}
    except HTTPException:
        raise
    except NaiveTimestamp:
        raise HTTPException(502, "The storage service returned upload times that cannot be "
                                 "compared. Nothing was aborted.") from None
    except Exception as exc:
        raise _s3_error(exc) from None
    if res["aborted"] or res["failed"]:
        keys, size = [], 0
        for k in sorted({r["key"] for r in res["aborted"] + res["failed"]})[:AUDIT_KEYS]:
            size += len(k.encode()) + 4
            if size > AUDIT_BYTES:
                break
            keys.append(k)
        details: dict[str, Any] = {
            "uid": acct.uid, "mode": "selected" if selected else "older_than",
            "aborted": len(res["aborted"]), "failed": len(res["failed"]),
            "notFound": len(res["notFound"]), "truncated": truncated, "keys": keys}
        if not selected:
            details.update(olderThanDays=body.olderThanDays, prefix=prefix)
        await _audit(request, user.username, "bucket.multipart_abort", full, **details)
    return {**res, "truncated": truncated}
