"""Per-request S3 clients that act as the signed-in user (the S3 data plane).

Every S3 call Bucketeer makes on a user's behalf goes through `S3Clients` (one per
app, `app.state.s3`): it takes the user's OWN first access key from croit (never
from the browser), resolves the endpoint like the credentials screen does, and builds
a boto3 client for that single request. RGW then enforces ownership, policies and
object lock. Never use an admin S3 identity for a user action, never build a boto3
client anywhere else, and never log or audit a key.

`factory(endpoint, access_key, secret_key, settings)` builds the boto3 client; tests
replace it to spy on which access key a call used (moto does not enforce identities,
so authorization is asserted at this layer). Calls are synchronous boto3: run them
with `await app.state.s3.call(acct, fn, ...)`, which uses `asyncio.to_thread`.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from fastapi import HTTPException

from .admin import croit_http_error
from .config import Settings
from .credentials import EndpointResolver
from .croit import CroitClient, CroitClientError

log = logging.getLogger("bucketeer.s3client")


def make_config(settings: Settings) -> Config:
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": settings.s3_addressing_style},
        # newer SDKs send CRC checksums that many Ceph RGW versions reject
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 2, "mode": "standard"},
        connect_timeout=5,
        read_timeout=30,
    )


def default_factory(endpoint: str, access_key: str, secret_key: str, settings: Settings) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=make_config(settings),
    )


class S3Clients:
    def __init__(
        self,
        croit: CroitClient,
        settings: Settings,
        endpoints: EndpointResolver,
        factory: Callable[[str, str, str, Settings], Any] = default_factory,
    ):
        self.croit, self.settings, self.endpoints = croit, settings, endpoints
        self.factory = factory
        # botocore logs canonical requests and SigV4 signatures at DEBUG (also for
        # presigned URLs): keep them out of the log whatever LOG_LEVEL is.
        for name in ("botocore", "boto3", "urllib3.connectionpool"):
            logging.getLogger(name).setLevel(logging.WARNING)

    async def client_for(self, uid: str) -> Any:
        """A boto3 client signed with the first access key of RGW user `uid`.
        Raises HTTPException (503/409) when there is no endpoint or key."""
        try:
            keys = await self.croit.list_keys(uid)
        except CroitClientError as e:
            raise croit_http_error(e) from None
        own = [k for k in keys or [] if isinstance(k, dict) and k.get("user") == uid
               and k.get("access_key") and k.get("secret_key")]
        if not own:
            raise HTTPException(409, "You have no access key yet. Create one on the "
                                     "credentials card, then try again.")
        endpoint = await self.endpoints.resolve(uid)
        if not endpoint:
            raise HTTPException(503, "The S3 endpoint is not configured. Ask an administrator.")
        return self.factory(endpoint, own[0]["access_key"], own[0]["secret_key"], self.settings)

    async def call(self, uid: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """`fn(s3_client, *args, **kwargs)` in a worker thread, as the user."""
        client = await self.client_for(uid)
        return await asyncio.to_thread(fn, client, *args, **kwargs)
