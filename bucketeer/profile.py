"""Profile API: the signed-in user's own preferences, keyed by AD username.

- `GET /api/profile` -> `{username, displayName, email, defaultBudgetNumber|null,
  budgetExample|null}` (the admin's example hint)
- `PUT /api/profile` `{defaultBudgetNumber?: string|null, advancedMode?: bool,
  location?: "onprem"|"cloud"|null}` -> the same shape (plus `advancedMode` and the
  location fields of `location.view`). Partial update: only the fields present in
  the body change. Null or an empty budget string clears it. Rules:
  `budget.validate_budget_number` (422 with a message). Each changed field is
  audited as `profile.update` with `field`, old and new value. `location: "cloud"` is
  refused (422) while cloud is not available; null follows the admin's default.

The username comes only from the session, never from the request.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import SessionUser, require_user
from .budget import BudgetNumberError, budget_example, validate_budget_number
from .location import LOCATIONS, cloud_available
from .location import view as location_view

# Owner, 2026-11-21: no new bucket or credential until the user saved a default budget
# number. Admins are exempt; existing keys stay revealable. Tests switch it off
# (conftest) and test it in tests/test_budget_gate.py.
REQUIRE_DEFAULT_BUDGET = True
BUDGET_MISSING_TEXT = ("Save a default budget number in your profile first. Every bucket "
                       "and every credential is charged to one.")


class BudgetMissing(Exception):
    """Answered as 409 {detail, budgetMissing: true} by the handler in main.py."""


async def require_default_budget(request: Request,
                                 user: Annotated[SessionUser, Depends(require_user)]) -> None:
    if not REQUIRE_DEFAULT_BUDGET or user.is_admin:
        return
    db = request.app.state.db
    if not (await db.run(db.profiles.get, user.username))["default_budget_number"]:
        raise BudgetMissing

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaultBudgetNumber: str | None = Field(default=None, max_length=4096)
    advancedMode: bool | None = None
    location: str | None = None


async def _view(request: Request, user: SessionUser, profile: dict) -> dict:
    db = request.app.state.db
    return {
        **await db.run(location_view, request.app, profile),
        "username": user.username,
        "displayName": user.display_name,
        "email": user.email,
        "defaultBudgetNumber": profile["default_budget_number"],
        "advancedMode": profile["advanced_mode"],
        "budgetExample": await db.run(budget_example, db.settings),
    }


@router.get("")
async def get_profile(request: Request, user: Annotated[SessionUser, Depends(require_user)]):
    db = request.app.state.db
    return await _view(request, user, await db.run(db.profiles.get, user.username))


@router.put("")
async def put_profile(
    request: Request, body: ProfileUpdate, user: Annotated[SessionUser, Depends(require_user)]
):
    db = request.app.state.db
    sent = body.model_fields_set
    if "advancedMode" in sent and not isinstance(body.advancedMode, bool):
        raise HTTPException(422, "advancedMode must be true or false")
    if "location" in sent:  # checked before anything is written
        if body.location is not None and body.location not in LOCATIONS:
            raise HTTPException(422, "location must be \"onprem\", \"cloud\" or null")
        if body.location == "cloud" and not cloud_available(request.app):
            raise HTTPException(422, "Cloud storage isn't available right now.")
    profile = await db.run(db.profiles.get, user.username)
    budget, advanced = profile["default_budget_number"], profile["advanced_mode"]
    if "defaultBudgetNumber" in sent:
        try:
            value = await asyncio.to_thread(
                validate_budget_number, body.defaultBudgetNumber, db.settings
            )
        except BudgetNumberError as exc:
            raise HTTPException(422, str(exc)) from None
        if value != budget:
            await db.run(db.profiles.set_default_budget_number, user.username, value)
            await db.run(db.audit.add, user.username, "profile.update", "profile",
                         user.username,
                         {"field": "default_budget_number", "old": budget, "new": value})
            budget = value
    if "advancedMode" in sent and body.advancedMode != advanced:
        await db.run(db.profiles.set_advanced_mode, user.username, body.advancedMode)
        await db.run(db.audit.add, user.username, "profile.update", "profile",
                     user.username,
                     {"field": "advanced_mode", "old": advanced, "new": body.advancedMode})
        advanced = body.advancedMode
    if "location" in sent and body.location != profile["location"]:
        await db.run(db.profiles.set_location, user.username, body.location)
        await db.run(db.audit.add, user.username, "profile.update", "profile",
                     user.username,
                     {"field": "location", "old": profile["location"], "new": body.location})
    return await _view(request, user, await db.run(db.profiles.get, user.username))
