# CONTRACT: platform-backups

Shared backup for every PVC. VolSync 0.17 with the restic mover is the backup. One CronJob copies k3s etcd snapshots into their own rest-server repository. ClusterPolicy `pvc-backup` enrolls every PVC, or the PVC is annotated `backup: none`. `ci/checks/pvc-backup` checks that this policy and its `backup: none` allow-list are present.

Core is one trusted zone with one restic credential: core namespaces, add-ons and `host-etcd-snapshots` all use `backups-restic`. A hosted app (`app-<name>` namespace) is not trusted: its PVCs use only that app's own credential, on a separate rest-server that confines each app to its own folder (App credentials, below). `ci/checks/backup-isolation` checks both directions.

## Offers

- Namespace `platform-backups`.
- rest-server 0.14: one Deployment and ClusterIP Service `restic` on port 8000. No Ingress.
- Bootstrap restic credentials (Secret `backups-restic`). Add-ons are in core's trusted zone, so their PVCs use this credential through the same policy ([services/CONTRACT.md](../../services/CONTRACT.md)).
- CronJob `etcd-snapshots`: a read-only copy of the k3s snapshot directory into repository `host-etcd-snapshots`.
- ClusterPolicy `pvc-backup` enrolls every PVC: one restic Secret and one VolSync `ReplicationSource` in that PVC's namespace. A PVC in an `app-` namespace gets them only from that app's own registered credential, never from `backups-restic`; with no registered credential it gets neither.
- Opt-in apps rest-server (`platform/backups/apps`): Deployment and ClusterIP Service `restic-apps` on port 8000, with `--private-repos`. Off by default; nothing for apps runs until an env lists it.
- One credential per hosted app that keeps data (Secret `platform-backups/backups-restic-app-<name>`), minted by `make bootstrap-secrets APP=app-<name>`. It opens only `ns-app-<name>/` on the apps server.
- A StatefulSet volume gets one `ReplicationSource` per PVC Kubernetes creates (`<template>-<statefulset>-<ordinal>`). Git does not list the ordinals.
- Opt-in CronJob `restic-offsite` (`platform/backups/offsite`): a nightly `restic copy` of every local repository to the env's off-host restic backend (Operations, Off-host copy).
- Opt-out for rebuildable observability data: the unprefixed annotation `backup: none` on a PVC, or on a `volumeClaimTemplate`'s `metadata`. Allowed only under `services/observability/victoria-metrics/` and `services/observability/victoria-logs/`.

## Expects

- Flux SOPS decryption via `flux-system/sops-age`. The file in Git has no plaintext secret.
- `envs/<env>/secrets/backups-restic.sops.yaml`. Flux decrypts it.
- Target path, retention counts, `backup_schedule`, `backup_check_schedule`, and `time_zone` come from `envs/<env>/values/cluster-vars.yaml`.
- The backup-role node (`node-role.jarvis.io/backup=true`) for the rest-server hostPath.
- The control-plane node (`node-role.jarvis.io/control_plane=true`), where k3s writes the snapshot files.
- rest-server, CronJob `etcd-snapshots`, and CronJob `restic-check` each match their role with required node affinity: `node-role.jarvis.io/<role>=true`, or, until that term is removed, `role.<dns_suffix>/<role>=true`. Each tolerates `node-role.jarvis.io/dedicated=true:NoSchedule`. Drill pods copy that affinity and toleration.
- An env that lists `platform/backups/offsite` in its platform-configs list also lists and holds `envs/<env>/secrets/backups-offsite.sops.yaml` (`ci/checks/env-schema` requires the file once the env is bootstrapped). CronJob `restic-offsite` has the same backup-role affinity and dedicated toleration as `restic-check`, without the transitional term.
- An env that registers an app credential lists `../../../../core/platform/backups/apps` under `resources:` and `backups/app-<name>` under `components:` in `envs/<env>/flux/platform-configs/kustomization.yaml`, and holds `envs/<env>/secrets/app-<name>/backups-restic-app-<name>.sops.yaml` (`ci/checks/env-schema`, App credentials below). `restic-apps` has the rest-server's backup-role affinity and dedicated toleration.
- A mover needs egress to the server its volume uses, TCP 8000, plus cluster DNS: a core or add-on namespace to Service `restic.platform-backups.svc.cluster.local`, an `app-<name>` namespace to Service `restic-apps.platform-backups.svc.cluster.local` only. A network policy that blocks this fails that namespace's backups.
- `make restore` needs CronJob `platform-backups/etcd-snapshots` deployed. `tools/restore` reads that CronJob once in `run()`, before it changes anything, and stops when the CronJob is missing or its image is not digest-pinned `restic/restic` (`restic/restic@sha256:` or `restic/restic:<version>@sha256:`, and 64 hex digits).

## Health

- The namespace exists.
- HelmRelease `flux-system/volsync` is Ready. `platform-controllers` health-checks it, so `platform-configs` applies the backup objects only after the VolSync controller is up.
- Secret `backups-restic` is present. Its value is not logged.
- Service `restic` has a ready endpoint.
- Kyverno admission is Ready.
- A PVC without `backup: none` has Secret `restic-<pvc>` and ReplicationSource `<pvc>`, and that source completes a sync.
- A PVC annotated `backup: none` has neither.
- A PVC in an `app-` namespace whose app has no registered credential has neither, and `make verify` fails it (`<namespace>/<pvc>: not backed up; <namespace> has no registered backup credential`).
- `make verify` checks the static half of app isolation: no user in `backups-restic`'s htpasswd is an app user (`ns-app-*`; only user names are read, nothing is printed), and, where the env lists the apps server, Deployment `restic-apps` runs `--private-repos` and mounts no `backups-restic`.
- Where the env lists the apps server: Service `restic-apps` has a ready endpoint. Its log line `app users: <n>` counts the registered apps.
- During `make restore`, that PVC's ReplicationSource stays paused until the destination finishes. It stays paused longer while the rebuild hold exists.
- `make drill` deletes a scratch volume, restores it, and compares checksums.
- A Job from CronJob `etcd-snapshots` completes, and `restic snapshots` on `host-etcd-snapshots` lists a snapshot that holds the newest k3s snapshot file.
- A Job from CronJob `restic-check` (on `backup_check_schedule` in `time_zone`, default Wednesday 06:30, on the backup-role node) completes. It runs `restic check --no-lock --read-data-subset=<n>/12` on every repository under `backup_path/restic` (the PVC repositories, app repositories included, and `host-etcd-snapshots`) through a read-only mount, and prints only `PASS` or `FAIL` per repository. An app repository whose app has no registered credential prints `FAIL <path>: no backup credential registered for its app`. It takes no lock, so it never blocks a backup, prune, or restore; a prune that runs at the same time can fail it, and the Job retries. `make verify` fails when its last success is older than one schedule interval plus 12 hours. With no success yet, `make verify` warns while the first run is pending or running inside the first interval plus 12 hours, and fails once a run finished without success or that time has passed. The `etcd-snapshots` check uses the same first-run rule without the 12 hours.
- Where the env lists the off-host copy: a Job from CronJob `restic-offsite` (daily 04:00 in `time_zone`) completes and prints `PASS` for every repository. `make verify` checks its last success with the `restic-check` rule (one interval plus 12 hours, same first-run warning).

