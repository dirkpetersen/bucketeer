"""Bucket notifications under /api/buckets/{name}/notifications (Advanced UI only).

RGW sends bucket events to a broker through an SNS-style TOPIC; the bucket's notification
configuration points at the topic ARN. Users never type addresses: they pick a NAME from the
administrator's allowlist (`notification_endpoints` setting, no credentials in v1) and Bucketeer
builds the topic itself.

Topic design: RGW topics are namespaced per TENANT, not per user, so the topic name carries the
user: `bucketeer-<enc(endpoint)>-<enc(bare uid)>` (`topic_name`). `enc` keeps [A-Za-z0-9] and
writes every other character as `_xx` (two hex digits), so it never produces `-` and the first
`-` after the prefix separates the two parts: injective. A component whose encoding exceeds 118
characters becomes `_h` + 40 hex of its SHA-256 (`_h` cannot occur in an escaped name), keeping
the whole name within 247 characters (Kafka allows 249, RGW 256). For each endpoint a
notification uses, Bucketeer calls CreateTopic with the USER'S OWN key
(`S3Clients.call_with_sns`). The name is only a
convention that keeps users apart: on RGW versions with topic ownership (Squid and later) RGW
also refuses a second owner; on older versions RGW has NO topic ownership and any user holding
their own keys can rewrite any topic in the tenant (cluster check, see TODO.md). Attributes come
ONLY from the allowlist entry (`topic_attributes`): `push-endpoint` = url; `verify-ssl` for
https/amqps; `amqp-exchange` = the entry's topic (required for amqp). A Kafka topic is the RGW
topic name itself (`bucketeer-<endpoint>-<user>`; the broker must auto-create topics), so Kafka
entries have no `topic` field. CreateTopic is called on every save that changes something, which
re-syncs the attributes from the CURRENT allowlist entry. Until a user saves, topics keep the old
attributes after an admin edits or deletes an endpoint. Orphan topics are left in place.

- `GET` -> `{notifications: [{id, endpoint (allowlist name|null), managed, kind
  (topic|queue|lambda), topic (name), events, prefix|null, suffix|null}], endpoints:
  [{name, type}], etag}`. A notification is `managed` ONLY when its topic ARN is exactly the session
  user's own topic for a CURRENT allowlist entry (`arn:aws:sns:<zonegroup>:<tenant>:<topic_name>`,
  tenant = the user's tenant) and it uses only prefix/suffix filters. Everything else (other
  users' bucketeer-* topics, other tenants, other topics, queues, Lambda, removed endpoints) is
  external: shown read-only, kept verbatim, removable.
- `PUT {notifications: [...], etag}`: managed items `{id, endpoint, events, prefix?, suffix?}`;
  an external item is kept by sending `{id, external: true}` (an id that is not an external one
  in the current configuration -> 422); omitting it removes it. Events: s3:ObjectCreated:*|Put|
  Post|Copy|CompleteMultipartUpload and s3:ObjectRemoved:*|Delete|DeleteMarkerCreated (an event
  already stored on that id is accepted unchanged); not `*` together with a sub-event of the same
  family. ids unique, at most 64 characters; prefix/suffix at most 1024 bytes, no control
  characters; at most 100. Failures -> 422 `{detail, errors: [{item, field, message}]}`.
- Same shape back plus `changed`. `etag` (SHA-256 of the current configuration) is re-checked
  INSIDE `s3docs.bucket_lock`: 409 `{etagMismatch: true, current}`.
- Audit `bucket.notifications` (before/after summaries by endpoint NAME, never addresses).
"""

import hashlib
import json
import logging
import re
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import s3docs
from .auth import SessionUser, require_user
from .buckets import _audit, _full, _own_buckets, _own_name
from .policy_settings import endpoint_choices, load_endpoints
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.notifications")

