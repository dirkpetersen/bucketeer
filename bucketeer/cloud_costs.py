"""Cloud (AWS) costs per bucket from the CUR 2.0 data export (P6-11, PLAN 3, decision 18).

`tools/aws-setup --billing-export` makes AWS deliver a daily CUR 2.0 export (gzip CSV,
overwritten each refresh) to `CLOUD_BILLING_BUCKET` under `cur/`; Bucketeer's AWS admin
user may read that bucket and nothing else (`ListBucket` + `GetObject`). Cost allocation
tags and Cost Explorer's resource data are NOT used: a member account can't see them.

* `month_costs(app, month)` -> `{available, month, buckets: {name: cost}, unknown: {},
  system: {name: cost}, unattributed, exportTotal, otherS3, generatedAt, final, fetchedAt,
  files, rows, reason}` (costs in the export's currency, USD). Only
  `line_item_product_code == AmazonS3` rows whose usage starts in the month count, summed
  from `line_item_unblended_cost` per `line_item_resource_id` (the bucket name for S3).
  Buckets with `CLOUD_BUCKET_PREFIX` go to `buckets`, except system buckets
  (`is_system_bucket`: the billing bucket `<prefix>cloud-billing-<12 hex>` and the backup
  bucket `<prefix>backup-<12 hex>`, the same name as on premises), which go to `system`.
  `exportTotal` = the sum of `buckets` (what the sheet should add up to). S3 rows without a
  resource id (taxes, account-level charges) are `unattributed`; other S3 buckets of the
  account are `otherS3`.
  `final` = the month ended and today is the 5th of the next month or later
  (`FINAL_DAY`): AWS has finished it.
* Files: the newest `*Manifest.json` under `cur/.../metadata/BILLING_PERIOD=<month>/`
  names the data files (`dataFiles`); without a usable one, the newest folder of
  `*.csv.gz` / `*.csv` files under `.../data/BILLING_PERIOD=<month>/`. Each file is
  streamed, gunzipped and parsed row by row (bounded memory).
* Cache: settings key `cloud_costs:<month>`, re-read when older than `REFRESH_SECONDS`
  (1 day); a result fetched once the month was final is kept for good. A failed read
  returns the cached result with `stale: true` and the reason, else `available: false`.
* `bucket_facts(app, names)`: each bucket's `bucketeer-*` tags read with the admin user's
  GetBucketTagging; the last facts seen are remembered (settings key
  `cloud_billing_facts`), so a bucket deleted during the month is still billed to its
  owner.

The sheet rows are built in `billing_report.cloud_rows`.
"""

from __future__ import annotations

import asyncio
import codecs
import csv
import gzip
import io
import json
import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import bucket_facts
from .cloud import CloudError

log = logging.getLogger("bucketeer.cloud_costs")

EXPORT_PREFIX = "cur/"
CACHE_PREFIX = "cloud_costs:"
FACTS_KEY = "cloud_billing_facts"
REFRESH_SECONDS = 86400
FINAL_DAY = 5  # a month is final on the 5th of the next month (and the email waits)
S3_PRODUCT = "AmazonS3"
MAX_LIST = 100_000  # objects listed under cur/
MAX_FACTS = 5000
_CSV_LIMIT = 1 << 20  # one CSV field at most 1 MiB


def billing_enabled(app) -> bool:
    s = app.state.settings
    return bool(getattr(app.state, "cloud", None) is not None and s.cloud_billing_bucket)


_SYSTEM_SUFFIX = r"(backup-[0-9a-f]{12}|cloud-billing-[0-9a-f]{12}|billing-[0-9a-f]{8,12})"


def is_system_bucket(name: str, prefix: str, billing_bucket: str) -> bool:
    """Bucketeer's own buckets, not a person's: the billing bucket, the backup bucket and the
    older `billing-` name (its owner word `billing` is reserved, so a person never gets it)."""
    return bool(name and (name == billing_bucket
                          or re.fullmatch(re.escape(prefix) + _SYSTEM_SUFFIX, name)))


def is_final(month: str, today: date) -> bool:
    y, m = int(month[:4]), int(month[5:7])
    y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return today >= date(y, m, FINAL_DAY)


def _money(d: Decimal) -> float:
    return float(round(d, 8))


# -- finding the export files ---------------------------------------------------------


def _list(s3, bucket: str) -> list[dict]:
    out, token = [], None
    while True:
        params = {"Bucket": bucket, "Prefix": EXPORT_PREFIX,
                  **({"ContinuationToken": token} if token else {})}
        res = s3("list_objects_v2", params)
        out += res.get("Contents", [])
        if not res.get("IsTruncated") or len(out) >= MAX_LIST:
            return out
        token = res.get("NextContinuationToken")


