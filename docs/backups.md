# Backups: runbooks

What backs up and how: [core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md). Every command runs from the instance repository with `~/.kube/jarvis-<env>.yaml`, and checks the repository's and the cluster's env markers before it changes anything. Never print a Secret value.

Do not merge a change to the `pvc-backup` policies during a restore: Flux recreates them (`force: Enabled`), and Kyverno regenerates every Secret and ReplicationSource they own.

## Restore one namespace

```bash
jarvis restore --env <env> --namespace <namespace> [--as-of <RFC3339>]
```

The default `--as-of` is one second before the PVC's `creationTimestamp`, right for a claim that was deleted and recreated; a volume that was not recreated needs `--as-of`. For each PVC, restore suspends its Flux Kustomization, CronJobs and writers, annotates `backup: paused` and waits until nothing mounts it, binds an unbound PVC with a short-lived pod placed like its workload (`NODE=<node>` to pin), applies a Direct ReplicationDestination, and fails on `No eligible snapshots found` or an empty mover log. On success it undoes every step, except the pause while the rebuild hold exists. On failure it leaves the quiesced state and prints one command per undo step.

Done: `PASS restore <namespace>/<pvc> snapshot <id>`, `SUMMARY …`, exit 0.

## Restore every enrolled PVC

```bash
jarvis restore --all --env <env> [--retire ns-<namespace>/pvc-<name>,…] [--accept-empty <namespace>/<pvc>,…] [--node <node>]
```

Selects every PVC not `backup: none` and not created by VolSync. `--accept-empty` accepts an empty result for that exact PVC; `--retire` drops a held repository that has no PVC any more. The hold is deleted only when every listed repository is restored, accepted empty or retired; then the pauses are lifted, and put back if that fails.

Done: `HOLD lifted` (when there was one), `SUMMARY …`, exit 0. A fresh env prints `nothing to restore: no rebuild hold and no PVC`.

## Drill

```bash
jarvis drill --env <env>
```

Writes a scratch payload in `platform-drill`, waits for a fresh sync, deletes and restores the volume with `jarvis restore`, and compares checksums. It also restores the newest file from `host-etcd-snapshots` and compares it with the k3s file. It refuses a rebuild hold or drill leftovers, and always cleans up; if cleanup cannot finish, run `jarvis drill --cleanup-only --env <env>`.

Done: `PASS drill cleanup complete`, exit 0. `newest etcd snapshot <file> is not yet in the restic copy` means run it again after the next `etcd-snapshots` run.

## etcd cluster reset

Owner-run, as root on the server node, never as a drill on the live env. It uses a k3s snapshot file, not restic.

1. `ls -l <backup_path>/etcd-snapshots` and choose a file.
2. `systemctl stop k3s`, then once `/usr/local/bin/k3s server --cluster-reset --cluster-reset-restore-path=<backup_path>/etcd-snapshots/<file>`, then `systemctl start k3s`.

k3s secrets-encryption is on, so a rebuilt node also needs the original server token (next section).

## Rebuild a dead node

`envs/<env>/secrets/k3s-token.sops.yaml` must hold this cluster's server token: k3s encrypts its datastore and snapshots with it. `jarvis secrets` mints tokens only for a new env. For a cluster that already exists, seal it before any loss: a mode `0600` Secret manifest with `stringData.server_token` (the passphrase after `::server:` in `/var/lib/rancher/k3s/server/token`, not the full `K10…` form, which pins the old CA) and `stringData.agent_token`, then `jarvis encrypt --env <env> --name k3s-token <manifest>`.

**Server.**

1. Reinstall the node, mount the surviving data disk at `/var/lib/jarvis`, and run `jarvis node prep` ([deploy.md](deploy.md)). If the disk is lost too, prepare a new one and `restic restore` a file from `host-etcd-snapshots` into `<backup_path>/etcd-snapshots` first.
2. `jarvis site --env <env>`. With restic repositories on the backup host and no root GitRepository yet, it creates the rebuild hold.
3. Once Flux has reconciled: `jarvis restore --all --env <env>`.

**Rebuild in place (OS kept).** As root on a single node whose backups share the OS disk:

1. Record `restic snapshots` counts for each local repository (and the off-host copy).
2. The k3s role installs no uninstall script, so fetch `install.sh` at the pinned tag (`https://raw.githubusercontent.com/k3s-io/k3s/<k3s_version>/install.sh`, `+` as `%2B`) and run `INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true INSTALL_K3S_BIN_DIR=/usr/local/bin sh install.sh`.
3. `/usr/local/bin/k3s-uninstall.sh`. It removes k3s's own directories and nothing under `/var/lib/jarvis`.
4. Check `<backup_path>/restic` and `<backup_path>/etcd-snapshots` are intact and the counts match step 1.
5. `jarvis site --env <env>`, then `jarvis restore --all --env <env>`.

If the rebuild ships with a change to Flux sources or paths, suspend every Kustomization before it merges. If the CA was rotated, trust the new `ca.crt` on clients.

**Agents.** A reinstalled agent with the same hostname is refused by its stored node password: `kubectl delete node <host>` first, then `jarvis site --env <env> --limit <host>`.

Done: `jarvis site` and `jarvis restore --all` exit 0, then `jarvis verify --env <env>` passes.

## Rotate the restic password

Core repositories only: skip every `ns-app-*` directory and every PVC in an `app-` namespace, and skip the drill repository `ns-platform-drill/pvc-drill`. `jarvis secrets` never replaces an existing `backups-restic` file; `--rotate-restic` only mints a new one for a lost file and adds it to no repository. Keep the old key until the last step. Run `restic` and `find` as root on the backup node; password files are mode `0600`.

