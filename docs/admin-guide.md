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

### Products

Bucketeer knows three products: `Tier-3-S3` (on premises), `Archive-S3` and `Cloud-S3`
(AWS). On the Billing tab you can change each product's name, price per TiB-month and the
short text users see. The `Tier-3-S3` price is the price used for the monthly charges.
Defaults: 0, 1 and 18 per TiB-month.

### Chargeback sheet (.xlsx)

Pick a month and click **Download .xlsx** for the sheet finance imports
(`bucketeer-chargeback-YYYY-MM.xlsx`). Sheet 1 has one row per bucket:

| Column | Value |
|---|---|
| A Username / Email | the owner's email address from AD (the S3 user id when none is found) |
| B Index | the bucket's budget number (empty when missing or not in the right format) |
| C Product Name | `Tier-3-S3` for on-premises buckets |
| D Quantity | the month's daily average **quota** in TiB, rounded up |
| E, F Order / Fulfillment Date | the first and last day of the month |
| G Note, I Reference ID | the bucket name (`tenant:bucket` with a tenant) |
| H Order, J Project Name | empty |

How the quantity is worked out: Bucketeer also records each bucket's quota every day. The
quantity is the sum of the daily quotas in TiB divided by the days in the month, rounded
up. Days without a reading count 0. Example: 10 TiB for 20 days, then 40 TiB for 10 days
of a 30-day month = (200 + 400) / 30 = 20 TiB. A bucket without a quota is billed on its
measured size instead.

The second sheet, **Problems**, lists what needs chasing: buckets without a budget number
or with one in the wrong format, owners without an email address, and buckets without a
quota.

### Monthly email

Enter one or more **billing contacts** (up to 10 addresses), the **send day** (1-28) and
tick **Email last month's sheet**. On that day at 06:00 server time Bucketeer emails last
month's sheet to the contacts. If the server was down then, it sends at the next start; it
never sends the same month twice. A failed send is retried every hour. Switching the email
on after this month's send day starts with next month's.

The email needs Bucketeer's email to be on (`SMTP_FROM` and `APP_URL`, see the deployment
guide); while it is off, the tab says so and the email can't be switched on. **Send now**
mails the chosen month at once, for testing. Every send or failure is in the audit log
(`billing.report_sent`, `billing.report_failed`).

## Other tabs

| Tab | What it does |
|---|---|
| Users | Quotas, suspend and restore. Suspending a user also suspends their bucket keys. |
| Buckets | Usage, bucket quotas, budget numbers (with a check against the format), chargeback CSV. |
| Bucket keys | All keys scoped to one bucket; revoke them. Admins never see a secret. |
| Audit | Who did what and when; filter and export CSV. |
| Offboarding | Daily check against AD. Starts in report-only mode; switch to enforce when you trust it. |
| Settings | Bucket naming rules, budget number format, placements, default quota, uid mappings, notification endpoints. |

For installation and the cluster side, see [Deployment](deployment.md) and
[Admin Ops setup](admin-ops-setup.md).
