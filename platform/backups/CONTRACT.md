# CONTRACT: platform-backups

Shared backup for every PVC. VolSync 0.17 with the restic mover is the backup. One CronJob copies k3s etcd snapshots into their own rest-server repository. ClusterPolicy `pvc-backup` enrolls every PVC, or the PVC is annotated `backup: none`. `ci/checks/pvc-backup` checks that this policy and its `backup: none` allow-list are present.

## Offers

- Namespace `platform-backups`.
- rest-server 0.14: one Deployment and ClusterIP Service `restic` on port 8000. No Ingress.
- Bootstrap restic credentials (Secret `backups-restic`).
- CronJob `etcd-snapshots`: a read-only copy of the k3s snapshot directory into repository `host-etcd-snapshots`.
- ClusterPolicy `pvc-backup` enrolls every PVC: one restic Secret and one VolSync `ReplicationSource` in that PVC's namespace.
- A StatefulSet volume gets one `ReplicationSource` per PVC Kubernetes creates (`<template>-<statefulset>-<ordinal>`). Git does not list the ordinals.
- Opt-out for rebuildable observability data: the unprefixed annotation `backup: none` on a PVC, or on a `volumeClaimTemplate`'s `metadata`. Allowed only under `services/observability/victoria-metrics/` and `services/observability/victoria-logs/`.

## Expects

- Flux SOPS decryption via `flux-system/sops-age`. The file in Git has no plaintext secret.
- `envs/<env>/secrets/backups-restic.sops.yaml`. Flux decrypts it.
- Target path, retention counts, `backup_schedule`, `backup_check_schedule`, and `time_zone` come from `envs/<env>/values/cluster-vars.yaml`.
- The backup-role node (`role.<dns_suffix>/backup=true`) for the rest-server hostPath.
- The control-plane node (`role.<dns_suffix>/control_plane=true`), where k3s writes the snapshot files.
- `make restore` needs CronJob `platform-backups/etcd-snapshots` deployed. `tools/restore` reads that CronJob once in `run()`, before it changes anything, and stops when the CronJob is missing or its image is not digest-pinned `restic/restic` (`restic/restic@sha256:` or `restic/restic:<version>@sha256:`, and 64 hex digits).

## Health

- The namespace exists.
- HelmRelease `flux-system/volsync` is Ready. `platform-controllers` health-checks it, so `platform-configs` applies the backup objects only after the VolSync controller is up.
- Secret `backups-restic` is present. Its value is not logged.
- Service `restic` has a ready endpoint.
- Kyverno admission is Ready.
- A PVC without `backup: none` has Secret `restic-<pvc>` and ReplicationSource `<pvc>`, and that source completes a sync.
- A PVC annotated `backup: none` has neither.
- During `make restore`, that PVC's ReplicationSource stays paused until the destination finishes. It stays paused longer while the rebuild hold exists.
- `make drill` deletes a scratch volume, restores it, and compares checksums.
- A Job from CronJob `etcd-snapshots` completes, and `restic snapshots` on `host-etcd-snapshots` lists a snapshot that holds the newest k3s snapshot file.
- A Job from CronJob `restic-check` (on `backup_check_schedule` in `time_zone`, default Wednesday 06:30, on the backup-role node) completes. It runs `restic check --no-lock --read-data-subset=<n>/12` on every repository under `backup_path/restic` (the PVC repositories and `host-etcd-snapshots`) through a read-only mount, and prints only `PASS` or `FAIL` per repository. It takes no lock, so it never blocks a backup, prune, or restore; a prune that runs at the same time can fail it, and the Job retries. `make verify` fails when its last success is older than one schedule interval plus 12 hours. With no success yet, `make verify` warns while the first run is pending or running inside the first interval plus 12 hours, and fails once a run finished without success or that time has passed. The `etcd-snapshots` check uses the same first-run rule without the 12 hours.

## Config

