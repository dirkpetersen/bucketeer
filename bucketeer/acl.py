"""Bucket and object ACLs under /api/buckets/{name}/ (Advanced UI only).

ACLs are the legacy S3 permission model, but RGW still supports them. Authorization is the same
as every bucket route (`require_active_rgw_user`, own bucket verified in croit, the user's OWN S3
key via `app.state.s3`, CSRF on PUT); the server never looks at `advancedMode`. There is no
object browser: the object is chosen by its key (autocomplete is `GET /objects`).

- `GET /acl` and `GET /objects/acl?key=` -> `{scope: bucket|object, key?, owner: {id, name},
  grants: [{kind, id, uri, email, name, permission, scope, removable}], etag, permissions}`.
  `kind` is `owner` (always FULL_CONTROL, `removable: false`), `user` (a known RGW user),
  `unknown` (a canonical ID croit does not list, or an e-mail grantee: shown as "unknown user"),
  or `group` (`uri`; `scope` is `public` for AllUsers, `signed_in` for AuthenticatedUsers,
  `logs` for LogDelivery, else null). RGW's reserved user id `anonymous` is shown as a `group`
  with scope `public` (`id: "anonymous"`) and follows the same rules: removable, never addable.
  The etag also covers the scope and object key. `permissions` lists what the scope allows
  (WRITE is for buckets only).
- `PUT` (same paths) `{etag, keep: [{id|uri|email, permission}], add: [{login, permission}]}`
  replaces the non-owner grants with `keep` plus `add`. The owner's FULL_CONTROL is always
  written and cannot be changed. `keep` can only name grantees already in the ACL (a person's
  permission may change); `add` finds people exactly like sharing (`sharing.resolve_recipient`:
  rate limited per owner, Q8 eligibility, uniform 404, S3 account auto-created). **Adding a
  group grant is refused (422)**: public access is not enabled in Bucketeer yet (Q12); an
  existing group grant can be kept or removed. A stale `etag` is 409 `{etagMismatch, current}`;
  the etag is re-checked inside the per-bucket lock. Returns the GET shape plus `changed`.
- RGW may refuse to show or change the ACL of an object the bucket owner did not write (403).
- Audit `bucket.acl` / `object.acl` (target_type bucket, tenant-qualified name; before/after
  grants as `{who, permission}`, `key` for objects), only when something changed.
"""

import hashlib
import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .auth import SessionUser, require_user
from .bucket_keys import _bucket_lock
from .buckets import _audit
from .rgw_user import RgwAccount, require_active_rgw_user
from .share_links import _check_text, _code, _own
from .sharing import resolve_recipient

log = logging.getLogger("bucketeer.acl")

