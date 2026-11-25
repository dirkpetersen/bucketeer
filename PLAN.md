# Plan: next features (draft, 2026-11-24)

Implemented in TODO Phase 6 (2026-11-25); CLAUDE.md is authoritative.

Requested by the owner on 2026-11-24. Decisions taken the same day are marked
**(decided)**. Items marked **(Q)** are still open (list at the end); the proposal next
to them is what gets built unless the owner says otherwise. Once work starts, decisions
move to CLAUDE.md and the work into TODO.md as reviewable items.

Shared goal: if `data/bucketeer.sqlite3` is lost, the important facts must still be in
Ceph (or AWS), and a daily backup must hold everything else.

---

## 0. Service catalog (decided)

Bucketeer offers three storage products. Each bucket is exactly one of them, chosen at
creation and never changed afterwards.

| Product (sheet column C) | Where | Storage class | Advertised price | Location tag |
|---|---|---|---|---|
| `Tier-3-S3` | on premises (Ceph) | Ceph placement (as today) | admin price per TiB-month | `onprem` |
| `Archive-S3` | AWS, us-west-2 | Glacier Deep Archive | **about $1 per TiB-month** | `cloud` |
| `Cloud-S3` | AWS, us-west-2 | S3 Intelligent-Tiering | **about $13-18 per TiB-month** | `cloud` |

- The product name, price per TiB-month and advertised text of each product are admin
  settings (Billing tab). The values above are the defaults. Changes are audited.
- **How each product is billed (decided):**
  - `Tier-3-S3` (on premises) is billed on the **quota**: users pay for the space they
    reserve, at the admin's price per TiB-month.
  - `Archive-S3` and `Cloud-S3` pass through the **real AWS cost**, read per bucket from
    Cost Explorer through cost allocation tags (`bucketeer-bucket`). That includes
    storage, requests, retrieval, egress and Deep Archive's 180-day minimum. The quota
    still limits growth (weekly limit, over-quota alert), but it is not what is billed.
- Whenever a user picks or changes a quota, the dialog shows the monthly cost live:
  "10 TiB x $5 = **$50 per month**" on premises, and an estimate from the advertised price
  for cloud: "about $10 per month if full" (Archive-S3) or "about $130-180 per month if
  full" (Cloud-S3), with a note that the real AWS cost is billed.

## 1. Bucket facts in Ceph / AWS (tags and quota)

Each bucket carries its own record as `bucketeer-*` tags, written with the owner's key
through `s3docs.update_bucket_tagging`, like `bucketeer-budget` today. Cloud buckets get
the same tags in AWS.

| Tag | Value | Example | Notes |
|---|---|---|---|
| `bucketeer-budget` | budget number | `DN002480 AC02619` | exists already |
| `bucketeer-bucket` | bucket name (`tenant:bucket` with a tenant) | `bucketeer-jdoe-scans` | the cost allocation key: one value per bucket, so Cost Explorer reports per bucket |
| `bucketeer-owner` | owner uid; `tenant:uid` with a tenant | `jdoe`, `acme:jdoe` | `$` isn't allowed in tag values |
| `bucketeer-owner-email` | owner's AD `mail` | `Jane.Doe@university.edu` | column A of the sheet; refreshed on reconcile |
| `bucketeer-creator` | AD username of the creator | `jdoe` | differs from the owner only when an admin creates it |
| `bucketeer-created` | creation date, UTC | `2026-11-24` | |
| `bucketeer-quota` | quota in whole TiB | `10` | readable copy of the quota; see below |
| `bucketeer-product` | product name | `Archive-S3` | |
| `bucketeer-location` | `onprem` or `cloud` | `cloud` | |

- **Quota record:** on premises, RGW enforces the bucket quota (Admin Ops
  `PUT /admin/bucket?quota`), so Ceph holds it. AWS has no bucket quota, so for cloud
  buckets the tag is the copy that survives outside SQLite. The UI and the billing sheet
  read RGW (on prem) or SQLite (cloud). The tags only rebuild what is lost.
