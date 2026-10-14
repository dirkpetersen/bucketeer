"""Admin settings API under /api/admin/settings (CSRF via the parent /api router).

Stores and validates bucket naming rules, placement defaults, the default user
quota, AD-to-RGW uid mappings and the notification endpoint allowlist. Nothing
here applies a setting to a feature except uid mappings and the default quota,
which `rgw_user` reads at login; bucket create and notifications call the
helpers in `policy_settings` later. Every change is audited with before/after
values; no response or audit entry carries a secret (endpoints have no
credentials in v1).
"""

import asyncio
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .admin import Admin, Croit, Db, Rgw, _audit, croit_http_error
from .auth import require_admin
from .croit import CroitClientError
from .policy_settings import (
    ENDPOINTS_KEY,
    MAX_PATTERN_LEN,
    NAMING_KEY,
    PLACEMENT_KEY,
    QUOTA_KEY,
    SettingsError,
    check_dns_name,
    check_endpoint_capacity,
    check_mapping_unique,
    load_default_quota,
    load_endpoints,
    load_naming,
    load_placement,
    normalize_endpoint,
    normalize_naming,
    normalize_placement,
    normalize_quota,
    placement_names,
    suggest_prefix,
    validate_ad_username,
    validate_bucket_name,
    validate_rgw_uid,
)

router = APIRouter(
    prefix="/admin/settings", tags=["admin-settings"], dependencies=[Depends(require_admin)]
)
Body_ = Annotated[dict[str, Any], Body()]


def _bad(e: SettingsError, status: int = 422) -> HTTPException:
    return HTTPException(status, str(e))


# -- placements (read from croit) -------------------------------------------------


async def _placements(croit) -> tuple[list[dict], str | None]:
    """(rows, croit's zone default rule). Raises HTTPException on croit errors."""
    try:
        res = await croit.list_placements()
    except CroitClientError as e:
        raise croit_http_error(e) from e
    return (res.get("placements") or []), res.get("defaultPlacementRule")


def _placement_view(stored: dict, rows: list[dict] | None, croit_default: str | None, error):
    classes: dict[str, list[str]] = {}
    for r in rows or []:
        classes.setdefault(str(r["name"]), []).append(str(r["storageClass"]))
    available = None if rows is None else [
        {"name": n, "storageClasses": sorted(c), "isDefault": n == croit_default}
        for n, c in classes.items()
    ]
    names = list(classes)
    stale = (
        sorted({n for n in [stored["default"], *stored["allowed"]] if n and n not in names})
        if rows is not None else []
    )
    return {
        "default": stored["default"], "allowed": stored["allowed"],
        "croitDefault": croit_default, "available": available,
        "stale": stale, "error": error,
    }


# -- mappings ------------------------------------------------------------------------


def _mapping_view(m: dict, exists: bool | None) -> dict[str, Any]:
    return {
        "adUsername": m["ad_username"], "rgwUid": m["rgw_uid"], "exists": exists,
        "createdAt": m["created_at"], "createdBy": m["created_by"],
    }


async def _known_uids(croit) -> set[str] | None:
    try:
        return {str(u["uid"]) for u in await croit.list_users()}
    except CroitClientError:
        return None  # a warning, never an error


# -- reads ---------------------------------------------------------------------------


@router.get("")
async def get_settings(_a: Admin, croit: Croit, db: Db):
    s = db.settings
    stored_placement = await db.run(load_placement, s)
    rows, croit_default, perr = None, None, None
    try:
        res = await croit.list_placements()
        rows, croit_default = (res.get("placements") or []), res.get("defaultPlacementRule")
    except CroitClientError as e:
        perr = croit_http_error(e).detail
    known = await _known_uids(croit)
    quota = await db.run(load_default_quota, s)
    mappings = await db.run(db.uid_mappings.list)
    return {
        "naming": await db.run(load_naming, s),
        "placement": _placement_view(stored_placement, rows, croit_default, perr),
        "defaultQuota": {"isSet": quota is not None, "quota": quota},
        "uidMappings": [
            _mapping_view(m, None if known is None else m["rgw_uid"] in known) for m in mappings
        ],
        "notificationEndpoints": await db.run(load_endpoints, s),
        "limits": {"maxPatternLength": MAX_PATTERN_LEN},
    }


