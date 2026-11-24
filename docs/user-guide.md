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
password. Your S3 account is created the first time you log in, with a storage limit of
1 TiB unless your admin set something else.

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

## 4. Create a bucket

Click **Create bucket**. Bucket names use lowercase letters, digits and hyphens (no dots).
Versioning is on by default: when you overwrite or delete a file, the old version is kept
for 30 days, so you can get it back.

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
| Delete a bucket | **More, Delete**. A bucket with data needs you to type its name. |

## Advanced

The **Advanced** switch at the top shows more: your access keys and config files, key
rotation, subusers, bucket policies, ACLs, CORS, tags, object lock, notifications and the
full lifecycle editor. Your choice is remembered.

!!! tip "Lost a file?"
    If versioning is on, an older version is usually still there for 30 days. Ask your admin
    if you need help to get it back.
