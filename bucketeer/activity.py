"""My activity: GET /api/activity, what happened to the person's own account and buckets.

`GET /api/activity?before_id=&limit=` (`require_user`, `no-store`; `limit` default 50, silently
capped at 100, < 1 -> 422; newest first by id, `nextCursor` = the last id of a full page, pass it
as `before_id`) -> `{entries: [{id, ts, area, action, target, actor, summary}], nextCursor,
partial}`. A row shows when it is
- done by the person: `actor` is their AD username (case-insensitively, as SQLite NOCASE
  compares), except `login.failure` / `login.throttled`, whose actor is whatever name was
  TYPED (anyone can type someone else's name);
- about the person's things, done by Bucketeer (`system`) or by an administrator (an `admin.*`
  or `offboarding.*` action): target `rgw_user` = their resolved RGW uid, `profile` = their
  username, `access_key` = one of their current own keys (on premises from the admin client,
  cloud from IAM; a failed read skips that part), `bucket_key` = one of their bucket key rows,
  `bucket` = one of their buckets (on premises from the admin client's list, else, while it
  cannot answer, the SQLite rows; cloud from the SQLite rows), never rows from before a
  bucket's recorded creation (an earlier owner's bucket of the same name);
- a share of somebody else's bucket that names them as the recipient (`bucket.share|
  share_update|unshare` whose `details.uid` is their uid, or `cloud.bucket.share|share_update|
  unshare` whose `details.recipient` is their AD name).
Other people's rows about the same names (an earlier owner of a bucket, someone who typed the
person's username at login) never show. `partial` is true when the account or the bucket list
could not be read, so admin/system rows about them may be missing.

Nothing raw leaves: no `details`, no actor names. `actor` is `you`, `bucketeer` (system),
`owner` (the owner of a bucket shared with you) or `admin` (anyone else; the UI says "An
administrator"); `summary` comes from a per-action table that reads only known detail keys
(counts, sizes, access levels, reason codes, the person's own budget numbers and recipients);
unknown actions get one made from the action name; `target` is a short display name (bucket
short name, key as its last 4 characters, subuser or bucket-key label) or null. The filtering
is ONE SQL statement of three indexed branches joined by UNION (the actor branch uses the
index `audit_log_actor_nocase`). Reading is not audited.
"""

import ipaddress
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response

from .auth import SessionUser, require_user
from .cloud import CloudError
from .croit import CroitClientError
from .db import Database, scrub
from .rgw_user import UNAVAILABLE, RgwAccount, current_account, split_uid

log = logging.getLogger("bucketeer.activity")

router = APIRouter(tags=["activity"])  # mounted under /api in main.create_app

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
SHARE_ACTIONS = ("bucket.share", "bucket.share_update", "bucket.unshare")
# cloud shares (cloud_sharing.py) name the recipient by AD name in `details.recipient`
CLOUD_SHARE_ACTIONS = ("cloud.bucket.share", "cloud.bucket.share_update",
                       "cloud.bucket.unshare")
# The actor of these is the name typed into the login form, not an authenticated person.
TYPED_LOGIN_ACTIONS = ("login.failure", "login.throttled")
_TOP = 2**63 - 1
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class Scope:
    """What belongs to the session's person, gathered once per request."""

    username: str
    uid: str | None  # resolved RGW uid, None when there is no usable account
    buckets: tuple[str, ...] = ()  # tenant-qualified on-prem names plus full cloud names
    cloud_buckets: frozenset[str] = frozenset()
    keys: tuple[str, ...] = ()  # access key ids (public halves only)
    key_labels: tuple[tuple[str, str], ...] = ()  # (bucket_keys row id, label)
    cloud_prefix: str = ""  # `<CLOUD_BUCKET_PREFIX><owner part>-`: stripped from cloud names
    partial: bool = False

    @property
    def tenant(self) -> str:
        return split_uid(self.uid)[0] if self.uid else ""


def same_name(a: str, b: str) -> bool:
    """SQLite NOCASE: ASCII letters fold, everything else compares exactly."""
    return a.encode().lower() == b.encode().lower()


# -- the query ---------------------------------------------------------------------------

_COLS = "id, ts, actor, action, target_type, target, details"


