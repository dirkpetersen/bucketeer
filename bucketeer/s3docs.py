"""Shared-document helpers for bucket policy, lifecycle and tagging.

Several features write the same per-bucket documents (sharing, bucket keys, simple
lifecycle rules, the Advanced raw editors). Everything here only ever adds, replaces
or removes entries whose `Sid` / `ID` / tag key starts with `bucketeer-`; every other
entry is preserved unchanged and in its original order. Always go through these
helpers; never call put_bucket_policy / put_bucket_lifecycle_configuration /
put_bucket_tagging directly.

Two layers:
1. Pure functions on parsed documents (dicts), no I/O. They never mutate their input.
   Functions that can empty a document return None, meaning "delete it" (S3 rejects
   an empty Statement / Rules / TagSet).
2. `update_bucket_policy` / `update_bucket_lifecycle` / `update_bucket_tagging`:
   read-modify-write with a caller-provided boto3 S3 client. They are synchronous;
   async callers run them with `asyncio.to_thread`.

Concurrency: S3 has no ETag or conditional write for these documents, so two
concurrent read-modify-writes can lose an update. Within this single process the
update_* functions take `bucket_lock(bucket)` (one re-entrant lock per bucket,
shared by all three documents), so concurrent calls serialize. Callers that do
several steps for one bucket (e.g. policy then DB bookkeeping) may hold the same
lock around them. Writers outside this process (a user with their own S3 client)
are not covered: a lost update is possible in that narrow window.

Share / bucket-key action sets (supported by Ceph RGW's IAM policy engine):
  read:      s3:GetObject, s3:GetObjectVersion, s3:ListBucket,
             s3:ListBucketVersions, s3:GetBucketLocation
  readwrite: read + s3:PutObject, s3:DeleteObject, s3:DeleteObjectVersion,
             s3:AbortMultipartUpload, s3:ListMultipartUploadParts,
             s3:ListBucketMultipartUploads
No bucket-admin actions (PutBucketPolicy, PutLifecycleConfiguration, DeleteBucket, ...).
Resources are `arn:aws:s3:::<bucket>` and `arn:aws:s3:::<bucket>/*`.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Callable, Iterable
from typing import Any

from botocore.exceptions import ClientError

PREFIX = "bucketeer-"
POLICY_VERSION = "2012-10-17"
MAX_DAYS = 36500  # 100 years; a typo guard, not a product limit

READ_ACTIONS = (
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:GetBucketLocation",
)
WRITE_ACTIONS = (
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
    "s3:ListBucketMultipartUploads",
)

# Characters we allow in the part after `bucketeer-` for Sids / IDs we build.
_SAFE_SUFFIX = re.compile(r"^[A-Za-z0-9._-]+$")
# Bucket-key users are `<owner>+<label>`: `+` (valid in IAM user names, absent from AD
# names) is allowed in a principal ARN for them only, never in a Sid or a human uid.
_SAFE_KEY_UID = re.compile(r"^[A-Za-z0-9._-]+\+[A-Za-z0-9-]+$")


class S3DocError(ValueError):
    """A document is malformed or a change would break the bucketeer-* rules."""


class DuplicateIdError(S3DocError):
    """Two entries in one document share a Sid / ID (S3 requires them unique)."""


class ForeignIdError(S3DocError):
    """A write through these helpers named an entry not starting with `bucketeer-`."""


def is_managed(identifier: Any) -> bool:
    return isinstance(identifier, str) and identifier.startswith(PREFIX)


# --------------------------------------------------------------------- generic

def _check_entries(entries: Any, what: str, id_key: str) -> list[dict]:
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise S3DocError(f"{what} must be a list of objects")
    seen: set[str] = set()
    for e in entries:
        ident = e.get(id_key)
        if ident is None:
            continue
        if not isinstance(ident, str):
            raise S3DocError(f"{id_key} {ident!r} in {what} must be a string")
        if ident in seen:
            raise DuplicateIdError(f"duplicate {id_key} {ident!r} in {what}")
        seen.add(ident)
    return entries


def _require_managed(entries: Iterable[dict], id_key: str) -> list[dict]:
    entries = list(entries)
    for e in entries:
        if not is_managed(e.get(id_key)):
            raise ForeignIdError(
                f"refusing to write {id_key} {e.get(id_key)!r}: must start with {PREFIX!r}")
    seen: set[str] = set()
    for e in entries:
        if e[id_key] in seen:
            raise DuplicateIdError(f"duplicate {id_key} {e[id_key]!r} in the new entries")
        seen.add(e[id_key])
    return entries


def _upsert(existing: list[dict], new: list[dict], id_key: str) -> list[dict]:
    """Replace entries with a matching id in place; append the others in order."""
    new_by_id = {e[id_key]: copy.deepcopy(e) for e in new}
    out: list[dict] = []
    for e in existing:
        ident = e.get(id_key)
        out.append(new_by_id.pop(ident) if ident in new_by_id else copy.deepcopy(e))
    out.extend(new_by_id.values())  # dicts keep insertion order
    return out


def _remove(existing: list[dict], doomed: Callable[[str], bool], id_key: str) -> list[dict]:
    return [copy.deepcopy(e) for e in existing
            if not (is_managed(e.get(id_key)) and doomed(e[id_key]))]


def _predicate(ids: Iterable[str] | Callable[[str], bool]) -> Callable[[str], bool]:
    if callable(ids):
        return ids
    wanted = {ids} if isinstance(ids, str) else set(ids)
    for i in wanted:
        if not is_managed(i):
            raise ForeignIdError(f"refusing to remove {i!r}: must start with {PREFIX!r}")
    return wanted.__contains__


# ---------------------------------------------------------------------- policy

def parse_policy(text: str | bytes | dict | None) -> dict | None:
    """Parse a policy; None / empty means 'no policy'. `Statement` is normalized to a list."""
    if text is None:
        return None
    if isinstance(text, (str, bytes)):
        if not text.strip():
            return None
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise S3DocError(f"bucket policy is not valid JSON: {exc}") from exc
    else:
        doc = copy.deepcopy(text)
    if not isinstance(doc, dict):
        raise S3DocError("bucket policy must be a JSON object")
    if not doc:
        return None
    stmts = doc.get("Statement", [])
    if isinstance(stmts, dict):
        stmts = [stmts]
    if stmts == []:
        return None  # S3 rejects a policy without statements
    doc["Statement"] = _check_entries(stmts, "Statement", "Sid")
    return doc


def serialize_policy(policy: dict) -> str:
    return json.dumps(policy, separators=(",", ":"))


def upsert_statements(policy: dict | str | None, statements: Iterable[dict]) -> dict | None:
    """Add or replace managed statements (by Sid); everything else is kept in order.

    Returns None only if the result has no statements at all (nothing to write).
    """
    new = _require_managed(statements, "Sid")
    # Version is set only on a brand-new policy; an existing document's Version (or its
    # absence, which AWS reads as 2008-10-17) is never touched.
    doc = parse_policy(policy) or {"Version": POLICY_VERSION, "Statement": []}
    doc["Statement"] = _upsert(doc["Statement"], new, "Sid")
    return doc if doc["Statement"] else None


def remove_statements(policy: dict | str | None,
                      sids: Iterable[str] | str | Callable[[str], bool]) -> dict | None:
    """Remove managed statements by Sid list or predicate (never foreign ones).

    Returns None when no statements remain: the policy must then be deleted.
    """
    doc = parse_policy(policy)
    if doc is None:
        return None
    doc["Statement"] = _remove(doc["Statement"], _predicate(sids), "Sid")
    return doc if doc["Statement"] else None


def managed_statements(policy: dict | str | None) -> list[dict]:
    doc = parse_policy(policy)
    return [s for s in doc["Statement"] if is_managed(s.get("Sid"))] if doc else []


_TENANT = re.compile(r"^[A-Za-z0-9_]*$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _principal_arn(tenant: str | None, uid: str, key_user: bool = False) -> str:
    tenant = tenant or ""
    if not isinstance(tenant, str) or not _TENANT.match(tenant):
        raise S3DocError(f"invalid tenant {tenant!r}")
    ok = _SAFE_KEY_UID if key_user else _SAFE_SUFFIX
    if not isinstance(uid, str) or not ok.match(uid):
        raise S3DocError(f"invalid uid {uid!r}")
    return f"arn:aws:iam::{tenant}:user/{uid}"


def _grant(sid: str, bucket: str, uid: str, tenant: str | None, access: str,
           key_user: bool = False) -> dict:
    if access not in ("read", "readwrite"):
        raise S3DocError(f"access must be 'read' or 'readwrite', not {access!r}")
    if not isinstance(bucket, str) or not _BUCKET.match(bucket):
        raise S3DocError(f"invalid bucket name {bucket!r}")
    actions = list(READ_ACTIONS) + (list(WRITE_ACTIONS) if access == "readwrite" else [])
    return {
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"AWS": _principal_arn(tenant, uid, key_user)},
        "Action": actions,
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
    }


def _suffix(value: Any, what: str) -> str:
    value = str(value)
    if not _SAFE_SUFFIX.match(value):
        raise S3DocError(f"{what} {value!r} may only contain letters, digits, '.', '_' and '-'")
    return value


def share_sid(principal_uid: str, tenant: str | None = None) -> str:
    """Sid of a share: `bucketeer-share-u-<uid>` or `bucketeer-share-t-<tenant>-<uid>`.

    Unambiguous: the `u-` / `t-` marker separates the two forms, and a tenant never
    contains '-' (it is [A-Za-z0-9_]), so the first '-' after `t-` ends the tenant even
    though a uid may contain '.', '_' and '-'. Distinct (tenant, uid) pairs therefore
    never share a Sid.
    """
    _principal_arn(tenant, principal_uid)  # validates both
    if tenant:
        return f"{PREFIX}share-t-{tenant}-{principal_uid}"
    return f"{PREFIX}share-u-{principal_uid}"


def share_statement(bucket: str, principal_uid: str, tenant: str | None, access: str) -> dict:
    sid = share_sid(principal_uid, tenant)
    return _grant(sid, bucket, principal_uid, tenant, access)


def bucket_key_statement(bucket: str, key_uid: str, tenant: str | None, access: str,
                         key_id: str | int) -> dict:
    sid = PREFIX + "key-" + _suffix(key_id, "key id")
    return _grant(sid, bucket, key_uid, tenant, access, key_user=True)


# ------------------------------------------------------------------- lifecycle

def parse_lifecycle(doc: dict | str | bytes | None) -> dict | None:
    """Parse a lifecycle configuration; None / empty / no rules means 'none'."""
    if doc is None:
        return None
    if isinstance(doc, (str, bytes)):
        if not doc.strip():
            return None
        try:
            doc = json.loads(doc)
        except json.JSONDecodeError as exc:
            raise S3DocError(f"lifecycle configuration is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise S3DocError("lifecycle configuration must be a JSON object")
    # Only Rules: boto3's GET also returns ResponseMetadata and (newer S3) a
    # TransitionDefaultMinimumObjectSize that put_bucket_lifecycle_configuration
    # takes as a separate parameter, not inside LifecycleConfiguration.
    if not doc.get("Rules"):
        return None
    return {"Rules": _check_entries(copy.deepcopy(doc["Rules"]), "Rules", "ID")}


def upsert_rules(config: dict | None, rules: Iterable[dict]) -> dict | None:
    new = _require_managed(rules, "ID")
    doc = parse_lifecycle(config) or {"Rules": []}
    doc["Rules"] = _upsert(doc["Rules"], new, "ID")
    return doc if doc["Rules"] else None


def remove_rules(config: dict | None,
                 ids: Iterable[str] | str | Callable[[str], bool]) -> dict | None:
    """Remove managed rules; returns None when no rules remain (delete the config)."""
    doc = parse_lifecycle(config)
    if doc is None:
        return None
    doc["Rules"] = _remove(doc["Rules"], _predicate(ids), "ID")
    return doc if doc["Rules"] else None


def _days(n: Any) -> int:
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_DAYS:
        raise S3DocError(f"days must be an integer from 1 to {MAX_DAYS}, not {n!r}")
    return n


def _simple_rule(rule_id: str, **action: Any) -> dict:
    return {"ID": rule_id, "Status": "Enabled", "Filter": {"Prefix": ""}, **action}


EXPIRE_CURRENT_ID = PREFIX + "expire-current"
EXPIRE_NONCURRENT_ID = PREFIX + "expire-noncurrent"
ABORT_MULTIPART_ID = PREFIX + "abort-multipart"


def expire_current_rule(days: int) -> dict:
    return _simple_rule(EXPIRE_CURRENT_ID, Expiration={"Days": _days(days)})


def expire_noncurrent_rule(days: int) -> dict:
    return _simple_rule(EXPIRE_NONCURRENT_ID,
                        NoncurrentVersionExpiration={"NoncurrentDays": _days(days)})


def abort_multipart_rule(days: int) -> dict:
    return _simple_rule(ABORT_MULTIPART_ID,
                        AbortIncompleteMultipartUpload={"DaysAfterInitiation": _days(days)})


# ------------------------------------------------------------------------ tags

def parse_tags(tags: Iterable[dict] | None) -> list[dict] | None:
    """A TagSet as a list of {Key, Value}; None / empty means 'no tags'. Keys are unique."""
    if not tags:
        return None
    tags = [copy.deepcopy(t) for t in tags]
    if not all(isinstance(t, dict) and isinstance(t.get("Key"), str) for t in tags):
        raise S3DocError("TagSet must be a list of objects with a Key")
    _check_entries(tags, "TagSet", "Key")
    return tags


def upsert_tags(tags: Iterable[dict] | None, new: Iterable[dict]) -> list[dict] | None:
    new = _require_managed(new, "Key")
    return _upsert(parse_tags(tags) or [], new, "Key") or None


def remove_tags(tags: Iterable[dict] | None,
                keys: Iterable[str] | str | Callable[[str], bool]) -> list[dict] | None:
    """Remove managed tags; returns None when no tags remain (delete the tag set)."""
    current = parse_tags(tags)
    if current is None:
        return None
    return _remove(current, _predicate(keys), "Key") or None


# -------------------------------------------------------------------- I/O layer

def _foreign(entries: Iterable[dict], id_key: str) -> list[dict]:
    return [e for e in entries if not is_managed(e.get(id_key))]


def _guard(current: list[dict], new: list[dict], id_key: str, what: str) -> None:
    if _foreign(current, id_key) != _foreign(new, id_key):
        raise ForeignIdError(
            f"mutation changed non-{PREFIX}* {what} entries; pass allow_foreign=True "
            "only from the raw editor")


def _guard_policy(current: dict | None, new: dict | None) -> None:
    _guard(current["Statement"] if current else [], new["Statement"] if new else [],
           "Sid", "policy")
    if current and new:
        keep = {k: v for k, v in current.items() if k != "Statement"}
        if keep != {k: v for k, v in new.items() if k != "Statement"}:
            raise ForeignIdError("mutation changed top-level policy fields")


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def bucket_lock(bucket: str) -> threading.RLock:
    """The per-bucket lock for read-modify-write of policy / lifecycle / tagging.

    Re-entrant; use as `with bucket_lock(name): ...`. Held internally by the update_*
    functions below.
    """
    with _locks_guard:
        return _locks.setdefault(bucket, threading.RLock())


def _missing(exc: ClientError, *codes: str) -> bool:
    return exc.response.get("Error", {}).get("Code") in codes


def update_bucket_policy(s3: Any, bucket: str,
                         mutate: Callable[[dict | None], dict | None],
                         *, allow_foreign: bool = False) -> dict | None:
    """Read, mutate and write back the bucket policy. Returns the policy now in force.

    `mutate` gets the parsed policy (None if there is none) and returns the new one;
    None (or no statements) deletes the policy. Nothing is written when unchanged.
    Unless `allow_foreign` (the Advanced raw editor only), a result that alters any
    non-`bucketeer-*` statement or top-level field raises ForeignIdError and writes
    nothing; so a callback that forgets to `return` cannot wipe the policy.
    """
    with bucket_lock(bucket):
        try:
            current = parse_policy(s3.get_bucket_policy(Bucket=bucket)["Policy"])
        except ClientError as exc:
            if not _missing(exc, "NoSuchBucketPolicy"):
                raise
            current = None
        new = mutate(copy.deepcopy(current))
        new = parse_policy(new) if new is not None else None
        if not allow_foreign:
            _guard_policy(current, new)
        if new == current:
            return current
        if new is None:
            s3.delete_bucket_policy(Bucket=bucket)
        else:
            s3.put_bucket_policy(Bucket=bucket, Policy=serialize_policy(new))
        return new


def update_bucket_lifecycle(s3: Any, bucket: str,
                            mutate: Callable[[dict | None], dict | None],
                            *, allow_foreign: bool = False) -> dict | None:
    """Read-modify-write the lifecycle configuration; same contract as update_bucket_policy."""
    with bucket_lock(bucket):
        try:
            current = parse_lifecycle(s3.get_bucket_lifecycle_configuration(Bucket=bucket))
        except ClientError as exc:
            if not _missing(exc, "NoSuchLifecycleConfiguration"):
                raise
            current = None
        new = mutate(copy.deepcopy(current))
        new = parse_lifecycle(new) if new is not None else None
        if not allow_foreign:
            _guard(current["Rules"] if current else [], new["Rules"] if new else [],
                   "ID", "lifecycle")
        if new == current:
            return current
        if new is None:
            s3.delete_bucket_lifecycle(Bucket=bucket)
        else:
            s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=new)
        return new


def update_bucket_tagging(s3: Any, bucket: str,
                          mutate: Callable[[list[dict] | None], list[dict] | None],
                          *, allow_foreign: bool = False) -> list[dict] | None:
    """Read-modify-write the bucket TagSet; same contract as update_bucket_policy."""
    with bucket_lock(bucket):
        try:
            current = parse_tags(s3.get_bucket_tagging(Bucket=bucket)["TagSet"])
        except ClientError as exc:
            if not _missing(exc, "NoSuchTagSet"):
                raise
            current = None
        new = mutate(copy.deepcopy(current))
        new = parse_tags(new) if new is not None else None
        if not allow_foreign:
            _guard(current or [], new or [], "Key", "tag")
        if new == current:
            return current
        if new is None:
            s3.delete_bucket_tagging(Bucket=bucket)
        else:
            s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": new})
        return new
