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

### Bucket quotas

Every bucket has a quota in whole TiB (at least 1). Owners choose it when they create a
bucket and change it later with **More, Change quota**; both dialogs show the monthly cost
from the `Tier-3-S3` price. On the **Settings** tab, **Bucket quotas** sets:

- **Default bucket quota** (1 TiB unless changed): pre-filled in the create dialog.
- **Weekly increase limit per person** (`maxWeeklyQuotaIncreaseTiB`, 100 TiB unless
  changed): the most quota one person may add across all their buckets in any 7 days
  (rolling). A new bucket counts its starting quota; lowering a quota is always allowed and
  gives nothing back. Over the limit, users see how much they can still add and when.

Admins are not limited, and can set any bucket's quota on the **Buckets** tab (recorded,
never counted against the owner). Changes are audited (`bucket.quota`,
`admin.bucket.quota`, `admin.settings.bucket_quota`).

The account-wide user quota is no longer used: new S3 accounts get an unlimited user quota,
and once after the upgrade Bucketeer sets known people whose user quota was still the old
default (1 TiB, or what the old "Default quota" setting said) to unlimited, audited as
`rgw.user.quota_unlimited`. User quotas an admin had set to something else are left alone.
Bucket keys keep their zero user quota.

### Chargeback sheet (.xlsx)

Pick a month and click **Download .xlsx** for the sheet finance imports
(`bucketeer-chargeback-YYYY-MM.xlsx`). Sheet 1 has one row per bucket:

| Column | Value |
|---|---|
| A Username / Email | the owner's email address from AD (the S3 user id when none is found) |
| B Index | the bucket's budget number (empty when missing or not in the right format) |
| C Product Name | `Tier-3-S3` for on-premises buckets; `Archive-S3` or `Cloud-S3` for cloud buckets |
| D Quantity | on premises: the month's daily average **quota** in TiB, rounded up; cloud: the real AWS cost in **cents** |
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
quota. With [cloud billing](#cloud-billing) it also lists, each with its amount: cloud
buckets Bucketeer can't place (no `bucketeer-owner` tag, or an unknown
`bucketeer-product`), Bucketeer's own system costs (the billing and backup buckets), S3
costs without a bucket name (taxes, account-level charges), and a note while the month is
not final yet or the billing export can't be read.

### Monthly email

Enter one or more **billing contacts** (up to 10 addresses), the **send day** (1-28) and
tick **Email last month's sheet**. On that day at 06:00 server time Bucketeer emails last
month's sheet to the contacts. If the server was down then, it sends at the next start; it
never sends the same month twice. A failed send is retried every hour. Switching the email
on after this month's send day starts with next month's.

