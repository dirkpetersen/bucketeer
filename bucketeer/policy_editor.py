"""Raw bucket policy editor under /api/buckets/{name}/policy (Advanced UI only).

The one place that may change non-`bucketeer-*` statements, via
`s3docs.update_bucket_policy(..., allow_foreign=True)`. The server never looks at the
user's `advancedMode`: the toggle only hides the UI, authorization is the same as for
every bucket route (`require_active_rgw_user`, own bucket verified in croit, the user's
OWN S3 key, CSRF on PUT/DELETE under the parent `/api` router).

- `GET` -> `{policy: object|null, managedSids: [...], etag}`. `etag` is a SHA-256 of the
  canonical (sorted-key) normalized policy, `"none"`-hashed when there is no policy.
- `PUT {policy: object|string, etag, confirmManaged?}` validates the document (JSON object,
  at most 20480 bytes compact, Version 2012-10-17 / 2008-10-17 if present, only
  Version/Id/Statement at top level, every statement with Effect Allow|Deny, Principal or
  NotPrincipal, Action or NotAction, Resource or NotResource; a Sid must be a string and
  unique). Nothing else is judged: RGW rejects what it does not understand and that answer
  comes back as 422. An empty `Statement` list deletes the policy. Missing `Version` stays
  missing (AWS reads it as 2008-10-17), like everywhere else in `s3docs`.
- `DELETE {etag, confirmManaged?}` removes the whole policy (idempotent when none exists).
- Optimistic concurrency: the etag is re-checked INSIDE `s3docs.bucket_lock` (the same
  lock sharing and bucket keys take), so a share or key added after the editor was loaded
  is never clobbered: 409 `{etagMismatch: true, current: {policy, managedSids, etag}}`.
  Writers outside this process remain a narrow lost-update window (S3 has no conditional
  write).
- If the change adds, alters or removes any `bucketeer-*` statement (or the policy holding
  them is deleted) and `confirmManaged` is not true: 409 `{requiresConfirmation: true,
  managedChanges: [sid, ...]}`, nothing written. Sharing then shows `out_of_sync`
  (repairable); a bucket key whose statement is gone simply has no access. No Bucketeer
  rows are changed automatically.
- Public and self-lockout statements (Q12): a new or altered statement that is an `Allow`
  for everyone or a `Deny` that would lock the owner out is 422 `{detail, errors: [{statement
  (index), sid, kind: "public"|"lockout", message}]}`, nothing written, no audit entry. One
  already stored (whole statement identical after canonical JSON) may be kept or removed.
  Judged inside the lock against the stored policy; definitions in `policy_guard.py`. GET
  and every write answer also carry `flags: [{index, sid, kind}]` for the stored/new policy.
- Audit `bucket.policy` / `bucket.policy_delete`: statement counts, changed Sids and the
  before/after documents (policies hold no secrets; each omitted above 8 KB).
"""

import copy
import hashlib
import json
import logging
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from . import policy_guard, s3docs
from .auth import SessionUser, require_user
from .buckets import _audit, _full, _own_buckets, _own_name
from .rgw_user import RgwAccount, require_active_rgw_user, split_uid

log = logging.getLogger("bucketeer.policy_editor")