# -- naming --------------------------------------------------------------------------


@router.put("/naming")
async def set_naming(body: Body_, admin: Admin, db: Db):
    try:
        new = await asyncio.to_thread(normalize_naming, body)
    except SettingsError as e:
        raise _bad(e) from e
    before = await db.run(load_naming, db.settings)
    await db.run(db.settings.set, NAMING_KEY, new, admin.username)
    await _audit(db, admin, "admin.settings.naming", "setting", NAMING_KEY,
                 {"before": before, "after": new})
    return new


@router.post("/naming/test")
async def test_naming(body: Body_, _a: Admin):
    """Try a name against *draft* rules without saving them."""
    name, username = body.get("name"), body.get("username")
    if not isinstance(name, str) or not isinstance(username, str) or len(name) > 255:
        raise HTTPException(422, "Give a name and a username to test with.")
    try:
        rules = await asyncio.to_thread(normalize_naming, body.get("rules") or {})
    except SettingsError as e:
        return {"rulesError": str(e), "errors": [], "prefix": ""}
    return {
        "rulesError": None,
        "errors": await asyncio.to_thread(validate_bucket_name, name, username, rules),
        "prefix": suggest_prefix(username, rules),
        "dnsOnly": check_dns_name(name),
    }


# -- placement -----------------------------------------------------------------------


@router.put("/placement")
async def set_placement(body: Body_, admin: Admin, croit: Croit, db: Db):
    rows, croit_default = await _placements(croit)
    try:
        new = normalize_placement(body, placement_names(rows))
    except SettingsError as e:
        raise _bad(e) from e
    before = await db.run(load_placement, db.settings)
    await db.run(db.settings.set, PLACEMENT_KEY, new, admin.username)
    await _audit(db, admin, "admin.settings.placement", "setting", PLACEMENT_KEY,
                 {"before": before, "after": new})
    return _placement_view(new, rows, croit_default, None)


# -- default quota -------------------------------------------------------------------


@router.put("/default-quota")
async def set_default_quota(body: Body_, admin: Admin, db: Db):
    try:
        new = normalize_quota(body)
    except SettingsError as e:
        raise _bad(e) from e
    before = await db.run(load_default_quota, db.settings)
    await db.run(db.settings.set, QUOTA_KEY, new, admin.username)
    await _audit(db, admin, "admin.settings.default_quota", "setting", QUOTA_KEY,
                 {"before": before, "after": new})
    return {"isSet": True, "quota": new}


@router.delete("/default-quota")
async def unset_default_quota(admin: Admin, db: Db):
    before = await db.run(load_default_quota, db.settings)
    if before is None:
        return {"isSet": False, "quota": None}
    await db.run(db.settings.delete, QUOTA_KEY)
    await _audit(db, admin, "admin.settings.default_quota", "setting", QUOTA_KEY,
                 {"before": before, "after": None})
    return {"isSet": False, "quota": None}


# -- uid mappings --------------------------------------------------------------------


def _invalidate_mapping(rgw, ad: str, *targets: str | None) -> None:
    """Forget cached resolutions a mapping change can alter: the AD user, whoever
    resolved to an old or new target, and the AD user whose default name is a
    target's bare uid (their cached `uid_taken` or old result)."""
    rgw.invalidate(username=ad)
    for t in targets:
        if t:
            rgw.invalidate(uid=t)
            rgw.invalidate(username=t.rpartition("$")[2])


