"""Object lock under /api/buckets/{name}/ (Advanced UI only): the bucket's default retention,
and retention and legal hold of one object chosen by key.

Object lock can only be switched on when a bucket is created (create dialog), never later. The
server never looks at `advancedMode`; authorization is the same as for every bucket route
(`require_active_rgw_user`, own bucket verified in croit, the user's OWN S3 key, CSRF on PUT).

Default retention (what new objects get):
- `GET /object-lock` -> `{enabled, defaultRetention: {mode, days|years}|null, etag}`. A bucket
  without object lock answers `enabled: false` (and no etag use); PUT then gives 409.
- `PUT /object-lock {defaultRetention: {mode GOVERNANCE|COMPLIANCE, days 1..36500 | years 1..100}
  | null, etag, confirm?}` -> GET shape + `changed`. Etag re-checked inside `bucket_lock` (409
  `etagMismatch`). A COMPLIANCE default that is not already set needs `confirm: true`, else 409
  `requiresConfirmation` (objects written while it is set cannot be deleted by anyone, admins
  included, until their retention expires).

One object (`key` query parameter; `versionId` optional = latest). The state is read with one
`head_object`, so nothing here lists or downloads objects:
- `GET /objects/lock?key=&versionId=` -> `{key, versionId, retention: {mode, retainUntil}|null,
  legalHold: ON|OFF}` (an expired retention reads as null).
- `PUT /objects/retention?key=&versionId= {mode, retainUntil, confirm?, bypassGovernance?}`;
  `mode: null` (with `retainUntil: null`) removes the retention. retainUntil is ISO 8601 (naive =
  UTC), in the future and at most 100 years away. Rules, judged on the state read inside the lock:
  COMPLIANCE can only be lengthened (never shortened, removed or turned into GOVERNANCE: 409
  `locked`); a COMPLIANCE result needs `confirm`; shortening or removing GOVERNANCE needs
  `bypassGovernance: true` (409 `requiresBypass` otherwise). `BypassGovernanceRetention` is sent
  to S3 only when that case applies AND the user said so. Lengthening, and GOVERNANCE ->
  COMPLIANCE with a later date, need neither. 409 carries `requiresConfirmation` and/or
  `requiresBypass` for everything still missing.
- `PUT /objects/legal-hold?key=&versionId= {status: ON|OFF}`; same status = no change.
Both answer the GET shape plus `changed`. Audit `bucket.object_lock`, `object.retention`,
`object.legal_hold` (before/after, only when changed; never keys or secrets).
"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import s3docs
from .auth import SessionUser, require_user
from .buckets import _audit, _object_lock_on
from .rgw_user import RgwAccount, require_active_rgw_user
from .share_links import _check_text, _code, _own

log = logging.getLogger("bucketeer.object_lock")

router = APIRouter(prefix="/buckets", tags=["object-lock"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

MODES = ("GOVERNANCE", "COMPLIANCE")
MAX_DAYS = 36500
MAX_YEARS = 100
MAX_AHEAD = timedelta(days=36500)
NO_LOCK = ("This bucket was created without object lock. It can't be added later.")


class DefaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaultRetention: dict | None  # required: null removes the default
    etag: str
    confirm: bool = False


class RetentionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["GOVERNANCE", "COMPLIANCE"] | None
    retainUntil: str | None = None
    confirm: bool = False
    bypassGovernance: bool = False


class HoldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ON", "OFF"]


class NotLocked(Exception):
    pass


class EtagMismatch(Exception):
    def __init__(self, current: dict):
        self.current = current


class Needs(Exception):
    def __init__(self, confirm: bool = False, bypass: bool = False):
        self.confirm, self.bypass = confirm, bypass


class Refused(Exception):
    def __init__(self, message: str):
        self.message = message


# -- default retention --

def normalize_default(raw: dict | None) -> dict | None:
    """Validate a submitted default retention -> {mode, days|years} (422 on anything else)."""
    if raw is None:
        return None
    extra = sorted(set(raw) - {"mode", "days", "years"})
    if extra:
        raise HTTPException(422, f"Unknown field {extra[0]!r}.")
    mode = raw.get("mode")
    if mode not in MODES:
        raise HTTPException(422, "Choose Governance or Compliance as the mode.")
    days, years = raw.get("days"), raw.get("years")
    if (days is None) == (years is None):
        raise HTTPException(422, "Give the period either in days or in years, not both.")
    unit, limit = ("days", MAX_DAYS) if days is not None else ("years", MAX_YEARS)
    n = days if days is not None else years
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= limit:
        raise HTTPException(422, f"The period must be a whole number of {unit} from 1 to {limit}.")
    return {"mode": mode, unit: n}


def _default_of(cfg: dict) -> dict | None:
    dr = ((cfg.get("Rule") or {}).get("DefaultRetention")) or {}
    mode = dr.get("Mode")
    if mode not in MODES:
        return None
    if (dr.get("Days") or 0) > 0:
        return {"mode": mode, "days": int(dr["Days"])}
    if (dr.get("Years") or 0) > 0:
        return {"mode": mode, "years": int(dr["Years"])}
    return None


def _read_default(s3, bucket: str) -> dict:
    try:
        cfg = s3.get_object_lock_configuration(Bucket=bucket)["ObjectLockConfiguration"]
    except ClientError as exc:
        if _code(exc) in ("ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"):
            return default_view(False, None)
        raise
    if cfg.get("ObjectLockEnabled") != "Enabled":
        return default_view(False, None)
    return default_view(True, _default_of(cfg))


def default_view(enabled: bool, default: dict | None) -> dict:
    etag = hashlib.sha256(json.dumps([enabled, default], sort_keys=True).encode()).hexdigest()
    return {"enabled": enabled, "defaultRetention": default, "etag": etag}


def _apply_default(s3, bucket: str, new: dict | None, etag: str, confirm: bool) -> dict:
    with s3docs.bucket_lock(bucket):
        cur = _read_default(s3, bucket)
        if not cur["enabled"]:
            raise NotLocked
        if cur["etag"] != etag:
            raise EtagMismatch(cur)
        if cur["defaultRetention"] == new:
            return {"before": cur, "after": cur}
        if new and new["mode"] == "COMPLIANCE" and not confirm:
            raise Needs(confirm=True)
        cfg: dict[str, Any] = {"ObjectLockEnabled": "Enabled"}
        if new:
            period = {"Days": new["days"]} if "days" in new else {"Years": new["years"]}
            cfg["Rule"] = {"DefaultRetention": {"Mode": new["mode"], **period}}
        s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration=cfg)
        try:
            after = _read_default(s3, bucket)
        except Exception:  # the write succeeded: never lose the audit entry over the read-back
            log.warning("object lock read-back failed after a successful write", exc_info=True)
            after = default_view(True, new)
        return {"before": cur, "after": after}


# -- one object --

def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _object_view(head: dict, now: datetime) -> dict:
    until = head.get("ObjectLockRetainUntilDate")
    mode = head.get("ObjectLockMode")
    retention = None
    if mode in MODES and isinstance(until, datetime) and until.astimezone(UTC) > now:
        retention = {"mode": mode, "retainUntil": _iso(until)}
    hold = "ON" if head.get("ObjectLockLegalHoldStatus") == "ON" else "OFF"
    return {"retention": retention, "legalHold": hold, "versionId": head.get("VersionId")}


def _head(s3, bucket: str, key: str, version: str | None, now: datetime) -> dict:
    if not _object_lock_on(s3, bucket):
        raise NotLocked
    args = {"Bucket": bucket, "Key": key}
    if version:
        args["VersionId"] = version
    return _object_view(s3.head_object(**args), now)


def _args(bucket: str, key: str, version: str | None) -> dict:
    return {"Bucket": bucket, "Key": key, **({"VersionId": version} if version else {})}


def parse_until(text: str | None, now: datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(text.strip()) if text else None
    except (ValueError, AttributeError):
        dt = None
    if dt is None:
        raise HTTPException(422, "Enter the date until which the file stays locked.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        dt = dt.astimezone(UTC).replace(microsecond=0)
    except OverflowError:  # e.g. 9999-12-31T23:00:00-05:00 does not fit in UTC
        raise HTTPException(422, "The date can be at most 100 years from now.") from None
    if dt <= now:
        raise HTTPException(422, "The date must be in the future.")
    if dt > now + MAX_AHEAD:
        raise HTTPException(422, "The date can be at most 100 years from now.")
    return dt


def _apply_retention(s3, bucket: str, key: str, version: str | None, mode: str | None,
                     until: datetime | None, confirm: bool, bypass: bool) -> dict:
    now = datetime.now(UTC)
    with s3docs.bucket_lock(bucket):
        before = _head(s3, bucket, key, version, now)
        cur = before["retention"]
        cur_until = _from_iso(cur["retainUntil"]) if cur else None
        if cur and cur["mode"] == "COMPLIANCE":
            if mode is None or mode != "COMPLIANCE":
                raise Refused("This file is locked in Compliance mode. The lock can't be removed "
                              "or weakened by anyone until it expires. It can only be extended.")
            if until < cur_until:
                raise Refused("This file is locked in Compliance mode until "
                              f"{cur['retainUntil']}. The date can't be moved earlier by anyone.")
        if cur and mode == cur["mode"] and until == cur_until:
            return {"before": before, "after": before, "bypassUsed": False}
        if not cur and mode is None:
            return {"before": before, "after": before, "bypassUsed": False}
        needs_bypass = bool(cur and cur["mode"] == "GOVERNANCE"
                            and (mode is None or until < cur_until))
        needs_confirm = mode == "COMPLIANCE"
        if (needs_bypass and not bypass) or (needs_confirm and not confirm):
            raise Needs(confirm=needs_confirm and not confirm, bypass=needs_bypass and not bypass)
        retention = {"Mode": mode, "RetainUntilDate": until} if mode else {}
        extra = {"BypassGovernanceRetention": True} if needs_bypass else {}
        pinned = _args(bucket, key, version or before["versionId"])
        s3.put_object_retention(**pinned, Retention=retention, **extra)
        try:
            after = _head(s3, bucket, key, version, datetime.now(UTC))
        except Exception:
            log.warning("object lock read-back failed after a successful write", exc_info=True)
            after = {**before, "retention": {"mode": mode, "retainUntil": _iso(until)}
                     if mode else None}
        return {"before": before, "after": after, "bypassUsed": needs_bypass}


def _apply_hold(s3, bucket: str, key: str, version: str | None, status: str) -> dict:
    with s3docs.bucket_lock(bucket):
        before = _head(s3, bucket, key, version, datetime.now(UTC))
        if before["legalHold"] == status:
            return {"before": before, "after": before}
        pinned = _args(bucket, key, version or before["versionId"])
        s3.put_object_legal_hold(**pinned, LegalHold={"Status": status})
        return {"before": before, "after": {**before, "legalHold": status}}


# -- errors and shared plumbing --

def _s3_error(exc: Exception) -> HTTPException:
    code = _code(exc)
    log.warning("object lock S3 call failed: %s", code or type(exc).__name__)
    if code in ("NoSuchKey", "NotFound", "404"):
        return HTTPException(404, "There is no file with that name in this bucket.")
    if code == "NoSuchVersion":
        return HTTPException(404, "That version of the file does not exist.")
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code == "ObjectLockConfigurationNotFoundError":
        return HTTPException(409, NO_LOCK)
    if code in ("InvalidRequest", "InvalidBucketState"):
        # The lock configuration was verified before this call, so this is not "no lock".
        return HTTPException(409, f"The storage service refused this lock change ({code}).")
    if code in ("405", "MethodNotAllowed"):
        return HTTPException(409, "That version is a delete marker. Pick a real version.")
    if code in ("AccessDenied", "403"):
        return HTTPException(403, (
            "You are not allowed to change the lock of this file. It may have been uploaded by "
            "another user or a bucket key, or your account lacks the permission needed."))
    if code in ("InvalidArgument", "MalformedXML"):
        return HTTPException(422, "The storage service did not accept this lock setting.")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


async def _run(request: Request, acct: RgwAccount, fn, *args):
    try:
        return await request.app.state.s3.call(acct.uid, fn, *args)
    except NotLocked:
        raise HTTPException(409, NO_LOCK) from None
    except HTTPException:
        raise
    except (Needs, Refused, EtagMismatch):
        raise
    except Exception as exc:
        raise _s3_error(exc) from None


def _object_params(key: str, versionId: str | None) -> tuple[str, str | None]:
    key = _check_text(key, "object key", allow_empty=False)
    if versionId is not None:
        versionId = _check_text(versionId, "version id", allow_empty=False)
    return key, versionId


def _needs_response(exc: Needs, extra: str) -> JSONResponse:
    body: dict[str, Any] = {}
    parts = []
    if exc.bypass:
        body["requiresBypass"] = True
        parts.append("This lock is in Governance mode. Shortening or removing it needs your "
                     "explicit OK to bypass governance retention.")
    if exc.confirm:
        body["requiresConfirmation"] = True
        parts.append(extra)
    return JSONResponse(status_code=409, content={"detail": " ".join(parts), **body})


COMPLIANCE_OBJECT = ("Compliance mode can't be undone: nobody, administrators included, can "
                     "delete this file or shorten the lock until the date has passed.")
COMPLIANCE_DEFAULT = ("A Compliance default can't be undone for the files it covers: every file "
                      "written while it is set can't be deleted by anyone, administrators "
                      "included, until its retention expires.")


# -- routes --

@router.get("/{name}/object-lock")
async def get_object_lock(name: str, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    short, _ = await _own(request, acct, name)
    return await _run(request, acct, _read_default, short)


@router.put("/{name}/object-lock")
async def put_object_lock(name: str, body: DefaultBody, request: Request, response: Response,
                          acct: Account, user: User):
    response.headers["Cache-Control"] = "no-store"
    new = normalize_default(body.defaultRetention)
    short, full = await _own(request, acct, name)
    try:
        res = await _run(request, acct, _apply_default, short, new, body.etag, body.confirm)
    except EtagMismatch as exc:
        return JSONResponse(status_code=409, content={
            "detail": "The default retention changed since you opened it. Review the current "
                      "setting and try again.", "etagMismatch": True, "current": exc.current})
    except Needs as exc:
        return _needs_response(exc, COMPLIANCE_DEFAULT)
    before, after = res["before"]["defaultRetention"], res["after"]["defaultRetention"]
    changed = before != after
    if changed:
        await _audit(request, user.username, "bucket.object_lock", full, uid=acct.uid,
                     before=before, after=after)
    return {**res["after"], "changed": changed}


@router.get("/{name}/objects/lock")
async def get_lock(name: str, key: str, request: Request, response: Response, acct: Account,
                   versionId: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    key, versionId = _object_params(key, versionId)
    short, _ = await _own(request, acct, name)
    view = await _run(request, acct, _head, short, key, versionId, datetime.now(UTC))
    return {"key": key, **view}


@router.put("/{name}/objects/retention")
async def put_retention(name: str, key: str, body: RetentionBody, request: Request,
                        response: Response, acct: Account, user: User,
                        versionId: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    key, versionId = _object_params(key, versionId)
    if body.mode is None:
        if body.retainUntil is not None:
            raise HTTPException(422, "Choose a mode, or leave the date out to remove the lock.")
        until = None
    else:
        until = parse_until(body.retainUntil, datetime.now(UTC))
    short, full = await _own(request, acct, name)
    try:
        res = await _run(request, acct, _apply_retention, short, key, versionId, body.mode,
                         until, body.confirm, body.bypassGovernance)
    except Refused as exc:
        return JSONResponse(status_code=409, content={"detail": exc.message, "locked": True})
    except Needs as exc:
        return _needs_response(exc, COMPLIANCE_OBJECT)
    before, after = res["before"]["retention"], res["after"]["retention"]
    changed = before != after
    if changed:
        await _audit(request, user.username, "object.retention", full, uid=acct.uid, key=key,
                     versionId=versionId, before=before, after=after,
                     bypassGovernance=res["bypassUsed"])
    return {"key": key, **res["after"], "changed": changed}


@router.put("/{name}/objects/legal-hold")
async def put_legal_hold(name: str, key: str, body: HoldBody, request: Request,
                         response: Response, acct: Account, user: User,
                         versionId: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    key, versionId = _object_params(key, versionId)
    short, full = await _own(request, acct, name)
    res = await _run(request, acct, _apply_hold, short, key, versionId, body.status)
    before, after = res["before"]["legalHold"], res["after"]["legalHold"]
    changed = before != after
    if changed:
        await _audit(request, user.username, "object.legal_hold", full, uid=acct.uid, key=key,
                     versionId=versionId, before=before, after=after)
    return {"key": key, **res["after"], "changed": changed}