router = APIRouter(prefix="/buckets", tags=["policy"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

MAX_DEPTH = 32
MAX_POLICY_BYTES = 20480
MAX_INPUT_CHARS = 200_000
AUDIT_DOC_MAX = 8192
VERSIONS = ("2012-10-17", "2008-10-17")
TOP_KEYS = {"Version", "Id", "Statement"}


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: dict | str
    etag: str
    confirmManaged: StrictBool = False


class DeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    etag: str
    confirmManaged: StrictBool = False


class EtagMismatch(Exception):
    def __init__(self, current: dict | None):
        self.current = current


class Refused(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors


class ConfirmNeeded(Exception):
    def __init__(self, sids: list[str]):
        self.sids = sids


def etag_of(policy: dict | None) -> str:
    text = "none" if policy is None else json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def managed_sids(policy: dict | None) -> list[str]:
    stmts = (policy or {}).get("Statement", [])
    return [s["Sid"] for s in stmts if s3docs.is_managed(s.get("Sid"))]


def view(policy: dict | None, bucket: str | None = None, uid: str | None = None) -> dict[str, Any]:
    out = {"policy": policy, "managedSids": managed_sids(policy), "etag": etag_of(policy)}
    if bucket is not None and uid is not None:
        tenant, bare = split_uid(uid)
        out["flags"] = policy_guard.flags(policy, bucket, tenant, bare)
        # Only for the browser's early warning (it mirrors policy_guard); not authoritative.
        out["owner"] = {"arn": policy_guard.owner_arn(tenant, bare),
                        "tenant": tenant,
                        "bucketArns": policy_guard.bucket_arns(bucket, tenant)}
    return out


def too_deep(doc: Any, limit: int = MAX_DEPTH) -> bool:
    """True if dicts/lists nest deeper than `limit` (iterative: safe on any input)."""
    stack = [(doc, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (dict, list)):
            if depth > limit:
                return True
            kids = node.values() if isinstance(node, dict) else node
            stack.extend((k, depth + 1) for k in kids if isinstance(k, (dict, list)))
    return False


def _validate(raw: dict | str) -> dict | None:
    """Parse and check a submitted policy; returns the normalized document, or None when it
    has no statements (meaning: delete). Raises ValueError with a user-facing message."""
    if isinstance(raw, str):
        if len(raw) > MAX_INPUT_CHARS:
            raise ValueError("The policy is too large.")
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).") from None
    if not isinstance(raw, dict):
        raise ValueError("A bucket policy must be a JSON object.")
    if too_deep(raw):
        raise ValueError(f"The policy is nested too deeply (at most {MAX_DEPTH} levels).")
    if len(s3docs.serialize_policy(raw).encode()) > MAX_POLICY_BYTES:
        raise ValueError(f"A bucket policy can be at most {MAX_POLICY_BYTES // 1024} KB.")
    extra = sorted(set(raw) - TOP_KEYS)
    if extra:
        raise ValueError(f"Unknown top-level field {extra[0]!r}. Allowed: Version, Id, Statement.")
    if "Version" in raw and raw["Version"] not in VERSIONS:
        raise ValueError('Version must be "2012-10-17" or "2008-10-17".')
    if "Id" in raw and not isinstance(raw["Id"], str):
        raise ValueError("Id must be a string.")
    if "Statement" not in raw:
        raise ValueError("The policy needs a Statement.")
    stmts = raw["Statement"]
    if isinstance(stmts, dict):
        stmts = [stmts]
    if not isinstance(stmts, list) or not all(isinstance(s, dict) for s in stmts):
        raise ValueError("Statement must be an object or a list of objects.")
    for i, s in enumerate(stmts, 1):
        where = f"Statement {i}"
        if "Sid" in s and not isinstance(s["Sid"], str):
            raise ValueError(f"{where}: Sid must be a string.")
        if s.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f'{where}: Effect must be "Allow" or "Deny".')
        for pair in (("Principal", "NotPrincipal"), ("Action", "NotAction"),
                     ("Resource", "NotResource")):
            have = [k for k in pair if k in s]
            if len(have) != 1:
                raise ValueError(f"{where}: needs exactly one of {pair[0]} or {pair[1]}.")
    doc = dict(raw)
    doc["Statement"] = stmts
    try:
        return s3docs.parse_policy(doc)  # also rejects duplicate Sids; [] -> None
    except s3docs.S3DocError as exc:
        raise ValueError(str(exc)) from None


def validate(raw: dict | str) -> dict | None:
    try:
        return _validate(raw)
    except RecursionError:
        raise ValueError("The policy is nested too deeply or is too complex.") from None


def managed_changes(current: dict | None, new: dict | None) -> list[str]:
    """Sids of bucketeer-* statements that are added, altered or removed (sorted)."""
    def managed(doc):
        return {s["Sid"]: s for s in (doc or {}).get("Statement", [])
                if s3docs.is_managed(s.get("Sid"))}
    a, b = managed(current), managed(new)
    return sorted(sid for sid in a.keys() | b.keys() if a.get(sid) != b.get(sid))


def changed_sids(current: dict | None, new: dict | None) -> list[str]:
    def by_sid(doc):
        return {s.get("Sid"): s for s in (doc or {}).get("Statement", []) if s.get("Sid")}
    a, b = by_sid(current), by_sid(new)
    return sorted(sid for sid in a.keys() | b.keys() if a.get(sid) != b.get(sid))


def _read(s3, bucket: str) -> dict | None:
    try:
        return s3docs.parse_policy(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
            raise
        return None


def _apply(s3, bucket: str, uid: str, new: dict | None, etag: str, confirm: bool) -> dict:
    """Etag check, managed-change check and write, all under the bucket lock."""
    seen: dict[str, Any] = {}

    def mutate(current):
        if etag_of(current) != etag:
            raise EtagMismatch(current)
        seen["before"] = copy.deepcopy(current)
        if new is not None:
            tenant, bare = split_uid(uid)
            errors = policy_guard.refusals(current, new, bucket, tenant, bare)
            if errors:
                raise Refused(errors)
        seen["managed"] = managed_changes(current, new)
        if seen["managed"] and not confirm:
            raise ConfirmNeeded(seen["managed"])
        return copy.deepcopy(new)

    after = s3docs.update_bucket_policy(s3, bucket, mutate, allow_foreign=True)
    return {"before": seen["before"], "after": after, "managed": seen["managed"]}


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
    log.warning("policy S3 call failed: %s", code or type(exc).__name__)
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code == "AccessDenied":
        return HTTPException(403, "You are not allowed to change this bucket's policy.")
    if code in ("MalformedPolicy", "InvalidPolicyDocument"):
        return HTTPException(422, f"The storage service rejected the policy: "
                                  f"{str(err.get('Message') or code)[:300]}")
    if isinstance(exc, s3docs.S3DocError):
        return HTTPException(502, "The policy stored on this bucket cannot be read: "
                                  f"{str(exc)[:200]}")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


def _refused(exc: Refused) -> JSONResponse:
    return JSONResponse(status_code=422, content={
        "detail": exc.errors[0]["message"], "errors": exc.errors})


def _conflict(exc: EtagMismatch, bucket: str, uid: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": "The policy changed since you opened it (a share or key may have been "
                  "added). Reload to see the current policy.",
        "etagMismatch": True, "current": view(exc.current, bucket, uid)})


def _confirm_needed(exc: ConfirmNeeded, deleting: bool) -> JSONResponse:
    what = "Deleting the policy removes" if deleting else "This change touches"
    return JSONResponse(status_code=409, content={
        "detail": f"{what} statements Bucketeer manages for sharing or bucket keys. "
                  "Confirm to continue.",
        "requiresConfirmation": True, "managedChanges": exc.sids})


def _doc_detail(doc: dict | None) -> dict | None:
    if doc is None or len(json.dumps(doc)) <= AUDIT_DOC_MAX:
        return doc
    return {"omitted": "larger than 8 KB"}


async def _run(request: Request, acct: RgwAccount, user: SessionUser, short: str, full: str,
               new: dict | None, etag: str, confirm: bool, action: str):
    try:
        res = await request.app.state.s3.call(acct.uid, _apply, short, acct.uid, new, etag, confirm)
    except EtagMismatch as exc:
        return _conflict(exc, short, acct.uid)
    except Refused as exc:
        return _refused(exc)
    except ConfirmNeeded as exc:
        return _confirm_needed(exc, new is None)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    before, after = res["before"], res["after"]
    changed = before != after
    if changed:
        count = lambda d: len((d or {}).get("Statement", []))  # noqa: E731
        await _audit(request, user.username, action, full, uid=acct.uid,
                     statements_before=count(before), statements_after=count(after),
                     changed_sids=changed_sids(before, after), managed_changes=res["managed"],
                     before=_doc_detail(before), after=_doc_detail(after))
    return {**view(after, short, acct.uid), "changed": changed, "managedChanges": res["managed"]}


@router.get("/{name}/policy")
async def get_policy(name: str, request: Request, acct: Account):
    short, _ = await _own(request, acct, name)
    try:
        policy = await request.app.state.s3.call(acct.uid, _read, short)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    return JSONResponse(view(policy, short, acct.uid), headers={"Cache-Control": "no-store"})


@router.put("/{name}/policy")
async def put_policy(name: str, body: PolicyBody, request: Request, acct: Account, user: User):
    short, full = await _own(request, acct, name)
    try:
        new = validate(body.policy)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return await _run(request, acct, user, short, full, new, body.etag, body.confirmManaged,
                      "bucket.policy")


@router.delete("/{name}/policy")
async def delete_policy(name: str, body: DeleteBody, request: Request, acct: Account,
                        user: User):
    short, full = await _own(request, acct, name)
    return await _run(request, acct, user, short, full, None, body.etag, body.confirmManaged,
                      "bucket.policy_delete")
