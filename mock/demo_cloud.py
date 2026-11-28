"""The demo's pretend AWS account (`mock/demo.py --cloud`): moto's in-process `mock_aws`.

Dev tooling only, like the rest of `mock/`: nothing in `bucketeer/` imports it. It

- starts `mock_aws` (it patches botocore globally, so the boto3 clients `CloudAdmin` builds
  in worker threads are covered too). It must start BEFORE the demo's moto S3 server is
  seeded: starting it resets moto's in-process backends, which that server shares;
- keeps the cloud in its own moto account (`DEMO_ACCOUNT`), so the on-premises buckets of
  the moto S3 server (default account) never show up as AWS buckets and the other way round.
  The bootstrap calls name the account with moto's `x-moto-account-id` header; afterwards
  every request is signed with a key IAM made in that account, which moto maps back to it;
- creates what `tools/aws-setup` would: the `BucketeerUserBoundary` policy under
  `/bucketeer/`, Bucketeer's own IAM user under `/bucketeer-admin/` and its key, and the
  billing bucket with a CUR 2.0 style export (gzip CSV + manifest) for last month and this
  month, plus CloudWatch `BucketSizeBytes` readings for the cloud buckets the demo suggests;
- builds a `CloudAdmin` whose GetUser answers carry the boundary (moto omits it; AWS sends
  it, and `CloudAdmin.is_ours` checks it).

No network, no real AWS: every call ends in moto. The account id is a made-up number.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
from datetime import UTC, date, datetime, timedelta

import boto3

DEMO_ACCOUNT = "000011112222"  # made up; only moto ever sees it
REGION = "us-west-2"
USER_PREFIX = "s3-"
BUCKET_PREFIX = "bucketeer-"
BILLING_BUCKET = "bucketeer-cloud-billing-00000000dd01"
EXPORT = "cur/bucketeer-s3-costs"
ADMIN_USER = "bucketeer-demo-admin"
BOUNDARY_DOC = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
     "Resource": "*"},
    {"Effect": "Allow", "Action": ["s3:*Object*", "s3:ListBucket*", "s3:*MultipartUpload*"],
     "Resource": [f"arn:aws:s3:::{BUCKET_PREFIX}*", f"arn:aws:s3:::{BUCKET_PREFIX}*/*"]}]}
# Cloud buckets the demo pre-fills sizes and costs for: create them in the UI as alice
# ("scans", Intelligent-Tiering; "archive", Deep Archive) to see figures right away.
DEMO_BUCKETS = {f"{BUCKET_PREFIX}alice-scans": ("IntelligentTieringFAStorage", 0.42 * 2**40),
                f"{BUCKET_PREFIX}alice-archive": ("DeepArchiveStorage", 3.1 * 2**40)}
TAKEN_BUCKET = f"{BUCKET_PREFIX}alice-photos"
# Those buckets also get a few sample objects when the UI creates them (moto holds a few
# bytes; CloudWatch above pretends the real size), so the delete dialog shows a bucket
# with content and its delete job has work to do.
DEMO_OBJECTS = 24
CUR_HEADER = ["bill_billing_period_start_date", "line_item_usage_start_date",
              "line_item_product_code", "line_item_resource_id", "line_item_line_item_type",
              "line_item_unblended_cost"]


def _boot_client(service: str):
    """A bootstrap client acting inside DEMO_ACCOUNT (moto reads the header)."""
    c = boto3.client(service, region_name=REGION, aws_access_key_id="demo-bootstrap",
                     aws_secret_access_key="demo-bootstrap")
    c.meta.events.register(
        "before-send.*.*",
        lambda request, **_: request.headers.__setitem__("x-moto-account-id", DEMO_ACCOUNT))
    return c


def start():
    """Start mock_aws (call before the moto S3 server is seeded). Returns the mock."""
    from moto import mock_aws

    m = mock_aws()
    m.start()
    return m


def bootstrap() -> tuple[str, str]:
    """aws-setup's part: boundary policy, admin user and key. Returns (access, secret)."""
    iam = _boot_client("iam")
    iam.create_policy(PolicyName="BucketeerUserBoundary", Path="/bucketeer/",
                      PolicyDocument=json.dumps(BOUNDARY_DOC))
    iam.create_user(UserName=ADMIN_USER, Path="/bucketeer-admin/")
    key = iam.create_access_key(UserName=ADMIN_USER)["AccessKey"]
    return key["AccessKeyId"], key["SecretAccessKey"]


def _cur_line(resource: str, day: date, cost: str) -> dict:
    return {"bill_billing_period_start_date": f"{day:%Y-%m}-01T00:00:00.000Z",
            "line_item_usage_start_date": f"{day.isoformat()}T00:00:00.000Z",
            "line_item_product_code": "AmazonS3", "line_item_resource_id": resource,
            "line_item_line_item_type": "Usage", "line_item_unblended_cost": cost}


