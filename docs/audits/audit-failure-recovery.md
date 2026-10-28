# P0a failure and recovery

Read of `main` at `1ccf51c71ebe4bad7c91730737060793ca6bb707`. Code and in-repo docs only. No playbook run, no live cluster, no age-key or decrypted-secret access.

The live env inventory sets no `data_disk`. k3s, Flux, and the age key are the paths that leave the owner stuck. A wipe-and-rebuild still works when the age private key is on the controller. Etcd snapshot files are written and nothing in this repo restores them (P0b).

## Findings

### 1. A node address change wedges embedded etcd, and a re-run of site.yml stays wedged

**Where:** `infra/ansible/roles/k3s/templates/config.yaml.j2:3`, `infra/ansible/roles/k3s/templates/k3s.service.j2:23-25`, `tools/prep-node:618-620`

**What:** The server config sets `cluster-init: true` and does not set `node-ip`. That selects embedded etcd and lets k3s record the address on the default route as the member URL. After that address changes, upstream k3s exits with "not a member of the etcd cluster" and names the old URL and the new one. The unit's `Restart=always` starts the same process again. The k3s role's start task fails the same way on every later `site.yml` run. The kubeconfig role points `server` at the inventory hostname, and `tls-san` is that hostname, so DNS is a separate problem: kubectl follows the name once the process is up. The only prevention in the repo is `prep-node`'s netplan pin, and declining the prompt leaves the interface on DHCP. Plaintext addresses are not allowed in the env, so git cannot pin the address either.

**Fix:** In `infra/ansible/README.md`, next to the k3s tag: when the k3s log says the member URL does not match, stop the unit and run `/usr/local/bin/k3s server --cluster-reset` once (the upstream flag "Forget all peers and become sole member of a new cluster", with no `--cluster-reset-restore-path`), then start the unit. Say the prep-node prompt is what keeps the address stable, and that a later `site.yml` run will not clear this.

**Why:** A DHCP lease change, or a move to another network, takes the API down. The playbook the env README tells the owner to re-run fails at "start k3s" and leaves the crash loop in place. Wipe-and-rebuild is the only path written down today, and it throws away the datastore. P0a has no restore for the etcd snapshots it already writes.

**Impact:** high

### 2. Reverting main does not repair Flux once the FluxInstance or the operator Deployment exists

**Where:** `infra/ansible/roles/flux-operator/tasks/instance.yml:17-40`, `infra/ansible/roles/flux-operator/tasks/operator.yml:18-47`, `envs/env-01/README.md:16`, `tools/verify/p0a:206`, `docs/code-delivery/CONTRACT.md:60`

**What:** Child Kustomizations set `retryInterval: 1m`. A failed build keeps `lastAppliedRevision` and retries, so a revert on `main` applies while `kustomize-controller` is up. The Flux Operator chart's HelmRelease upgrade remediation is `retries: 3` with the default strategy `rollback`, so a failed chart upgrade returns to the previous release and then waits for the git spec to change. The FluxInstance and the operator Deployment are different. Ansible applies each once, when `kubectl get` shows the object, and a later `site.yml` (including `--tags flux-operator`) skips both, including the CRD and rollout waits. A FluxInstance spec that kubeconform accepts and the operator then applies (a bad distribution version, or the kustomize-controller patch) can take those controllers down. The revert sits in git. The same skip covers a first bootstrap killed after Helm created the Deployment and before it was Ready: the re-run never calls Helm again. The env README tells the admin to re-run `site.yml` after a merge. `make verify` asks for a rollback-drill log. No doc says the steps, or what to do when the controllers are the broken part. Nothing in the tree is an Alert or a Provider, so a Not Ready Kustomization is visible only if someone looks.

