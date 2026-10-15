# To do

Work top to bottom. Each `- [ ]` item should be one reviewable change: code,
tests and docs together. Design decisions are in CLAUDE.md under "Product
design"; don't re-decide them here. Tick an item (`- [x]`) in the same commit
that completes it. An item tagged `(blocked: Qn)` waits for an answer in
QUESTIONS.md. Skip it, and skip anything that depends on it. Items tagged
`(needs cluster)` need a real croit/Ceph cluster or cluster changes; the loop
skips them too.

## Phase 0: Foundations

- [x] Project skeleton: `app.py` (uvicorn, `PORT`/`HOST`, proxy headers),
  config loading from `.env` with REQUIRED/Default validation matching
  `.env.default`, `GET /health`, `requirements.txt`, `requirements-dev.txt`
  (pytest, ruff, httpx, moto[server]), `static/` served at `/`. Document the
  run, lint, test and single-test commands in CLAUDE.md.
- [x] Mock croit API at `mock/croit_mock.py` (FastAPI, in-memory, seeded). It
  covers:
  - auth (`/auth/login`, `/auth/login-form`, `/auth/token-info`, `/api-tokens`
    CRUD with roles)
  - S3 users: CRUD, suspend, quotas, `/s3/user-names`
  - keys: GET, whole-set PUT that drops missing keys, single PUT/DELETE, `/key`,
    `cfg-file/download`
  - buckets: list, create, patch, delete with the `deleteData` and
    confirmation-sentence flow, plus `/policy`, `/quota` and `/usage/*`
  - tasks that stay RUNNING briefly before FINISHED
  - `/config/rgw-ssl-cert` and `/servers/ha-groups` (for `tools/croit-cert`)

  Mirror croit's rules: duplicate email rejected, user with buckets can't be
  deleted, `tenant$uid`. Return croit's `ErrorMessage` shape. Add a test that
  validates responses against `docs/api/api-1.yaml`.
- [x] S3 test backend: tests start `moto` in server mode as the S3 endpoint. The
  mock croit's buckets and keys must line up with what's in moto, e.g. mock
  bucket creation also creates the bucket in moto.
- [x] croit client module: async httpx client, Bearer token, `ErrorMessage` →
  exception, task polling, `$` URL-encoding of uids.
- [x] SQLite storage under `DATA_DIR`: schema plus migrations for settings, uid
  mappings, bucket keys, and the audit log.

## Phase 1: Login and credentials (MVP)

- [x] AD login over LDAPS (CLAUDE.md "Authentication"): service bind, filter
  with `{login}`, exactly one match, nested-group check, user bind, reject
  empty passwords. Signed session cookie, `SESSION_MAX_AGE`. Tests use a fake
  LDAP layer.
- [x] Login throttle fix: a successful login resets the per-account counter but
  not its "already reported" flag in `bucketeer/auth.py`, so the next block on
  that account may not write a `login.throttled` audit entry. Reset both.
- [x] Login throttle: raise the per-typed-login and per-account limits from 5
  to 10 attempts per 15 minutes (Q3), and keep the IP limit at 20. Update the
  tests and CLAUDE.md.
- [x] RGW user resolution: sAMAccountName → uid, honoring admin uid mappings.
  Auto-create on first login with the default quota.
- [x] Credentials API and screen: show access and secret keys (secret masked,
  reveal and copy) and the `~/.aws/config` + `~/.aws/credentials` snippets,
  with copy and download. Endpoint comes from `S3_ENDPOINT_URL` or the croit
  `.s3cfg`.
- [x] Stop sending the AD email to croit when auto-creating an RGW user (Q5):
  pass only uid and display name. Remove the `email_conflict` path and its
  message, and update the tests and CLAUDE.md.
- [x] Key management: create an additional key, rotate (add new, then remove
  old), delete. Never use the whole-set PUT.
- [x] Visual design system: CSS tokens, Beaver Orange accent, light and dark,
  login and dashboard layouts, motion with reduced-motion support. Vanilla JS
  modules and web components only.

## Phase 2: Buckets

- [x] Local demo: `mock/demo.py` starts the
  mock croit with moto and Bucketeer on localhost, with a built-in fake
  directory (a few users incl. one admin, fixed demo passwords printed on
  start), injected through `create_app(authenticator=...)`. No production code
  path or setting can enable it. It uses a temporary DATA_DIR and plain-http
  cookies, binds 127.0.0.1 only, and shows a "Demo mode" banner. One command:
  `.venv/bin/python3 mock/demo.py`. Document it in CLAUDE.md and README.md.
