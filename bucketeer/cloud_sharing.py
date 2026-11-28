"""Sharing a cloud (AWS) bucket with a person, read-only or read-write (cloud sharing).

Works like on-premises sharing (`sharing.py`, Q8) with the cloud's identities:
- **Recipients** are exactly the people who could log in: the same exact AD lookup
  (`sharing.resolve_recipient`: per-owner rate limit shared with on-premises sharing, uniform
  404, refuses yourself, suspended and unusable accounts, bucket-key users; the recipient's S3
  account is made on the spot as their first login would). The share dialog's type-ahead is
  the same `GET /api/people/search`. The recipient then gets a cloud account on the spot,
  `cloud_users.ensure_account` for their AD name (audited as the owner's action, `via: share`;
  decision 17: an IAM user with that name that Bucketeer did not make is refused with a clear
  409). No access key is made for them: they make one with "Set up this computer" in Cloud.
- **Access** is one statement per recipient in the bucket's policy, written by Bucketeer's
  AWS admin user (`CloudAdmin.change_statements`: locked read-modify-write, only `bucketeer-*`
  Sids, retried while IAM propagates a new user): Sid `cloud.share_sid(iam_user)`
  (`bucketeer-share-i-<IAM user>`), Principal the recipient's IAM user ARN, actions
  `cloud.share_actions(access)` (read: GetBucketLocation, ListBucket, ListBucketVersions,
  GetObject, GetObjectVersion; read-write adds PutObject, DeleteObject, DeleteObjectVersion,
  AbortMultipartUpload, ListMultipartUploadParts, ListBucketMultipartUploads, RestoreObject),
  resources the bucket and `bucket/*`. All inside `BucketeerUserBoundary` (tools/aws-setup).
  The statement is the source of truth; the `shares` row (`location = 'cloud'`, the full AWS
  name, `recipient_iam`) gives names for the lists.
- **Not offered for cloud buckets:** share links and bucket keys (on premises only).

Routes (`/api/cloud/...`, `require_active_rgw_user`, cloud available else 503, CSRF on
mutations, `no-store`; `{name}` is the short name, own cloud bucket only, else 404):
- `GET  /cloud/buckets/{name}/share-lookup?q=` -> `{username, displayName, isYou}`.
- `GET  /cloud/buckets/{name}/shares` -> `{shares: [{id, recipient, displayName, uid,
  iamUser, access, createdAt|null, status}], checked}`; `status` `active`, `out_of_sync`
  (statement missing or not the canonical one: repair with PUT), `unknown` (a
  `bucketeer-share-*` statement without a row; `id` is its Sid, revoke it), `unchecked` (the
  policy could not be read, `checked: false`).
- `POST /cloud/buckets/{name}/shares {login, access}` -> 201 the share view (`accountCreated`:
  the recipient's cloud account was made now; `emailed`), or 200 `updated: true` for a person
  who has a live share already (access changed, statement repaired).
- `PUT  /cloud/buckets/{name}/shares/{id} {access}` (also repairs out_of_sync; 409 when the
  recipient's IAM user is gone: share again to set it up anew).
- `DELETE /cloud/buckets/{name}/shares/{id}`: the statement first, then the row is revoked
  (idempotent; an unknown statement is addressed by its Sid).
- `GET /cloud/shared-with-me` -> `{shares: [{bucket (full AWS name), owner, access, sharedAt}],
  profile (CLOUD_PROFILE_NAME), region, location}`: live cloud shares whose recipient is the
  session's AD name, only while the person's effective location is cloud (else `[]`); shares
  whose bucket Bucketeer no longer has are left out.
New shares and access changes are refused (409) while the bucket's delete job runs. A deleted
cloud bucket's shares are marked revoked (`revoke_for_deleted_bucket`, from `cloud_buckets`
and `cloud_delete`). Invitation email: `Mailer.send_share_invite(..., location="cloud")`,
same on/off rules as on premises, never a key or a URL that grants access.
Audit (target_type `bucket`, the full AWS name): `cloud.bucket.share`, `share_update`,
`unshare` (details recipient AD name, `iamUser`, access); refusals are logged, not audited.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from . import cloud as cloudmod
from . import location
from .auth import SessionUser, require_user
from .bucket_keys import _bucket_lock
from .cloud import CloudAdmin, CloudError, CloudUnavailable
from .cloud_buckets import _own_row
from .cloud_users import (
    ACCESS_DENIED,
    CloudRefused,
    cloud_http_error,
    ensure_account,
    require_cloud,
)
from .db import Database
from .rgw_user import RgwAccount, require_active_rgw_user, same_uid
from .sharing import AccessBody, ShareBody, _find_person, _log_refusal, resolve_recipient

log = logging.getLogger("bucketeer.cloud_sharing")

SHARE_PREFIX = cloudmod.SHARE_SID_PREFIX
MSG_NOT_OURS = ("That person can't get a cloud account automatically: an AWS user with their "
                "name already exists that Bucketeer did not make. Ask an administrator.")
MSG_BAD_NAME = "That person's user name can't be used for cloud storage. Ask an administrator."
MSG_GONE = ("That person's cloud account no longer exists. Share the bucket with them again to "
            "set it up anew.")
MSG_DELETING = "This bucket is being deleted, so its shares can't be changed."

router = APIRouter(prefix="/cloud", tags=["cloud"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]
Cloud = Annotated[CloudAdmin, Depends(require_cloud)]


# -- helpers ---------------------------------------------------------------------------


async def _audit(app, actor: str, action: str, full: str, **details) -> None:
    db: Database = app.state.db
    log.info("%s %s by %s: %s", action, full, actor,
             " ".join(f"{k}={v}" for k, v in details.items() if v is not None))
    try:
        await db.run(db.audit.add, actor, action, "bucket", full, details)
    except Exception:
        log.exception("could not audit %s for %s", action, full)


def _policy_error(e: Exception, what: str) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, CloudUnavailable):
        return HTTPException(503, "AWS could not be reached. Try again shortly.")
    if isinstance(e, CloudError):
        if e.code == "AccessDenied":
            return HTTPException(502, ACCESS_DENIED)
        if e.code == "MalformedPolicy":
            # e.g. the recipient's new IAM user is still not visible after the retries
            return HTTPException(502, "AWS did not accept the bucket policy yet. Try again in "
                                      "a minute.")
        return HTTPException(502, f"Could not {what} ({e.code}). Try again later or ask an "
                                  "administrator.")
    log.warning("cloud sharing: could not %s: %s", what, type(e).__name__)
    return HTTPException(502, f"Could not {what}. Try again.")


def _view(row: dict, status: str = "active") -> dict[str, Any]:
    return {"id": row["id"], "recipient": row["recipient_ad"],
            "displayName": row["recipient_name"], "uid": row["recipient_uid"],
            "iamUser": row.get("recipient_iam"), "access": row["access"],
            "createdAt": row["created_at"], "status": status}


def _mine(acct: RgwAccount, row: dict | None, full: str) -> bool:
    return (bool(row) and row.get("location") == "cloud" and row["bucket_name"] == full
            and same_uid(row["owner_uid"], acct.uid))


async def _sweep(app, acct: RgwAccount, actor: str, full: str) -> None:
    """Live cloud rows of this bucket that belong to another owner are stale (the bucket was
    deleted outside Bucketeer and its name used again): revoke them. Never raises."""
    db: Database = app.state.db
    try:
        for r in await db.run(db.shares.list_live, full, None, "cloud"):
            if not same_uid(r["owner_uid"], acct.uid) and await db.run(db.shares.revoke, r["id"]):
                await _audit(app, actor, "cloud.bucket.unshare", full, share=r["id"],
                             recipient=r["recipient_ad"], iamUser=r["recipient_iam"],
                             reason="owner_changed", previousOwner=r["owner_uid"])
    except Exception:
        log.exception("could not sweep stale shares of %s", full)


async def _owned(request: Request, cloud: CloudAdmin, acct: RgwAccount, name: str,
                 actor: str) -> dict:
    row = await _own_row(request.app, cloud, acct, name)
    await _sweep(request.app, acct, actor, row["name"])
    return row


async def _not_deleting(app, full: str) -> None:
    db: Database = app.state.db
    job = await db.run(db.delete_jobs.latest_for, full)
    if job and job["state"] in ("queued", "running"):
        raise HTTPException(409, MSG_DELETING)


def _share_statements(statements: list[dict]) -> dict[str, dict]:
    return {str(s.get("Sid")): s for s in statements
            if str(s.get("Sid") or "").startswith(SHARE_PREFIX)}


# -- owner side -------------------------------------------------------------------------


@router.get("/buckets/{name}/share-lookup")
async def share_lookup(name: str, q: str, request: Request, response: Response, acct: Account,
                       cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    await _owned(request, cloud, acct, name, acct.username)
    person = await _find_person(request, acct.username, q)
    return {"username": person.username, "displayName": person.display_name,
            "isYou": person.username.lower() == acct.username.lower()}


@router.get("/buckets/{name}/shares")
async def list_shares(name: str, request: Request, response: Response, acct: Account,
                      cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    db: Database = request.app.state.db
    full = (await _owned(request, cloud, acct, name, acct.username))["name"]
    rows = [r for r in await db.run(db.shares.list_live, full, None, "cloud")
            if same_uid(r["owner_uid"], acct.uid)]  # defense in depth beside _sweep
    checked = True
    try:
        by_sid = _share_statements(await cloud.bucket_statements(full))
        wanted = {r["id"]: cloudmod.share_statement(
            full, await cloud.user_arn(r["recipient_iam"] or ""), r["recipient_iam"] or "",
            r["access"]) for r in rows}
    except CloudError as e:
        log.warning("cloud sharing: could not read the policy of %s: %s", full, e.code)
        by_sid, wanted, checked = {}, {}, False
    out, seen = [], set()
    for r in rows:
        sid = cloudmod.share_sid(r["recipient_iam"] or "")
        seen.add(sid)
        if not checked:
            status = "unchecked"
        elif cloudmod.same_statement(by_sid.get(sid), wanted[r["id"]]):
            status = "active"
        else:
            status = "out_of_sync"
        out.append(_view(r, status))
    for sid, st in by_sid.items():
        if sid in seen:
            continue
        iam = cloudmod.statement_user(st) or ""
        out.append({"id": sid, "recipient": iam, "displayName": "", "uid": "",
                    "iamUser": iam or None, "access": cloudmod.statement_access(st),
                    "createdAt": None, "status": "unknown"})
    return {"shares": out, "checked": checked}


@router.post("/buckets/{name}/shares")
async def create_share(name: str, body: ShareBody, request: Request, response: Response,
                       acct: Account, user: User, cloud: Cloud, background: BackgroundTasks):
    with _log_refusal("cloud.bucket.share", name, user.username, recipient=body.login,
                      access=body.access):
        return await _create_share(name, body, request, response, acct, user, cloud,
                                   background)


async def _recipient_account(app, person, actor: str) -> tuple[dict, bool]:
    """The recipient's cloud_accounts row (made now if needed) and whether it was made now."""
    db: Database = app.state.db
    had = await db.run(db.cloud_accounts.get, person.username)
    try:
        row = await ensure_account(app, person.username, actor=actor, via="share")
    except CloudRefused as e:
        log.info("cloud sharing: no cloud account for %s (%s)", person.username, e.reason)
        raise HTTPException(409, MSG_NOT_OURS if e.reason == "not_ours" else MSG_BAD_NAME) \
            from None
    except CloudError as e:
        raise cloud_http_error(e) from None
    return row, had is None or had["iam_user"] != row["iam_user"]


