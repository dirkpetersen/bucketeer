---
title: Admin guide
description: Users, buckets, quotas, offboarding and billing for Bucketeer admins.
---

<div class="osu-hero" markdown>
<span class="osu-kicker">For admins</span>

# Admin guide

Admins see every S3 user and bucket, set quotas, suspend access, run offboarding, edit the
portal's rules and see what each user and bucket costs.
</div>

## Who is an admin

Members of the AD group `LDAP_ADMIN_GROUP_DN`, and the usernames listed in `ADMIN_USERS`
in the `.env` file (comma-separated, for example `ADMIN_USERS=jdoe,asmith`). They still log
in with their normal AD account. Admins open the admin page from the menu under their name.

<figure class="osu-shot" markdown>
![The admin page with the Users tab](assets/screens/admin-users.webp#only-light)
![The admin page with the Users tab](assets/screens/admin-users-dark.webp#only-dark)
<figcaption>Users, buckets, bucket keys, audit, offboarding, billing and settings.</figcaption>
</figure>

## Billing

The **Billing** tab shows what each user and each bucket costs per month.

- Set the **price per TiB per month**, the **currency** and the **fiscal year start**
  (January 1, April 1, July 1 or October 1). Changes are recorded in the audit log.
- The table shows the last 12 months (the current month so far) and **fiscal year to
  date**, per user or per bucket. Bucket rows show the budget number.
- **Export CSV** downloads the same table for finance, with totals.

<figure class="osu-shot" markdown>
![The Billing tab with monthly charges per bucket](assets/screens/admin-billing.webp#only-light)
![The Billing tab with monthly charges per bucket](assets/screens/admin-billing-dark.webp#only-dark)
<figcaption>Monthly charges per bucket, with the totals and the days measured in each month.</figcaption>
</figure>

How the charge is calculated:

- Ceph keeps no history of sizes, so Bucketeer reads the size of every bucket each hour
  and keeps the last reading of each day.
- A month's charge = (sum of the daily sizes in TiB) / (days in the month) x price. A
  bucket that holds 2 TiB all month costs 2 x the price.
- A day without a reading (before Bucketeer started, or when the cluster could not be
  read) costs nothing. **Days measured** shows this for each month.
- The current price applies to all months shown. 1 TiB = 2^40 bytes.

## Bucket tags

Every bucket carries its own record as `bucketeer-*` tags, so its facts survive outside
Bucketeer's database:

| Tag | Value |
|---|---|
| `bucketeer-budget` | budget number |
| `bucketeer-bucket` | bucket name (`tenant:bucket` with a tenant) |
| `bucketeer-owner` | owner's S3 user (`tenant:uid` with a tenant) |
| `bucketeer-owner-email` | owner's email from AD |
| `bucketeer-creator` | who created it in Bucketeer |
| `bucketeer-created` | creation date (UTC) |
| `bucketeer-quota` | bucket quota in whole TiB (rounded up), or `unlimited` |
| `bucketeer-product` | product, `Tier-3-S3` on premises |
| `bucketeer-location` | `onprem` |

Bucketeer writes them with the owner's own key when a bucket is created, when its budget
number or quota changes, and when a failed write is retried. Users see them read-only in
the tag editor; they leave 41 of the 50 tags a bucket can have for users. Tags are a
record, not proof: an owner can change them with their own keys, and nothing that
decides access reads them.

For buckets that existed before, use **Write Bucketeer tags** on the **Buckets** tab. It
writes the tags to every bucket with its owner's key and reports how many were updated,
already up to date, skipped (owner suspended, unknown or without an S3 key) and failed.
It changes nothing else and is safe to run again. Facts Bucketeer doesn't know (for
example the creation date of a bucket made outside Bucketeer) are left out.

## Other tabs

| Tab | What it does |
|---|---|
| Users | Quotas, suspend and restore. Suspending a user also suspends their bucket keys. |
| Buckets | Usage, bucket quotas, budget numbers (with a check against the format), chargeback CSV, Write Bucketeer tags. |
| Bucket keys | All keys scoped to one bucket; revoke them. Admins never see a secret. |
| Audit | Who did what and when; filter and export CSV. |
| Offboarding | Daily check against AD. Starts in report-only mode; switch to enforce when you trust it. |
| Settings | Bucket naming rules, budget number format, placements, default quota, uid mappings, notification endpoints. |

For installation and the cluster side, see [Deployment](deployment.md) and
[Admin Ops setup](admin-ops-setup.md).