- [x] User profile: a profile page where users set a default budget number
  (index/worktag), stored in SQLite.
- [x] Bucket list with usage, quota and budget number. Create bucket: name
  checked against the admin naming rules, a **required** budget number
  (index/worktag) prefilled from the user's profile default, versioning, and
  under Advanced object lock (with warning) and placement. Delete bucket:
  typed-name confirmation when non-empty. Budget number rules: CLAUDE.md (Q2).
- [x] Versioning toggle (enable/suspend).
- [x] Shared-document helpers for bucket policy and lifecycle: read-modify-write
  that only touches `bucketeer-*` Sids/IDs. Unit-test heavily.
- [x] Sharing: share a bucket with an AD user (read-only or read-write); list
  and revoke shares. Recipients are people who could log in (Q8): members of
  `LDAP_REQUIRED_GROUP_DN` when set, anyone in AD when not; a recipient
  without an S3 account gets one created on the spot.
- [x] Versioning on by default (Q7): new buckets default to versioning on, and a
  new versioned bucket gets the managed "expire noncurrent versions after 30
  days" lifecycle rule. Update the create dialog default and tests.
- [x] Bucket keys: create a scoped RGW user (zero quota) plus a policy
  statement, and record ownership in SQLite. List, revoke, and show snippets
  for each key. Verify the zero-quota assumption on the real cluster.
- [x] Bucket keys cleanup: at startup, finish or roll back bucket-key rows
  left `pending` by a crash (the same rollback as a failed create), audited.
- [x] Simple lifecycle rules: expire after N days, expire noncurrent versions,
  abort incomplete multipart uploads.
- [x] Lifecycle polish: when the server answers 409 requiresConfirmation (rules
  changed in another tab), reload the dialog or show the confirm prompt
  instead of a dead-end error; offer to also enable "expire old versions" in
  the expire-current confirm when versioning is on; add tests for foreign
  rules that use a legacy top-level Prefix or Filter And/Tag.
- [x] Presigned share links, 7 days at most. Choose the object with a key field
  plus prefix autocomplete from a read-only listing (Q1); no tree browser.
- [x] Audit log entries for every bucket, key and share change.
- [x] Raw policy editor: refuse public and self-lockout statements (Q12). A new
  or changed `Allow` for principal `*`, or a `Deny` that would lock the owner
  out of the bucket or its policy, is 422 with a clear message; such a
  statement already stored may only be kept as stored or removed. Show the
  reason in the editor before saving.

## Phase 3: Admin

- [x] Admin page gated by `LDAP_ADMIN_GROUP_DN`: all users and buckets (with
  budget numbers), set
  quotas, suspend and unsuspend.
- [x] Admin settings: bucket naming rules, placement defaults, default quota,
  uid mappings, notification endpoint allowlist.
- [x] Admin setting: budget number pattern plus example hint (Q2), validated
  with the same safe-regex machinery as the naming rules. Default quota
  setting defaults to 1 TiB instead of "Not set" (Q4).
- [x] Chargeback CSV export on the admin page: bucket, owner, budget number,
  usage (Q2).
- [x] Admin: list all bucket keys (owner, bucket, access, status) and revoke
  any of them, audited as the admin.
- [x] Audit log viewer with filters.
- [x] Offboarding job (daily, in-process): suspend RGW users and their bucket
  keys when the AD account is disabled or removed from the group. Never delete
  anything. Safeguards per CLAUDE.md (Q10): known accounts only, auto-unsuspend
  on login for job suspensions, abort thresholds, report-only until an admin
  enables enforce, 03:00 daily plus run-now.

## Phase 4: Advanced (behind the global Advanced toggle)

- [x] Advanced toggle, remembered per user.
- [x] Bucket row polish: each row now has seven controls and the actions wrap;
  group the less common ones (Keys, Lifecycle, Share link, Share) into a
  "More" menu that is keyboard-accessible and works on phones.
- [x] Raw bucket policy editor, with a warning before editing `bucketeer-*`
  statements.
- [x] Bucket and object ACLs.
- [x] CORS rules editor.
- [x] Bucket tagging.
- [x] Object lock: default retention, plus legal hold and retention for a given
  object key.
