"""Budget number (index/worktag) validation: pure, no I/O except reading the setting.

`validate_budget_number(value, settings_store)` returns the cleaned value (or None
for "none") and raises `BudgetNumberError` with a user-facing message.

Base rules, always applied: text; trimmed; at most MAX_LEN characters; only the
characters S3 allows in tag values (the value is mirrored into the bucket tag
`bucketeer-budget`): ASCII letters, digits, spaces and `+ - = . _ : / @`. No
control characters, no markup, no `#`, no non-ASCII. Empty after trimming means
"none".

If the `budget_number` setting exists with a non-empty `pattern` (the admin-editable
format, a later TODO item; optional `example` shown in the message) the value must
also fullmatch it. Matching goes through the approved-pattern machinery of
`policy_settings` only, which can block briefly on a new pattern: async callers
use `asyncio.to_thread`.
"""

import re
from typing import Any

from .policy_settings import SettingsError, pattern_matches

MAX_LEN = 64
BUDGET_KEY = "budget_number"  # settings table: {"pattern": str, "example": str}

_ALLOWED = re.compile(r"[A-Za-z0-9 +\-=._:/@]+\Z")


class BudgetNumberError(ValueError):
    pass


def validate_budget_number(value: Any, settings_store) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BudgetNumberError("The budget number must be text.")
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_LEN:
        raise BudgetNumberError(f"The budget number can be at most {MAX_LEN} characters.")
    if any(not c.isprintable() for c in value):
        raise BudgetNumberError("The budget number cannot contain control characters.")
    if not _ALLOWED.match(value):
        raise BudgetNumberError(
            "Use only letters (A-Z), digits, spaces and the characters + - = . _ : / @"
        )
    rules = settings_store.get(BUDGET_KEY, None)
    pattern = rules.get("pattern") if isinstance(rules, dict) else None
    if isinstance(pattern, str) and pattern:
        example = rules.get("example")
        hint = f" Example: {example}" if isinstance(example, str) and example else ""
        try:
            ok = pattern_matches(pattern, value)
        except TimeoutError:
            raise BudgetNumberError("The budget number could not be checked; try again.") from None
        except SettingsError:
            raise BudgetNumberError(
                "The administrators' budget number format is misconfigured."
            ) from None
        if not ok:
            raise BudgetNumberError(
                "The budget number does not match the format required by the "
                "administrators." + hint
            )
    return value