def _sql(scope: Scope, before_id: int | None, limit: int) -> tuple[str, dict[str, Any]]:
    p: dict[str, Any] = {
        "me": scope.username, "me_lower": scope.username.lower(),
        "before": before_id or _TOP, "n": limit + 1,
    }
    typed = ", ".join(f"'{a}'" for a in TYPED_LOGIN_ACTIONS)
    branches = [
        # (a) done by the person
        f"SELECT {_COLS} FROM audit_log WHERE actor = :me COLLATE NOCASE AND id < :before "
        f"AND action NOT IN ({typed}) ORDER BY id DESC LIMIT :n",
    ]
    about = [
        "(target_type = 'profile' AND target IN (:me, :me_lower))",
        "(target_type = 'bucket_key' AND target IN (SELECT CAST(k.id AS TEXT) FROM bucket_keys k "
        "WHERE k.owner_ad_username = :me))",
    ]
    if scope.uid:
        p.update(uid=scope.uid, keys=json.dumps(list(scope.keys)),
                 buckets=json.dumps(list(scope.buckets)))
        about += [
            "(target_type = 'rgw_user' AND target = :uid)",
            "(target_type = 'access_key' AND target IN (SELECT value FROM json_each(:keys)))",
            "(target_type = 'bucket' AND target IN (SELECT value FROM json_each(:buckets)) "
            "AND NOT EXISTS (SELECT 1 FROM buckets b WHERE b.name = audit_log.target "
            "AND b.creation_known = 1 AND b.created_at > audit_log.ts))",
        ]
    # (b) about the person's things, by Bucketeer or an administrator
    branches.append(
        f"SELECT {_COLS} FROM audit_log WHERE id < :before AND (actor = 'system' "
        "OR action LIKE 'admin.%' OR action LIKE 'offboarding.%') "
        f"AND ({' OR '.join(about)}) ORDER BY id DESC LIMIT :n")
    if scope.uid:
        # (c) somebody else's bucket shared with the person (details.uid = the recipient);
        # `+target_type` keeps the planner on the action index (few rows), never on
        # audit_log_target, where 'bucket' covers most of the log
        shares = ", ".join(f"'{a}'" for a in SHARE_ACTIONS)
        branches.append(
            f"SELECT {_COLS} FROM audit_log WHERE +target_type = 'bucket' "
            f"AND action IN ({shares}) AND id < :before "
            "AND lower(CASE WHEN json_valid(details) THEN json_extract(details, '$.uid') END) "
            "= lower(:uid) ORDER BY id DESC LIMIT :n")
    # (d) somebody else's cloud bucket shared with the person (details.recipient = AD name)
    cloud_shares = ", ".join(f"'{a}'" for a in CLOUD_SHARE_ACTIONS)
    branches.append(
        f"SELECT {_COLS} FROM audit_log WHERE +target_type = 'bucket' "
        f"AND action IN ({cloud_shares}) AND id < :before "
        "AND lower(CASE WHEN json_valid(details) THEN json_extract(details, '$.recipient') END) "
        "= :me_lower ORDER BY id DESC LIMIT :n")
    sql = " UNION ".join(f"SELECT * FROM ({b})" for b in branches)
    return f"{sql} ORDER BY id DESC LIMIT :n", p


def feed(db: Database, scope: Scope, before_id: int | None = None,
         limit: int = DEFAULT_LIMIT) -> tuple[list[dict[str, Any]], int | None]:
    """The person's entries, newest first, and the next cursor (None on the last page).
    Sync (DB only): call through `db.run`."""
    sql, params = _sql(scope, before_id, limit)
    rows = db.query(sql, params)
    more = len(rows) > limit
    rows = rows[:limit]
    return [entry(r, scope) for r in rows], (rows[-1]["id"] if more else None)


# -- entries -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ctx:
    action: str
    d: dict[str, Any]
    mine: bool  # the person did it
    received: bool  # a share of somebody else's bucket naming the person
    scope: Scope


def _details(raw: Any) -> dict[str, Any]:
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return scrub(d) if isinstance(d, dict) else {}


def entry(r: dict[str, Any], scope: Scope) -> dict[str, Any]:
    d = _details(r["details"])
    mine = same_name(str(r["actor"]), scope.username)
    uid = d.get("uid")
    recipient = d.get("recipient")
    received = bool(not mine and r["target_type"] == "bucket" and (
        (scope.uid and r["action"] in SHARE_ACTIONS
         and isinstance(uid, str) and uid.lower() == scope.uid.lower())
        or (r["action"] in CLOUD_SHARE_ACTIONS and isinstance(recipient, str)
            and same_name(recipient, scope.username))))
    actor = ("you" if mine else "bucketeer" if r["actor"] == "system"
             else "owner" if received else "admin")
    ctx = _Ctx(str(r["action"]), d, mine, received, scope)
    return {
        "id": r["id"], "ts": r["ts"], "area": area(ctx.action), "action": ctx.action,
        "target": _target(r, ctx), "actor": actor, "summary": summary(ctx),
    }