The email needs Bucketeer's email to be on (`SMTP_FROM` and `APP_URL`, see the deployment
guide); while it is off, the tab says so and the email can't be switched on. With cloud
billing set up, the email waits until **the 5th** (a send day of 1-4 counts as 5), because
AWS finishes a month's costs only a few days after it ends. **Send now**
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
| Cloud buckets | only when cloud is configured: every `<CLOUD_BUCKET_PREFIX>*` bucket in the AWS account except the backup and billing buckets, from its `bucketeer-*` tags read by Bucketeer's AWS admin user: owner, creator, creation date, budget number, quota, and the storage class from the bucket's `bucketeer-class` lifecycle rule (else its product tag). Only the admin user can write tags on cloud buckets, so here the tags are trusted. A bucket without an owner tag gets no row; a missing quota tag becomes the default quota and is listed as a problem. |

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
cluster and, once cloud is set up, to a bucket with the same name in AWS (see
[Backups in AWS](#backups-in-aws)). The **Backup** tab shows that bucket, its object lock and the last backup, sets the
**backup time** (default 03:00 server time), the **retention** (default 30 days) and how often
the backup is **restore-tested** (default every 30 days, see [Restore test](#restore-test)),
starts a backup with **Back up now** and lists the last 30 runs. A failed backup emails the
[alert contacts](#alert-emails).

**Turning it on.** Set `BACKUP_BUCKET` in `.env` (with appmotel: `appmo env bucketeer`) and
restart. While it is empty, backups are off, and the log and the Backup tab suggest a fresh
name of the form `<CLOUD_BUCKET_PREFIX>backup-<12 hex digits>`, for example
`bucketeer-backup-3f9a1c0b7e2d`. Bucketeer never writes `.env` itself. On the first run it
creates:

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

### Backups in AWS

Once cloud is set up (`CLOUD_ADMIN_*` in `.env`), every run writes the archive to **two
places at once**: the backup bucket on premises and a bucket in AWS with **the same name**,
`BACKUP_BUCKET`. The name must be `<CLOUD_BUCKET_PREFIX>backup-` followed by 12 lowercase
hex digits (Bucketeer refuses to start otherwise), so the same name can be used on both
sides and recognised as Bucketeer's own. Each copy is tried on its own: the run is shown as
**failed** when either copy fails (the Backup tab says which place failed), and the copy that
succeeded is kept. The location switch no longer changes where backups go.

If the AWS copy fails because the name is already taken by another AWS account, pick another
name of that form and set it as `BACKUP_BUCKET` in `.env` (then restart). Bucketeer's
database keeps the backups written under the old name, so they stay where they were.

On the first run Bucketeer's AWS admin user creates:

- the bucket named `BACKUP_BUCKET` in `CLOUD_REGION`. Like on premises it has **object lock in
  COMPLIANCE mode** with the same retention, versioning and lifecycle rules, plus Block
  Public Access, SSE-S3 encryption at rest and the tags `bucketeer-bucket`,
  `bucketeer-product=system` and `bucketeer-location=cloud`. In AWS not even the root user
  can remove a COMPLIANCE-locked object early.
- the IAM user `<CLOUD_USER_PREFIX>bucketeer-backup` under `/bucketeer/`, with the
  `BucketeerUserBoundary` boundary from `tools/aws-setup` and an inline policy that only
  allows writing, reading and listing that one bucket. A separate user is needed because
  Bucketeer's admin user has an explicit Deny on reading and writing objects. An AD account
  named `bucketeer-backup` (or `bucketeer-admin`) never gets cloud storage, so no person
  can end up as this user.

**Keys are made per run.** AWS shows a secret only once, and Bucketeer never stores one. So
each backup (and each restore) deletes any key the backup user still has (left by a crash),
creates a fresh key, uses it for that one upload or download and deletes it afterwards, also
when the run fails. In IAM you will normally see the backup user with no access key.

### Restore test { #restore-test }

A backup is only worth something if it can be restored. The **Restore test** card on the
Backup tab proves that the **newest** archive in each place (on premises and, with cloud, in
AWS) can be restored, without touching anything live:

1. it downloads the archive into a temporary folder inside `DATA_DIR` (for AWS with a
   temporary key of the backup IAM user, exactly like a backup run);
2. checks the SHA-256 of every file against `meta.json`;
3. opens the database copy read-only and runs SQLite's integrity check, checks that its schema
   is not newer than this version of Bucketeer and that the tables and row counts match
   `meta.json`;
4. deletes the folder. The live database and `.env` are never read or written.

The card shows the last result for each place (archive, schema version, tables and rows, or
the error) and when the next automatic test runs. **Test restore now** runs one at once.
Automatically, a test runs right after a successful nightly backup when the last test is at
least **Restore test every** days old (default 30; `0` turns the automatic test off), and
again the night after a test failed. A failed test emails the alert contacts. Each place's
result is recorded in the audit log (`backup.restore_test`, `backup.restore_test_failed`).

### Alert emails { #alert-emails }

The **Alert emails** card on the Settings tab holds up to 10 addresses (a team list works
well) that Bucketeer emails when something needs an administrator:

- a backup run failed in at least one place (each place and its error);
- a restore test failed;
- no backup succeeded for more than 48 hours while backups are on (at most one email a day
  until a backup succeeds again);
- an offboarding run was aborted (AD looked wrong, nobody was suspended) or failed.

The emails use the same mail settings as the share invitations (`SMTP_*`). While email is off
(`SMTP_FROM` empty) or no address is set, alerts are only written to the server log as
warnings. Alerts never contain a key or password. Each email is recorded in the audit log as
`admin.alert_sent` (or `admin.alert_failed` when no mail server took it); the card shows the
last alert.

### Restore

Stop Bucketeer, then run in its folder:

```bash
.venv/bin/python3 app.py --restore-backup latest
# or a given archive:
.venv/bin/python3 app.py --restore-backup backup/bucketeer-2026-11-24T0300Z.tar.gz
# choose the place (default: on premises):
.venv/bin/python3 app.py --restore-backup latest --from cloud
# an older on-premises bucket (archives written before BACKUP_BUCKET was renamed):
.venv/bin/python3 app.py --restore-backup latest --backup-bucket bucketeer-backup-0123456789ab
```

From AWS it needs the `CLOUD_*` settings and takes the bucket name from `BACKUP_BUCKET`. If
that is not set (a new machine) it finds the single `<prefix>backup-*` bucket in the account,
or you name it with `--aws-bucket NAME`. It uses a temporary key of the backup IAM user
exactly like a backup run.

On premises it reads `BACKUP_BUCKET`, or the bucket named with `--backup-bucket NAME`. Use
that after renaming `BACKUP_BUCKET`: the older archives stay in the previous bucket, and while
cloud is set up Bucketeer refuses an old-style name in `.env`, so the option is the only way to
reach them. It cannot be combined with `--from cloud` (use `--aws-bucket` there).

It downloads the archive with the backup user's key, checks the SHA-256 of every file and
refuses to run while Bucketeer still answers on its port (`--force` overrides that). Then it:

1. keeps the current database as `data/bucketeer.sqlite3.replaced-<time>` and puts the
   backed-up one in its place;
2. opens it, which upgrades an older schema automatically (with a copy taken first);
3. writes the backed-up `.env` next to the live one as `.env.restored-<time>` (mode 0600),
   never over it: compare the two and copy what you need by hand;
4. adds buckets, keys and shares created after the backup from the cluster, when this
   version has that catch-up (otherwise it says so), and prints a short summary of it;
5. records `system.restore` in the restored audit log (with the catch-up's counts and at
   most 50 of its problems).

A restore cannot bring back what happened after the backup was written: **audit entries,
usage readings for billing and admin settings changes** made since then are lost.

## Other tabs

| Tab | What it does |
|---|---|
| Users | Quotas, suspend and restore. Suspending a user also suspends their bucket keys. |
| Buckets | On-premises and cloud buckets (filter All / On premises / Cloud; cloud rows have a "Cloud" badge and their storage class). Usage, bucket quotas, budget numbers (with a check against the format), chargeback CSV, Write Bucketeer tags. A cloud bucket's size comes from AWS CloudWatch (daily, a day or two late; empty while AWS can't be reached) and its quota is in whole TiB; an administrator's quota change is not limited by the weekly limit but is recorded, and the bucket's quota tag is updated. |
| Bucket keys | All keys scoped to one bucket; revoke them. Admins never see a secret. |
| Audit | Who did what and when; filter and export CSV. |
| Offboarding | Daily check against AD. Starts in report-only mode; switch to enforce when you trust it. An aborted or failed run emails the alert contacts. |
| Settings | Default location (On Premises or Cloud), [alert emails](#alert-emails), bucket naming rules, budget number format, placements, bucket quotas (default bucket quota and weekly limit), uid mappings, notification endpoints. |

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
`CLOUD_REGION`. See `tools/README.md` for details. The script is safe to run again with the
same arguments (and `--billing-bucket` set to your `CLOUD_BILLING_BUCKET`): it gives the
policies a new version. Do that after updating Bucketeer when a release changes the admin
policy (for example the read-only `LookUpPersonUsersByName` statement, without which a
person's first cloud key or setup command failed with "AWS refused the request
(AccessDenied)"). If creating a cloud bucket fails with AccessDenied, the policy was made
with another `--prefix` than `CLOUD_BUCKET_PREFIX` (the log says which prefix to use):
re-run the script with the matching `--prefix`.

**2. Put the lines into `.env`** (`appmo env bucketeer`) and restart:

| Variable | Meaning |
|---|---|
| `CLOUD_REGION` | AWS region, default `us-west-2`. |
| `CLOUD_BUCKET_PREFIX` | Every cloud bucket is `<prefix><username>-<name>`; default `bucketeer-`. While cloud is configured, new on-premises bucket names may not start with it. |
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

**4. People's AWS users and keys.** Each person gets their own IAM user,
`<CLOUD_USER_PREFIX><username>`, the first time they ask for a cloud key (or tick the cloud
option of the setup command). Bucketeer creates it under the path `/bucketeer/` with the
permissions boundary `BucketeerUserBoundary` and the tag `bucketeer-owner=<username>`.

- **Existing users with that name are never taken over.** If an IAM user with the name
  already exists but Bucketeer did not make it (another path, no boundary, or no owner tag
  for that person), the person is told to ask an administrator and the audit log has
  `cloud.user.refused`. Remove or rename the old user, then the person can try again.
  Usernames `bucketeer-admin`, `bucketeer-backup`, `backup`, `billing` and `cloud` never get
  cloud.
- **Access comes from each bucket's own policy.** The user's inline policy
  `bucketeer-buckets` only lets them list buckets. Each cloud bucket's bucket policy has a
  `bucketeer-owner` statement naming its owner's IAM user. Bucketeer does not use a name
  wildcard such as `<prefix><username>-*`: usernames may contain hyphens, so `jdoe`'s
  wildcard would also match the buckets of `jdoe-x`.
- **Keys:** at most two per person (AWS's limit). AWS shows a secret only once, so Bucketeer
  shows it once, right after it makes the key, and never stores it. Admins never see it.
- **Suspension:** suspending a person (by hand or by the offboarding job) also sets their
  cloud keys to *Inactive*; nothing is deleted. Unsuspending, or the person's next login
  after an offboarding suspension, makes exactly those keys active again. If AWS can't be
  reached, the suspension still happens on premises and the failure is shown (502 on the
  Users tab, `partial` in the offboarding report); the next enforce run finishes the keys.

**5. Cloud buckets.** People create them on the bucket card while they work in Cloud. Each
is `<CLOUD_BUCKET_PREFIX><username>-<name>` in `CLOUD_REGION` (dots, `_`, `@` and `+` in the
username become hyphens; usernames `backup`, `billing` and `cloud`, or starting with
`backup-`, `billing-` or `cloud-`, get no cloud buckets, since those names belong to the
backup and billing buckets). Bucketeer's admin user creates each bucket and sets Block Public Access, SSE-S3
default encryption, *Bucket owner enforced*, versioning (on by default, old versions expire
after 30 days), a lifecycle rule `bucketeer-class` that moves every object to
Intelligent-Tiering or Glacier Deep Archive on day 0, a rule that aborts unfinished uploads
after 7 days, the owner's `bucketeer-owner` policy statement and the `bucketeer-*` tags
(owner, owner email, budget, product, quota, location `cloud`). If any step fails, the bucket
is deleted again (audit `cloud.bucket.create_failed`). The quota is kept in Bucketeer's
database and the `bucketeer-quota` tag (AWS has no bucket quota) and counts toward the same
weekly limit as on premises. Once a day Bucketeer reads each bucket's size from CloudWatch;
a bucket over its quota gets an email to its owner, at most once a week
(`cloud.bucket.over_quota`; with email off only the audit entry). Deleting a cloud bucket
needs its typed name; one with data is emptied by a background job that uses a temporary
key of the owner's IAM user (deleted again afterwards; a person with two keys is asked to
delete one first). Audit: `cloud.bucket.create`, `quota`, `budget`, `delete`,
`delete_started`, `delete_failed`, `delete_retry`.

!!! note "Profile names changed"
    The on-premises profile on users' computers is now `s3-onprem` (`S3_PROFILE_NAME`; it
    was `bucketeer`) and cloud keys use `s3-cloud` (`CLOUD_PROFILE_NAME`). Existing `[bucketeer]` sections on
    users' computers are left alone; the user guide tells people to switch.

### Cloud billing { #cloud-billing }

Cloud buckets are billed at their real AWS cost: storage per tier, requests, retrievals,
transitions, early-deletion fees and transfer. AWS writes these costs into a daily **data
export** (CUR 2.0) in a billing bucket of the account, and Bucketeer sums them per bucket
name. No cost allocation tags or Cost Explorer access are needed (a member account of an
AWS Organization doesn't get them).

1. An AWS administrator runs the setup again with `--billing-export`. It creates the
   billing bucket `<prefix>cloud-billing-<random>` (used for cost data only; public access
   blocked, a policy that lets only AWS billing for this account deliver into it), the
   export `<prefix>s3-costs`, and
   lets Bucketeer's admin user read that one bucket (and still no other objects):

    ```bash
    tools/aws-setup --admin-user <name> --user-prefix <prefix> --billing-export
    ```

2. Put the printed `CLOUD_BILLING_BUCKET=<bucket>` into `.env` and restart.
3. AWS delivers the first export within about 24 hours and refreshes it daily. Until then
   the Billing tab says that no export exists yet.

On the **Billing** tab, under the month picker, Bucketeer shows for that month the
export's S3 total for the cloud buckets next to the sum of the sheet's cloud rows, what
is not billed (see the Problems sheet), the system and unassigned costs, whether the month
is **final** (from the 5th of the next month) and when AWS generated the export. Bucketeer
reads the export at most once a day per month, and never again once a month is final.
Each cloud row's product, owner email and budget number come from the bucket's
`bucketeer-*` tags; a bucket deleted during the month is billed on the tags Bucketeer saw
last.

For installation and the cluster side, see [Deployment](deployment.md) and
[Admin Ops setup](admin-ops-setup.md).
