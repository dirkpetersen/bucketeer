"""Subusers (Advanced): extra S3 identities of the signed-in user, with their own keys.

A subuser is `uid:name` on the user's RGW account with one permission (`read`, `write`,
`read-write` or `full-control`) that applies across ALL the user's buckets. It is not a bucket
key (see `bucket_keys.py`, which is scoped to one bucket). RGW suspends subusers together with
their user, so there is no cascade here. Whether RGW enforces the permission for S3 requests
(not only Swift) is a cluster check (TODO.md).

All routes: `require_active_rgw_user` (the uid comes only from the session, so another user's
subuser is simply a 404), CSRF via the parent router, `Cache-Control: no-store`, the per-uid
`KeyLocks` lock around every mutation. Works on both admin backends through the
`CroitClient` / `RgwAdminClient` subuser methods.

- `GET /api/subusers` -> `{subusers: [{name, permission, keys: [{accessKey}]}], limits:
  {maxSubusers, maxKeys}, permissions}`. Never a secret.
- `POST /api/subusers {name, permission, confirm?}` -> 201 `{name, permission, keys}`. Name
  `[a-z0-9-]`, 1-20 characters, no leading or trailing `-`. At most 5 subusers (else 409).
  A name is never reused: every name is recorded in SQLite (`subuser_names`) and an earlier
  one is refused even after deletion, as is a name RGW already has (409). `full-control`
  needs `confirm: true`, else 409 `{requiresConfirmation}`. The create makes exactly one
  key (`generate_key_pair`) and reads it back; if that fails the subuser is rolled back.
- `PATCH /api/subusers/{name} {permission, confirm?}`: the same confirm rule for full-control.
- `DELETE /api/subusers/{name}`: removes the subuser and all its keys.
- `POST /api/subusers/{name}/keys` -> `{accessKey}`: at most 2 keys per subuser (else 409).
- `POST /api/subusers/{name}/keys/{accessKey}/rotate` -> `{accessKey, oldKeyRemoved,
  message?}`: new key first, confirmed in the list, only then the old one is deleted.
- `DELETE /api/subusers/{name}/keys/{accessKey}`: the last key is refused (409); rotate it.
- `POST /api/subusers/{name}/reveal {accessKey}` -> `{accessKey, secretKey}` for a key of
  that subuser only (else 404). Audited BEFORE answering, never cached. This is the only
  response that carries a subuser secret.

Audit (`subuser.create|update|delete|reveal`, `subuser.key.create|rotate|rotate_failed|
delete`): uid, name, permission, access keys; never a secret. Subuser keys appear in the
backend's key list with `user` = `uid:name`, so the credentials screen (which keeps only
`user == uid`) never shows them as the user's own keys and they do not count towards
`MAX_KEYS`.
"""

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .admin import croit_http_error
from .credentials import _key_locks, generate_key_pair
from .croit import SUBUSER_PERMISSIONS, CroitClient, CroitClientError, CroitError
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.subusers")

MAX_SUBUSERS = 5
MAX_KEYS = 2  # per subuser, so a key can be rotated (new key first)
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?\Z")

