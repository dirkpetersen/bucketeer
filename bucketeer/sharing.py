"""Sharing a bucket with an AD user (CLAUDE.md "Sharing", Q8).

A share is one bucket-policy statement (`s3docs.share_statement`, Sid
`bucketeer-share-u-<uid>` / `bucketeer-share-t-<tenant>-<uid>`) written with the OWNER's own S3
key, plus a `shares` row for names and the recipient's view. The statement is the source of
truth for access; the list reconciles the two.

Recipients are exactly the people who could log in (Q8): the owner types a username or email,
the server finds that person with the login filter (`AdAuthenticator.lookup_user`: service
bind, escaped search, exactly one match, required group when set; NO password bind). Any
failure (unknown, ambiguous, not eligible) is one uniform 404. Lookups are rate limited per
owner (`LOOKUP_LIMIT` per `LOOKUP_WINDOW`) so the endpoint cannot enumerate the directory; it is
an exact lookup, never a search. A recipient without an S3 account gets one created on the spot
through `RgwResolver.resolve` exactly as their first login would (mapping wins, case-insensitive
match, `uid_taken` refusal, default quota, never an email).

Routes (owner side: `require_active_rgw_user`, own bucket verified in croit, `no-store`):
- `GET  /api/buckets/{name}/share-lookup?q=` -> `{username, displayName, isYou}`
- `GET  /api/buckets/{name}/shares` -> `{shares: [{id, recipient, displayName, uid, access,
  createdAt|null, status}], checked}`; `status`: `active`, `out_of_sync` (statement missing or
  not exactly the canonical one, e.g. widened Principal: repair with PUT),
  `unknown` (a `bucketeer-share-*` statement without a row, `id` is
  its Sid; revoke it), `unchecked` (policy could not be read, `checked: false`).
- `POST /api/buckets/{name}/shares {login, access: read|readwrite}` -> 201 the share view
  (`accountCreated` when the recipient's S3 account was made now); an existing live share of
  that person is changed instead (200, `updated: true`). Refused: yourself, a bucket-key user,
  a suspended recipient.
- `PUT  /api/buckets/{name}/shares/{id} {access}` change access (also repairs out_of_sync).
- `DELETE /api/buckets/{name}/shares/{id}` remove the statement, then mark the row revoked
  (idempotent; an unknown statement is addressed by its Sid).
- `GET /api/shared-with-me` -> `{shares: [{bucket, address, owner, access, sharedAt}]}`: live
  rows whose recipient uid is the session's. `address` is what to put after `s3://` (the
  `tenant:bucket` form when the owner is in another tenant).
Audit (target_type bucket, tenant-qualified name): `bucket.share`, `bucket.share_update`,
`bucket.unshare`. A deleted bucket's shares are marked revoked (`revoke_for_deleted_bucket`).
"""

import logging
import time
from collections import deque
from typing import Annotated, Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import s3docs
from .auth import SessionUser, require_user
from .bucket_keys import _bucket_lock, _bucket_name, _full, _own_bucket
from .croit import CroitClientError
from .ldapauth import InvalidCredentials, LdapUnavailable
from .rgw_user import (
    CREATION_FAILED,
    MSG_UNAVAILABLE,
    RESERVED_UIDS,
    SUSPENDED,
    UNAVAILABLE,
    RgwAccount,
    is_service_uid,
    require_active_rgw_user,
    same_uid,
    split_uid,
)

log = logging.getLogger("bucketeer.sharing")

LOOKUP_LIMIT = 30  # lookups (and share requests) per owner ...
LOOKUP_WINDOW = 900  # ... per 15 minutes
MSG_NOT_FOUND = ("No eligible person found with that username or email address. "
                 "They must be able to sign in to Bucketeer.")
SHARE_PREFIX = s3docs.PREFIX + "share-"

