# CONTRACT: platform-backups

Shared backup contract for every PVC (R31, D7). P0a provides the namespace and the restic secret. p0b-09 installs VolSync and a removable backup-and-restore test. The VolSync-or-CronJob choice, retention, prune, and the R31 check follow the live result.

## Offers

- Namespace `platform-backups`.
- Bootstrap restic credentials for VolSync (Secret `backups-restic`).
- One `ReplicationSource` per PVC, including an on-demand trigger.
- A StatefulSet volume gets one `ReplicationSource` per PVC named `<template>-<statefulset>-<ordinal>`.
- Opt-out for rebuildable observability data: the unprefixed annotation `backup: none` on a PVC, or on a `volumeClaimTemplate`'s `metadata`. Allowed only under `services/observability/victoria-metrics/` and `services/observability/victoria-logs/`.

## Expects

- Flux SOPS decryption via `flux-system/sops-age` (R25).
- `envs/<env>/secrets/backups-restic.sops.yaml` (D10 decrypt proof in P0a).
- Target path, retention, and weekly prune come from `envs/<env>/values/cluster-vars.yaml`.

## Health

Namespace exists; Secret `backups-restic` is present (value not logged). A deleted volume restores with the same checksum (P0b and P2 drills).

## Config

- Target path: `backup_path`. Volume data sits under `local_path_data`. etcd snapshots use the same backup path and are for same-node rollback only.
- Retention: `backup_retention_daily`, `backup_retention_weekly`, `backup_retention_monthly`.
- Weekly prune uses those retention counts and `time_zone` from cluster-vars. p0b-09 writes the schedule into this section after the backup-and-restore test.
- Restore is a `ReplicationDestination`:
  1. Record the checksum of the source data.
  2. Apply a `ReplicationDestination` that restores the restic snapshot from `backup_path` into the target PVC.
  3. When that destination is complete, compare the checksum to the original.
- Until the live result is recorded here, restore is a `ReplicationDestination`.
- OQ7 test bundle, removed after that result is read: `platform/backups/test/`. Flux applies it as four leaf Kustomizations, `platform-backups-test-prep`, `platform-backups-test-backup`, `platform-backups-test-restore`, and `platform-backups-test-verify`. They depend on `platform-configs` and then on each other, outside the configs-to-services chain. `prep` substitutes `backup_path` from cluster-vars onto a hostPath volume. The controller path installs the VolumeSnapshot CRD so VolSync can start, and does not install a snapshot controller. VolSync mounts that volume with `moverVolumes` at `/mnt/repo` and reads the password from Secret `backups-restic`. Namespace `platform-backups` carries `volsync.backube/privileged-movers: "true"` so the mover can write the root-owned host path. Read the result with read-only kubectl:

  ```
  kubectl -n flux-system get kustomization platform-backups-test-prep platform-backups-test-backup platform-backups-test-restore platform-backups-test-verify
  kubectl -n flux-system get helmrelease volsync
  kubectl -n platform-backups logs job/volsync-test-seed
  kubectl -n platform-backups logs job/volsync-test-verify
  kubectl -n platform-backups get replicationsource/volsync-test replicationdestination/volsync-test
  ```

  The seed log and the verify log each print one sha256. They match when the restored bytes match the source. Delete the test directory and those four Flux Kustomizations to remove the bundle. The host directory under `backup_path` stays on the node. The assembled Secret `volsync-test-restic` is owned by Job `volsync-test-repo-secret`, so pruning that Job deletes it.

## Proven by

- `ci/tests/test_flux_entry.py` (SOPS-encrypted secret in Git, platform-configs decryption settings).
- `ci/tests/test_volsync_backup.py` (the test repo path is `backup_path` from cluster-vars).
- The R31 CI check (`ci/checks/pvc-backup`, p0b-09).
- The P0b and P2 restore drills.
