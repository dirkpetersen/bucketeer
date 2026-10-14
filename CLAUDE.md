# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Bucketeer is a self-service S3 portal for Ceph clusters managed by **croit**. Users log in with AD, get their S3 (RGW) access/secret keys with ready-to-paste `~/.aws/config` and `~/.aws/credentials` snippets, and manage their buckets: create, delete, versioning, sharing, scoped keys, lifecycle, and the full set of S3 bucket features. Admins get an admin page. See "Product design" below for decisions already made, and `TODO.md` for the phased plan.

The repo is greenfield; see `TODO.md` for what exists so far. Code layout: `app.py` (uvicorn entry point), the `bucketeer/` package (`config.py` settings, `main.py` app factory, `croit.py` croit client, `db.py` SQLite storage, `s3docs.py` shared bucket policy/lifecycle/tag helpers, `ldapauth.py` AD login, `auth.py` sessions/CSRF/throttle/login routes, `admin.py` admin API), `static/` (frontend, served at `/`), `tests/`, and `mock/` (dev/test-only mock croit API). JSON endpoints go under `/api/...`; the static mount at `/` is registered last so it never shadows them or `/health`.

### Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # setup
cp .env.default .env   # then fill in the REQUIRED values
.venv/bin/python3 app.py                       # run locally, same as appmotel
.venv/bin/python3 mock/croit_mock.py           # mock croit API on :18080 (CROIT_URL=http://127.0.0.1:18080/api), prints its tokens
CROIT_MOCK_S3=moto .venv/bin/python3 mock/croit_mock.py   # same, plus a moto S3 server on :18081 (S3_ENDPOINT_URL=http://127.0.0.1:18081) mirroring its buckets/keys
.venv/bin/ruff check .                         # lint
.venv/bin/pytest                               # all tests
.venv/bin/pytest tests/test_config.py::test_parsing   # single test
```

`mock/croit_mock.py` is an in-memory FastAPI mock of the croit API (seeded users `alice`, `bob`, `acme$carol`, admin and viewer API tokens, `admin`/`admin` login). Tests use `create_app(task_seconds=0)` in-process and `tests/test_croit_mock.py` validates its responses against `docs/api/api-1.yaml`. It never imports from `bucketeer/`, and `bucketeer/` never imports it. Options are the `CROIT_MOCK_*` variables listed in its docstring.

S3 tests use a session-wide `moto_server` (moto `ThreadedMotoServer` on a free port) and the per-test `s3_endpoint` fixture (resets moto) from `tests/conftest.py`. `mock/s3_bridge.py` implements `MockHooks` so seeded and later-created croit buckets, versioning/object-lock flags and deletions (objects and versions included) appear in moto, created with the owner's own keys; `tests/test_s3_backend.py` shows the wiring. Tenant bucket `acme:reports` is `reports` in moto. **moto does not enforce RGW identities:** it accepts any credentials and has one account, so ownership/authorization (a user touches only their own uid and buckets) must be tested at the Bucketeer layer, not through moto.

Settings read `./.env` only (no upward search). Tests never read a real `.env`: the fixture clears the settings variables and tests build settings with `Settings(_env_file=None, **REQUIRED)` (see `tests/conftest.py`).

## Stack constraints (set by the owner)

- **Backend:** Python + FastAPI. It is the only component that talks to the croit API. The browser never calls croit directly and never sees a croit token.
- **Frontend:** plain HTML, CSS, and vanilla JavaScript. **No JS frameworks or build step** (no React/Vue/Svelte/Tailwind, no bundler). FastAPI serves the static files.
- **The frontend must look exceptionally polished.** This is a top-level requirement, not an extra. Use a deliberate visual design (typography, spacing, motion, light/dark) and don't settle for default or template styling.

### Frontend layout and rules

- `static/index.html` loads `css/{tokens,base,components,login,app}.css`, a tiny classic `js/theme.js` (sets `data-theme` before paint) and the module `js/app.js`, which switches between `views/login.js` and `views/dashboard.js` based on `/api/me`. `static/showcase.html` (not linked) demonstrates every component; open it at `/showcase.html` to review UI changes in light and dark.
- Web components live in `js/components/` and render into the light DOM (no Shadow DOM) so tokens and theming just work: `secret-field`, `code-panel` (tabs, copy, download, INI highlighter in `js/ini.js`), `toast-region` (`toast.show/error`), `confirm-dialog` (`confirmDialog({...requireText})`, native `<dialog>`).
- **Always call `/api` through `js/api.js`** (`api()`, `getMe`, `login`, `logout`): it sends `X-Requested-With: bucketeer` and the session cookie, throws `ApiError`, and fires `bucketeer:unauthorized` on 401 so the app returns to login. Never use bare `fetch` for `/api`.
- **Design-token rule:** colours, spacing, radii, shadows, type sizes and motion values come from `css/tokens.css` (`light-dark()` plus `[data-theme]` override); never hard-code them in other CSS. Orange (`--accent`, #D73F09) is the only accent; check AA contrast for any new text/background pair. Everything must honour `prefers-reduced-motion`. No OSU logo or wordmark.
- **CSP rule:** `main.py` sends a strict CSP (`script-src 'self'; style-src 'self'`), so no inline `<script>`, `<style>`, `style=` attributes or `on*` handlers; use external files, classes and `element.style` (CSSOM). Build DOM with `js/dom.js` `h()`; never `innerHTML` with dynamic data. `tests/test_static.py` enforces this. Fonts (Inter, Fraunces, JetBrains Mono; all OFL, licenses next to them) are self-hosted in `static/fonts/`.

## Configuration

- All runtime config comes from environment variables loaded from `.env`. Use `python-dotenv` without override, so variables already in the process environment (such as the ones systemd sets) take precedence.
- `.env` is git-ignored and holds real values and secrets. `.env.default` is committed and documents every variable. Each one is marked either:
  - **REQUIRED**: left empty in the file, and the app refuses to start until it is set. Never fall back to a placeholder.
  - **Default**: the value in the file must match the default in the code, so a missing line behaves the same as the template.
- Whenever you add a variable to the code, add it to `.env.default` with the same kind of comment.
- `PORT` is required and is the port the server listens on. `.env.default` must define it.
- Put comments on their own lines. systemd's `EnvironmentFile` treats a trailing `# ...` as part of the value.

