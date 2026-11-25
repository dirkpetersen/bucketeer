"""Cloud (AWS) buckets in the admin views (Q19, P6-14).

`cloud_rows(app)` gives the admin Buckets tab and the chargeback CSV (`admin.py`) one entry
per cloud bucket from SQLite (`buckets` rows with `location = 'cloud'`; system buckets never
have a row and are filtered again), with its CloudWatch size (`cloud_buckets.size_of`, cached
1 h) while cloud is available, else size null. The quota is the row's `quota_tib`.

Route (`/api/admin/cloud-buckets`, admin only, CSRF via the parent router):
- `PUT /{name}/quota {quotaTiB}` (`name` = the full AWS name) -> `{bucket, quotaTiB, changed,
  monthlyCost, tagSynced, warning?}`. Admins are exempt from the weekly limit, but the change
  is recorded in `quota_changes` (not counted), like the on-premises admin quota route. The
  `bucketeer-quota` tag is re-synced by the AWS admin user (a failure leaves the row
  `tag_synced = 0`, which the daily cloud tick retries). Audit `admin.bucket.quota`
  (target_type `bucket`, details `location: cloud`, `owner`, `oldTiB`, `newTiB`, `tagSynced`),
  only when the quota changed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictInt

from . import bucket_quota, catalog, cloud_buckets
from .auth import SessionUser, require_admin
from .buckets import _user_lock
from .cloud_costs import is_system_bucket

router = APIRouter(prefix="/admin/cloud-buckets", tags=["admin"],
                   dependencies=[Depends(require_admin)])

Admin = Annotated[SessionUser, Depends(require_admin)]

TIB = 2**40
_NAME = re.compile(r"\A[a-z0-9][a-z0-9-]{1,62}\Z")


class QuotaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quotaTiB: StrictInt


def cloud_ready(app) -> bool:
    cloud = getattr(app.state, "cloud", None)
    return cloud is not None and bool(cloud.available)


def _system(app, name: str) -> bool:
    s = app.state.settings
    return is_system_bucket(name, s.cloud_bucket_prefix,
                            getattr(s, "cloud_billing_bucket", "") or "")


async def cloud_rows(app) -> list[dict[str, Any]]:
    """[{row, size ({bytes, asOf} | None), product}] for every cloud bucket in SQLite."""
    db = app.state.db
    rows = [r for r in await db.run(db.buckets.list_cloud) if not _system(app, r["name"])]
    cat = await db.run(catalog.load_catalog, db)
    ready = cloud_ready(app)
    sem = asyncio.Semaphore(4)

    async def one(r: dict) -> dict | None:
        if not ready:
            return None
        async with sem:
            return await cloud_buckets.size_of(app, r["name"])

    sizes = await asyncio.gather(*(one(r) for r in rows))
    return [{"row": r, "size": sz, "product": cloud_buckets._product(cat, r.get("storage_class"))}
            for r, sz in zip(rows, sizes, strict=True)]


def view(entry: dict, matches: bool | None) -> dict[str, Any]:
    """One cloud bucket in the shape of `admin._bucket_view`, plus the cloud fields."""
    r, size, product = entry["row"], entry["size"], entry["product"]
    tib = r.get("quota_tib")
    nbytes = size["bytes"] if size else None
    return {
        "name": r["name"], "tenant": "", "owner": r["owner_uid"],
        "ownerAd": r.get("created_by"),
        "objects": None, "bytes": nbytes, "sizeAsOf": size["asOf"] if size else None,
        "quota": {"enabled": bool(tib), "maxSize": tib * TIB if tib else None,
                  "maxObjects": None},
        "quotaTiB": tib,
        "budgetNumber": r.get("budget_number"), "budgetMatches": matches,
        "location": "cloud", "storageClass": r.get("storage_class"),
        "product": catalog.public(product),
        "overQuota": bool(tib and nbytes is not None and nbytes > tib * TIB),
        "createdAt": r.get("created_at") if r.get("creation_known", 1) else None,
        "tagPending": not r.get("tag_synced", 1),
    }


@router.put("/{name}/quota")
async def set_cloud_quota(name: str, body: QuotaBody, request: Request, admin: Admin):
    app, db = request.app, request.app.state.db
    if not _NAME.match(name) or _system(app, name):
        raise HTTPException(404, "Bucket not found.")
    try:
        new = bucket_quota.check_tib(body.quotaTiB)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    row = await db.run(db.buckets.get, name, "cloud")
    if row is None:
        raise HTTPException(404, "Bucket not found.")
    async with _user_lock(request, row["owner_uid"]):  # the owner's own changes wait
        row = await db.run(db.buckets.get, name, "cloud")
        if row is None:
            raise HTTPException(404, "Bucket not found.")
        product = cloud_buckets._product(await db.run(catalog.load_catalog, db),
                                         row.get("storage_class"))
        old = row.get("quota_tib")
        if old == new and row.get("tag_synced", 1):
            return {"bucket": name, "quotaTiB": new, "changed": False,
                    "monthlyCost": catalog.monthly_cost(product, new), "tagSynced": True}
        await db.run(db.buckets.set_quota_tib, name, new)
        if old != new:
            # recorded for the history, never counted against the owner's weekly limit
            await bucket_quota.record(app, username=None, uid=row["owner_uid"], full=name,
                                      old=old, new=new, actor=admin.username, counted=False)
        ok = await cloud_buckets._try_tags(app, {**row, "quota_tib": new}, None)
        if old != new:
            await db.run(db.audit.add, admin.username, "admin.bucket.quota", "bucket", name,
                         {"location": "cloud", "owner": row["owner_uid"], "oldTiB": old,
                          "newTiB": new, "tagSynced": ok})
    out: dict[str, Any] = {"bucket": name, "quotaTiB": new, "changed": old != new,
                           "monthlyCost": catalog.monthly_cost(product, new), "tagSynced": ok}
    if not ok:
        out["warning"] = ("The quota was changed, but the bucket's quota tag could not be "
                          "updated yet. It will be retried automatically.")
    return out
