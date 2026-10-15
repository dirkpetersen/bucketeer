# croit tools

Standalone admin scripts for the croit API. They are not part of Bucketeer:
they don't import it, read its `.env`, or get deployed with it. Each one is a
single file that uses only the Python standard library.

They share the croit environment variables:

| Variable | Purpose |
|---|---|
| `CROIT_URL` | API base URL, e.g. `https://croit.example.org/api` |
| `CROIT_API_TOKEN` | API token, sent as `Authorization: Bearer` (preferred) |
| `CROIT_USERNAME` / `CROIT_PASSWORD` | croit or LDAP login, used when no token is set |
| `CROIT_CA_CERT_FILE` | CA bundle for the croit endpoint |
| `CROIT_TLS_VERIFY` | `false` skips TLS verification of croit |

## croit-cert

Installs a TLS certificate and private key for the S3 gateways. HAProxy groups
in `http` proxy mode terminate TLS with that same certificate.

```bash
# Full chain + key, e.g. from certbot
tools/croit-cert --cert fullchain.pem --key privkey.pem

# Also restart HAProxy node by node, then check what clients actually see
tools/croit-cert --cert fullchain.pem --key privkey.pem \
    --restart-haproxy --verify s3.example.org

# Validate and compare only
tools/croit-cert --pem haproxy.pem --dry-run
```

What it does:
1. Checks locally that the key matches the certificate and the certificate
   hasn't expired.
2. Compares against the certificate croit already has. If they're identical it
   changes nothing and exits 0, so it's safe from cron or a certbot
   `--deploy-hook`.
3. Uploads the certificate via `PUT /config/rgw-ssl-cert` and waits for croit's
   rolling gateway restart. Pass `--no-rolling-restart` to skip that restart.
4. With `--restart-haproxy`, restarts HAProxy one node at a time on the members
   of `http`-mode HA groups, waiting for each node to report running again.
5. With `--verify HOST[:PORT]`, connects to the endpoint and confirms it serves
   the new certificate. It exits 3 if it doesn't.

The croit API doesn't say whether HAProxy reloads the certificate on its own.
On the first run, use `--verify` without `--restart-haproxy`. If verification
fails, add `--restart-haproxy`.

Run `tools/croit-cert --help` for all options.

## croit-gate

**Optional, and only for `ADMIN_BACKEND=croit`.** Since the default admin backend is `rgw` (RGW Admin Ops, see `docs/admin-ops-setup.md`), Bucketeer no longer talks to croit at all and needs neither this gateway nor a croit token; the gateway goes away together with the croit backend.

An allowlisting gateway in front of the croit API. The croit admin token can do
far more than Bucketeer needs (TLS certificates, servers, API tokens, ...). The
gateway holds that real token and forwards only the calls Bucketeer's
`CroitClient` makes, so a compromised portal can't use the token for anything
else, and can't read it. Bucketeer needs no code change: its `CROIT_URL` points
at the gateway and its `CROIT_API_TOKEN` is a separate client token that only
the gateway accepts.

```
Bucketeer --(client token)--> croit-gate --(real token, TLS)--> croit
```

### What it allows

- Clients must send `Authorization: Bearer <client token>` (constant-time
  compare); anything else gets 401. The gateway swaps in the real token and
  never forwards or logs either token.
- Only the (method, path) pairs in the `ROUTES` table of `tools/croit-gate`
  (`/s3/users...`, keys, quotas, buckets, bucket policy, placements, usage,
  `GET /tasks/{id}`, `GET /auth/token-info`). Everything else gets 403 and one
  log line (method, path, client address).
- Each `{param}` is exactly one non-empty segment. It is percent-decoded once
  as strict UTF-8 and must then match `[A-Za-z0-9._~@+$:-]+` and not be `.` or
  `..` (so `%252F`, `%3B` and overlong UTF-8 are refused). Paths with `..`, `//`, `%2F`, `%5C`, backslash, NUL, control characters or a
  fragment are refused. Query strings are passed through only on routes that
  use them and are never logged (`PUT .../keys/{accessKey}` carries a secret
  key in its query).
- Request bodies up to 1 MiB (else 413), responses up to 32 MiB (else 502).
  Requests with more than 32 header lines, lines over 8 KiB, or folded headers
  are refused. Cookies, hop-by-hop and `X-Forwarded-*` headers are dropped;
  upstream always gets a fixed `Accept`. Upstream
  redirects are not followed (502); upstream failures give 502/504.
- A new call in `bucketeer/croit.py` must also be added to `ROUTES`;
  `tests/test_croit_gate.py` fails until it is.

### Configuration

