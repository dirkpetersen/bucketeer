"""Credentials API: the signed-in user's access keys and the values for the
generated `~/.aws/config` / `~/.aws/credentials` snippets.

- `GET /api/credentials` -> `{profile, region, endpointUrl, addressingStyle,
  keys: [{accessKey}]}`. It never carries a secret.
- `POST /api/credentials/reveal` `{accessKey}` -> `{accessKey, secretKey}`. The
  key must be one of the user's own (fetched from croit and matched exactly);
  anything else is a 404. Each reveal is audited (`credentials.reveal`: actor,
  uid, access key, never the secret) and answered with `Cache-Control: no-store`.

The uid comes only from the session (`require_active_rgw_user`), so suspended
users get no keys. The browser builds the snippets from this data, so a secret
appears in no response other than the reveal.

Endpoint: `S3_ENDPOINT_URL` when set; otherwise the gateway address in croit's
`.s3cfg` for the user (`host_base`, `use_https`), cached in memory; otherwise
null and the UI tells the user to contact an administrator.
"""

import logging
import re
import time
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .admin import croit_http_error
from .config import Settings
from .croit import CroitClient, CroitClientError
from .rgw_user import RgwAccount, require_active_rgw_user

log = logging.getLogger("bucketeer.credentials")

ENDPOINT_TTL = 300.0  # seconds a discovered endpoint is remembered
ENDPOINT_MISS_TTL = 30.0  # ... and a failed discovery (retry soon)

_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?\Z")


def endpoint_from_s3cfg(text: str) -> str | None:
    """`https?://host_base` from `.s3cfg` text, or None when it has no usable host.
    Comment lines are ignored (the file also mentions the DNS name in one)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, sep, value = line.partition("=")
        if sep:
            values.setdefault(key.strip().lower(), value.strip())
    host = values.get("host_base", "")
    if not _HOST.match(host):
        return None
    https = values.get("use_https", "").lower() in ("true", "yes", "1", "on")
    return f"{'https' if https else 'http'}://{host}"


class EndpointResolver:
    """S3_ENDPOINT_URL, else the croit `.s3cfg` host (cached in memory)."""

    def __init__(
        self, croit: CroitClient, settings: Settings, clock: Callable[[], float] = time.time
    ):
        self.croit, self.configured, self.clock = croit, settings.s3_endpoint_url, clock
        self._cached: tuple[float, str | None] | None = None  # (expires, endpoint)

    async def resolve(self, uid: str) -> str | None:
        if self.configured:
            return self.configured
        now = self.clock()
        if self._cached and self._cached[0] > now:
            return self._cached[1]
        endpoint = None
        try:
            cfg = await self.croit.cfg_file(uid)
            text = cfg.get("fileContents") if isinstance(cfg, dict) else None
            endpoint = endpoint_from_s3cfg(text) if isinstance(text, str) else None
        except CroitClientError as e:
            log.warning("endpoint discovery via the croit .s3cfg failed: %s", type(e).__name__)
        self._cached = (now + (ENDPOINT_TTL if endpoint else ENDPOINT_MISS_TTL), endpoint)
        return endpoint


router = APIRouter(prefix="/credentials", tags=["credentials"])  # mounted under /api

Account = Annotated[RgwAccount, Depends(require_active_rgw_user)]


class RevealBody(BaseModel):
    accessKey: str = Field(min_length=1, max_length=256)


async def _own_keys(request: Request, uid: str) -> list[dict]:
    try:
        keys = await request.app.state.croit.list_keys(uid)
    except CroitClientError as e:
        raise croit_http_error(e) from None
    # Subuser keys (user "uid:sub") are not offered until subusers exist.
    return [k for k in keys or [] if isinstance(k, dict) and k.get("access_key")
            and k.get("user") == uid]


@router.get("")
async def get_credentials(request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    settings: Settings = request.app.state.settings
    keys = await _own_keys(request, acct.uid)
    endpoint = await request.app.state.endpoints.resolve(acct.uid)
    return {
        "profile": settings.aws_profile_name,
        "region": settings.s3_region,
        "endpointUrl": endpoint,
        "addressingStyle": settings.s3_addressing_style,
        "keys": [{"accessKey": k["access_key"]} for k in keys],
    }


@router.post("/reveal")
async def reveal(body: RevealBody, request: Request, response: Response, acct: Account):
    response.headers["Cache-Control"] = "no-store"
    key = next((k for k in await _own_keys(request, acct.uid)
                if k["access_key"] == body.accessKey and k.get("secret_key")), None)
    if key is None:
        raise HTTPException(404, "Access key not found.")
    db = request.app.state.db
    # Audit first: if the log cannot be written, the secret is not handed out.
    await db.run(db.audit.add, acct.username, "credentials.reveal", "access_key",
                 key["access_key"], {"uid": acct.uid})
    return {"accessKey": key["access_key"], "secretKey": key["secret_key"]}
