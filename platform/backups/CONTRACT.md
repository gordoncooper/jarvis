# CONTRACT: platform-backups

Shared backup contract for every PVC (R31, D7). VolSync 0.17 with the restic mover is the backup. There is no restic CronJob.

## Offers

- Namespace `platform-backups`.
- rest-server 0.14: one Deployment and ClusterIP Service `restic` on port 8000. No Ingress.
- Bootstrap restic credentials (Secret `backups-restic`).
- One `ReplicationSource` per PVC, including an on-demand trigger.
- A StatefulSet volume gets one `ReplicationSource` per PVC named `<template>-<statefulset>-<ordinal>`.
- Opt-out for rebuildable observability data: the unprefixed annotation `backup: none` on a PVC, or on a `volumeClaimTemplate`'s `metadata`. Allowed only under `services/observability/victoria-metrics/` and `services/observability/victoria-logs/`.

## Expects

- Flux SOPS decryption via `flux-system/sops-age` (R25).
- `envs/<env>/secrets/backups-restic.sops.yaml` (D10 decrypt proof in P0a).
- Target path, retention counts, and `time_zone` come from `envs/<env>/values/cluster-vars.yaml`.
- The backup-role node (`role.<dns_suffix>/backup=true`) for the rest-server hostPath.

## Health

Namespace exists; Secret `backups-restic` is present (value not logged); Service `restic` has a ready endpoint. A deleted volume restores with the same checksum (P0b and P2 drills).

## Config

- VolSync 0.17, restic mover, `copyMethod: Direct`. The controller installs the VolumeSnapshot CRD and no snapshot controller. etcd snapshots stay at `backup_path/etcd-snapshots` and are for same-node rollback only.
- rest-server data is the hostPath `backup_path/restic`, pinned to the backup-role node. One repository per PVC is the directory `backup_path/restic/<namespace>/<pvc>`.
- A volume's repository URL is `rest:http://restic.platform-backups.svc.cluster.local:8000/<namespace>/<pvc>`. The password stays out of the URL.
- Secret `backups-restic` holds `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (the server prefix `rest:http://restic.platform-backups.svc.cluster.local:8000/`), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`. A volume secret sets `RESTIC_REPOSITORY` to that volume's URL and copies the password and `RESTIC_REST_*` keys. VolSync 0.17 passes those two keys into the mover.
- The process runs with htpasswd auth and without `--append-only` or `--private-repos`. The mover forgets on every backup and prunes with the same credentials. Repository paths are `<namespace>/<pvc>`.
- Retention is 7 daily, 4 weekly, and 3 monthly (`backup_retention_daily`, `backup_retention_weekly`, `backup_retention_monthly`). A ReplicationSource copies those into `restic.retain` and sets `pruneIntervalDays: 7`.
- The VolSync controller pod's `TZ` is `time_zone`. The 0.17 cron parser uses the process local zone and has no timezone field. The chart has no env hook, so a HelmRelease postRenderer sets `TZ` and mounts the node zoneinfo directory.
- `platform-backups` does not set `volsync.backube/privileged-movers`. No mover in this namespace mounts a hostPath. The rest-server runs as root so it can write that hostPath.
- Restore is a one-shot `ReplicationDestination` from the volume's repository into the target PVC (`copyMethod: Direct`). Record the source checksum first and compare it when the destination completes. The restore procedure and Kyverno enrollment are later PRs.
- The OQ7 test bundle is removed. `${backup_path}/volsync-test` is still on the node (Retain + hostPath). Delete that directory by hand once.

## Proven by

- `ci/tests/test_flux_entry.py` (SOPS-encrypted secret in Git, platform-configs decryption).
- `ci/tests/test_restic_rest_server.py` and `ci/tests/test_bootstrap_secrets.py`.
- `ci/checks/pvc-backup` (R31) and `ci/tests/test_pvc_backup.py`.
- The P0b scratch drill (Direct source and destination against this rest-server, checksum match).
