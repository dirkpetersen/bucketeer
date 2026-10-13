# croit API: getting credentials

`api-1.yaml` in this directory is the croit OpenAPI 3.1 spec. The online
reference is at https://www.croit.io/api-docs. This page explains how to get
credentials for Bucketeer, the scripts in `tools/`, or your own calls.

croit accepts two kinds of credentials. Both are sent as
`Authorization: Bearer <token>`:

| | API token | Session token |
|---|---|---|
| Use for | Services and scripts (Bucketeer, `tools/croit-cert`, cron) | Interactive use, and creating your first API token |
| Obtained from | `POST /api-tokens` (needs an admin session) | `POST /auth/login` with a croit or LDAP username and password |
| Lifetime | Until its expiry date (or forever if none was set) or until deleted | Short-lived session (`expires_in` seconds) |
| Tied to | Its own roles, not to a person | Your user account and roles |

Use an API token for anything automated.

## 1. Find the API base URL

All paths in the spec (`/auth/login`, `/api-tokens`, and so on) are relative to
the croit API base URL. The spec doesn't record it, so ask your croit
administrator. It is the croit management host followed by the API prefix, for
example:

```bash
export CROIT_URL=https://croit.example.org/api
```

If croit uses a self-signed or internal-CA certificate, add
`--cacert /path/to/ca.pem` to the `curl` commands below.

## 2. Log in to get a session token

Use a croit or LDAP account that has the **admin** role, since only admins can
create API tokens. `curl -u name` prompts for the password, which keeps it out
of your shell history:

```bash
SESSION=$(curl -sf -u your-admin-user \
  -d grant_type=client_credentials \
  "$CROIT_URL/auth/login" | jq -r .access_token)
```

The response also includes `expires_in`, `roles` and `passwordNeedsChange`. If
you get a 401, check the username and password. If you get a connection error,
check the URL and certificate.

Equivalent form-based login, the one the croit web UI uses:

```bash
curl -s "$CROIT_URL/auth/login-form" \
  --data-urlencode grant_type=password \
  --data-urlencode username=your-admin-user \
  --data-urlencode password='...'
```

## 3. Create an API token

```bash
curl -sf -X POST "$CROIT_URL/api-tokens" \
  -H "Authorization: Bearer $SESSION" \
  -H "Content-Type: application/json" \
  -d '{"name": "bucketeer", "roles": ["viewer", "admin"], "expiry": "2027-10-13T00:00:00Z"}' \
  | jq -r .
```

The response is the secret token. **croit shows it exactly once.** It stores
only a hash, so a lost token cannot be read back and has to be replaced.

Request fields:
- `name` (required): a label, 1 to 64 characters. Name each token after what
  uses it.
- `roles` (required): one or more of `viewer`, `admin`, `securefolder`. An
  admin token must also have `viewer`.
- `expiry` (optional): when the token stops working, as an ISO-8601 timestamp.
  If you leave it out, the token **never expires**, so set one and rotate the
  token before then.

### Which roles?

| Consumer | Roles | Why |
|---|---|---|
| Bucketeer | `viewer`, `admin` | Reads users' S3 secret keys and, with `S3_AUTO_CREATE_USER=true`, creates RGW users |
| `tools/croit-cert` | `viewer`, `admin` | The spec marks `/config/rgw-ssl-cert` admin-only, because the private key can impersonate the S3 endpoint |
| Read-only monitoring or reporting | `viewer` | |

Use a separate token for each consumer. Then you can revoke one without
breaking the others.

## 4. Use and verify the token

```bash
export CROIT_API_TOKEN='<the token from step 3>'
curl -sf -H "Authorization: Bearer $CROIT_API_TOKEN" "$CROIT_URL/auth/token-info" | jq .
```

This returns the token's identity and `roles`. If it works, the URL and token
are correct.

Where to put the token:
- **Bucketeer:** `CROIT_URL` and `CROIT_API_TOKEN` in `.env` (on appmotel:
  `appmo env bucketeer`). `.env` is git-ignored. Never put a real token in
  `.env.default`.
- **`tools/` scripts:** export `CROIT_URL` and `CROIT_API_TOKEN`. For cron, put
  them in a `chmod 600` file and source it. See `tools/README.md`.

## 5. Manage tokens

All of these need an admin session or admin token:

```bash
AUTH="Authorization: Bearer $SESSION"

# List tokens: id, name, roles, expiry. Secrets are never returned.
curl -sf -H "$AUTH" "$CROIT_URL/api-tokens" | jq .

# Change a token's roles (only roles can be changed)
curl -sf -X PATCH -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"roles": ["viewer"]}' "$CROIT_URL/api-tokens/<id>"

# Revoke a token, effective immediately
curl -sf -X DELETE -H "$AUTH" "$CROIT_URL/api-tokens/<id>"
```

**Rotation:**
1. Create a new token.
2. Update the consumer's configuration and restart it (`appmo restart bucketeer`).
3. Confirm it works.
4. Delete the old token.

## Notes on session tokens

- A croit user has **one session**. `POST /auth/logout` ends it everywhere,
  including that user's open browser tabs. This is another reason automation
  should use its own API token rather than a person's login.
- `GET /auth/token-info` also extends the session.
- LDAP users log in the same way as local croit users when LDAP is configured
  in croit. OIDC logins (`/auth/oidc/*`) are browser redirects and are not
  practical for scripts.