- **Tags are a record, not proof.** Owners can rewrite their tags with their own keys.
  Nothing that decides access trusts a tag. A reconcile rewrites tags that differ.
- Limits: tag values are at most 256 characters, from `[\w +-=.:/@]`; anything else is
  left out, with a log warning. 9 managed tags leave 41 for users.
- **Existing buckets:** a one-time admin action ("Write Bucketeer tags to all buckets")
  plus the regular reconcile.
- **Restore from Ceph/AWS:** `python3 app.py --rebuild-from-ceph` rebuilds `buckets`
  (owner, creator, created, budget, quota, product) from tags, `bucket_keys` from the key
  users and their policy statements, and `shares` from the `bucketeer-share-*`
  statements.
- **Key-user display names** hold owner, bucket and access
  (`bucketeer key: jdoe / reports / rw`), so bucket keys can be rebuilt too.

## 2. Users set bucket quotas themselves

- Every bucket has a quota in **whole TiB** (minimum 1), set on create (required, default
  1 TiB) and changeable later ("Change quota…"). The monthly cost is shown live while
  choosing (feature 0).
- **Weekly limit per person (decided: 100 TiB):** the sum of all quota **increases** a
  person made across all their buckets in the last 7 days (rolling) may not exceed
  `maxWeeklyQuotaIncreaseTiB`. This is an admin setting, default 100. Decreases are always
  allowed and give nothing back. A new bucket's starting quota counts as an increase.
  On-prem and cloud count together.
