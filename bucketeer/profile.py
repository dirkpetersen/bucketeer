"""Profile API: the signed-in user's own preferences, keyed by AD username.

- `GET /api/profile` -> `{username, displayName, email, defaultBudgetNumber|null}`
- `PUT /api/profile` `{defaultBudgetNumber: string|null}` -> the same shape. Null or
  an empty string clears it. Rules: `budget.validate_budget_number` (422 with a
  message). A change is audited as `profile.update` with old and new value.

The username comes only from the session, never from the request.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import SessionUser, require_user
from .budget import BudgetNumberError, validate_budget_number

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaultBudgetNumber: str | None = Field(default=None, max_length=4096)


def _view(user: SessionUser, stored: str | None) -> dict:
    return {
        "username": user.username,
        "displayName": user.display_name,
        "email": user.email,
        "defaultBudgetNumber": stored,
    }


@router.get("")
async def get_profile(request: Request, user: Annotated[SessionUser, Depends(require_user)]):
    db = request.app.state.db
    profile = await db.run(db.profiles.get, user.username)
    return _view(user, profile["default_budget_number"])


@router.put("")
async def put_profile(
    request: Request, body: ProfileUpdate, user: Annotated[SessionUser, Depends(require_user)]
):
    db = request.app.state.db
    try:
        value = await asyncio.to_thread(
            validate_budget_number, body.defaultBudgetNumber, db.settings
        )
    except BudgetNumberError as exc:
        raise HTTPException(422, str(exc)) from None
    old = (await db.run(db.profiles.get, user.username))["default_budget_number"]
    if value != old:
        await db.run(db.profiles.set_default_budget_number, user.username, value)
        await db.run(db.audit.add, user.username, "profile.update", "profile",
                     user.username, {"field": "default_budget_number", "old": old, "new": value})
    return _view(user, value)
