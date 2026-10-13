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