async def _create_share(name: str, body: ShareBody, request: Request, response: Response,
                        acct: RgwAccount, user: SessionUser, cloud: CloudAdmin,
                        background: BackgroundTasks):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    full = (await _owned(request, cloud, acct, name, user.username))["name"]
    await _not_deleting(app, full)
    person, _rcpt, uid = await resolve_recipient(request, acct, body.login)
    account, created = await _recipient_account(app, person, user.username)
    iam = account["iam_user"]
    try:
        statement = cloudmod.share_statement(full, await cloud.user_arn(iam), iam, body.access)
    except CloudError as e:
        raise cloud_http_error(e) from None

    async with _bucket_lock(request, full):
        existing = await db.run(db.shares.live_cloud_for, full, iam)
        if existing and not same_uid(existing["owner_uid"], acct.uid):
            await db.run(db.shares.revoke, existing["id"])  # another owner's stale row
            await _audit(app, user.username, "cloud.bucket.unshare", full,
                         share=existing["id"], recipient=existing["recipient_ad"],
                         iamUser=iam, reason="owner_changed",
                         previousOwner=existing["owner_uid"])
            existing = None
        try:
            await cloud.change_statements(full, [statement])
        except Exception as e:
            raise _policy_error(e, "update the bucket policy") from None
        warning = None
        if existing:
            before = existing["access"]
            await db.run(db.shares.set_access, existing["id"], body.access)
            row = await db.run(db.shares.get, existing["id"])
            await _audit(app, user.username, "cloud.bucket.share_update", full,
                         share=row["id"], recipient=person.username, iamUser=iam,
                         before=before, after=body.access)
        else:
            try:
                row = await db.run(lambda: db.shares.create(
                    full, acct.uid, acct.display_name, person.username, uid,
                    person.display_name, body.access, user.username, location="cloud",
                    recipient_iam=iam))
            except Exception:
                log.exception("could not record the share of %s with %s", full, iam)
                row = None
                warning = ("The bucket was shared, but Bucketeer could not record it. "
                           "It shows as an unknown share; revoke it from the list if needed.")
            await _audit(app, user.username, "cloud.bucket.share", full,
                         share=row["id"] if row else None, recipient=person.username,
                         iamUser=iam, access=body.access, accountCreated=created)
    if row is None:
        row = {"id": None, "recipient_ad": person.username, "recipient_name":
               person.display_name, "recipient_uid": uid, "recipient_iam": iam,
               "access": body.access, "created_at": None}
    out = _view(row)
    out["accountCreated"] = created
    if existing:
        out["updated"] = True
        response.status_code = 200
    else:
        mailer = app.state.mailer
        if mailer.enabled:
            background.add_task(mailer.send_share_invite, person.email,
                                acct.display_name or user.display_name, full, body.access,
                                location="cloud")
            out["emailed"] = "queued"
        else:
            out["emailed"] = None
        response.status_code = 201
    if warning:
        out["warning"] = warning
    return out


