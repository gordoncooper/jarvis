# CONTRACT: platform-backups

Shared backup contract for every PVC (R31, D7). VolSync 0.17 with the restic mover is the backup. There is no restic CronJob.

## Offers

- Namespace `platform-backups`.
- Bootstrap restic credentials (Secret `backups-restic`).
- One `ReplicationSource` per PVC, including an on-demand trigger.
- A StatefulSet volume gets one `ReplicationSource` per PVC named `<template>-<statefulset>-<ordinal>`.
- Opt-out for rebuildable observability data: the unprefixed annotation `backup: none` on a PVC, or on a `volumeClaimTemplate`'s `metadata`. Allowed only under `services/observability/victoria-metrics/` and `services/observability/victoria-logs/`.

## Expects

- Flux SOPS decryption via `flux-system/sops-age` (R25).
- `envs/<env>/secrets/backups-restic.sops.yaml` (D10 decrypt proof in P0a).
- Target path and retention counts come from `envs/<env>/values/cluster-vars.yaml`.

## Health

Namespace exists; Secret `backups-restic` is present (value not logged). A deleted volume restores with the same checksum (P0b and P2 drills).

## Config

- VolSync 0.17, restic mover, `copyMethod: Direct`. The controller installs the VolumeSnapshot CRD and no snapshot controller. etcd snapshots stay at `backup_path/etcd-snapshots` and are for same-node rollback only.
- One shared Secret `backups-restic` holds `RESTIC_PASSWORD` and `RESTIC_REPOSITORY=/mnt/repo`. Each PVC's restic repository is a directory under `backup_path`, mounted at `/mnt/repo` via `moverVolumes`.
- Retention is 7 daily, 4 weekly, and 3 monthly (`backup_retention_daily`, `backup_retention_weekly`, `backup_retention_monthly`). A ReplicationSource copies those into `restic.retain` and sets `pruneIntervalDays: 7`.
- The mover runs as root via the namespace annotation `volsync.backube/privileged-movers: "true"`. That annotation covers every mover in the namespace, because a hostPath repository ignores fsGroup. Revisit this when the target moves to NFS.
- Restore is a `ReplicationDestination` from that repository into the target PVC. Record the source checksum first and compare it when the destination completes.
- The OQ7 test bundle is removed. `${backup_path}/volsync-test` is still on the node (Retain + hostPath). Delete that directory by hand once.

## Proven by

- `ci/tests/test_flux_entry.py` (SOPS-encrypted secret in Git, platform-configs decryption).
- `ci/checks/pvc-backup` (R31) and `ci/tests/test_pvc_backup.py`.
- The P0b and P2 restore drills.
