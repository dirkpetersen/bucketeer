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
- [x] Offboarding abort thresholds as admin settings (Q14): `maxPercent` (default 10),
  `minPeopleForPercent` (default 4) and `maxPeople` (default 20) on the Offboarding tab;
  abort when candidates > maxPercent % of known people AND >= minPeopleForPercent, or
  > maxPeople. Validated, audited, shown in the run report.
- [x] croit allowlisting gateway `tools/croit-gate` (limits the croit admin
  token's blast radius; croit support called it a reasonable hardening
  measure). A standalone tool under the `tools/` rules (single file, stdlib
  only, never imports Bucketeer):
  - Listens on 127.0.0.1 (`CROIT_GATE_LISTEN`, e.g. `127.0.0.1:18089`),
    forwards to `CROIT_URL` with the real `CROIT_API_TOKEN` (TLS via
    `CROIT_CA_CERT_FILE` / `CROIT_TLS_VERIFY`). Clients authenticate with a
    separate `CROIT_GATE_CLIENT_TOKEN` (constant-time compare); the real
    token never leaves the gateway, and croit's responses never echo it.
  - Forwards only the method+path pairs Bucketeer's `CroitClient` uses
    (`/s3/...` users, keys, quotas, buckets, placements, usage, bucket
    policy; `GET /tasks/{id}`; `GET /auth/token-info`). Everything else gets
    403 and a log line (method, path, client; never tokens, bodies or query
    strings, which can hold secret keys). Match on the raw path: refuse `..`,
    `//`, encoded `/` (`%2F`) and anything that is not a single path segment
    per parameter. Keep a test that compares the allowlist against
    `bucketeer/croit.py`, so a new client call fails the test until the
    gateway allows it.
  - Streams request and response bodies, has size and time limits, and needs
    no Bucketeer change: Bucketeer's `CROIT_URL` points to the gateway and its
    `CROIT_API_TOKEN` holds the client token. If `Settings` refuses
    `http://` for `CROIT_URL`, allow plain http for 127.0.0.1/localhost
    only.
  - **Design goal (owner): independent of Bucketeer's deployments.** The
    gateway runs under its own Unix user as that user's own systemd user
    service (with lingering enabled), installed outside the Bucketeer repo:
    the script is copied to that user's `~/.local/bin`, and its config lives
    in its own `~/.config/croit-gate/env` (0600). appmotel deploys, updates
    and `git reset --hard` of Bucketeer never touch it, and Bucketeer's user
    can't read the real token. Offer `croit-gate install` (copies itself,
    writes the unit and an env template without overwriting an existing one,
    and prints the `loginctl enable-linger` and `systemctl --user enable
    --now` steps). Updating the gateway is a deliberate re-install by its own
    user.
  - Document it in `tools/README.md`: the dedicated user, the install and
    update steps, the systemd user unit, and token rotation. Tests run it
    in-process against the mock croit.

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
- [x] Subusers (read/write/readwrite/full) and their keys (Q15 rules in CLAUDE.md), on both admin backends.

## Phase 5: RGW Admin Ops instead of the croit API (owner decision)

Design in CLAUDE.md, "Two API planes". Items build on each other in this
order, except that A3 to A6 can run in parallel once A2 is done.

- [x] A1. Admin Ops mock: `/admin/...` routes for every Admin Ops call the
  client will make, with SigV4 checked against a configured admin key and
  caps enforced. State is shared with the mock croit and the moto bridge, so
  users, keys and buckets line up whichever backend created them. Responses
  follow the Ceph Admin Ops docs for Reef (JSON shapes, error codes), and
  `mock/demo.py` can start either backend. Tests.
- [x] A2. `bucketeer/rgw_admin.py` `RgwAdminClient` with the same public
  methods and exceptions as `CroitClient`, and the settings `ADMIN_BACKEND`
  (default `croit`), `RGW_ADMIN_URL`, `RGW_ADMIN_ACCESS_KEY` and
  `RGW_ADMIN_SECRET_KEY` (required with `rgw`) in `.env.default`. Tenant uids,
  safe path and query encoding, no secrets in logs. Calls that have no Admin
  Ops counterpart (tasks, `.s3cfg`, placements) raise a clear error until
  A3 to A5 replace their callers. `create_app` builds the chosen client. A
  contract test suite runs the same client tests against both backends.