- Over the limit: 409 with how much is left and when it frees up ("You can add 40 TiB
  more on Dec 1"). Admins are not limited and can change any bucket's quota (audited).
- **The account-wide user quota goes away (decided):** Bucketeer sets the RGW user quota
  to unlimited (new accounts and, once, existing ones). The bucket quotas plus the weekly
  limit replace it. The "Default quota" admin setting becomes "Default bucket quota".
  Bucket keys keep their zero user quota, since that is about where they may store data,
  not about billing.
- Increases are recorded in `quota_changes` (person, bucket, old, new, time) and audited
  `bucket.quota`. Losing that table only resets the rolling week.
- Cloud buckets: AWS can't enforce a quota. A daily size check (CloudWatch) emails the
  owner only (decided; not the billing contact) when a cloud bucket goes over its quota,
  at most once a week per bucket while it stays over. The excess is billed anyway, since
  cloud passes through the real cost.

## 3. Monthly billing spreadsheet and email

### Sheet format (.xlsx)

One row per bucket per month:

| Col | Header | Bucketeer value |
|---|---|---|
| A | Username / Email | owner's AD `mail` (fallback: the uid, listed on the Problems sheet) |
| B | Index | the bucket's budget number |
| C | Product Name | `Tier-3-S3`, `Archive-S3` or `Cloud-S3` |
| D | Quantity | `Tier-3-S3`: the month's **daily average quota in TiB, rounded up** (decided); `Archive-S3`/`Cloud-S3`: the month's AWS cost **in cents** (unit price $0.01, decided) |
| E | Order Date | first day of the month, real Excel date, `M/D/YYYY` |
| F | Fulfillment Date | last day of the month, `M/D/YYYY` |
| G | Note | bucket name (`tenant:bucket` with a tenant) |
| H | Order | empty |
| I | Reference ID | bucket name |
| J | Project Name | empty |

- File `bucketeer-chargeback-YYYY-MM.xlsx`, new dependency `openpyxl` (added to
  `requirements.txt` and `docs/stack.md`). Every cell goes through `csv_safe`, since
  formula injection works in xlsx too.
- **Cloud rows: AWS Data Export, by bucket name, no tags needed.** A CUR 2.0 data export
  (`bcm-data-exports`, daily granularity, `INCLUDE_RESOURCES`, gzip CSV, overwritten
  each refresh) is delivered by AWS to a billing bucket in our account
  (`<prefix>billing-<random>`). For S3, `line_item_resource_id` is the **bucket name**, so
  Bucketeer sums `line_item_unblended_cost` per bucket for the month, filtered to
  `line_item_product_code = AmazonS3` and buckets with our prefix. That covers storage per
  tier, requests, retrievals, transitions, Deep Archive's early-deletion fees and egress:
  a real pass-through.
  - AWS finalizes a month a few days after it ends, so the sheet for month M is built no
    earlier than the 5th of M+1 (the send day is at least 5 while cloud is set up).
  - S3 lines without a resource id, and lines for our prefix whose bucket Bucketeer
    doesn't know, go on the Problems sheet with their amount, never silently dropped.
    The Billing tab shows the export's S3 total for our buckets next to the sum of the
    rows, so a gap is visible.
  - The `bucketeer-*` cost allocation tags stay useful for people reading Cost Explorer,
    but billing no longer depends on them. Asking the platform team to activate them is
    optional.
  - Reading the export: Bucketeer's AWS admin user gets `GetObject`/`ListBucket` on the
    billing bucket only (the "no object access" Deny gets a `NotResource` exception for
    it), and `tools/aws-setup` creates the billing bucket, its policy for
    `bcm-data-exports.amazonaws.com` and the export itself.
- The quota for the month comes from a daily quota reading per bucket (next to the
  existing size reading in `usage_daily`): the sum of the daily quotas in TiB divided by
  the days in the month, rounded up to a whole TiB. Days without a reading (bucket not
  yet created, or already deleted) count as 0. Example: 10 TiB for 20 days of a 30-day
  month, then 40 TiB for 10 days = (200 + 400) / 30 = 20 TiB.
- Buckets without a valid budget number still get a row with an empty Index. A second
  sheet "Problems" lists them (and owners without an email) so they can be chased.
- Billing tab: pick a month and download the .xlsx. The CSV exports stay. Audit
  `admin.export.billing_xlsx`.

### Billing contact and monthly email

- Billing tab settings: **billing contact** (one or more email addresses), **send day**
  (1-28, default 1), **send on/off**. Audit `admin.settings.billing`.
- On the send day at 06:00 server time, Bucketeer emails last month's sheet as an
  attachment to the contact. The month sent is recorded, so a restart never sends twice,
  and a missed day (server down) is caught up at the next start. Audit
  `billing.report_sent` / `billing.report_failed` (recipients and month, not the content).
- Needs email to be on (`SMTP_FROM`, `APP_URL`). While it is off, the tab says so and
  sending can't be switched on. A "Send now" button, with a month picker, is for testing.

## 4. Cloud (AWS)

### Where the switch is (decided)

- **Global setting (admin):** "Default location: On Premises / Cloud", default On
  Premises. Cloud can only be chosen once the AWS account is set up and its connection
  check passes.
- **Per user:** with Advanced on, a **Cloud / On Premises** switch sits to the left of
  the Advanced toggle in the header, starting at the global default. With Advanced off,
  the user sees the global default only. So once an admin sets Cloud, everyone works in
  Cloud by default, Advanced or not. The user's choice is remembered like `advancedMode`.
- The switch picks which buckets the bucket list shows and where new buckets go. Keys
  and setup are shown for both locations.

### AWS side (decided)

- One shared AWS account. Bucketeer's own identity is the IAM user
  **Bucketeer's AWS admin user** (decided; its name is chosen by the operator), set up by **`tools/aws-setup`** (run once by an AWS
  administrator; idempotent; `--print` shows the policies, `--create-key` prints the
  `.env` lines once, `--activate-tags` turns on the cost allocation tags). `.env`:
  `CLOUD_REGION` (default `us-west-2`), `CLOUD_BUCKET_PREFIX` (default `bucketeer-`),
  `CLOUD_USER_PREFIX`, `CLOUD_ADMIN_ACCESS_KEY`, `CLOUD_ADMIN_SECRET_KEY`. What the script grants:
  - **IAM:** person users `s3-*` under path `/bucketeer/` only, and only created with the
    permissions boundary `BucketeerUserBoundary` (object access to `<prefix>*` buckets,
    nothing else). So even a stolen admin key can't create a user with wider rights.
    The admin user lives under `/bucketeer-admin/`, outside what it can manage, and
    an explicit Deny stops it from removing boundaries, attaching or editing policies, or
    creating console logins.
  - **Buckets:** create, delete, tags, versioning, lifecycle, bucket policy, encryption,
    public access block, ownership controls, object lock (backup bucket) and
    Intelligent-Tiering configuration, on `<prefix>*` buckets only. **No object read or
    write** (explicit Deny): user data is only reached with the person's own key, and
    backups with the backup user.
  - **Monitoring:** CloudWatch S3 metrics (`BucketSizeBytes`, `NumberOfObjects`, daily, per
    storage class), used for the over-quota check.
  - **Billing:** Cost Explorer read (cost and usage, per tag, forecast) and cost
    allocation tags (`bucketeer-bucket`, `-owner`, `-budget`, `-product`, `-location`):
    the real AWS cost per bucket is what cloud buckets are billed. In an AWS Organization, tag
    activation has to happen in the management account. Cost Explorer API calls cost
    $0.01 each, so Bucketeer calls it at most once a day.
  - Honest limit: the admin user can still create access keys for person users and
    write bucket policies, so it can reach data indirectly, like the on-prem Admin Ops
    key. It can't escape the boundary or the `<prefix>` buckets.