router = APIRouter(prefix="/buckets", tags=["notifications"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]
User = Annotated[SessionUser, Depends(require_user)]

MAX_ITEMS = 100
MAX_ID = 64
MAX_FILTER = 1024
AUDIT_MAX = 8192
EVENTS = (
    "s3:ObjectCreated:*", "s3:ObjectCreated:Put", "s3:ObjectCreated:Post",
    "s3:ObjectCreated:Copy", "s3:ObjectCreated:CompleteMultipartUpload",
    "s3:ObjectRemoved:*", "s3:ObjectRemoved:Delete", "s3:ObjectRemoved:DeleteMarkerCreated",
)
FAMILIES = ("s3:ObjectCreated:", "s3:ObjectRemoved:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
KINDS = {"topic": ("TopicConfigurations", "TopicArn"),
         "queue": ("QueueConfigurations", "QueueArn"),
         "lambda": ("LambdaFunctionConfigurations", "LambdaFunctionArn")}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notifications: list[dict]
    etag: str


class EtagMismatch(Exception):
    def __init__(self, current: list[dict], endpoints: list[dict]):  # entries
        self.current, self.endpoints = current, endpoints


class Invalid(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors


def _enc(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]", lambda m: f"_{ord(m[0]):02x}", value)
    if len(out) > 118:
        out = "_h" + hashlib.sha256(value.encode()).hexdigest()[:40]
    return out


def split_uid(uid: str) -> tuple[str, str]:
    """(tenant, bare uid) of an RGW uid `tenant$uid` or `uid`."""
    tenant, sep, bare = uid.rpartition("$")
    return (tenant, bare) if sep else ("", uid)


def topic_name(uid: str, endpoint_name: str) -> str:
    """Per user and endpoint, injective (see the module docstring)."""
    return f"bucketeer-{_enc(endpoint_name)}-{_enc(split_uid(uid)[1])}"


def _arn_tenant_ok(arn_tenant: str, tenant: str) -> bool:
    return arn_tenant == tenant


def _own_topic(arn: str, uid: str, endpoints: list[dict]) -> str | None:
    """The endpoint NAME when `arn` is exactly this user's topic for a current endpoint."""
    parts = arn.split(":")
    if len(parts) != 6 or parts[:3] != ["arn", "aws", "sns"]:
        return None
    if not _arn_tenant_ok(parts[4], split_uid(uid)[0]):
        return None
    for e in endpoints:
        if parts[5] == topic_name(uid, e["name"]):
            return e["name"]
    return None


def topic_attributes(ep: dict) -> dict[str, str]:
    """The ONLY source of topic attributes: the allowlist entry."""
    attrs = {"push-endpoint": ep["url"]}
    if ep["type"] == "amqp":
        if not ep.get("topic"):
            raise Invalid([{"item": None, "field": "endpoint", "message": (
                f"The endpoint {ep['name']!r} has no AMQP exchange yet. Ask an administrator.")}])
        attrs["amqp-exchange"] = ep["topic"]
    if ep["url"].startswith(("https://", "amqps://")):
        attrs["verify-ssl"] = "true" if ep.get("verifyTls", True) else "false"
    return attrs


def etag_of(entries: list[dict]) -> str:
    return hashlib.sha256(json.dumps(
        [{"v": e["view"], "raw": e["raw"]} for e in entries], sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _filters(raw: dict) -> tuple[str | None, str | None, bool]:
    prefix = suffix = None
    plain = True
    for r in (raw.get("Filter", {}).get("Key", {}) or {}).get("FilterRules", []):
        name = str(r.get("Name", "")).lower()
        if name == "prefix":
            prefix = r.get("Value") or None
        elif name == "suffix":
            suffix = r.get("Value") or None
        else:
            plain = False
    other = set(raw.get("Filter", {})) - {"Key"}
    return prefix, suffix, plain and not other


def parse(config: dict, endpoints: list[dict], uid: str) -> list[dict]:
    """Entries `{view, raw, kind}` from a get_bucket_notification_configuration answer."""
    out, seen = [], set()
    for kind, (key, arn_key) in KINDS.items():
        for raw in config.get(key, []) or []:
            arn = raw.get(arn_key, "")
            last = arn.rsplit(":", 1)[-1]
            prefix, suffix, plain = _filters(raw)
            nid = raw.get("Id") or f"unnamed-{len(out) + 1}"
            while nid in seen:
                nid += "#"
            seen.add(nid)
            ep = _own_topic(arn, uid, endpoints) if kind == "topic" else None
            out.append({"kind": kind, "raw": raw, "view": {
                "id": nid, "endpoint": ep if plain else None, "managed": bool(ep and plain),
                "kind": kind, "topic": last, "events": list(raw.get("Events", [])),
                "prefix": prefix, "suffix": suffix}})
    return out


def _read(s3, bucket: str, endpoints: list[dict], uid: str) -> list[dict]:
    try:
        return parse(s3.get_bucket_notification_configuration(Bucket=bucket), endpoints, uid)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in (
                "NoSuchNotificationConfiguration", "NoSuchBucketNotification"):
            raise
        return []


def view(entries: list[dict], endpoints: list[dict]) -> dict[str, Any]:
    return {"notifications": [e["view"] for e in entries], "endpoints": endpoint_choices(endpoints),
            "etag": etag_of(entries)}


def normalize(raw: list[dict], current: list[dict], endpoints: list[dict]) -> list[dict]:
    """Validate submitted items. Returns `[{external: entry}|{view...}]`; raises Invalid."""
    errs: list[dict] = []
    if len(raw) > MAX_ITEMS:
        raise Invalid([{"item": None, "field": "notifications",
                        "message": f"At most {MAX_ITEMS} notifications are allowed."}])
    names = {e["name"].lower(): e["name"] for e in endpoints}
    stored = {e["view"]["id"]: e for e in current}
    out, ids = [], set()
    for i, r in enumerate(raw, 1):
        def err(field, message, i=i):
            errs.append({"item": i, "field": field, "message": message})
        extra = sorted(set(r) - {"id", "endpoint", "events", "prefix", "suffix", "external"})
        if extra:
            err("item", f"Unknown field {extra[0]!r}.")
        nid = r.get("id")
        if not isinstance(nid, str) or not nid or len(nid) > MAX_ID or _CONTROL.search(nid):
            err("id", f"The name must be 1 to {MAX_ID} characters without control characters.")
            continue
        if nid in ids:
            err("id", f"The name {nid!r} is used twice.")
            continue
        ids.add(nid)
        old = stored.get(nid)
        if r.get("external"):
            if set(r) - {"id", "external"} or not old or old["view"]["managed"]:
                err("id", "That is not an existing external notification.")
            else:
                out.append({"external": old})
            continue
        ep = names.get(str(r.get("endpoint", "")).lower()) if isinstance(
            r.get("endpoint"), str) else None
        if not ep:
            err("endpoint", "Choose one of the endpoints your administrator has set up.")
        events = r.get("events")
        if (not isinstance(events, list) or not events
                or not all(isinstance(e, str) for e in events)):
            err("events", "Choose at least one event.")
            events = []
        allowed = set(EVENTS) | (set(old["view"]["events"]) if old else set())
        bad = [e for e in events if e not in allowed]
        if bad:
            err("events", f"{bad[0]!r} is not an event you can choose here.")
        uniq = [e for e in dict.fromkeys(events)]
        for fam in FAMILIES:
            if fam + "*" in uniq and any(e.startswith(fam) and e != fam + "*" for e in uniq):
                err("events", f"Choose either all {fam[3:-1]} events or specific ones, not both.")
        filt = {}
        for fld in ("prefix", "suffix"):
            v = r.get(fld)
            if v in (None, ""):
                filt[fld] = None
            elif (not isinstance(v, str) or _CONTROL.search(v)
                  or len(v.encode()) > MAX_FILTER):
                err(fld, f"The {fld} must be text of at most {MAX_FILTER} bytes without "
                         "control characters.")
                filt[fld] = None
            else:
                filt[fld] = v
        if ep and not any(e["field"] for e in errs if e["item"] == i):
            order = {e: n for n, e in enumerate(EVENTS)}
            uniq.sort(key=lambda e: order.get(e, len(order)))
            out.append({"id": nid, "endpoint": ep, "events": uniq, **filt})
    if errs:
        raise Invalid(errs)
    return out


def _s3_config(item: dict, arn: str | None = None) -> dict:
    cfg: dict[str, Any] = {"Id": item["id"], "TopicArn": arn, "Events": item["events"]}
    rules = [{"Name": n, "Value": item[f]} for n, f in (("prefix", "prefix"), ("suffix", "suffix"))
             if item.get(f)]
    if rules:
        cfg["Filter"] = {"Key": {"FilterRules": rules}}
    return cfg


def _apply(s3, sns, bucket: str, items: list[dict], etag: str, endpoints: list[dict],
           uid: str) -> dict:
    by_name = {e["name"]: e for e in endpoints}
    with s3docs.bucket_lock(bucket):
        current = _read(s3, bucket, endpoints, uid)
        if etag_of(current) != etag:
            raise EtagMismatch(current, endpoints)
        normed = normalize(items, current, endpoints)
        desired = []
        for n in normed:
            if "external" in n:
                desired.append(n["external"]["view"])
            else:
                desired.append({"id": n["id"], "endpoint": n["endpoint"], "managed": True,
                                "kind": "topic", "topic": topic_name(uid, n["endpoint"]),
                                "events": n["events"], "prefix": n["prefix"],
                                "suffix": n["suffix"]})
        if desired == [e["view"] for e in current]:
            return {"before": current, "after": current}
        arns: dict[str, str] = {}
        for n in normed:  # (re)sync each used topic from the allowlist, with the user's own key
            if "external" not in n and n["endpoint"] not in arns:
                ep = by_name[n["endpoint"]]
                arns[n["endpoint"]] = sns.create_topic(
                    Name=topic_name(uid, ep["name"]), Attributes=topic_attributes(ep))["TopicArn"]
        config: dict[str, list] = {}
        for n in normed:
            if "external" in n:
                e = n["external"]
                config.setdefault(KINDS[e["kind"]][0], []).append(e["raw"])
            else:
                config.setdefault("TopicConfigurations", []).append(
                    _s3_config(n, arns[n["endpoint"]]))
        s3.put_bucket_notification_configuration(Bucket=bucket,
                                                 NotificationConfiguration=config)
        try:
            after = _read(s3, bucket, endpoints, uid)
        except Exception:  # the write succeeded: never lose the audit entry over the read-back
            log.warning("notification read-back failed after a successful write", exc_info=True)
            after = [{"view": v, "raw": {}, "kind": v["kind"]} for v in desired]
        return {"before": current, "after": after}


async def _own(request: Request, acct: RgwAccount, name: str) -> tuple[str, str]:
    short = _own_name(name)
    full = _full(acct.uid, short)
    if not any(b.get("bucket") == full
               for b in await _own_buckets(request.app.state.croit, acct.uid)):
        raise HTTPException(404, "Bucket not found.")
    return short, full


def _allowlist(request: Request):
    db = request.app.state.db
    return db.run(load_endpoints, db.settings)


def _s3_error(exc: Exception) -> HTTPException:
    err = exc.response.get("Error", {}) if isinstance(exc, ClientError) else {}
    code = err.get("Code")
    log.warning("notifications S3/SNS call failed: %s", code or type(exc).__name__)
    if code == "NoSuchBucket":
        return HTTPException(404, "Bucket not found.")
    if code in ("AccessDenied", "AuthorizationError"):
        return HTTPException(403, "You are not allowed to change this bucket's notifications.")
    if getattr(exc, "operation_name", "") in ("CreateTopic", "GetTopicAttributes"):
        # RGW's text can echo the push-endpoint address: never relay it.
        return HTTPException(422, "The storage service rejected the notification topic. "
                                  "Ask an administrator to check the endpoint settings.")
    if code in ("MalformedXML", "InvalidRequest", "InvalidArgument", "InvalidParameter",
                "InvalidTopic"):
        return HTTPException(422, "The storage service rejected the notifications: "
                                  f"{str(err.get('Message') or code)[:300]}")
    return HTTPException(502, "Could not reach the notification service for this bucket. "
                              "Try again, or ask an administrator.")


def _summary(entries: list[dict]) -> Any:
    out = [{"id": e["view"]["id"],
            "endpoint": e["view"]["endpoint"] or f"external:{e['view']['topic']}",
            "events": e["view"]["events"], "prefix": e["view"]["prefix"],
            "suffix": e["view"]["suffix"]} for e in entries]
    return out if len(json.dumps(out)) <= AUDIT_MAX else {"omitted": "larger than 8 KB"}


@router.get("/{name}/notifications")
async def get_notifications(name: str, request: Request, acct: Account):
    short, _ = await _own(request, acct, name)
    endpoints = await _allowlist(request)
    try:
        entries = await request.app.state.s3.call(acct.uid, _read, short, endpoints, acct.uid)
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    return JSONResponse(view(entries, endpoints), headers={"Cache-Control": "no-store"})


@router.put("/{name}/notifications")
async def put_notifications(name: str, body: Body, request: Request, acct: Account, user: User):
    short, full = await _own(request, acct, name)
    endpoints = await _allowlist(request)
    try:
        res = await request.app.state.s3.call_with_sns(
            acct.uid, _apply, short, body.notifications, body.etag, endpoints, acct.uid)
    except Invalid as exc:
        first = exc.errors[0]
        return JSONResponse(status_code=422, content={
            "detail": first["message"] if first["item"] is None
            else f"Notification {first['item']}: {first['message']}", "errors": exc.errors})
    except EtagMismatch as exc:
        return JSONResponse(status_code=409, content={
            "detail": "The notifications changed since you opened them. "
                      "Reload to see the current ones.",
            "etagMismatch": True,
            "current": view(exc.current, exc.endpoints)})
    except HTTPException:
        raise
    except Exception as exc:
        raise _s3_error(exc) from None
    before, after = res["before"], res["after"]
    changed = [e["view"] for e in before] != [e["view"] for e in after]
    if changed:
        await _audit(request, user.username, "bucket.notifications", full, uid=acct.uid,
                     count_before=len(before), count_after=len(after),
                     before=_summary(before), after=_summary(after))
    return {**view(after, endpoints), "changed": changed}
