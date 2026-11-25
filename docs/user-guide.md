---
title: User guide
description: Log in, set up your computer, and manage your buckets with Bucketeer.
---

<div class="osu-hero" markdown>
<span class="osu-kicker">For users</span>

# User guide

Bucketeer is your self-service door to S3 storage on our Ceph cluster. Log in with your
university account, connect your computer in one step, and create buckets for your data.
</div>

## 1. Log in

Open Bucketeer and log in with your **username or email address** and your normal
password. Your S3 account is created the first time you log in. It has no overall storage
limit: each bucket has its own quota (see "Create a bucket").

<figure class="osu-shot" markdown>
![The Bucketeer login page](assets/screens/login.webp#only-light)
![The Bucketeer login page](assets/screens/login-dark.webp#only-dark)
<figcaption>Username or email address, and your normal password.</figcaption>
</figure>

## 2. Save your budget number

Every bucket is charged to a budget number (an index or worktag, for example `DN099999`).
Until you save a default one, Bucketeer opens your profile first after you log in, and you
cannot create buckets or keys. Type your number, click **Save**, then **Go to my
workspace**. You can still choose another number for each bucket.

<figure class="osu-shot" markdown>
![The profile page asks for a default budget number](assets/screens/profile-first.webp#only-light)
![The profile page asks for a default budget number](assets/screens/profile-first-dark.webp#only-dark)
<figcaption>The profile page, shown first until a default budget number is saved.</figcaption>
</figure>

## 3. Set up this computer

Your workspace has two cards: **Credentials** and **Your buckets**. In **Set up this
computer**, choose your system and click **Create setup command**. Copy the command into a
terminal (PowerShell on Windows) and run it within 15 minutes. It installs your keys as the
AWS profile `s3-onprem`, makes a backup of any file it changes, and tests the connection.
The link works only once.

<figure class="osu-shot" markdown>
![The workspace with the Credentials and Your buckets cards](assets/screens/workspace.webp#only-light)
![The workspace with the Credentials and Your buckets cards](assets/screens/workspace-dark.webp#only-dark)
<figcaption>Your workspace: connect this computer on the left, your buckets on the right.</figcaption>
</figure>

Then use any [S3 client](s3-clients.md). With the AWS CLI:

```bash
aws --profile s3-onprem s3 ls
```

!!! note "Used Bucketeer before? The profile is now `s3-onprem`"
    Earlier versions wrote the profile `bucketeer`. Running the setup command again adds
    `s3-onprem` and leaves an existing `[bucketeer]` section alone, so old scripts keep
    working. Switch them to `--profile s3-onprem` (or `AWS_PROFILE=s3-onprem`), then delete
    the old section from `~/.aws/config` and `~/.aws/credentials` when you no longer need it.

### Cloud (AWS) keys { #cloud-keys }

When your administrator has set up cloud storage and you work in **Cloud**, the
Credentials card also shows **Cloud (AWS)**. Your first cloud key also creates your AWS
user. Cloud keys are separate from your on-premises keys and use the profile `s3-cloud`:

```bash
aws --profile s3-cloud s3 ls
```

- **The secret is shown once.** AWS hands out a secret only when the key is made, and
  Bucketeer does not keep it. Copy it (or the ready-made `~/.aws/credentials` text) right
  away, then click **Done, I saved it**. If you lose it, create a new key or rotate.
- **At most two keys.** AWS allows two keys per user. **Rotate** makes a new key and then
  deletes the old one, so it needs a free slot: with two keys, delete the one you don't use
  first. Your last key can't be deleted, only rotated.
- **Setup command.** Tick **Also set up Cloud (AWS)** in *Set up this computer* (it is ticked
  when you work in Cloud). The command then installs `s3-cloud` next to `s3-onprem` with a
  new AWS key; if you already have two keys, the oldest is replaced. A new AWS key can take
  a minute before it works everywhere, so if the test at the end fails, try again shortly.
- If the card says to **ask an administrator**, an AWS user with your name already exists
  that Bucketeer did not make. An administrator has to remove it first.

## 4. Create a bucket

Click **Create bucket**. Bucket names use lowercase letters, digits and hyphens (no dots).
Versioning is on by default: when you overwrite or delete a file, the old version is kept
for 30 days, so you can get it back.

Every bucket has a **quota** in whole TiB (at least 1; the dialog suggests your admin's
default). It is the most the bucket can hold, and on premises it is also what you pay for:
the dialog shows the monthly cost as you choose, for example "10 TiB x $5 = **$50 per
month**". Old versions count toward the quota.

<figure class="osu-shot" markdown>
![The Create bucket dialog](assets/screens/create-bucket.webp#only-light)
![The Create bucket dialog](assets/screens/create-bucket-dark.webp#only-dark)
<figcaption>Name, budget number and versioning for a new bucket.</figcaption>
</figure>

## 5. Everyday tasks

Each bucket row has the tasks you need most. Rare ones are in the **More** menu.

| Task | Where |
|---|---|
| Share a bucket with a colleague (read-only or read-write) | **Share** on the bucket row. They see it under **Shared with you** (and get an email when the portal sends mail). |
| Send one file to anyone, for up to 7 days | **More, Share link** |
| A key for one bucket only (for a script or instrument) | **More, Keys** |
| Delete old files or old versions automatically | **More, Lifecycle** |
| Change the budget number | **More, Change budget number** |
| Make a bucket bigger or smaller | **More, Change quota**. The monthly cost is shown as you choose. |
| Delete a bucket | **More, Delete**. A bucket with data needs you to type its name. |

### Quota and the weekly limit

Each bucket row shows its quota and what it costs per month. Lowering a quota always works
(below the data already stored, the bucket simply accepts no more writes). Raising quotas is
limited per person: all the quota you add across your buckets in any 7 days, new buckets
included, may not exceed a limit your admins set (100 TiB unless they changed it). Lowering
a quota does not give that allowance back. When you reach the limit, the dialog tells you
how much you can still add and when more frees up, for example "You can add 40 TiB more
right now. You can add 60 TiB after Dec 1". Ask an admin if you need more at once.

## Advanced

The **Advanced** switch at the top shows more: your access keys and config files, key
rotation, subusers, bucket policies, ACLs, CORS, tags, object lock, notifications and the
full lifecycle editor. Your choice is remembered.

!!! tip "Lost a file?"
    If versioning is on, an older version is usually still there for 30 days. Ask your admin
    if you need help to get it back.