def area(action: str) -> str:
    """bucket | key | share | account | profile | cloud | login | other (the UI's icon)."""
    if action in ("login.success", "logout"):
        return "login"
    if action.startswith(("bucket.share", "cloud.bucket.share")) or action in (
            "bucket.unshare", "cloud.bucket.unshare"):
        return "share"
    if action.startswith("cloud."):
        return "cloud"
    if action.startswith(("credentials.", "subuser.", "bucket_key.", "admin.bucket_key.")):
        return "key"
    if action.startswith(("bucket.", "object.", "admin.bucket.", "admin.buckets.")):
        return "bucket"
    if action.startswith(("rgw.user.", "admin.user.", "offboarding.")):
        return "account"
    if action.startswith("profile."):
        return "profile"
    return "other"


def _bucket_name(full: str, ctx: _Ctx) -> str:
    """`tenant:name` -> `name` in the person's own tenant (else kept as the address);
    a cloud bucket's full AWS name -> the name the person chose."""
    s = ctx.scope
    tenant, sep, short = full.rpartition(":")
    name = short if sep and tenant == s.tenant else full
    cloudy = full in s.cloud_buckets or ctx.action.startswith("cloud.") or \
        ctx.d.get("location") == "cloud"
    if cloudy and s.cloud_prefix and name.startswith(s.cloud_prefix) and \
            len(name) > len(s.cloud_prefix):
        return name[len(s.cloud_prefix):]
    return name


def _target(r: dict[str, Any], ctx: _Ctx) -> str | None:
    kind, t = r["target_type"], str(r["target"])
    if kind == "bucket":
        return None if t == "*" else _bucket_name(t, ctx)
    if kind == "access_key":
        return _key(t)
    if kind == "subuser":
        return t.rpartition(":")[2] or None
    if kind == "bucket_key":
        label = dict(ctx.scope.key_labels).get(t) or _text(ctx.d.get("label"), 20)
        return label or f"#{t}"
    if kind == "rgw_user":
        # the person's own account; another uid only appears in their own admin work
        own = ctx.scope.uid is not None and t.lower() == ctx.scope.uid.lower()
        return None if own or not ctx.mine else _text(t, 80)
    if kind in ("profile", "user", "cloud_user"):
        return None  # the person's own profile, sign-in or cloud identity
    # anything else (settings, reports, runs) is only ever the person's own admin work
    return _text(t, 80) if ctx.mine else None


# -- summary helpers ---------------------------------------------------------------------


