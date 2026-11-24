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
| Buckets | Usage, bucket quotas, budget numbers (with a check against the format), chargeback CSV. |
| Bucket keys | All keys scoped to one bucket; revoke them. Admins never see a secret. |
| Audit | Who did what and when; filter and export CSV. |
| Offboarding | Daily check against AD. Starts in report-only mode; switch to enforce when you trust it. |
| Settings | Bucket naming rules, budget number format, placements, default quota, uid mappings, notification endpoints. |

For installation and the cluster side, see [Deployment](deployment.md) and
[Admin Ops setup](admin-ops-setup.md).
