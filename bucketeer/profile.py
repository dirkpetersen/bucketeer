"""Profile API: the signed-in user's own preferences, keyed by AD username.

- `GET /api/profile` -> `{username, displayName, email, defaultBudgetNumber|null,
  budgetExample|null}` (the admin's example hint)
- `PUT /api/profile` `{defaultBudgetNumber?: string|null, advancedMode?: bool}` ->
  the same shape (plus `advancedMode`). Partial update: only the fields present in
  the body change. Null or an empty budget string clears it. Rules:
  `budget.validate_budget_number` (422 with a message). Each changed field is
  audited as `profile.update` with `field`, old and new value.

The username comes only from the session, never from the request.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import SessionUser, require_user
from .budget import BudgetNumberError, budget_example, validate_budget_number

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaultBudgetNumber: str | None = Field(default=None, max_length=4096)
    advancedMode: bool | None = None


def _view(user: SessionUser, stored: str | None, advanced: bool, example: str | None) -> dict:
    return {
        "username": user.username,
        "displayName": user.display_name,
        "email": user.email,
        "defaultBudgetNumber": stored,
        "advancedMode": advanced,
        "budgetExample": example,
    }


@router.get("")
async def get_profile(request: Request, user: Annotated[SessionUser, Depends(require_user)]):
    db = request.app.state.db
    profile = await db.run(db.profiles.get, user.username)
    example = await db.run(budget_example, db.settings)
    return _view(user, profile["default_budget_number"], profile["advanced_mode"], example)


@router.put("")
async def put_profile(
    request: Request, body: ProfileUpdate, user: Annotated[SessionUser, Depends(require_user)]
):
    db = request.app.state.db
    sent = body.model_fields_set
    if "advancedMode" in sent and not isinstance(body.advancedMode, bool):
        raise HTTPException(422, "advancedMode must be true or false")
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
    return _view(user, budget, advanced, await db.run(budget_example, db.settings))