**Fix:** In `infra/ansible/README.md`, state the two recoveries. While the controllers are up, revert the commit and let the next reconcile apply it. When they are down, from a checkout of the reverted commit run `k3s kubectl apply -f` on `envs/<env>/flux/flux-instance.yaml` on the node (the on-node kubeconfig, not the runner's copy). When the operator Deployment exists and is not usable and helm-controller is not up, delete that Deployment and re-run `--tags flux-operator` so the one-time Helm install runs again. Point the verify checklist's rollback line at those steps.

**Why:** A bad FluxInstance is the case the rollback drill has to survive, and the apply-once tasks make the usual re-run a no-op. The owner either wipes the node or invents the `kubectl apply`. A killed first install leaves the same no-op with Flux never started.

**Impact:** high

### 3. A missing age key file is a green site.yml run, and the mismatch error points at deleting the only remaining copy

**Where:** `infra/ansible/roles/flux-operator/tasks/sops_age.yml:35-44`, `infra/ansible/roles/flux-operator/tasks/sops_age.yml:82-94`, `tools/prep-node:958-968`, `infra/ansible/README.md:27`, `envs/env-01/secrets/README.md:15`

**What:** `flux-system/sops-age` is the copy Flux decrypts with. It is marked `reconcile: disabled`, so git will not recreate it. When the secret exists and the key file does not, `site.yml` exits 0 and leaves the secret alone. The README says that and stops there. The secret's `age.agekey` field is then the only copy. The compare task, when a file is present and differs, tells the owner to remove the secret or point at the original key. Removing it deletes the copy that can decrypt the committed files. `bootstrap-secrets` skips any `*.sops.yaml` that already exists, and encryption uses the public recipient, so a new key cannot read the old files. `prep-node` offers to retarget `.sops.yaml` and delete those files only when the public keys differ. Declining that prompt leaves the new key file in place. The next `site.yml` installs that key into the secret when the secret is absent, and refuses to replace it when the secret is still the old key. `platform-configs` decrypts as a whole, so one bad key stops that Kustomization, including the non-secret objects in it.

**Fix:** In the secrets README and the ansible README, write the two paths. While the API is up and the key file is gone, copy the `age.agekey` field of `flux-system/sops-age` back to the `age_key_file` path and set mode `0600`, before deleting anything. When both copies are gone, the old values are gone with them: create a new key, accept the prep-node prompt (or delete that env's `*.sops.yaml` and update the recipient), run `make bootstrap-secrets`, delete the cluster secret if it is still there, re-run `site.yml`, and commit. Say that declining the prompt leaves a key that cannot decrypt the files still in git.

**Why:** The controller path in cluster-vars is the file `prep-node` creates, and the script's only backup hint is a line printed at creation time. A deleted file, or a wiped controller that held the only file, has no path back to the CA, the restic password, or the API keys. Following the current fail message makes that permanent. A full rebuild with the file still on the controller is the existing prep-node, bootstrap-secrets, `site.yml` sequence. A rebuild without the file works only through the new-key path above, and the previous secret values are not recoverable in P0a.

**Impact:** high

### 4. A netplan try that times out still leaves the static file for the next boot

**Where:** `tools/prep-node:583-594`, `tools/prep-node:621-632`

**What:** On a DHCP interface the script renames the other netplan YAML to `.bak`, writes `/etc/netplan/99-jarvis-static.yaml`, then runs `netplan try --timeout 120` with no `--config-file`. Current netplan snapshots `/etc/netplan` only when `--config-file` is set, treats a timeout as a reject, reverts the runtime network, and exits 0. This script restores the `.bak` files only when that exit code is non-zero. It then checks that the same address is still configured. After a timeout the address is still there because DHCP is back, so the script reports the static config kept. The unconfirmed YAML is what the next boot applies. Ctrl-C during the prompt does not hit the restore either: the local SSH dies and the `.bak` files stay renamed.

**Fix:** Treat try as success only when its output contains `Configuration accepted.` Otherwise run the existing restore. Pass the new file as `netplan try --config-file` so netplan's own revert drops it, and write it into `/etc/netplan` only after that accept.

**Why:** The pin is the thing that prevents finding 1. Missing the 120 second confirm, which is easy in a script that already asked several questions, keeps a static config the owner did not accept. A wrong gateway then drops SSH at the next boot. Recovery is the console or a reinstall. The README describes the pin and the restore, and the restore does not run on this path.

**Impact:** medium

### 5. A Helm Renovate PR bumps the version and leaves the checksum

**Where:** `infra/ansible/roles/flux-operator/defaults/main.yml:11-17`, `infra/ansible/roles/flux-operator/tasks/helm_cli.yml:18-24`, `renovate.json:36-47`

**What:** The custom manager captures `helm_version` and the Flux Operator chart tag. It does not capture the Helm tarball sha256 lines. `automerge` is false. Merging the Helm PR as opened makes the next download fail its checksum. The role installs Helm only when `/usr/local/bin/helm` is absent, with no version check, so the node that already has the binary keeps the old CLI and looks fine. The failure shows up on a new machine or a rebuild, at `flux-operator`, before the operator exists. The chart pin is in better shape: a failed HelmRelease upgrade rolls back (finding 2) and a revert on `main` is a new spec the controller will try. `k3s` is disabled in Renovate. Python pins in `requirements-dev.txt` go through CI before merge.

**Fix:** Add one sentence to the `flux-operator` row in `infra/ansible/README.md`: a Renovate diff that changes `helm_version` is incomplete until the sha256 lines in the role defaults change with it, and an existing Helm binary is left in place. A check that rejects a `helm_version` whose recorded sha256 does not match the upstream tarball would stop the merge; the README line is what the owner needs either way.

**Why:** The only warning is a comment in the defaults file, which the Renovate diff does not force anyone to edit. The live cluster stays up. The next full rebuild stops while installing the operator, which is the step a wiped node cannot skip.

**Impact:** medium

### 6. An unsynced clock is allowed through prep, and Flux then looks like GitHub is down for good

**Where:** `tools/prep-node:731-733`, `infra/ansible/roles/base-os/templates/chrony.conf.j2:4-14`, `infra/ansible/roles/base-os/tasks/main.yml:59-67`

**What:** `prep-node` reads `NTPSynchronized` once. When the value is not `yes` it prints a warning and asks to continue, defaulting to yes. `base-os` then installs chrony with only `pool … nts` lines, `makestep 1.0 3`, and starts the service. It never checks that a source was reached. If outbound 4460/tcp to the pools in `ntp_pools` does not work, chrony has no other source and the clock stays where it is. k3s still starts and issues certificates against that clock. source-controller keeps retrying GitHub, TLS fails, and the last artifact stays forever. Bringing GitHub back does not move it. The chrony template comment says 123/udp and 4460/tcp must stay open, and no operator doc says to look at `timedatectl` when Flux is stuck on the git source.

**Fix:** Default the prep-node clock prompt to no. Add one sentence to the ansible README: Flux stuck fetching git, with the route otherwise fine, is `timedatectl`; chrony speaks NTS only, to the pools in cluster-vars.

**Why:** A new install often has a wrong clock until the first sync. Enter at the prompt, or a network that blocks NTS, leaves a cluster that matches the "GitHub was unreachable" case and then never recovers when the network is fine. The owner has no written next step.

**Impact:** medium

## Scenarios

1. **Reboot or power loss.** On an env with no `data_disk`, the k3s unit is `WantedBy=multi-user.target`, `After=network-online.target`, `Restart=always`, and `TimeoutStartSec=300`. Etcd data stays on the root disk under `/var/lib/rancher/k3s`. The process and the Flux controllers come back and reconcile. A declared data disk is the appendix item.
2. **site.yml interrupted, then re-run.** base-os, a disk that already carries `jarvis-data`, and k3s are safe to re-run: the env marker is written before the binary, so the foreign-k3s guard does not trip. Flux is finding 2. A kill during the k3s handler flush is the appendix item.
3. **prep-node or bootstrap-secrets interrupted, then re-run.** `bootstrap-secrets` writes each file with a temp-file replace and skips paths that exist, so a re-run creates only the missing files. `prep-node` stops at the first error and can be run again; packages, keys, and sudo are idempotent. The dangerous parts are findings 3 and 4.
4. **Bad commit on main.** A broken build of the flux entry fails `tools/tests/test_flux_entry.py` before merge. An object that builds and still fails on the cluster stays unapplied while the controllers are up; revert the commit. Finding 2 is the case where that revert does nothing.
5. **GitHub unreachable, then back.** The FluxInstance sync has no pull secret (public HTTPS). source-controller keeps the last artifact and retries. The next successful fetch catches `main` up. The manifest sets no interval; the operator default applies. This does not describe a wrong clock (finding 6).
6. **sops-age deleted, or the key file lost.** Secret gone and the key file present: re-run `site.yml` and the create task writes it again. Anything else is finding 3.
7. **Address change, or a lost or wrong runner kubeconfig.** The address change is finding 1. `--tags kubeconfig` fetches `/etc/rancher/k3s/k3s.yaml` and replaces a `https://127.0.0.1:6443` server line with the inventory hostname, overwriting the runner's `~/.kube/config` (Ansible keeps a backup copy beside it). Flux tasks call `k3s kubectl` on the node and ignore that file.
8. **Disk full, clock, certificates.** Clock is finding 6. Disk full and the runner kubeconfig's certificate lifetime are appendix items. k3s renews certificates on the node when it starts and they are inside 90 days of their 365-day life.
9. **Bad Renovate merge.** The Helm CLI pin is finding 5. A bad Flux Operator chart upgrade rolls back and waits for a new git spec. A bad Python pin has to pass CI with `automerge` off.
10. **Node wiped.** With the age key file still on the controller: `prep-node`, `make bootstrap-secrets` (no change when the files are already in git), commit if prep-node edited the env, then `site.yml`. The hostname still has to resolve first. Without the key file, finding 3, and the previous secret values are gone. The new cluster does not pick up the old etcd data. Snapshots under `backup_path` have no restore task here.

## Appendix

- Data disk: `ansible.posix.mount` uses `defaults,nofail` (`infra/ansible/roles/node-disk/tasks/main.yml:78`) and the k3s drop-in is `RequiresMountsFor` on `local_path_data` and `backup_path` (`infra/ansible/roles/node-disk/templates/k3s-mount.conf.j2:4`). The OS boots when the disk is missing; k3s stays down until that mount is started, and nothing retries the mount. The handler only reloads systemd.
- A `jarvis-data` label makes `node-disk` mount and skip format. A half-written filesystem with that label is not repaired by a re-run.
- Runner kubeconfig copies expire. k3s refreshes the on-node file when it starts; the runner's copy moves only when the kubeconfig tag runs again. The tag table does not say so.
- Disk fill: journald `SystemMaxUse=2G`, kubelet `nodefs.available<10%`, and five etcd snapshots on `backup_path` with no size cap. With no `data_disk` those snapshots share the root disk. Free space and start k3s. No written cleanup.
- A k3s play killed after a template change and before handlers leaves the new files unactivated when the next run reports no change. Restart the unit, or reboot.
- Kustomizations set neither `wait` nor `healthChecks`, so a Deployment that applies and then crash-loops still shows Ready.
- `prep-node` writes `.sops.yaml` and then deletes `*.sops.yaml`. A kill between those two lines leaves files encrypted to the old recipient. The next run sees a matching recipient and keeps them. Delete the files and run `bootstrap-secrets`.
- P0a writes etcd snapshots (`k3s_etcd_snapshot_schedule_cron`, retention 5) and has no restore command. A corrupt datastore has no path in this repo.

## Unsure

- I did not run `k3s server --cluster-reset` on the pinned `k3s_version`. The flag text is "Forget all peers and become sole member of a new cluster". The "keeps the datastore when no snapshot path is given" part is the upstream maintainer's account of this failure, not something this repo demonstrates.
- netplan's timeout exit code and its decision to skip the `/etc/netplan` snapshot are from current upstream `netplan try`. The Ubuntu package on a given node may differ. The script's own success test is still exit code 0 plus the same address.
- The FluxInstance manifest sets no sync interval. I did not confirm the operator's default. Retry-and-keep-the-last-artifact is the source-controller behavior either way.
- A Flux Operator chart upgrade that Helm marks deployed, and that then breaks admission, might stop helm-controller from applying the revert. The remediation block covers a failed upgrade.
- No node was contacted. The mount drop-in is absent on an env whose inventory sets no `data_disk`.
