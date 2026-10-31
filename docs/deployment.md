# Deployment

Bucketeer deploys unchanged on [appmotel](https://github.com/dirkpetersen/appmotel), a small
platform of systemd user services behind a Traefik HTTPS proxy.

## What appmotel needs

- `app.py` at the repository root. It calls `uvicorn.run` and reads `PORT`.
- `requirements.txt` at the root. appmotel builds `.venv` from it.
- `.env.default`, which appmotel copies to the live `.env` on first deploy.
- `HEALTH_CHECK_PATH=/health`. Traefik polls it every 30 seconds.

## Operate

```bash
appmo env bucketeer       # edit the live .env
appmo restart bucketeer
appmo update bucketeer    # pull and redeploy
appmo status bucketeer
appmo logs bucketeer
```

appmotel assigns a port from 10001 to 59999 at deploy. Set `PORT` in the live `.env` to
that port. A `PORT` in `.env` overrides the one in the systemd unit.

appmotel never adds new lines to an existing `.env`. After an update, compare your `.env`
with `.env.default`. Values with `;`, backticks, `$(`, `&&`, `||` or an unquoted `|` must
be quoted.

## Settings by area

`.env.default` documents every variable. Each one is **REQUIRED** (empty, the app refuses
to start) or **Default** (the file matches the code default).

| Area | Variables |
|---|---|
| Server | `PORT`, `HOST`, `LOG_LEVEL`, `DATA_DIR`, `HEALTH_CHECK_PATH` |
| Sessions | `SESSION_SECRET`, `SESSION_MAX_AGE`, `SESSION_COOKIE_SECURE` |
| Active Directory | `LDAP_URL`, `LDAP_BASE_DN`, `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD`, `LDAP_USER_FILTER`, `LDAP_REQUIRED_GROUP_DN`, `LDAP_ADMIN_GROUP_DN`, `LDAP_CA_CERT_FILE`, `LDAP_CA_SHA256`, `LDAP_TLS_VERIFY` |
| Admin backend | `ADMIN_BACKEND`, `RGW_ADMIN_URL`, `RGW_ADMIN_ACCESS_KEY`, `RGW_ADMIN_SECRET_KEY` (or the `CROIT_*` set) |
| S3 | `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ADDRESSING_STYLE`, `S3_TENANT`, `S3_CA_CERT_FILE`, `S3_TLS_VERIFY` |
| Email | `SMTP_FROM` (turns email on), `APP_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD` |

## LDAP CA certificate

If the directory uses a private CA, set `LDAP_CA_CERT_FILE` to a path on the server.

- When the file does not exist, the app fetches the CA certificates from the first
  reachable LDAP server at startup and logs their SHA-256 fingerprints.
- By hand: `python3 app.py --fetch-ldap-ca [--force]`.
- The first fetch trusts the network. Compare the fingerprints with the directory team,
  then set `LDAP_CA_SHA256` so only that CA is accepted.
- If the server does not send the root CA, the fetch fails with a clear message. Copy the
  file to the server by hand then.

## Email

Email is on only when `SMTP_FROM` is set, and then `APP_URL` is required. With `SMTP_HOST`
empty, the app delivers directly to the recipient domain's mail server (MX lookup). With
`SMTP_HOST` set, it uses that relay. A relay that allows by sender address needs the
server's address allowed by its administrators.

## Cluster preparation

See [Admin Ops setup](admin-ops-setup.md) for the RGW service user and its capabilities.
