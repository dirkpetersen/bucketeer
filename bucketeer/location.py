"""Storage location: On Premises or Cloud (AWS), Phase 6 (P6-8).

- **Global default** (admin, settings key `location` `{default: onprem|cloud}`, absent =
  `onprem`): `GET /api/admin/settings/location` -> `{default, configured, region,
  bucketPrefix, check: {ok, accountId, region, reason, checkedAt}|null}` (runs a fresh
  connection check when cloud is configured); `PUT {default}` -> the same. `cloud` is
  refused with 422 unless cloud is configured and a fresh check passes. Audited
  `admin.settings.location` (before/after) when changed.
- **Per user** (`user_profiles.location`, NULL = follow the default): the user's choice
  counts only while Advanced mode is on; otherwise everyone works in the global default.
  `view()` gives `/api/me` and `/api/profile` `{location (effective), locationChoice,
  defaultLocation, cloudAvailable}`. Cloud is effective only while `cloudAvailable`
  (configured and the last connection check passed); else on premises.

Later items (cloud buckets, keys, backups) read `effective_location`.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .admin import Admin, Db, _audit
from .auth import require_admin

LOCATION_KEY = "location"
LOCATIONS = ("onprem", "cloud")
DEFAULT_LOCATION = "onprem"


def load_default_location(settings_store) -> str:
    stored = settings_store.get(LOCATION_KEY, None) or {}
    value = stored.get("default") if isinstance(stored, dict) else None
    return value if value in LOCATIONS else DEFAULT_LOCATION


def cloud_available(app) -> bool:
    cloud = getattr(app.state, "cloud", None)
    return cloud is not None and cloud.available


def effective_location(choice: str | None, advanced: bool, default: str,
                       available: bool) -> str:
    loc = choice if advanced and choice in LOCATIONS else default
    return loc if loc != "cloud" or available else "onprem"


def view(app, profile: dict) -> dict[str, Any]:
    """Location fields for /api/me and /api/profile (sync: one settings read)."""
    db = app.state.db
    default = load_default_location(db.settings)
    available = cloud_available(app)
    return {
        "location": effective_location(profile.get("location"), profile["advanced_mode"],
                                       default, available),
        "locationChoice": profile.get("location"),
        "defaultLocation": default,
        "cloudAvailable": available,
    }


# -- admin setting ---------------------------------------------------------------------

router = APIRouter(prefix="/admin/settings/location", tags=["admin-settings"],
                   dependencies=[Depends(require_admin)])


async def _admin_view(request: Request, db, check: dict | None = None) -> dict[str, Any]:
    cloud = request.app.state.cloud
    s = request.app.state.settings
    if cloud is not None and check is None:
        check = await cloud.check()
    return {
        "default": await db.run(load_default_location, db.settings),
        "configured": cloud is not None,
        "region": s.cloud_region,
        "bucketPrefix": s.cloud_bucket_prefix,
        "check": check,
    }


@router.get("")
async def get_location(request: Request, _a: Admin, db: Db):
    return await _admin_view(request, db)


@router.put("")
async def set_location(request: Request, body: Annotated[dict[str, Any], Body()],
                       admin: Admin, db: Db):
    value = body.get("default")
    if set(body) != {"default"} or value not in LOCATIONS:
        raise HTTPException(422, "default must be \"onprem\" or \"cloud\"")
    cloud = request.app.state.cloud
    check = None
    if value == "cloud":
        if cloud is None:
            raise HTTPException(422, "Cloud is not set up: set the CLOUD_* variables in "
                                     ".env first (see the admin guide).")
        check = await cloud.check()
        if not check["ok"]:
            raise HTTPException(422, f"The AWS connection check failed: {check['reason']}")
    before = await db.run(load_default_location, db.settings)
    if value != before:
        await db.run(db.settings.set, LOCATION_KEY, {"default": value}, admin.username)
        await _audit(db, admin, "admin.settings.location", "setting", LOCATION_KEY,
                     {"before": before, "after": value})
    return await _admin_view(request, db, check)