- [x] Full lifecycle editor: prefix and tag filters, object-size filters, and
  expiration rules (current, noncurrent with versions-to-keep, delete-marker
  cleanup, multipart abort), alongside the existing simple rules.
- [x] Bucket notifications, with endpoints from the admin allowlist only
  (no broker credentials, Q6).
- [x] Multipart upload cleanup (list and abort).
- [ ] (blocked: Q15) Subusers (read/write/readwrite/full) and their keys.

## Verify against a real croit cluster

Things the spec leaves unclear. Check each one once a real cluster is
available, and fix the client and the mock if reality differs:
- [ ] (needs cluster) `GET /s3/users` without `uid` (the spec marks it required, but the
  description says it lists all users).
- [ ] (needs cluster) Where croit puts the bucket-delete confirmation sentence (the client
  reads `details`, then falls back to the message text), and the exact
  wording.
- [ ] (needs cluster) Policy Sids with `-` and `.` (e.g. `bucketeer-share-u-j.doe`)
  are accepted by RGW. AWS documents Sids as alphanumeric only. If RGW
  rejects them, switch to id-based Sids mapped to uids in SQLite.
- [ ] (needs cluster) Resource ARN form for buckets in a tenant
  (`arn:aws:s3:::<bucket>` vs `arn:aws:s3::<tenant>:<bucket>`).
- [x] (needs cluster) Duplicate-email error on `POST /s3/users`. No longer needed:
  Bucketeer never sends an email to croit (Q5).
- [ ] (needs cluster) Creating a uid that already exists: 409, or an idempotent
  200?
- [ ] (needs cluster) Tenant users in `/s3/user-names` and `/s3/users`: is it
  `tenant$uid`, or a bare uid plus a separate `tenant` field? `get_user`'s
  exact match assumes `tenant$uid`. Does an unknown `uid` filter return 404 or
  an empty list?
- [ ] (needs cluster) Key `user` field for tenant users in `GET /s3/users/{uid}/keys`:
  `tenant$uid` (assumed) or bare uid? If bare, tenant users see "no access keys".
- [ ] (needs cluster) Sharing: RGW honours a `bucketeer-share-*` statement for the
  recipient's own keys (read vs readwrite), and the cross-tenant address
  `tenant:bucket` works with the AWS CLI.
- [ ] (needs cluster) Object lock: RGW accepts removing GOVERNANCE retention (an
  empty `Retention` with `BypassGovernanceRetention`) and honours the bypass
  only for the bucket owner.
- [ ] (needs cluster) Lifecycle: RGW supports object-size filters
  (`ObjectSizeGreaterThan`/`LessThan`) and `NewerNoncurrentVersions`, and refuses
  tag filters combined with delete-marker cleanup or multipart abort.
- [ ] (needs cluster) Notifications: RGW topic attributes (push-endpoint,
  amqp-exchange, verify-ssl) via CreateTopic with a user's key, CreateTopic
  updating an existing topic's attributes, and events actually delivered to
  HTTP/AMQP/Kafka endpoints. Pre-Squid RGW has no topic ownership, so any
  user with their own keys can rewrite a topic in the tenant; verify the
  cluster version and behaviour. Kafka topics are per-user RGW topic names
  (`bucketeer-<endpoint>-<user>`): check broker auto-creation.
- [ ] (needs cluster) User quota: `maxObjects` null means unlimited and
  `maxSize` is in bytes (1 TiB default).
- [ ] (needs cluster) RGW uids are case-sensitive (`Alice` and `alice` are
  different users).
- [ ] (needs cluster) Bucket keys: a zero user quota doesn't block writes to the shared
  bucket, because writes count against the bucket owner's quota.

## Later (needs cluster work first)

- [ ] (needs cluster) Server-side encryption: SSE-KMS/SSE-S3 needs a key server
  (Vault/Barbican/KMIP), SSE-C needs HTTPS only.
- [ ] (needs cluster) Static website hosting: needs RGW website mode and wildcard DNS.
- [ ] (needs cluster) STS temporary credentials: needs an RGW STS key and roles. This also
  gives short-lived bucket-scoped keys.

## Roadmap (deferred by the owner, not planned)

The loop never picks these; they need a new decision from the owner first.
- Group shares: share a bucket with an AD group, expanded to its members'
  uids at share time (Q11).
- Public buckets: allow public policy statements with a warning, an extra
  confirmation and an admin list of public buckets (Q12).
- Lifecycle transitions to another storage class (Q13). Until then admins
  handle tiering.
