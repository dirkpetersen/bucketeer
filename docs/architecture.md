# Architecture

## Two API planes

- **Admin plane.** Creates and changes S3 users, keys, quotas and suspension, and lists
  buckets. It uses the RGW Admin Ops API, signed with
  SigV4 using a dedicated service user.
- **Data plane.** Everything S3 itself offers: bucket create, versioning, lifecycle, CORS,
  tags, object lock, notifications, policies, ACLs and presigned URLs. These calls use
  **the logged-in user's own keys**, so RGW enforces ownership and policy. Bucketeer never
  uses an admin S3 identity for a user action.

## Login flow

1. Bind to Active Directory with the service account.
2. Search with the login filter. A user name, an email address and a user principal name
   all find the same account. Exactly one match is required.
3. Check the required group, when one is set. Nested groups count.
4. Verify the password with a bind as the user. Empty passwords are refused.
5. Take the identity from the directory attribute (`sAMAccountName`), never from the typed
   text. Start a signed session cookie.

The RGW user id is the lowercased `sAMAccountName`. The first login creates the RGW user
with the default quota.

## Request path

Browser, then Traefik (TLS), then uvicorn, then FastAPI. Each `/api` router sits behind the
CSRF check. The user id always comes from the server session, never from the request.

## Data

SQLite holds settings, user id mappings, bucket and key ownership, shares, delete jobs,
setup tokens, known accounts for offboarding, and an append-only audit log. Secrets are
never stored in it.

## Background jobs

- Offboarding: a daily job suspends users whose AD account is disabled or left the group.
  It starts in report-only mode and has safety limits.
- Bucket delete: a non-empty bucket is emptied through S3 with the user's key, with
  progress, and resumes after a restart.
- Key clean-up: unfinished bucket-key creations are rolled back at startup.
- Share invitation email: sent after the share answer, so a slow mail server never delays
  it.

## Mocks

`mock/` lets you run and test the whole app without Ceph or Active Directory.