@router.put("/buckets/{name}/shares/{share_id}")
async def update_share(name: str, share_id: int, body: AccessBody, request: Request,
                       response: Response, acct: Account, user: User, cloud: Cloud):
    with _log_refusal("cloud.bucket.share_update", name, user.username, share=share_id,
                      access=body.access):
        return await _update_share(name, share_id, body, request, response, acct, user, cloud)


async def _update_share(name: str, share_id: int, body: AccessBody, request: Request,
                        response: Response, acct: RgwAccount, user: SessionUser,
                        cloud: CloudAdmin):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    full = (await _owned(request, cloud, acct, name, user.username))["name"]
    await _not_deleting(app, full)
    async with _bucket_lock(request, full):
        row = await db.run(db.shares.get, share_id)
        if not _mine(acct, row, full) or row["revoked_at"]:
            raise HTTPException(404, "Share not found.")
        iam = row["recipient_iam"] or ""
        try:
            if await cloud.get_user(iam) is None:
                raise HTTPException(409, MSG_GONE)
            statement = cloudmod.share_statement(full, await cloud.user_arn(iam), iam,
                                                 body.access)
            await cloud.change_statements(full, [statement])
        except Exception as e:
            raise _policy_error(e, "update the bucket policy") from None
        before = row["access"]
        await db.run(db.shares.set_access, share_id, body.access)
        await _audit(app, user.username, "cloud.bucket.share_update", full, share=share_id,
                     recipient=row["recipient_ad"], iamUser=iam, before=before,
                     after=body.access)
    return _view(await db.run(db.shares.get, share_id))