def _num(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _text(v: Any, limit: int = 64) -> str | None:
    if not isinstance(v, str):
        return None
    v = _CTRL.sub("", v).strip()
    return (v[: limit - 1] + "…" if len(v) > limit else v) or None


def _key(v: Any) -> str | None:
    v = _text(v, 128)
    return None if v is None else ("…" + v[-4:] if len(v) > 4 else v)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


_UNITS = (("PiB", 2**50), ("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10))


def _bytes(n: int) -> str:
    for unit, size in _UNITS:
        if n >= size:
            v = n / size
            return f"{v:.0f} {unit}" if v >= 100 or v == int(v) else f"{v:.1f} {unit}"
    return f"{n} B"


def _tib(v: Any) -> str | None:
    n = _num(v)
    return None if n is None else f"{n:,} TiB"


def _quota(q: Any) -> str | None:
    """A {enabled, maxSize, maxObjects} quota as text ("5 TiB", "unlimited"), or None."""
    if not isinstance(q, dict):
        return None
    size = _num(q.get("maxSize"))
    if not q.get("enabled") or not size:
        return "unlimited"
    return _bytes(size)


def _change(old: str | None, new: str | None, what: str) -> str:
    if old and new and old != new:
        return f"{what} changed from {old} to {new}"
    if new:
        return f"{what} set to {new}"
    return f"{what} changed"


def _duration(seconds: Any) -> str | None:
    s = _num(seconds)
    if not s:
        return None
    if s % 86400 == 0:
        return _plural(s // 86400, "day")
    if s % 3600 == 0:
        return _plural(s // 3600, "hour")
    return _plural(max(1, s // 60), "minute")


def _ip(v: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(v)) if isinstance(v, str) else None
    except ValueError:
        return None


def _with(base: str, *extras: str | None) -> str:
    parts = [e for e in extras if e]
    return f"{base} ({', '.join(parts)})" if parts else base


_ACCESS = {"read": "read-only", "readwrite": "read and write"}
_PERMISSION = {"read": "read", "write": "write", "read-write": "read and write",
               "full-control": "full control"}
_OS = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
_CLASS = {"intelligent": "Intelligent-Tiering", "deep_archive": "Deep Archive"}
_UNSHARE = {"bucket_deleted": "the bucket was deleted",
            "bucket_gone": "the bucket no longer exists",
            "owner_changed": "the bucket has a new owner"}
_SETUP_REFUSED = {
    "admin_unavailable": "the storage service was unavailable", "no_account": "no S3 account",
    "suspended": "the account is suspended", "key_gone": "the key was rotated or deleted",
    "no_endpoint": "no S3 endpoint is configured",
    "unsafe_value": "the key can't be installed automatically",
    "cloud_unavailable": "cloud storage was unavailable",
    "cloud_refused": "the cloud account couldn't be set up",
}
_OFFBOARDING = {"disabled": "your directory account is disabled",
                "not_in_group": "you are no longer in the group that may use Bucketeer",
                "not_found": "your directory account was not found"}


def _word(v: Any, table: dict[str, str]) -> str | None:
    return table.get(v) if isinstance(v, str) else None


# -- per-action summaries ------------------------------------------------------------------
# Each reads only the detail keys it names; values are numbers, known words, or (only for the
# person's own actions) their own budget numbers, recipients and subuser names.


def _login(c: _Ctx) -> str:
    ip = _ip(c.d.get("ip"))
    return f"Signed in from {ip}" if ip else "Signed in"


def _profile(c: _Ctx) -> str:
    field, new = c.d.get("field"), c.d.get("new")
    if field == "default_budget_number":
        value = _text(new) if c.mine else None
        return f"Default budget number set to {value}" if value else \
            "Default budget number removed" if new is None else "Default budget number changed"
    if field == "advanced_mode" and isinstance(new, bool):
        return f"Advanced mode turned {'on' if new else 'off'}"
    if field == "location":
        where = {"cloud": "Cloud", "onprem": "On premises"}.get(new)
        return f"Storage location set to {where}" if where else \
            "Storage location set to follow the default"
    return "Profile updated"


def _rotated(base: str) -> Callable[[_Ctx], str]:
    def fn(c: _Ctx) -> str:
        new = _key(c.d.get("newKey"))
        text = f"{base}, new key {new}" if new else base
        return text + ("; the old key could not be deleted"
                       if c.d.get("old_key_removed") is False else "")
    return fn


def _setup_link(c: _Ctx) -> str:
    return _with("Setup command made", _word(c.d.get("os"), _OS),
                 "Cloud" if c.d.get("location") == "cloud" else None)


def _setup(c: _Ctx) -> str:
    ip = _ip(c.d.get("ip"))
    return _with("Computer set up", _word(c.d.get("os"), _OS), f"from {ip}" if ip else None)


def _setup_refused(c: _Ctx) -> str:
    why = _word(c.d.get("reason"), _SETUP_REFUSED)
    return f"Setup command refused: {why}" if why else "Setup command refused"


def _subuser_create(c: _Ctx) -> str:
    perm = _word(c.d.get("permission"), _PERMISSION)
    return f"Subuser created with {perm} access" if perm else "Subuser created"


def _subuser_update(c: _Ctx) -> str:
    old, new = _word(c.d.get("old"), _PERMISSION), _word(c.d.get("permission"), _PERMISSION)
    return _change(old, new, "Subuser access")


def _subuser_key(base: str) -> Callable[[_Ctx], str]:
    def fn(c: _Ctx) -> str:
        name = _text(c.d.get("subuser"), 20) if c.mine else None
        return f"{base} for subuser {name}" if name else base
    return fn


def _bucket_key_create(c: _Ctx) -> str:
    bucket = c.d.get("bucket")
    where = _bucket_name(bucket, c) if isinstance(bucket, str) and c.mine else None
    access = _word(c.d.get("access"), _ACCESS)
    return _with(f"Bucket key created for {where}" if where else "Bucket key created", access)


def _bucket_key_cleanup(c: _Ctx) -> str:
    return "Unfinished bucket key cleaned up" if c.d.get("result") == "rolled_back" else \
        "Unfinished bucket key needs clean-up; revoke it to retry"


def _bucket_create(c: _Ctx) -> str:
    if c.d.get("adopted") is True:
        return "Existing bucket added to Bucketeer"
    lock = "object lock" if c.d.get("objectLock") is True else None
    ver = None if lock else {True: "versioning on", False: "versioning off"}.get(
        c.d.get("versioning"))
    return _with("Bucket created", lock, ver, _tib(c.d.get("quotaTiB")))


def _deleted(base: str) -> Callable[[_Ctx], str]:
    def fn(c: _Ctx) -> str:
        n, size = _num(c.d.get("objects")), _num(c.d.get("bytes"))
        if c.d.get("dataDeleted") is True and n:
            return _with(f"{base} with {_plural(n, 'object')}", _bytes(size) if size else None)
        return base
    return fn


def _delete_started(base: str) -> Callable[[_Ctx], str]:
    def fn(c: _Ctx) -> str:
        n = _num(c.d.get("objects"))
        return _with(base, f"{_plural(n, 'object')} to delete" if n else None)
    return fn


def _versioning(c: _Ctx) -> str:
    return {"enabled": "Versioning turned on", "suspended": "Versioning paused"}.get(
        c.d.get("after"), "Versioning changed")


def _budget(c: _Ctx) -> str:
    if not c.mine:
        return "Budget number changed"
    if c.d.get("new") is None:
        return "Budget number removed"
    return _change(_text(c.d.get("old")), _text(c.d.get("new")), "Budget number")


def _tib_change(c: _Ctx) -> str:
    return _change(_tib(c.d.get("oldTiB")), _tib(c.d.get("newTiB")), "Quota")


def _admin_bucket_quota(c: _Ctx) -> str:
    if c.d.get("location") == "cloud":
        return _tib_change(c)
    return _change(_quota(c.d.get("before")), _quota(c.d.get("after")), "Quota")


def _lifecycle(c: _Ctx) -> str:
    after = c.d.get("after")
    if not isinstance(after, dict):
        return "Lifecycle rules changed"
    parts = [f"{what} after {_plural(n, 'day')}" for key, what in (
        ("expireCurrentDays", "objects expire"), ("expireNoncurrentDays", "old versions expire"),
        ("abortMultipartDays", "unfinished uploads end"))
        if (n := _num(after.get(key)))]
    return f"Lifecycle: {'; '.join(parts)}" if parts else "Lifecycle rules removed"


def _counted(base: str, key: str, word: str, gone: str) -> Callable[[_Ctx], str]:
    def fn(c: _Ctx) -> str:
        n = _num(c.d.get(key))
        if n == 0:
            return gone
        return _with(base, _plural(n, word) if n else None)
    return fn


def _multipart(c: _Ctx) -> str:
    n, failed = _num(c.d.get("aborted")) or 0, _num(c.d.get("failed")) or 0
    text = f"{_plural(n, 'unfinished upload')} cleared"
    return text + (f", {failed:,} could not be cleared" if failed else "")


def _object_lock(c: _Ctx) -> str:
    after = c.d.get("after")
    if after is None:
        return "Default retention removed"
    if isinstance(after, dict):
        mode = {"GOVERNANCE": "governance", "COMPLIANCE": "compliance"}.get(after.get("mode"))
        days, years = _num(after.get("days")), _num(after.get("years"))
        span = _plural(days, "day") if days else _plural(years, "year") if years else None
        if mode and span:
            return f"Default retention set to {span}, {mode} mode"
    return "Default retention changed"


def _retention(c: _Ctx) -> str:
    text = "Object retention removed" if c.d.get("after") is None else "Object retention changed"
    return text + (" (governance bypassed)" if c.d.get("bypassGovernance") is True else "")


def _legal_hold(c: _Ctx) -> str:
    return {"ON": "Legal hold placed on an object", "OFF": "Legal hold lifted from an object"}.get(
        c.d.get("after"), "Legal hold changed")


def _share_link(c: _Ctx) -> str:
    span = _duration(c.d.get("expiresIn"))
    return f"Share link made, valid for {span}" if span else "Share link made"


def _recipient(c: _Ctx) -> str | None:
    # only in the person's own actions: whom they shared with is theirs to see
    return (_text(c.d.get("recipient")) or _text(c.d.get("uid"))) if c.mine else None


def _share(c: _Ctx) -> str:
    access = _word(c.d.get("access"), _ACCESS)
    if c.received:
        return f"Shared with you, {access}" if access else "Shared with you"
    who = _recipient(c)
    text = f"Shared with {who}" if who else "Bucket shared"
    text = f"{text}, {access}" if access else text
    return text + ("; their S3 account was created" if c.d.get("accountCreated") is True else "")


def _share_update(c: _Ctx) -> str:
    after = _word(c.d.get("after"), _ACCESS)
    if c.received:
        return f"Your access changed to {after}" if after else "Your access changed"
    who = _recipient(c)
    base = f"Access for {who}" if who else "Share access"
    return f"{base} changed to {after}" if after else f"{base} changed"


def _unshare(c: _Ctx) -> str:
    why = _word(c.d.get("reason"), _UNSHARE)
    if c.received:
        base = "Your access was removed"
    else:
        who = _recipient(c)
        base = f"Stopped sharing with {who}" if who else "Share removed"
    return _with(base, why)


def _cloud_create(c: _Ctx) -> str:
    return _with("Cloud bucket created", _word(c.d.get("storageClass"), _CLASS),
                 _tib(c.d.get("quotaTiB")))


def _over_quota(c: _Ctx) -> str:
    quota = _tib(c.d.get("quotaTiB"))
    text = f"Over its {quota} quota" if quota else "Over its quota"
    return text + ("; you were emailed" if c.d.get("emailed") is True else "")


def _cloud_key(base: str) -> Callable[[_Ctx], str]:
    def fn(c: _Ctx) -> str:
        why = {"make_room": "to make room for a new one", "unusable": "it could not be used"}
        return _with(base, "for a setup command" if c.d.get("via") == "setup" else None,
                     _word(c.d.get("reason"), why))
    return fn


def _rgw_create(c: _Ctx) -> str:
    return "S3 account created" + ("; its quota is set at your next visit"
                                   if c.d.get("quota_applied") is False else "")


def _admin_quota(c: _Ctx) -> str:
    after = _quota(c.d.get("after"))
    return f"Account quota set to {after}" if after else "Account quota changed"


def _keys_note(c: _Ctx) -> str | None:
    n = len(c.d["keys"]) if isinstance(c.d.get("keys"), list) else 0
    return _plural(n, "key") if n else None


def _admin_suspend(c: _Ctx) -> str:
    return _with("Account suspended", _keys_note(c))


def _admin_unsuspend(c: _Ctx) -> str:
    return _with("Account reactivated", _keys_note(c))


def _offboarding_suspend(c: _Ctx) -> str:
    if c.d.get("keys_only") is True:
        return _with("Remaining keys suspended", _keys_note(c))
    why = _word(c.d.get("reason"), _OFFBOARDING)
    return f"Account suspended: {why}" if why else "Account suspended"


def _fixed(text: str) -> Callable[[_Ctx], str]:
    return lambda c: text


_SETUP_FAILED = "Your S3 account could not be set up; contact your administrator"

SUMMARIES: dict[str, Callable[[_Ctx], str]] = {
    "login.success": _login,
    "logout": _fixed("Signed out"),
    "profile.update": _profile,
    "credentials.reveal": _fixed("Secret key shown"),
    "credentials.key.create": _fixed("Access key created"),
    "credentials.key.rotate": _rotated("Access key rotated"),
    "credentials.key.rotate_failed": _fixed("Key rotation did not finish; the old key was kept"),
    "credentials.key.delete": _fixed("Access key deleted"),
    "credentials.setup_link": _setup_link,
    "credentials.setup": _setup,
    "credentials.setup_refused": _setup_refused,
    "subuser.create": _subuser_create,
    "subuser.create_failed": _fixed("Subuser could not be created"),
    "subuser.update": _subuser_update,
    "subuser.delete": _fixed("Subuser deleted"),
    "subuser.key.create": _subuser_key("Key created"),
    "subuser.key.rotate": _rotated("Subuser key rotated"),
    "subuser.key.rotate_failed": _fixed(
        "Subuser key rotation did not finish; the old key was kept"),
    "subuser.key.delete": _subuser_key("Key deleted"),
    "subuser.reveal": _subuser_key("Secret key shown"),
    "bucket_key.create": _bucket_key_create,
    "bucket_key.create_failed": _fixed("Bucket key could not be created"),
    "bucket_key.reveal": _fixed("Bucket key secret shown"),
    "bucket_key.revoke": _fixed("Bucket key revoked"),
    "bucket_key.revoke_failed": _fixed("Bucket key could not be revoked; ask an administrator"),
    "bucket_key.cleanup": _bucket_key_cleanup,
    "admin.bucket_key.revoke": _fixed("Bucket key revoked"),
    "admin.bucket_key.revoke_failed": _fixed("Bucket key revoke did not finish"),
    "bucket.create": _bucket_create,
    "bucket.create_failed": _fixed("Bucket creation did not finish; check your bucket list"),
    "bucket.delete": _deleted("Bucket deleted"),
    "bucket.delete_started": _delete_started("Bucket deletion started"),
    "bucket.delete_blocked": _fixed("Bucket deletion stopped: some objects are locked"),
    "bucket.delete_failed": _fixed("Bucket deletion did not finish"),
    "bucket.delete_retry": _fixed("Bucket deletion retried"),
    "bucket.delete_superseded": _fixed("An earlier deletion of this bucket was ended"),
    "bucket.versioning": _versioning,
    "bucket.versioning_failed": _fixed("Versioning change did not finish"),
    "bucket.budget": _budget,
    "bucket.quota": _tib_change,
    "bucket.tag_sync": _fixed("Bucket tags written"),
    "bucket.lifecycle": _lifecycle,
    "bucket.lifecycle_rules": _counted("Lifecycle rules changed", "rules_after", "rule",
                                       "Lifecycle rules removed"),
    "bucket.cors": _counted("CORS rules changed", "rules_after", "rule", "CORS rules removed"),
    "bucket.notifications": _counted("Notifications changed", "count_after", "notification",
                                     "Notifications removed"),
    "bucket.tags": _counted("Tags changed", "tags_after", "tag", "Tags removed"),
    "bucket.multipart_abort": _multipart,
    "bucket.object_lock": _object_lock,
    "object.retention": _retention,
    "object.legal_hold": _legal_hold,
    "bucket.policy": _counted("Bucket policy changed", "statements_after", "statement",
                              "Bucket policy emptied"),
    "bucket.policy_delete": _fixed("Bucket policy removed"),
    "bucket.share_link": _share_link,
    "bucket.acl": _fixed("Bucket permissions (ACL) changed"),
    "object.acl": _fixed("Object permissions (ACL) changed"),
    "bucket.share": _share,
    "bucket.share_update": _share_update,
    "bucket.unshare": _unshare,
    "cloud.bucket.share": _share,
    "cloud.bucket.share_update": _share_update,
    "cloud.bucket.unshare": _unshare,
    "admin.bucket.quota": _admin_bucket_quota,
    "cloud.bucket.create": _cloud_create,
    "cloud.bucket.create_failed": _fixed("Cloud bucket could not be created"),
    "cloud.bucket.quota": _tib_change,
    "cloud.bucket.budget": _budget,
    "cloud.bucket.delete": _deleted("Cloud bucket deleted"),
    "cloud.bucket.delete_started": _delete_started("Cloud bucket deletion started"),
    "cloud.bucket.delete_blocked": _fixed("Cloud bucket deletion stopped"),
    "cloud.bucket.delete_failed": _fixed("Cloud bucket deletion did not finish"),
    "cloud.bucket.delete_retry": _fixed("Cloud bucket deletion retried"),
    "cloud.bucket.over_quota": _over_quota,
    "cloud.user.create": _fixed("Cloud (AWS) account created"),
    "cloud.user.refused": _fixed("Cloud (AWS) account could not be set up; ask an administrator"),
    "cloud.key.create": _cloud_key("Cloud key created"),
    "cloud.key.rotate": _rotated("Cloud key rotated"),
    "cloud.key.delete": _cloud_key("Cloud key deleted"),
    "rgw.user.create": _rgw_create,
    "rgw.user.create_failed": _fixed(_SETUP_FAILED),
    "rgw.user.default_quota": _fixed("Account quota set"),
    "rgw.user.quota_unlimited": _fixed("Account-wide quota removed; bucket quotas apply"),
    "rgw.user.case_match": _fixed("Existing S3 account linked"),
    "rgw.user.invalid_username": _fixed(_SETUP_FAILED),
    "rgw.user.ambiguous_uid": _fixed(_SETUP_FAILED),
    "rgw.user.uid_taken": _fixed(_SETUP_FAILED),
    "rgw.user.mapping_target_missing": _fixed(_SETUP_FAILED),
    "admin.user.quota": _admin_quota,
    "admin.user.suspend": _admin_suspend,
    "admin.user.unsuspend": _admin_unsuspend,
    "offboarding.suspend": _offboarding_suspend,
    "offboarding.unsuspend": _fixed("Account reactivated when you signed in"),
    "offboarding.unsuspend_failed": _fixed(
        "Reactivation did not finish; it is retried at your next sign-in"),
}


def generic(action: str) -> str:
    """`admin.settings.uid_mapping.set` -> "Admin settings uid mapping set"."""
    words = " ".join(w.replace("_", " ") for w in action.split(".") if w)
    return (_text(words, 80) or "Activity").capitalize()


def summary(ctx: _Ctx) -> str:
    fn = SUMMARIES.get(ctx.action)
    if fn is not None:
        try:
            return fn(ctx)
        except Exception:  # odd details must never break the page
            log.warning("activity: no summary for %s (bad details)", ctx.action)
    return generic(ctx.action)


# -- scope -----------------------------------------------------------------------------


async def _cloud_keys(state: Any, db: Database, username: str) -> list[str]:
    cloud = getattr(state, "cloud", None)
    if cloud is None or not cloud.available:
        return []
    row = await db.run(db.cloud_accounts.get, username)
    if not row:
        return []
    try:
        return [k["accessKey"] for k in await cloud.list_access_keys(row["iam_user"])]
    except CloudError as e:
        log.info("activity: cloud keys of %s not read: %s", username, type(e).__name__)
        return []


def _cloud_prefix(settings: Any, username: str) -> str:
    """`<CLOUD_BUCKET_PREFIX><owner part>-` as `CloudAdmin.bucket_name` builds it."""
    if not getattr(settings, "cloud_configured", False):
        return ""
    slug = re.sub(r"[^a-z0-9-]", "-", username.lower()).strip("-")
    return f"{settings.cloud_bucket_prefix}{slug}-" if slug else ""


async def gather(request: Request, user: SessionUser, acct: RgwAccount) -> Scope:
    """The person's account, buckets and keys. Backend trouble narrows the scope (and sets
    `partial` for the account and bucket list); it never fails the request."""
    state = request.app.state
    db: Database = state.db
    uid = acct.uid if acct.resolved else None
    partial = acct.status == UNAVAILABLE
    labels = tuple((str(k["id"]), k["label"]) for k in await db.run(
        lambda: db.bucket_keys.list(user.username, include_revoked=True)) if k["label"])
    if not uid:
        return Scope(user.username, None, key_labels=labels, partial=partial)
    croit = state.croit
    try:
        listed = await croit.list_user_buckets(uid)
        buckets = [b["bucket"] for b in listed or []
                   if isinstance(b, dict) and b.get("owner") == uid
                   and isinstance(b.get("bucket"), str)]
    except CroitClientError as e:
        log.info("activity: buckets of %s not listed (%s); using the recorded ones",
                 uid, type(e).__name__)
        partial = True
        buckets = [r["name"] for r in await db.run(db.buckets.list, uid)]
    cloud = [r["name"] for r in await db.run(db.buckets.list_cloud, uid)]
    try:
        keys = [k["access_key"] for k in await croit.list_keys(uid) or []
                if isinstance(k, dict) and k.get("user") == uid
                and isinstance(k.get("access_key"), str)]
    except CroitClientError as e:
        log.info("activity: keys of %s not listed: %s", uid, type(e).__name__)
        keys = []
    keys += await _cloud_keys(state, db, user.username)
    return Scope(user.username, uid, tuple(buckets + cloud), frozenset(cloud), tuple(keys),
                 labels, _cloud_prefix(state.settings, user.username), partial)


# -- route -----------------------------------------------------------------------------


@router.get("/activity")
async def my_activity(
    request: Request,
    response: Response,
    user: Annotated[SessionUser, Depends(require_user)],
    acct: Annotated[RgwAccount, Depends(current_account)],
    limit: Annotated[int, Query(ge=1)] = DEFAULT_LIMIT,
    before_id: Annotated[int | None, Query(ge=1, le=2**62)] = None,
):
    response.headers["Cache-Control"] = "no-store"
    scope = await gather(request, user, acct)
    db: Database = request.app.state.db
    entries, nxt = await db.run(feed, db, scope, before_id, min(limit, MAX_LIMIT))
    return {"entries": entries, "nextCursor": nxt, "partial": scope.partial}
