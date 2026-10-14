# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Bucketeer is a self-service S3 portal for Ceph clusters managed by **croit**. Users log in with AD, get their S3 (RGW) access/secret keys with ready-to-paste `~/.aws/config` and `~/.aws/credentials` snippets, and manage their buckets: create, delete, versioning, sharing, scoped keys, lifecycle, and the full set of S3 bucket features. Admins get an admin page. See "Product design" below for decisions already made, and `TODO.md` for the phased plan.

The repo is greenfield: so far only the README, LICENSE, a Python `.gitignore`, and the croit OpenAPI spec exist. There is no build, lint, or test tooling yet. When you add it, document the commands here, including how to run a single test. To match production, run the app locally with `python app.py` (see the appmotel section below).

## Stack constraints (set by the owner)

- **Backend:** Python + FastAPI. It is the only component that talks to the croit API. The browser never calls croit directly and never sees a croit token.
- **Frontend:** plain HTML, CSS, and vanilla JavaScript. **No JS frameworks or build step** (no React/Vue/Svelte/Tailwind, no bundler). FastAPI serves the static files.
- **The frontend must look exceptionally polished.** This is a top-level requirement, not an extra. Use a deliberate visual design (typography, spacing, motion, light/dark) and don't settle for default or template styling.

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

- Spec: `docs/api/api-1.yaml` (OpenAPI 3.1, ~40k lines). Online docs: https://www.croit.io/api-docs. The file is too big to read whole, so grep for `^  /<path>:` and for `^    <SchemaName>:`. Ignore `docs/api/api-1.yaml:Zone.Identifier`, which is a Windows download artifact.
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
| Buckets | `PUT` / `PATCH` / `DELETE /s3/buckets/{bucketName}` | Create and delete return tasks. Deleting a non-empty bucket needs `deleteData` plus the confirmation sentence croit names in its first refusal |

- RGW uids can carry a tenant (`tenant$uid`). URL-encode the `$` in path parameters.
- Errors come back as the `ErrorMessage` schema.
- `docs/api/s3-features.md` lists every S3 endpoint croit offers, and the S3 features users have to manage through the S3 protocol instead. `docs/api/README.md` covers how to get croit tokens.
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
- Read-modify-write them, and only touch statements or rules whose `Sid`/`ID` starts with `bucketeer-` (e.g. `bucketeer-key-<id>`, `bucketeer-share-<uid>`).
- Leave every other statement intact.
- The raw editors show everything but must warn before editing `bucketeer-*` entries.
- Policy principals are `arn:aws:iam::<tenant>:user/<uid>`, with an empty tenant when there is none.

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
- There is no object/file browser.

### Admin page and storage

- Runtime policy is edited on the admin page, not in `.env`: bucket naming rules, placement defaults, default quota, uid mappings, and the notification endpoint allowlist. `.env` keeps only infrastructure and secrets.
- State lives in **SQLite** under `DATA_DIR`. That covers settings, uid mappings, bucket-key ownership and the **audit log** of who created or deleted buckets, keys and shares.
- `DATA_DIR` defaults to `data/` in the repo (git-ignored). appmotel updates use `git reset --hard`, which keeps ignored files, and its backups and restores tar the repo, so the database is included.
- Assume a single uvicorn process: SQLite plus in-process background jobs such as the offboarding sync.

### Later features

These need cluster work first:
- **Server-side encryption** needs a key server (Vault, Barbican or KMIP).
- **Static website hosting** needs RGW website mode and wildcard DNS.
- **STS temporary credentials** need an STS key and roles on RGW. They would also give short-lived bucket-scoped keys.