## Config

- VolSync 0.17, restic mover, `copyMethod: Direct`. The controller installs the VolumeSnapshot CRD and no snapshot controller.
- k3s keeps writing etcd snapshots to `backup_path/etcd-snapshots`. The snapshot settings in the k3s config do not change. Those files are the same-node rollback copy.
- rest-server data is the hostPath `backup_path/restic`, pinned to the backup-role node.
- One repository per PVC is the directory `backup_path/restic/ns-<namespace>/pvc-<pvc>`.
- A volume's repository URL is `rest:http://restic.platform-backups.svc.cluster.local:8000/ns-<namespace>/pvc-<pvc>`.
- The `ns-` and `pvc-` prefixes keep a path segment off rest-server's reserved names (`data`, `index`, `keys`, `locks`, `snapshots`, `config`). The password stays out of the URL.
- Secret `backups-restic` holds `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (the server prefix `rest:http://restic.platform-backups.svc.cluster.local:8000/`), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`.
- A volume secret sets `RESTIC_REPOSITORY` to that volume's URL and copies the password and `RESTIC_REST_*` keys. VolSync 0.17 passes those two keys into the mover.
- The process runs with htpasswd auth and without `--append-only` or `--private-repos`. The mover forgets on every backup and prunes with the same credentials. Its htpasswd is only `backups-restic`'s, so it has core's one user and no app user.
- The apps server `restic-apps` runs the same pinned image on the same hostPath `backup_path/restic`, as root, with `--private-repos` and without `--append-only`: user `ns-app-<name>` can init, back up, forget and prune under `ns-app-<name>/` and nothing else. It never mounts `backups-restic`.
- Its htpasswd is built at start, in a memory-backed `emptyDir`, from the `htpasswd` key of each registered credential, mounted at `/credentials/app-<name>/htpasswd`. The user name is `ns-` plus that folder name; the name in the sealed line is ignored, so a credential can only ever be its own app's user. Only folders named `app-[a-z0-9-]*` count.
- A registration change edits the `restic-apps` pod template, so the Deployment restarts with the new user list (`Recreate`). rest-server reloads a changed htpasswd file, but the list is only rebuilt at start.
- An app's repository URL is `rest:http://restic-apps.platform-backups.svc.cluster.local:8000/ns-app-<name>/pvc-<pvc>`, the same `ns-<namespace>/pvc-<pvc>` layout on the same disk, so the rebuild hold, `make restore` and `restore-all` see app repositories like any other.
- Secret `backups-restic-app-<name>` holds `RESTIC_PASSWORD` (that app's own), `RESTIC_REPOSITORY` (the apps server prefix), `RESTIC_REST_USERNAME` (`ns-app-<name>`), `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`.
- `restic-check` and `restic-offsite` open an `ns-app-<name>/...` repository with that app's own password, mounted at `/credentials/app-<name>/RESTIC_PASSWORD`, with core's password removed from that command's environment. Core repositories use core's password as before. Each job mounts only the `RESTIC_PASSWORD` key of an app credential, and the apps server only the `htpasswd` key.
- Retention counts are the env settings `backup_retention_daily`, `backup_retention_weekly`, and `backup_retention_monthly`.
- A ReplicationSource copies those into `restic.retain`, sets `retain.within: 7d`, and sets `pruneIntervalDays: 7`.
- `within` keeps every snapshot from the last 7 days (VolSync passes `--keep-within`), so one backup of an empty recreated claim cannot forget that day's good snapshot yet.
- It does not replace the rebuild hold: after 7 days, keep-daily still keeps only the newest snapshot of the loss day.
- Enrollment is ClusterPolicy `pvc-backup` in this directory (platform-configs, after the VolSync CRDs).
- `failurePolicy` is Ignore on `spec.webhookConfiguration.failurePolicy` only. `spec.failurePolicy` is unset.
- Each generate rule sets `generateExisting` and `synchronize`. It skips a PVC annotated `backup: none` and a PVC labeled `app.kubernetes.io/created-by: volsync`.
- A PVC is enrolled once it is bound (`spec.volumeName` is set). VolSync 0.17 starts the mover Job immediately, and generating it before local-path binds the claim races two `restic init`s on the new repository.
- ClusterPolicy `pvc-backup` is annotated `kustomize.toolkit.fluxcd.io/force: Enabled`. Generate rules cannot be edited in place, so Flux recreates the policy. Recreating it deletes and regenerates the generated Secrets and ReplicationSources. Restic data on disk stays.
- For every PVC that is not annotated `backup: none` and not labeled `app.kubernetes.io/created-by: volsync`, it generates Secret `restic-<pvc>` and ReplicationSource `<pvc>` in that namespace.
- `RESTIC_REPOSITORY` is Secret `backups-restic` key `RESTIC_REPOSITORY` plus `ns-<namespace>/pvc-<pvc>`.
- `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, and `RESTIC_REST_PASSWORD` are copied from that Secret with a data generate and an apiCall lookup.
- Rules `restic-secret` and `replication-source` exclude namespaces `app-*`. Rules `restic-secret-app` and `replication-source-app` match only `app-*`. They read `platform-backups/backups-restic-<namespace>` and nothing else. Their apiCall defaults are empty, so a missing credential skips the claim (precondition) instead of erroring. The app Secret is built like the core one from that app's Secret. The app ReplicationSource is the same generate block as the core one, hold and `backup: paused` included.
- Generate runs on PVC events and policy changes, not on a new Secret. A credential registered after the app's PVC already exists enrolls it on the PVC's next update (App credentials, below).
- Rule `privileged-movers` still covers every enrolled namespace, `app-*` included: local-path ignores `fsGroup`, so an app's mover runs privileged too.
- The ReplicationSource uses the restic mover, `copyMethod: Direct`, `cacheCapacity: 1Gi`, and `trigger.schedule` from `backup_schedule`.
- A mutate-existing rule sets annotation `volsync.backube/privileged-movers: "true"` on the PVC's namespace. It lands asynchronously, so the first sync may fail once and pass on retry.
- The VolSync controller pod's `TZ` is `time_zone`. The 0.17 cron parser uses the process local zone and has no timezone field. The chart has no env hook, so a HelmRelease postRenderer sets `TZ` and mounts the node zoneinfo directory.
- `platform-backups` does not set `volsync.backube/privileged-movers`. No mover in this namespace mounts a hostPath.
- The rest-server runs as root so it can write its hostPath.
- The etcd CronJob runs as root so it can read the snapshot files (mode 0600). It is not a mover.
- CronJob `etcd-snapshots` schedules on `node-role.jarvis.io/control_plane=true`, and still matches `role.<dns_suffix>/control_plane=true` until that affinity term is removed.
- It mounts `backup_path/etcd-snapshots` read-only (`hostPath` type `Directory`) and runs `restic backup` of that mount into `host-etcd-snapshots`.
- That repository is the `RESTIC_REPOSITORY` prefix from Secret `backups-restic` plus the path segment `host-etcd-snapshots`.
- `restic init` runs only when `restic cat config` exits 10 (repository missing). Any other error stops the run.
- `spec.timeZone` is `time_zone`. The schedule is `5 */12 * * *`, five minutes after the k3s role default `0 */12 * * *`.
- `concurrencyPolicy` is `Forbid`.
- Each run forgets with `backup_retention_daily`, `backup_retention_weekly`, and `backup_retention_monthly`, then prunes.
- The snapshot host is the fixed name `etcd-snapshots`, so retention is one group across runs.
- The image is official `restic/restic:<version>@sha256:`, digest-pinned. Renovate updates the version and the digest.
- The container requests `100m` CPU and `128Mi` memory, limits memory to `256Mi`, and sets no CPU limit.
- `backup: paused` is how a restore holds one PVC. The policy sets ReplicationSource `spec.paused` from that annotation or from ConfigMap `kube-system/backup-rebuild-hold` (`to_boolean`).
- The hold's apiCall `default` is the string `false`, so a missing ConfigMap is not paused.
- A forbidden read is the same as a missing ConfigMap, which is why `kyverno:backup-generate` must be allowed to get that ConfigMap.
- VolSync 0.17 still creates the mover Job and sets its parallelism to 0, so no mover pod runs and restic does not backup or forget.
- `trigger.manual` is not the pause. Setting it starts a sync. `backup: none` is not the pause. That annotation means the volume is never enrolled.
- The rebuild hold is cluster state, not Git.
- `site.yml` creates ConfigMap `kube-system/backup-rebuild-hold` only when the root `GitRepository` `flux-system/flux-system` is absent (the cluster is new and Flux syncs nothing yet) and the inventory `backup` host already has restic repositories under `backup_path/restic/ns-*/pvc-*`. Any other lookup error fails the play.
- The ConfigMap's `data.repos` is that list. While it exists, new ReplicationSources are generated paused, so a cluster rebuilt on a disk that still has restic data cannot snapshot empty volumes over the last good backup.
- A later `site.yml`, once the root `GitRepository` exists, does not create or change the hold.
- Deleting the ConfigMap does not re-run generate on sources that already exist.
- The destination uses the restic mover, `copyMethod: Direct`, `enableFileDeletion: true`, and `restoreAsOf` as in Operations.
- There is no VolumeSnapshot, volume populator, or snapshot class.
- The recreated PVC must already carry `backup: paused` before it binds, because a new ReplicationSource starts its mover immediately and an unpaused source would snapshot the empty claim.
- Direct copy needs the PVC free of other mounts.

## Proven by

- `ci/tests/test_flux_entry.py` (SOPS-encrypted secret in Git, platform-configs decryption).
- `ci/tests/test_restic_rest_server.py` (rest-server rules and the etcd CronJob rules) and `ci/tests/test_bootstrap_secrets.py`.
- `ci/checks/pvc-backup` and `ci/tests/test_pvc_backup.py` (policy present, `backup: none` allow-list, no ReplicationSource in Git, no hand-written VolSync object or restic repository URL under `services/` or `apps/`).
- `ci/tests/test_backup_enrollment.py` (Kyverno controllers, aggregated roles, policy rules, `backup: paused` sets `spec.paused`).
- `ci/tests/test_restore.py` (restore pauses the source, then applies a Direct destination with `restoreAsOf` before the PVC creation time and `enableFileDeletion`; no snapshot fails from mover status; `AS_OF` restores a live volume; `restore-all` keeps, retires, or lifts the hold).
- `ci/tests/test_verify.py` (read-only, fail-closed standing checks, including enrollment freshness and the rebuild hold).
- `ci/tests/test_restic_rest_server.py` (off-host copy renders only where the env lists it; the copy script initialises a missing destination, copies each repository, prints only `PASS`/`FAIL`, and fails every repository at once when the target is down; the source-password key wiring), `ci/tests/test_env_schema.py` (listing it requires the sealed file), `ci/tests/test_verify.py` (its freshness).
- `ci/tests/test_drill.py` and `make drill ENV=<env>` (real restore target, checksum comparisons, etcd-copy check, and in-cluster cleanup).
- `ci/checks/backup-isolation` (on every env's rendered platform-configs and on `platform/backups/apps`) and `ci/tests/test_backup_isolation.py`: a registered app renders its Secret and mounts only where needed; a core-only env renders nothing for apps; seeded failures for a Secret rule that can reach an `app-` namespace from `backups-restic`, an app credential in core's server, `backups-restic` in the apps server, the apps server without `--private-repos`, and an app credential without the apps server; and env-schema's registration rules.
- `ci/tests/test_rest_server_isolation.py`: the pinned `rest-server` and `restic` (installed by `make deps`) run as the manifests configure them, core's server plus the apps server's own start script on one data directory. An app login inits, backs up, forgets and prunes its own repository. It cannot open, forget or prune a core repository or another app's on either server, even with the right restic password. It cannot create a core repository. A sealed line naming core's user is still served only as `ns-app-<name>`.
- `ci/tests/test_backup_enrollment.py` (the `-app` rules read only the app's own Secret and skip a missing one), `ci/tests/test_restic_rest_server.py` (check and copy use the app's own password with core's unset, and fail an unregistered app's repository alone), `ci/tests/test_bootstrap_secrets.py` (`APP=` mints the app's own credential once and refuses a recorded one), `ci/tests/test_verify.py` (the app-isolation check and an unregistered app PVC).

## Operations

### Role labels and the dedicated taint

Merge first and let Flux roll the new pod spec before Ansible removes `role.<dns_suffix>/` labels. rest-server is `Recreate`, and the new spec still matches the old label. The label play reads rest-server and both CronJobs and stops if one exists without the new affinity term and the dedicated toleration. A missing object does not stop it: this play runs before Flux.

1. Merge. Wait until `platform-configs` is Ready, rest-server is Running, and both CronJob templates show the new affinity and toleration.
2. `make site ENV=<env> TAGS=labels CHECK=1`, then the same without `CHECK=1`, then once more (`changed=0`). (`TAGS=labels` runs only the label play and needs no age key; `TAGS=k3s` also runs it but reads the sealed tokens.)
3. That run adds `node-role.jarvis.io/<role>=true`, removes `role.<dns_suffix>/` labels, and sets `node-role.jarvis.io/dedicated=true:NoSchedule` only on nodes with neither `workers` nor `edge`.
4. `make verify` reads `node-role.jarvis.io/` labels, so run it after that run.

A Job started before this merge has no deadline. Delete one stuck `Pending`: `concurrencyPolicy: Forbid` blocks the next run, and `activeDeadlineSeconds: 21600` ends only a later Job.

After Ansible has run, `kubectl label node <node> role.<dns_suffix>/<role>=true` for each role and `kubectl taint node <node> node-role.jarvis.io/dedicated:NoSchedule-` (`KUBECONFIG=~/.kube/jarvis-<env>.yaml`), then revert. Do not run Ansible, including `make site`, until that revert lands.

Do not merge a change to ClusterPolicy `pvc-backup` during a restore. The policy is annotated `kustomize.toolkit.fluxcd.io/force: Enabled`, so Flux recreates it and Kyverno regenerates every Secret and ReplicationSource.

### App credentials

Each hosted app that keeps data gets its own restic password, its own apps-server login and its own repositories, before it stores data. Owner-run, needs the age public key only.

Turn the apps server on and add an app:

1. `make bootstrap-secrets ENV=<env> APP=app-<name>`. It seals `envs/<env>/secrets/app-<name>/backups-restic-app-<name>.sops.yaml` and writes `envs/<env>/flux/platform-configs/backups/app-<name>/kustomization.yaml`, the Component that applies that Secret and mounts it in `restic-apps`, `restic-check` and `restic-offsite`. Nothing is printed but the created paths. A second run changes nothing. If git already records that app's sealed file, it refuses: the app's repositories open only with the first password.
2. In `envs/<env>/flux/platform-configs/kustomization.yaml`, list `../../../../core/platform/backups/apps` under `resources:` once per env, and add `backups/app-<name>` under `components:`. Commit and push.
3. After Flux applies it: `restic-apps` restarts with the new user (`app users: <n>`). The app's PVCs get Secret `restic-<pvc>` and ReplicationSource `<pvc>` when they bind. A PVC that existed before step 2 is enrolled on its next update; annotate it once (`kubectl annotate pvc -n app-<name> <pvc> backup-credential=registered --overwrite`).
4. `make verify ENV=<env>` passes, with no `not backed up` line for that app.

How to tell it worked: `restic-apps` has a ready endpoint, the app's ReplicationSource completes a sync, and the next `restic-check` prints `PASS ns-app-<name>/pvc-<pvc>`.

Retire an app (after its app is removed and its data is no longer wanted):

1. Remove `backups/app-<name>` from `components:` and delete `envs/<env>/flux/platform-configs/backups/app-<name>/`. Commit and push. `restic-apps` restarts without that user, so its login stops working. Flux keeps Secret `backups-restic-app-<name>` (prune disabled); delete it with `kubectl -n platform-backups delete secret backups-restic-app-<name>`.
2. The repositories stay on disk, and `restic-check` and `restic-offsite` print `FAIL ns-app-<name>/...: no backup credential registered for its app` until they are removed. Remove `<backup_path>/restic/ns-app-<name>` as root on the backup-role node once the data is not needed. The off-host copy keeps its copy until the owner removes it there.
3. Leave `envs/<env>/secrets/app-<name>/backups-restic-app-<name>.sops.yaml` in place. Re-adding that app name later reuses it (step 1 of adding writes only the Component), so any repositories still on disk open again. Once Git has recorded that file, `make bootstrap-secrets` never mints a new one for that name.

When the last app is retired, remove the `core/platform/backups/apps` line too. Flux keeps Deployment `restic-apps` (prune disabled); delete it and Service `restic-apps` by hand.

### Restore one namespace

From the repo root, with `~/.kube/jarvis-<env>.yaml` present:

```bash
make restore ENV=<env> APP=<namespace>
```

`APP` is the namespace. Optional `AS_OF=<RFC3339>` sets `restoreAsOf` for every selected PVC. The default is one second before the PVC `creationTimestamp`, which is the right point for a claim that was deleted and recreated. A volume that was not recreated needs `AS_OF`, because that default is before its first snapshot.

Before any change, restore checks the repository env marker and the cluster's `flux-system/cluster-vars` `jarvis_env` marker, then reads CronJob `platform-backups/etcd-snapshots`. It stops if that CronJob is missing or its image is not digest-pinned `restic/restic`.

What it changes, for each selected PVC:

- Suspends the Flux Kustomization labeled on the PVC or its Deployment, StatefulSet, or CronJob.
- Suspends CronJobs that mount the PVC, and scales mounting Deployments and StatefulSets to 0.
- Annotates `backup: paused` and waits until ReplicationSource `spec.paused` is true, the source Job's parallelism is 0, and no pod that mounts the PVC is running, pending, or terminating. This last guard also refuses active Jobs, DaemonSets, and other unowned writers. `status.active` does not count a terminating pod, and that pod can still hold the volume.
- An unbound PVC is annotated `backup: paused` first, then bound by a short-lived pod that uses the CronJob image and the `nodeSelector`, `affinity`, and `tolerations` of the Deployment, StatefulSet, or CronJob that mounts it (a template `nodeName` becomes node affinity for that node), because local-path puts the volume on that pod's node. Optional `NODE=<node>` pins it to that node instead and tolerates the node's taints. With no mounting workload and no `NODE`, or mounting workloads that ask for different placement, restore stops before any change. That pod is deleted and must be gone before the copy.
- Waits for Secret `restic-<pvc>`, applies a ReplicationDestination, and waits for `status.lastManualSync`. VolSync then deletes the mover Job.
- The result is `status.latestMoverStatus.logs`. `No eligible snapshots found` means it restored nothing and exited 0. This mode fails on that line. A log with neither that line nor a `restoring` line fails, including the empty log VolSync stores when it cannot read the mover pod.
- On success it deletes the destination, restores replica counts and CronJob suspension, and resumes the Flux Kustomization. It removes `backup: paused`, including one set on purpose, unless the rebuild hold still exists.
- On failure or interruption it leaves the pause and quiesced state and prints one environment-bound command for each remaining undo action.

`make restore` does not lift the rebuild hold. While the hold exists, a successful `make restore` leaves `backup: paused` in place.

How to tell it worked: a `PASS restore <namespace>/<pvc> snapshot <id>` line, then `SUMMARY restored=... accepted=... retired=...`, and exit 0.

### Restore every enrolled PVC

```bash
make restore-all ENV=<env>
```

Optional `RETIRE=ns-<namespace>/pvc-<name>[,...]`, `ACCEPT_EMPTY=<namespace>/<pvc>[,...]`, and `NODE=<node>`. One `NODE` pins every unbound PVC in the run to that single node.

`restore-all` selects every PVC that is not `backup: none` and not labeled `app.kubernetes.io/created-by: volsync`, including one that is not bound yet. It reads the etcd CronJob image once, before any change, the same way `make restore` does.

What it changes: the same per-PVC restore as above. `No eligible snapshots found` fails unless `ACCEPT_EMPTY` names that exact `namespace/pvc`. This acceptance is separate from `RETIRE`.

`RETIRE` is only for a held repository that no longer has a PVC, such as an app removed from Git. `ACCEPT_EMPTY` is only for selected PVCs whose empty state the owner accepts. `--retire` and `--accept-empty` are only valid with `restore-all`.

`make restore-all` deletes the hold only after every restore has succeeded or was explicitly accepted empty and every listed repository has a PVC or is named in `RETIRE`. It then removes `backup: paused`. Sources cannot report unpaused while the hold exists, so restore deletes the hold before removing those annotations and checking every source. If resume or that check fails, it re-adds the annotations and restores the hold. If rollback itself fails, it prints the exact environment-bound recovery commands and exits nonzero. A listed repository that is neither restored, accepted empty, nor retired keeps the hold, and the restored PVCs stay `backup: paused`.

How to tell it worked: `HOLD lifted` when a hold was set, `SUMMARY` with restored, accepted-empty, and retired counts, and exit 0. With no hold and no PVC (a fresh env), one line `nothing to restore: no rebuild hold and no PVC` and exit 0. `make restore APP=<namespace>` with no match still fails.

### Drill

```bash
make drill ENV=<env>
```

Uses `~/.kube/jarvis-<env>.yaml` and checks both env markers. Before any change, including `make drill-cleanup`, it reads CronJob `platform-backups/etcd-snapshots` and stops when that CronJob is missing or its image is not digest-pinned `restic/restic`. `make drill` refuses a rebuild hold, an existing `platform-drill` namespace, leftover drill pods in `platform-backups`, or an existing drill repository path. Leftovers point to `make drill-cleanup ENV=<env>`.

Repository preflight uses its own short-lived backup-node pod with the rest-server hostPath mounted read-only, and removes that pod before continuing. The pod image comes from the live `etcd-snapshots` CronJob.

What it changes:

- Writes a scratch payload and waits for a fresh ReplicationSource sync.
- Deletes and recreates the paused PVC, runs `make restore ENV=<env> APP=platform-drill NODE=<node>` with the node the writer pod ran on, and compares checksums it computed itself.
- Separately, a freshly created (never reused) scratch pod in `platform-backups`, where `backups-restic` exists, restores the newest snapshot file from `host-etcd-snapshots` and compares it with the file on the read-only backup path.
- The tool never reads or copies Secret data client-side, never runs a cluster reset, and never prints Secret values.
- On success, failure, SIGINT, SIGTERM, or SIGHUP, it removes the scratch namespace with a bounded wait, then runs a short-lived pod on the backup-role node that mounts the rest-server data path and removes `ns-platform-drill/pvc-drill`.
- If either cleanup cannot finish, it prints `make drill-cleanup ENV=<env>` for an env-bound retry. Adopters do not clean a drill repository on a node by hand.

How to tell it worked: `PASS drill cleanup complete` and exit 0. The volume checksums match. After the two etcd checksums match, the tool prints one `ETCD snapshot ... checksum` line. When the newest k3s snapshot file is not in the latest restic snapshot yet, the drill fails with `newest etcd snapshot <file> is not yet in the restic copy`; run it again after the next `etcd-snapshots` run.

### etcd cluster reset

Owner-run. It is not a make target and it is not part of the drill. Do not print Secret values. Do not run it as a drill on the live env.

k3s writes snapshot files on the server node to `<backup_path>/etcd-snapshots`. That directory is `etcd-snapshot-dir` in the k3s config. The reset uses one of those files. It does not use restic, and it does not need the cluster to be up. Run these steps as root on the server node.

1. Run `ls -l <backup_path>/etcd-snapshots` and choose a file.
2. Stop k3s (`systemctl stop k3s`). Run `/usr/local/bin/k3s server --cluster-reset --cluster-reset-restore-path=<backup_path>/etcd-snapshots/<file>` once. Start k3s (`systemctl start k3s`).

What it changes: the node's k3s datastore is replaced by that snapshot.

How to tell it worked: k3s starts and serves the restored datastore.

k3s secrets-encryption is on, so restoring onto a rebuilt node also needs the original k3s server token (Rebuild a dead node, below). The steps above are for the same node. The drill only checks that the newest restic copy matches the k3s file's sha256.

### Rebuild a dead node

Owner-run. Do not print a token. It needs `envs/<env>/secrets/k3s-token.sops.yaml` to hold this cluster's server token. k3s uses that token as the passphrase for its datastore and snapshots, so a server with a new token cannot read them. `make bootstrap-secrets` mints tokens only for a new env. For a cluster that already exists, the owner seals its token before any loss: `make seal ENV=<env> NAME=k3s-token` from a mode `0600` Secret manifest with `stringData.server_token` and `stringData.agent_token`. Seal only the passphrase in `server_token`: the part of `/var/lib/rancher/k3s/server/token` after `::server:`. The full `K10<ca-hash>::server:<passphrase>` form pins the cluster CA hash, so it fails on any rebuild that mints a fresh CA (see [k3s token](https://docs.k3s.io/cli/token)). The passphrase is the same value, so older snapshots stay restorable. The k3s role refuses a `K10…` server token on a server that has no token file yet. A new env gets an agent token that is a separate random value. A cluster sealed from its node may have an agent token equal to its server token until the agent token is rotated at its next rebuild.

**Server.**

1. Reinstall the node, mount a surviving data disk at `/var/lib/jarvis`, and run `tools/prep-node` (`docs/deploy.md`). If the data disk is lost too, prepare a new one and, before step 2, `restic restore` a snapshot file from repository `host-etcd-snapshots` on the backup host (password: `RESTIC_PASSWORD` in `backups-restic`) into `<backup_path>/etcd-snapshots`. When the server is also the backup host, that disk held the only copy.
2. `make site ENV=<env>`. The server starts with the sealed token, and agents join with the agent token. When the backup host still has restic repositories and there is no root `GitRepository` (`flux-system/flux-system`) yet, `site.yml` creates the rebuild hold.
3. Once Flux has reconciled, run `make restore-all ENV=<env>` (above).

With the same token, the etcd cluster reset above can also restore a snapshot onto the rebuilt server.

**Rebuild in place (OS kept).** For a single-node env whose backups share the OS disk: remove k3s and keep the OS and `/var/lib/jarvis`, then rebuild the cluster on the same node. As root on the node:

1. Record `restic snapshots` counts for the local repositories (and the off-host copy, when the env lists it), to compare after step 4.
2. The k3s role installs the pinned binary and its own unit, not upstream `install.sh`, so the node has no `k3s-uninstall.sh`. Fetch `install.sh` at the pinned tag (`https://raw.githubusercontent.com/k3s-io/k3s/<k3s_version>/install.sh`, `+` as `%2B`) and let it write only the scripts: `INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true INSTALL_K3S_BIN_DIR=/usr/local/bin sh install.sh`. It finds the existing binary, rewrites the unit, and writes `/usr/local/bin/k3s-uninstall.sh` and `k3s-killall.sh`. It does not start k3s.
3. Run `/usr/local/bin/k3s-uninstall.sh`. At the current pin it stops k3s and its pods, unmounts and removes `/run/k3s`, `/var/lib/kubelet/pods` and `/var/lib/kubelet/plugins`, removes `/var/lib/rancher/k3s` (the k3s data dir), `/etc/rancher/k3s`, `/var/lib/kubelet`, `/var/lib/cni` and `/run/flannel`, the binary, and the unit. It touches nothing under `/var/lib/jarvis`, which holds local volume data (`local_path_data`). `backup_path`, with the restic repositories and the etcd snapshot files, stays too unless it sits inside one of the removed directories. It skips the removal when another `k3s*.service` unit exists.
4. Check that the restic repositories under `<backup_path>/restic` and the snapshot files in `<backup_path>/etcd-snapshots` are intact, and that `restic snapshots` counts match step 1.
5. `make site ENV=<env>`, then `make restore-all ENV=<env>` as above.

The new cluster starts from an empty datastore with the sealed tokens, and restores no etcd snapshot. An older etcd snapshot stays restorable as long as `server_token` is unchanged. When the env has no PVCs, `restore-all` is a clean no-op (`nothing to restore: no rebuild hold and no PVC`, exit 0).

When the rebuild ships with a change to Flux sources or paths, suspend every Flux Kustomization before that change merges, so the new sources do not prune objects the old paths applied. When the CA was rotated, trust the new `ca.crt` on clients and remove the old one.

**Agents.** While the server is down, agents keep their pods running but cannot reach the API. They reconnect when it is back. An agent reinstalled under the same hostname is refused by the server's stored node password. Delete its Node (`kubectl delete node <host>`) or Secret `kube-system/<host>.node-password.k3s` first, then run `make site ENV=<env> LIMIT=<host>`.

How to tell it worked: `make site` exits 0, `restore-all` prints `HOLD lifted` and `SUMMARY` and exits 0, and then `make verify ENV=<env>` passes. A planned bench rehearsal with the data disk kept proves OS loss only, not disk loss.

### Rotate the restic password

This rotation covers core repositories only. Wherever a step below lists repositories, skip every `ns-app-*` directory, and in the hash check skip PVCs in `app-` namespaces: each app's repositories open only with that app's own password, which this rotation does not touch.

Every core repository uses the one password in Secret `platform-backups/backups-restic`. Kyverno copies it into each `restic-<pvc>` Secret (`synchronize: true` on an apiCall). CronJob `etcd-snapshots` reads that same Secret and writes repository `host-etcd-snapshots`. Changing the Secret changes every repository at once.

`make bootstrap-secrets` does not replace a `backups-restic` file git already records unless `ROTATE_RESTIC=1`. That flag mints a new password and does not add it to existing repositories. A live rotation is the steps below. Do not point them at the drill repository: `make drill` creates and deletes `ns-platform-drill/pvc-drill` in the same run, and this rotation does not run `make drill`. Keep the old key until the last step. While any Secret still holds the old password, that key still opens every repository.

Run the `restic` and `find` commands as root on the backup-role node. `restic` must be installed there. The rest-server data is the hostPath `backup_path/restic`, and that hostPath is root-owned. `<backup_path>` is the env setting `backup_path`. Password files are mode `0600` and are not printed. `sops` decrypts with `SOPS_AGE_KEY_FILE` when that is set, otherwise with the default age key path.

1. Write the current password and the new password to files before the Secret changes. The pipeline writes the current password and does not print it. `test -s` checks that the file is non-empty. The new password has no trailing newline.

```bash
umask 077
KUBECONFIG=~/.kube/jarvis-<env>.yaml kubectl get secret -n platform-backups backups-restic -o jsonpath='{.data.RESTIC_PASSWORD}' | base64 -d > old-password
test -s old-password
openssl rand -base64 32 | tr -d '\n' > new-password
```

List repositories and add the new key to each one, including `host-etcd-snapshots`. Each directory that contains `config` is one repository. This list is only the set that receives the new key now. The last step lists repositories again. Skip `ns-platform-drill/pvc-drill` if it is present.

```bash
find <backup_path>/restic -type f -name config -printf '%h\n'
```

For each directory, record the current id from `restic key list`, then add the new key and keep the old one:

```bash
RESTIC_PASSWORD_FILE=old-password restic -r <repo> key list
RESTIC_PASSWORD_FILE=old-password restic -r <repo> key add --new-password-file new-password
```

2. Write that same new password into the sealed file, then commit and push to the branch `flux_git_ref` names. Flux updates Secret `platform-backups/backups-restic`. The value file is a JSON string, so the password is not a command argument.

```bash
python3 -c 'import json, pathlib, sys; pathlib.Path(sys.argv[2]).write_text(json.dumps(pathlib.Path(sys.argv[1]).read_text()))' new-password new-password.json
sops set --value-file envs/<env>/secrets/backups-restic.sops.yaml '["stringData"]["RESTIC_PASSWORD"]' new-password.json
```

3. Confirm Secret `platform-backups/backups-restic` and every generated `restic-<pvc>` Secret changed. Compare sha256 of `new-password` with sha256 of each Secret's `RESTIC_PASSWORD` bytes. The command prints hashes only. It reads those Secrets by name. It lists PVCs to learn the names. It does not read every Secret in the cluster.

```bash
sha256sum new-password
KUBECONFIG=~/.kube/jarvis-<env>.yaml python3 -c 'import base64, hashlib, json, subprocess
def get(*args):
    return json.loads(subprocess.check_output(["kubectl", *args]))
def emit(ns, name):
    raw = (get("get", "secret", "-n", ns, name, "-o", "json").get("data") or {}).get("RESTIC_PASSWORD")
    print(f"{ns}/{name} MISSING" if raw is None else f"{ns}/{name} {hashlib.sha256(base64.b64decode(raw)).hexdigest()}")
emit("platform-backups", "backups-restic")
for pvc in get("get", "pvc", "-A", "-o", "json")["items"]:
    meta = pvc["metadata"]
    if (meta.get("annotations") or {}).get("backup") == "none":
        continue
    if (meta.get("labels") or {}).get("app.kubernetes.io/created-by") == "volsync":
        continue
    emit(meta["namespace"], "restic-" + meta["name"])'
```

The pinned Kyverno chart does not promise that a change only in the apiCall source regenerates those Secrets. If a hash is still the old one, change an annotation on each enrolled PVC (every PVC that is not `backup: none`) so the generate rule runs again, then repeat the hash check. Do not edit a generated Secret. `kubectl annotate pvc -n <namespace> <name> restic-password-generation=<token> --overwrite` is enough. Until every hash matches `new-password`, leave the old key in place.

4. Prove the new password on an existing repository before any old key is removed.

A Job from CronJob `etcd-snapshots` is the backup of `host-etcd-snapshots` an operator can start. Delete a finished Job of this name first if it exists. The Job reads Secret `backups-restic`.

```bash
KUBECONFIG=~/.kube/jarvis-<env>.yaml kubectl -n platform-backups create job --from=cronjob/etcd-snapshots restic-password-rotation
KUBECONFIG=~/.kube/jarvis-<env>.yaml kubectl -n platform-backups wait --for=condition=complete job/restic-password-rotation --timeout=600s
```

VolSync has no on-demand backup. Do not set `trigger.manual`. After the hashes match, pick one repository that step 1 already listed, other than `host-etcd-snapshots`. Its directory is `<backup_path>/restic/ns-<namespace>/pvc-<pvc>`, and its ReplicationSource is `<pvc>` in that namespace. Record `status.lastSyncTime`. Wait until that field is a later time. If a sync was already running when the Secret changed, wait for one more change, so the mover started after the hash matched. The mover reads Secret `restic-<pvc>`. That later sync is the proof VolSync is using the new password on that existing repository. `make verify` is not this proof. Its backup freshness check only asks whether the last sync falls inside the schedule window, and a sync from before the change can still pass.

```bash
KUBECONFIG=~/.kube/jarvis-<env>.yaml kubectl -n <namespace> get replicationsource <pvc> -o jsonpath='{.status.lastSyncTime}{"\n"}'
RESTIC_PASSWORD_FILE=new-password restic -r <backup_path>/restic/ns-<namespace>/pvc-<pvc> cat config >/dev/null
```

5. List repositories again immediately before removing any old key. A repository created during this rotation (a new PVC, or a sync that initialized a repository before the Secret changed) is not on the step 1 list. If it has only the old password, removing keys from the old list would leave it behind.

Open every directory from the new list, except `ns-platform-drill/pvc-drill`, with the new password. If any open fails, stop. Do not remove an old key from any repository. Add the new key to the one that failed, then repeat this check from the new list.

```bash
find <backup_path>/restic -type f -name config -printf '%h\n' > repos.txt
while IFS= read -r repo; do
  case "$repo" in
    */ns-platform-drill/pvc-drill) continue ;;
  esac
  RESTIC_PASSWORD_FILE=new-password restic -r "$repo" cat config >/dev/null || exit 1
done < repos.txt
```

Only after every remaining repository opens, remove old keys. For a repository from step 1, remove the id recorded then. For a repository that was not on that list, run `key list`. A single id means the repository was created after the Secret changed and the only key is the new one: leave it. More than one id, with no id recorded in step 1, means stop and do not guess which id is old.

```bash
RESTIC_PASSWORD_FILE=new-password restic -r <repo> key list
RESTIC_PASSWORD_FILE=new-password restic -r <repo> key remove <old-id>
```

Delete `old-password`, `new-password`, `new-password.json`, and `repos.txt`.

### Off-host copy

What it does: CronJob `restic-offsite` runs daily at 04:00 in `time_zone` on the backup-role node. It mounts `backup_path/restic` read-only and, for `host-etcd-snapshots` and every `ns-*/pvc-*`, runs `restic copy --no-lock` from that local repository into the off-host repository `<RESTIC_REPOSITORY><path>`. A missing off-host repository is created first with `restic init --copy-chunker-params`, so the copy deduplicates. It prints only `PASS <path>`, `FAIL <path>: restic exited <n>`, or the unreachable-target line below. It never forgets or prunes on either side. The local side takes no lock, so a run that overlaps a prune can only fail and retry. A missing `host-etcd-snapshots` fails.

Credentials: the local password is `RESTIC_PASSWORD` from `backups-restic` for core repositories. A hosted app's repository `ns-app-<name>/...` is copied with that app's own password on both sides, so its off-host copy also opens only with the app's password, and is kept apart from core's by path and key. It needs no new target login: apps never hold the target login, and they cannot reach the target. An app repository whose app has no registered credential prints `FAIL <path>: no backup credential registered for its app`. Everything about the target is in one Secret per env, `platform-backups/backups-offsite`, loaded as environment variables:

- `RESTIC_REPOSITORY`: the off-host prefix, ending in `/`, for example `rest:http://<user>:<password>@<host>:8000/` (`https` also works), or `s3:https://<endpoint>/<bucket>/`. `sftp:` is not supported.
- rest-server setup: one rest-server per env, run without `--private-repos` (it serves only two path levels, and the user-name level would break `ns-<ns>/pvc-<pvc>`). `RESTIC_REPOSITORY` is the server root ending in `/`. Append-only mode is fine because the copy never forgets or prunes.
- `RESTIC_PASSWORD`: the off-host restic password. It is not the local one.
- The backend's own variables: `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for `s3:`; `RESTIC_REST_USERNAME` and `RESTIC_REST_PASSWORD` for `rest:` when the login is not in the URL.
- The Secret is read only as environment variables; nothing is mounted from it.
- A first `restic cat config` probe of the target, limited to 30 seconds, runs before any repository. When it neither succeeds nor reports a missing repository (exit 10), every repository prints `FAIL <path>: off-host target not reachable (probe exited <n>)` at once and the Job fails.

Turn it on (owner-run, needs the age public key only):

1. Set up the target: an empty bucket or a rest-server that only this env writes, and a login for it.
2. Write a mode `0600` Secret manifest outside the repo: `kind: Secret`, `metadata.name: backups-offsite`, `metadata.namespace: platform-backups`, annotation `kustomize.toolkit.fluxcd.io/prune: disabled`, and the `stringData` keys above. Generate the off-host password new (`openssl rand -base64 32`).
3. `make seal ENV=<env> NAME=backups-offsite FILE=<manifest>`, then delete the manifest.
4. Add `../../../../platform/backups/offsite` and `../../secrets/backups-offsite.sops.yaml` to `envs/<env>/flux/platform-configs/kustomization.yaml`, commit, and push.
5. After Flux applies it: `kubectl -n platform-backups create job --from=cronjob/restic-offsite restic-offsite-first`, wait for it to complete, and read its log: one `PASS` line per repository. `restic snapshots` against the target lists each repository.

Keep-safe: the off-host restic password goes on the owner's offline keep-safe list (`envs/<env>/secrets/README.md`). The sealed copy needs the age key, so a lost controller and a lost age key otherwise lose the off-host copy too.

#### Restore from the off-host copy into the drill namespace

Owner-run. It copies one off-host repository back to the local drill repository `ns-platform-drill/pvc-drill`, then restores it with `make restore`. Run it only when `make drill` is not running and `platform-drill` does not exist.

1. Create namespace `platform-drill` and PVC `drill` in it, annotated `backup: paused`, with the size and storage class of the PVC being restored.
2. Apply this Job with `<image>` from CronJob `restic-offsite`, `<backup_path>` from the env, and `<source>` the repository path such as `ns-<namespace>/pvc-<pvc>`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: restic-offsite-restore
  namespace: platform-backups
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - {key: node-role.jarvis.io/backup, operator: In, values: ["true"]}
      tolerations:
        - {key: node-role.jarvis.io/dedicated, operator: Equal, value: "true", effect: NoSchedule}
      containers:
        - name: restic
          image: <image>
          securityContext: {runAsUser: 0}
          envFrom:
            - secretRef: {name: backups-offsite}
          env:
            - name: RESTIC_FROM_PASSWORD
              valueFrom: {secretKeyRef: {name: backups-offsite, key: RESTIC_PASSWORD}}
            - name: RESTIC_PASSWORD
              valueFrom: {secretKeyRef: {name: backups-restic, key: RESTIC_PASSWORD}}
            - {name: SOURCE, value: <source>}
          command:
            - /bin/sh
            - -c
            - |
              dest=/repositories/ns-platform-drill/pvc-drill
              if restic --repo "$dest" init --from-repo "$RESTIC_REPOSITORY$SOURCE" --copy-chunker-params >/dev/null 2>&1 &&
                restic --repo "$dest" copy --from-repo "$RESTIC_REPOSITORY$SOURCE" >/dev/null 2>&1; then
                echo "PASS $SOURCE"
              else
                echo "FAIL $SOURCE"; exit 1
              fi
          volumeMounts:
            - {name: repositories, mountPath: /repositories}
      volumes:
        - name: repositories
          hostPath: {path: <backup_path>/restic, type: Directory}
```

3. When its log says `PASS`, delete the Job and run `make restore ENV=<env> APP=platform-drill NODE=<node>`.
4. Mount PVC `drill` in a short-lived pod and compare its files with the source volume.
5. `make drill-cleanup ENV=<env>` removes the namespace and `ns-platform-drill/pvc-drill`.

How to tell it worked: `PASS <source>` from the Job, then `PASS restore platform-drill/drill snapshot <id>` and exit 0 from `make restore`, and the files match.

## Limits

- Backups live on the same host as the data unless `backup_path` is on another disk. They cover mistakes and rollback, not disk loss, unless the env turns on the off-host copy.
- The off-host copy only adds snapshots. Nothing forgets or prunes off-host, so that side grows until the owner prunes it.
- While the rest-server's data sits on the same disk as the k3s snapshot files, the etcd restic copy adds retention and dedup, not disk-failure protection.
- There is no on-demand backup. The policy does not set `trigger.manual`. VolSync ignores the schedule while `trigger.manual` is set.
- Restore uses no VolumeSnapshot, volume populator, or snapshot class.
- etcd cluster-reset is not a make target and is not part of the drill.
- An older snapshot that exists only in the restic copy has no owner-run path yet.
- App isolation is at the rest-server login: both servers run as root on one hostPath, so isolation holds as long as neither process is compromised. Core is trusted the other way: core's login on `restic` (no `--private-repos`) can delete `ns-app-*` files, though it cannot decrypt them.
- A retired app's repositories left on disk make `restic-check` fail, so `make verify`'s repository check fails until they are removed (App credentials, Retire). An app's mover still runs privileged in its own namespace. App repositories have no size cap, and nothing retires them automatically.
- An app login can forget and prune its own repositories (no `--append-only`), the same as core's.
- Kyverno's background controller can read every Secret, app credentials included, as it already reads `backups-restic`.
