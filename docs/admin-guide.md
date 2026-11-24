---
title: Admin guide
description: Users, buckets, quotas, offboarding, billing and backups for Bucketeer admins.
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
<figcaption>Users, buckets, bucket keys, audit, offboarding, billing, backup and settings.</figcaption>
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
example the creation date of a bucket made outside Bucketeer) are left out. When an
owner sets a budget number on such a bucket, Bucketeer still does not write a creator or
creation date for it: whatever the bucket already has stays.

## Rebuild after losing the database { #rebuild }

If `data/bucketeer.sqlite3` is lost or damaged and there is no backup, rebuild what the
cluster still knows. Stop the service, then run from the app directory:

```bash
.venv/bin/python3 app.py --rebuild-from-ceph            # full: only into an empty database
.venv/bin/python3 app.py --rebuild-from-ceph --fill-gaps # add only what is missing
```

The full rebuild refuses a database that already has buckets, bucket keys or shares;
`--force` runs it anyway. Neither mode changes or deletes a row that exists. Each
bucket is read with its owner's own key, and the owner always comes from Ceph, never
from a tag. The command prints what it added, what it skipped and any problems, exits
with 1 if it could not run, and writes `system.rebuild` to the audit log. One bucket that
can't be read never stops the run.

What comes back:

| Table | From |
|---|---|
| Buckets | the `bucketeer-*` tags: budget number, creator, creation date. A bucket without them gets no row, as before Bucketeer knew it. |
| Bucket keys | the key users (`owner+label`) and their `bucketeer-key-<id>` policy statements: same id, owner (from the key user's display name), bucket, access, the public half of the key, suspended or not. |
| Shares | the `bucketeer-share-*` policy statements whose person still has an S3 account. |

A tag owner that differs from Ceph's is reported, and that bucket's tags are rewritten
at the owner's next visit.

What doesn't come back: the audit log, settings and uid mappings, user profiles (default
budget numbers, Advanced mode), revoked keys and shares (so a deleted key's label can be
used again), offboarding runs, usage readings for billing, bucket delete jobs and setup
links. Shares come back only for people whose AD name equals their S3 user name or who
have a uid mapping; others are listed as problems
(set their uid mapping, then run `--fill-gaps` again).

## Backup

Every night Bucketeer writes its database and its `.env` file to a backup bucket on the
cluster. The **Backup** tab shows that bucket, its object lock and the last backup, sets the
**backup time** (default 03:00 server time) and the **retention** (default 30 days), starts
a backup with **Back up now** and lists the last 30 runs.

**Turning it on.** Set `BACKUP_BUCKET` in `.env` (with appmotel: `appmo env bucketeer`) and
restart. While it is empty, backups are off, and the log and the Backup tab suggest a fresh
name such as `bucketeer-3f9a1c0b7e2d`. Bucketeer never writes `.env` itself. On the first
run it creates:

- the RGW user `bucketeer-backup`, which owns the bucket. It is not a person: it is hidden
  from the admin lists, billing and offboarding, and nobody can log in as it, map to it or
  share with it. Its key is read from the cluster at each run and never stored.
- the bucket, with **object lock in COMPLIANCE mode**, versioning, and lifecycle rules that
  delete a backup the day after its retention ends.

**What a backup holds.** One archive per run,
`backup/bucketeer-YYYY-MM-DDTHHMMZ.tar.gz` (UTC): a consistent copy of the SQLite database,
the live `.env` file and `meta.json` (version, schema version, row counts, SHA-256 of each
file). A successful run also removes the database copies Bucketeer keeps before an upgrade.

Keep in mind:

- **The archive is not encrypted.** `.env` holds every secret (session secret, LDAP and
  Admin Ops passwords and keys), so the bucket must stay private: only `bucketeer-backup`
  can read it. Don't share it, add a policy or give anyone that user's key.
- **Rotated secrets stay in old backups** until those roll off (30 days by default), because
  locked objects cannot be deleted early.
- **Retention can only be lengthened.** A longer period locks the backups written from then
  on; older backups keep the lock they were written with.
- **Through S3 nobody can delete a backup early**, not even Bucketeer. A Ceph administrator
  with `radosgw-admin` (or Admin Ops `purge-objects`) can most likely still remove locked
  objects; that is a cluster check.

### Restore

Stop Bucketeer, then run in its folder:

```bash
.venv/bin/python3 app.py --restore-backup latest
# or a given archive:
.venv/bin/python3 app.py --restore-backup backup/bucketeer-2026-11-24T0300Z.tar.gz
```

It downloads the archive with the backup user's key, checks the SHA-256 of every file and
refuses to run while Bucketeer still answers on its port (`--force` overrides that). Then it:

1. keeps the current database as `data/bucketeer.sqlite3.replaced-<time>` and puts the
   backed-up one in its place;
2. opens it, which upgrades an older schema automatically (with a copy taken first);
3. writes the backed-up `.env` next to the live one as `.env.restored-<time>` (mode 0600),
   never over it: compare the two and copy what you need by hand;
4. adds buckets, keys and shares created after the backup from the cluster, when this
   version has that catch-up (otherwise it says so);
5. records `system.restore` in the restored audit log.

A restore cannot bring back what happened after the backup was written: **audit entries,
usage readings for billing and admin settings changes** made since then are lost.

## Other tabs

| Tab | What it does |
|---|---|
| Users | Quotas, suspend and restore. Suspending a user also suspends their bucket keys. |
| Buckets | Usage, bucket quotas, budget numbers (with a check against the format), chargeback CSV, Write Bucketeer tags. |
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
