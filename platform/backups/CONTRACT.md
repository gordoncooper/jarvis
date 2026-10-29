# CONTRACT: platform-backups

Shared backup contract for every PVC (R31, D7). VolSync 0.17 with the restic mover is the backup. One CronJob copies k3s etcd snapshots into their own rest-server repository.

## Offers

- Namespace `platform-backups`.
- rest-server 0.14: one Deployment and ClusterIP Service `restic` on port 8000. No Ingress.
- Bootstrap restic credentials (Secret `backups-restic`).
- CronJob `etcd-snapshots`: a read-only copy of the k3s snapshot directory into repository `host-etcd-snapshots`.
- ClusterPolicy `pvc-backup` enrolls every PVC: one restic Secret and one VolSync `ReplicationSource` in that PVC's namespace, including an on-demand trigger.
- A StatefulSet volume gets one `ReplicationSource` per PVC Kubernetes creates (`<template>-<statefulset>-<ordinal>`). Git does not list the ordinals.
- Opt-out for rebuildable observability data: the unprefixed annotation `backup: none` on a PVC, or on a `volumeClaimTemplate`'s `metadata`. Allowed only under `services/observability/victoria-metrics/` and `services/observability/victoria-logs/`.

## Expects

- Flux SOPS decryption via `flux-system/sops-age` (R25).
- `envs/<env>/secrets/backups-restic.sops.yaml` (D10 decrypt proof in P0a).
- Target path, retention counts, `backup_schedule`, and `time_zone` come from `envs/<env>/values/cluster-vars.yaml`.
- The backup-role node (`role.<dns_suffix>/backup=true`) for the rest-server hostPath.
- The control-plane node (`role.<dns_suffix>/control_plane=true`), where k3s writes the snapshot files.

## Health

Namespace exists; Secret `backups-restic` is present (value not logged); Service `restic` has a ready endpoint. Kyverno admission is Ready. A PVC without `backup: none` has Secret `restic-<pvc>` and ReplicationSource `<pvc>`, and that source completes a sync. A PVC annotated `backup: none` has neither. A deleted volume restores with the same checksum (P0b and P2 drills). A Job from CronJob `etcd-snapshots` completes, and `restic snapshots` on `host-etcd-snapshots` lists a snapshot that holds the newest k3s snapshot file.

## Config