def _manifest_files(s3, bucket: str, obj: dict, keys: set[str]) -> list[str] | None:
    try:
        body = s3("get_object", {"Bucket": bucket, "Key": obj["Key"]})["Body"]
        doc = json.loads(body.read(4 << 20))
    except (CloudError, ValueError) as e:
        log.warning("cloud billing: manifest unreadable (%s)", type(e).__name__)
        return None
    entries = doc.get("dataFiles") if isinstance(doc, dict) else None
    files = []
    for f in entries if isinstance(entries, list) else []:
        uri = f if isinstance(f, str) else \
            str(f.get("key") or f.get("uri") or "") if isinstance(f, dict) else ""
        key = uri.split(f"s3://{bucket}/", 1)[1] if uri.startswith(f"s3://{bucket}/") else uri
        if key in keys:
            files.append(key)
    return files or None


def find_files(s3, bucket: str, month: str) -> tuple[list[str], datetime | None]:
    """The data files of the newest export of `month`, and when they were written.
    `s3(operation, params)` is a synchronous S3 call."""
    period = f"BILLING_PERIOD={month}/"
    objects = [o for o in _list(s3, bucket) if period in o["Key"]]
    data = [o for o in objects if "/data/" in o["Key"]
            and o["Key"].endswith((".csv.gz", ".csv"))]
    manifests = sorted((o for o in objects if "/metadata/" in o["Key"]
                        and o["Key"].endswith("Manifest.json")),
                       key=lambda o: o["LastModified"])
    if manifests:
        files = _manifest_files(s3, bucket, manifests[-1], {o["Key"] for o in data})
        if files:
            return files, manifests[-1]["LastModified"]
    if not data:
        return [], None
    folders: dict[str, list[dict]] = {}
    for o in data:
        folders.setdefault(o["Key"].rsplit("/", 1)[0], []).append(o)
    newest = max(folders.values(), key=lambda g: max(o["LastModified"] for o in g))
    return (sorted(o["Key"] for o in newest), max(o["LastModified"] for o in newest))


# -- reading them ---------------------------------------------------------------------


class Totals:
    def __init__(self, month: str, prefix: str, billing_bucket: str):
        self.month, self.prefix, self.billing_bucket = month, prefix, billing_bucket
        self.buckets: dict[str, Decimal] = {}
        self.system: dict[str, Decimal] = {}
        self.unattributed = Decimal(0)
        self.other = Decimal(0)
        self.rows = 0
        self.bad = 0

    def add(self, row: dict[str, str]) -> None:
        if (row.get("line_item_product_code") or "") != S3_PRODUCT:
            return
        start = (row.get("line_item_usage_start_date") or
                 row.get("bill_billing_period_start_date") or "")
        if start[:7] != self.month:
            return
        try:
            cost = Decimal((row.get("line_item_unblended_cost") or "0").strip() or "0")
        except InvalidOperation:
            self.bad += 1
            return
        if not cost.is_finite():
            self.bad += 1
            return
        self.rows += 1
        name = (row.get("line_item_resource_id") or "").strip()
        if not name:
            self.unattributed += cost
        elif is_system_bucket(name, self.prefix, self.billing_bucket):
            self.system[name] = self.system.get(name, Decimal(0)) + cost
        elif name.startswith(self.prefix):
            self.buckets[name] = self.buckets.get(name, Decimal(0)) + cost
        else:
            self.other += cost

    def read(self, stream, gzipped: bool) -> None:
        text = (io.TextIOWrapper(gzip.GzipFile(fileobj=stream), encoding="utf-8-sig",
                                 newline="") if gzipped
                else codecs.getreader("utf-8-sig")(stream))
        for row in csv.DictReader(text):
            self.add(row)

    def result(self) -> dict[str, Any]:
        buckets = {k: _money(v) for k, v in sorted(self.buckets.items())}
        return {"buckets": buckets,
                "system": {k: _money(v) for k, v in sorted(self.system.items())},
                "unattributed": _money(self.unattributed), "otherS3": _money(self.other),
                "exportTotal": _money(sum(self.buckets.values(), Decimal(0))),
                "rows": self.rows, "badRows": self.bad}


