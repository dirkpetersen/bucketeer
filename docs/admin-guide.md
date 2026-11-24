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

## Other tabs

| Tab | What it does |
|---|---|
| Users | Quotas, suspend and restore. Suspending a user also suspends their bucket keys. |
| Buckets | Usage, bucket quotas, budget numbers (with a check against the format), chargeback CSV. |
| Bucket keys | All keys scoped to one bucket; revoke them. Admins never see a secret. |
| Audit | Who did what and when; filter and export CSV. |
| Offboarding | Daily check against AD. Starts in report-only mode; switch to enforce when you trust it. |
| Settings | Default location (On Premises or Cloud), bucket naming rules, budget number format, placements, default quota, uid mappings, notification endpoints. |

## Cloud (AWS) { #cloud }

Bucketeer can also keep buckets in one shared AWS account. It is off until you set it up.

**1. Prepare the AWS account (once).** An AWS administrator runs `tools/aws-setup` with
the AWS CLI v2. Pick a name for Bucketeer's own IAM user and a prefix for the per-person
IAM users (2-17 lowercase letters, digits, `_` or `-`, ending with `-`):

```bash
tools/aws-setup --admin-user <name> --user-prefix <prefix> --print        # show the policies only
tools/aws-setup --admin-user <name> --user-prefix <prefix> --create-key   # set up, print the .env lines once
```

The script creates the permissions boundary for person users, the admin policy and the
admin user, and with `--create-key` prints the `CLOUD_*` lines. `--prefix` (default
`bucketeer-`) and `--region` (default `us-west-2`) must match `CLOUD_BUCKET_PREFIX` and
`CLOUD_REGION`. See `tools/README.md` for details.

**2. Put the lines into `.env`** (`appmo env bucketeer`) and restart:

| Variable | Meaning |
|---|---|
| `CLOUD_REGION` | AWS region, default `us-west-2`. |
| `CLOUD_BUCKET_PREFIX` | Every cloud bucket is `<prefix><username>-<name>`; default `bucketeer-`. |
| `CLOUD_USER_PREFIX` | Name prefix of the per-person IAM users (the `--user-prefix`). |
| `CLOUD_ADMIN_ACCESS_KEY`, `CLOUD_ADMIN_SECRET_KEY` | The admin user's key from `--create-key`. |
| `CLOUD_PROFILE_NAME` | Profile name for cloud keys on users' computers, default `s3-cloud`. |

Cloud is on only when `CLOUD_USER_PREFIX` and both keys are set. Setting only some of them
stops Bucketeer at startup with a message naming the missing ones. Bucketeer checks the
connection at startup and then every hour (every 5 minutes while it fails).

**3. Choose the default location.** On **Settings > Default location** Bucketeer shows the
result of a fresh connection check (account and region, or why it failed). **Cloud** can
be chosen only while the check passes. Once Cloud is the default, everyone works in the
cloud by default, with Advanced mode on or off; people with Advanced mode on get a
**Cloud | On Premises** switch next to the Advanced toggle and can choose for themselves.
Changes are recorded in the audit log.

!!! note "Profile names changed"
    The on-premises profile on users' computers is now `s3-onprem` (`AWS_PROFILE_NAME`; it
    was `bucketeer`) and cloud keys use `s3-cloud` (`CLOUD_PROFILE_NAME`). Existing `[bucketeer]` sections on
    users' computers are left alone; the user guide tells people to switch.

For installation and the cluster side, see [Deployment](deployment.md) and
[Admin Ops setup](admin-ops-setup.md).
