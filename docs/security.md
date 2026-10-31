# Security

- **Login.** LDAPS only. The typed login is escaped in the filter. Ambiguous matches,
  empty passwords and control characters are refused. A throttle limits attempts per login,
  per account and per client address, and stays below the AD lockout threshold.
- **Session.** A signed, stateless cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, name
  `__Host-bucketeer`. It holds no password and no S3 key. Expiry is also checked on the
  server.
- **CSRF.** Every state-changing `/api` request needs the header `X-Requested-With:
  bucketeer`. No CORS is allowed.
- **Browser hardening.** Strict CSP, `nosniff`, `no-referrer`, `X-Frame-Options: DENY`.
- **Secrets.** A secret key leaves the server only through the reveal endpoint and the
  one-time setup script. It never appears in logs, errors or the audit log. Setup links are
  stored as a hash, expire after 15 minutes and work once.
- **Ownership.** A user can touch only their own RGW user and buckets. Bucket-scoped keys,
  shares and subusers are separate identities with limited rights.
- **TLS.** Verification is on for LDAP, S3 and Admin Ops. `LDAP_CA_CERT_FILE` can name a
  private CA, and `LDAP_CA_SHA256` pins it. See [Deployment](deployment.md).
- **Policy editor.** It refuses new public statements and self-lockouts.
- **Audit.** Every change is written to an append-only log. A test fails when a new
  mutating route is not audited.
- **Offboarding.** It suspends but never deletes, aborts when AD looks wrong, and starts in
  report-only mode.