def _month_rows(first: date, days: int) -> list[dict]:
    rows = []
    for i in range(days):
        d = first + timedelta(days=i)
        rows.append(_cur_line(f"{BUCKET_PREFIX}alice-scans", d, "0.2523"))
        rows.append(_cur_line(f"{BUCKET_PREFIX}alice-archive", d, "0.1031"))
        rows.append(_cur_line(BILLING_BUCKET, d, "0.0004"))
    rows.append(_cur_line("", first, "0.0150"))  # S3 cost without a bucket
    return rows


def seed_billing_and_metrics(today: date | None = None) -> None:
    """The billing bucket with last month's and this month's export, and CloudWatch sizes."""
    today = today or date.today()
    s3 = _boot_client("s3")
    s3.create_bucket(Bucket=BILLING_BUCKET,
                     CreateBucketConfiguration={"LocationConstraint": REGION})
    this_month = today.replace(day=1)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    for first, days in ((last_month, (this_month - last_month).days),
                        (this_month, max(today.day - 1, 1))):
        month = f"{first:%Y-%m}"
        buf = io.StringIO()
        w = csv.DictWriter(buf, CUR_HEADER, restval="")
        w.writeheader()
        w.writerows(_month_rows(first, days))
        key = f"{EXPORT}/data/BILLING_PERIOD={month}/bucketeer-s3-costs-00001.csv.gz"
        s3.put_object(Bucket=BILLING_BUCKET, Key=key, Body=gzip.compress(buf.getvalue().encode()))
        s3.put_object(Bucket=BILLING_BUCKET,
                      Key=f"{EXPORT}/metadata/BILLING_PERIOD={month}/bucketeer-s3-costs-Manifest.json",
                      Body=json.dumps({"dataFiles": [f"s3://{BILLING_BUCKET}/{key}"]}))
    # A bucket already in the account but unknown to Bucketeer: typing "photos" as alice
    # shows the "name in use" answer with suggestions.
    s3.create_bucket(Bucket=TAKEN_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    cw = _boot_client("cloudwatch")
    for bucket, (storage, nbytes) in DEMO_BUCKETS.items():
        for back in (54, 30):
            cw.put_metric_data(Namespace="AWS/S3", MetricData=[{
                "MetricName": "BucketSizeBytes", "Value": float(nbytes), "Unit": "Bytes",
                "Timestamp": datetime.now(UTC) - timedelta(hours=back),
                "Dimensions": [{"Name": "BucketName", "Value": bucket},
                               {"Name": "StorageType", "Value": storage}]}])


def boundary_session_factory():
    """Session factory for `CloudAdmin`: GetUser answers carry the boundary that CreateUser
    sent, as on AWS (moto leaves it out)."""
    created: dict[str, str | None] = {}

    def factory(**kw):
        session = boto3.session.Session(**kw)
        real = session.client

        def client(service, **ckw):
            c = real(service, **ckw)
            if service == "iam":
                def remember(params, **_):
                    created[params["UserName"]] = params.get("PermissionsBoundary")

                def add(parsed, **_):
                    u = parsed.get("User") or {}
                    if created.get(u.get("UserName")):
                        u["PermissionsBoundary"] = {
                            "PermissionsBoundaryType": "Policy",
                            "PermissionsBoundaryArn": created[u["UserName"]]}

                c.meta.events.register("provide-client-params.iam.CreateUser", remember)
                c.meta.events.register("after-call.iam.GetUser", add)
            return c

        session.client = client
        return session
    return factory


def cloud_settings(access: str, secret: str) -> dict:
    """The CLOUD_* values for the demo's Settings."""
    return {"cloud_region": REGION, "cloud_bucket_prefix": BUCKET_PREFIX,
            "cloud_user_prefix": USER_PREFIX, "cloud_admin_access_key": access,
            "cloud_admin_secret_key": secret, "cloud_billing_bucket": BILLING_BUCKET}


def make_cloud_admin(settings):
    """A `CloudAdmin` on moto; IAM propagation waits are off (moto has none)."""
    from bucketeer import backup, cloud

    cloud.GRANT_WAITS = ()
    backup.KEY_WAITS = ()
    admin = cloud.CloudAdmin(settings, session_factory=boundary_session_factory())
    real_create = admin.create_bucket

    async def create_bucket(name: str) -> None:
        await real_create(name)
        if name in DEMO_BUCKETS:
            await asyncio.to_thread(_fill, name)

    admin.create_bucket = create_bucket
    return admin


def _fill(bucket: str) -> None:
    s3 = _boot_client("s3")
    for i in range(DEMO_OBJECTS):
        s3.put_object(Bucket=bucket, Key=f"demo/sample-{i:03d}.txt", Body=b"demo data\n")
