# Bucketeer

Bucketeer is a self-service S3 portal for Ceph clusters. Users log in with their Active
Directory account. They get S3 credentials and manage their buckets: create, delete,
versioning, sharing, scoped keys, lifecycle rules and the other S3 bucket features.

Everyday features are always visible. Rare features sit behind one **Advanced** toggle.
Admins get an extra page for users, buckets, settings, audit and offboarding.

Where to start:

- [Software stack](stack.md): every part of the web stack, backend and frontend.
- [Architecture](architecture.md): how the parts work together.
- [Security](security.md): the controls that protect users and keys.
- [Deployment](deployment.md): run it on appmotel, with all the settings.
- [Admin Ops setup](admin-ops-setup.md): prepare the Ceph cluster.

The source of truth for product decisions is `CLAUDE.md` in the repository.
