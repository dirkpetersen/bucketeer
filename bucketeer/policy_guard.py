"""Pure checks for the raw policy editor: public and self-lockout statements (Q12).

The owner decided that Bucketeer's editor refuses to ADD or ALTER a statement that makes a
bucket public or that locks its owner out. A statement already stored that way may be kept
exactly as stored or removed. Users can still do either with the AWS CLI and their own keys;
this is a guard rail, not a security boundary. The server is authoritative; the browser
(`static/js/components/policy-dialog.js`) mirrors these rules only to warn early.

Definitions (conditions never change the verdict: a `Condition` can be bypassed or
mis-written, so a statement with one is judged as if it had none):

- tenant principal (only under `AWS`, or a plain-string/list Principal; never `Service`,
  `Federated`, `CanonicalUser`): RGW reads `arn:aws:iam::<t>:root`, and any plain string
  without `:` or
  `/` (including `""`), as the whole TENANT `<t>` (the plain string is the tenant name). It
  matches every user of that tenant. Anonymous requests run as user `anonymous` in the EMPTY
  tenant, so `arn:aws:iam:::root` and `""` also cover anonymous callers.
- public: Effect `Allow` with a principal that covers everyone or a whole tenant: `"*"`
  (also `{"AWS": "*"}`, in a list, or under another principal type) or ANY tenant principal
  (a tenant grant is not a named user). `NotPrincipal` with `Allow` is public too (it
  grants everyone except the named principals). `arn:aws:iam::*:root`, `arn:aws:iam::*:user/*`
  and a space-padded `*` are not wildcards in RGW; they are refused defensively anyway.
  Not covered: partial wildcards such as `arn:aws:iam::acme:user/*` (named users only).
- lockout: Effect `Deny`, a principal that covers the owner, an action that covers any of
  `s3:*`/`*`/`s3:PutBucketPolicy`/`s3:GetBucketPolicy`/`s3:DeleteBucketPolicy`, and a
  resource that covers the bucket ARN. Owner covered: `*` forms, the owner's ARN
  `arn:aws:iam::<tenant>:user/<uid>` (also by glob), or the owner's own tenant principal
  (`arn:aws:iam::<tenant>:root` or the bare tenant name; `""` for users without a tenant);
  `NotPrincipal` covers the owner unless it names them. `NotAction` covers unless it
  excludes all three policy actions; `NotResource` covers unless it excludes the bucket.
  Actions match case-insensitively with `*` and `?` globs. Bucket ARNs checked:
  `arn:aws:s3:::<bucket>` and, for tenant users, `arn:aws:s3::<tenant>:<bucket>`.
"""

import json
import re
from typing import Any

from . import s3docs

POLICY_ACTIONS = ("s3:putbucketpolicy", "s3:getbucketpolicy", "s3:deletebucketpolicy")
_PUBLIC_ARNS = {"arn:aws:iam::*:root", "arn:aws:iam::*:user/*"}


def _glob(pattern: str, text: str, fold: bool = False) -> bool:
    """IAM-style wildcard match: `*` any run, `?` one character, nothing else is special."""
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, text, re.DOTALL | (re.IGNORECASE if fold else 0)) is not None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()]
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str)]
    return []


def principal_values(principal: Any) -> list[tuple[str, str]]:
    """`(type, value)` for every string in a Principal/NotPrincipal (`"*"`, a list, or
    `{type: str|list}`). A plain string or list is an `AWS` principal."""
    if isinstance(principal, dict):
        return [(str(k), v) for k, val in principal.items() for v in _strings(val)]
    return [("AWS", v) for v in _strings(principal)]


def _public_pair(kind: str, v: str) -> bool:
    """Everyone (any principal type) or, for `AWS` values only, a whole tenant."""
    return _is_public_value(v) or (kind == "AWS" and tenant_of(v) is not None)


_TENANT_ROOT = re.compile(r"arn:aws:iam::([^:/]*):root")


def tenant_of(v: str) -> str | None:
    """Tenant named by a principal string as RGW reads it (see module docstring), else None."""
    if v == "*":
        return None
    m = _TENANT_ROOT.fullmatch(v)
    if m:
        return m.group(1)
    return v if (":" not in v and "/" not in v) else None


def _is_public_value(v: str) -> bool:
    return v == "*" or v in _PUBLIC_ARNS


def owner_arn(tenant: str, bare_uid: str) -> str:
    return s3docs._principal_arn(tenant or None, bare_uid)  # noqa: SLF001 (the one ARN builder)


