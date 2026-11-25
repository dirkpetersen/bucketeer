"""Cloud billing (P6-11): the CUR 2.0 data export summed per bucket (cloud_costs.py), the
cloud rows and Problems entries of the chargeback sheet, the cache, the email send day and
`tools/aws-setup --billing-export` (via --print, plus shellcheck). Every AWS call goes to
moto's in-process mock (`mock_aws`)."""

import asyncio
import csv
import gzip
import io
import json
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from openpyxl import load_workbook

from bucketeer import billing, billing_report, cloud, cloud_costs
from bucketeer.cloud import CloudError

from .test_credentials import Env

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "tools" / "aws-setup"
BILLING = "bucketeer-billing-0123456789ab"
BACKUP = "bucketeer-backup-00000000000a"
CLOUD = {"cloud_user_prefix": "s3-", "cloud_admin_access_key": "AKIAEXAMPLEKEY000001",
         "cloud_admin_secret_key": "cloud-secret-for-tests-only",
         "cloud_billing_bucket": BILLING, "s3_endpoint_url": "https://s3.example.org"}
HEADER = ["bill_billing_period_start_date", "line_item_usage_start_date",
          "line_item_usage_end_date", "line_item_usage_account_id", "line_item_product_code",
          "line_item_resource_id", "line_item_usage_type", "line_item_operation",
          "line_item_line_item_type", "line_item_usage_amount", "pricing_unit",
          "line_item_unblended_cost", "line_item_net_unblended_cost"]
EXPORT = "cur/bucketeer-s3-costs"


@pytest.fixture(autouse=True)
def no_cloud_monitor(monkeypatch):
    monkeypatch.setattr(cloud, "CHECK_ON_START", False)


def line(product, resource, day, cost, period="2026-10-01T00:00:00.000Z"):
    return {"bill_billing_period_start_date": period,
            "line_item_usage_start_date": f"{day}T00:00:00.000Z" if day else "",
            "line_item_product_code": product, "line_item_resource_id": resource,
            "line_item_line_item_type": "Usage", "line_item_unblended_cost": str(cost)}


def csv_gz(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, HEADER, restval="")
    w.writeheader()
    w.writerows(rows)
    return gzip.compress(buf.getvalue().encode())


OCTOBER = [
    line("AmazonS3", "bucketeer-alice-scans", "2026-10-03", "1.234"),
    line("AmazonS3", "bucketeer-alice-scans", "2026-10-04", "0.0101"),
    line("AmazonS3", "bucketeer-bob-deep", "2026-10-05", "0.505"),
    line("AmazonS3", "bucketeer-ghost-x", "2026-10-06", "2"),
    line("AmazonS3", "bucketeer-carol-odd", "2026-10-07", "3"),
    line("AmazonS3", BILLING, "2026-10-08", "0.1"),
    line("AmazonS3", BACKUP, "2026-10-08", "0.2"),
    line("AmazonS3", "", "2026-10-01", "0.05"),
    line("AmazonS3", "someone-elses-bucket", "2026-10-02", "9"),
    line("AmazonEC2", "bucketeer-alice-scans", "2026-10-03", "99"),
    line("AmazonS3", "bucketeer-alice-scans", "2026-09-30", "50"),
    line("AmazonS3", "bucketeer-alice-scans", "", "0.0001"),  # no usage date: the period
]


def s3():
    return boto3.client("s3", region_name="us-west-2")