## Authentication

Users log in with Active Directory credentials over LDAPS, using the `LDAP_*` settings. The portal does its own AD login and does not use croit's `/auth/login`. The flow:
1. Bind as the service account (`LDAP_BIND_DN`).
2. Search `LDAP_USER_FILTER`, with `{login}` replaced by the LDAP-escaped text from the login form. The filter matches `sAMAccountName`, `mail` or `userPrincipalName`, so **both username and email address work**.
3. Require **exactly one** match. `mail` is not unique in AD, so refuse an ambiguous login rather than picking one.
4. Check membership of `LDAP_REQUIRED_GROUP_DN`. Use the `LDAP_MATCHING_RULE_IN_CHAIN` OID (1.2.840.113556.1.4.1941) so nested groups count.
5. Bind as the found DN with the user's password to verify it. Reject empty passwords: AD treats an empty password as an unauthenticated bind, and that bind succeeds.
6. Take the user's identity (and RGW uid) from `LDAP_USERNAME_ATTRIBUTE` (`sAMAccountName`), never from the typed login. That way username and email logins reach the same S3 user.

**Code:** `bucketeer/ldapauth.py` (`AdAuthenticator`, ldap3 run via `asyncio.to_thread`) and `bucketeer/auth.py`.
- LDAPS only: `Settings` refuses `ldap://` URLs (and a filter without `{login}`). URLs are tried in order for the service bind; user bind and group checks stay on that server. `{login}` is always RFC 4515-escaped. Group checks are a base-scope search on the user's DN with `memberOf:1.2.840.113556.1.4.1941:=<group>`.
- Failures: `InvalidCredentials` (wrong password, unknown user, ambiguous match, not in group, empty input; one uniform message to the user, the specific `.reason` only in the INFO log and audit `details`) versus `LdapUnavailable` (503). Empty passwords, and ones ldap3's SASLprep would reject (control characters), are refused as bad passwords before any connection; otherwise they would show up as 503 and tell an attacker which accounts exist. A rejected service bind fails the login at once (503) without trying other servers, so a stale service password cannot lock the service account; only connection errors fail over. Never log or audit a password.
- Tests inject a fake connector (`AdAuthenticator(settings, connector)`; `create_app(..., authenticator=, clock=)`), see `tests/test_auth.py`. No network.
- **Session:** stateless signed cookie (itsdangerous, `SESSION_SECRET`), name `__Host-bucketeer` (`bucketeer` when `SESSION_COOKIE_SECURE=false`), `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, `Max-Age=SESSION_MAX_AGE`. Payload is only `username` (the sAMAccountName, also the RGW uid), `display_name`, `email`, `is_admin`, `issued_at`; never a password or S3 key. Expiry is also enforced server-side from `issued_at`. Every login issues a new token. Because it is stateless, logout cannot revoke a copied cookie before it expires.
- **Routes:** `POST /api/login` (JSON `{login, password}`), `POST /api/logout`, `GET /api/me`. Statuses: 401 uniform failure, 429 throttled (`Retry-After`), 503 LDAP down, 400 malformed body. Login/logout write audit actions `login.success`, `login.failure`, `login.throttled`, `logout`.
- **Dependencies for later endpoints:** `Depends(auth.require_user)` returns the `SessionUser` (401 if none), `Depends(auth.require_admin)` adds a 403 for non-admins. Take the identity from the session, never from the request.
- **CSRF rule:** every state-changing (non GET/HEAD/OPTIONS) `/api` request must send `X-Requested-With: bucketeer`, else 403. `create_app` enforces it by including all `/api` routers into one `APIRouter(prefix="/api", dependencies=[Depends(csrf_protect)])`: add new routers there (`api.include_router(...)`), never register `/api` routes straight on `app`. The frontend's fetch helper must always send the header. Never add CORS middleware that allows that header, and never make a GET change state.
- **Throttle:** in-memory `LoginThrottle`, constants in `auth.py`: 5 attempts per normalized typed login, 5 password checks per resolved account (`resolve()` then `verify()`, so sAMAccountName/mail/UPN count together and stay below AD's lockout threshold) and 20 per client IP in 15 minutes, then 429 without contacting LDAP. Attempts count when they start and are refunded on success or LDAP outage. It resets on restart (single process assumed). The IP is `request.client.host`, which is correct only because uvicorn runs with `proxy_headers` and `forwarded_allow_ips="127.0.0.1"`.

The backend calls croit as a single service identity, with `CROIT_API_TOKEN` sent as a Bearer token. Because of that, the backend must make sure a user only ever reads or changes their own RGW uid.

## Deployment: appmotel

The app has to deploy unmodified on [appmotel](https://github.com/dirkpetersen/appmotel) (`appmo add bucketeer <user/repo>`). appmotel is a small PaaS: systemd user services behind a Traefik HTTPS reverse proxy. Its detection rules shape the repo layout:

- **Entry point:** `app.py` at the repo root. appmotel starts it as `.venv/bin/python3 app.py` with the repo root as the working directory, so `app.py` must call `uvicorn.run(...)` itself, reading `PORT` from the environment. There is no `start.sh` or Procfile.
- **Dependencies:** `requirements.txt` at the repo root. appmotel creates `.venv` and runs `pip install -r requirements.txt`. It does **not** install from `pyproject.toml`.
- **`install.sh`** (optional) runs on every deploy and update, from the repo root.
- **`.env` handling:** on first deploy, appmotel copies `.env.default` to its config directory as `.env` and symlinks `repo/.env` to that copy. It also passes the file to systemd as `EnvironmentFile`. It replaces any `PORT` below 10001 with a port it assigns itself (10001–59999), so a low default such as `PORT=8000` works for local development. Operators edit the live file with `appmo env bucketeer`.
- **Value restrictions:** appmotel's `.env` parser skips any value containing `$(`, a backtick, `;`, `&&`, `||`, or an unquoted `|`. Secrets with those characters, such as a croit service-account password, must be quoted, or avoid those characters.
- **Proxy:** Traefik terminates TLS and forwards to `http://localhost:$PORT`. Bind to `127.0.0.1`, and run uvicorn with `proxy_headers=True` and `forwarded_allow_ips="127.0.0.1"` so request scheme and URLs come out as `https`. Mark session cookies `Secure`.
- **Health check:** set `HEALTH_CHECK_PATH=/health` in `.env.default` and serve a cheap `GET /health` that doesn't call croit. Traefik polls it every 30s.
- **Layout gotcha:** a top-level or `src/` directory named `frontend`, `backend`, `api`, `worker`, `db` or `cache` that contains a `requirements.txt`, `package.json`, `pyproject.toml` or `go.mod` switches appmotel into multi-component mode. Keep everything in a single component: one `requirements.txt` at the root, and static assets in a directory such as `static/`.
- appmotel's `.env` also accepts `MEMORY_LIMIT`, `CPU_QUOTA`, `RATE_LIMIT_AVG`, `RATE_LIMIT_BURST` and `DISABLE_RATE_LIMIT`.

## croit API

- Spec: `docs/api/api-1.yaml` (OpenAPI 3.1, ~40k lines). Online docs: https://www.croit.io/api-docs. The file is too big to read whole, so grep for `^  /<path>:` and for `^    <SchemaName>:`.
- The spec has no `servers` entry, so the croit base URL has to come from an environment variable (see Configuration).

Endpoints this app relies on:

| Purpose | Call | Notes |
|---|---|---|
| Log in | `POST /auth/login` | HTTP Basic auth header + form body `grant_type=client_credentials`. Returns `Token` (`access_token`, `expires_in`, `roles`, `passwordNeedsChange`). Works for croit users and LDAP users when LDAP is configured. `POST /auth/login-form` is the variant that takes `grant_type=password` |
| Authenticated calls | `Authorization: Bearer <access_token>` | API tokens are sent this way directly and skip `/auth/login` |
| Session check/refresh | `GET /auth/token-info` | Calling it also extends the session |
| OIDC login (optional) | `/auth/oidc/*` | |
| Read S3 keys | `GET /s3/users/{uid}/keys` | Returns `RgwUserKey[]`: `{user, access_key, secret_key}`, and the keys can be null |
| Endpoint discovery | `GET /s3/users/{uid}/cfg-file/download` | Returns a `.s3cfg` whose text includes a running RGW gateway address and the DNS name. Useful for filling in `endpoint_url` |
| Create S3 user | `POST /s3/users` | `NewUserRequest`: `uid` and `name` are required. RGW rejects duplicate emails |
| Change keys | `PUT /s3/users/{uid}/keys` | **Replaces the whole key set** for the user and subusers, so any key left out is deleted. Always read-modify-write. Returns an async task |
| Add / remove one key | `PUT` / `DELETE /s3/users/{uid}/keys/{accessKey}` | Use these for creating and rotating keys |
| Suspend user | `PATCH /s3/users/{uid}` | `RgwUserUpdateRequest.suspended`, used by offboarding |
| Quotas | `PUT /s3/users/{uid}/quota`, `PUT /s3/buckets/{bucketName}/quota` | |
| Buckets | `PUT` / `PATCH` / `DELETE /s3/buckets/{bucketName}` | Create and delete return tasks. Deleting a non-empty bucket needs `deleteData` plus the confirmation sentence and the `ignoreWarning` token croit names in its first refusal (400 `IgnoreWarningResponse`) |

- RGW uids can carry a tenant (`tenant$uid`). URL-encode the `$` in path parameters.
- Errors come back as the `ErrorMessage` schema.
- `docs/api/s3-features.md` lists every S3 endpoint croit offers, and the S3 features users have to manage through the S3 protocol instead. `docs/api/README.md` covers how to get croit tokens.
- **Client:** `bucketeer/croit.py` (`CroitClient`, async httpx). `create_app` builds one per app (`app.state.croit`, `get_croit` dependency) and closes it on shutdown; tests pass `transport=httpx.ASGITransport(app=mock)` (see `tests/test_croit_client.py`). Methods return croit's JSON as plain dicts; task-returning calls (`create_bucket`, `delete_bucket`) return the task for `wait_for_task`.
  - Errors: `CroitError(status, code, message, details)` for non-2xx; `CroitWarning` (a `CroitError`) for a 400 `ignorableWarning`, with `.token` kept out of str/repr; `CroitUnavailable` for network/timeout; `CroitTaskFailed`/`CroitTaskTimeout` from `wait_for_task`.
  - Every path parameter goes through `_path()` (`quote(value, safe="")`: `tenant$uid` -> `tenant%24uid`, `/` can't add segments). Never build a path by hand.
  - There is no whole-set `PUT .../keys` method on purpose. `delete_bucket_and_data` runs the non-empty delete handshake and only with `user_confirmed=True`.
- Planned work, including a mock croit API, is tracked in `TODO.md`. Questions for the owner go in `QUESTIONS.md`. Don't guess at product decisions: ask there and work on something else.

## Generated AWS config

Formats in the two files differ, so keep them straight:
- `~/.aws/credentials`: `[<profile>]` with `aws_access_key_id` / `aws_secret_access_key`.
- `~/.aws/config`: `[profile <profile>]` (bare `[default]` for the default profile) with `endpoint_url` pointing at the RGW gateway, plus `region`. Ceph doesn't need a region, but the AWS SDKs do. AWS CLI v2 and current boto3 honor `endpoint_url` in the config file.

## `tools/`: standalone croit scripts

`tools/` holds admin scripts that use the croit API but are **not part of Bucketeer**. Keep them independent:
- Single-file, executable, standard library only.
- Configured through the shared `CROIT_*` environment variables (see `tools/README.md`), not Bucketeer's `.env`.
- Never import anything from the app, and never import tools code into the app.

`tools/croit-cert` installs the RGW/HAProxy TLS certificate through `PUT /config/rgw-ssl-cert`. When changing it:
- Keep it idempotent and keep its exit codes stable (0 ok/unchanged, 1 error, 3 `--verify` mismatch), since it is meant to run from cron or a certbot hook.
- Never call `/auth/logout`. croit allows one session per user, so logging out also ends that user's browser sessions.

## Product design (decided by the owner)

### Two API planes

- **croit API** (admin plane, with `CROIT_API_TOKEN`): S3 users, keys, quotas, suspension, usage, and bucket create/delete, as listed in `docs/api/s3-features.md`.
- **S3 API** (data plane, boto3 against `S3_ENDPOINT_URL`): everything croit lacks. That covers lifecycle, CORS, object-lock retention and legal hold, tagging, notifications, presigned URLs, bucket policies and ACLs.
  - S3 calls run with **the logged-in user's own keys**, fetched from croit on the server, so RGW itself enforces ownership, policies and object lock.
  - Never use an admin S3 identity for user actions. Keys stay on the server and never reach the browser except on the credentials screen.

### Users and access

- Login requires membership of the AD group `LDAP_REQUIRED_GROUP_DN`. Opening it to all users later just means clearing that setting.
- The RGW uid is the AD `sAMAccountName`. The admin page can map exceptions, such as existing RGW users with different names.
- The first login creates the RGW user automatically, with the default quota from the admin page.
- Admins are members of `LDAP_ADMIN_GROUP_DN`. They see all users and buckets, set quotas, suspend users, and edit settings.
- **Offboarding:** a daily job suspends (`PATCH /s3/users/{uid}` with `suspended`) every RGW user whose AD account is disabled or no longer in the group, together with that user's bucket keys. It never deletes anything; an admin decides what happens to the data.

### Bucket keys: keys scoped to one bucket

S3 keys belong to users, not buckets, so a bucket-scoped key needs its own identity:
- **Create:** make a separate RGW user (e.g. `alice.backup-job`) and give it a zero user quota, so it can't store data in buckets of its own. Writes into the shared bucket count against the bucket owner's quota. Verify this on the real cluster.
- **Grant access:** add a bucket-policy statement that gives this user read-only or read-write access to that one bucket.
- **Ownership:** record the key's owner in SQLite.
- **Revoke:** delete the RGW user and its policy statement.
- **Offboarding:** bucket keys are suspended along with their owner.

Subusers (read/write/readwrite/full across *all* of a user's buckets) are offered only under Advanced.

### Sharing

A simple, always-visible form shares a bucket with an AD user, read-only or read-write, by adding a bucket-policy statement. Policies can't reference AD groups, so sharing with a group expands it to its members' uids at share time.

### Bucket policy and lifecycle are shared documents

Several features write the same per-bucket policy and lifecycle configuration: bucket keys, sharing, simple lifecycle rules, and the Advanced raw editors.
- Read-modify-write them, and only touch statements or rules whose `Sid`/`ID` starts with `bucketeer-` (e.g. `bucketeer-key-<id>`, `bucketeer-share-u-<uid>` or `bucketeer-share-t-<tenant>-<uid>`).
- Leave every other statement intact.
- The raw editors show everything but must warn before editing `bucketeer-*` entries.
- Policy principals are `arn:aws:iam::<tenant>:user/<uid>`, with an empty tenant when there is none.
- **Always use `bucketeer/s3docs.py`** (pure document functions plus `update_bucket_policy` / `update_bucket_lifecycle` / `update_bucket_tagging`). Never call `put_bucket_policy`, `put_bucket_lifecycle_configuration` or `put_bucket_tagging` directly. The helpers refuse to write non-`bucketeer-*` entries, raise on duplicate Sids/IDs, and delete the document when the last entry goes (S3 rejects empty ones). Builders: `share_statement`, `bucket_key_statement`, and the three simple lifecycle rules. Their action sets are documented in the module docstring.
- The helpers are synchronous: call them with `asyncio.to_thread`. There is no conditional write, so they take `bucket_lock(bucket)` (per-bucket, re-entrant); hold it yourself around any multi-step change to one bucket. Writers outside this process can still cause a lost update.

### Admin API

`bucketeer/admin.py`, router `/api/admin` included in the parent `/api` router (CSRF applies); every route depends on `require_admin`. `GET /users` (uid, name, email, tenant, suspended, `quota`/`bucketQuota` as `{enabled, maxSize, maxObjects}` with null = unlimited, `bucketCount`, `bucketKey` when the uid is a live bucket-key user) and `GET /buckets` (name, tenant, owner, objects, bytes, quota, `budgetNumber` joined from the `buckets` table, null = missing, not validated: Q2). `PUT /users/{uid}/quota`, `PUT /buckets/{bucket}/quota` (body `{enabled, maxSize, maxObjects}`, bytes/counts 0..2^62 or null; enabled needs a limit; `enabled:false` keeps stored limits). `POST /users/{uid}/(un)suspend` PATCHes croit and (un)suspends the owner's live `bucket_keys` (croit user plus `suspended_at`), never deletes; `bucket_keys.suspended_by` (migration v2: `owner`, `direct`, reserved `offboarding`) records who suspended a key: unsuspending an owner wakes only `owner` rows (none if the owner was not suspended), a directly suspended key wakes only when unsuspended directly, revoked keys never wake; an admin cannot suspend themselves (409); a key failure still audits and answers 502. Mutations audit `admin.user.quota|suspend|unsuspend`, `admin.bucket.quota` with before/after, no secrets. croit errors map through `croit_http_error` (404, 400, 502, 503) without croit's URL or token. Key owner lookup uses the uid as the AD username until Q4 (uid mapping) is answered. Tests: `tests/test_admin.py`.

### Always visible vs. Advanced

- **Always visible:**
  - credentials and config snippets
  - key create, rotate and delete. Rotation adds the new key before removing the old, never with a whole-set PUT that drops keys.
  - bucket list, create and delete. Deleting a non-empty bucket requires typing its name.
  - versioning
  - usage and quota display
  - sharing and bucket keys
  - presigned share links (at most 7 days)
  - simple lifecycle rules: expire after N days, expire noncurrent versions, abort incomplete multipart uploads
- **Advanced:** one global toggle, remembered per user:
  - the raw bucket-policy editor and ACLs
  - CORS and tagging
  - object lock: offered only in the create-bucket dialog with a can't-be-undone warning, plus retention and legal hold
  - the full lifecycle editor
  - notifications: endpoints only from the admin-defined list
  - multipart cleanup
  - subusers
  - placement and storage class choice, shown only when more than one exists
- **Budget numbers:** every bucket must have a budget number (index/worktag) when it is created. Users can store a default in their profile, which prefills the create form. Format, storage and editing details: see QUESTIONS.md Q2.
- There is no object/file browser. Presigned share links choose the object with a key field plus prefix autocomplete from a read-only listing (Q1).

### Admin page and storage

- Runtime policy is edited on the admin page, not in `.env`: bucket naming rules, placement defaults, default quota, uid mappings, and the notification endpoint allowlist. `.env` keeps only infrastructure and secrets.
- State lives in **SQLite** under `DATA_DIR`. That covers settings, uid mappings, bucket-key ownership and the **audit log** of who created or deleted buckets, keys and shares.
- `DATA_DIR` defaults to `data/` in the repo (git-ignored). appmotel updates use `git reset --hard`, which keeps ignored files, and its backups and restores tar the repo, so the database is included.
- Assume a single uvicorn process: SQLite plus in-process background jobs such as the offboarding sync.
- **Code:** `bucketeer/db.py`. The file is `DATA_DIR/bucketeer.sqlite3` (WAL; file and `-wal`/`-shm` 0600; a data dir Bucketeer creates is 0700, an existing one keeps its mode and logs a warning if group/other can access it). `create_app` opens it in the lifespan (`app.state.db`, `get_db` dependency) and closes it on shutdown; tests pass `create_app(settings, db=Database(":memory:"))`. `/health` never touches it.
  - One connection behind an RLock; repositories (`db.settings`, `uid_mappings`, `profiles`, `buckets`, `bucket_keys`, `audit`) are sync and return plain dicts. Async handlers call `await db.run(db.buckets.get, name)`. Multi-statement writes use `with db.tx():`.
  - **Migrations:** `MIGRATIONS` in `db.py`, tracked by `PRAGMA user_version`, each applied in one transaction at startup. Append a new migration, never edit an applied one. The app refuses to start on a database newer than the code.
  - **Audit log** is append-only: use `db.audit.add(...)` (no update/delete helpers exist). Never put secrets in `details`; keys named like secret/password/token are redacted as a safety net, not a license.
  - `settings` takes caller-supplied defaults (`db.settings.get(key, default)`); the schema holds no policy values. `buckets.budget_number` is nullable and unvalidated until Q2 is answered.

### Later features

These need cluster work first:
- **Server-side encryption** needs a key server (Vault, Barbican or KMIP).
- **Static website hosting** needs RGW website mode and wildcard DNS.
- **STS temporary credentials** need an STS key and roles on RGW. They would also give short-lived bucket-scoped keys.