Environment variables (the gateway never reads Bucketeer's `.env`):

| Variable | Purpose |
|---|---|
| `CROIT_URL` | croit API base URL, e.g. `https://croit.example.org/api`. The gateway serves the same base path |
| `CROIT_API_TOKEN` | the real croit token (required) |
| `CROIT_CA_CERT_FILE` / `CROIT_TLS_VERIFY` | as for the other tools |
| `CROIT_GATE_CLIENT_TOKEN` | token clients must send (required, at least 32 characters, different from the real one) |
| `CROIT_GATE_LISTEN` | `host:port`, default `127.0.0.1:18089` |
| `CROIT_GATE_TIMEOUT` | seconds per upstream request, and the total time a client has to send its request (slower clients are cut off), default 30 |
| `CROIT_GATE_MAX_CONNECTIONS` | concurrent client connections, default 64; more get 503 |

Requirements: Python 3.9 or newer (checked at startup). Both tokens may only
contain printable ASCII without spaces, so a stray carriage return from a CRLF
env file is refused at startup. A plain `http://` `CROIT_URL` is accepted only
for a loopback host.

The gateway refuses a non-loopback `CROIT_GATE_LISTEN` unless started with
`--allow-remote`. It speaks plain HTTP, so on a shared network anyone who can
sniff the traffic or reach the port can use the client token (and thereby
everything the allowlist permits). Prefer a loopback address on the Bucketeer
host. `croit-gate --check` validates the configuration and calls
`GET /auth/token-info` upstream.

### Install: its own Unix user, outside Bucketeer

Run the gateway as a dedicated user with its own systemd user service, so
Bucketeer's deployments (appmotel updates, `git reset --hard`) never touch it
and Bucketeer's user can't read the real token.

```bash
# as root, once
useradd --system --create-home --shell /usr/sbin/nologin croit-gate
loginctl enable-linger croit-gate        # lets the user service run without a login

# as the gateway user (copy tools/croit-gate to somewhere it can read first)
sudo -u croit-gate -H env XDG_RUNTIME_DIR=/run/user/$(id -u croit-gate) \
    /path/to/croit-gate install
sudoedit -u croit-gate /home/croit-gate/.config/croit-gate/env   # fill in the tokens
sudo -u croit-gate -H env XDG_RUNTIME_DIR=/run/user/$(id -u croit-gate) \
    systemctl --user daemon-reload
sudo -u croit-gate -H env XDG_RUNTIME_DIR=/run/user/$(id -u croit-gate) \
    systemctl --user enable --now croit-gate
```

`croit-gate install` (no root, run by the gateway's own user):
1. copies the script to `~/.local/bin/croit-gate` (0755);
2. writes `~/.config/systemd/user/croit-gate.service` (`ExecStart` at that copy,
   `EnvironmentFile=~/.config/croit-gate/env`, `Restart=on-failure`,
   `NoNewPrivileges`, `PrivateTmp`, `MemoryMax=256M`, `TasksMax=256` (two threads per connection: keep it above 2 x `CROIT_GATE_MAX_CONNECTIONS` + 16),
   `ProtectSystem=strict`, `ProtectHome=read-only`, kernel and cgroup protections,
   `RestrictAddressFamilies` (including `AF_UNIX` for name resolution), `UMask=0077`);
3. writes an env template `~/.config/croit-gate/env` (0600) only if there is
   none. An existing env file and its tokens are never overwritten;
4. prints the next steps. It runs no `systemctl` unless you pass `--enable`
   (then it runs `daemon-reload` and `enable --now`). `loginctl enable-linger`
   needs an administrator and is never run by the script.

Some sandboxing options need user namespaces; if the unit fails to start with a
namespacing error, drop those lines from the unit.

Unit example (what `install` writes, with `$HOME` expanded):

```ini
[Service]
ExecStart=/home/croit-gate/.local/bin/croit-gate
EnvironmentFile=/home/croit-gate/.config/croit-gate/env
Restart=on-failure
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
```

**Updating** the gateway is a deliberate step by its own user: get the new
`tools/croit-gate`, run `croit-gate install` again (it refreshes the script and
the unit, keeps the env file), then `systemctl --user restart croit-gate`.

### Pointing Bucketeer at it

In Bucketeer's `.env` (`appmo env bucketeer`):

```
CROIT_URL=http://127.0.0.1:18089/api
CROIT_API_TOKEN=<the gateway's CROIT_GATE_CLIENT_TOKEN>
```

Plain `http://` is fine for a loopback address. Leave `CROIT_CA_CERT_FILE` and
`CROIT_TLS_VERIFY` alone: they apply to the Bucketeer-to-gateway hop.

### Token rotation

- **Client token** (Bucketeer's): generate a new one
  (`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`), set it as
  `CROIT_GATE_CLIENT_TOKEN` in the gateway's env file and restart the gateway,
  then set the same value as `CROIT_API_TOKEN` in Bucketeer's `.env` and restart
  Bucketeer. Calls fail with 401 in between, so do both steps back to back.
- **Real croit token**: create the new API token in croit, put it into the
  gateway's `CROIT_API_TOKEN`, run `croit-gate --check` (with the env file
  loaded), restart the gateway, then delete the old token in croit. Bucketeer
  is unaffected.
