# bucketeer
Self-service S3 bucket and credential portal for Ceph clusters, powered by the croit API

## Try it locally

No Active Directory, croit or Ceph needed:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # once
.venv/bin/python3 mock/demo.py
```

Open http://127.0.0.1:8000/ and sign in with `alice`, `bob`, `dana` (administrator) or `erin` (no S3 account yet, created on first login), all with the password `demo`, by username or email. The command starts a mock croit API and an in-memory S3 server (moto) next to Bucketeer, and prints the URLs, users and a throwaway recipe for using the revealed keys with the aws CLI (download the files into the demo's temp folder and point `AWS_CONFIG_FILE` / `AWS_SHARED_CREDENTIALS_FILE` there; your real `~/.aws` is never touched). A "Demo mode" pill is shown in the UI. Everything is bound to 127.0.0.1, uses a temporary data directory (deleted on exit; `--keep-data` keeps it) and ignores your `.env`. Ports: `--port`, `--croit-port`, `--moto-port`. Ctrl+C stops it all.
