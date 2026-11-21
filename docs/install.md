---
title: Install
description: Install Bucketeer step by step, from the Ceph service user to the first login.
---

<div class="osu-hero" markdown>
<span class="osu-kicker">For admins</span>

# Install Bucketeer

From an empty server to the first login in seven steps. The source code is on GitHub:
[dirkpetersen/bucketeer](https://github.com/dirkpetersen/bucketeer).
</div>

## Try it first (optional)

The demo runs Bucketeer on your laptop with a fake directory and a mock cluster. It needs
no AD and no Ceph, and it changes nothing outside a temporary folder.

```bash
git clone https://github.com/dirkpetersen/bucketeer.git
cd bucketeer
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 mock/demo.py
```

Open <http://127.0.0.1:8000> and log in as `alice` (a user) or `dana` (an admin) with the
password `demo`.

## What you need

| Item | Details |
|---|---|
| Ceph | RGW on Reef or later, with shell access to run `radosgw-admin` once |
| S3 endpoint | The public S3 address, for example `https://s3.example.edu`. For virtual-host addressing (the default) also a wildcard DNS record `*.s3.example.edu` and a wildcard TLS certificate |
| Active Directory | LDAPS (port 636), a service account that can search users, and an AD group for the people who may use Bucketeer |
| Server | Linux with Python 3.12 or later, and a DNS name for Bucketeer with HTTPS. [appmotel](https://github.com/dirkpetersen/appmotel) gives you HTTPS, systemd and updates |

## Step 1. Create the Ceph service user

On a Ceph admin node:

```bash
radosgw-admin user create --uid=bucketeer-admin --display-name="Bucketeer Admin Ops service"
radosgw-admin caps add --uid=bucketeer-admin \
    --caps="users=read,write;buckets=read,write;usage=read;metadata=read"
radosgw-admin user info --uid=bucketeer-admin   # note access_key and secret_key
```

Keep the secret key safe: it can read every user's keys. Details, the internal Admin Ops
address and TLS: [Admin Ops setup](admin-ops-setup.md).

## Step 2. Prepare Active Directory

1. A **service account** for Bucketeer, for example `svc-bucketeer`, with a strong password.
   It only needs to read users and groups.
2. A **user group**: only its members can log in (nested groups count). Note its DN.
3. Optional: an **admin group**. You can also name admins in `ADMIN_USERS`.

## Step 3. Deploy the code

=== "With appmotel (recommended)"

    As the appmotel operator:

    ```bash
    appmo add bucketeer dirkpetersen/bucketeer
    ```

    appmotel clones the repository, builds `.venv` from `requirements.txt`, copies
    `.env.default` to the live `.env`, gives the app a port and an HTTPS address behind
    Traefik, and starts it. The first start fails on purpose: the REQUIRED settings are
    still empty.

=== "Without appmotel"

    ```bash
    git clone https://github.com/dirkpetersen/bucketeer.git
    cd bucketeer
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.default .env && chmod 600 .env
    ```

    Run it with `.venv/bin/python3 app.py` (for example from a systemd unit), from the
    repository folder. It listens on `127.0.0.1:$PORT` only, so put an HTTPS reverse proxy
    in front of it (Traefik, nginx or Caddy) that sends `X-Forwarded-Proto`.

## Step 4. Fill in the settings

Open the live `.env` (with appmotel: `appmo env bucketeer`). Every variable is explained in
the file. These are the ones you must fill in:

```ini title=".env (required part)"
# Sessions: generate with  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
SESSION_SECRET=

# Active Directory
LDAP_URL=ldaps://dc1.example.edu:636,ldaps://dc2.example.edu:636
LDAP_BASE_DN="DC=example,DC=edu"
LDAP_BIND_DN=svc-bucketeer@example.edu
LDAP_BIND_PASSWORD="..."
LDAP_REQUIRED_GROUP_DN="CN=S3 Users,OU=Groups,DC=example,DC=edu"
LDAP_ADMIN_GROUP_DN="CN=S3 Admins,OU=Groups,DC=example,DC=edu"
ADMIN_USERS=jdoe

# Ceph (from step 1)
RGW_ADMIN_URL=https://rgw-internal.example.edu
RGW_ADMIN_ACCESS_KEY=...
RGW_ADMIN_SECRET_KEY=...
S3_ENDPOINT_URL=https://s3.example.edu
```

!!! tip "Quote values with special characters"
    appmotel skips a value that contains `;`, a backtick, `$(`, `&&`, `||` or an unquoted
    `|`. Put such passwords in double quotes.

!!! note "The LDAP certificate fetches itself"
    On the first start Bucketeer fetches the CA certificate from your domain controllers
    into `data/ldap-ca.pem` and logs its fingerprint. To pin it, set `LDAP_CA_SHA256` to
    that fingerprint (or your root CA's). See
    [Deployment, LDAP CA certificate](deployment.md#ldap-ca-certificate).

Optional, but useful from day one:

- `SMTP_FROM` and `APP_URL`: send an email invitation when someone shares a bucket
  ([Deployment, Email](deployment.md#email)).
- `S3_ADDRESSING_STYLE=path` if your S3 endpoint has no wildcard DNS.

## Step 5. Start and check

=== "With appmotel"

    ```bash
    appmo restart bucketeer
    appmo logs bucketeer
    ```

=== "Without appmotel"

    ```bash
    .venv/bin/python3 app.py
    ```

The log starts with a short report: the user, the settings file, the LDAP CA file and its
fingerprint. If a REQUIRED setting is missing, the app names it and stops. Then:

```bash
curl -s https://bucketeer.example.edu/health    # {"status":"ok"}
```

## Step 6. Log in and set up the portal

1. Log in with an account from `ADMIN_USERS` or the admin group.
2. Save a default budget number in your profile (everyone is asked to do this first).
3. Open **Admin, Settings** and set:
    - **Placements**: the placement ids users may pick. Check them on the cluster with
      `radosgw-admin zonegroup placement list`.
    - **Bucket naming** and the **budget number format** (with an example).
    - **Default quota** for new users (1 TiB if you change nothing).
4. Open **Admin, Billing**: the price per TiB per month and the fiscal year start.
5. **Admin, Offboarding** starts in report-only mode. Switch to enforce when the reports
   look right.

Then ask a test user to log in, run **Set up this computer**, and create a bucket. Point
your users to the [user guide](user-guide.md) and the [S3 clients](s3-clients.md) page.

## Step 7. Keep it up to date

=== "With appmotel"

    ```bash
    appmo update bucketeer
    ```

=== "Without appmotel"

    ```bash
    git pull && .venv/bin/pip install -r requirements.txt
    # then restart the service
    ```

The SQLite database in `data/` is kept, and schema changes are applied at start. After an
update, compare your `.env` with `.env.default`: new settings are not added for you.