def make_bucket(name, tags=None):
    s3().create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    if tags:
        s3().put_bucket_tagging(Bucket=name, Tagging={
            "TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]})


def upload_export(month, files, manifest=True, extra=()):
    keys = []
    for i, rows in enumerate(files, 1):
        key = f"{EXPORT}/data/BILLING_PERIOD={month}/bucketeer-s3-costs-{i:05d}.csv.gz"
        s3().put_object(Bucket=BILLING, Key=key, Body=csv_gz(rows))
        keys.append(key)
    for key, rows in extra:  # stale files a manifest does not name
        s3().put_object(Bucket=BILLING, Key=key, Body=csv_gz(rows))
    if manifest:
        s3().put_object(
            Bucket=BILLING, Key=f"{EXPORT}/metadata/BILLING_PERIOD={month}/"
                                f"bucketeer-s3-costs-Manifest.json",
            Body=json.dumps({"dataFiles": [f"s3://{BILLING}/{k}" for k in keys]}))


def seed_buckets():
    make_bucket(BILLING)
    make_bucket("bucketeer-alice-scans", {
        "bucketeer-owner": "alice", "bucketeer-owner-email": "alice@example.org",
        "bucketeer-budget": "DN123456", "bucketeer-product": "Cloud-S3",
        "bucketeer-location": "cloud"})
    make_bucket("bucketeer-bob-deep", {"bucketeer-owner": "bob", "bucketeer-product": "archive"})
    make_bucket("bucketeer-carol-odd", {"bucketeer-owner": "carol",
                                        "bucketeer-product": "Tier-3-S3"})


@pytest.fixture
def env(tmp_path):
    with mock_aws():
        e = Env(tmp_path, **CLOUD)
        e.now[0] = datetime(2026, 11, 20, 12).timestamp()
        seed_buckets()
        e.bob, e.alice = e.session("bob"), e.session("alice")
        yield e
        e.db.close()


def run(coro):
    return asyncio.run(coro)


# -- reading the export ------------------------------------------------------------------


def test_month_costs_sums_s3_per_bucket(env):
    upload_export("2026-10", [OCTOBER[:6], OCTOBER[6:]],
                  extra=[(f"{EXPORT}/data/BILLING_PERIOD=2026-10/stale.csv.gz",
                          [line("AmazonS3", "bucketeer-alice-scans", "2026-10-09", "1000")])])
    upload_export("2026-09", [[line("AmazonS3", "bucketeer-alice-scans", "2026-09-03", "7",
                                    period="2026-09-01T00:00:00.000Z")]])
    c = run(cloud_costs.month_costs(env.app, "2026-10"))
    assert c["available"] is True and c["files"] == 2 and c["final"] is True
    assert c["buckets"] == {"bucketeer-alice-scans": 1.2442, "bucketeer-bob-deep": 0.505,
                            "bucketeer-carol-odd": 3.0, "bucketeer-ghost-x": 2.0}
    assert c["system"] == {BACKUP: 0.2, BILLING: 0.1}
    assert c["unattributed"] == 0.05 and c["otherS3"] == 9.0
    assert c["exportTotal"] == 6.7492 and c["generatedAt"].endswith("+00:00")
    sept = run(cloud_costs.month_costs(env.app, "2026-09"))
    assert sept["buckets"] == {"bucketeer-alice-scans": 7.0}


def test_without_a_manifest_the_newest_folder_counts(env):
    upload_export("2026-10", [OCTOBER[:3]], manifest=False)
    c = run(cloud_costs.month_costs(env.app, "2026-10"))
    assert c["files"] == 1 and c["buckets"]["bucketeer-bob-deep"] == 0.505


def test_no_export_yet(env):
    c = run(cloud_costs.month_costs(env.app, "2026-11"))
    assert c["available"] is True and c["files"] == 0 and c["buckets"] == {}
    assert c["final"] is False and "24 hours" in c["reason"]


def test_plain_csv_and_bad_cost_rows(env):
    key = f"{EXPORT}/data/BILLING_PERIOD=2026-10/x.csv"
    rows = [line("AmazonS3", "bucketeer-alice-scans", "2026-10-03", "1.5"),
            line("AmazonS3", "bucketeer-alice-scans", "2026-10-03", "n/a")]
    s3().put_object(Bucket=BILLING, Key=key, Body=gzip.decompress(csv_gz(rows)))
    c = run(cloud_costs.month_costs(env.app, "2026-10"))
    assert c["buckets"] == {"bucketeer-alice-scans": 1.5} and c["badRows"] == 1


def test_system_buckets():
    assert cloud_costs.is_system_bucket(BILLING, "bucketeer-", BILLING)
    assert cloud_costs.is_system_bucket(BACKUP, "bucketeer-", BILLING)
    assert not cloud_costs.is_system_bucket("bucketeer-backup-photos", "bucketeer-", BILLING)
    assert not cloud_costs.is_system_bucket("bucketeer-alice-x", "bucketeer-", "")


def test_final_after_the_fifth():
    assert not cloud_costs.is_final("2026-10", date(2026, 11, 4))
    assert cloud_costs.is_final("2026-10", date(2026, 11, 5))
    assert cloud_costs.is_final("2026-12", date(2027, 1, 5))
    assert not cloud_costs.is_final("2026-12", date(2026, 12, 31))


# -- cache -------------------------------------------------------------------------------


def test_cache_refreshes_daily_and_keeps_final_months(env, monkeypatch):
    upload_export("2026-11", [[line("AmazonS3", "bucketeer-alice-scans", "2026-11-03", "1",
                                    period="2026-11-01T00:00:00.000Z")]])
    upload_export("2026-10", [OCTOBER])
    reads = []
    real = cloud_costs.read_month
    monkeypatch.setattr(cloud_costs, "read_month",
                        lambda *a: reads.append(a[2]) or real(*a))
    run(cloud_costs.month_costs(env.app, "2026-11"))
    run(cloud_costs.month_costs(env.app, "2026-11"))
    run(cloud_costs.month_costs(env.app, "2026-10"))
    assert reads == ["2026-11", "2026-10"]
    env.now[0] += 86400 + 1
    run(cloud_costs.month_costs(env.app, "2026-11"))
    run(cloud_costs.month_costs(env.app, "2026-10"))  # final: never read again
    assert reads == ["2026-11", "2026-10", "2026-11"]
    # a failed refresh answers the cached figures, marked stale
    env.now[0] += 86400 + 1

    def fail(*_a):
        raise CloudError(403, "AccessDenied", "AWS refused ListObjectsV2 (AccessDenied)")

    monkeypatch.setattr(cloud_costs, "read_month", fail)
    c = run(cloud_costs.month_costs(env.app, "2026-11"))
    assert c["stale"] is True and c["buckets"] == {"bucketeer-alice-scans": 1.0}
    assert "AccessDenied" in c["reason"]
    env.db.settings.delete(cloud_costs.CACHE_PREFIX + "2026-11")
    c = run(cloud_costs.month_costs(env.app, "2026-11"))
    assert c["available"] is False and "AccessDenied" in c["reason"]


def test_off_without_a_billing_bucket(tmp_path):
    e = Env(tmp_path, s3_endpoint_url="https://s3.example.org")
    try:
        assert cloud_costs.billing_enabled(e.app) is False
        assert run(cloud_costs.month_costs(e.app, "2026-10"))["available"] is False
        bob = e.session("bob")
        r = bob.get("/api/admin/billing/cloud?month=1999-10")
        assert r.status_code == 422
        e.now[0] = datetime(2026, 11, 20, 12).timestamp()
        bob = e.session("bob")
        assert bob.get("/api/admin/billing/cloud?month=2026-10").json() == {
            "enabled": False, "month": "2026-10", "currency": "USD"}
    finally:
        e.db.close()


# -- facts, rows and problems --------------------------------------------------------------


def test_facts_come_from_tags_and_are_remembered(env):
    names = ["bucketeer-alice-scans", "bucketeer-ghost-x"]
    f = run(cloud_costs.bucket_facts_for(env.app, names))
    assert f["bucketeer-alice-scans"] == {
        "owner": "alice", "email": "alice@example.org", "budget": "DN123456",
        "product": "Cloud-S3", "source": "tags"}
    assert f["bucketeer-ghost-x"]["source"] == "none"
    assert "no longer exists" in f["bucketeer-ghost-x"]["reason"]
    s3().delete_bucket_tagging(Bucket="bucketeer-alice-scans")
    s3().delete_bucket(Bucket="bucketeer-alice-scans")  # deleted during the month
    f = run(cloud_costs.bucket_facts_for(env.app, names))
    assert f["bucketeer-alice-scans"]["source"] == "remembered"
    assert f["bucketeer-alice-scans"]["email"] == "alice@example.org"


def test_cents_and_products():
    assert billing_report.cents(1.2442) == 124 and billing_report.cents(0.505) == 51
    assert billing_report.cents(0.004) == 0 and billing_report.cents(-1.236) == -124
    s = {"products": billing.DEFAULT_PRODUCTS}
    renamed = {"products": [{**p, "name": "Glacier"} if p["id"] == "archive" else p
                            for p in billing.DEFAULT_PRODUCTS]}
    assert billing_report.cloud_product(s, "cloud-s3") == "cloud"
    assert billing_report.cloud_product(s, " Archive-S3 ") == "archive"
    assert billing_report.cloud_product(renamed, "glacier") == "archive"
    assert billing_report.cloud_product(renamed, "Archive-S3") == "archive"  # renamed since
    assert billing_report.cloud_product(s, "Tier-3-S3") is None
    assert billing_report.cloud_product(s, "") is None


def test_cloud_rows_in_the_sheet(env):
    upload_export("2026-10", [OCTOBER])
    r = env.bob.get("/api/admin/billing/chargeback.xlsx?month=2026-10")
    assert r.status_code == 200, r.text
    ws, prob = load_workbook(io.BytesIO(r.content)).worksheets
    rows = {row[6].value: [c.value for c in row] for row in ws.iter_rows(min_row=2)}
    assert set(rows) == {"bucketeer-alice-scans", "bucketeer-bob-deep"}
    a = rows["bucketeer-alice-scans"]
    assert a[:4] == ["alice@example.org", "DN123456", "Cloud-S3", 124]
    assert a[4] == datetime(2026, 10, 1) and a[5] == datetime(2026, 10, 31)
    assert a[8] == "bucketeer-alice-scans"
    assert rows["bucketeer-bob-deep"][:4] == ["bob", None, "Archive-S3", 51]
    problems = {(p[0].value, p[2].value): p[3].value for p in prob.iter_rows(min_row=2)}
    assert set(problems) == {
        ("bucketeer-bob-deep", "No budget number"),
        ("bucketeer-bob-deep", "No email address for the owner"),
        ("bucketeer-ghost-x", "Unknown cloud bucket"),
        ("bucketeer-carol-odd", "Unknown product"),
        (BILLING, "System cost"), (BACKUP, "System cost"),
        ("(no bucket)", "S3 cost without a bucket name")}
    assert problems[("bucketeer-ghost-x", "Unknown cloud bucket")].startswith("2.00 USD")
    assert "3.00 USD" in problems[("bucketeer-carol-odd", "Unknown product")]
    assert "0.10 USD" in problems[(BILLING, "System cost")]
    assert "billing export" in problems[(BILLING, "System cost")]
    assert "backups" in problems[(BACKUP, "System cost")]
    assert "0.05 USD" in problems[("(no bucket)", "S3 cost without a bucket name")]


def test_budget_format_applies_to_cloud_rows(env):
    env.db.settings.set("budget_number", {"pattern": r"XX\d+", "example": "XX1"})
    upload_export("2026-10", [OCTOBER[:2]])
    rows, problems, _ = run(billing_report.cloud_part(env.app, "2026-10", r"XX\d+"))
    assert rows[0]["index"] is None
    assert [p["problem"] for p in problems] == ["Budget number does not match the format"]


def test_current_month_is_not_final_and_the_summary(env):
    upload_export("2026-11", [[line("AmazonS3", "bucketeer-alice-scans", "2026-11-03", "2.5",
                                    period="2026-11-01T00:00:00.000Z"),
                               line("AmazonS3", "bucketeer-ghost-x", "2026-11-03", "1",
                                    period="2026-11-01T00:00:00.000Z")]])
    r = env.bob.get("/api/admin/billing/cloud?month=2026-11")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["enabled"] is True and s["available"] is True and s["final"] is False
    assert s["exportTotal"] == 3.5 and s["rowsTotal"] == 2.5 and s["rows"] == 1
    assert s["unknownTotal"] == 1.0 and s["currency"] == "USD" and s["generatedAt"]
    rows, problems, _ = run(billing_report.cloud_part(env.app, "2026-11", None))
    assert ("(cloud)", "Cloud costs not final") in {(p["bucket"], p["problem"])
                                                    for p in problems}
    assert env.alice.get("/api/admin/billing/cloud?month=2026-11").status_code == 403


def test_unreadable_export_is_a_problem(env, monkeypatch):
    def fail(*_a):
        raise CloudError(403, "AccessDenied", "AWS refused ListObjectsV2 (AccessDenied)")

    monkeypatch.setattr(cloud_costs, "read_month", fail)
    rows, problems, s = run(billing_report.cloud_part(env.app, "2026-10", None))
    assert rows == [] and s["available"] is False
    assert [(p["problem"], "AccessDenied" in p["details"]) for p in problems] == [
        ("Cloud costs unavailable", True)]


def test_email_waits_for_the_fifth(env):
    settings = {**billing.load(env.db), "sendDay": 1}
    assert billing_report.send_settings(env.app, settings)["sendDay"] == 5
    assert billing_report.send_settings(env.app, {**settings, "sendDay": 9})["sendDay"] == 9
    env.db.settings.set(billing.KEY, {"contacts": ["f@example.org"], "sendDay": 2,
                                      "sendEnabled": True})

    class Mailer:
        enabled = True
        calls = []
        s = type("S", (), {"app_name": "Bucketeer", "app_url": "x"})()

        async def send_with_attachment(self, *a):
            self.calls.append(a)
            return (["f@example.org"], [])

    env.app.state.mailer = Mailer()
    tick = billing_report.report_tick
    assert run(tick(env.app, datetime(2026, 11, 4, 7))) is None  # day 2 would be due
    rep = env.bob.get("/api/admin/billing").json()["report"]
    assert rep["sendDay"] == 5 and rep["cloudBilling"] is True
    r = run(tick(env.app, datetime(2026, 11, 5, 6)))
    assert r["month"] == "2026-10" and Mailer.calls


# -- tools/aws-setup ---------------------------------------------------------------------


def setup_print(*extra):
    out = subprocess.run(
        ["bash", str(SETUP), "--admin-user", "example-admin", "--user-prefix", "s3-",
         "--print", *extra], capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin"}).stdout
    docs, current, name = {}, [], None
    for text in out.splitlines():
        if text.startswith("# "):
            if name:
                docs[name] = json.loads("\n".join(current))
            name, current = text[2:].split(" (")[0], []
        else:
            current.append(text)
    docs[name] = json.loads("\n".join(current))
    return docs


def by_sid(doc):
    return {s["Sid"]: s for s in doc["Statement"]}


def test_setup_without_a_billing_bucket_has_no_exception():
    docs = setup_print()
    assert set(docs) == {"BucketeerUserBoundary", "BucketeerAdmin"}
    admin = by_sid(docs["BucketeerAdmin"])
    assert admin["NoObjectRead"]["Resource"] == "*" and "NotResource" not in admin["NoObjectRead"]
    assert admin["NoObjectRead"]["Action"] == ["s3:GetObject"]
    assert "ReadBillingExport" not in admin and "LeaveBillingBucketAlone" not in admin
    assert "s3:GetObject" not in admin["NoObjectAccess"]["Action"]
    assert "s3:PutObject" in admin["NoObjectAccess"]["Action"]
    assert "NeverTheBillingBucket" not in by_sid(docs["BucketeerUserBoundary"])


def test_setup_with_a_billing_bucket():
    docs = setup_print("--billing-bucket", BILLING, "--billing-export")
    arn = f"arn:aws:s3:::{BILLING}"
    admin = by_sid(docs["BucketeerAdmin"])
    assert admin["NoObjectRead"]["NotResource"] == f"{arn}/*"
    assert "Resource" not in admin["NoObjectRead"]
    read = admin["ReadBillingExport"]
    assert read["Effect"] == "Allow" and read["Resource"] == [arn, f"{arn}/*"]
    assert set(read["Action"]) == {"s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject"}
    assert admin["LeaveBillingBucketAlone"]["Effect"] == "Deny"
    assert by_sid(docs["BucketeerUserBoundary"])["NeverTheBillingBucket"]["Effect"] == "Deny"
    (st,) = docs[f"bucket policy of {BILLING}"]["Statement"]
    assert set(st["Principal"]["Service"]) == {"billingreports.amazonaws.com",
                                               "bcm-data-exports.amazonaws.com"}
    assert st["Action"] == ["s3:PutObject", "s3:GetBucketPolicy"]
    cond = st["Condition"]
    assert cond["StringEquals"]["aws:SourceAccount"] == "123456789012"
    assert cond["StringLike"]["aws:SourceArn"] == [
        "arn:aws:cur:us-east-1:123456789012:definition/*",
        "arn:aws:bcm-data-exports:us-east-1:123456789012:export/*"]
    export = docs["data export bucketeer-s3-costs"]
    assert export["Name"] == "bucketeer-s3-costs"
    q = export["DataQuery"]["QueryStatement"]
    for col in ("line_item_product_code", "line_item_resource_id",
                "line_item_unblended_cost", "line_item_usage_start_date"):
        assert col in q
    assert q.endswith("FROM COST_AND_USAGE_REPORT")
    assert export["DataQuery"]["TableConfigurations"]["COST_AND_USAGE_REPORT"] == {
        "INCLUDE_RESOURCES": "TRUE", "TIME_GRANULARITY": "DAILY",
        "INCLUDE_SPLIT_COST_ALLOCATION_DATA": "FALSE",
        "INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY": "FALSE"}
    dest = export["DestinationConfigurations"]["S3Destination"]
    assert (dest["S3Bucket"], dest["S3Prefix"], dest["S3Region"]) == (BILLING, "cur",
                                                                      "us-west-2")
    assert dest["S3OutputConfigurations"] == {"OutputType": "CUSTOM", "Format": "TEXT_OR_CSV",
                                              "Compression": "GZIP",
                                              "Overwrite": "OVERWRITE_REPORT"}
    assert export["RefreshCadence"] == {"Frequency": "SYNCHRONOUS"}


def test_setup_refuses_a_bad_billing_bucket_name():
    r = subprocess.run(["bash", str(SETUP), "--admin-user", "example-admin", "--user-prefix",
                        "s3-", "--print", "--billing-bucket", "Bad_Name"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "--billing-bucket" in r.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_setup_is_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(SETUP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