- VolSync 0.17, restic mover, `copyMethod: Direct`. The controller installs the VolumeSnapshot CRD and no snapshot controller.
- k3s keeps writing etcd snapshots to `backup_path/etcd-snapshots`. The k3s config does not change. Those files are the same-node rollback copy.
- rest-server data is the hostPath `backup_path/restic`, pinned to the backup-role node. One repository per PVC is the directory `backup_path/restic/ns-<namespace>/pvc-<pvc>`.
- A volume's repository URL is `rest:http://restic.platform-backups.svc.cluster.local:8000/ns-<namespace>/pvc-<pvc>`. The `ns-` and `pvc-` prefixes keep a path segment off rest-server's reserved names (`data`, `index`, `keys`, `locks`, `snapshots`, `config`). The password stays out of the URL.
- Secret `backups-restic` holds `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (the server prefix `rest:http://restic.platform-backups.svc.cluster.local:8000/`), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`. A volume secret sets `RESTIC_REPOSITORY` to that volume's URL and copies the password and `RESTIC_REST_*` keys. VolSync 0.17 passes those two keys into the mover.
- The process runs with htpasswd auth and without `--append-only` or `--private-repos`. The mover forgets on every backup and prunes with the same credentials. Repository paths are `ns-<namespace>/pvc-<pvc>`.
- Retention is 7 daily, 4 weekly, and 3 monthly (`backup_retention_daily`, `backup_retention_weekly`, `backup_retention_monthly`). A ReplicationSource copies those into `restic.retain` and sets `pruneIntervalDays: 7`.
- Enrollment is ClusterPolicy `pvc-backup` in this directory (platform-configs, after the VolSync CRDs). `failurePolicy` is Ignore on `spec.failurePolicy` and on `spec.webhookConfiguration.failurePolicy`. Each generate rule sets `generateExisting` and `synchronize`. It skips a PVC annotated `backup: none` and a PVC labeled `app.kubernetes.io/created-by: volsync`.
- For every other PVC it generates Secret `restic-<pvc>` and ReplicationSource `<pvc>` in that namespace. `RESTIC_REPOSITORY` is Secret `backups-restic` key `RESTIC_REPOSITORY` plus `ns-<namespace>/pvc-<pvc>`. `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, and `RESTIC_REST_PASSWORD` are copied from that Secret with a data generate and an apiCall lookup. The ReplicationSource uses the restic mover, `copyMethod: Direct`, `cacheCapacity: 1Gi`, and `trigger.schedule` from `backup_schedule`. `trigger.manual` is PVC annotation `backup.manual`, or `0` when that annotation is absent.
- A mutate-existing rule sets annotation `volsync.backube/privileged-movers: "true"` on the PVC's namespace. It lands asynchronously, so the first sync may fail once and pass on retry.
- The VolSync controller pod's `TZ` is `time_zone`. The 0.17 cron parser uses the process local zone and has no timezone field. The chart has no env hook, so a HelmRelease postRenderer sets `TZ` and mounts the node zoneinfo directory.
- `platform-backups` does not set `volsync.backube/privileged-movers`. No mover in this namespace mounts a hostPath. The rest-server runs as root so it can write its hostPath. The etcd CronJob runs as root so it can read the snapshot files (mode 0600). It is not a mover.
- CronJob `etcd-snapshots` is pinned to `role.<dns_suffix>/control_plane=true`. It mounts `backup_path/etcd-snapshots` read-only (`hostPath` type `Directory`) and runs `restic backup` of that mount into `host-etcd-snapshots`: the `RESTIC_REPOSITORY` prefix from Secret `backups-restic` plus that one path segment. `restic init` runs only when `restic cat config` exits 10 (repository missing). Any other error stops the run. `spec.timeZone` is `time_zone`. The schedule is five minutes after the k3s snapshot cron (role default `0 */12 * * *`, so `5 */12 * * *`). `concurrencyPolicy` is `Forbid`. Each run forgets with `backup_retention_daily`, `backup_retention_weekly`, and `backup_retention_monthly`, then prunes. The snapshot host is the fixed name `etcd-snapshots`, so retention is one group across runs. The image is official `restic/restic`, digest-pinned; Renovate updates the digest.
- While the rest-server's data sits on the same disk, this copy adds retention and dedup, not disk-failure protection.
- Volume restore is a one-shot `ReplicationDestination` from the volume's repository into the target PVC (`copyMethod: Direct`). Record the source checksum first and compare it when the destination completes. `make restore` creates that destination at run time, not in Git. It is a later PR.
- etcd restore is owner-run. It is not a make target. Do not print Secret values. Set `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, and `RESTIC_REST_PASSWORD` from Secret `backups-restic`, and set `RESTIC_REPOSITORY` to that Secret's prefix plus `host-etcd-snapshots`. On the server node, `restic snapshots`, then `restic restore <id> --target <dir> --include /snapshots/<file>`. The k3s file lands at `<dir>/snapshots/<file>`. Stop k3s, run `/usr/local/bin/k3s server --cluster-reset --cluster-reset-restore-path=<dir>/snapshots/<file>` once, then start k3s. A pod pinned to the control-plane role can reach the Service and write the file through a hostPath; the reset itself runs on the node. Do not run that reset as a drill on the live env. k3s secrets-encryption is on, so restoring onto a rebuilt node also needs the original k3s server token; same-node restore is the stated scope.
- The OQ7 test bundle is removed. `${backup_path}/volsync-test` is still on the node (Retain + hostPath). Delete that directory by hand once.

## Proven by

- `ci/tests/test_flux_entry.py` (SOPS-encrypted secret in Git, platform-configs decryption).
- `ci/tests/test_restic_rest_server.py` (rest-server rules and the etcd CronJob rules) and `ci/tests/test_bootstrap_secrets.py`.
- `ci/checks/pvc-backup` (R31) and `ci/tests/test_pvc_backup.py` (policy present, `backup: none` allow-list, no ReplicationSource in Git, no hand-written VolSync object or restic repository URL under `services/` or `apps/`).
- `ci/tests/test_backup_enrollment.py` (Kyverno controllers, aggregated roles, policy rules).
- The P0b scratch drill (Direct source and destination against this rest-server, checksum match).