- AD usernames that would collide with the system users (`bucketeer-admin`,
  `bucketeer-backup`) are refused for cloud.
- **One IAM user per person, named `<CLOUD_USER_PREFIX><username>`** (prefix chosen by the
  operator and passed to `tools/aws-setup --user-prefix`; lowercased sAMAccountName, same
  rules as the RGW uid; created on first use of cloud, like the RGW account on first
  login). Each has its own access keys, managed like the on-prem keys (max 5, rotate,
  reveal). Bucketeer never uses its own AWS identity for user data.
- Each user gets an inline IAM policy for exactly their own buckets (listed by tag
  `bucketeer-owner` and kept in step by Bucketeer). Buckets get Block Public Access, SSE-S3
  encryption, versioning per Q7 and the `bucketeer-*` tags.
- **Bucket names (decided):** AWS names are global and the IAM policies are scoped by a
  prefix, so a cloud bucket is `<CLOUD_BUCKET_PREFIX><username>-<name>`, e.g.
  `bucketeer-jdoe-scans`; the user types only `<name>`. The dialog checks availability
  as they type (`HeadBucket`: 404 = free, 403/200 = taken) and, when it is taken,
  suggests free alternatives (`<name>-2`, `<name>-<year>`, `<name>-<4 random>`), each
  checked before it is shown.
- **Storage class per bucket (decided):** the user picks **Intelligent-Tiering** or
  **Glacier Deep Archive**. A bucket lifecycle rule moves every object to that class on
  day 0, so uploads with any client land there without extra flags. Deep Archive objects
  need a restore before reading; **restores are not part of Bucketeer** (another tool
  does them). The create dialog says so.
- Costs to keep in mind for Deep Archive: AWS charges each object for at least 180 days,
  plus retrieval and egress. The create dialog warns about the 180 days.
- Offboarding suspends cloud users as well (deactivates their access keys) under the same
  safeguards.
- **First version covers:** buckets, keys, quota, tags, billing, setup command.
  Sharing, bucket keys and share links stay on premises only in the first version
  (decided).

### Profiles (decided)