- [x] A3. Bucket create and versioning on the S3 data plane with the user's
  own key when `ADMIN_BACKEND=rgw`: CreateBucket with the placement as
  `LocationConstraint`, object lock at create, PutBucketVersioning (object-lock
  guard kept). Idempotent adoption, budget tag and noncurrent rule unchanged.
  Bucket-create tests run on both backends.
- [x] A4. Delete a non-empty bucket as a background job with the rgw backend:
  same typed-name confirmation, job state in SQLite (migration), progress
  shown in the UI, resumable after a restart, audited (`bucket.delete` when
  done, `bucket.delete_failed`). Bucket keys and shares are revoked as today.
- [x] A5. Placement and endpoint without croit: with `rgw`, admins enter the
  allowed placements on the Settings tab (validated only for format, with a
  hint to check them against `radosgw-admin zonegroup placement list`), and
  `S3_ENDPOINT_URL` is required (Settings refuses to start without it).
- [x] A6. Admin lists and jobs on the rgw backend: the user list with details
  (metadata list plus per-user reads with bounded concurrency and a short
  cache), bucket stats in one call, and chargeback, offboarding, the resolver
  and bucket keys all working on `rgw`. Run the existing admin, offboarding,
  chargeback and bucket-key tests on both backends.
- [x] A7. Switch the default to `ADMIN_BACKEND=rgw`. Update README,
  CLAUDE.md, `.env.default` and the croit-gate docs (now optional), and add an
  operator guide for creating the Admin Ops service user with its caps on a
  croit cluster.
- [ ] (needs cluster) Admin Ops on the real Reef cluster: caps, tenant uids,
  key add and remove, quotas, suspend, bucket stats, purge, and whether
  croit's UI shows the changes at once. Bucket create with the user's own key
  (A3): RGW accepts `:<placement>` (empty api name, which Reef's parser
  does not check against the zonegroup) as `LocationConstraint` and uses the
  default placement when none is sent. If it rejects the empty prefix, the
  fallback is to set `S3_REGION` to the zonegroup api name and send
  `<api>:<placement>`. On Reef a repeat CreateBucket of an own bucket
  answers 200 (not `BucketAlreadyOwnedByYou`), so Bucketeer sends HeadBucket
  first; check that HeadBucket gives 200 for an owned bucket and 403/404
  otherwise, and that `ObjectLockEnabledForBucket` also enables versioning.
  Also every shape the mock marks as uncertain (module docstring of
  `mock/rgw_admin_mock.py`): repeat create without `exclusive`, key-delete
  errors, quota body forms, `?list`, tenant bucket forms, and whether
  `bucket?stats=true` without `uid` drops tenant buckets (A6 already adds them with
  one `uid=` call per tenant user, bounded; if the global call turns out to be
  complete, drop that loop in `RgwAdminClient._fetch_buckets`), and that
  concurrent admin page loads stay fast with the 45 s user-list cache. Fix the mock where
  reality differs.
- [ ] (needs cluster) Remove the croit backend after that check (owner
  decides).

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
- [ ] (needs cluster) Subusers: RGW enforces subuser permissions (read, write, read-write) for S3
  requests, not only Swift.
- [ ] (needs cluster) Subuser keys over Admin Ops: `PUT|DELETE /admin/user?key&uid=&subuser=<name>` really
  adds/removes a key of the subuser (RGW may route a `subuser` parameter to the subuser op first), and
  `DELETE ?subuser=&purge-keys=true` removes its keys. Real RGW routes on the FIRST admin sub-resource
  in the query (`key` is sent first; a test pins that order).
- [ ] (needs cluster) croit / Admin Ops may auto-generate a Swift or S3 key when a subuser is created;
  Bucketeer would then need to delete it, as bucket-key create removes croit's auto-key.
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
- RGW accounts (after a Tentacle upgrade): croit's UI doesn't support them
  but won't interfere. Moving a user into an account
  (`radosgw-admin user modify --uid=... --account-id=...`) is irreversible.
- croit S3-scoped API tokens: requested from croit as a feature (2026-10).
  If they ship, the gateway becomes optional.