@router.delete("/buckets/{name}/shares/{share_id}")
async def delete_share(name: str, share_id: str, request: Request, response: Response,
                       acct: Account, user: User, cloud: Cloud):
    with _log_refusal("cloud.bucket.unshare", name, user.username, share=share_id):
        return await _delete_share(name, share_id, request, response, acct, user, cloud)


async def _delete_share(name: str, share_id: str, request: Request, response: Response,
                        acct: RgwAccount, user: SessionUser, cloud: CloudAdmin):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    full = (await _owned(request, cloud, acct, name, user.username))["name"]
    async with _bucket_lock(request, full):
        row = None
        if share_id.isdigit():
            row = await db.run(db.shares.get, int(share_id))
            if not _mine(acct, row, full):
                raise HTTPException(404, "Share not found.")
            if row["revoked_at"]:  # an old id: never touch what is in force now
                return {"id": row["id"], "revoked": True}
            sid = cloudmod.share_sid(row["recipient_iam"] or "")
            who = {"recipient": row["recipient_ad"], "iamUser": row["recipient_iam"]}
        elif share_id.startswith(SHARE_PREFIX):
            sid = share_id
            try:
                st = _share_statements(await cloud.bucket_statements(full)).get(sid)
            except Exception as e:
                raise _policy_error(e, "read the bucket policy") from None
            for r in await db.run(db.shares.list_live, full, None, "cloud"):
                if (cloudmod.share_sid(r["recipient_iam"] or "") == sid
                        and same_uid(r["owner_uid"], acct.uid)):
                    row = r
                    break
            if row is not None:
                who = {"recipient": row["recipient_ad"], "iamUser": row["recipient_iam"]}
            elif st is None:
                return {"id": share_id, "revoked": True}
            else:
                who = {"iamUser": cloudmod.statement_user(st)}
        else:
            raise HTTPException(404, "Share not found.")
        try:
            await cloud.change_statements(full, remove={sid})
        except Exception as e:
            raise _policy_error(e, "update the bucket policy") from None
        changed = True
        if row is not None:
            changed = await db.run(db.shares.revoke, row["id"])
        if changed:
            await _audit(app, user.username, "cloud.bucket.unshare", full,
                         share=row["id"] if row else None, **who)
    return {"id": share_id if row is None else row["id"], "revoked": True}