router = APIRouter(tags=["sharing"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]


class ShareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(max_length=256)
    access: Literal["read", "readwrite"]


class AccessBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access: Literal["read", "readwrite"]


# -- helpers ---------------------------------------------------------------------------


def _throttle(request: Request, owner: str) -> None:
    """Per-owner sliding window; 429 when exceeded. In memory, resets on restart."""
    state = request.app.state
    if not hasattr(state, "share_lookups"):
        state.share_lookups = {}
    clock = getattr(state.throttle, "clock", time.time)
    now = clock()
    dq: deque[float] = state.share_lookups.setdefault(owner, deque())
    while dq and dq[0] <= now - LOOKUP_WINDOW:
        dq.popleft()
    if len(dq) >= LOOKUP_LIMIT:
        retry = max(int(dq[0] + LOOKUP_WINDOW - now) + 1, 1)
        raise HTTPException(429, "Too many lookups. Try again later.",
                            headers={"Retry-After": str(retry)})
    dq.append(now)


async def _find_person(request: Request, owner: str, login: str):
    """The AD person for `login` or a uniform 404. Rate limited per owner."""
    login = (login or "").strip()
    if not login:
        raise HTTPException(422, "Enter a username or email address.")
    _throttle(request, owner)
    try:
        return await request.app.state.authenticator.lookup_user(login)
    except InvalidCredentials as e:
        log.info("share lookup by %s found nobody (%s)", owner, e.reason)
        raise HTTPException(404, MSG_NOT_FOUND) from None
    except LdapUnavailable:
        raise HTTPException(503, "The directory is temporarily unavailable. "
                                 "Try again shortly.") from None


async def resolve_recipient(request: Request, acct: RgwAccount, login: str):
    """Look a person up like a share does (rate limited, Q8 eligibility, uniform 404) and
    resolve their RGW uid, creating the S3 account on the spot. Refuses yourself, a
    suspended or unusable account and bucket-key users. Returns (person, RgwAccount, uid)."""
    db = request.app.state.db
    person = await _find_person(request, acct.username, login)
    if person.username.lower() == acct.username.lower():
        raise HTTPException(409, "That is you. The bucket is already yours.")

    rcpt = await request.app.state.rgw.resolve(person.username, person.display_name)
    if rcpt.status == UNAVAILABLE:
        raise HTTPException(503, MSG_UNAVAILABLE)
    if rcpt.status == CREATION_FAILED or not rcpt.uid:
        raise HTTPException(409, "An S3 account could not be set up for that person. "
                                 "Contact your administrator.")
    if rcpt.status == SUSPENDED:
        raise HTTPException(409, "That person's S3 account is suspended, so the bucket cannot "
                                 "be shared with them now.")
    uid = rcpt.uid
    try:
        service = await is_service_uid(request.app.state.croit, uid)
    except CroitClientError:
        raise HTTPException(503, MSG_UNAVAILABLE) from None
    if split_uid(uid)[1].lower() in RESERVED_UIDS or service:
        raise HTTPException(409, "That account name is reserved and cannot be used.")
    if same_uid(uid, acct.uid):
        raise HTTPException(409, "That person uses the same S3 account as you.")
    if await db.run(db.bucket_keys.get_by_uid, uid):
        raise HTTPException(409, "That account is a bucket key and cannot be a share recipient.")
    return person, rcpt, uid


def _statement_view(st: dict) -> dict[str, Any]:
    """{sid, uid, access} of a bucketeer-share statement (best effort)."""
    principal = ((st.get("Principal") or {}).get("AWS") if isinstance(st.get("Principal"), dict)
                 else None)
    if isinstance(principal, list):
        principal = principal[0] if principal else None
    uid = ""
    if isinstance(principal, str) and ":user/" in principal:
        head, _, bare = principal.partition(":user/")
        tenant = head.rpartition(":")[2]
        uid = f"{tenant}${bare}" if tenant else bare
    actions = st.get("Action") or []
    actions = [actions] if isinstance(actions, str) else actions
    access = "readwrite" if "s3:PutObject" in actions else "read"
    return {"sid": st.get("Sid"), "uid": uid, "access": access}


def _sid_for(uid: str) -> str:
    tenant, bare = split_uid(uid)
    return s3docs.share_sid(bare, tenant or None)


def _read_shares(s3, short: str) -> list[dict]:
    try:
        text = s3.get_bucket_policy(Bucket=short)["Policy"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "NoSuchBucketPolicy":
            return []
        raise
    return [st for st in s3docs.managed_statements(text)
            if str(st.get("Sid", "")).startswith(SHARE_PREFIX)]


async def _policy(request: Request, owner_uid: str, short: str, mutate) -> None:
    def run(s3):
        return s3docs.update_bucket_policy(s3, short, mutate)

    await request.app.state.s3.call(owner_uid, run)


def _view(row: dict, status: str = "active") -> dict[str, Any]:
    return {"id": row["id"], "recipient": row["recipient_ad"],
            "displayName": row["recipient_name"], "uid": row["recipient_uid"],
            "access": row["access"], "createdAt": row["created_at"], "status": status}


async def _audit(request: Request, actor: str, action: str, full: str, **details) -> None:
    db = request.app.state.db
    try:
        await db.run(db.audit.add, actor, action, "bucket", full, details)
    except Exception:
        log.exception("could not audit %s for %s", action, full)


def _policy_error(e: Exception, what: str) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, s3docs.S3DocError):
        return HTTPException(409, f"The bucket policy could not be changed safely: {e}")
    log.warning("sharing: could not %s: %s", what, type(e).__name__)
    return HTTPException(502, f"Could not {what}. Nothing was changed; try again.")


async def _sweep(request: Request, acct: RgwAccount, actor: str, full: str) -> None:
    """Live rows of this bucket name that belong to another owner are stale (the bucket was
    deleted outside Bucketeer and recreated, or its owner changed): revoke them with an
    audit entry. Never show, reuse or repair them. Never raises."""
    db = request.app.state.db
    try:
        for r in await db.run(db.shares.list_live, full):
            if not same_uid(r["owner_uid"], acct.uid) and await db.run(db.shares.revoke, r["id"]):
                await _audit(request, actor, "bucket.unshare", full, share=r["id"],
                             recipient=r["recipient_ad"], uid=r["recipient_uid"],
                             reason="owner_changed", previousOwner=r["owner_uid"])
    except Exception:
        log.exception("could not sweep stale shares of %s", full)


def _mine(acct: RgwAccount, row: dict | None, full: str) -> bool:
    return bool(row) and row["bucket_name"] == full and same_uid(row["owner_uid"], acct.uid)


async def _owned(request: Request, acct: RgwAccount, name: str,
                 actor: str = "system") -> tuple[str, str]:
    short = _bucket_name(name)
    full = _full(acct.uid, short)
    await _own_bucket(request.app.state.croit, acct.uid, full)
    await _sweep(request, acct, actor, full)
    return short, full


# -- owner side -------------------------------------------------------------------------


@router.get("/buckets/{name}/share-lookup")
async def share_lookup(name: str, q: str, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    await _owned(request, acct, name, acct.username)
    person = await _find_person(request, acct.username, q)
    return {"username": person.username, "displayName": person.display_name,
            "isYou": person.username.lower() == acct.username.lower()}


def _canonical(short: str, row: dict) -> dict | None:
    """The statement a share row should have in the policy (None if it cannot be built)."""
    tenant, bare = split_uid(row["recipient_uid"])
    try:
        return s3docs.share_statement(short, bare, tenant or None, row["access"])
    except s3docs.S3DocError:
        return None


@router.get("/buckets/{name}/shares")
async def list_shares(name: str, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    short, full = await _owned(request, acct, name, acct.username)
    db = request.app.state.db
    rows = [r for r in await db.run(db.shares.list_live, full)
            if same_uid(r["owner_uid"], acct.uid)]  # defense in depth beside _sweep
    checked = True
    try:
        statements = await request.app.state.s3.call(acct.uid, _read_shares, short)
    except Exception as e:
        log.warning("sharing: could not read the policy of %s: %s", full, type(e).__name__)
        statements, checked = [], False
    by_sid = {s.get("Sid"): s for s in statements}
    out, seen = [], set()
    for row in rows:
        try:
            sid = _sid_for(row["recipient_uid"])
        except s3docs.S3DocError:
            sid = None
        st = by_sid.get(sid)
        seen.add(sid)
        if not checked:
            status = "unchecked"
        elif st is None or st != _canonical(short, row):
            status = "out_of_sync"
        else:
            status = "active"
        out.append(_view(row, status))
    for sid, st in by_sid.items():
        if sid in seen:
            continue
        v = _statement_view(st)
        out.append({"id": sid, "recipient": v["uid"], "displayName": "", "uid": v["uid"],
                    "access": v["access"], "createdAt": None, "status": "unknown"})
    return {"shares": out, "checked": checked}


@router.post("/buckets/{name}/shares")
async def create_share(name: str, body: ShareBody, request: Request, response: Response,
                       acct: Account, user: User):
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    short, full = await _owned(request, acct, name, user.username)
    person, rcpt, uid = await resolve_recipient(request, acct, body.login)
    tenant, bare = split_uid(uid)
    try:
        statement = s3docs.share_statement(short, bare, tenant or None, body.access)
    except s3docs.S3DocError:
        raise HTTPException(422, "That person's account name cannot be used in a bucket "
                                 "policy. Contact your administrator.") from None

    async with _bucket_lock(request, full):
        existing = await db.run(db.shares.live_for, full, uid)
        if existing and not same_uid(existing["owner_uid"], acct.uid):
            # another owner's stale row (a swallowed sweep): retire it, never reuse it
            await db.run(db.shares.revoke, existing["id"])
            await _audit(request, user.username, "bucket.unshare", full, share=existing["id"],
                         recipient=existing["recipient_ad"], uid=existing["recipient_uid"],
                         reason="owner_changed", previousOwner=existing["owner_uid"])
            existing = None
        try:
            await _policy(request, acct.uid, short,
                          lambda p: s3docs.upsert_statements(p, [statement]))
        except Exception as e:
            raise _policy_error(e, "update the bucket policy") from None
        warning = None
        if existing:
            before = existing["access"]
            await db.run(db.shares.set_access, existing["id"], body.access)
            row = await db.run(db.shares.get, existing["id"])
            await _audit(request, user.username, "bucket.share_update", full, share=row["id"],
                         recipient=person.username, uid=uid, before=before, after=body.access)
        else:
            try:
                row = await db.run(db.shares.create, full, acct.uid, acct.display_name,
                                   person.username, uid, person.display_name, body.access,
                                   user.username)
            except Exception:
                log.exception("could not record the share of %s with %s", full, uid)
                row = None
                warning = ("The bucket was shared, but Bucketeer could not record it. "
                           "It shows as an unknown share; revoke it from the list if needed.")
            await _audit(request, user.username, "bucket.share", full,
                         share=row["id"] if row else None, recipient=person.username, uid=uid,
                         access=body.access, accountCreated=rcpt.created)
    if row is None:
        row = {"id": None, "recipient_ad": person.username, "recipient_name":
               person.display_name, "recipient_uid": uid, "access": body.access,
               "created_at": None}
    out = _view(row)
    out["accountCreated"] = bool(rcpt.created)
    if existing:
        out["updated"] = True
        response.status_code = 200
    else:
        response.status_code = 201
    if warning:
        out["warning"] = warning
    return out


@router.put("/buckets/{name}/shares/{share_id}")
async def update_share(name: str, share_id: int, body: AccessBody, request: Request,
                       response: Response, acct: Account, user: User):
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    short, full = await _owned(request, acct, name, user.username)
    async with _bucket_lock(request, full):
        row = await db.run(db.shares.get, share_id)
        if not _mine(acct, row, full) or row["revoked_at"]:
            raise HTTPException(404, "Share not found.")
        tenant, bare = split_uid(row["recipient_uid"])
        try:
            statement = s3docs.share_statement(short, bare, tenant or None, body.access)
            await _policy(request, acct.uid, short,
                          lambda p: s3docs.upsert_statements(p, [statement]))
        except Exception as e:
            raise _policy_error(e, "update the bucket policy") from None
        before = row["access"]
        await db.run(db.shares.set_access, share_id, body.access)
        await _audit(request, user.username, "bucket.share_update", full, share=share_id,
                     recipient=row["recipient_ad"], uid=row["recipient_uid"], before=before,
                     after=body.access)
    return _view(await db.run(db.shares.get, share_id))


@router.delete("/buckets/{name}/shares/{share_id}")
async def delete_share(name: str, share_id: str, request: Request, response: Response,
                       acct: Account, user: User):
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    short, full = await _owned(request, acct, name, user.username)
    async with _bucket_lock(request, full):
        row = None
        if share_id.isdigit():
            row = await db.run(db.shares.get, int(share_id))
            if not _mine(acct, row, full):
                raise HTTPException(404, "Share not found.")
            if row["revoked_at"]:  # an old id: never touch what is in force now
                return {"id": row["id"], "revoked": True}
            sid = _sid_for(row["recipient_uid"])
            who = {"recipient": row["recipient_ad"], "uid": row["recipient_uid"]}
        elif share_id.startswith(SHARE_PREFIX):
            sid = share_id
            try:
                found = await request.app.state.s3.call(acct.uid, _read_shares, short)
            except Exception as e:
                raise _policy_error(e, "read the bucket policy") from None
            st = next((s for s in found if s.get("Sid") == sid), None)
            for r in await db.run(db.shares.list_live, full):  # a live row for this statement
                try:
                    match = _sid_for(r["recipient_uid"]) == sid
                except s3docs.S3DocError:
                    match = False
                if match and same_uid(r["owner_uid"], acct.uid):
                    row = r
                    break
            if row is not None:
                who = {"recipient": row["recipient_ad"], "uid": row["recipient_uid"]}
            elif st is None:
                return {"id": share_id, "revoked": True}
            else:
                who = {"uid": _statement_view(st)["uid"]}
        else:
            raise HTTPException(404, "Share not found.")
        try:
            await _policy(request, acct.uid, short, lambda p: s3docs.remove_statements(p, sid))
        except Exception as e:
            raise _policy_error(e, "update the bucket policy") from None
        changed = True
        if row is not None:
            changed = await db.run(db.shares.revoke, row["id"])
        if changed:
            await _audit(request, user.username, "bucket.unshare", full,
                         share=row["id"] if row else None, **who)
    return {"id": share_id if row is None else row["id"], "revoked": True}


async def revoke_for_deleted_bucket(request: Request, actor: str, full: str,
                                    owner_uid: str | None = None) -> int:
    """After a bucket was deleted through Bucketeer: its statements went with it; mark the
    shares revoked. Never raises; returns how many."""
    db = request.app.state.db
    try:
        rows = await db.run(db.shares.revoke_bucket, full, owner_uid)
        for r in rows:
            await _audit(request, actor, "bucket.unshare", full, share=r["id"],
                         recipient=r["recipient_ad"], uid=r["recipient_uid"],
                         reason="bucket_deleted")
        return len(rows)
    except Exception:
        log.exception("could not revoke the shares of deleted bucket %s", full)
        return 0


# -- recipient side ---------------------------------------------------------------------


@router.get("/shared-with-me")
async def shared_with_me(request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    mine_tenant = split_uid(acct.uid)[0]
    rows = await db.run(db.shares.list_live, None, acct.uid)
    # Drop shares whose bucket its owner no longer has (deleted outside Bucketeer). If croit
    # cannot answer for an owner, that owner's rows stay as they are.
    gone: set[int] = set()
    for owner in {r["owner_uid"] for r in rows}:
        try:
            listed = await request.app.state.croit.list_user_buckets(owner)
        except CroitClientError:
            continue
        if not isinstance(listed, list):
            # Not the documented array: don't read it as "owns nothing" and revoke.
            continue
        owned = {b.get("bucket") for b in listed
                 if isinstance(b, dict) and b.get("owner") == owner}
        for r in rows:
            if r["owner_uid"] == owner and r["bucket_name"] not in owned:
                if await db.run(db.shares.revoke, r["id"]):
                    await _audit(request, "system", "bucket.unshare", r["bucket_name"],
                                 share=r["id"], recipient=r["recipient_ad"],
                                 uid=r["recipient_uid"], reason="bucket_gone")
                gone.add(r["id"])
    out = []
    for r in rows:
        if r["id"] in gone:
            continue
        full = r["bucket_name"]
        tenant, _, short = full.rpartition(":")
        out.append({
            "bucket": short or full,
            # RGW addresses another tenant's bucket as tenant:bucket
            "address": short if (not tenant or tenant == mine_tenant) else full,
            "owner": r["owner_name"] or r["owner_uid"],
            "access": r["access"],
            "sharedAt": r["created_at"],
        })
    out.sort(key=lambda s: (s["bucket"].lower(), s["owner"].lower()))
    return {"shares": out}

