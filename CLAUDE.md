# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Bucketeer is a self-service S3 portal for Ceph clusters managed by **croit**. Users log in with AD, get their S3 (RGW) access/secret keys with ready-to-paste `~/.aws/config` and `~/.aws/credentials` snippets, and manage their buckets: create, delete, versioning, sharing, scoped keys, lifecycle, and the full set of S3 bucket features. Admins get an admin page. See "Product design" below for decisions already made, and `TODO.md` for the phased plan.

The repo is greenfield; see `TODO.md` for what exists so far. Code layout: `app.py` (uvicorn entry point), the `bucketeer/` package (`config.py` settings, `main.py` app factory, `croit.py` croit client, `db.py` SQLite storage, `s3docs.py` shared bucket policy/lifecycle/tag helpers, `ldapauth.py` AD login, `auth.py` sessions/CSRF/throttle/login routes, `rgw_user.py` RGW user resolution, `credentials.py` keys/snippet API, `admin.py` admin API, `admin_settings.py` + `policy_settings.py` admin settings, `profile.py` + `budget.py` user profile, `s3client.py` user S3 clients, `buckets.py` bucket API, `bucket_keys.py` bucket keys, `lifecycle.py` simple lifecycle rules, `share_links.py` presigned share links, `sharing.py` sharing with AD users, `audit_viewer.py` admin audit log viewer), `static/` (frontend, served at `/`), `tests/`, and `mock/` (dev/test-only mock croit API). JSON endpoints go under `/api/...`; the static mount at `/` is registered last so it never shadows them or `/health`.

### Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # setup
cp .env.default .env   # then fill in the REQUIRED values
.venv/bin/python3 app.py                       # run locally, same as appmotel
.venv/bin/python3 mock/croit_mock.py           # mock croit API on :18080 (CROIT_URL=http://127.0.0.1:18080/api), prints its tokens
CROIT_MOCK_S3=moto .venv/bin/python3 mock/croit_mock.py   # same, plus a moto S3 server on :18081 (S3_ENDPOINT_URL=http://127.0.0.1:18081) mirroring its buckets/keys
.venv/bin/python3 mock/demo.py                # local demo: moto + mock croit + Bucketeer, fake users (demo password printed), http://127.0.0.1:8000
.venv/bin/ruff check .                         # lint
.venv/bin/pytest                               # all tests
.venv/bin/pytest tests/test_config.py::test_parsing   # single test
```

`mock/croit_mock.py` is an in-memory FastAPI mock of the croit API (seeded users `alice`, `bob`, `acme$carol`, admin and viewer API tokens, `admin`/`admin` login). Tests use `create_app(task_seconds=0)` in-process and `tests/test_croit_mock.py` validates its responses against `docs/api/api-1.yaml`. It never imports from `bucketeer/`, and `bucketeer/` never imports it. Options are the `CROIT_MOCK_*` variables listed in its docstring.

S3 tests use a session-wide `moto_server` (moto `ThreadedMotoServer` on a free port) and the per-test `s3_endpoint` fixture (resets moto) from `tests/conftest.py`. `mock/s3_bridge.py` implements `MockHooks` so seeded and later-created croit buckets, versioning/object-lock flags and deletions (objects and versions included) appear in moto, created with the owner's own keys; `tests/test_s3_backend.py` shows the wiring. Tenant bucket `acme:reports` is `reports` in moto. **moto does not enforce RGW identities:** it accepts any credentials and has one account, so ownership/authorization (a user touches only their own uid and buckets) must be tested at the Bucketeer layer, not through moto.

**Demo code lives only in `mock/demo.py`.** It builds its own Settings (never the real `.env` or Settings env vars), injects a fake directory (`DemoDirectory`, the real authenticator interface) through `create_app(authenticator=...)`, uses a temp `DATA_DIR`, plain-http cookies and 127.0.0.1 only. The one production-side hook is `app.state.demo` (always False in `create_app`, set True only by `mock/demo.py`) behind the public `GET /api/demo`, which the frontend uses for the "Demo mode" pill. Never add a setting, env var or import of `mock/` in `bucketeer/` to enable it; `tests/test_demo.py` enforces this.

Settings read `./.env` only (no upward search). Tests never read a real `.env`: the fixture clears the settings variables and tests build settings with `Settings(_env_file=None, **REQUIRED)` (see `tests/conftest.py`).

## Stack constraints (set by the owner)

- **Backend:** Python + FastAPI. It is the only component that talks to the croit API. The browser never calls croit directly and never sees a croit token.
- **Frontend:** plain HTML, CSS, and vanilla JavaScript. **No JS frameworks or build step** (no React/Vue/Svelte/Tailwind, no bundler). FastAPI serves the static files.
- **The frontend must look exceptionally polished.** This is a top-level requirement, not an extra. Use a deliberate visual design (typography, spacing, motion, light/dark) and don't settle for default or template styling.

### Frontend layout and rules

- `static/index.html` loads `css/{tokens,base,components,login,app}.css`, a tiny classic `js/theme.js` (sets `data-theme` before paint) and the module `js/app.js`, which switches between `views/login.js` and `views/dashboard.js` based on `/api/me`. `static/showcase.html` (not linked) demonstrates every component; open it at `/showcase.html` to review UI changes in light and dark.
- Web components live in `js/components/` and render into the light DOM (no Shadow DOM) so tokens and theming just work: `secret-field`, `code-panel` (tabs, copy, download, INI highlighter in `js/ini.js`), `toast-region` (`toast.show/error`), `confirm-dialog` (`confirmDialog({...requireText})`, native `<dialog>`), `menu-button` (`menuButton({label, ariaLabel, items:[{label, icon, hint, badge, danger, disabled, reason, separatorBefore, onSelect}]})`: manual popover/bottom sheet on phones, flips up, full menu-button keyboard pattern; the bucket row keeps versioning and Share visible and puts Keys, Lifecycle, Share link and Delete (last, danger) in "More").
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
- **Session:** stateless signed cookie (itsdangerous, `SESSION_SECRET`), name `__Host-bucketeer` (`bucketeer` when `SESSION_COOKIE_SECURE=false`), `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, `Max-Age=SESSION_MAX_AGE`. Payload is only `username` (the sAMAccountName; the RGW uid is resolved from it, see "RGW user resolution"), `display_name`, `email`, `is_admin`, `issued_at`; never a password or S3 key. Expiry is also enforced server-side from `issued_at`. Every login issues a new token. Because it is stateless, logout cannot revoke a copied cookie before it expires.
- **Routes:** `POST /api/login` (JSON `{login, password}`), `POST /api/logout`, `GET /api/me`. Statuses: 401 uniform failure, 429 throttled (`Retry-After`), 503 LDAP down, 400 malformed body. Login/logout write audit actions `login.success`, `login.failure`, `login.throttled`, `logout`.
- **Dependencies for later endpoints:** `Depends(auth.require_user)` returns the `SessionUser` (401 if none), `Depends(auth.require_admin)` adds a 403 for non-admins. Take the identity from the session, never from the request. Endpoints that act on the user's RGW account use `Depends(rgw_user.require_rgw_user)` instead (see below).
- **CSRF rule:** every state-changing (non GET/HEAD/OPTIONS) `/api` request must send `X-Requested-With: bucketeer`, else 403. `create_app` enforces it by including all `/api` routers into one `APIRouter(prefix="/api", dependencies=[Depends(csrf_protect)])`: add new routers there (`api.include_router(...)`), never register `/api` routes straight on `app`. The frontend's fetch helper must always send the header. Never add CORS middleware that allows that header, and never make a GET change state.
- **Throttle:** in-memory `LoginThrottle`, constants in `auth.py`: 10 attempts per normalized typed login, 10 password checks per resolved account (`resolve()` then `verify()`, so sAMAccountName/mail/UPN count together and stay below AD's lockout threshold) and 20 per client IP in 15 minutes, then 429 without contacting LDAP. The message stays "Too many attempts" (Q3). The limit of 10 is the owner's choice, so make sure it stays below the AD lockout threshold. Attempts count when they start and are refunded on success or LDAP outage. It resets on restart (single process assumed). The IP is `request.client.host`, which is correct only because uvicorn runs with `proxy_headers` and `forwarded_allow_ips="127.0.0.1"`.

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
- `~/.aws/config`: `[profile <profile>]` (bare `[default]` when the profile is `default`) with `region`, `endpoint_url` (the RGW gateway), `request_checksum_calculation` / `response_checksum_validation = when_required` (newer AWS SDKs/CLI send CRC checksums that many Ceph RGW versions reject) and `s3 =` / `addressing_style = <path|virtual|auto>` as the last, indented block. Ceph doesn't need a region, but the AWS SDKs do. AWS CLI v2 and current boto3 honor `endpoint_url` in the config file.

**Credentials API** (`bucketeer/credentials.py`, `/api/credentials`, uid only from the session via `require_active_rgw_user`, so suspended users get nothing): `GET` returns `{profile, region, endpointUrl, addressingStyle, keys:[{accessKey}]}` and **never a secret**; `POST /reveal {accessKey}` returns `{accessKey, secretKey}` only for a key found in the user's own croit key list (exact match, else 404), audits `credentials.reveal` (actor, uid, access key; **never the secret**) before answering, and sends `Cache-Control: no-store`. **Reveal rule:** a secret key may leave the server only through that endpoint; no other response, log line, audit entry or error may contain one. The snippets are built in the browser (`static/js/awsconfig.js`) from the GET data plus the revealed secret; `secret-field` takes a `fetcher` so the secret exists in memory only while revealed (60 s) and is dropped on re-mask, key change or navigation. Never put a secret in a DOM attribute, URL, storage or console.
- **Key management** (same router, `require_active_rgw_user`, CSRF, audited `credentials.key.create|rotate|delete|rotate_failed` with uid/new key/`old_key_removed`, never a secret): `POST /keys` -> `{accessKey}` (max `MAX_KEYS`=5 own keys, else 409; the secret is only available through reveal); `POST /keys/{accessKey}/rotate` -> `{accessKey, oldKeyRemoved, message?}` (own `user == uid` key, else 404; put new key, confirm it in croit's list, only then delete the old; a failed delete is still 200 with `oldKeyRemoved:false`; an unconfirmed new key keeps the old and answers 502); `DELETE /keys/{accessKey}` (own key else 404; the last own key is refused with 409). Keys are generated by `generate_key_pair()` (`secrets`; access 20 x [A-Z0-9], secret 40 x [A-Za-z0-9]); a croit 409 ("belongs to another user") retries, a generated access key already in the list is skipped. Mutations hold a per-uid `KeyLocks` lock; no server cache holds key lists (the list is read from croit each time). `CroitClient.put_key` sends the secret as a query parameter and scrubs it from croit error text; never log or audit request URLs from it.
- **Endpoint:** `S3_ENDPOINT_URL`, else `https?://host_base` from the user's croit `.s3cfg` (`CroitClient.cfg_file`; comment lines ignored; cached 5 min, a miss 30 s, in `EndpointResolver`), else null and the UI tells users to ask an administrator.
- **Safe values:** `Settings` refuses at startup an `AWS_PROFILE_NAME` / `S3_REGION` / `S3_ENDPOINT_URL` outside a strict alphabet (no spaces, brackets, `=`, `#`, newlines), so nothing can add INI lines. `S3_ADDRESSING_STYLE` is `path` (default, RGW without wildcard DNS), `virtual` or `auto`.

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
- The RGW uid is the **lowercased** AD `sAMAccountName` (Q4). At login, an existing RGW user whose uid matches case-insensitively (checked against croit's uid list) is used instead, with an audit entry. Admin uid mappings always win.
- Logging in with an email address or UPN only finds the account: the AD search resolves it to the `sAMAccountName`. A uid is **never** derived from an email address.
- The first login creates the RGW user automatically with the default user quota. That default is **1 TiB** unless an admin changes it on the Settings tab, so a quota is always applied (Q4).
- **Never send an email address to croit when creating an RGW user (Q5).** `NewUserRequest` only requires `uid` and `name`, and email is optional, so the uid comes from the sAMAccountName (an email login is first resolved to the sAMAccountName through AD) and `email` stays empty. That also rules out RGW's duplicate-email rejection. Pass an email only if a future croit version makes it required, and then pass both.
- Admins are members of `LDAP_ADMIN_GROUP_DN`. They see all users and buckets, set quotas, suspend users, and edit settings.
- **Offboarding:** a daily job suspends (`PATCH /s3/users/{uid}` with `suspended`) every RGW user whose AD account is disabled or no longer in the group, together with that user's bucket keys. It never deletes anything; an admin decides what happens to the data.

### RGW user resolution

`bucketeer/rgw_user.py`, `app.state.rgw` (`RgwResolver`). **The RGW uid comes only from the server session, through `Depends(require_rgw_user)` (or `require_active_rgw_user`, which also refuses suspended users with 403); never read a uid from the path, query, body or headers for the user's own account.** `require_rgw_user` returns an `RgwAccount` (`username`, `uid`, `status`, `display_name`, `quota`, `reason`) and answers 503 (unavailable) or 409 (creation failed); a `suspended` account is returned, so check `status` or use the active variant. `GET /api/account` (never raises, no keys) returns `{uid, status, reason, message, displayName, quota, created}` for the dashboard.
- **Order:** admin uid mapping (case-insensitive AD name, uid used verbatim; the target must already exist, else `mapping_target_missing`, uncached, nothing created) -> lowercased sAMAccountName, `tenant$` prefixed when `S3_TENANT` is set, validated (`[a-z0-9][a-z0-9._@+-]{0,63}`, else `creation_failed`/`invalid_username`); an existing uid matching in the same tenant (exact) with the bare uid case-insensitive (croit `/s3/user-names`) is used with audit `rgw.user.case_match` (two such uids: `ambiguous_uid`, refused) -> refused as `uid_taken` (audit `rgw.user.uid_taken`) if that uid is any mapping target or any `bucket_keys.rgw_uid` (revoked too; `uid_claimed`), so an unmapped AD user never shares someone else's account -> auto-create (`CroitClient.create_user` with uid and AD display name only; never an email, Q5) then the default quota (admin Settings value, else 1 TiB / unlimited objects); audit `rgw.user.create` / `rgw.user.create_failed`. If the quota call fails after the create, the audit entry says `quota_applied: false`; the next resolve of that uid sees it as newest and retries (durable, no extra table; a later success entry `rgw.user.default_quota` settles it).
- **Statuses:** `ready`, `suspended`, `creation_failed` (`croit_rejected`: a 4xx on create that is not "already exists", cached for the TTL; invalid username; ambiguous uid; uid_taken; mapping_target_missing), `unavailable` (any croit trouble, other than a create 4xx).
- **Concurrency/cache:** per-username `asyncio.Lock`; a 400/409/422 from create re-checks `/s3/user-names` and counts "exists" as success. Results cached 5 min per username (`CACHE_TTL`), never `unavailable`. `invalidate(username=|uid=)` is called on admin mapping put/delete (AD name, old and new targets, and the targets' bare names) and suspend/unsuspend; other changes (offboarding later!) must call it too.
- **Login:** `RgwResolver.warm` resolves in a background task after a successful login (best effort; login never waits for croit). Tests switch it off (`warm_on_login`, autouse fixture in `tests/conftest.py`).
- Frontend: `accountStatus()` in `views/credentials.js` shows the state in the Credentials card.

### Share links (presigned URLs, Q1)

`bucketeer/share_links.py` (module docstring has the API shapes), tests `tests/test_share_links.py`, UI `components/share-link-dialog.js` (a "Share link" button per bucket row).
- `GET /api/buckets/{name}/objects?prefix=&limit=` is a read-only autocomplete (one `list_objects_v2` page, Delimiter `/`, `limit` 20 default / 50 max, `truncated`), **not a file browser**: no pagination, download, upload or delete, never extend it into one. `POST /api/buckets/{name}/share-link {key, expiresIn}` (60..604800 s; UI offers 1 h / 1 d / 7 d) checks the object with `head_object` (404 if missing), then presigns a SigV4 GET with the user's own s3client (public endpoint, configured addressing style) -> `{url, expiresAt}`. Both: `require_active_rgw_user`, croit-verified own bucket, `no-store`.
- **The URL is a bearer secret:** never log or audit it. The audit `bucket.share_link` holds bucket, key, `expiresAt`, `expiresIn` only (the scrubber redacts `Signature=` values as a backstop). `S3Clients` pins the `botocore`/`boto3` loggers to WARNING because botocore logs SigV4 signatures at DEBUG. The browser keeps the link in memory only and drops it when key/duration change or the dialog closes. The link dies if the user's key is rotated/deleted or the account is suspended (the dialog says so).

### Bucket keys: keys scoped to one bucket

S3 keys belong to users, not buckets, so a bucket-scoped key needs its own identity:
- **Create:** make a separate RGW user (e.g. `alice.backup-job`) and give it a zero user quota, so it can't store data in buckets of its own. Writes into the shared bucket count against the bucket owner's quota. Verify this on the real cluster.
- **Grant access:** add a bucket-policy statement that gives this user read-only or read-write access to that one bucket.
- **Ownership:** record the key's owner in SQLite.
- **Revoke:** delete the RGW user and its policy statement.
- **Offboarding:** bucket keys are suspended along with their owner.

**Code:** `bucketeer/bucket_keys.py` (module docstring has the API shapes), tests `tests/test_bucket_keys.py`, UI `components/bucket-keys-dialog.js` (a "Keys" button per bucket row).
- Own buckets only (croit-verified); uid only from the session. RGW uid `<owner>+<label>` (`tenant$owner+label`; `+` cannot occur in an AD sAMAccountName, so a key uid can never equal a human's uid, which dotted names like `john.smith` could; `s3docs` allows `+` in a principal ARN for key users only, never in Sids or shares; encode it as `%2B` everywhere, `_path` and httpx params do), label `[a-z0-9-]` 1-20; refused if the uid exists in croit (case-insensitively) or in `bucket_keys` (revoked too: a uid is **never reused**). Limits (Q9, confirmed by the owner): 10 live keys per user, 5 per bucket, else 409.
- **Create is a saga** under the per-user `KeyLocks` lock plus a per-bucket asyncio lock: pending row (migration v4 adds `state` pending|active|failed and `access_key`, the public half only) -> croit user (no email) -> user quota enabled with maxSize 0 and maxObjects 0, **read back and checked, fail closed** -> exactly one generated key (croit's own auto-key is removed) -> `bucket_key_statement` (Sid `bucketeer-key-<row id>`) via `update_bucket_policy` with the OWNER's s3client -> row active. Any failure rolls back statement and RGW user, except that a croit 4xx on the user create means the user is not ours (kept), and after a timeout the user is deleted only if its display name is ours; clean rollback = row `failed` + revoked, unfinished rollback = row stays live as `failed` and shows "Needs clean-up" until a revoke retries it. A crash can leave a live `pending` row the same way.
- **Startup clean-up:** `cleanup_pending` runs as a background task from the lifespan (cancelled on shutdown; never blocks startup or `/health`; switched off in tests via `CLEANUP_ON_START`). Pending rows older than 10 min get the same `_rollback` (statement, then the user only if its display name is ours, row failed+revoked), audited `bucket_key.cleanup` (actor `system`). If the owner has no usable S3 client the row becomes `failed` ("Needs clean-up", statement and user left for a revoke). croit down: retried 3 times over ~10 min, then left until next start. Active/revoked/young rows are never touched.
- **Revoke** (idempotent): statement first (the key is dead from then on), then croit delete user (404 = fine). If croit refuses (the key user owns buckets) the row stays live with an "ask an administrator" 502; retry is safe. Bucket delete through Bucketeer revokes the bucket's keys afterwards (`revoke_for_deleted_bucket`, never fails the delete; leftovers give a `warning`).
- **Reveal:** only the row's owner (others 404), refused when the key or its owner is suspended, audited `bucket_key.reveal` first, `no-store`; same secret rules as credentials. Snippets use profile `bucketeer-<label>`. Audit `bucket_key.create|create_failed|reveal|revoke|revoke_failed`.
- Defense in depth: `RgwResolver` re-checks `uid_claimed` after get/create and answers an uncached `uid_taken`; the admin suspend cascade treats a croit 404 for a `pending`/`failed` row as done.
- moto enforces no policies, so tests assert the policy document, not enforcement. Whether RGW honours a zero user quota for a policy-granted user is the open cluster check.

Subusers (read/write/readwrite/full across *all* of a user's buckets) are offered only under Advanced.

### Sharing

A simple, always-visible form shares a bucket with an AD user, read-only or read-write, by adding a bucket-policy statement. Policies can't reference AD groups, so sharing with a group expands it to its members' uids at share time.
- **Who can receive a share (Q8):** exactly the people who could log in. When `LDAP_REQUIRED_GROUP_DN` is set, only its members (nested membership counts); when it is empty, anyone in AD. A recipient without an S3 account yet gets one created on the spot, exactly as their first login would (same uid rules, default quota, audit), so the share works immediately and they only need to log in to get keys.
- **Group shares:** not decided yet (QUESTIONS.md Q11); build individual shares only.

**Code:** `bucketeer/sharing.py` (module docstring has the API shapes), tests `tests/test_sharing.py`, UI `components/share-dialog.js` ("Share" button per bucket row) and `views/shared-with-me.js` ("Shared with you" card).
- Recipients are exactly the people who could log in (Q8). `AdAuthenticator.lookup_user(login)` (also on `DemoDirectory`) shares `_find_entry` with login (service bind, escaped filter, exactly one, nested required group when set) and makes NO password bind; every failure is one uniform 404, directory down is 503. Lookups and share requests count against a per-owner limit (30 per 15 min, in memory) so it cannot enumerate AD; it is an exact lookup, never a directory search. No group shares (Q11).
- `POST /buckets/{name}/shares {login, access}` resolves the recipient with `RgwResolver.resolve` (auto-creates the S3 account exactly like first login: mapping, case match, `uid_taken`, default quota, no email), refuses yourself, a bucket-key uid and a suspended recipient, then writes `s3docs.share_statement` with the OWNER's s3client (policy first, then the `shares` row, migration v7; at most one live row per bucket+recipient uid, sharing again changes access). The statement is the source of truth: the list shows `active`, `out_of_sync` (repair = PUT), `unknown` (a `bucketeer-share-*` statement without a row; its Sid is the id; revocable) or `unchecked`. `PUT|DELETE /shares/{id}`; revoke removes the statement, then marks the row. Bucket delete marks shares revoked. `GET /api/shared-with-me` lists live rows for the session's uid (`address` = `tenant:bucket` for another tenant). Audit `bucket.share|share_update|unshare`.

### Bucket policy and lifecycle are shared documents

Several features write the same per-bucket policy and lifecycle configuration: bucket keys, sharing, simple lifecycle rules, and the Advanced raw editors.
- Read-modify-write them, and only touch statements or rules whose `Sid`/`ID` starts with `bucketeer-` (e.g. `bucketeer-key-<id>`, `bucketeer-share-u-<uid>` or `bucketeer-share-t-<tenant>-<uid>`).
- Leave every other statement intact.
- The raw editors show everything but must warn before editing `bucketeer-*` entries.
- Policy principals are `arn:aws:iam::<tenant>:user/<uid>`, with an empty tenant when there is none.
- **Always use `bucketeer/s3docs.py`** (pure document functions plus `update_bucket_policy` / `update_bucket_lifecycle` / `update_bucket_tagging`). Never call `put_bucket_policy`, `put_bucket_lifecycle_configuration` or `put_bucket_tagging` directly. The helpers refuse to write non-`bucketeer-*` entries, raise on duplicate Sids/IDs, and delete the document when the last entry goes (S3 rejects empty ones). Builders: `share_statement`, `bucket_key_statement`, and the three simple lifecycle rules. Their action sets are documented in the module docstring.
- The helpers are synchronous: call them with `asyncio.to_thread`. There is no conditional write, so they take `bucket_lock(bucket)` (per-bucket, re-entrant); hold it yourself around any multi-step change to one bucket. Writers outside this process can still cause a lost update.

### User S3 calls and the bucket API

- **`bucketeer/s3client.py` (`app.state.s3`, `S3Clients`) is the only way to make S3 calls for a user.** `await s3.call(uid, fn, *args)` builds a per-request boto3 client from the user's OWN first croit key (never from the browser; no admin S3 identity), endpoint as in credentials, addressing style, region, `when_required` checksums, modest retries/timeouts, and runs `fn(client, ...)` via `asyncio.to_thread`. No key -> 409, no endpoint -> 503. Tests spy on `app.state.s3.factory(endpoint, access, secret, settings)` to assert which key was used (`tests/test_buckets.py`).
- **Bucket API** (`buckets.py`, `/api/buckets`, `require_active_rgw_user`): `GET` (own buckets from croit `list_user_buckets`, short names, `budgetMissing`, `tagPending`; read-only), `GET /options`, `POST` (name via `validate_bucket_name`, budget required unless admin, placement only from the allowed list, duplicate -> 409, croit create + task, SQLite row, `bucketeer-budget` tag via `s3docs.update_bucket_tagging` with the user's key), `POST /reconcile`, `DELETE /{name}` `{confirmName?}`. URL names are the user's short names; the tenant is added server-side and a name with `:` or capitals is 404, ownership is checked in croit's list. Delete of a non-empty bucket answers 409 `{requiresConfirmation, objects, bytes}` until `confirmName` equals the name, then `delete_bucket_and_data(user_confirmed=True)`.
- **Create is idempotent for the user's own bucket:** if croit already lists the name, owned by the session uid and not recorded for another owner (e.g. an earlier create timed out), it is adopted (row + tag written, 201 with `adopted`/`note`), never another user's (409); creates hold a per-uid lock. croit task output is logged, never returned. A SQLite failure after croit created the bucket still answers 201 with a `warning`. If croit's placement list is unreadable the admin's default placement still applies.
- **Versioning toggle:** `PUT /api/buckets/{name}/versioning {enabled}` goes through croit `PATCH /s3/buckets/{name}` (`versioning` ENABLED/SUSPENDED; admin plane, the spec supports it cleanly and the mock bridge mirrors it into moto), ownership verified in croit, idempotent (no write when already in state; `enabled:false` on a never-versioned bucket is a no-op), refused 409 when suspending an object-lock bucket (checked with the user's own key via `s3client`; croit's details carry no lock flag). The list adds `objectLock` (true/false/null) and `versioning` off|enabled|suspended. Audited `bucket.versioning` with before/after. UI: a `.switch` per row with a confirm dialog on pause; disabled for object-lock buckets. Tests: `tests/test_versioning.py`.
- **Versioning default (Q7):** `POST /api/buckets` takes `versioning` (default true; `objectLock` implies it) and `noncurrentDays` (default 30, null = none, bounds as lifecycle, else 422 before croit is called). A versioned new bucket gets the `bucketeer-expire-noncurrent` rule via `s3docs.update_bucket_lifecycle` with the user's own key; failure keeps the bucket, 201 with `warning`, audit `bucket.create` has `noncurrentDays` and `noncurrentRuleApplied`. Adopted buckets are never touched (not created by us). Dialog: versioning ticked, "Delete old versions after N days" field shown while ticked.
- **Tag reconcile:** if the tag write fails the bucket is kept, `buckets.tag_synced = 0` (migration v3), the 201 carries `warning`; the list shows `tagPending` and the UI then calls `POST /reconcile` (a POST because it changes state; GETs never do). Any future budget edit must set `tag_synced` the same way.
- **Simple lifecycle rules** (`lifecycle.py`, `GET|PUT /api/buckets/{name}/lifecycle`, own bucket croit-verified, user's own S3 key, loaded lazily by the dialog, never in the list): GET -> `{expireCurrentDays, expireNoncurrentDays, abortMultipartDays (days|null), foreignRules (count of non-`bucketeer-*` rules), versioning}`; PUT takes all three (null removes) via `s3docs.update_bucket_lifecycle`, bounds from the `s3docs` builders (422), answers GET shape + `changed` + `hint` (noncurrent rule while versioning is off). Enabling or shortening `expireCurrentDays` needs `confirm: true`, else 409 `requiresConfirmation` and no write. Audit `bucket.lifecycle` (before/after, only when changed). Frontend: `components/lifecycle-dialog.js` (confirm dialog before the destructive save; suggests a noncurrent rule when versioning is on and none exists), `.lcd-*` in `buckets.css`.
- **Admin list/revoke:** `admin_router` in `bucket_keys.py` (it needs `revoke` helpers, and `admin.py` is imported by it, so the routes live here, mounted under `/api/admin`). `GET /api/admin/bucket-keys?state=live|revoked|all&owner=&bucket=` -> `{keys: [{id, ownerAd, ownerUid, bucket, bucketFull, label, access, uid, accessKey, state (revoked|active|pending|failed), suspended, suspendedBy, createdAt, revokedAt, statementPendingCleanup}]}`. **Admins never see bucket-key secrets** (no reveal for them, none in the list). `DELETE /api/admin/bucket-keys/{id}` order: 1) delete the key user in croit (credential dead; if croit refuses, suspend it and answer 502, row stays live), 2) remove the policy statement with the OWNER's s3client, skipped when the owner is suspended and tolerated when it fails, 3) row revoked, `statement_pending_cleanup` (migration v5) set when the statement was left (harmless: its principal no longer exists). Calling DELETE again on such a revoked row retries only the statement and clears the flag (no automatic retry elsewhere: known leftover otherwise). Audit `admin.bucket_key.revoke` (actor = admin, details owner, `statement_removed`, `statement_left` reason, `retry`) or `admin.bucket_key.revoke_failed`. Idempotent. UI: "Bucket keys" tab in `views/admin.js` (revoked rows behind "Show revoked"). Tests `tests/test_admin_bucket_keys.py`.
- Frontend: `views/buckets.js` (card), `components/create-bucket-dialog.js` (Advanced block only when the global toggle is on), `budget.js` (instant checks), `css/buckets.css`; `ApiError.body` carries extra error fields.

### Admin API

`bucketeer/admin.py`, router `/api/admin` included in the parent `/api` router (CSRF applies); every route depends on `require_admin`. `GET /users` (uid, name, email, tenant, suspended, `quota`/`bucketQuota` as `{enabled, maxSize, maxObjects}` with null = unlimited, `bucketCount`, `bucketKey` when the uid is a live bucket-key user) and `GET /buckets` (name, tenant, owner, objects, bytes, quota, `budgetNumber` joined from the `buckets` table, null = missing, not validated: Q2). `PUT /users/{uid}/quota`, `PUT /buckets/{bucket}/quota` (body `{enabled, maxSize, maxObjects}`, bytes/counts 0..2^62 or null; enabled needs a limit; `enabled:false` keeps stored limits). `POST /users/{uid}/(un)suspend` PATCHes croit and (un)suspends the owner's live `bucket_keys` (croit user plus `suspended_at`), never deletes; `bucket_keys.suspended_by` (migration v2: `owner`, `direct`, reserved `offboarding`) records who suspended a key: unsuspending an owner wakes only `owner` rows (none if the owner was not suspended), a directly suspended key wakes only when unsuspended directly, revoked keys never wake; an admin cannot suspend themselves (409); a key failure still audits and answers 502. Mutations audit `admin.user.quota|suspend|unsuspend`, `admin.bucket.quota` with before/after, no secrets. croit errors map through `croit_http_error` (404, 400, 502, 503) without croit's URL or token. Bucket-key owners and the self-suspend check go through `rgw_user.ad_names_for_uid` (uid -> AD names via uid mappings, else the uid's bare part when the tenant fits and the uid is neither claimed nor mapped from another name), since `bucket_keys.owner_ad_username` holds AD names; suspend/unsuspend also invalidate the resolver cache. Tests: `tests/test_admin.py`.

### Audit log viewer

`bucketeer/audit_viewer.py` (router `/api/admin/audit`, `require_admin`, tests `tests/test_audit_viewer.py`). `GET /api/admin/audit?actor=&action=&target_type=&target=&target_contains=&since=&until=&limit=&before_id=` -> `{entries:[{id, ts, actor, action, targetType, target, details}], nextCursor}`, newest first by id; `action` ending in `.` is a prefix match (`bucket.`), else exact; `target` exact, `target_contains` case-insensitive substring; `since` inclusive, `until` exclusive, ISO 8601 (naive = UTC); `limit` default 50, silently capped at 200 (<1 -> 422); `nextCursor` is the last id of a full page, pass it as `before_id` (stable under concurrent inserts). Bad dates, since >= until, strings over 200 chars -> 422. `details` go through `scrub()` again on read (`db.audit.search`; unparseable JSON -> `{}`). `GET /audit/facets` -> `{actions, targetTypes}` (distinct, index `audit_log_action`, migration v6). `GET /audit/export.csv` takes the same filters (no paging), at most 50,000 rows, BOM, `csv_safe` cells, `no-store`, columns `id,time,actor,action,target_type,target,details(JSON)`, audited `admin.export.audit` (`report`/`audit`, details rows/truncated/filters). Reading is NOT audited, only the export. Frontend: "Audit" tab in `views/admin-audit.js` (details rendered as a key/value list via `h()`/`text`, never as markup: they carry attacker-influenced names and labels), styles at the end of `css/admin.css`.

### Admin settings

`bucketeer/admin_settings.py` (routes `/api/admin/settings`, admin only, CSRF via the parent router) and `bucketeer/policy_settings.py` (pure validators and helpers later features call; only the default quota and uid mappings are applied so far, by `rgw_user`). `GET /settings` returns everything. Stored in `settings` (keys `naming` {prefix, pattern}, `placement` {default, allowed}, `budget_number` {pattern, example}, `default_quota` absent = the built-in 1 TiB default, else {enabled, maxSize, maxObjects}, `notification_endpoints` list) and the `uid_mappings` table; load with `load_*`. Each change audits `admin.settings.*` with before/after.
- **Naming:** DNS-compatible rules always (`check_dns_name`); optional prefix template (`{username}` only, slugged lowercase, `_` becomes `-`) and optional extra regex (fullmatch, max 200 chars). `validate_bucket_name(name, username, rules) -> [errors]`, `suggest_prefix`. Regex safety layers: a static pre-check (`check_pattern_static`: counted repeats above 63, nested product above 256, since `regex` expands them at compile time), then `approve_pattern` compiles and probes in a subprocess (256 MB cap, 1 s), then in-process matching on the `regex` module with a 50 ms `timeout=` for approved patterns only (bounded cache). These can block briefly: call `normalize_naming`/`validate_bucket_name` via `asyncio.to_thread`. `POST /settings/naming/test` tries draft rules without saving. Use `\Z`, not `$`, when validating whole strings (`$` accepts a trailing newline).
- **Placement:** validated on save against croit `GET /s3/placements` (`CroitClient.list_placements`). Default null = croit's zone default; allowed [] = users get no choice. `placement_options()` gives the create-form view; the choice is shown only when `userChoice` (more than one).
- **Default quota:** same semantics as user quotas (0 is a limit). Unset = the built-in 1 TiB / unlimited objects (`policy_settings.DEFAULT_QUOTA`, Q4); `effective_default_quota(settings)` is the single source (used by `rgw_user`; never duplicate the constant). Explicit unlimited is stored as `{enabled: false}` and differs from unset. `GET /settings` `defaultQuota` = `{isSet, quota (stored|null), effective, default}`; `DELETE` resets to the default. UI shows "1 TiB (default)".
- **Budget numbers (Q2):** `PUT /settings/budget-number {pattern, example}` (both optional, empty pattern = base charset rules only) via `budget.normalize_budget_rules`: pattern through the naming guards (length, static check, `approve_pattern`), example must pass base rules and the pattern; audit `admin.settings.budget_number` (target_type `setting`). `POST /settings/budget-number/test {rules, value}` is a dry run (exempt in the audit test). `GET /settings` has `budgetNumber`; `budgetExample` is exposed by `GET /api/profile` and `GET /api/buckets/options`. Changing the pattern never alters stored numbers; `GET /api/admin/buckets` adds `budgetMatches` (true/false, null = no number or no pattern) and the table shows a "Format mismatch" badge.
- **Chargeback export (Q2):** `GET /api/admin/export/chargeback.csv` (admin; `text/csv; charset=utf-8`, attachment `bucketeer-chargeback-YYYY-MM-DD.csv`, `no-store`). One row per bucket (key users own none): `bucket` (tenant-qualified), `tenant`, `owner_uid`, `owner_name` (RGW display name), `budget_number`, `budget_status` (`ok`|`missing`|`mismatch`|`unknown` = pattern check timed out or was cut off; no pattern set means any number is `ok`), `objects`, `bytes`, `size_gib` (3 decimals), `quota_bytes` (empty unless the bucket quota is enabled with a size), `created_at` (empty for buckets not made through the portal). Every cell goes through `csv_safe` (leading `=+-@` tab CR gets a `'`; budget numbers and display names are user text). **UTF-8 with BOM** (Excel then reads it right); CRLF lines. Audited `admin.export.chargeback`, target_type `report` (a GET, so not in the audit ROUTES table). Admin Buckets tab has an "Export CSV" link (all buckets, ignores filters). Tests: `tests/test_chargeback.py`.
- **UID mappings:** `PUT/DELETE /settings/uid-mappings/{ad}`; uid syntax `uid` or `tenant$uid`; unique both ways (case-insensitive, 409); a missing RGW user is a warning only. Applied by `rgw_user` at resolution.
- **Notification endpoints:** `PUT/DELETE /settings/notification-endpoints/{name}`; types http|https|amqp|kafka with matching schemes, optional topic/exchange, `verifyTls`. No credentials in v1: URLs with user info, query or fragment are refused, so nothing secret is stored, audited or returned. If brokers need logins later, add write-only fields never returned. Users may only see `endpoint_choices()` (name and type).
- Frontend: Settings tab in `views/admin-settings.js`, styles in `css/settings.css`.

### Always visible vs. Advanced

- **Always visible:**
  - credentials and config snippets
  - key create, rotate and delete. Rotation adds the new key before removing the old, never with a whole-set PUT that drops keys. At most 5 keys per user, and a user's last key can't be deleted, only rotated (confirmed by the owner).
  - bucket list, create and delete. Deleting a non-empty bucket requires typing its name.
  - versioning: **on by default** for new buckets (Q7), and a new versioned bucket also gets the Bucketeer-managed lifecycle rule "expire noncurrent versions after 30 days" (`bucketeer-expire-noncurrent`). Users can untick versioning or change the rule.
  - usage and quota display
  - sharing and bucket keys
  - presigned share links (at most 7 days)
  - simple lifecycle rules: expire after N days, expire noncurrent versions, abort incomplete multipart uploads
- **Advanced:** one global toggle, remembered per user (`user_profiles.advanced_mode`, returned as `advancedMode` by `/api/me` and `/api/profile`, saved with `PUT /api/profile {advancedMode}`; frontend store `static/js/advanced.js`: optimistic with rollback + toast, no localStorage hint; gated UI reads `isAdvanced()` / `subscribe()` so it reacts without reload):
  - the raw bucket-policy editor and ACLs
  - CORS and tagging
  - object lock: offered only in the create-bucket dialog with a can't-be-undone warning, plus retention and legal hold
  - the full lifecycle editor
  - notifications: endpoints only from the admin-defined list
  - multipart cleanup
  - subusers
  - placement and storage class choice, shown only when more than one exists
- **Budget numbers** (index/worktag, Q2):
  - Users must enter a budget number for every bucket they create. A default saved in their profile prefills the form. **Only admins** can create a bucket without one.
  - **Format:** checked against an admin-editable pattern on the Settings tab, with an example shown as a hint. There is no lookup against a finance system.
  - **Storage:** kept in SQLite (`buckets.budget_number`) and mirrored as the Bucketeer-managed bucket tag `bucketeer-budget=<value>`. The tagging editor protects `bucketeer-*` tags.
  - **Editing:** the bucket owner can change it later; the audit log records the old and new values.
  - **Existing buckets without one** are flagged in the bucket list and on the admin page, but not blocked.
  - **Reporting:** the admin page offers a CSV chargeback export (bucket, owner, budget number, usage).
- **Profile** (`bucketeer/profile.py`, `/api/profile`, `require_user`, CSRF, keyed by the session's AD username, never by the request): `GET` -> `{username, displayName, email, defaultBudgetNumber|null}`; `PUT {defaultBudgetNumber?: string|null, advancedMode?: bool}` is a partial update (only the fields present change; null/empty budget clears; unknown fields or null `advancedMode` 422), audits `profile.update` (`field`, `old`/`new`, per field, only when changed). `GET` also has `advancedMode`. `budget.validate_budget_number(value, settings_store)` is pure: trim, max 64, printable, only S3 tag-value characters (the value is mirrored into a tag): ASCII letters/digits/space/`+-=._:/@` (no `#`, no non-ASCII), then, if the `budget_number` setting `{pattern, example?}` exists (the later admin-editable format), the value must fullmatch it through `policy_settings.pattern_matches` (approved patterns only; call via `asyncio.to_thread`). Reuse it for the per-bucket budget number. Frontend: `views/profile.js`, route `#/profile`, header-menu link. Tests: `tests/test_profile.py`.
- **Storage classes (Q6):** users choose only a placement for a new bucket, never a storage class. Storage classes are shown read-only.
- **Notification endpoints (Q6):** the admin allowlist holds no broker credentials in v1. URLs with userinfo are refused.
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
  - **Audit coverage (enforced):** `tests/test_audit_coverage.py` is the source of truth. Its `ROUTES` table maps every POST/PUT/PATCH/DELETE route (taken from the OpenAPI document) to its audit action(s); a new mutating route missing from the table fails the test, and each entry is exercised once against the mock croit + moto. `EVENTS` lists the non-route actions (`bucket_key.cleanup` by actor `system`, `rgw.user.*`, the `*_failed` ones). Scheme: `area.verb[.detail]` (lowercase, `_` inside a word); `logout` is the one old exception. Actor is the AD username (`system` for background jobs; the attempted name on `login.failure`). `target_type`/`target`: `bucket` + croit name (tenant-qualified), `rgw_user` + uid (also `admin.user.*`, `rgw.user.*`), `bucket_key` + row id, `access_key` + access key (`credentials.*`), `profile` + username, `setting` + settings key (`admin.settings.naming|placement|default_quota`), `uid_mapping` + AD name, `notification_endpoint` + name, `user` + AD name (login/logout). Mutations whose outcome is unknown after a failure (croit timeout, failed task, 5xx) audit `bucket.create_failed|delete_failed|versioning_failed` (a plain 4xx refusal changed nothing and is not audited). A scan over all rows from that run fails on secret keys, `Signature=`, Bearer tokens or passwords. Renames made then: `target_type` `user`->`rgw_user` (admin.user.*), `user_profile`->`profile`, `settings`->`setting`; action names unchanged.
  - `settings` takes caller-supplied defaults (`db.settings.get(key, default)`); the schema holds no policy values. `buckets.budget_number` is nullable and unvalidated until Q2 is answered.

### Later features

These need cluster work first:
- **Server-side encryption** needs a key server (Vault, Barbican or KMIP).
- **Static website hosting** needs RGW website mode and wildcard DNS.
- **STS temporary credentials** need an STS key and roles on RGW. They would also give short-lived bucket-scoped keys.
