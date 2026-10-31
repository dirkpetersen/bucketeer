# Operator guide: the RGW Admin Ops backend

Bucketeer manages S3 users, keys, quotas, suspension and bucket lists through the Ceph RGW
**Admin Ops API**, with a dedicated RGW service user.

This guide is for a Ceph cluster with RGW. Commands run on a node that has the
`radosgw-admin` tool and an admin keyring.

What Bucketeer needs, in short:

| Setting | Value |
|---|---|
| `RGW_ADMIN_URL` | internal RGW address that serves `/admin`, e.g. `https://rgw-internal.example.org` |
| `RGW_ADMIN_ACCESS_KEY` / `RGW_ADMIN_SECRET_KEY` | keys of the service user below |
| `S3_ENDPOINT_URL` | the public S3 endpoint users should use (Bucketeer never discovers it) |
| `S3_TLS_VERIFY` / `S3_CA_CERT_FILE` | TLS trust for the RGW addresses, see below |

Bucketeer refuses to start when one of the first three or `S3_ENDPOINT_URL` is missing, and names
the missing ones.

## 1. Create the service user

```bash
radosgw-admin user create --uid=bucketeer-admin --display-name="Bucketeer Admin Ops service"
```

The command prints a JSON document with one generated S3 key pair (`access_key`, `secret_key`).
Keep the secret; you can read it again with `radosgw-admin user info --uid=bucketeer-admin`.
To generate a fresh pair instead:

```bash
radosgw-admin key create --uid=bucketeer-admin --key-type=s3 --gen-access-key --gen-secret
```

Give it the Admin Ops capabilities:

```bash
radosgw-admin caps add --uid=bucketeer-admin \
    --caps="users=read,write;buckets=read,write;usage=read;metadata=read"
```

What each one is for:

- `users=read,write`: create users, read and add/remove keys, quotas, suspend. Also used to find
  the service user itself.
- `buckets=read,write`: bucket lists with usage, bucket quotas.
- `metadata=read`: the user list (`/admin/metadata/user`). Recommended: without it Bucketeer
  falls back to `GET /admin/user?list`, which is still unconfirmed on Reef.
- `usage=read`: usage queries.

Do not give it `*` and do not use a tenant for it. Check the result with
`radosgw-admin user info --uid=bucketeer-admin` (the `caps` list).

**Reserved uid.** Bucketeer recognises the service user by its access key (not by name) and
treats that uid, with any tenant, as reserved: it is hidden from every admin list, the chargeback
export and the offboarding report, and it can never be a login, a uid mapping target, a share or
ACL recipient, or the target of an admin suspend/quota change. Pick a name that no AD account
uses (`bucketeer-admin` has a hyphen, which is fine). If Bucketeer cannot look the service user
up, user resolution fails closed (503) instead of guessing.

## 2. Choose the Admin Ops address

`RGW_ADMIN_URL` is where Bucketeer sends signed `/admin/...` requests. Use an **internal** RGW
address, for example a gateway's cluster-network name, or an internal HAProxy front that only
the Bucketeer host can reach. Do not point it at the public S3 endpoint.

Keep `/admin` off the public endpoint. On HAProxy or any other front proxy that serves the
public S3 endpoint, deny the path. A HAProxy example for the public frontend:

```
acl admin_path path,url_dec -m reg -i ^/+admin(/|$)
http-request deny deny_status 403 if admin_path
```

Match the decoded path and only the exact `admin` segment: a plain `path_beg /admin` would also
block path-style buckets such as `admin-reports`, and `/%61dmin/...` would get past it. If a tool
generates your proxy configuration, check that a hand edit survives its next update.

Test from outside: `curl -i https://s3.example.org/admin/user` must not reach RGW (403 from
the proxy, not an RGW `AccessDenied`). Admin Ops requests need a valid signature, so this is
defence in depth, but there is no reason to expose it.

Plain `http://` is accepted only for a loopback address (for example an SSH or local proxy
tunnel to `127.0.0.1`); any other host must use `https://`, because the secret key signs every
request.

## 3. TLS

Admin Ops calls and Bucketeer's S3 calls (with each user's own keys) use the same trust
settings:

- `S3_TLS_VERIFY=true` (default). Never switch it off in production: secret keys travel over
  these connections.
- `S3_CA_CERT_FILE=/path/ca.pem` when the RGW certificate is signed by an internal CA.

If the internal address has a different certificate name from the public one, make sure the
certificate covers the name used in `RGW_ADMIN_URL`. `RGW_ADMIN_TIMEOUT` (default 15 s) is the
per-request timeout.