router = APIRouter(prefix="/subusers", tags=["subusers"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]


class CreateBody(BaseModel):
    name: str = Field(max_length=64)
    permission: str = Field(max_length=32)
    confirm: bool = False


class UpdateBody(BaseModel):
    permission: str = Field(max_length=32)
    confirm: bool = False


class RevealBody(BaseModel):
    accessKey: str = Field(min_length=1, max_length=256)


def _croit(request: Request) -> CroitClient:
    return request.app.state.croit


def _identity(uid: str, name: str) -> str:
    return f"{uid}:{name}"


def _check_permission(permission: str, confirm: bool) -> None:
    if permission not in SUBUSER_PERMISSIONS:
        raise HTTPException(422, "The permission must be read, write, read-write or "
                                 "full-control.")
    if permission == "full-control" and not confirm:
        raise HTTPException(409, detail={
            "message": "Full control can do everything you can do, including changing bucket "
                       "policies and permissions and deleting buckets. Confirm to continue.",
            "requiresConfirmation": True})


async def _read(request: Request, fn, *args):
    try:
        return await fn(*args)
    except CroitClientError as e:
        raise croit_http_error(e) from None


async def _subusers(request: Request, uid: str) -> list[dict]:
    return await _read(request, _croit(request).list_subusers, uid)


async def _own_subuser(request: Request, uid: str, name: str) -> dict:
    """The subuser of the session user, else 404 (also for an invalid name)."""
    row = next((s for s in await _subusers(request, uid) if s["name"] == name), None)
    if row is None:
        raise HTTPException(404, "Subuser not found.")
    return row


async def _keys(request: Request, uid: str, name: str) -> list[dict]:
    keys = await _read(request, _croit(request).list_subuser_keys, uid, name)
    identity = _identity(uid, name)
    return [k for k in keys or [] if isinstance(k, dict) and k.get("access_key")
            and k.get("user") == identity]


async def _all_access_keys(request: Request, uid: str) -> set[str]:
    keys = await _read(request, _croit(request).list_keys, uid)
    return {k["access_key"] for k in keys or [] if isinstance(k, dict) and k.get("access_key")}


async def _audit(request: Request, acct: RgwAccount, action: str, target_type: str,
                 target: str, **details):
    db = request.app.state.db
    await db.run(db.audit.add, acct.username, action, target_type, target,
                 {"uid": acct.uid, **details})


async def _add_key(request: Request, uid: str, name: str) -> str:
    """put_subuser_key with a fresh pair; retries when the backend says the access key is
    taken. A generated access key already used by the user (own or subuser) is skipped."""
    taken = await _all_access_keys(request, uid)
    for _ in range(5):
        access, secret = generate_key_pair()
        if access in taken:
            continue
        try:
            await _croit(request).put_subuser_key(uid, name, access, secret)
        except CroitError as e:
            if e.status == 409:
                continue
            raise croit_http_error(e) from None
        except CroitClientError as e:
            raise croit_http_error(e) from None
        return access
    raise HTTPException(502, "Could not generate a unique access key. Try again.")


def _view(row: dict, keys: list[dict]) -> dict:
    return {"name": row["name"], "permission": row["permission"],
            "keys": [{"accessKey": k["access_key"]} for k in keys]}


@router.get("")
async def list_subusers(request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    rows = await _subusers(request, acct.uid)
    all_keys = await _read(request, _croit(request).list_keys, acct.uid)
    out = []
    for row in sorted(rows, key=lambda r: r["name"]):
        identity = _identity(acct.uid, row["name"])
        keys = sorted((k for k in all_keys or [] if isinstance(k, dict)
                       and k.get("user") == identity and k.get("access_key")),
                      key=lambda k: k["access_key"])
        out.append(_view(row, keys))
    return {"subusers": out, "permissions": list(SUBUSER_PERMISSIONS),
            "limits": {"maxSubusers": MAX_SUBUSERS, "maxKeys": MAX_KEYS}}


@router.post("", status_code=201)
async def create_subuser(body: CreateBody, request: Request, response: Response,
                         acct: Account):
    response.headers["Cache-Control"] = "no-store"
    if not NAME.match(body.name):
        raise HTTPException(422, "The name must be 1 to 20 characters: lowercase letters, "
                                 "digits and '-', not starting or ending with '-'.")
    _check_permission(body.permission, body.confirm)
    croit, db, uid = _croit(request), request.app.state.db, acct.uid
    async with _key_locks(request)(uid):
        existing = await _subusers(request, uid)
        if len(existing) >= MAX_SUBUSERS:
            raise HTTPException(409, f"You can have at most {MAX_SUBUSERS} subusers. "
                                     "Delete one you no longer use first.")
        if any(s["name"] == body.name for s in existing):
            raise HTTPException(409, "You already have a subuser with this name.")
        if not await db.run(db.subusers.claim, uid, body.name):
            raise HTTPException(409, "You already had a subuser with this name. Names are "
                                     "never reused; pick another one.")
        try:
            await croit.create_subuser(uid, body.name, body.permission)
        except CroitError as e:
            if e.status in (400, 404, 409, 422):  # refused: nothing was created
                await db.run(db.subusers.release, uid, body.name)
            else:
                await _fail_create(request, acct, body, e)
            raise croit_http_error(e) from None
        except CroitClientError as e:  # unknown outcome (timeout): check and roll back
            await _fail_create(request, acct, body, e)
            raise croit_http_error(e) from None
        try:
            access = await _add_key(request, uid, body.name)
            keys = await _keys(request, uid, body.name)
            if not any(k["access_key"] == access for k in keys):
                raise HTTPException(502, "The new key could not be confirmed.")
        except Exception as e:
            await _fail_create(request, acct, body, e)
            raise
        await _audit(request, acct, "subuser.create", "subuser", _identity(uid, body.name),
                     name=body.name, permission=body.permission, accessKey=access)
        return _view({"name": body.name, "permission": body.permission}, keys)


async def _fail_create(request: Request, acct: RgwAccount, body: CreateBody,
                       error: Exception) -> None:
    """Remove a half-made subuser (and its keys) and audit the failure. The outcome is
    unknown here (timeout, 5xx, or the subuser existed for a moment), so the name stays
    recorded and is never reused; only a definite 4xx refusal releases it (in the caller)."""
    uid = acct.uid
    try:
        if any(s["name"] == body.name for s in await _croit(request).list_subusers(uid)):
            await _croit(request).delete_subuser(uid, body.name)
    except Exception as e:  # best effort; the user can delete it from the list
        log.warning("subuser rollback for %s failed: %s", uid, type(e).__name__)
    await _audit(request, acct, "subuser.create_failed", "subuser",
                 _identity(uid, body.name), permission=body.permission,
                 reason=type(error).__name__)


@router.patch("/{name}")
async def update_subuser(name: str, body: UpdateBody, request: Request, response: Response,
                         acct: Account):
    response.headers["Cache-Control"] = "no-store"
    _check_permission(body.permission, body.confirm)
    async with _key_locks(request)(acct.uid):
        row = await _own_subuser(request, acct.uid, name)
        changed = row["permission"] != body.permission
        if changed:
            await _read(request, _croit(request).set_subuser_permission, acct.uid, name,
                        body.permission)
            await _audit(request, acct, "subuser.update", "subuser",
                         _identity(acct.uid, name), name=name,
                         old=row["permission"], permission=body.permission)
    return {"name": name, "permission": body.permission, "changed": changed}


@router.delete("/{name}")
async def delete_subuser(name: str, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    async with _key_locks(request)(acct.uid):
        row = await _own_subuser(request, acct.uid, name)
        keys = await _keys(request, acct.uid, name)
        await _read(request, _croit(request).delete_subuser, acct.uid, name)
        db = request.app.state.db
        await db.run(db.subusers.mark_deleted, acct.uid, name)
        await _audit(request, acct, "subuser.delete", "subuser", _identity(acct.uid, name),
                     name=name, permission=row["permission"],
                     accessKeys=[k["access_key"] for k in keys])
    return {"name": name}


@router.post("/{name}/keys")
async def create_key(name: str, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    async with _key_locks(request)(acct.uid):
        row = await _own_subuser(request, acct.uid, name)
        if len(await _keys(request, acct.uid, name)) >= MAX_KEYS:
            raise HTTPException(409, f"A subuser can have at most {MAX_KEYS} access keys. "
                                     "Delete one first, or rotate a key.")
        access = await _add_key(request, acct.uid, name)
        await _audit(request, acct, "subuser.key.create", "access_key", access,
                     subuser=name, permission=row["permission"])
    return {"accessKey": access}


@router.post("/{name}/keys/{access_key}/rotate")
async def rotate_key(name: str, access_key: str, request: Request, response: Response,
                     acct: Account):
    response.headers["Cache-Control"] = "no-store"
    croit = _croit(request)
    async with _key_locks(request)(acct.uid):
        row = await _own_subuser(request, acct.uid, name)
        keys = await _keys(request, acct.uid, name)
        if not any(k["access_key"] == access_key for k in keys):
            raise HTTPException(404, "Access key not found.")
        if len(keys) > MAX_KEYS:  # at exactly MAX_KEYS a rotation is fine
            raise HTTPException(409, f"This subuser has more than {MAX_KEYS} access keys. "
                                     "Delete the ones you no longer use first.")
        new = await _add_key(request, acct.uid, name)
        details = {"subuser": name, "permission": row["permission"], "newKey": new}
        # Confirm the new key is really there before the old one goes away.
        try:
            confirmed = any(k["access_key"] == new for k in await _keys(request, acct.uid, name))
        except HTTPException:
            await _audit(request, acct, "subuser.key.rotate_failed", "access_key", access_key,
                         old_key_removed=False, confirmed=None, **details)
            raise
        if not confirmed:
            await _audit(request, acct, "subuser.key.rotate_failed", "access_key", access_key,
                         old_key_removed=False, confirmed=False, **details)
            raise HTTPException(502, "The new key could not be confirmed, so the old key "
                                     "was kept. Check the keys and try again.")
        try:
            await croit.delete_subuser_key(acct.uid, name, access_key)
            removed = True
        except CroitClientError as e:
            log.warning("subuser rotate: old key not removed for %s: %s", acct.uid,
                        type(e).__name__)
            removed = False
        await _audit(request, acct, "subuser.key.rotate", "access_key", access_key,
                     old_key_removed=removed, **details)
    out: dict = {"accessKey": new, "oldKeyRemoved": removed}
    if not removed:
        out["message"] = ("The new key was created, but the old key could not be removed. "
                          "Delete the old key again once the new one works.")
    return out


@router.delete("/{name}/keys/{access_key}")
async def delete_key(name: str, access_key: str, request: Request, response: Response,
                     acct: Account):
    response.headers["Cache-Control"] = "no-store"
    async with _key_locks(request)(acct.uid):
        row = await _own_subuser(request, acct.uid, name)
        keys = await _keys(request, acct.uid, name)
        if not any(k["access_key"] == access_key for k in keys):
            raise HTTPException(404, "Access key not found.")
        if len(keys) <= 1:
            raise HTTPException(409, "This is the only access key of the subuser. Rotate it "
                                     "instead of deleting it.")
        await _read(request, _croit(request).delete_subuser_key, acct.uid, name, access_key)
        await _audit(request, acct, "subuser.key.delete", "access_key", access_key,
                     subuser=name, permission=row["permission"])
    return {"accessKey": access_key}


@router.post("/{name}/reveal")
async def reveal(name: str, body: RevealBody, request: Request, response: Response,
                 acct: Account):
    response.headers["Cache-Control"] = "no-store"
    row = await _own_subuser(request, acct.uid, name)
    key = next((k for k in await _keys(request, acct.uid, name)
                if k["access_key"] == body.accessKey and k.get("secret_key")), None)
    if key is None:
        raise HTTPException(404, "Access key not found.")
    # Audit first: if the log cannot be written, the secret is not handed out.
    await _audit(request, acct, "subuser.reveal", "access_key", key["access_key"],
                 subuser=name, permission=row["permission"])
    return {"accessKey": key["access_key"], "secretKey": key["secret_key"]}