@router.put("/uid-mappings/{ad_username}")
async def put_mapping(ad_username: str, body: Body_, admin: Admin, croit: Croit, db: Db, rgw: Rgw):
    if set(body) - {"rgwUid"}:
        raise HTTPException(422, "Unknown field.")
    try:
        ad = validate_ad_username(ad_username)
        uid = validate_rgw_uid(body.get("rgwUid"))
    except SettingsError as e:
        raise _bad(e) from e
    from .bucket_keys import KEY_BARE
    from .rgw_user import same_uid, split_uid
    if KEY_BARE.match(split_uid(uid)[1].lower()) or any(
            same_uid(k["rgw_uid"], uid)
            for k in await db.run(db.bucket_keys.list, include_revoked=True)):
        raise HTTPException(422, f"{uid} is reserved for a bucket key and cannot be a "
                                 "mapping target.")
    existing = await db.run(db.uid_mappings.list)
    try:
        check_mapping_unique(ad, uid, existing)
    except SettingsError as e:
        raise _bad(e, 409) from e
    before = await db.run(db.uid_mappings.get, ad)
    try:
        await db.run(db.uid_mappings.set, ad, uid, admin.username)
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, f"{uid} is already mapped from another user.") from e
    _invalidate_mapping(rgw, ad, uid, before and before["rgw_uid"])
    known = await _known_uids(croit)
    exists = None if known is None else uid in known
    saved = await db.run(db.uid_mappings.get, ad)
    await _audit(db, admin, "admin.settings.uid_mapping.set", "uid_mapping", ad,
                 {"before": before and before["rgw_uid"], "after": uid, "rgwUserExists": exists})
    warning = None
    if exists is False:
        warning = (f"No RGW user {uid} exists yet. The mapping is saved, but the user cannot "
                   "use S3 until that RGW user exists.")
    elif exists is None:
        warning = "Could not check whether that RGW user exists (croit unreachable)."
    return {**_mapping_view(saved, exists), "warning": warning}


@router.delete("/uid-mappings/{ad_username}")
async def delete_mapping(ad_username: str, admin: Admin, db: Db, rgw: Rgw):
    try:
        ad = validate_ad_username(ad_username)
    except SettingsError as e:
        raise _bad(e) from e
    before = await db.run(db.uid_mappings.get, ad)
    if before is None:
        raise HTTPException(404, "No such mapping.")
    await db.run(db.uid_mappings.delete, ad)
    _invalidate_mapping(rgw, ad, before["rgw_uid"])
    await _audit(db, admin, "admin.settings.uid_mapping.delete", "uid_mapping",
                 before["ad_username"], {"before": before["rgw_uid"], "after": None})
    return {"adUsername": before["ad_username"], "deleted": True}


# -- notification endpoints ----------------------------------------------------------


@router.put("/notification-endpoints/{name}")
async def put_endpoint(name: str, body: Body_, admin: Admin, db: Db):
    try:
        ep = normalize_endpoint({**body, "name": name})
        existing = await db.run(load_endpoints, db.settings)
        check_endpoint_capacity(ep, existing)
    except SettingsError as e:
        raise _bad(e) from e
    old = next((e for e in existing if e["name"].lower() == ep["name"].lower()), None)
    new_list = [e for e in existing if e is not old] + [ep]
    new_list.sort(key=lambda e: e["name"].lower())
    await db.run(db.settings.set, ENDPOINTS_KEY, new_list, admin.username)
    await _audit(db, admin, "admin.settings.notification_endpoint.set", "notification_endpoint",
                 ep["name"], {"before": old, "after": ep})
    return ep


@router.delete("/notification-endpoints/{name}")
async def delete_endpoint(name: str, admin: Admin, db: Db):
    existing = await db.run(load_endpoints, db.settings)
    old = next((e for e in existing if e["name"].lower() == name.lower()), None)
    if old is None:
        raise HTTPException(404, "No such endpoint.")
    rest = [e for e in existing if e is not old]
    await db.run(db.settings.set, ENDPOINTS_KEY, rest, admin.username)
    await _audit(db, admin, "admin.settings.notification_endpoint.delete", "notification_endpoint",
                 old["name"], {"before": old, "after": None})
    return {"name": old["name"], "deleted": True}