async def revoke_for_deleted_bucket(app, actor: str, full: str,
                                    owner_uid: str | None = None) -> int:
    """After a cloud bucket was deleted through Bucketeer: its policy went with it; mark the
    shares revoked. Never raises; returns how many."""
    db: Database = app.state.db
    try:
        rows = await db.run(db.shares.revoke_bucket, full, owner_uid, "cloud")
        for r in rows:
            await _audit(app, actor, "cloud.bucket.unshare", full, share=r["id"],
                         recipient=r["recipient_ad"], iamUser=r["recipient_iam"],
                         reason="bucket_deleted")
        return len(rows)
    except Exception:
        log.exception("could not revoke the shares of deleted cloud bucket %s", full)
        return 0


# -- recipient side ---------------------------------------------------------------------


@router.get("/shared-with-me")
async def shared_with_me(request: Request, response: Response, acct: Account, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    app, db = request.app, request.app.state.db
    settings = app.state.settings
    profile = await db.run(db.profiles.get, acct.username)
    loc = location.view(app, profile)["location"]
    out: dict[str, Any] = {"shares": [], "profile": settings.cloud_profile_name,
                           "region": cloud.region, "location": loc}
    if loc != "cloud":
        return out
    for r in await db.run(db.shares.list_cloud_for_recipient, acct.username):
        bucket = await db.run(db.buckets.get, r["bucket_name"], "cloud")
        if bucket is None or not same_uid(bucket["owner_uid"], r["owner_uid"]):
            continue  # Bucketeer no longer has that bucket (a GET never changes state)
        out["shares"].append({"bucket": r["bucket_name"],
                              "owner": r["owner_name"] or r["owner_uid"],
                              "access": r["access"], "sharedAt": r["created_at"]})
    out["shares"].sort(key=lambda s: (s["bucket"], s["owner"].lower()))
    return out
