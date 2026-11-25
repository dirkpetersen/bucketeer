"""Cloud (AWS) users and keys, P6-9 (PLAN.md section 4, decision 17).

**IAM user per person.** `<CLOUD_USER_PREFIX><lowercased sAMAccountName>` (`CloudAdmin.user_name`),
created on first use of cloud (the first cloud key, a cloud setup link, later the first cloud
bucket) by `ensure_account`, under path `/bucketeer/` WITH the permissions boundary
`BucketeerUserBoundary` and tagged `bucketeer-owner=<ad name>`. The name follows the RGW uid
rules (`[a-z0-9][a-z0-9._@-]{0,63}`, at most 64 characters with the prefix); the system names
(`cloud.reserved_usernames()`, RGW's `anonymous`, and Bucketeer's own IAM user) are refused.
An IAM user with that name that Bucketeer did not make (another path, no boundary, or no
`bucketeer-owner` tag naming this person) is NEVER adopted: 409 "ask an administrator"
(audit `cloud.user.refused`), until an admin removes it. One that IS ours but unrecorded
(the database lost the row) is recorded again (`source` `recovered`). The link lives in
SQLite `cloud_accounts` (migration v17). Audit `cloud.user.create` (target_type `cloud_user`).

**Inline policy** `bucketeer-buckets` = only `s3:ListAllMyBuckets` and `s3:GetBucketLocation`
(written when the user is made). Access to a bucket comes from that bucket's own policy
(`CloudAdmin.grant_bucket_owner(bucket, await cloud.user_arn(iam_user))` on create,
`revoke_bucket_owner` on delete, P6-10), never from a name wildcard: hyphenated AD names
would overlap (`jdoe`'s `<prefix>jdoe-*` would match `jdoe-x`'s buckets). See `cloud.py`.

**Keys** (`/api/cloud/credentials`, `require_active_rgw_user` + cloud available (503), CSRF,
no-store, a per-person lock around changes):
- `GET` -> `{profile, region, iamUser|null, keys: [{accessKey, status, createdAt}], maxKeys}`;
  never a secret; never creates anything.
- `POST /keys` -> 201 `{accessKey, secretKey, status, createdAt, oneTime: true}` (creates the
  IAM user first if needed; at most 2 keys, IAM's hard limit, else 409).
- `POST /keys/{accessKey}/rotate` -> `{accessKey, secretKey, ..., oldKeyRemoved}`: the new key
  first, then the old one is deleted (a failed delete answers `oldKeyRemoved: false`). Needs a
  free slot: with 2 keys it is 409 (delete the unused one first).
- `DELETE /keys/{accessKey}` -> `{accessKey}`; the last key is refused (409).
- **No reveal.** AWS hands out a secret only in the CreateAccessKey answer, and Bucketeer
  stores none, so the create/rotate answer is the only time a cloud secret is shown (an
  owner-approved exception to the reveal rule). A setup link made in Cloud (the user's
  location decides) makes a fresh key when the script is fetched (`setup_key`, deleting the
  oldest key, an inactive one first, when there are already 2) and embeds it.
Audit `cloud.key.create|rotate|delete` (target_type `access_key`, details `iamUser`, `via`),
never a secret. Refusals are logged.

**Suspension** (`suspend_keys`, `wake_keys`, `relabel`): when an admin or the offboarding job
suspends a person, their Active cloud keys are deactivated (UpdateAccessKey Inactive) and
recorded in `cloud_key_suspensions` with `suspended_by` `owner` (admin) or `offboarding` (the
job), the row written BEFORE the call (an unknown outcome stays ours to undo). Waking
reactivates only the recorded keys of the allowed kinds. Failures are returned (never raised),
like bucket-key failures, so the caller reports them.
"""

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .cloud import (
    MAX_ACCESS_KEYS,
    CloudAdmin,
    CloudError,
    CloudUnavailable,
    reserved_usernames,
)
from .db import Database
from .profile import require_default_budget
from .rgw_user import _UID, RESERVED_UIDS, RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.cloud_users")