## 4. Configure Bucketeer

On appmotel, `appmo env bucketeer`, then `appmo restart bucketeer`:

```
ADMIN_BACKEND=rgw
RGW_ADMIN_URL=https://rgw-internal.example.org
RGW_ADMIN_ACCESS_KEY=...
RGW_ADMIN_SECRET_KEY=...
S3_ENDPOINT_URL=https://s3.example.org
S3_REGION=us-east-1
```

Quote values that contain
characters appmotel rejects (see `.env.default`).

## 5. Placements are entered by hand

Admin Ops cannot list placements, so on the admin Settings tab you type the placement ids that
users may choose (one per line) and a default. Bucketeer checks only the format. Verify the
ids against the cluster yourself:

```bash
radosgw-admin zonegroup placement list
```

When a user picks a placement, Bucketeer creates the bucket with the user's own key and
`LocationConstraint = ":<placement>"` (an empty api-name part before the colon). RGW compares
the part before the colon with the zonegroup's api name and skips an empty one, so `S3_REGION`
does not need to match. This is expected to work on Reef; it is one of the cluster checks below.
If RGW rejects the empty prefix, the fallback needs a small code change: Bucketeer always sends
`:<placement>` today, so setting `S3_REGION` alone does not change the constraint (it only
affects request signing). Report it; the change would send `<api_name>:<placement>` with the
zonegroup's api name (`radosgw-admin zonegroup get`, field `api_name`). With no placement chosen no
constraint is sent and RGW uses its default.

## 6. Rotating the service key

1. Create a second key: `radosgw-admin key create --uid=bucketeer-admin --key-type=s3
   --gen-access-key --gen-secret`.
2. Put the new pair into `RGW_ADMIN_ACCESS_KEY` / `RGW_ADMIN_SECRET_KEY` and restart Bucketeer.
3. Check the admin page loads and a login works.
4. Remove the old key: `radosgw-admin key rm --uid=bucketeer-admin --access-key=<old>`.

A user cannot hold the same access key twice, and Bucketeer finds the service user by the key it
is configured with, so always restart after step 2. Rotate whenever someone who could read the
`.env` leaves.

## 7. Blast radius: treat it like a root credential

The service user can read **every user's S3 secret keys** (key lists come back with secrets),
create users and keys, and suspend accounts. `users=write` also lets it add caps to any user,
itself included, so the cap list does not limit what a leaked key can do: in practice it is a
full RGW admin. With it, anyone can reach all S3 data on the cluster, though not the cluster's
management itself (no OSD or pool access). So:

- the `.env` is 0600 and readable only by Bucketeer's Unix user;
- keep `RGW_ADMIN_URL` internal and `/admin` off the public endpoint;
- never log it, paste it into tickets or reuse it elsewhere (Bucketeer never logs or returns it);
- rotate it as in section 6.

## 8. RGW accounts (Tentacle and later)

Ceph Tentacle introduces RGW accounts with IAM-style users and account root users. That is the
next step down in privilege (an admin per account instead of one user that sees everything),
but Bucketeer does not use it yet. After a Tentacle upgrade nothing changes for Bucketeer: the
existing users and the service user keep working. Moving users into accounts later would be a
Bucketeer change.

## 9. Cluster checks still to do

These are listed in `TODO.md` ("needs cluster", Phase 5). Until they are done, the Admin Ops
and S3 behaviour is verified against the mocks (`mock/rgw_admin_mock.py`, `mock/s3_bridge.py`)
only:

- Admin Ops caps work as above, including tenant uids, key add/remove, quotas, suspend, bucket
  stats and purge.
- Bucket create with the user's own key: RGW accepts `:<placement>` as `LocationConstraint`
  and uses the default placement when none is sent (else the `S3_REGION` fallback above).
- On Reef a repeat CreateBucket of an own bucket answers 200, so Bucketeer sends HeadBucket
  first: HeadBucket must give 200 for an owned bucket and 403/404 otherwise, and
  `ObjectLockEnabledForBucket` must also enable versioning.
- Every shape the mock marks uncertain (module docstring of `mock/rgw_admin_mock.py`): repeat
  create without `exclusive`, key-delete errors, quota body forms, `?list`, tenant bucket forms,
  and whether `bucket?stats=true` without `uid` drops tenant buckets.
- Concurrent admin page loads stay fast with the 45 s user-list cache.
- Whether RGW honours a zero user quota for a policy-granted bucket-key user.

Try it without a cluster first: `.venv/bin/python3 mock/demo.py` runs Bucketeer on this backend
against the Admin Ops mock and moto.
