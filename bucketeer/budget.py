"""Budget number (index/worktag) validation: pure, no I/O except reading the setting.

`validate_budget_number(value, settings_store)` returns the cleaned value (or None
for "none") and raises `BudgetNumberError` with a user-facing message.

Base rules, always applied: text; trimmed; at most MAX_LEN characters; only the
characters S3 allows in tag values (the value is mirrored into the bucket tag
`bucketeer-budget`): ASCII letters, digits, spaces and `+ - = . _ : / @`. No
control characters, no markup, no `#`, no non-ASCII. Empty after trimming means
"none".

If the `budget_number` setting exists with a non-empty `pattern` (the admin-editable
format; optional `example` shown in the message and as a hint in the forms) the value
must also fullmatch it. `normalize_budget_rules` validates the admin's input (same
guards as the naming pattern; the example must itself pass base rules and pattern).
A pattern change never touches stored numbers. Matching goes through the
approved-pattern machinery of `policy_settings` only, which can block briefly on a
new pattern: async callers use `asyncio.to_thread`.
"""

import re
from typing import Any

from .policy_settings import MAX_PATTERN_LEN, SettingsError, approve_pattern, pattern_matches

MAX_LEN = 64
BUDGET_KEY = "budget_number"  # settings table: {"pattern": str, "example": str}

_ALLOWED = re.compile(r"[A-Za-z0-9 +\-=._:/@]+\Z")


class BudgetNumberError(ValueError):
    pass


class BudgetFormatError(BudgetNumberError):
    """The value is fine as text but does not match the admin pattern."""


def _check(value: Any, rules: Any) -> str | None:
    """Base rules, then the admin pattern in `rules` ({pattern, example}) if any."""
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
    pattern = rules.get("pattern") if isinstance(rules, dict) else None
    if isinstance(pattern, str) and pattern:
        example = rules.get("example")
        hint = f" Example: {example}" if isinstance(example, str) and example else ""
        try:
            ok = pattern_matches(pattern, value)
        except TimeoutError:
            raise BudgetNumberError(
                "The budget number could not be checked; contact an administrator."
            ) from None
        except SettingsError:
            raise BudgetNumberError(
                "The administrators' budget number format is misconfigured."
            ) from None
        if not ok:
            raise BudgetFormatError(
                "The budget number does not match the format required by the "
                "administrators." + hint
            )
    return value


def validate_budget_number(value: Any, settings_store) -> str | None:
    return _check(value, settings_store.get(BUDGET_KEY, None))


def load_budget_rules(settings_store) -> dict[str, str]:
    """The stored shape, always `{pattern, example}` strings (malformed -> empty)."""
    raw = settings_store.get(BUDGET_KEY, None)
    raw = raw if isinstance(raw, dict) else {}
    return {k: raw[k] if isinstance(raw.get(k), str) else "" for k in ("pattern", "example")}


def budget_example(settings_store) -> str | None:
    """The hint shown in the profile and create-bucket forms (None = none set)."""
    return load_budget_rules(settings_store)["example"] or None


def budget_matches(value: str | None, pattern: str) -> bool | None:
    """Does a stored number satisfy the admin `pattern`? None = nothing to judge
    (no number, or no pattern). A TimeoutError propagates so callers can stop
    after a few. Blocking: async callers use `asyncio.to_thread`."""
    if not value or not pattern:
        return None
    try:
        return pattern_matches(pattern, value)
    except SettingsError:
        return False


def normalize_budget_rules(raw: Any) -> dict[str, str]:
    """Validate the admin's `{pattern, example}` (both optional, empty = off).
    The pattern passes the naming guards (length, static check, subprocess
    approval); the example must pass the base rules and the pattern. Blocking.
    Raises SettingsError."""
    if not isinstance(raw, dict):
        raise SettingsError("Budget number settings must be an object.")
    extra = set(raw) - {"pattern", "example"}
    if extra:
        raise SettingsError(f"Unknown field: {sorted(extra)[0]}.")
    pattern, example = raw.get("pattern") or "", raw.get("example") or ""
    if not isinstance(pattern, str) or not isinstance(example, str):
        raise SettingsError("Pattern and example must be text.")
    pattern, example = pattern.strip(), example.strip()
    if len(pattern) > MAX_PATTERN_LEN:
        raise SettingsError(f"The extra pattern can be at most {MAX_PATTERN_LEN} characters.")
    if pattern:
        approve_pattern(pattern)
    if example:
        try:
            _check(example, {"pattern": pattern})
        except BudgetFormatError:
            raise SettingsError("The example does not match the pattern.") from None
        except BudgetNumberError as e:
            raise SettingsError(f"The example is not a valid budget number: {e}") from None
    return {"pattern": pattern, "example": example}
