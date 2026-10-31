# Software stack

## Backend

| Part | Technology |
|---|---|
| Language | Python 3 (3.12 on the server) |
| Web framework | FastAPI (`>=0.115`) |
| Server | uvicorn[standard] (`>=0.30`), started by `app.py` |
| Settings | pydantic-settings (`>=2.4`) and python-dotenv (`>=1.0`) |
| HTTP client | httpx (`>=0.27`), async |
| S3 and signing | boto3 (`>=1.34`) and botocore |
| Active Directory | ldap3 (`>=2.9`), LDAPS only |
| Email | standard library SMTP and dnspython (`>=2.6`) for MX lookups |
| Sessions | itsdangerous (`>=2.2`), signed stateless cookie |
| Regular expressions | regex (`>=2024.0`), with a match timeout |
| Storage | SQLite (standard library), WAL mode |

Notes:

- FastAPI serves JSON under `/api/...`. The static files are mounted at `/` last, so they
  never hide an API route or `/health`.
- uvicorn binds to `127.0.0.1` and reads `PORT` from the environment. It runs with
  `proxy_headers=True` and `forwarded_allow_ips="127.0.0.1"`, so URLs and client addresses
  are correct behind the reverse proxy.
- Blocking libraries (ldap3, boto3, SMTP) run in worker threads with `asyncio.to_thread`.
- Background work (offboarding schedule, bucket delete jobs, key clean-up) runs as asyncio
  tasks in the same process. The design assumes one process.
- SQLite lives in `DATA_DIR/bucketeer.sqlite3`. Migrations are numbered, tracked with
  `PRAGMA user_version`, and applied at startup. The app refuses a database that is newer
  than the code.

## Frontend

The frontend uses plain HTML, CSS and vanilla JavaScript. There is **no framework, no
bundler and no build step**. FastAPI serves the files as they are.

- `index.html` loads the CSS files, a small classic script `theme.js` (sets the theme before
  the first paint), and the ES module `js/app.js`.
- `app.js` asks `/api/me` and shows the login view or the dashboard.
- Views (`static/js/views/`): login, dashboard, buckets, credentials, shared with me,
  subusers, profile, and the admin pages (users and buckets, settings, audit, offboarding).
- Components (`static/js/components/`) are native custom elements in the light DOM. They
  include the dialogs for sharing, keys, lifecycle, CORS, policy, ACL, notifications,
  object lock, multipart uploads, tags, quota and budget, and also the secret field, code
  panel, menu button, toast region, confirm dialog and setup panel. They use the browser's
  native `<dialog>` and popover features.
- Helpers: `api.js` (the only way to call `/api`; it sends the CSRF header), `dom.js` (the
  `h()` function that builds DOM safely), `awsconfig.js` (AWS config snippets), `zip.js`
  (setup file archive), `ini.js` (INI highlighter), `advanced.js` (Advanced toggle store).

### Look and feel

- CSS design tokens in `css/tokens.css` hold all colours, spacing, radii, shadows, type
  sizes and motion values. Other CSS files never hard-code them.
- Light and dark themes use `light-dark()` with a `data-theme` override.
- Orange (`#D73F09`) is the only accent colour. Every text and background pair meets AA
  contrast.
- All motion stops when `prefers-reduced-motion` is set.
- Fonts are self-hosted and open licensed: Inter, Fraunces and JetBrains Mono.
- `/showcase.html` shows every component in light and dark.

### Rules that tests enforce

- A strict CSP: `script-src 'self'; style-src 'self'`. No inline scripts, styles or event
  handlers.
- No `innerHTML` with dynamic data. DOM is built with `h()`.
- Every `/api` call goes through `api.js`.

## Cluster and directory side

- **Ceph RGW.** The admin plane is the RGW Admin Ops API. The data plane is the S3 API, called with the user's own keys.
- **Active Directory** over LDAPS. The app binds with a read-only service account, then
  verifies the user's password with a second bind.
- **SMTP** for share invitations. It is off by default.

## Testing and tools

| Part | Use |
|---|---|
| pytest (`>=8`) | about 2,500 tests; a full run takes about 11 minutes |
| moto[server] (`>=5`) | local S3 and SNS server for tests and the demo |
| ruff (`>=0.6`) | lint |
| cryptography | build test certificates for the LDAP CA fetch tests |
| node | a few tests (the zip writer) |

The `mock/` folder holds a mock RGW Admin Ops API (with real SigV4
checks), a bridge to moto, and `demo.py`, a local demo with fake users. Run it with
`python3 mock/demo.py`.

## Documentation

This site is built with Zensical from the `docs/` folder and `zensical.toml`.

```bash
pip install zensical
zensical serve    # preview
zensical build    # writes ./site
```