router = APIRouter(prefix="/buckets", tags=["acl"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

GROUP_BASE = "http://acs.amazonaws.com/groups/"
ALL_USERS = GROUP_BASE + "global/AllUsers"
SIGNED_IN = GROUP_BASE + "global/AuthenticatedUsers"
LOG_DELIVERY = GROUP_BASE + "s3/LogDelivery"
GROUP_SCOPES = {ALL_USERS: "public", SIGNED_IN: "signed_in", LOG_DELIVERY: "logs"}
BUCKET_PERMS = ("READ", "WRITE", "READ_ACP", "WRITE_ACP", "FULL_CONTROL")
OBJECT_PERMS = ("READ", "READ_ACP", "WRITE_ACP", "FULL_CONTROL")
MAX_ADD = 10
MAX_GRANTS = 100  # RGW's own limit
MSG_NO_PUBLIC = ("Public access is not enabled in Bucketeer yet, so group grants cannot be "
                 "added. You can keep or remove the ones that already exist.")
Permission = Literal["READ", "WRITE", "READ_ACP", "WRITE_ACP", "FULL_CONTROL"]


class Keep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr | None = Field(default=None, max_length=256)
    uri: StrictStr | None = Field(default=None, max_length=512)
    email: StrictStr | None = Field(default=None, max_length=256)
    permission: Permission


class Add(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: StrictStr = Field(max_length=256)
    permission: Permission


class AclBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    etag: StrictStr
    keep: list[Keep] = Field(default_factory=list, max_length=MAX_GRANTS)
    add: list[Add] = Field(default_factory=list, max_length=MAX_ADD)


class EtagMismatch(Exception):
    def __init__(self, current: dict):
        self.current = current


# -- pure helpers ----------------------------------------------------------------------------


ANONYMOUS = "anonymous"  # RGW evaluates anonymous requests as this user id


def _scope(key: str | None) -> tuple[str, str | None]:
    return ("bucket", None) if key is None else ("object", key)


def _grantee_key(g: dict) -> tuple[str, str]:
    """(type, identity) of a raw grantee."""
    if g.get("Type") == "Group" or g.get("URI"):
        return "uri", g.get("URI") or ""
    if g.get("ID"):
        return "id", g["ID"]
    return "email", g.get("EmailAddress") or ""


def etag_of(acl: dict, scope: str = "bucket", key: str | None = None) -> str:
    """Hash of scope, object key, owner and grants: a bucket etag never fits an object."""
    items = sorted([*_grantee_key(g["Grantee"]), g["Permission"]]
                   for g in acl.get("Grants") or [] if g.get("Grantee"))
    owner = (acl.get("Owner") or {}).get("ID", "")
    text = json.dumps([scope, key, owner, items], separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def view(acl: dict, perms: tuple[str, ...], known: set[str] | None, **extra) -> dict[str, Any]:
    owner = acl.get("Owner") or {}
    owner_id = owner.get("ID", "")
    grants: list[dict] = [{"kind": "owner", "id": owner_id, "uri": None, "email": None,
                           "name": owner.get("DisplayName") or owner_id,
                           "permission": "FULL_CONTROL", "scope": None, "removable": False}]
    for g in acl.get("Grants") or []:
        grantee, perm = g.get("Grantee") or {}, g.get("Permission")
        kind_, ident = _grantee_key(grantee)
        if kind_ == "id" and ident == owner_id:
            continue  # the owner's own entry is the fixed first row
        entry = {"kind": "unknown", "id": None, "uri": None, "email": None, "name": "",
                 "permission": perm, "scope": None, "removable": True}
        if kind_ == "uri":
            entry.update(kind="group", uri=ident, scope=GROUP_SCOPES.get(ident),
                         name=ident.rsplit("/", 1)[-1] or ident)
        elif kind_ == "id" and ident == ANONYMOUS:  # RGW's anonymous user: public access
            entry.update(kind="group", id=ident, scope="public", name=ident)
        elif kind_ == "id":
            is_known = known is None or ident in known or ident.partition("$")[2] in known
            entry.update(kind="user" if is_known else "unknown", id=ident,
                         name=grantee.get("DisplayName") or ident)
        else:
            entry.update(email=ident, name=ident)
        grants.append(entry)
    return {"owner": {"id": owner_id, "name": owner.get("DisplayName") or owner_id},
            "grants": grants,
            "etag": etag_of(acl, extra.get("scope", "bucket"), extra.get("key")),
            "permissions": list(perms), **extra}


def summary(acl: dict) -> list[dict]:
    """Compact grant list for the audit log."""
    return [{"who": g["id"] or g["uri"] or g["email"], "permission": g["permission"]}
            for g in view(acl, (), None)["grants"]]


def build(acl: dict, body: AclBody, added: list[tuple[str, str]], perms: tuple[str, ...]) -> dict:
    """The new ACL document: owner FULL_CONTROL, `keep` (existing grantees only), `add`.
    Raises HTTPException 422 for anything that would add a group grant or touch the owner."""
    owner_id = (acl.get("Owner") or {}).get("ID", "")
    current = [g for g in acl.get("Grants") or [] if g.get("Grantee")]
    existing_pairs = {(*_grantee_key(g["Grantee"]), g["Permission"]) for g in current}
    existing_ids = {_grantee_key(g["Grantee"]) for g in current}
    out: list[tuple[tuple[str, str], str]] = []
    for k in body.keep:
        given = [(t, v) for t, v in (("id", k.id), ("uri", k.uri), ("email", k.email)) if v]
        if len(given) != 1:
            raise HTTPException(422, "Each kept grant needs exactly one of id, uri or email.")
        key = given[0]
        if key == ("id", owner_id):
            raise HTTPException(422, "The owner always has full control and cannot be changed.")
        if key[0] == "uri" or key == ("id", ANONYMOUS):
            if (*key, k.permission) not in existing_pairs:
                raise HTTPException(422, MSG_NO_PUBLIC)
        elif key not in existing_ids:
            raise HTTPException(422, "A kept grant must already be in the ACL. Use add to give "
                                     "access to a new person.")
        elif key[0] != "id" and (*key, k.permission) not in existing_pairs:
            raise HTTPException(422, "The permission of this kind of grant cannot be changed. "
                                     "Remove it instead.")
        if k.permission not in perms and (*key, k.permission) not in existing_pairs:
            raise HTTPException(422, f"{k.permission} does not apply here.")
        out.append((key, k.permission))
    for uid, perm in added:
        if uid == ANONYMOUS:
            raise HTTPException(422, MSG_NO_PUBLIC)
        if perm not in perms:
            raise HTTPException(422, f"{perm} does not apply here.")
        out.append((("id", uid), perm))
    grants = [{"Grantee": {"Type": "CanonicalUser", "ID": owner_id}, "Permission": "FULL_CONTROL"}]
    seen = {(("id", owner_id), "FULL_CONTROL")}
    for key, perm in out:
        if (key, perm) in seen:
            continue
        seen.add((key, perm))
        if key[0] == "uri":
            grantee = {"Type": "Group", "URI": key[1]}
        elif key[0] == "email":
            grantee = {"Type": "AmazonCustomerByEmail", "EmailAddress": key[1]}
        else:
            grantee = {"Type": "CanonicalUser", "ID": key[1]}
        grants.append({"Grantee": grantee, "Permission": perm})
    if len(grants) > MAX_GRANTS:
        raise HTTPException(422, f"An ACL can hold at most {MAX_GRANTS} grants.")
    return {"Owner": {"ID": owner_id}, "Grants": grants}


# -- S3 ------------------------------------------------------------------------------------------


def _read(s3, bucket: str, key: str | None) -> dict:
    r = (s3.get_bucket_acl(Bucket=bucket) if key is None
         else s3.get_object_acl(Bucket=bucket, Key=key))
    return {"Owner": r.get("Owner") or {}, "Grants": r.get("Grants") or []}


def _apply(s3, bucket: str, key: str | None, body: AclBody, added: list[tuple[str, str]],
           perms: tuple[str, ...]):
    """Inside the bucket lock: re-read, compare the etag, write. -> (before, after)."""
    before = _read(s3, bucket, key)
    if etag_of(before, *_scope(key)) != body.etag:
        raise EtagMismatch(before)
    doc = build(before, body, added, perms)
    if key is None:
        s3.put_bucket_acl(Bucket=bucket, AccessControlPolicy=doc)
    else:
        s3.put_object_acl(Bucket=bucket, Key=key, AccessControlPolicy=doc)
    return before, _read(s3, bucket, key)


def _s3_error(exc: Exception, obj: bool) -> HTTPException:
    code = _code(exc)
    log.warning("acl S3 call failed: %s", code or type(exc).__name__)
    if code in ("NoSuchKey", "NotFound", "404"):
        return HTTPException(404, "There is no file with that name in this bucket.")
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code in ("AccessDenied", "403"):
        return HTTPException(403, (
            "You are not allowed to see or change the permissions of this file. It may have "
            "been uploaded by a bucket key or another user." if obj else
            "You are not allowed to see or change the permissions of this bucket."))
    if code in ("InvalidArgument", "MalformedACLError", "InvalidRequest",
                "UnresolvableGrantByEmailAddress"):
        return HTTPException(422, "The storage service did not accept these permissions.")
    return HTTPException(502, "Could not reach the S3 service for this bucket. Try again.")


async def _known_ids(request: Request) -> set[str] | None:
    try:
        return {u for u in await request.app.state.croit.list_user_names() if isinstance(u, str)}
    except Exception:
        return None  # unknown: show IDs as users rather than guess


async def _get(request: Request, acct: RgwAccount, name: str, key: str | None, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if key is not None:
        key = _check_text(key, "object key", allow_empty=False)
    short, full = await _own(request, acct, name)
    try:
        acl = await request.app.state.s3.call(acct.uid, _read, short, key)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc, key is not None) from None
    extra = {"scope": "bucket"} if key is None else {"scope": "object", "key": key}
    return view(acl, BUCKET_PERMS if key is None else OBJECT_PERMS,
                await _known_ids(request), **extra)


async def _put(request: Request, acct: RgwAccount, user: SessionUser, name: str, key: str | None,
               body: AclBody, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if key is not None:
        key = _check_text(key, "object key", allow_empty=False)
    short, full = await _own(request, acct, name)
    perms = BUCKET_PERMS if key is None else OBJECT_PERMS
    if any(g.permission not in perms for g in body.add):
        raise HTTPException(422, "WRITE only applies to buckets.")
    added: list[tuple[str, str]] = []
    created: list[str] = []
    for a in body.add:
        person, rcpt, uid = await resolve_recipient(request, acct, a.login)
        added.append((uid, a.permission))
        if rcpt.created:
            created.append(uid)
    try:
        async with _bucket_lock(request, full):
            before, after = await request.app.state.s3.call(
                acct.uid, _apply, short, key, body, added, perms)
    except EtagMismatch as e:
        scoped = {"scope": "bucket"} if key is None else {"scope": "object", "key": key}
        return JSONResponse(status_code=409, content={
            "detail": "The permissions changed since you opened them. Review the current ones "
                      "and try again.",
            "etagMismatch": True,
            "current": view(e.current, perms, await _known_ids(request), **scoped)})
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc, key is not None) from None
    changed = etag_of(before) != etag_of(after)  # same scope on both sides
    if changed:
        extra = {} if key is None else {"key": key}
        await _audit(request, user.username, "bucket.acl" if key is None else "object.acl", full,
                     uid=acct.uid, before=summary(before), after=summary(after),
                     accountsCreated=created, **extra)
    extra = {"scope": "bucket"} if key is None else {"scope": "object", "key": key}
    return view(after, perms, await _known_ids(request), changed=changed, **extra)


@router.get("/{name}/acl")
async def get_bucket_acl(name: str, request: Request, response: Response, acct: Account):
    return await _get(request, acct, name, None, response)


@router.put("/{name}/acl")
async def put_bucket_acl(name: str, body: AclBody, request: Request, response: Response,
                         acct: Account, user: User):
    return await _put(request, acct, user, name, None, body, response)


@router.get("/{name}/objects/acl")
async def get_object_acl(name: str, key: str, request: Request, response: Response,
                         acct: Account):
    return await _get(request, acct, name, key, response)


@router.put("/{name}/objects/acl")
async def put_object_acl(name: str, key: str, body: AclBody, request: Request,
                         response: Response, acct: Account, user: User):
    return await _put(request, acct, user, name, key, body, response)
