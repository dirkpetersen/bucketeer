# S3 features in the croit API

This page lists every S3/RGW capability exposed by the croit management API, as
documented in `api-1.yaml`: 68 endpoints under `/s3/*` plus the RGW
certificate. All paths are relative to `CROIT_URL`. See `README.md` for getting
a token.

These are **admin-side** calls made with a croit token, not with S3 keys. Some
S3 features are not in this API at all; users reach those with their own S3
keys through the S3 protocol (see the end of this page).

Conventions that apply throughout:
- `uid` may be tenant-qualified as `tenant$uid` (URL-encode the `$`). Buckets in
  a tenant are named `tenant:bucket`.
- Long operations return a task (`ManagedTask`). Poll `GET /tasks/{id}` until
  `done`, then check `state == FINISHED`.
- List endpoints return a plain array, or a page when you pass the
  `pagination` query parameter.

## Users

| Method | Path | What it does |
|---|---|---|
| GET | `/s3/users` | List users with their subusers and quotas (optionally filtered by repeated `uid`) |
| GET | `/s3/user-names` | List all uids (cheap) |
| POST | `/s3/users` | Create a user (`uid`, `name`; optional `email`, `tenant`, default placement and storage class) |
| PATCH | `/s3/users/{uid}` | Change name, email, default placement, or **suspend/unsuspend** |
| DELETE | `/s3/users/{uid}` | Delete a user. Refused while the user still owns buckets |
| GET | `/s3/users/{uid}/buckets` | Buckets the user owns |

## Credentials (access/secret keys)

| Method | Path | What it does |
|---|---|---|
| GET | `/s3/users/{uid}/keys` | All key pairs of a user or `uid:subuser`, **including secrets** |
| GET | `/s3/users/{uid}/key` | One key pair (null if none) |
| PUT | `/s3/users/{uid}/keys/{accessKey}` | Add or overwrite one key pair |
| DELETE | `/s3/users/{uid}/keys/{accessKey}` | Remove one key pair |
| PUT | `/s3/users/{uid}/keys` | **Replace the whole key set** of the user and its subusers. Any key left out is deleted. Returns a task |
| GET | `/s3/users/{uid}/cfg-file/download` | Ready-made `.s3cfg` (s3cmd) with keys and a gateway address |

## Subusers

| Method | Path | What it does |
|---|---|---|
| POST | `/s3/users/{uid}` | Create a subuser |
| GET | `/s3/users/{uid}/subusers` | List subusers |
| PUT | `/s3/users/{uid}/subusers` | Replace the whole subuser set (any left out are deleted) |
| PATCH | `/s3/users/{uid}/subusers/{subuser}` | Change the subuser's permissions |
| DELETE | `/s3/users/{uid}/subusers/{subuser}` | Delete a subuser |
| GET | `/s3/users/{uid}/subusers/{subuser}/keys` | Subuser's key pairs |
| PUT | `/s3/users/{uid}/subusers/{subuser}/keys/{accessKey}` | Add or overwrite a subuser key pair |
| DELETE | `/s3/users/{uid}/subusers/{subuser}/keys/{accessKey}` | Remove a subuser key pair |
| GET | `/s3/users/{uid}/subusers/{subuser}/cfg-file/download` | `.s3cfg` for the subuser |

## Buckets

| Method | Path | What it does |
|---|---|---|
| GET | `/s3/buckets` | All buckets, with owner, placement, quota and usage |
| PUT | `/s3/buckets/{bucketName}` | **Create** a bucket. Options: `owner`, `acl`, `placement`, `versioning` (OFF/ENABLED), `objectLock` (creation only; turns versioning on), `expectedNumberOfObjects` (pre-sharding). Returns a task |
| GET | `/s3/buckets/{bucketName}` | Details: ACL, versioning, CORS rules (read-only) |
| PATCH | `/s3/buckets/{bucketName}` | Change owner, ACL or versioning. OFF suspends versioning, since S3 cannot disable it |
| DELETE | `/s3/buckets/{bucketName}` | **Delete**. An empty bucket is removed directly. A non-empty one needs `deleteData`, the bucket `name` repeated, and the confirmation sentence the first refusal names. Returns a task |
| GET / POST / DELETE | `/s3/buckets/{bucketName}/policy` | Read, set or remove the bucket policy |
| PUT | `/s3/buckets/{bucketName}/quota` | Set the bucket's quota |