- VolSync 0.17, restic mover, `copyMethod: Direct`. The controller installs the VolumeSnapshot CRD and no snapshot controller.
- k3s keeps writing etcd snapshots to `backup_path/etcd-snapshots`. The k3s config does not change. Those files are the same-node rollback copy.
- rest-server data is the hostPath `backup_path/restic`, pinned to the backup-role node.
- One repository per PVC is the directory `backup_path/restic/ns-<namespace>/pvc-<pvc>`.
- A volume's repository URL is `rest:http://restic.platform-backups.svc.cluster.local:8000/ns-<namespace>/pvc-<pvc>`.
- The `ns-` and `pvc-` prefixes keep a path segment off rest-server's reserved names (`data`, `index`, `keys`, `locks`, `snapshots`, `config`). The password stays out of the URL.
- Secret `backups-restic` holds `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (the server prefix `rest:http://restic.platform-backups.svc.cluster.local:8000/`), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`.
- A volume secret sets `RESTIC_REPOSITORY` to that volume's URL and copies the password and `RESTIC_REST_*` keys. VolSync 0.17 passes those two keys into the mover.
- The process runs with htpasswd auth and without `--append-only` or `--private-repos`. The mover forgets on every backup and prunes with the same credentials.
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
- The ReplicationSource uses the restic mover, `copyMethod: Direct`, `cacheCapacity: 1Gi`, and `trigger.schedule` from `backup_schedule`.
- A mutate-existing rule sets annotation `volsync.backube/privileged-movers: "true"` on the PVC's namespace. It lands asynchronously, so the first sync may fail once and pass on retry.
- The VolSync controller pod's `TZ` is `time_zone`. The 0.17 cron parser uses the process local zone and has no timezone field. The chart has no env hook, so a HelmRelease postRenderer sets `TZ` and mounts the node zoneinfo directory.
- `platform-backups` does not set `volsync.backube/privileged-movers`. No mover in this namespace mounts a hostPath.
- The rest-server runs as root so it can write its hostPath.
- The etcd CronJob runs as root so it can read the snapshot files (mode 0600). It is not a mover.
- CronJob `etcd-snapshots` is pinned to `role.<dns_suffix>/control_plane=true`.
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
- `site.yml` creates ConfigMap `kube-system/backup-rebuild-hold` only when FluxInstance `flux` is absent and the inventory `backup` host already has restic repositories under `backup_path/restic/ns-*/pvc-*`. Any other lookup error fails the play.
- The ConfigMap's `data.repos` is that list. While it exists, new ReplicationSources are generated paused, so a cluster rebuilt on a disk that still has restic data cannot snapshot empty volumes over the last good backup.
- A later `site.yml`, once the FluxInstance exists, does not create or change the hold.
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
- `ci/tests/test_drill.py` and `make drill ENV=<env>` (real restore target, checksum comparisons, etcd-copy check, and in-cluster cleanup).

## Operations

Do not merge a change to ClusterPolicy `pvc-backup` during a restore. The policy is annotated `kustomize.toolkit.fluxcd.io/force: Enabled`, so Flux recreates it and Kyverno regenerates every Secret and ReplicationSource.

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

How to tell it worked: a `PASS restore <namespace>/<pvc>` line, then `SUMMARY restored=... accepted=... retired=...`, and exit 0.

### Restore every enrolled PVC

```bash
make restore-all ENV=<env>
```

Optional `RETIRE=ns-<namespace>/pvc-<name>[,...]`, `ACCEPT_EMPTY=<namespace>/<pvc>[,...]`, and `NODE=<node>`. One `NODE` pins every unbound PVC in the run to that single node.

`restore-all` selects every PVC that is not `backup: none` and not labeled `app.kubernetes.io/created-by: volsync`, including one that is not bound yet. It reads the etcd CronJob image once, before any change, the same way `make restore` does.

What it changes: the same per-PVC restore as above. `No eligible snapshots found` fails unless `ACCEPT_EMPTY` names that exact `namespace/pvc`. This acceptance is separate from `RETIRE`.

`RETIRE` is only for a held repository that no longer has a PVC, such as an app removed from Git. `ACCEPT_EMPTY` is only for selected PVCs whose empty state the owner accepts. `--retire` and `--accept-empty` are only valid with `restore-all`.

`make restore-all` deletes the hold only after every restore has succeeded or was explicitly accepted empty and every listed repository has a PVC or is named in `RETIRE`. It then removes `backup: paused`. Sources cannot report unpaused while the hold exists, so restore deletes the hold before removing those annotations and checking every source. If resume or that check fails, it re-adds the annotations and restores the hold. If rollback itself fails, it prints the exact environment-bound recovery commands and exits nonzero. A listed repository that is neither restored, accepted empty, nor retired keeps the hold, and the restored PVCs stay `backup: paused`.

How to tell it worked: `HOLD lifted` when a hold was set, `SUMMARY` with restored, accepted-empty, and retired counts, and exit 0.

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

k3s secrets-encryption is on, so restoring onto a rebuilt node also needs the original k3s server token. Same-node restore is the scope. The drill only checks that the newest restic copy matches the k3s file's sha256.

### Rotate the restic password

Every repository uses the one password in Secret `platform-backups/backups-restic`. Kyverno copies it into each `restic-<pvc>` Secret (`synchronize: true` on an apiCall). CronJob `etcd-snapshots` reads that same Secret and writes repository `host-etcd-snapshots`. Changing the Secret changes every repository at once.

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

## Limits

- Backups live on the same host as the data unless `backup_path` is on another disk. They cover mistakes and rollback, not disk loss.
- While the rest-server's data sits on the same disk as the k3s snapshot files, the etcd restic copy adds retention and dedup, not disk-failure protection.
- There is no on-demand backup. The policy does not set `trigger.manual`. VolSync ignores the schedule while `trigger.manual` is set.
- Restore uses no VolumeSnapshot, volume populator, or snapshot class.
- etcd cluster-reset is not a make target and is not part of the drill.
- An older snapshot that exists only in the restic copy has no owner-run path yet.
