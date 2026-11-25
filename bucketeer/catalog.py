"""The product catalog (PLAN 0) as bucket code needs it, read in ONE place.

The catalog itself is the `billing` setting's `products` (edited on the Billing tab,
`billing.py`: fixed ids `tier3|archive|cloud`, each `{id, name, pricePerTiBMonth,
advertised}`; `tier3`'s price is the billing `pricePerTiBMonth`). `load_catalog(db)` reads
it through `billing.load` and adds `currency`, `location` and `billedOn` (Tier-3-S3 is on
premises and billed on the quota; Archive-S3 and Cloud-S3 pass through the real AWS cost,
so their price is only an estimate). Missing products or fields fall back to
`DEFAULT_PRODUCTS` (the same values as billing's), so this is not a second catalog.

`product_for(catalog, location)` picks the product of a bucket (`tier3` on premises);
`monthly_cost(product, tib)` is what a quota costs per month, rounded to cents.
"""

from __future__ import annotations

import math
from typing import Any

from . import billing

TIER3, ARCHIVE, CLOUD = "tier3", "archive", "cloud"
IDS = (TIER3, ARCHIVE, CLOUD)
DEFAULT_PRODUCTS = billing.DEFAULT_PRODUCTS  # the one catalog: billing's
EXTRA = {TIER3: ("onprem", "quota"), ARCHIVE: ("cloud", "usage"), CLOUD: ("cloud", "usage")}


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v < 0:
        return None
    return float(v)


def build_catalog(stored: dict | None) -> list[dict[str, Any]]:
    """Pure: the catalog from billing's settings (`billing.load(db)`, the raw stored
    `billing` value, or None)."""
    stored = stored if isinstance(stored, dict) else {}
    currency = str(stored.get("currency") or "USD")
    by_id = {p.get("id"): p for p in stored.get("products") or [] if isinstance(p, dict)}
    out = []
    for d in DEFAULT_PRODUCTS:
        entry = by_id.get(d["id"]) or {}
        price = _number(entry.get("pricePerTiBMonth"))
        if d["id"] == TIER3 and _number(stored.get("pricePerTiBMonth")) is not None:
            price = _number(stored.get("pricePerTiBMonth"))  # tier3's price IS this one
        name, advertised = entry.get("name"), entry.get("advertised")
        location, billed = EXTRA[d["id"]]
        out.append({
            "id": d["id"],
            "name": name.strip() if isinstance(name, str) and name.strip() else d["name"],
            "pricePerTiBMonth": d["pricePerTiBMonth"] if price is None else price,
            "advertised": advertised.strip() if isinstance(advertised, str)
            and advertised.strip() else d["advertised"],
            "location": location, "billedOn": billed, "currency": currency})
    return out


def load_catalog(db) -> list[dict[str, Any]]:
    """The catalog (sync; call through `db.run`). `db` is the Database."""
    return build_catalog(billing.load(db))


def product(catalog: list[dict], pid: str) -> dict[str, Any]:
    return next(p for p in catalog if p["id"] == pid)


def product_for(catalog: list[dict], location: str = "onprem") -> dict[str, Any]:
    """The product of a bucket at `location` (on premises: always Tier-3-S3; the cloud
    products are chosen per bucket once cloud buckets exist)."""
    return product(catalog, TIER3 if location == "onprem" else CLOUD)


def monthly_cost(prod: dict, tib: int | None) -> float | None:
    """The monthly cost of `tib` TiB of quota (None without a quota), rounded to cents."""
    if tib is None:
        return None
    return round(tib * float(prod["pricePerTiBMonth"]), 2)


def public(prod: dict) -> dict[str, Any]:
    """What users see of a product."""
    return {k: prod[k] for k in ("id", "name", "pricePerTiBMonth", "currency", "advertised",
                                 "billedOn")}