def read_month(s3, bucket: str, month: str, prefix: str) -> dict[str, Any]:
    """Synchronous: find and sum one month's export (see the module docstring)."""
    csv.field_size_limit(_CSV_LIMIT)
    files, generated = find_files(s3, bucket, month)
    totals = Totals(month, prefix, bucket)
    for key in files:
        body = s3("get_object", {"Bucket": bucket, "Key": key})["Body"]
        try:
            totals.read(body, key.endswith(".gz"))
        except (OSError, EOFError, csv.Error, UnicodeDecodeError) as e:
            raise CloudError(502, "bad_export",
                             f"The billing export file could not be read "
                             f"({type(e).__name__})") from None
        finally:
            body.close()
    out = totals.result()
    out.update(files=len(files),
               generatedAt=generated.astimezone(UTC).isoformat(timespec="seconds")
               if generated else None)
    if not files:
        out["reason"] = "No billing export for this month yet (AWS delivers the first one " \
                        "within about 24 hours)"
    return out


async def month_costs(app, month: str, *, today: date | None = None) -> dict[str, Any]:
    """The month's cloud costs, from the cache when it is fresh (see the docstring)."""
    db = app.state.db
    if not billing_enabled(app):
        return {"available": False, "month": month, "reason": "Cloud billing is not set up"}
    now = datetime.fromtimestamp(app.state.clock(), UTC)
    today = today or date.fromtimestamp(app.state.clock())
    key = CACHE_PREFIX + month
    cached = await db.run(db.settings.get, key)
    if cached and cached.get("available"):
        age = (now - datetime.fromisoformat(cached["fetchedAt"])).total_seconds()
        if cached.get("final") or 0 <= age < REFRESH_SECONDS:
            return cached
    admin = app.state.cloud
    s = app.state.settings
    try:
        res = await asyncio.to_thread(
            read_month, lambda op, params: admin._call_sync("s3", op, params),
            s.cloud_billing_bucket, month, s.cloud_bucket_prefix)
    except CloudError as e:
        log.warning("cloud billing: export for %s unreadable: %s", month, e.message)
        if cached and cached.get("available"):
            return {**cached, "stale": True, "reason": e.message}
        return {"available": False, "month": month, "reason": e.message}
    res.update(available=True, month=month, final=bool(res["files"]) and is_final(month, today),
               fetchedAt=now.isoformat(timespec="seconds"))
    res.setdefault("reason", None)
    await db.run(db.settings.set, key, res, "system")
    log.info("cloud billing: %s read (%d files, %d S3 rows, %d buckets, final %s)", month,
             res["files"], res["rows"], len(res["buckets"]), res["final"])
    return res


# -- bucket facts from tags -----------------------------------------------------------


def _facts_from_tags(tags: list[dict]) -> dict[str, str | None]:
    t = {x.get("Key"): x.get("Value") for x in tags}
    return {"owner": t.get(bucket_facts.OWNER), "email": t.get(bucket_facts.OWNER_EMAIL),
            "budget": t.get(bucket_facts.BUDGET), "product": t.get(bucket_facts.PRODUCT)}


async def bucket_facts_for(app, names: list[str]) -> dict[str, dict[str, Any]]:
    """bucket -> {owner, email, budget, product, source: tags|remembered|none, reason?}.
    Tags read now win; a bucket that is gone (or unreadable) falls back to the facts
    remembered from an earlier read."""
    db = app.state.db
    admin = app.state.cloud
    remembered: dict = await db.run(db.settings.get, FACTS_KEY, {}) or {}
    out: dict[str, dict[str, Any]] = {}
    seen = datetime.fromtimestamp(app.state.clock(), UTC).date().isoformat()
    changed = False
    for name in names:
        try:
            res = await admin.call("s3", "get_bucket_tagging", Bucket=name)
            facts = _facts_from_tags(res.get("TagSet", []))
        except CloudError as e:
            if e.code == "NoSuchTagSet":
                facts = _facts_from_tags([])
            else:
                old = remembered.get(name)
                reason = ("the bucket no longer exists" if e.code == "NoSuchBucket"
                          else f"its tags could not be read ({e.code})")
                out[name] = ({**old, "source": "remembered", "reason": reason} if old
                             else {"owner": None, "email": None, "budget": None,
                                   "product": None, "source": "none", "reason": reason})
                continue
        out[name] = {**facts, "source": "tags"}
        if facts["owner"] and remembered.get(name) != {**facts, "seen": seen}:
            remembered[name] = {**facts, "seen": seen}
            changed = True
    if changed:
        if len(remembered) > MAX_FACTS:  # keep the most recently seen
            keep = sorted(remembered, key=lambda k: remembered[k].get("seen", ""))[-MAX_FACTS:]
            remembered = {k: remembered[k] for k in keep}
        await db.run(db.settings.set, FACTS_KEY, remembered, "system")
    return out