MAX_IAM_NAME = 64
ASK_ADMIN = ("Your cloud account can't be set up automatically: an AWS user with your name "
             "already exists that Bucketeer did not make. Ask an administrator.")


class CloudRefused(Exception):
    """Cloud is refused for this person (status 409); `message` is safe to show."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


ACCESS_DENIED = ("AWS refused the request (AccessDenied): Bucketeer's AWS permissions don't "
                 "allow it, so trying again won't help. Ask an administrator.")


def cloud_http_error(e: Exception) -> HTTPException:
    if isinstance(e, CloudRefused):
        return HTTPException(409, e.message)
    if isinstance(e, CloudUnavailable):
        return HTTPException(503, "AWS could not be reached. Try again shortly.")
    if isinstance(e, CloudError):
        if e.code == "LimitExceeded":
            return HTTPException(409, f"AWS allows at most {MAX_ACCESS_KEYS} access keys.")
        if e.code == "AccessDenied":  # Bucketeer's own AWS policy: trying again won't help
            return HTTPException(502, ACCESS_DENIED)
        return HTTPException(502, f"AWS refused the request ({e.code}). Try again later or "
                                  "ask an administrator.")
    raise e


def _locks(app) -> dict[str, asyncio.Lock]:
    if not hasattr(app.state, "cloud_locks"):
        app.state.cloud_locks = {}
    return app.state.cloud_locks


def _lock(app, username: str) -> asyncio.Lock:
    return _locks(app).setdefault(username.lower(), asyncio.Lock())


async def _audit(db: Database, actor: str, action: str, kind: str, target: str,
                 details: dict[str, Any]) -> None:
    await db.run(db.audit.add, actor, action, kind, target, details)


# -- accounts ------------------------------------------------------------------------


async def check_name(cloud: CloudAdmin, username: str) -> str:
    """The IAM user name for `username`, or CloudRefused."""
    u = username.lower()
    if not _UID.match(u) or u in reserved_usernames() or u in RESERVED_UIDS:
        raise CloudRefused("invalid_username", "Your user name can't be used for cloud "
                           "storage. Ask an administrator.")
    name = cloud.user_name(u)
    if len(name) > MAX_IAM_NAME:
        raise CloudRefused("name_too_long", "Your user name is too long for an AWS user. "
                           "Ask an administrator.")
    own = await cloud.own_user_name()
    if own and own.lower() == name.lower():
        raise CloudRefused("reserved", "Your user name can't be used for cloud storage. "
                           "Ask an administrator.")
    return name


async def ensure_account(app, username: str) -> dict[str, Any]:
    """The person's `cloud_accounts` row, creating the IAM user (and its policy) on first use.
    Raises CloudRefused (not ours, bad name) or CloudError (AWS trouble)."""
    db: Database = app.state.db
    cloud: CloudAdmin = app.state.cloud
    u = username.lower()
    async with _lock(app, u):
        name = await check_name(cloud, u)
        row = await db.run(db.cloud_accounts.get, u)
        if row and row["iam_user"] != name:
            row = None  # the user prefix changed; judge the new name afresh
        user = await cloud.get_user(name)
        created = False
        if user is None:
            try:
                await cloud.create_person_user(name, u)
                created = True
            except CloudError as e:
                if e.code != "EntityAlreadyExists":
                    raise
                user = await cloud.get_user(name)  # made meanwhile: judge it below
                if user is None:
                    # the name exists outside /bucketeer/ (we may not even read it): not ours
                    log.warning("cloud: refused %s: IAM user %s exists outside Bucketeer's "
                                "path", u, name)
                    await _audit(db, u, "cloud.user.refused", "cloud_user", name,
                                 {"adUsername": u, "reason": "not_ours"})
                    raise CloudRefused("not_ours", ASK_ADMIN) from None
        if created:
            row = await db.run(db.cloud_accounts.record, u, name, "created")
            log.info("cloud: created IAM user %s for %s", name, u)
            await _audit(db, u, "cloud.user.create", "cloud_user", name,
                         {"adUsername": u, "source": "created"})
        elif row is None:
            if not await cloud.is_ours(user, u):  # type: ignore[arg-type]
                log.warning("cloud: refused %s: IAM user %s exists and was not made by "
                            "Bucketeer for this person", u, name)
                await _audit(db, u, "cloud.user.refused", "cloud_user", name,
                             {"adUsername": u, "reason": "not_ours"})
                raise CloudRefused("not_ours", ASK_ADMIN)
            row = await db.run(db.cloud_accounts.record, u, name, "recovered")
            log.info("cloud: recorded existing IAM user %s for %s again", name, u)
            await _audit(db, u, "cloud.user.create", "cloud_user", name,
                         {"adUsername": u, "source": "recovered"})
        if not row["policy_synced"]:  # written once; retried until it succeeds
            await cloud.put_user_policy(name)
            await db.run(db.cloud_accounts.set_policy_synced, u, True)
            row = await db.run(db.cloud_accounts.get, u)
        return row  # type: ignore[return-value]


# -- keys ------------------------------------------------------------------------------


async def setup_key(app, username: str) -> dict[str, Any]:
    """A fresh key for the setup script (the secret exists only in its answer). With 2 keys,
    one is deleted first: an inactive one if any, else the oldest."""
    db: Database = app.state.db
    cloud: CloudAdmin = app.state.cloud
    row = await ensure_account(app, username)
    name, u = row["iam_user"], row["ad_username"]
    async with _lock(app, u):
        keys = await cloud.list_access_keys(name)
        if len(keys) >= MAX_ACCESS_KEYS:
            old = sorted(keys, key=lambda k: (k["status"] == "Active", k["createdAt"] or ""))[0]
            await cloud.delete_access_key(name, old["accessKey"])
            await db.run(db.cloud_key_suspensions.remove, old["accessKey"])
            await _audit(db, u, "cloud.key.delete", "access_key", old["accessKey"],
                         {"iamUser": name, "via": "setup", "reason": "make_room"})
        key = await cloud.create_access_key(name)
        await _audit(db, u, "cloud.key.create", "access_key", key["accessKey"],
                     {"iamUser": name, "via": "setup"})
    return key


async def discard_key(app, username: str, access_key: str) -> None:
    """Delete a key made for a setup script that could not be used (best effort)."""
    row = await app.state.db.run(app.state.db.cloud_accounts.get, username)
    if row is None:
        return
    try:
        await app.state.cloud.delete_access_key(row["iam_user"], access_key)
        await _audit(app.state.db, username.lower(), "cloud.key.delete", "access_key",
                     access_key, {"iamUser": row["iam_user"], "via": "setup",
                                  "reason": "unusable"})
    except CloudError as e:
        log.warning("cloud: unusable setup key %s not deleted: %s", access_key, e)


# -- suspension ------------------------------------------------------------------------


def _definite(e: CloudError) -> bool:
    return not isinstance(e, CloudUnavailable) and 400 <= e.status < 500


async def suspend_keys(app, names: list[str], by: str) -> tuple[list[str], list[str]]:
    """Deactivate the Active keys of the cloud accounts of `names` (AD names), recorded with
    `by` ('owner' | 'offboarding'). Returns (done, failed) as `cloud:<id>` entries."""
    db: Database = app.state.db
    rows = await db.run(db.cloud_accounts.for_names, list(names))
    if not rows:
        return [], []
    cloud: CloudAdmin | None = getattr(app.state, "cloud", None)
    if cloud is None:
        log.warning("cloud: not configured; cloud keys of %s were not deactivated",
                    ", ".join(r["iam_user"] for r in rows))
        return [], [f"cloud:{r['iam_user']}" for r in rows]
    done, failed = [], []
    for r in rows:
        name = r["iam_user"]
        try:
            keys = await cloud.list_access_keys(name)
        except CloudError as e:
            if e.code == "NoSuchEntity":
                continue  # the IAM user is gone: no key can work
            log.warning("cloud: listing keys of %s failed: %s", name, e)
            failed.append(f"cloud:{name}")
            continue
        for k in keys:
            if k["status"] != "Active":
                continue
            ak = k["accessKey"]
            # Recorded first: if the answer is lost the key may be inactive anyway, and then
            # it must count as ours so waking the owner reactivates it.
            await db.run(db.cloud_key_suspensions.add, ak, name, r["ad_username"], by)
            try:
                await cloud.set_access_key_status(name, ak, False)
                done.append(f"cloud:{ak}")
            except CloudError as e:
                log.warning("cloud: deactivating %s failed: %s", ak, e)
                if _definite(e):
                    await db.run(db.cloud_key_suspensions.remove, ak)
                if e.code != "NoSuchEntity":
                    failed.append(f"cloud:{ak}")
    return done, failed


async def wake_keys(app, names: list[str], kinds: set[str]) -> tuple[list[str], list[str]]:
    """Reactivate the keys Bucketeer deactivated for `names` whose `suspended_by` is in
    `kinds`. Returns (done, failed)."""
    db: Database = app.state.db
    rows = [s for n in names for s in await db.run(db.cloud_key_suspensions.for_user, n)
            if s["suspended_by"] in kinds]
    if not rows:
        return [], []
    cloud: CloudAdmin | None = getattr(app.state, "cloud", None)
    if cloud is None:
        log.warning("cloud: not configured; cloud keys were not reactivated")
        return [], [f"cloud:{s['access_key']}" for s in rows]
    done, failed = [], []
    for s in rows:
        try:
            await cloud.set_access_key_status(s["iam_user"], s["access_key"], True)
            done.append(f"cloud:{s['access_key']}")
        except CloudError as e:
            if e.code != "NoSuchEntity":  # a deleted key has nothing to wake
                log.warning("cloud: reactivating %s failed: %s", s["access_key"], e)
                failed.append(f"cloud:{s['access_key']}")
                continue
        await db.run(db.cloud_key_suspensions.remove, s["access_key"])
    return done, failed


async def relabel(app, names: list[str], old: str, new: str) -> None:
    db: Database = app.state.db
    for n in names:
        await db.run(db.cloud_key_suspensions.relabel, n, old, new)


async def active_keys(app, names: list[str]) -> int | None:
    """How many cloud keys of `names` are still Active (None = could not tell)."""
    db: Database = app.state.db
    rows = await db.run(db.cloud_accounts.for_names, list(names))
    if not rows:
        return 0
    cloud = getattr(app.state, "cloud", None)
    if cloud is None:
        return None
    total = 0
    for r in rows:
        try:
            total += sum(1 for k in await cloud.list_access_keys(r["iam_user"])
                         if k["status"] == "Active")
        except CloudError as e:
            if e.code != "NoSuchEntity":
                return None
    return total


# -- API -------------------------------------------------------------------------------

router = APIRouter(prefix="/cloud/credentials", tags=["cloud"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]


def require_cloud(request: Request) -> CloudAdmin:
    cloud = getattr(request.app.state, "cloud", None)
    if cloud is None or not cloud.available:
        raise HTTPException(503, "Cloud storage is not available right now.")
    return cloud


Cloud = Annotated[CloudAdmin, Depends(require_cloud)]


def _owned(keys: list[dict], access_key: str) -> dict:
    key = next((k for k in keys if k["accessKey"] == access_key), None)
    if key is None:
        raise HTTPException(404, "Access key not found.")
    return key


async def _account(request: Request, acct: RgwAccount) -> dict:
    try:
        return await ensure_account(request.app, acct.username)
    except (CloudRefused, CloudError) as e:
        log.info("cloud: %s refused for %s: %s", request.url.path, acct.username, e)
        raise cloud_http_error(e) from None


@router.get("")
async def get_cloud_credentials(request: Request, response: Response, acct: Account,
                                cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    s = request.app.state.settings
    db: Database = request.app.state.db
    row = await db.run(db.cloud_accounts.get, acct.username)
    keys: list[dict] = []
    if row:
        try:
            keys = await cloud.list_access_keys(row["iam_user"])
        except CloudError as e:
            if e.code != "NoSuchEntity":
                raise cloud_http_error(e) from None
            row = None  # removed by an admin; the next key request makes a new one
    return {"profile": s.cloud_profile_name, "region": s.cloud_region,
            "iamUser": row["iam_user"] if row else None, "keys": keys,
            "maxKeys": MAX_ACCESS_KEYS}


@router.post("/keys", status_code=201, dependencies=[Depends(require_default_budget)])
async def create_cloud_key(request: Request, response: Response, acct: Account, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    row = await _account(request, acct)
    name, u, db = row["iam_user"], row["ad_username"], request.app.state.db
    async with _lock(request.app, u):
        try:
            keys = await cloud.list_access_keys(name)
            if len(keys) >= MAX_ACCESS_KEYS:
                log.info("cloud: key create refused for %s: limit", u)
                raise HTTPException(409, f"AWS allows at most {MAX_ACCESS_KEYS} access keys. "
                                         "Delete the one you no longer use first.")
            key = await cloud.create_access_key(name)
        except CloudError as e:
            raise cloud_http_error(e) from None
        await _audit(db, acct.username, "cloud.key.create", "access_key", key["accessKey"],
                     {"iamUser": name})
    return {**key, "oneTime": True}


@router.post("/keys/{access_key}/rotate")
async def rotate_cloud_key(access_key: str, request: Request, response: Response,
                           acct: Account, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    row = await _account(request, acct)
    name, u, db = row["iam_user"], row["ad_username"], request.app.state.db
    async with _lock(request.app, u):
        try:
            keys = await cloud.list_access_keys(name)
            _owned(keys, access_key)
            if len(keys) >= MAX_ACCESS_KEYS:
                log.info("cloud: rotate refused for %s: no free slot", u)
                raise HTTPException(409, f"AWS allows only {MAX_ACCESS_KEYS} access keys, so "
                                         "there is no room for the new one. Delete the key "
                                         "you no longer use, then rotate.")
            new = await cloud.create_access_key(name)
        except CloudError as e:
            raise cloud_http_error(e) from None
        try:
            await cloud.delete_access_key(name, access_key)
            removed = True
        except CloudError as e:
            log.warning("cloud: rotate: old key of %s not removed: %s", u, e)
            removed = False
        await _audit(db, acct.username, "cloud.key.rotate", "access_key", access_key,
                     {"iamUser": name, "newKey": new["accessKey"], "old_key_removed": removed})
    out: dict[str, Any] = {**new, "oneTime": True, "oldKeyRemoved": removed}
    if not removed:
        out["message"] = ("The new key was created, but the old key could not be removed. "
                          "Delete the old key once the new one works.")
    return out


@router.delete("/keys/{access_key}")
async def delete_cloud_key(access_key: str, request: Request, response: Response,
                           acct: Account, cloud: Cloud):
    response.headers["Cache-Control"] = "no-store"
    db: Database = request.app.state.db
    row = await db.run(db.cloud_accounts.get, acct.username)
    if row is None:
        raise HTTPException(404, "Access key not found.")
    name, u = row["iam_user"], row["ad_username"]
    async with _lock(request.app, u):
        try:
            keys = await cloud.list_access_keys(name)
            _owned(keys, access_key)
            if len(keys) <= 1:
                log.info("cloud: delete of the last key refused for %s", u)
                raise HTTPException(409, "This is your only cloud key. Rotate it instead of "
                                         "deleting it.")
            await cloud.delete_access_key(name, access_key)
        except CloudError as e:
            raise cloud_http_error(e) from None
        await db.run(db.cloud_key_suspensions.remove, access_key)
        await _audit(db, acct.username, "cloud.key.delete", "access_key", access_key,
                     {"iamUser": name})
    return {"accessKey": access_key}