1. Save both passwords, list the repositories (each directory holding `config`), and add the new key to each, recording its current key id:

   ```bash
   umask 077
   KUBECONFIG=~/.kube/jarvis-<env>.yaml kubectl get secret -n platform-backups backups-restic -o jsonpath='{.data.RESTIC_PASSWORD}' | base64 -d > old-password
   test -s old-password
   openssl rand -base64 32 | tr -d '\n' > new-password
   find <backup_path>/restic -type f -name config -printf '%h\n'
   RESTIC_PASSWORD_FILE=old-password restic -r <repo> key list
   RESTIC_PASSWORD_FILE=old-password restic -r <repo> key add --new-password-file new-password
   ```

2. Seal the new password, then commit and push:

   ```bash
   python3 -c 'import json, pathlib, sys; pathlib.Path(sys.argv[2]).write_text(json.dumps(pathlib.Path(sys.argv[1]).read_text()))' new-password new-password.json
   sops set --value-file envs/<env>/secrets/backups-restic.sops.yaml '["stringData"]["RESTIC_PASSWORD"]' new-password.json
   ```

3. Check that `backups-restic` and every `restic-<pvc>` Secret now hold it, comparing hashes only:

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

   A hash still old: `kubectl annotate pvc -n <namespace> <name> restic-password-generation=<token> --overwrite` on each enrolled PVC and check again. Never edit a generated Secret.

4. Prove the new password on existing repositories: run `kubectl -n platform-backups create job --from=cronjob/etcd-snapshots restic-password-rotation` and wait for it to complete, and wait for one ReplicationSource's `status.lastSyncTime` to move past the hash check (one more if a sync was already running). Then `RESTIC_PASSWORD_FILE=new-password restic -r <backup_path>/restic/ns-<namespace>/pvc-<pvc> cat config`. `jarvis verify` is not this proof.

5. List the repositories again and open each with the new password; stop on any failure, add the key there, and repeat:

   ```bash
   find <backup_path>/restic -type f -name config -printf '%h\n' > repos.txt
   while IFS= read -r repo; do
     case "$repo" in */ns-platform-drill/pvc-drill) continue ;; esac
     RESTIC_PASSWORD_FILE=new-password restic -r "$repo" cat config >/dev/null || exit 1
   done < repos.txt
   ```

   Then `restic key remove <old-id>` with the id step 1 recorded. A repository not on step 1's list with a single key was created after the change: leave it. More than one key and no recorded id: stop. Delete `old-password`, `new-password`, `new-password.json` and `repos.txt`.

## App credentials

An app's backups are on unless its entry in `values/apps.yaml` sets `backups: false`. With them on, an app whose file keeps a `backup: true` volume has its own credential: `jarvis validate` requires it, the render registers it, and app-hosting's apps server (`restic-apps`) serves it ([app-hosting](../core/feature/app-hosting/CONTRACT.md)). Schedule and retention are the env's. With them off, nothing is sealed and `jarvis verify` does not check the app's volumes.

**Add an app.**

1. `jarvis secrets --env <env>` seals each credential an app needs and lacks. A second run changes nothing; a recorded file that has gone missing is refused, since its repositories open only with the first password.
2. `jarvis render --env <env>` registers it. Commit and push.
3. `restic-apps` restarts with the new user. A PVC that existed before: `kubectl annotate pvc -n app-<name> <pvc> backup-credential=registered --overwrite`.
4. `jarvis verify --env <env>` passes; the next `restic-check` prints `PASS ns-app-<name>/pvc-<pvc>`.

**Retire an app.**

1. Take it out of `values/apps.yaml` (or set its `backups: false`) and delete `secrets/app-<name>/` (`jarvis validate` fails a sealed file nothing reads), re-render, commit and push. Then `kubectl -n platform-backups delete secret backups-restic-app-<name>` (Flux does not prune it). Git history keeps the credential: restore the file before listing the name again, and its repositories open again.
2. Once the data is not needed, remove `<backup_path>/restic/ns-app-<name>` as root on the backup node; until then `restic-check` and `restic-offsite` print `FAIL … no backup credential registered for its app`.

## Off-host copy

**Turn it on** (needs the age public key only):

1. Set up the target: an empty bucket, or a rest-server only this env writes, run without `--private-repos` from a binary its own service owns (not the jarvis CLI's tool copy, whose path moves with each release).
2. Write a mode `0600` manifest outside the repo: Secret `platform-backups/backups-offsite`, annotation `kustomize.toolkit.fluxcd.io/prune: disabled`, `stringData` `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` (new: `openssl rand -base64 32`), and the backend's login.
3. `jarvis encrypt --env <env> --name backups-offsite <manifest>`, then delete the manifest.
4. List `offsite-backup-copy` under `features`, `jarvis render --env <env>`, commit and push.
5. `kubectl -n platform-backups create job --from=cronjob/restic-offsite restic-offsite-first`; its log has one `PASS` per repository. A target that does not answer fails every repository at once with `off-host target not reachable`.

Put the off-host restic password on the offline keep-safe list ([secrets.md](secrets.md)).

**Restore from it into the drill namespace**, only while no drill runs:

1. Create namespace `platform-drill` and PVC `drill`, annotated `backup: paused`, sized like the source.
2. Apply this Job, with `<image>` from CronJob `restic-offsite` and `<source>` such as `ns-<namespace>/pvc-<pvc>`:

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

3. On `PASS`, delete the Job and run `jarvis restore --env <env> --namespace platform-drill --node <node>`.
4. Compare PVC `drill`'s files with the source, then `jarvis drill --cleanup-only --env <env>`.