def is_public(stmt: dict) -> bool:
    """Allow to everyone (see the module docstring)."""
    if stmt.get("Effect") != "Allow":
        return False
    if "NotPrincipal" in stmt:
        return True
    return any(_public_pair(k, v) for k, v in principal_values(stmt.get("Principal")))


def _covers_owner(stmt: dict, tenant: str, bare_uid: str) -> bool:
    arn = owner_arn(tenant, bare_uid)

    def names_owner(pair: tuple[str, str]) -> bool:
        kind, v = pair
        return (_is_public_value(v) or _glob(v, arn)
                or (kind == "AWS" and tenant_of(v) == tenant))

    if "NotPrincipal" in stmt:
        return not any(names_owner(v) for v in principal_values(stmt["NotPrincipal"]))
    return any(names_owner(v) for v in principal_values(stmt.get("Principal")))


def _covers_actions(stmt: dict) -> bool:
    if "NotAction" in stmt:
        pats = [a.lower() for a in _strings(stmt["NotAction"])]
        return not all(any(_glob(p, a, True) for p in pats) for a in POLICY_ACTIONS)
    pats = [a.lower() for a in _strings(stmt.get("Action"))]
    return any(_glob(p, a, True) for p in pats for a in POLICY_ACTIONS)


def bucket_arns(bucket: str, tenant: str) -> list[str]:
    return [f"arn:aws:s3:::{bucket}"] + ([f"arn:aws:s3::{tenant}:{bucket}"] if tenant else [])


def _covers_bucket(stmt: dict, bucket: str, tenant: str) -> bool:
    arns = bucket_arns(bucket, tenant)
    if "NotResource" in stmt:
        pats = _strings(stmt["NotResource"])
        return not any(_glob(p, a) for p in pats for a in arns)
    return any(_glob(p, a) for p in _strings(stmt.get("Resource")) for a in arns)


def is_lockout(stmt: dict, bucket: str, tenant: str, bare_uid: str) -> bool:
    """Deny that would take the bucket's owner away from the bucket or its policy."""
    return (stmt.get("Effect") == "Deny" and _covers_owner(stmt, tenant, bare_uid)
            and _covers_actions(stmt) and _covers_bucket(stmt, bucket, tenant))


MESSAGES = {
    "public": ("would make the bucket public: it allows everyone, not just named users. "
               "A Condition does not change that. Bucketeer does not support public buckets."),
    "lockout": ("would lock you out of this bucket or its policy: it denies you (or "
                "everyone) the bucket policy actions or all S3 actions. A Condition does "
                "not change that."),
}


def classify(stmt: Any, bucket: str, tenant: str, bare_uid: str) -> str | None:
    """`"public"`, `"lockout"` or None for one statement dict."""
    if not isinstance(stmt, dict):
        return None
    if is_public(stmt):
        return "public"
    if is_lockout(stmt, bucket, tenant, bare_uid):
        return "lockout"
    return None


def _canon(stmt: Any) -> str:
    return json.dumps(stmt, sort_keys=True, separators=(",", ":"))


def flags(policy: dict | None, bucket: str, tenant: str, bare_uid: str) -> list[dict]:
    """`[{index, sid, kind}]` for every public/lockout statement of a policy."""
    out = []
    for i, s in enumerate((policy or {}).get("Statement", [])):
        kind = classify(s, bucket, tenant, bare_uid)
        if kind:
            out.append({"index": i, "sid": s.get("Sid"), "kind": kind})
    return out


def refusals(current: dict | None, new: dict | None, bucket: str, tenant: str,
             bare_uid: str) -> list[dict]:
    """Errors for statements of `new` that are public/lockout and not stored verbatim in
    `current` (whole-statement canonical JSON equality; removing is always fine)."""
    stored = {_canon(s) for s in (current or {}).get("Statement", [])}
    errors = []
    for f in flags(new, bucket, tenant, bare_uid):
        stmt = new["Statement"][f["index"]]
        if _canon(stmt) in stored:
            continue
        who = f"Statement {f['index'] + 1}" + (f" ({f['sid']})" if f["sid"] else "")
        errors.append({"statement": f["index"], "sid": f["sid"], "kind": f["kind"],
                       "message": f"{who} {MESSAGES[f['kind']]} "
                                  "A statement like this that is already stored may only be "
                                  "kept as it is or removed."})
    return errors
