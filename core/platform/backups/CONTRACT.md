# CONTRACT: platform-backups

Every PVC is backed up by VolSync's restic mover to a rest-server on the backup-role node, and k3s's etcd snapshots are copied there too. Core (core namespaces, add-ons, `host-etcd-snapshots`) is one trusted zone with one credential. Hosted apps' volumes are app-hosting's, each on its own credential ([app-hosting](../../feature/app-hosting/CONTRACT.md)). Runbooks: [docs/backups.md](../../../docs/backups.md).

## Offers

| What | Detail |
| --- | --- |
| Namespace `platform-backups` | Layer `platform`, Pod Security `privileged` (its pods run as root on host paths). Does not set `volsync.backube/privileged-movers`; no mover runs here. |
| Service `restic`, port 8000 | rest-server on hostPath `backup_path/restic`, htpasswd auth, no `--append-only`, no `--private-repos`. Its only user is core's. No route. |
| Secret `backups-restic` | `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (`rest:http://restic.platform-backups.svc.cluster.local:8000/`), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, bcrypt `htpasswd`. Minted once by `jarvis secrets`. |
| Enrollment: GeneratingPolicy `pvc-backup` | For each bound PVC outside `app-*`: Secret `restic-<pvc>` from `backups-restic` and ReplicationSource `<pvc>` in its namespace. Repository `ns-<namespace>/pvc-<pvc>`. A StatefulSet volume gets one source per ordinal PVC. |
| ReplicationSource | restic mover, `copyMethod: Direct`, `cacheCapacity: 1Gi`, schedule `backup_schedule`, retain `backup_retention_*` plus `within: 7d`, `pruneIntervalDays: 7`. |
| PVC annotation `backup: none` | Never enrolled. Allowed under `addons/observability/victoria-metrics/` and `victoria-logs/`, and on a hosted app's volume its app file declares `backup: false`. |
| PVC annotation `backup: paused`, ConfigMap `kube-system/backup-rebuild-hold` | Either one sets `spec.paused` on the source. `site.yml` creates the hold only on a new cluster (no `flux-system/flux-system` GitRepository) whose backup host already has `ns-*/pvc-*` repositories; `data.repos` lists them. |
| CronJob `etcd-snapshots` | Hourly (`15 * * * *`, `time_zone`) on the control-plane node: `restic backup --skip-if-unchanged` of `backup_path/etcd-snapshots` (read-only) into `host-etcd-snapshots`, then `forget --prune` with `backup_retention_*`. Host name `etcd-snapshots`. |
| CronJob `restic-check` | On `backup_check_schedule` (default Wednesday 06:30) on the backup node: `restic check --no-lock --read-data-subset=<n>/12` on every repository, read-only; an `ns-app-*` repository only with its app's password mounted at `/credentials/<namespace>/`. Prints `PASS` or `FAIL` per repository. |
| Feature `offsite-backup-copy` | CronJob `restic-offsite`, daily 04:00: `restic copy --no-lock` of every local repository into `<backups-offsite RESTIC_REPOSITORY><path>`, `init --copy-chunker-params` when missing. Never forgets or prunes. An `ns-app-*` repository is copied with that app's password on both sides. |

## Expects

| What | From |
| --- | --- |
| `envs/<env>/secrets/backups-restic.sops.yaml`, decrypted with `flux-system/sops-age` | `jarvis secrets` |
| `backup_path`, `backup_schedule`, `backup_check_schedule`, `backup_retention_*`, `time_zone` | `values/cluster-vars.yaml` |
| A node labelled `node-role.jarvis.io/backup=true` (rest-server, `restic-check`, `restic-offsite`) and one `control_plane` (`etcd-snapshots`). Each tolerates `node-role.jarvis.io/dedicated=true:NoSchedule`. | inventory |
| HelmRelease `flux-system/volsync` Ready before the policies apply | `platform-controllers` health check |
| Kyverno's background controller may list ConfigMaps (the hold) and Secrets in `platform-backups` | [kyverno](../kyverno/CONTRACT.md) |
| Namespaces with enrolled PVCs set `volsync.backube/privileged-movers: "true"` (local-path ignores `fsGroup`) | the namespace's own manifest |
| For the off-host copy: `secrets/backups-offsite.sops.yaml` with `RESTIC_REPOSITORY` (ending `/`; `rest:` or `s3:`, not `sftp:`), `RESTIC_PASSWORD` (not the local one), and the backend's login variables | the owner ([docs/backups.md](../../../docs/backups.md#off-host-copy)) |
| `jarvis restore` and `jarvis drill` find CronJob `etcd-snapshots` with a digest-pinned `restic/restic` image | this component |

## Health

| Check | Pass |
| --- | --- |
| Service `restic` | a ready endpoint |
| Each PVC not `backup: none` outside `app-*`, and each ReplicationSource | has `restic-<pvc>` and ReplicationSource `<pvc>`, and the source syncs within one schedule interval |
| `restic-check`, `restic-offsite` | last success within one interval plus 12 hours; before the first success, a warning while the first run is due |
| `etcd-snapshots` | last success within one interval; the newest k3s snapshot is in the latest restic snapshot (`jarvis drill`) |
| `jarvis drill` | a scratch volume restored with matching checksums, and the etcd copy matching the k3s file |

## Limits

- Backups share a host with the data unless `backup_path` is on another disk, or the env lists the off-host copy. The off-host side only grows; its owner prunes it.
- No on-demand backup: `trigger.manual` starts a sync and is not a pause.
- App isolation is at the rest-server login. Both servers run as root on one hostPath, and core's login can delete `ns-app-*` files, though it cannot decrypt them.
- A retired app's repositories left on disk make `restic-check` fail until they are removed.
- Kyverno's background controller can read every Secret, app credentials included.
- Two `etcd-snapshots` copies started at the same moment on a repository that doesn't exist yet can leave it unreadable (each runs `restic init`). Don't start a copy by hand in a new cluster's first hour, before the hourly job has run once.
