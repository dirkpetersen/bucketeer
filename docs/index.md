---
title: Bucketeer
description: Self-service S3 storage on Ceph, with AD login.
---

<div class="osu-hero" markdown>
<span class="osu-kicker">Self-service S3 storage</span>

# Bucketeer

Log in with your university account, connect your computer with one command, and manage
your own S3 buckets: create, share, version, and clean up, without a ticket.
</div>

<div class="osu-cards" markdown>
<div class="osu-card" markdown>
<span class="osu-tag">Start here</span>

### [User guide](user-guide.md)

Log in, save your budget number, set up your computer, create a bucket.
</div>
<div class="osu-card blue" markdown>
<span class="osu-tag">Tools</span>

### [S3 clients](s3-clients.md)

22 tools compared, with real screenshots and settings for Cyberduck, AWS CLI, rclone and more.
</div>
<div class="osu-card pine" markdown>
<span class="osu-tag">Admins</span>

### [Admin guide](admin-guide.md)

Users, quotas, offboarding, and the monthly billing per user and bucket.
</div>
<div class="osu-card gold" markdown>
<span class="osu-tag">Operators</span>

### [Install](install.md)

From an empty server to the first login in seven steps, on appmotel or on its own.
</div>
</div>

<figure class="osu-shot" markdown>
![The Bucketeer workspace](assets/screens/workspace.webp#only-light)
![The Bucketeer workspace](assets/screens/workspace-dark.webp#only-dark)
<figcaption>The workspace: connect this computer on the left, your buckets on the right.</figcaption>
</figure>

## Source code

Bucketeer is open source: [github.com/dirkpetersen/bucketeer](https://github.com/dirkpetersen/bucketeer).
Report problems and ideas in its [issues](https://github.com/dirkpetersen/bucketeer/issues).

## How it works

- [Software stack](stack.md): every part of the web stack, backend and frontend.
- [Architecture](architecture.md): how the parts work together.
- [Security](security.md): the controls that protect users and keys.
- [Admin Ops setup](admin-ops-setup.md): prepare the Ceph cluster.