## Objects (browse and manage bucket contents)

| Method | Path | What it does |
|---|---|---|
| GET | `/s3/buckets/{bucketName}/objects` | List objects (S3 list semantics) |
| GET | `/s3/buckets/{bucketName}/objects-paginated` | List one prefix level as folders and objects |
| PUT | `/s3/buckets/{bucketName}/upload` | Upload an object |
| GET | `/s3/buckets/{bucketName}/download` | Download an object |
| DELETE | `/s3/buckets/{bucketName}/objects` | Delete an object or one stored version |
| GET | `/s3/buckets/{bucketName}/versions-paginated` | List an object's stored versions |
| PUT | `/s3/buckets/{bucketName}/objects/restore-version` | Make an old version current again |
| GET / PUT | `/s3/buckets/{bucketName}/objects/acl` | Read or replace an object's ACL |
| GET / PUT | `/s3/buckets/{bucketName}/objects/metadata` | Read or replace object metadata |
| PUT | `/s3/buckets/{bucketName}/folders` | Create a folder (prefix) |
| PUT | `/s3/buckets/{bucketName}/entries/copy` | Copy entries into a folder |
| PUT | `/s3/buckets/{bucketName}/entries/move` | Move entries into a folder |
| DELETE | `/s3/buckets/{bucketName}/entries` | Delete entries; folders recursively |
| GET | `/s3/buckets/{bucketName}/multipart-uploads` | List active or abandoned multipart uploads |
| GET | `/s3/buckets/{bucketName}/multipart-uploads-paginated` | Same, below a prefix, paginated |
| DELETE | `/s3/buckets/{bucketName}/multipart-uploads` | Abort a multipart upload |

## Quotas and usage

| Method | Path | What it does |
|---|---|---|
| PUT | `/s3/users/{uid}/quota` | Quota over everything a user stores |
| PUT | `/s3/users/{uid}/bucket-quota` | Quota applied to each of the user's buckets separately |
| PUT | `/s3/buckets/{bucketName}/quota` | Quota for one bucket |
| GET | `/s3/usage/by-user/{uid}` | Usage statistics for a user |
| GET | `/s3/usage/by-bucket/{bucketName}` | Latest usage statistics for a bucket |

## Placement and storage classes

| Method | Path | What it does |
|---|---|---|
| GET | `/s3/placements` | Placement rules of the zone, one row per storage class |
| PUT | `/s3/placements/{name}` | Create a placement rule |
| PUT | `/s3/placements/{placementRule}/storage-classes/{storageClass}` | Add a storage class to a rule |
| DELETE | `/s3/placements/{placementRule}/storage-classes/{storageClass}` | Remove a storage class or the whole rule |
| PUT | `/s3/default-placement` | Placement rule new buckets land on |

## Cluster-level RGW administration

| Method | Path | What it does |
|---|---|---|
| POST | `/s3/bootstrap` | Initial S3 setup: pools, realm, zonegroup, zone |
| POST | `/s3/services` | Add RGW gateways to servers |
| GET / PUT | `/config/rgw-ssl-cert` | Read or set the gateway/HAProxy TLS certificate (used by `tools/croit-cert`) |
| GET | `/s3/multisite/status` | Multisite membership and sync lag |
| POST | `/s3/multisite/primary` | Turn this site into a multisite primary |
| GET | `/s3/multisite/primary/token` | Token a secondary uses to join |
| POST | `/s3/multisite/secondary` | Join another site as a secondary |
| POST | `/s3/multisite/promote` | Promote this site to primary |
| PATCH | `/s3/multisite/readonly` | Set or clear read-only on a secondary zone |
| PATCH | `/s3/multisite/endpoints` | Change where other sites reach this S3 |

The HA groups under `/servers/ha-groups` (virtual IPs and HAProxy in front of
the gateways) are also S3-related, but they are server configuration.

## Not available through the croit API

The croit API has no endpoints for these. RGW supports most of them through the
S3 protocol, so users can manage them with their own keys, e.g. `aws s3api`
against `endpoint_url`:

- Lifecycle rules (object metadata only *shows* which rule expires an object)
- Setting CORS rules (croit can read them, not write them)
- Object lock retention periods and legal holds on objects (croit can only
  enable object lock at bucket creation)
- Bucket and object tagging
- Server-side encryption settings
- Bucket notifications
- Static website hosting
- Presigned URLs (generated client-side with S3 keys)
- STS / temporary credentials
