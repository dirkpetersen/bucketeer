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
| Server | `PORT`, `HOST`, `LOG_LEVEL`, `DATA_DIR`, `HEALTH_CHECK_PATH`, `DOCS_URL` (the docs the workspace links to; empty hides the links) |
| Sessions | `SESSION_SECRET`, `SESSION_MAX_AGE`, `SESSION_COOKIE_SECURE` |
| Active Directory | `LDAP_URL`, `LDAP_BASE_DN`, `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD`, `LDAP_USER_FILTER`, `LDAP_REQUIRED_GROUP_DN`, `LDAP_ADMIN_GROUP_DN`, `ADMIN_USERS`, `LDAP_CA_CERT_FILE`, `LDAP_CA_SHA256`, `LDAP_TLS_VERIFY` |
| Admin backend | `RGW_ADMIN_URL`, `RGW_ADMIN_ACCESS_KEY`, `RGW_ADMIN_SECRET_KEY` |
| S3 | `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ADDRESSING_STYLE`, `S3_TENANT`, `S3_CA_CERT_FILE`, `S3_TLS_VERIFY` |
| Email | `SMTP_FROM` (turns email on), `APP_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD` |

!!! note "Virtual-host addressing is the default"
    `S3_ADDRESSING_STYLE=virtual` puts the bucket name in front of the host
    (`https://my-bucket.s3.example.edu`). The S3 endpoint needs a wildcard DNS record
    (`*.s3.example.edu`) and a wildcard TLS certificate for it, and RGW needs the host in
    `rgw_dns_name` (or the zonegroup `hostnames`). Without these, set
    `S3_ADDRESSING_STYLE=path`.

## LDAP CA certificate

`LDAP_CA_CERT_FILE` defaults to `data/ldap-ca.pem`. The app keeps this file up to date:

- When the file does not exist, the app fetches the CA certificates from the first
  reachable LDAP server at startup and logs their SHA-256 fingerprints.
- When the servers in `LDAP_URL` change, the app fetches again. A record file next to the
  CA file (`ldap-ca.pem.hosts`) remembers which servers the file came from. If the new fetch
  fails, the old file stays and the log says so. A file you supplied by hand is kept until
  the servers change.
- By hand: `python3 app.py --fetch-ldap-ca [--force]`.
- The first fetch trusts the network. After it, compare the logged fingerprints with the
  directory team, then set `LDAP_CA_SHA256` so only that CA is accepted.
- The fetch asks every server in `LDAP_URL` and keeps the CA certificates they send.
  The domain controllers usually send only an intermediate CA, not the root. That is
  enough: the app trusts an intermediate CA from the file as an anchor.
- With a pin, only matching CAs are kept. You may pin the root CA (several fingerprints
  are allowed, separated by commas). Because the servers never send the root, the app
  then reads the CA certificates that AD publishes under Public Key Services, using the
  service account. It keeps only the pinned one, and writes it only if every server's
  chain leads to it. The service login in this step runs over a connection checked only
  against the CA certificates the server sent, and the log says so.
- At startup the log names the user the app runs as, its folder, the settings file, and the
  LDAP CA in use with its fingerprints and whether it is pinned. It warns when `.env` can
  be read by other users (fix: `chmod 600`), and when the file does not hold the pinned CA.
- In appmotel multi-user mode the app runs as its own user (for example `a-bucketeer`),
  with its files under that user's home. Edit files as that user; run `appmo` as
  `appmotel`.
- For a directory with a public CA, set `LDAP_CA_CERT_FILE` empty to use the system trust
  store. With `LDAP_TLS_VERIFY=false` no CA file is used.

## Email

Email is on only when `SMTP_FROM` is set, and then `APP_URL` is required. With `SMTP_HOST`
empty, the app delivers directly to the recipient domain's mail server (MX lookup). With
`SMTP_HOST` set, it uses that relay. A relay that allows by sender address needs the
server's address allowed by its administrators.

## Cluster preparation

See [Admin Ops setup](admin-ops-setup.md) for the RGW service user and its capabilities.