- `S3_PROFILE_NAME` default changes from `bucketeer` to `s3-onprem`. A new
  `CLOUD_PROFILE_NAME` defaults to `s3-cloud`. The setup script and the snippets write
  `[s3-onprem]` and, when the person has cloud keys, `[s3-cloud]` (region `us-west-2`,
  AWS's own endpoint, no `endpoint_url`). Existing `[bucketeer]` sections on users'
  computers are left alone; the docs tell users to switch.

## 5. Backup bucket

- `.env`: `BACKUP_BUCKET` (Default empty = backups off), named `bucketeer-<random>`. With
  an empty value, the log and the admin page suggest a name with a fresh random suffix
  (`secrets.token_hex(6)`), because the app never writes `.env`. The admin page shows the
  bucket and sets the time and retention (decided).
- **Where (decided):** to the location the **global** setting names. On premises by
  default; in AWS (`us-west-2`) once the global default is Cloud. Switching the default
  starts backing up to the new place; old backups stay where they are and roll off.
- **Owner:** on premises, a dedicated RGW user `bucketeer-backup` that Bucketeer creates
  through Admin Ops. Its key is read from Admin Ops each time, so it is never stored, and
  it is hidden from admin lists like the service user. In AWS, a dedicated IAM user
  backup IAM user (created by the admin user under the same boundary) whose
  inline policy only allows `PutObject`/`GetObject`/`ListBucket` on that bucket. Never a
  person's account.
- **Can't be deleted early:** object lock in **COMPLIANCE** mode with a default retention
  of `retentionDays` (default 30), versioning on, plus lifecycle rules: expire current
  objects after retention + 1 day, delete noncurrent versions 1 day after, remove
  expired delete markers. Through S3, nobody can delete or shorten a backup before it
  rolls off, not even Bucketeer.
  - Cluster check: whether Admin Ops `purge-objects` or `radosgw-admin` can still remove
    locked objects (a Ceph admin likely can). In AWS, not even the root user can remove
    COMPLIANCE-locked objects early.
- **Admin settings ("Backup" tab):** shows the bucket and its state (exists, object lock,
  retention, last backup with size), **backup time** (default 03:00 server time),
  **retention days** (default 30; only lengthens retention on existing objects), "Back
  up now", and the last 30 backups. Audit `admin.settings.backup`, `backup.run`,
  `backup.failed`.
- **Contents (decided): the database and `.env`.** One archive per run,
  `backup/bucketeer-YYYY-MM-DDTHHMMZ.tar.gz`, holding a consistent SQLite snapshot
  (`.backup` API), the live `.env` and `meta.json` (app version, schema version, row
  counts, SHA-256 of each file).
- **Not encrypted by Bucketeer (decided, Q21).** `.env` holds every secret, so what
  protects the archive is who can read the bucket:
  - only the backup user (`bucketeer-backup` on premises, the backup IAM user in AWS);
    no person, share, bucket key or public access, and Bucketeer never offers it in the UI
  - in AWS: Block Public Access, SSE-S3 at rest, and the admin user has an
    explicit Deny on object reads
  - on premises, the Admin Ops key can read the backup user's key, but it is itself in
    `.env`, so nobody new gets access
  - consequence of COMPLIANCE lock: after a secret is rotated, the old value stays in
    the backups until they roll off (30 days). The admin guide says so.
- **Restore:** `python3 app.py --restore-backup <object key|latest>` downloads, checks the
  hashes and, while the app is stopped, replaces
  `data/bucketeer.sqlite3`. `.env` is written next to the live one as
  `.env.restored-<time>` (mode 0600), never over it, so an admin compares and copies by
  hand. Documented and tested.

---

## 6. Automatic upgrade of an older database

**Goal (owner, 2026-11-24):** whenever new features add to the SQLite schema, an older
installation (or a restored backup) is detected at startup and brought up to the latest
schema on the fly, with its data kept, without anyone running a command.

**Already in place:** `MIGRATIONS` in `db.py` is an append-only list, the database's
version is `PRAGMA user_version`, and at startup every missing migration is applied in
order, each in one transaction (an installation from v1 goes through v2...v12 in one
start). A database newer than the code is refused. Every feature in this plan adds its
tables and columns this way (v13 onward), never by editing an applied migration.

**What this feature adds on top:**

- **Before migrating**, Bucketeer copies the file to `data/bucketeer.sqlite3.pre-v<old>-
  <time>` (consistent `.backup`, mode 0600) so a failed upgrade can be undone. Kept until
  the next successful backup run, at most 3 copies.
- **Each migration runs in its own transaction** (as today). On failure the app refuses
  to start, names the migration and the error, and leaves the pre-migration copy in
  place. A database newer than the code is still refused (no downgrades).
- **Data migrations, not only schema:** a migration may be a Python step as well as SQL,
  for changes that need code (e.g. filling `bucketeer-*` tags into new columns, splitting
  a setting, converting user quotas to bucket quotas for feature 2). Each one is
  idempotent and tested from every older schema version: a test builds a database at
  each past `user_version` with sample rows (fixtures kept in `tests/db_versions/`),
  upgrades it and checks the rows.
- **Catch-up after a restore:** `--restore-backup` (feature 5) runs the migrations, then
  the reconcile from feature 1 (`--rebuild-from-ceph` in "fill gaps" mode): buckets,
  bucket keys and shares created after the backup are added from Ceph/AWS tags and
  policies; nothing in the restored database is overwritten. The result (rows added per
  table, items it couldn't place) is printed and audited `system.restore`.
- **What can't be caught up** is listed in the admin guide: audit entries, usage
  readings and settings changes made after the backup.

## Order of work (proposal)

1. Bucket tags + `--rebuild-from-ceph` (feature 1). It protects existing data at once.
2. Backup bucket on premises (5).
3. Bucket quotas with the weekly limit, the user quota made unlimited, cost shown live
   (0 and 2).
4. xlsx sheet, billing contact, monthly email (3).
5. Cloud (4), then backups to AWS. Needs the AWS account and its IAM identity first.

Feature 6 (database upgrade) goes in with step 2, since a restore needs it.

## Decisions (owner, 2026-11-24)

1. Billing is on the quota. The monthly cost is shown whenever a user picks a quota.
2. Products: `Tier-3-S3` (on premises) and `Archive-S3` (cloud).
3. The account-wide user quota is dropped in favour of bucket quotas.
4. The weekly increase limit is in TiB: 100 TiB by default.
5. AWS, one shared account, an IAM user per person (prefix plus username), default region
   `us-west-2`. No restore requests in the UI (a different tool does them).
6. The global location setting picks where backups go. Once it is Cloud, Cloud is the
   default for everyone, even with Advanced off.
7. Cloud users choose per bucket between Intelligent-Tiering (`Cloud-S3`, advertised
   $13-18 per TiB-month) and Glacier Deep Archive (`Archive-S3`, $1 per TiB-month).
8. Bucketeer's AWS identity is its own IAM admin user, set up by
   `tools/aws-setup`.
9. `Archive-S3` and `Cloud-S3` pass through the real AWS cost per bucket, through cost
   allocation tags. `Tier-3-S3` stays billed on quota.
10. `Tier-3-S3` quantity is the month's daily average quota in TiB, rounded up.
11. Cloud over-quota alerts go to the bucket owner only.
12. Cloud bucket names are checked for availability. If the name is taken, the dialog
    suggests free alternatives.
13. Accepted proposals: buckets without a budget number stay in the sheet with an empty
    Index plus a Problems sheet; cloud v1 has no sharing, bucket keys or share links;
    `.env` names the backup bucket and the admin page sets time and retention; the cloud
    amount is Quantity in cents at $0.01.
14. Backups hold the database and `.env`.
15. Backups are not encrypted by Bucketeer (no age key).
16. An older database (older installation or restored backup) is upgraded to the latest
    schema automatically at startup, keeping its data (feature 6).
17. Existing IAM users with a per-person name made outside Bucketeer: Bucketeer refuses cloud for that person with "ask an
    administrator" until an admin removes the old user or maps the person to another
    name.
18. Billing for cloud uses the AWS data export by bucket name; the cost allocation tag
    request to the platform team is optional.

## Open questions


None right now.
