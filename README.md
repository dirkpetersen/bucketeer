# Bucketeer
Self-service S3 bucket and credential portal for Ceph clusters (croit-managed or plain RGW), powered by the RGW Admin Ops API

<img width="776" height="1104" alt="image" src="https://github.com/user-attachments/assets/2944d1fb-3adc-45b2-b489-0950b50eccbd" />


## Admin backends and setup

Bucketeer manages S3 users, keys, quotas and bucket lists through an admin backend, chosen with `ADMIN_BACKEND`:

- **`rgw` (the default)**: the RGW Admin Ops API, signed with the key of a dedicated RGW service user. No croit token is needed. Requires `RGW_ADMIN_URL`, `RGW_ADMIN_ACCESS_KEY`, `RGW_ADMIN_SECRET_KEY` and `S3_ENDPOINT_URL`. Buckets are created, versioned and deleted over S3 with the user's own keys (a non-empty delete runs as a background job), and admins type the allowed placements on the Settings tab.
- **`croit`**: the croit management API (`CROIT_URL`, `CROIT_API_TOKEN`, optionally behind `tools/croit-gate`). Kept until the cluster check is done; select it explicitly with `ADMIN_BACKEND=croit`.

The app refuses to start, naming the missing variables, when the selected backend is not configured; it never falls back to the other one. `.env.default` marks which settings are required for which backend.

Setup for the default backend (service user and caps, an internal `RGW_ADMIN_URL`, TLS, key rotation, placements, migrating from croit): [docs/admin-ops-setup.md](docs/admin-ops-setup.md).

**Upgrading an existing deployment:** appmotel never adds new lines to an existing `.env`, so a deployment set up before the Admin Ops switch has no `ADMIN_BACKEND` line and will refuse to start after the update. Before `appmo update`, run `appmo env bucketeer` and add `ADMIN_BACKEND=croit` to keep the old behaviour, or configure the Admin Ops settings first (see section 9 of the guide).

## Try it locally

No Active Directory, croit or Ceph needed:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # once
.venv/bin/python3 mock/demo.py
```

Open http://127.0.0.1:8000/ and sign in with `alice`, `bob`, `dana` (administrator) or `erin` (no S3 account yet, created on first login), all with the password `demo`, by username or email. The command runs Bucketeer on the default `rgw` backend against a mock of the RGW Admin Ops API and an in-memory S3 server (moto), so every feature works end to end (buckets made or deleted in the UI show up in the admin lists; the placements `default-placement` and `nvme-placement` are pre-filled on the Settings tab). `--backend croit` runs the same demo on a mock croit API instead. It prints the URLs, users and a throwaway recipe for using the revealed keys with the aws CLI (download the files into the demo's temp folder and point `AWS_CONFIG_FILE` / `AWS_SHARED_CREDENTIALS_FILE` there; your real `~/.aws` is never touched). A "Demo mode" pill is shown in the UI. Everything is bound to 127.0.0.1, uses a temporary data directory (deleted on exit; `--keep-data` keeps it) and ignores your `.env`. Ports: `--port`, `--moto-port`, `--admin-ops-port` (and `--croit-port` with `--backend croit`). Ctrl+C stops it all.
