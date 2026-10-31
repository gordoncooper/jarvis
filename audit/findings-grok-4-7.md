# Whole-repo audit findings (Grok 4.7)

SHA `5561280d2c57e0b7d0d7d1a8c2eaa1a95cf9bdd8` (215 tracked files). No code was changed. `OWNER_NAME="Gordon Cooper" make ci-verify` passed: the ten checks in `ci/rule-checks.txt`, then 201 pytest tests.

This is an audit of the foundation (node setup, k3s, Flux, backups, certificates, DNS, secrets, and the agent guardrails). Services and apps are still placeholders.

## 1. Big picture

The foundation is real and mostly fail-closed. Env identity stays under `envs/`, core manifests take `${…}` from one rendered ConfigMap, SOPS files are ciphertext, the backup policy is one Kyverno generate path, and the CI checks actually fail on seeded bad values. The disk wipe has two gates. Flux bootstrap is one-shot and then Git owns the cluster. That shape should be kept.

What is fragile is the operator edge and the promise that a second machine, a second env, or a lost disk still fits the same design. `prep-node` finishes by running a checker that is not in the tree, after it has already changed the node and the checkout. `restore` will delete files on a PVC against whatever kubeconfig is current, and a failure or interrupt leaves workloads scaled to zero. Enrollment of backups fails open at admission time. The only non-example env stores restic on the same root disk as k3s, and an etcd restore onto a new disk still needs the server token, which is not in Git.

Three structural themes show up in more than one layer:

1. **A value that looks configurable often has a second, hidden pin.** `backup_path` and `local_path_data` are env keys, but the data disk always mounts at `/var/lib/jarvis`. The etcd snapshot cron and its retention live in an Ansible default, while the restic copy job is a literal kept in step by a test. `kubectl` is pinned in `tools/versions.env` and k3s is pinned per env, with no check that the minors match. `cluster.local` is baked into the FluxInstance and into the restic URL at secret-creation time.
2. **The guardrails scan Git text, and they stop at the chart boundary.** Digest pin and manifest lint do not treat a HelmChartConfig image or a missing Helm probe as a violation. Nothing checks that every `${…}` in a manifest is a cluster-vars key, which the schema contract requires. PVC backup CI checks the policy and the opt-out list, not that a live PVC was enrolled.
3. **The landing seam for services is a placeholder with no pipe.** `hermes.yaml` and `apps.yaml` are validated and then unused by the cluster. Bootstrap already writes Grafana and Hermes secrets that no Flux kustomization applies. `services/` and `apps/` are each one Flux Kustomization with `prune: true`. The first real service inherits that boundary, and the contracts already talk about Gatus, an admin gate, and a second code host as if they were present.

An adopter with a full copy can see the Ansible order and the env schema. They cannot see a single path from clone to a running cluster, because the root README points at a plan repo outside this tree, and the only complete env is the one the new-env note says not to copy.

## 2. Ranked findings

Correctness and safety, then brittle, then rigid, then hard to follow, then size.

| ID | Rank | Type | Confidence | Location | One line |
| --- | --- | --- | --- | --- | --- |
| F1 | 1 | correctness | high | `tools/prep-node:1002` | Final gate runs `tools/checks/env-schema`, which is not in the repo |
| F2 | 2 | safety | high | `tools/restore:175` | Restore deletes files on the current kubeconfig and leaves the cluster quiesced |
| F3 | 3 | safety | high | `platform/backups/enrollment.yaml:9` | Backup admission fails open, and no verify pass checks that a PVC was enrolled |
| F4 | 4 | safety | high | `infra/ansible/roles/node-disk/vars/main.yml:2` | Data disk always mounts at `/var/lib/jarvis`, regardless of the env paths |
| F5 | 5 | safety | high | `envs/env-01/inventory/hosts.yml:3` | The shipped env's backups sit on the root disk; rebuild still needs the node token |
| F6 | 6 | safety | medium | `envs/env-01/secrets/README.md:27` | Replacing the CA secret does not reissue the wildcard |
| F7 | 7 | safety | high | `infra/ansible/roles/node-disk/tasks/main.yml:51` | `wipefs -a` runs when the documented extra checks are skipped |
| F8 | 8 | brittle | high | `schema/CONTRACT.md:23` | No CI check enforces "every `${…}` is a cluster-vars key" |
| F9 | 9 | rigid | high | `infra/ansible/site.yml:14` | Exactly one control plane, always `cluster-init: true` |
| F10 | 10 | rigid | high | `infra/ansible/roles/k3s/tasks/main.yml:28` | k3s install is amd64 only |
| F11 | 11 | brittle | high | `ci/checks/lib/_digest_pin.py:6` | Chart-shaped workloads skip digest pin and probe requirements |
| F12 | 12 | brittle | high | `envs/env-01/flux/kustomizations/platform-controllers.yaml:12` | platform-configs does not wait until Helm or admission is Ready |
| F13 | 13 | rigid | high | `README.md:5` | A fresh copy has no path to a first deploy on different hardware |
| F14 | 14 | rigid | high | `schema/env.py:557` | Hermes and app values, and their secrets, never enter the cluster |
| F15 | 15 | brittle | medium | `infra/ansible/roles/kubeconfig/tasks/main.yml:43` | Kubeconfig always replaces `~/.kube/config` and only rewrites `127.0.0.1` |
| F16 | 16 | brittle | high | `tools/restore:409` | Quiesce scales only Deployments and StatefulSets; tests never see a workload |
| F17 | 17 | complexity | high | `docs/code-delivery/CONTRACT.md:17` | Contracts describe checks and products the tree does not contain |
| F18 | 18 | brittle | medium | `tools/versions.env:33` | Kubernetes minor, etcd cadence, and cluster DNS domain are split pins |
| F19 | 19 | complexity | high | `ci/checks/lib/_manifests.py:9` | Manifest scan roots are copied in a second module |
| F20 | 20 | brittle | high | `ci/ci-local-verify:10` | The documented pre-push command fails until `OWNER_NAME` is exported |
| F21 | 21 | rigid | medium | `tools/prep-node:613` | Static netplan moves every other netplan file aside |
| F22 | 22 | rigid | high | `infra/ansible/tasks/assert-gpu.yml:23` | A `gpu` host must be NVIDIA PCI `0x10de` |
| F23 | 23 | brittle | high | `tools/verify/p0a:228` | P0a exits 0 while owner items and PVC enrollment are unchecked |
| F24 | 24 | brittle | high | `infra/ansible/README.md:44` | Operator-tag examples omit the inventory and the vars file |
| F25 | 25 | brittle | high | `ci/checks/lib/_cli_permissions.py:388` | Command guard and secret scan have known category gaps |
| F26 | 26 | brittle | medium | `tools/install-deps:97` | Apt install does not update the package index first |

## 3. Findings

### F1. prep-node's last step calls a checker that does not exist

- **Location:** `tools/prep-node:1002`, called from `tools/prep-node:1076`.
- **Problem:** After SSH, hostname, sudo, optional netplan, inventory edits, and the age-key prompt, `run_env_schema` executes `tools/checks/env-schema`. That path is not in the tree. The real check is `ci/checks/env-schema`. `FileNotFoundError` is not a `PrepError`, so the handler that prints `git diff` and re-raises does not run, and `main` does not turn it into the script's error line.
- **Evidence:**

```
tools/prep-node:1002-1013
def run_env_schema(repo: Path) -> None:
    result = subprocess.run(
        [str(repo / "tools" / "checks" / "env-schema")],
        ...
    )
```

```
tools/prep-node:1076-1086
apply_env_edits(...)
sync_age_key(...)
try:
    run_env_schema(REPO_ROOT)
except PrepError as exc:
    schema_error = exc
print("git diff:")
```

Command:

```
test ! -e tools/checks/env-schema && echo MISSING
.venv/bin/python -c "import runpy; from pathlib import Path; ns=runpy.run_path('tools/prep-node', run_name='not_main'); ns['run_env_schema'](Path('.'))"
```

Output: `MISSING`, then `FileNotFoundError [Errno 2] No such file or directory: 'tools/checks/env-schema'`.

`rg run_env_schema ci/tests` has no matches. The 15 `test_prep_node.py` tests passed without calling it.

- **Effect:** A prep run that changed the node and the checkout dies with a traceback. The env-schema gate never runs, so a bad edit is not rejected by this command. The operator does not get the promised diff.
- **Direction:** Call the same entry CI uses (`ci/checks/env-schema` or `schema.env.validate_env_tree`). One simpler call replaces the missing path. Add a test that a fixture edit makes this call fail.
- **Compromise:** The check then depends on the CI entry staying where it is. A wrong edit fails the command instead of being left for a later `make ci-verify`.
- **Risk / blast radius:** Anyone using `tools/prep-node`. No CI check covers this call. Touches the env the command just wrote (`hosts.yml`, `cluster-vars.yaml`, the generated ConfigMap, maybe `.sops.yaml` and deleted `*.sops.yaml`).
- **Type:** correctness.
- **Confidence:** high.

### F2. Restore is destructive, unbound to an env, and not resumed on interrupt

- **Location:** `tools/restore:175`, `tools/restore:409`, `tools/restore:709`, `Makefile:38`.
- **Problem:** `make restore APP=…` does not take `ENV`. It uses whatever `kubectl` context is current. The destination is a VolSync Direct copy with `enableFileDeletion: true`, so files on that PVC that are absent from the snapshot are removed. On `Fail`, the tool prints undo commands and does not scale workloads back or clear `backup=paused`. `KeyboardInterrupt` is outside that handler, so an interrupt skips the undo text as well.
- **Evidence:**

```
tools/restore:182-187
"copyMethod": "Direct",
"enableFileDeletion": True,
"restoreAsOf": as_of,
```

```
tools/restore:709-721
except Fail:
    kubectl(["delete", "pod", _binder_name(pvc), ...])
    _print_left(namespace, pvc, state)
    raise
```

`Makefile:38-40` runs `python3 tools/restore --app "$(APP)"` with no env and no context flag. `tools/restore:91` calls `kubectl` from `PATH`.

`platform/backups/CONTRACT.md:47` already says a failure leaves the pause, the scaled workloads, and the destination, and prints one command each. The contract does not bind the command to an env, and the `except` does not cover interrupt.

- **Effect:** A restore aimed at the wrong context, or the default `creationTimestamp - 1s` on a PVC that was not recreated, overwrites live data. A failed or interrupted run leaves Deployments and StatefulSets at 0 replicas and backups paused until someone runs the printed commands. An interrupt does not print them.
- **Direction:** Keep Direct restore (it is the method this stack has). Require the command to show the current context and the `jarvis_env` marker, and refuse when they disagree. Use one cleanup path for failure and interrupt that at least prints the undo list. Do not auto-resume a half-applied copy.
- **Compromise:** An operator on a controller with one kubeconfig types one more confirmation. Auto-resume would hide a half-written volume.
- **Risk / blast radius:** Every PVC `make restore` or `make restore-all` touches. Interacts with the rebuild hold (`tools/restore:858`) and with Kyverno regenerate if `pvc-backup` is force-applied during the pause (`platform/backups/CONTRACT.md:40`). Tests in `ci/tests/test_restore.py` cover the pause-before-copy order and the no-snapshot failure. They do not cover interrupt.
- **Type:** safety.
- **Confidence:** high.

### F3. Backup enrollment fails open, and nothing checks the result

- **Location:** `platform/backups/enrollment.yaml:9`, `tools/verify/p0a:225`.
- **Problem:** `failurePolicy: Ignore` is set on the admission webhook. A PVC create succeeds when Kyverno cannot answer. Enrollment is also deferred until `spec.volumeName` is set. `make verify PHASE=p0a` checks Flux Ready, that Secret `backups-restic` has keys, node labels, and the edge load-balancer label. It does not list PVCs against ReplicationSources.
- **Evidence:**

```
platform/backups/enrollment.yaml:9-10
webhookConfiguration:
  failurePolicy: Ignore
```

`ci/tests/test_backup_enrollment.py:138-139` asserts that `Ignore` and that `spec.failurePolicy` is unset. `ci/checks/lib/_pvc_backup.py:55` checks policy presence, the opt-out allow-list, and that Git has no ReplicationSource. It does not, and cannot, see a PVC created at runtime.

- **Effect:** While admission is down, or before the first generate after bind, a volume can hold data with no restic copy. `generateExisting: true` should catch up after Kyverno is healthy. Until then the gap is silent. P0a still exits 0.
- **Direction:** Keep `Ignore`. Switching the webhook to `Fail` would block every PVC, including local-path binds, whenever Kyverno is down. Add a read-only verify item: every bound PVC that is not `backup: none` has Secret `restic-<name>` and a ReplicationSource. That is the check the webhook is not.
- **Compromise:** `Fail` would make a Kyverno outage an outage for any workload that creates a claim. The verify item is a point-in-time check, not an admission guarantee.
- **Risk / blast radius:** Every future PVC. The policy is force-recreated on edit (`enrollment.yaml:7`), which deletes and regenerates generated objects (`platform/backups/CONTRACT.md:38`). CI check `pvc-backup` and `test_backup_enrollment.py` lock the current Ignore behaviour, so changing it is a deliberate contract edit.
- **Type:** safety.
- **Confidence:** high.

### F4. The data disk mount is a role constant, not the env paths

- **Location:** `infra/ansible/roles/node-disk/vars/main.yml:2`, `schema/env.py:364`.
- **Problem:** `node_disk_mount` is `/var/lib/jarvis`, filesystem `ext4`, label `jarvis-data`. `backup_path` and `local_path_data` are required env strings and only have to be absolute. Nothing checks that they live on that mount. Both example and env-01 happen to use `/var/lib/jarvis/backup` and `/var/lib/jarvis/local-path`. The k3s drop-in waits for those two paths (`infra/ansible/roles/node-disk/templates/k3s-mount.conf.j2`), which does not mean the formatted disk is what is mounted there.
- **Evidence:**

```
infra/ansible/roles/node-disk/vars/main.yml:1-4
# Product constants. Paths come from cluster-vars.
node_disk_mount: /var/lib/jarvis
node_disk_label: jarvis-data
node_disk_fstype: ext4
```

`schema/env.py:364-365` rejects a `local_path_data` that does not start with `/`. A search of `schema/env.py` finds no `node_disk_mount` and no prefix check against `backup_path`.

- **Effect:** An adopter who sets `backup_path: /mnt/backup` gets a formatted disk at `/var/lib/jarvis` and restic data on the root filesystem at `/mnt/backup`. The play still succeeds. k3s `RequiresMountsFor` waits for a path that was never the data disk.
- **Direction:** One rule: when a host has `data_disk`, `backup_path` and `local_path_data` must be under the mount, and the mount should be the env value (or a single documented constant that the schema requires as a prefix). ext4 can stay until someone needs another filesystem.
- **Compromise:** A prefix check rejects env layouts that put restic on a second disk that is not this mount. That layout would need the mount itself to be the env setting.
- **Risk / blast radius:** `node-disk`, k3s `default-local-storage-path`, rest-server hostPath, etcd snapshot dir. No current CI check relates the three paths. Both committed envs match the constant, so today's suite stays green.
- **Type:** safety.
- **Confidence:** high.

### F5. The shipped env cannot rebuild from a lost disk

- **Location:** `envs/env-01/inventory/hosts.yml:3`, `platform/backups/CONTRACT.md:46` and `:59`.
- **Problem:** env-01 puts every role on one host and sets no `data_disk`. The inventory comment says Jarvis data stays on the root disk on purpose. rest-server and the etcd snapshot dir therefore share the OS disk. The backup contract states that this copy adds retention and dedup, not disk-failure protection. An etcd restore onto a rebuilt node also needs the original k3s server token. That token is not an env secret and not in Git. `secrets-encryption: true` is on (`infra/ansible/roles/k3s/templates/config.yaml.j2:4`).
- **Evidence:**

```
envs/env-01/inventory/hosts.yml:3-7
# bench.lan keeps Jarvis data on the root disk on purpose.
# It has no data_disk. The other drive is the OS-install disk and stays out of this inventory.
all:
  hosts:
    bench.lan:
      node_roles: [control_plane, workers, storage, backup, edge]
```

The hostname is this env's inventory name. The defect is the missing `data_disk` and every role on that one host.

`platform/backups/CONTRACT.md:46`: "While the rest-server's data sits on the same disk, this copy adds retention and dedup, not disk-failure protection."

`platform/backups/CONTRACT.md:59`: restoring onto a rebuilt node needs the original k3s server token; same-node restore is the stated scope.

`envs/env-01/README.md:23-31` tells a new env not to copy this folder, and does not mention a data disk or where the server token lives.

- **Effect:** Disk loss on the only shipped layout loses etcd and the restic repos together. A new node plus the age key restores Git and can recreate empty PVCs. It does not restore cluster secrets or PVC bytes unless a restic copy survived on some other disk. The foundation claim (rebuild from repo, env folder, and key, including backed-up data) is not true for this env.
- **Direction:** Keep same-disk as a legal choice for a wipeable test box. Say so in the new-env steps, and name the two things that have to live off that disk before the data matters: the restic directory (a `data_disk` or a host whose only role is `backup`) and a copy of the k3s server token next to the age key. Do not pretend the CronJob is off-site backup.
- **Compromise:** A test env that is wiped on purpose stays simple. The docs get a harder sentence, and a serious env uses a disk the schema already allows.
- **Risk / blast radius:** env-01 only, until another env copies it. `restore-all` and the rebuild hold assume the restic files are still on the backup host.
- **Type:** safety.
- **Confidence:** high.

### F6. A new internal CA does not replace the serving certificate

- **Location:** `envs/env-01/secrets/README.md:27`. The cert-manager contract does not say this (`platform/cert-manager/CONTRACT.md:25`).
- **Problem:** The secrets README says cert-manager v1.21.3 does not reissue when the issuer CA changes, so `wildcard-tls` keeps the old leaf until about day 60 unless someone deletes that Secret. Rotation steps (delete the SOPS file and `ca.crt`, bootstrap again, wait for Flux, delete `wildcard-tls`) live only in the env-01 secrets note.
- **Evidence:** `platform/cert-manager/CONTRACT.md:25` says the CA lifetime is 10 years, set by bootstrap, and the leaf lifetime is the chart default. It does not mention reissue. `envs/env-01/secrets/README.md:27-31` has the day-60 behaviour and the `kubectl delete secret` recovery.
- **Effect:** An adopter who rotates the CA by the contract alone keeps serving the previous leaf. Clients that trust the new `ca.crt` fail verification until the secret is deleted or the leaf ages out.
- **Direction:** Put the reissue limit and the delete step in `platform/cert-manager/CONTRACT.md`. A later small controller or a documented `make` note can delete `wildcard-tls` after the CA secret changes. Do not build a rotator now.
- **Compromise:** The contract grows an operational warning. Automatic deletion of `wildcard-tls` causes a short window with no default certificate, which the README already describes.
- **Risk / blast radius:** One certificate, every HTTPS name on Traefik. `make verify PHASE=p0b` checks the chain against `envs/<env>/ca.crt` and would fail during that window.
- **Type:** safety.
- **Confidence:** medium (the README states the upstream behaviour; this audit did not run cert-manager).

### F7. wipefs runs in the cases the role says it does not check

- **Location:** `infra/ansible/roles/node-disk/tasks/main.yml:51`, `infra/ansible/README.md:54`, `infra/ansible/roles/node-disk/tasks/guards.yml:41`.
- **Problem:** If the disk is not already labeled `jarvis-data`, the play runs `wipefs -a` and then `community.general.filesystem` with `force: true`. The guards refuse the root disk, a second `jarvis-data` label, and a wipe unless `wipe_confirmed` is boolean true and `jarvis_wipe_confirm` contains `hostname:disk-id`. The README lists what is still not checked: mounted elsewhere, a foreign or non-empty mountpoint, partition contents, data paths outside the mount, and other members of a multi-device btrfs filesystem.
- **Evidence:**

```
infra/ansible/roles/node-disk/tasks/main.yml:51-53
argv: [wipefs, -a, "{{ node_disk_path }}"]
when: not (node_disk_ours | bool)
```

`infra/ansible/README.md:54` ends with the "Not checked" list quoted above. `envs/env-00/inventory/hosts.yml` sets `wipe_confirmed: true` on several example disks. The extra `-e jarvis_wipe_confirm=…` is still required, so the git flag alone does not wipe.

- **Effect:** Two confirmations against the wrong by-id, or a disk that is mounted somewhere other than `/` and holds data, still get `wipefs -a`. A rerun after a failed format wipes again if the label was not written and both confirms are still set.
- **Direction:** Keep the two confirms. Add the check the README already names as missing: refuse when `findmnt` shows this disk (or its partitions) mounted anywhere. That is smaller than a new disk framework.
- **Compromise:** A disk the operator intends to wipe while it is mounted at `/mnt/old` would need an explicit unmount first.
- **Risk / blast radius:** Any host in the `data_disk` group. `ci/tests/test_node_disk.py` and `ci/tests/fixtures/node-disk/refusals.yml` lock the root-disk refusals. They do not cover "mounted elsewhere."
- **Type:** safety.
- **Confidence:** high.

### F8. The schema's substitution rule is not a check

- **Location:** `schema/CONTRACT.md:23`. Consumers: `platform/**/*.yaml` (eight keys today).
- **Problem:** The contract says cluster-vars keys referenced as `${…}` in manifests must exist in that file. `schema/env.py` validates the env files only. Flux is configured with `StrictPostBuildSubstitutions=true` (`infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2:32`), so a typo fails at reconcile time, on the cluster, not in CI.
- **Evidence:** Platform manifests use only `app_dns_cluster_ip`, `backup_path`, `backup_retention_daily`, `backup_retention_monthly`, `backup_retention_weekly`, `backup_schedule`, `dns_suffix`, and `time_zone`. All eight are in `_CLUSTER_VARS_KNOWN`. A temp tree with a ConfigMap `data.host: "${not_a_cluster_var}"` produced no digest-pin hit and no manifest-lint hit. `scan_pvc_backup` reported only the missing policy, which is a different rule. `rg '\$\{' ci/checks schema` does not show a substitution scanner.
- **Effect:** A new manifest can ship a typo or a key that exists only in one env. CI stays green. The kustomization fails after merge, and with the controllers/configs ordering in F12 that failure can sit next to a half-applied platform.
- **Direction:** One scanner: extract `${name}` from YAML under `platform/`, `services/`, `apps/`, and `envs/*/flux/`, and require each name to be in `_CLUSTER_VARS_KNOWN`. `$$` escapes stay literals. That is the contract sentence turned into the check that should already exist.
- **Compromise:** A scanner must understand the `$${` escape and must ignore files that are not substituted (the flux-operator HelmRelease sets substitute disabled). A sloppy regex will false-fail those.
- **Risk / blast radius:** Every postBuild manifest. `test_flux_entry.py` already fails if the rendered ConfigMap drifts from `cluster-vars.yaml`. It does not walk `${}`.
- **Type:** brittle.
- **Confidence:** high.

### F9. The cluster is permanently one server

- **Location:** `infra/ansible/site.yml:14`, `infra/ansible/roles/k3s/templates/config.yaml.j2:3`, `schema/env.py:77` and `:676`.
- **Problem:** The play asserts `groups['control_plane'] | length == 1`. The server config always sets `cluster-init: true`. The schema requires exactly one `control_plane` and exactly one `backup`. Agents are `all:!control_plane`, so extra workers can join. A second server cannot. There is no join-as-server path and no etcd member replace.
- **Evidence:**

```
infra/ansible/site.yml:14-20
- name: Assert exactly one control_plane host
  ansible.builtin.assert:
    that:
      - groups['control_plane'] | default([]) | length == 1
```

```
infra/ansible/roles/k3s/templates/config.yaml.j2:3
cluster-init: true
```

`schema/env.py:676-678` errors when a singleton role count is not 1. `ci/tests/test_k3s.py` asserts `cluster-init: true` on the server render and that it is absent on the agent render.

- **Effect:** Adding a worker is an inventory edit plus `site.yml`. Adding a second control plane, or replacing a dead one without the etcd snapshot dance in the backup contract, is a design change. Losing the one server is a cluster loss (see F5 for the token).
- **Direction:** Leave the single server in place for this hardware. Write the ceiling down next to the schema table so it is not rediscovered as a bug. A second server is its own later change (embedded etcd membership, token, and the label singleton). Do not add a fake `ha: false` flag.
- **Compromise:** Home clusters stay one etcd. A site that wants the control plane to survive a node loss waits for that later design.
- **Risk / blast radius:** `site.yml`, k3s config, node label task, rest-server and etcd CronJob nodeSelectors (`role.${dns_suffix}/control_plane` and `.../backup`). Schema tests reject a second control plane on purpose.
- **Type:** rigid.
- **Confidence:** high.

### F10. k3s is amd64 only, while the controller tools are not

- **Location:** `infra/ansible/roles/k3s/tasks/main.yml:28`, `schema/CONTRACT.md:50`, `tools/versions.env:7`.
- **Problem:** The role asserts `ansible_facts['architecture'] == 'x86_64'` and downloads the release asset named `k3s`, which is the amd64 binary, checked against `k3s_sha256`. The schema describes that hash as the amd64 binary. `tools/install-deps` and the Helm CLI pin both amd64 and arm64. A controller can be arm64. A node cannot.
- **Evidence:**

```
infra/ansible/roles/k3s/tasks/main.yml:28-31
- name: Require an amd64 host
  ansible.builtin.assert:
    that: ansible_facts['architecture'] == 'x86_64'
    fail_msg: k3s role supports only amd64 hosts.
```

```
infra/ansible/roles/k3s/tasks/main.yml:72-79
url: https://github.com/k3s-io/k3s/releases/download/{{ k3s_version }}/k3s
checksum: sha256:{{ k3s_sha256 }}
```

`schema/CONTRACT.md:50`: "SHA-256 of the pinned `k3s` amd64 release binary".

- **Effect:** An adopter with an arm64 board fails at the k3s role after prep-node has already recorded the host. Mixed arm workers are rejected the same way. GPU scheduling across nodes does not help if those nodes cannot join.
- **Direction:** Either document amd64 as a hard limit in the new-env steps, or add an arch field and a second sha in cluster-vars and have the role pick `k3s` or `k3s-arm64`. The second option is the small design. Do not guess the arm hash in this audit.
- **Compromise:** Supporting arm means two hashes per bump, and the Traefik "checked on k3s x" line (`platform/traefik/CONTRACT.md:32`) has to say which arches were tried. Refusing arm keeps one pin and excludes a common home-lab board.
- **Risk / blast radius:** k3s role, `k3s_sha256` schema, Renovate (k3s bumps are disabled in `renovate.json` on purpose). `ci/tests/test_k3s.py` locks the URL and the checksum variable.
- **Type:** rigid.
- **Confidence:** high.

### F11. Digest pin and probe lint stop at the chart boundary

- **Location:** `ci/checks/lib/_digest_pin.py:6`, `ci/checks/lib/_manifest_lint.py:91`.
- **Problem:** Digest pin checks pod `image` fields and HelmRelease `values` image maps. The module says HelmChartConfig is the k3s chart and is not scanned. Manifest lint requires probes on native pod containers. For Helm values it only rejects a probe that is explicitly disabled. A HelmRelease or HelmChartConfig that sets resources and omits probes passes.
- **Evidence:** `_digest_pin.py:6-7`: "HelmChartConfig is the k3s chart and is not scanned."

A temp `HelmChartConfig` whose `valuesContent` set `image: traefik:v3.1.0` returned `scan_digest_pin` `[]` and `scan_manifest_lint` `[]`.

A temp `HelmRelease` with `spec.values.resources` set and no probes returned `scan_manifest_lint` `[]`.

The same digest scanner did flag `repository: nginx` plus `tag: "1.27"` on a HelmRelease (`image nginx is not pinned by digest`), so the HelmRelease path works. The hole is the chart config and the missing-probe case.

`platform/traefik/helmchartconfig.yaml` sets resources and does not set an image. The running image is whatever the bundled chart tag is. `platform/traefik/CONTRACT.md:32` records "chart v40 on k3s v1.36.5+k3s1" as a checked line, and `ci/tests/test_traefik.py:69` fails if a non-example env's `k3s_version` drifts from that sentence. That is a version note, not a digest.

- **Effect:** The one LoadBalancer the cluster exposes can move when the k3s pin moves, with no digest in Git. A later HelmRelease can ship a chart with probes off in the chart and no probe in values, and CI will not ask for one. Native Deployments stay covered (`test_manifest_lint.py` seeds a missing memory limit, a CPU limit, and a missing probe).
- **Direction:** Scan HelmChartConfig `valuesContent` for image references the same way as HelmRelease values. For probes, require them only where the values document has a workload-shaped probe key or where the contract says this chart's probes must stay on (Traefik already asserts the override does not disable them). Do not pretend a values walk sees templates that are not in Git.
- **Compromise:** Pinning the bundled Traefik image means tracking an image the chart owns, or accepting the k3s pin as the pin and saying so in the digest-pin contract. Requiring probes inside arbitrary Helm values will false-fail charts that use different key names.
- **Risk / blast radius:** `ci/checks/digest-pin`, `ci/checks/manifest-lint`, Traefik, and every later chart. Seeded tests exist and should stay; this adds a case they currently exclude on purpose.
- **Type:** brittle.
- **Confidence:** high.

### F12. Configs are applied when the controller kustomization exists, not when it is Ready

- **Location:** `envs/env-01/flux/kustomizations/platform-controllers.yaml:12`, `envs/env-01/flux/kustomizations/platform-configs.yaml:14`.
- **Problem:** The controllers kustomization comment says a HelmRelease healthCheck would stall platform-configs on a public chart pull. `dependsOn` in Flux waits for the dependency kustomization to be Ready, and Ready here means the apply succeeded, not that Kyverno admission or the VolSync webhook is serving. platform-configs then applies the ClusterPolicy, the CA, the rest-server, and the test app.
- **Evidence:**

```
envs/env-01/flux/kustomizations/platform-controllers.yaml:12-13
# A HelmRelease healthCheck would stall platform-configs on a public chart pull.
```

```
envs/env-01/flux/kustomizations/platform-configs.yaml:14-15
dependsOn:
  - name: platform-controllers
```

`ci/tests/test_flux_entry.py:177` locks the chain order and does not lock a health check.

- **Effect:** On a slow pull, the policy and the Certificate are applied before the webhooks exist. Flux retries. Combined with F3, the first PVCs in that window are not enrolled until a later generate. This is survivable and it is also how a "green" reconcile hides a not-yet-ready stack.
- **Direction:** Keep the decision to not block the whole layer on a chart download. Add health checks on the two releases that later objects call (Kyverno admission, cert-manager webhook), or accept the retry window in the backup and cert contracts in one sentence. The comment is the design; the contracts should match it.
- **Compromise:** A health check on Kyverno makes the first install wait for the chart. That is the stall the comment avoided. A documented retry window leaves the race in place and makes it obvious.
- **Risk / blast radius:** First boot and every period where a controller is absent. `platform/kustomization.yaml` already orders the VolSync controller before Kyverno inside the controllers bundle, which is the right order once both applies have finished.
- **Type:** brittle.
- **Confidence:** high.

### F13. First contact does not reach a deploy

- **Location:** `README.md:5`, `docs/README.md:3`, `envs/env-01/README.md:23`, `envs/env-00/values/cluster-vars.yaml:1`.
- **Problem:** The root README is five lines and links to `https://github.com/<owner>/project-plans/...`, which is a placeholder, not a path an adopter can open. `docs/README.md` says the plan stays outside the repo. `envs/env-00` is a never-deployed example with `k3s_version: v0.0.0+k3s0` and no `flux/` or `secrets/`. `envs/env-01/README.md` says create an env empty and do not copy env-01, then lists values, inventory, a `.sops.yaml` rule, bootstrap, a ConfigMap render, and "add a flux/ tree". It does not say which files that tree contains. The only working tree is `envs/env-01/flux/` (FluxInstance, four Kustomizations, platform-configs, generated ConfigMap).
- **Evidence:** `README.md:3-5` and `docs/README.md:3-7` point at `<owner>/project-plans`. `envs/env-00/values/cluster-vars.yaml:1` and `:16` are the invented example and `v0.0.0+k3s0`. `envs/env-01/README.md:25-31` is the whole new-env procedure. `envs/env-00` has no `flux/` directory (`schema/CONTRACT.md:102` says the example has no secrets, SOPS file, or flux).
- **Effect:** An adopter on hardware unlike env-01 opens the root README, leaves the repo, and comes back to a folder they are told not to copy and an example that will not install k3s. Counting files to discover the flux entry: the root README, `docs/README.md`, `infra/ansible/README.md`, `schema/CONTRACT.md`, `envs/env-01/README.md`, `envs/env-01/secrets/README.md`, and then the eight files under `envs/env-01/flux/` to see what step 5 means.
- **Direction:** One short deploy note in this repo: prerequisites (section 8), the file list a new `envs/<env>/flux/` must contain, and "use env-00 for shape, use env-01's flux/ as the file list, copy neither identity nor secrets." Keep the plan repo outside. Replace the `<owner>` placeholder with "the plan repo you were given" or drop the link until the distribution decision exists.
- **Compromise:** The repo gains a page that will drift if nobody updates it. A wrong copy of env-01 secrets is worse than a short page.
- **Risk / blast radius:** Docs only. `core-names` allows env names under `docs/` and still rejects hostnames, the DNS suffix, and the owner handle there.
- **Type:** rigid.
- **Confidence:** high.

### F14. Service and app config is validated and then dropped

- **Location:** `schema/env.py:557` and `:594`, `tools/lib/render_cluster_vars_configmap.py:41`, `tools/bootstrap-secrets:273`, `envs/env-01/flux/platform-configs/kustomization.yaml:3`, `envs/env-01/flux/kustomizations/services.yaml:9`.
- **Problem:** The renderer builds ConfigMap `cluster-vars` from cluster-vars only. `hermes.yaml` and `apps.yaml` are not in it. Hermes validation checks key names and that a tier is `frontier`, `mid`, or `cheap`. It does not require every bot to be in `bot_tier_map`, does not require model maps to be non-empty, and does not check `lane_model_map` shape. Apps validation checks that `apps` is a list. The contract says apps have sizes (`schema/CONTRACT.md:79`). env-00's app entry is the string `example-app`. Bootstrap always creates Grafana and four Hermes secrets. platform-configs applies only `backups-restic` and `cert-manager-ca`. `services` and `apps` are each one kustomization (`path: ./services` and `path: ./apps`, `prune: true`) whose only resource is a ConfigMap in `default`.
- **Evidence:** `render_cluster_vars_configmap.py:41-46` flattens `resolve_cluster_vars` and nothing else. `_validate_hermes` (`schema/env.py:567-591`) walks `bot_tier_map` values and does not compare them to `bots`. `_validate_apps` (`schema/env.py:608-610`) stops at "must be a list". `envs/env-01/values/hermes.yaml:2` is `bots: []` while `bot_tier_map` names five bots and both model maps are empty. `tools/bootstrap-secrets:290-323` builds `grafana-admin` and the four Hermes secrets. `envs/env-01/flux/platform-configs/kustomization.yaml:3-6` lists backups, cert-manager config, testapp, and two SOPS files.
- **Effect:** The first Hermes or app PR has to invent how its env file becomes a pod, and how a secret that already exists gets into a kustomization. A bad apps list or an empty model map passes CI. One shared kustomization means one bad file and one prune boundary for every service. The placeholder ConfigMaps in `default` are deleted on the first prune once they leave the resources list, which is fine, and easy to miss.
- **Direction:** Before the first service, pick the pipe: either render `hermes.yaml` / `apps.yaml` the way cluster-vars is rendered, or have the service chart read a ConfigMap the env flux tree emits. Split Flux so each service is its own Kustomization with its own `dependsOn`, instead of growing `./services`. Tighten the schema when the service's real fields exist, not before. The placeholder ConfigMaps can stay until that PR replaces them.
- **Compromise:** A renderer for hermes now would freeze a file format the service does not exist to consume. Leaving the schema loose means the first service PR is also a schema PR. One kustomization is less YAML until the second service.
- **Risk / blast radius:** `schema/env.py`, bootstrap, and the four flux kustomizations. `ci/checks/env-schema` and `test_env_schema.py` will need new cases when the shape gets stricter. `pvc-backup` allow-list paths `services/observability/victoria-metrics/` and `victoria-logs/` do not exist yet; the first observability PVC is enrolled unless it is added under those prefixes.
- **Type:** rigid.
- **Confidence:** high.

### F15. The kubeconfig role has one destination and one URL shape

- **Location:** `infra/ansible/roles/kubeconfig/tasks/main.yml:25` and `:43`.
- **Problem:** The fetched admin kubeconfig is rewritten only when the server URL matches `https://127.0.0.1:6443`, then copied to `~/.kube/config` on the controller with `backup: true`. There is no context name and no per-env file. `restore` and `make verify` then use that file (F2, F23).
- **Evidence:**

```
infra/ansible/roles/kubeconfig/tasks/main.yml:25-29
regexp: 'https://127\.0\.0\.1:6443'
replace: "https://{{ inventory_hostname }}:6443"
```

```
infra/ansible/roles/kubeconfig/tasks/main.yml:43-48
dest: "{{ k3s_kubeconfig_runner_path }}"
mode: "0600"
backup: true
```

`k3s_kubeconfig_runner_path` is `$HOME/.kube/config` (`:4-5`).

- **Effect:** A k3s kubeconfig that uses another loopback form keeps pointing at the controller's own port 6443. A second env's `site.yml` on the same controller replaces the only kubeconfig. The previous file is a timestamped backup, and the tools do not select it.
- **Direction:** Write `~/.kube/jarvis-<env>.config` and set `KUBECONFIG` in the verify and restore wrappers. Keep the 127.0.0.1 rewrite, and fail the play if the server line did not change.
- **Compromise:** Scripts and habits that assume `~/.kube/config` need the wrapper. Failing on an unexpected URL blocks a k3s upgrade that changes the default server line, which is the point.
- **Risk / blast radius:** The controller user's kubeconfig. `ci/tests/test_kubeconfig.py` covers the current rewrite. Agents are denied `kubectl` writes by the command guard; this role is the supported writer.
- **Type:** brittle.
- **Confidence:** medium (the 127.0.0.1 form matches current k3s; the single path is what the file does).

### F16. Restore's quiesce is narrower than its wait, and the tests never scale anything

- **Location:** `tools/restore:409`, `ci/tests/test_restore.py:126`.
- **Problem:** Quiesce lists Deployments and StatefulSets, suspends the Flux Kustomization labeled on the PVC or those objects, and scales those to 0. The wait then requires no pod in the namespace to be running, pending, or terminating while mounting the PVC. A DaemonSet, Job, or bare Pod is not scaled, so the wait fails after the scale and the pause have already happened. The test double returns an empty list for every Deployment and StatefulSet get.
- **Evidence:**

```
tools/restore:409-429
deployments = _items(kubectl, "deployment", namespace)
statefulsets = _items(kubectl, "statefulset", namespace)
...
_scale_kind(..., "deployment", ...)
_scale_kind(..., "statefulset", ...)
```

```
ci/tests/test_restore.py:126-127
if argv[:2] in (["get", "deployment"], ["get", "statefulset"]):
    return _completed(argv, json.dumps({"items": []}))
```

- **Effect:** A node agent or a Job that mounts the claim cannot be restored by this command. The failure path of F2 then leaves the Deployments that did scale sitting at 0. A regression that stops scaling, or that scales the wrong object, stays green.
- **Direction:** Teach the test double to return one Deployment that mounts the PVC and assert the scale to 0 and the scale back. For other kinds, fail before scaling with a clear "unsupported owner" line, or suspend the owning controller the same way. DaemonSet support matters once a service uses one.
- **Compromise:** Failing fast on a DaemonSet refuses a restore the operator might have done by deleting the pod by hand. An empty test double is shorter and hides the bug.
- **Risk / blast radius:** `tools/restore` and `ci/tests/test_restore.py` (15 tests, all passing against the empty lists).
- **Type:** brittle.
- **Confidence:** high.

### F17. Several contracts describe work that is not in the tree

- **Location:** `docs/code-delivery/CONTRACT.md:14`, `platform/traefik/CONTRACT.md:13`, `ci/checks/lib/_contract_md.py:9`.
- **Problem:** The code-delivery contract offers an issue queue, a draft-PR publisher, GHCR, and `ci/checks/admin-gate` as a required check. None of those files exist. `rule-checks.txt` has ten checks and no `admin-gate`. The exposure contract says each component adds a Gatus check. Gatus is not in the repo. `platform/testapp/CONTRACT.md:18` says the fixture does not add one yet. `_contract_md.py` lists future roots (`docs/build-agent`, `services/hermes/deploy/...`, `services/observability`) that are absent, so the check skips them until the directory appears. The contracts also cite phase ids (`p0a-16`, `p2-24`, `p4a-04`) as proof.
- **Evidence:** `docs/code-delivery/CONTRACT.md:17` names `ci/checks/admin-gate`. `ls ci/checks` has no such file. `platform/traefik/CONTRACT.md:13`: "The component adds its own Gatus check for each exposed endpoint (R5)." `rg -l gatus` hits the contracts and the rulebook, not a manifest. `rulebooks/coding-agents.md:6` says "Gatus stays a later check."
- **Effect:** A new engineer cannot tell which sentences are true. An agent treating the contract as the spec will build Gatus or admin-gate "because the contract offers it," or will think CI already has the gate. The rulebook and the testapp contract contradict the exposure contract.
- **Direction:** Split each contract into what the directory implements now and a short "later" list, or mark unimplemented offers as not yet offered. Keep the phase ids out of the proof section until the check exists. The template at `docs/CONTRACT.template.md` already asks for proof that enforces the contract.
- **Compromise:** Future work becomes less visible in the file a caller reads. That is the point: the file stops being a plan.
- **Risk / blast radius:** Docs and `ci/checks/contract-md`, which only checks that present roots have a file, not that the file matches the tree.
- **Type:** complexity.
- **Confidence:** high.

### F18. Three pins that look like one source are three sources

- **Location:** `tools/versions.env:33`, `infra/ansible/roles/k3s/defaults/main.yml:8`, `tools/bootstrap-secrets:56`, `infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2:21`.
- **Problem:**
  - `kubectl_version=1.36.5` in `tools/versions.env`. `k3s_version` is per env (`v1.36.5+k3s1` on the non-example env). No test references `kubectl_version`. The Traefik contract test compares k3s to a sentence, not to kubectl.
  - etcd snapshot cron `0 */12 * * *` and retention `5` are role defaults, not cluster-vars. The copy CronJob schedule is a literal. `ci/tests/test_restic_rest_server.py:130` asserts the copy is 1–15 minutes after the role default. Retention of the on-disk k3s snapshots is not the restic retention in cluster-vars.
  - Flux `domain: cluster.local` is fixed in the instance template. `RESTIC_REPOSITORY` is the literal `rest:http://restic.platform-backups.svc.cluster.local:8000/` written once by bootstrap. Bootstrap does not overwrite an existing file (`tools/bootstrap-secrets:2` and `:344`).
- **Evidence:** `rg kubectl_version ci schema` finds nothing. `infra/ansible/roles/k3s/defaults/main.yml:8-9` holds the cron and the retention. `tools/bootstrap-secrets:56` holds the rest URL. `flux-instance.yaml.j2:21` holds `domain: cluster.local`. The committed env FluxInstance matches the template (`test_git_flux_instance_matches_ansible_template`), so those two do not drift today.
- **Effect:** A k3s minor bump that forgets `tools/versions.env` leaves an older kubectl on the controller. Changing the etcd cron in the role without editing the CronJob fails the test, which is good, and still cannot be set per env. A non-default cluster domain cannot be fixed without deleting the restic secret, which rotates the password and orphans existing repos.
- **Direction:** Compare `kubectl_version` to the k3s minor in one test. Leave the 12-hour etcd cron as the default and move it to cluster-vars only if an env needs a different cadence. Leave `cluster.local` until an env needs something else; if it moves, it has to move in the template and in the bootstrap URL together, with an explicit secret rotation step.
- **Compromise:** A test that ties kubectl to k3s makes a deliberate skew (controller kubectl ahead of the nodes) fail CI. Putting the etcd cron in cluster-vars adds a key every env must set.
- **Risk / blast radius:** Controller CLI, etcd snapshots, restic URLs inside existing SOPS files. `renovate.json` already refuses automatic k3s and Helm CLI bumps.
- **Type:** brittle.
- **Confidence:** medium.

### F19. The manifest walk is copied

- **Location:** `ci/checks/lib/_manifests.py:9`, `ci/checks/lib/_pvc_backup.py:21`.
- **Problem:** `SCAN_ROOTS` and `SKIP_ENV_PREFIXES` are defined again as `_SCAN_ROOTS` and `_SKIP_ENV_PREFIXES` in the PVC backup module. `_tool_env()` is copied in `tools/restore.py:79`, `tools/verify/p0a:37`, and `tools/verify/p0b:37`. The copies match today.
- **Evidence:**

```
ci/checks/lib/_manifests.py:9-10
SCAN_ROOTS = ("apps/", "platform/", "services/", "envs/")
SKIP_ENV_PREFIXES = ("secrets/", "values/", "inventory/")
```

```
ci/checks/lib/_pvc_backup.py:21-23
_SCAN_ROOTS = ("apps/", "platform/", "services/", "envs/")
_HANDWRITTEN_ROOTS = ("services/", "apps/")
_SKIP_ENV_PREFIXES = ("secrets/", "values/", "inventory/")
```

- **Effect:** A new top-level tree, or a new env prefix that must stay unscanned, gets updated in one walker and not the other. Backup rules and digest rules then disagree about what "the repo deploys" means.
- **Direction:** Import the tuple from `_manifests`. Leave `_HANDWRITTEN_ROOTS` local. A shared `_tool_env` is worth doing when one of the three copies changes, not as its own project.
- **Compromise:** `pvc-backup` becomes a caller of the manifest helper. A future backup-only skip has to be explicit instead of a quiet edit to the copy.
- **Risk / blast radius:** `ci/checks/digest-pin`, `manifest-lint`, `loadbalancer`, `pvc-backup`. Behaviour should be unchanged if the tuples stay equal.
- **Type:** complexity.
- **Confidence:** high.

### F20. Pre-push CI fails closed on a variable the Makefile never mentions

- **Location:** `ci/ci-local-verify:10`, `Makefile:25`, `.github/workflows/ci.yml:30`, `ci/checks/lib/_core_names.py:87`.
- **Problem:** `scan_core_names` raises `OwnerNameUnset` when `OWNER_NAME` is missing or blank. The message is `OWNER_NAME is unset` and does not contain the value. GitHub Actions sets it from `vars.OWNER_NAME`. `make ci-verify` runs `ci/ci-local-verify`, which exports `PATH` and does not mention `OWNER_NAME`. The rulebook tells agents to run `make ci-verify` before every push (`rulebooks/coding-agents.md:36`).
- **Evidence:** Unsetting the variable and calling `scan_core_names` raised `OwnerNameUnset: OWNER_NAME is unset`. `ci/ci-local-verify:1-12` has no `OWNER_NAME`. `.github/workflows/ci.yml:30-32` sets `OWNER_NAME: ${{ vars.OWNER_NAME }}` for `bash ci/run-checks` only. The pytest step does not set it; unit tests inject it themselves. With `OWNER_NAME="Gordon Cooper"`, the full suite exited 0.
- **Effect:** A fork's CI is red until the repository variable exists. That is the right failure. A local or agent run of the documented command is red for the same reason, with no hint in `make help`. The variable is how personal names stay out of core. The gap is the missing hint, not the check.
- **Direction:** One line in `make help` and in `infra/ansible/README.md` or the deploy note: `OWNER_NAME` must be set for `make ci-verify`, and the GitHub repo needs the same variable. Do not default it inside the check.
- **Compromise:** The hint names the variable. It must not name a person. Putting a default in the check would let a personal name into core unnoticed.
- **Risk / blast radius:** `ci/checks/core-names` and every pre-push. `test_owner_name_unset_fails` already locks the failure.
- **Type:** brittle.
- **Confidence:** high.

### F21. prep-node's static network replaces every other netplan file

- **Location:** `tools/prep-node:608`.
- **Problem:** If the primary route is DHCP and the operator confirms, the script moves every `/etc/netplan/*.yaml` except `99-jarvis-static.yaml` to `*.bak`, then writes one file and runs `netplan try`. A rejected try restores the backups. An accepted try leaves the other files as `.bak`. DNS is the first resolver on that interface only.
- **Evidence:**

```
tools/prep-node:613-616
for other in /etc/netplan/*.yaml; do
  [ "$other" = "$file" ] && continue
  mv "$other" "$other.bak"
done
```

`tools/prep-node:599-600` takes the first DNS from `resolvectl`. The operator can refuse the prompt and stay on DHCP (`tools/prep-node:648`).

- **Effect:** A node with a second NIC, a Wi-Fi file, or a vendor netplan split loses those files from the active set after a successful pin. Rollback works only when `netplan try` does not report acceptance.
- **Direction:** Keep the prompt and `netplan try`. Write the static file and leave other files in place unless they define the same interface. That is the smaller change. Skipping the pin and using a DHCP reservation is already the README's other option (`infra/ansible/README.md:68`).
- **Compromise:** Two files that both set the same NIC can make `netplan try` fail or flap. The current move avoids that by being the only file.
- **Risk / blast radius:** Hosts that go through `configure_network` in `prep-node`. No CI test runs the remote script.
- **Type:** rigid.
- **Confidence:** medium (the shell is what the file contains; this audit did not run it on a node).

### F22. GPU hosts are NVIDIA-only

- **Location:** `infra/ansible/tasks/assert-gpu.yml:23`.
- **Problem:** A host in the `gpu` group must have a PCI vendor file equal to `0x10de`. The find follows symlinks, which is correct for `/sys/bus/pci/devices`. There is no other vendor, no "GPU present but unused" escape, and no scheduling logic yet. The schema allows the role so that a later service can select on it.
- **Evidence:**

```
infra/ansible/tasks/assert-gpu.yml:23-30
node_pci_vendor_ids.results | ...
| select('equalto', '0x10de') | list | length > 0
fail_msg: "gpu host {{ inventory_hostname }} has no NVIDIA device."
```

`ci/tests/test_pci_gpu.py` covers the symlink and the missing-vendor failure.

- **Effect:** An AMD or Intel GPU node cannot take the `gpu` role. Voice or local-model pods have nowhere to land in the schema except this role, so they wait on an NVIDIA node or a schema change.
- **Direction:** Keep the assert until a service schedules on `gpu`. When that service lands, decide whether the role means "NVIDIA" or "any GPU," and change the assert in that PR. Do not add a vendor matrix now.
- **Compromise:** A non-NVIDIA lab cannot use the role. Renaming it later touches inventory and any selector the service added.
- **Risk / blast radius:** The assert play in `site.yml`, which runs before any change. env-01 has no `gpu` host, so this play is a no-op there.
- **Type:** rigid.
- **Confidence:** high.

### F23. P0a reports success with an unchecked owner list

- **Location:** `tools/verify/p0a:228`.
- **Problem:** After Flux, the backup secret's key names, node role labels, and the edge load-balancer label, the command prints an owner checklist (second `site.yml` recap, second bootstrap, one seeded PR per CI check, a Renovate PR, a rollback log) and returns 0 whenever the automatic part passed. It also uses the ambient kubeconfig (F15) and does not read `/etc/jarvis/env`. The secret check prints key names, not values (`tools/verify/p0a:139-143`), which matches the contract.
- **Evidence:**

```
tools/verify/p0a:228-233
remaining = _print_checklist()
if failed:
    return 1
if remaining:
    _line(f"automatic checks passed; {remaining} owner items remain")
return 0
```

`_owner_items` (`tools/verify/p0a:197`) builds that list from `ci/rule-checks.txt` plus the manual lines. `ci/tests/test_verify_p0a.py` drives a fake kubectl. It does not claim the owner lines were performed.

- **Effect:** `make verify PHASE=p0a` is green on a cluster that has never had the seeded-PR drill or a clean second Ansible run. A kubeconfig for a different cluster with the same node names passes the node check.
- **Direction:** Keep the owner lines as a printed list. Exit 0 only for the automatic part if the command's first line says so, and compare a node annotation or the marker value to `--env` before trusting the context. Enrollment belongs in this command (F3).
- **Compromise:** Exiting non-zero until a human pastes a log makes the command unusable in a loop. The current exit code is what a script will trust.
- **Risk / blast radius:** The phase exit people use to decide the platform is done. `test_verify_p0a.py` locks the automatic failures (wrong labels, missing Flux).
- **Type:** brittle.
- **Confidence:** high.

### F24. Tag examples are not the command the prose just required

- **Location:** `infra/ansible/README.md:44`.
- **Problem:** The full playbook command, with both inventories and `jarvis_cluster_vars`, is at lines 32-35. The tag table then shows `ansible-playbook site.yml --tags base-os` and the same shape for `node-disk`, `k3s`, `flux-operator`, and `kubeconfig`. Line 60 says to pass the inventory and the vars as for a normal run, after the table.
- **Evidence:**

```
infra/ansible/README.md:44-46
| `base-os` | ... | `../../.venv/bin/ansible-playbook site.yml --tags base-os` |
| `node-disk` | ... | `... --tags node-disk -e "jarvis_wipe_confirm=..."` |
```

`site.yml:8-12` loads `jarvis_cluster_vars` and sets `ansible_user` from `ssh_user`. Without the env file, `ssh_user` falls through to `ubuntu` (`site.yml:10`) and cluster-vars such as `time_zone` are missing. The node-disk example also omits `-i`, so the wipe confirm has no `data_disk` group.

- **Effect:** A copy-paste from the table fails, or, with a default inventory, targets the wrong hosts. The wipe example is the dangerous one: it looks like the whole command and is not.
- **Direction:** Put the same `-i` / `-e` prefix on every example, or say the table adds flags to the command above and delete the partial lines.
- **Compromise:** The table gets wider. A partial example is how someone wipes with the wrong inventory.
- **Risk / blast radius:** Operator docs only. `ci/tests/test_ansible_operator_tags.py` covers the `kubeconfig` tag with a fixture inventory, not these shell lines.
- **Type:** brittle.
- **Confidence:** high.

### F25. The agent guardrails are real, and they are category-limited

- **Location:** `ci/checks/cli-deny:14`, `ci/checks/lib/_cli_permissions.py:342` and `:388`, `ci/checks/secret-scan:14`.
- **Problem:** Both checks can fail, and both have gaps that are properties of the design rather than a dead script.
  - The command guard is default-allow: with no Shell allow entries, a command the deny list does not match is allowed (`_cli_permissions.py:342-344`). It applies to the Cursor Shell and Read entries in `.cursor/cli.json`. The dry-run corpus runs inside `ci/checks/cli-deny` on every CI pass (`dry-run OK` in this run). The matcher is case-sensitive and the file already records a mixed-case resource-name gap (`_cli_permissions.py:388-390`). `ci/tests/test_cli_permissions.py` tests the matcher on small synthetic lists. It does not call `run_dry_run` on the repo file. The check executable does.
  - Secret scan is pinned gitleaks 8.30.1 with upstream rules and no repo config file. This run reported 120 commits and no leaks. `ci/tests/test_flux_entry.py::test_secret_scan_rejects_planted_age_key` shows a generated age key fails the same detector. The scan sees the git tree it is pointed at. Encoded or split material is outside what stock rules promise.
- **Evidence:** This audit's `make ci-verify` printed `dry-run OK` and gitleaks `no leaks found`. `test ! -f .gitleaks.toml`. The mixed-case note is the comment at `_cli_permissions.py:388-390`. No bypass string belongs in this file.
- **Effect:** An author who stays inside Shell, uses a shape the corpus lists, and commits plaintext that gitleaks knows, is stopped. An author who uses another tool channel, a case shape the comment already calls a gap, or a secret shape stock rules do not have, is not stopped by these two checks. `core-names` and review are the other layers.
- **Direction:** Keep the dry-run corpus in the check (it is the proof). Add one pytest that runs `run_dry_run` on `.cursor/cli.json` so a corpus edit fails in the unit file too. Leave secret scan on pinned upstream rules. Add a project rule only when a real secret shape has already been missed. Do not widen this document with examples.
- **Compromise:** A pytest of the full corpus duplicates the check's runtime. A custom gitleaks rule increases false positives on a repo whose secrets are already SOPS.
- **Risk / blast radius:** `.cursor/cli.json`, `ci/checks/cli-deny`, `ci/checks/secret-scan`. Agents are the users these exist for. Tightening deny entries can block the restore and verify commands the Makefile is supposed to run.
- **Type:** brittle.
- **Confidence:** high.

### F26. install-deps does not refresh apt before it installs

- **Location:** `tools/install-deps:97`.
- **Problem:** When `python3`, PyYAML, `ensurepip`, or bcrypt is missing, the script runs `sudo apt-get install -y` on those packages and does not run `apt-get update`. If the local index has no candidate, it exits with the apt command and never downloads the pinned CLIs.
- **Evidence:** On this machine, before `apt-get update`, `apt-cache policy python3-venv` showed `Candidate: (none)` and `python3 tools/install-deps` exited 1 with `apt-get install failed (exit 100)`. After `apt-get update`, the same script installed sops, age, kubectl, kustomize, kubeconform, and gitleaks. `tools/install-deps:97-116` is the install path; there is no `update` subcommand.
- **Effect:** A fresh or stale image fails `make deps` with a message that is accurate and incomplete. The retry works once the index exists. This is fail-closed, and it is a poor first command.
- **Direction:** Run `apt-get update` once in that branch, or print it in the hint next to the install line. Do not hide an apt failure.
- **Compromise:** Every `make deps` on a machine missing a system package hits the network twice. Machines that already have the packages skip the branch, as they do now.
- **Risk / blast radius:** `make deps` and CI's "Install pinned CLIs" step. CI runners usually have a fresh index. A developer image might not.
- **Type:** brittle.
- **Confidence:** medium.

## 4. Better-approach proposals

These are proposals, not work started. Each flexibility item names the use it serves.

### P1. Render the env flux entry from the schema

- **Replaces:** Hand-maintained copies of `flux/kustomization.yaml`, `flux-instance.yaml`, the four Kustomizations, and `platform-configs/kustomization.yaml` (F13, F14).
- **Serves:** A second env, and someone else's deployment.
- **Pros:** One template, the path `envs/<env>/` filled in, the ConfigMap render and the FluxInstance test become the same code path Ansible already renders.
- **Cons:** A generated tree that people also edit by hand will drift. The SOPS secret list in platform-configs is per env and has to stay data, not a copy of env-01's filenames.
- **Size:** M.

### P2. One Flux Kustomization per service, health on the few that others call

- **Replaces:** A single `./services` and a single `./apps` kustomization, and the decision to apply configs with no controller health (F12, F14).
- **Serves:** Services and apps landing, and a service that needs a different namespace, prune rule, or secret set.
- **Pros:** A bad service does not prune its neighbour. Kyverno and cert-manager can be Ready before the policy and the certificate. The placeholder ConfigMaps go away with the first service instead of living in `default` forever.
- **Cons:** More YAML per service. Health checks reintroduce the chart-pull stall the controllers file avoided, so only the two webhooks should block, not every chart.
- **Size:** M. Do it in the PR that adds the first service, not before.

### P3. Tie the data disk, the backup path, and the rebuild story together

- **Replaces:** The role constant `/var/lib/jarvis` plus an unspoken same-disk backup (F4, F5).
- **Serves:** Another disk layout, and a rebuild after disk loss on someone else's hardware.
- **Pros:** The schema becomes the place that says where bytes live. A test env can still decline a data disk, and the docs say that choice has no disk-failure copy.
- **Cons:** Existing env-01 is a legal "no data disk" layout and must keep working. The server token still has to be backed up by the owner; the repo should not gain a plaintext token.
- **Size:** S for the prefix check and the doc sentence. M if the mount path itself becomes an env key.

### P4. arch-specific k3s checksums in cluster-vars

- **Replaces:** The amd64 assert and the single `k3s_sha256` (F10).
- **Serves:** Other people's hardware, including an arm worker beside an amd64 server, which is a likely home-lab mix.
- **Pros:** The role already takes the hash from cluster-vars. A second hash and a URL suffix is a small branch.
- **Cons:** Every k3s bump updates two hashes. The Traefik chart line has to say it was only checked on amd64 until someone checks arm.
- **Size:** M. Until then, one sentence that nodes are amd64 is the S version.

### P5. Leave the single control plane alone

- **Replaces:** Nothing yet. This is the proposal not to build F9.
- **Serves:** A lost control-plane node. Workers are already a separate path.
- **Pros of waiting:** Embedded etcd membership, the server token, and the backup nodeSelector stay simple on the hardware this repo runs.
- **Cons of waiting:** The one server is a single failure domain. etcd snapshots do not remove that until the token and the restic disk are off the machine (F5).
- **Size:** L when it is actually needed. S to document the ceiling now.

### P6. Do not move DNS forwarding or Tailscale into the cluster

- **Replaces:** Nothing. The design uses the home router for both, on purpose.
- **Serves:** Remote access and name resolution for every deployment.
- **Pros of leaving them outside:** No public controller, no extra daemon, no tailnet identity in Git. App DNS already answers the env suffix inside the LAN once the router forwards it.
- **Cons:** Every adopter must configure a router and a tailnet. The repo cannot smoke-test that.
- **Size:** S, and it is documentation (section 8), not a component.

## 5. Keep list

Leave these alone.

- **Env schema as the gate for `envs/<env>/`.** Unknown keys, singleton roles, CIDRs, the App DNS address (not network, not broadcast, not the first or the tenth service address), IANA time zones, k3s version shape, shared owner handle across non-example envs, and one `.sops.yaml` rule per non-example env. `ci/checks/env-schema` and `ci/tests/test_env_schema.py` are the right size of proof.
- **Identity scan.** `core-names` reads identity from non-example envs and from `OWNER_NAME`, fails closed when the variable is empty, and the failure text does not contain the value. Example env `env-00` is not an identity source. Per-owner files (`envs/env-01/**`, `.mailmap`, the repository variable) are the right place for that data.
- **SOPS handling in `tools/lib/_sops.py` and `tools/seal`.** Plaintext goes to sops on stdin. A failed encrypt leaves no target file. `seal` requires a Secret and an explicit force to rotate. Bootstrap does not overwrite. This audit's metadata pass found one age recipient, `encrypted_regex: ^(data|stringData)$`, a mac, and encrypted leaves on every `*.sops.yaml`.
- **Public CA only at `envs/<env>/ca.crt`.** Bootstrap's certificate writer refuses a private key in that file (`tools/bootstrap-secrets` `_public_certificate`). The committed file is a certificate, not a key.
- **GitOps bootstrap split.** Ansible installs the Flux operator and the FluxInstance only when they are absent, then Git reconciles them. The chart tag is read from `platform/flux-operator/oci-repository.yaml` (`chart_pin.yml`). The committed FluxInstance is tested against the Ansible template. Rebuild hold is created only for a new cluster that already has restic repos, and a later run does not refresh it. That is the right one-shot.
- **k3s foreign-install guard.** A host with k3s and no matching `/etc/jarvis/env` is refused (`infra/ansible/roles/k3s/tasks/guard.yml`).
- **Two inventories.** Env hosts plus `infra/inventory/roles.yml`, not a copy of the role groups inside each env. `data_disk` exists even when empty so the play matches nothing instead of warning.
- **Node labels and edge load-balancer.** `role.<dns_suffix>/<role>=true`, and `enablelb=true` only on `edge`. The label task removes stale role labels and enablelb when a node leaves the group. P0a checks the result.
- **Backup policy shape.** One ClusterPolicy, generate on bind, opt out with `backup: none` only on the two observability prefixes, no ReplicationSource in Git, restic URL prefixes `ns-` and `pvc-` to avoid rest-server's reserved names, prune disabled on the rest-server Deployment and on the VolumeSnapshot CRD. The CRD header points at external-snapshotter v8.4.0 and matches that story (Direct copy, no snapshot controller).
- **Image digest pins on pod containers and HelmRelease images, Renovate groups, and k3s bumps disabled.** `renovate.json` groups VolSync, Kyverno, cert-manager, and the Flux distribution, and turns k3s and the Helm CLI off so a bump stays a reviewed PR.
- **Base OS upgrade policy.** Unattended upgrades are installed and inert. Automatic reboot is off. Journald is capped. Chrony uses NTS from `ntp_pools`.
- **Testapp as a canary with no PVC.** It does not enrol a backup, it does not depend on `services/`, and the body hash is fixed. Keep it until something else answers `testapp.<dns_suffix>`.
- **Placeholder ConfigMaps.** `apps/placeholder.yaml` and `services/placeholder.yaml` are what make the Flux paths non-empty. Deleting them without a replacement breaks the kustomization.
- **Command-guard dry-run corpus and gitleaks pin.** They ran in this CI and they fail on the cases they claim (F25). Keep the corpus.
- **`ci/run-checks` expected-list guard.** `ci/rule-checks.txt` must match the executable files in `ci/checks/` exactly.

## 6. Delete list

Nothing tracked should be deleted as unused.

- `apps/placeholder.yaml`, `services/placeholder.yaml`, and `platform/testapp/` are load-bearing (see the keep list). `rg placeholder` shows Flux points at those directories.
- `envs/env-00/` is the example the schema requires. It is not a leftover deploy.
- The future paths in `EXPLICIT_COMPONENT_ROOTS` (`docs/build-agent`, Hermes deploy paths, `services/observability`) are absent on disk. The check ignores missing directories. Deleting the names would drop the requirement that those directories arrive with a `CONTRACT.md`.
- `storage` as a node role has no scheduler yet. The schema says `backup` and `storage` are labels until a service selects them. Removing `storage` now means the first storage service re-adds the role and the example inventory in one PR. Leave it.
- On the node, outside Git, `platform/backups/CONTRACT.md:60` still names `${backup_path}/volsync-test` as a directory to remove by hand. That is not a repo file.

No duplicate test file turned up as a pure restatement that should be deleted. The weakest tests copy a structure the suite already enforces elsewhere (`test_flux_operator.py` locks `site.yml` play order; `test_prune_guards.py` lists objects that must set prune disabled). They fail when that order or that annotation changes, which is a rule, not a copy of a task list. Leave them until a behaviour test replaces one.

## 7. Ready for growth?

What hurts first, in the order it will be hit.

1. **The first service PR** hits F14. There is no rendered form of `hermes.yaml` or `apps.yaml`, the secrets for that service already exist and are not applied, and `./services` is one prune unit. Smallest change that heads it off: in that PR, add one Flux Kustomization per service, point it at the secret it needs, and decide in the same PR whether the env file is rendered to a ConfigMap or mounted from the env flux tree. Do not grow the placeholder.
2. **The first PVC** hits F3 and F12. Enrollment can lag admission, and platform-configs does not wait for Kyverno. Smallest change: a verify item that lists bound PVCs missing a ReplicationSource, run after install. Keep `failurePolicy: Ignore`.
3. **A second node** that is a worker is fine. A second node that is a server, an arm board, or a non-NVIDIA GPU hits F9, F10, and F22. Smallest change: document those three ceilings in the deploy note so the inventory edit is not the way people discover them. Implement arm checksums only when a board is actually joining.
4. **A second env** hits F13, F15, and F18. The flux tree is copied by hand, kubeconfig is a single file, and the restic URL is sealed at bootstrap. Smallest change: P1's file list written down, plus a per-env kubeconfig path (F15). A generator can wait until the second env is real.
5. **A second person, or a second user of the same Jarvis,** is not represented anywhere except "the admin is the only user" in `envs/env-01/README.md:7` and the owner handle shared across envs. Nothing in the schema is per user. Smallest change: do nothing in this foundation until a service has a user id. Do not add a user table to cluster-vars in advance. When it comes, it belongs in that service's env file, not in core.

The etcd cron, journald cap, and Flux `size: medium` (`flux-instance.yaml.j2:18`) are fixed and acceptable until a measured env needs otherwise. They are not the first break.

## 8. Ready for other people?

Walk from a fresh copy, using the repo only. Nothing was deployed.

**Prerequisites this repo does not implement, and should not:**

- The home router forwards DNS for the env's suffix to the edge node. App DNS and ServiceLB assume that. Do not move forwarding into the cluster.
- Remote access is Tailscale or the home router. Cluster Services stay on the LAN or the tailnet. Do not install Tailscale here.
- Ubuntu Server nodes, SSH from the controller, hostnames that resolve before `prep-node` (the script refuses otherwise).
- An age key. Public half in `.sops.yaml`. Private half only on the controller (`age_key_file` or `SOPS_AGE_KEY_FILE`).
- Two https Git repos on the chosen host (`code_delivery: github` is the only value that validates). Flux pulls without a deploy key.
- Outbound HTTPS for Git, images, and the k3s binary. Chrony needs NTS: UDP 123 and TCP 4460 to `ntp_pools` (`infra/ansible/roles/base-os/templates/chrony.conf.j2:2`).
- Nodes are amd64 today (F10).

**Where someone gets stuck, most costly first:**

1. **No deploy page.** `README.md` and `docs/README.md` point at `<owner>/project-plans`. Smallest fix: one page in this repo with the prerequisites above and the command order `make deps`, `prep-node`, `bootstrap-secrets`, commit, `site.yml`. Files opened before this is obvious: 2, and they lead out of the repo.
2. **Which env to copy.** env-00 will not install (`v0.0.0+k3s0`, no flux). env-01 says do not copy it, and its flux tree is the only complete one (F13). Smallest fix: publish the file list (section 7's P1 inputs) and say identity, secrets, and `ca.crt` are never copied. Files opened: `envs/env-00/values/cluster-vars.yaml`, `envs/env-01/README.md`, then eight files under `envs/env-01/flux/`.
3. **Disk and backup mean different things than the keys suggest** (F4, F5). Smallest fix: two sentences on the deploy page. A `data_disk` is formatted at `/var/lib/jarvis`. No `data_disk` means restic shares the OS disk and does not survive that disk. The k3s server token is backed up with the age key if etcd restore matters.
4. **`make ci-verify` and GitHub CI** need `OWNER_NAME` (F20). Smallest fix: the `make help` line, and a note to set the Actions repository variable. The check is right to fail without it.
5. **Tag table** (F24). Smallest fix: complete the examples. One file: `infra/ansible/README.md`.
6. **`k3s_sha256` and `app_dns_cluster_ip`.** The schema checks the hash shape and the IP rules, and the new-env steps do not say how to choose them. Smallest fix: "sha256 of the amd64 `k3s` binary for `k3s_version`; App DNS address inside `service_cidr`, not .1 and not .10." The schema text at `schema/CONTRACT.md:50-53` already has the rule. Point at it.
7. **Age key path.** `envs/env-01/secrets/README.md:15` says bootstrap does not read `age_key_file` and that `SOPS_AGE_KEY_FILE` must be set for that env's non-default path. A new env on the default path can miss that distinction. Smallest fix: one sentence in the deploy page. Encryption uses the public key in `.sops.yaml`. Decryption and `site.yml` use the private file.
8. **Hardware that is not this env.** arm (F10), second server (F9), non-NVIDIA GPU (F22), multi-NIC netplan (F21), cluster domain other than `cluster.local` (F18). Smallest fix: list them as current limits on the same page. Do not implement them in the doc PR.
9. **Contracts that over-promise** (F17). An adopter who reads `docs/code-delivery/CONTRACT.md` will look for `admin-gate` and an image registry. Smallest fix: mark those offers as later. Files opened: that contract plus `ci/rule-checks.txt`.

Per-owner values under `envs/env-01/`, `.mailmap`, and `OWNER_NAME` are expected. Replacing them is easy once the file list exists, and hard while the only instructions are "do not copy this folder."

## 9. Coverage

215 tracked files. `finding` means a finding above names the file. `no finding` means it was read or checked and not given its own finding. Vendored and generated files were checked against their source. `*.sops.yaml` had a metadata check only (version, encrypted regex, recipient count, mac, encrypted leaves). They were not decrypted.

| File | Tag |
| --- | --- |
| .cursor/cli.json | finding |
| .github/pull_request_template.md | no finding |
| .github/workflows/ci.yml | finding |
| .gitignore | no finding |
| .mailmap | no finding |
| .sops.yaml | no finding |
| AGENTS.md | no finding |
| Makefile | finding |
| README.md | finding |
| apps/kustomization.yaml | finding |
| apps/placeholder.yaml | finding |
| ci/checks/cli-deny | finding |
| ci/checks/contract-md | no finding |
| ci/checks/core-names | no finding |
| ci/checks/digest-pin | no finding |
| ci/checks/env-schema | no finding |
| ci/checks/lib/_cli_permissions.py | finding |
| ci/checks/lib/_contract_md.py | finding |
| ci/checks/lib/_core_names.py | finding |
| ci/checks/lib/_digest_pin.py | finding |
| ci/checks/lib/_loadbalancer.py | no finding |
| ci/checks/lib/_manifest_lint.py | finding |
| ci/checks/lib/_manifests.py | finding |
| ci/checks/lib/_product_ci.py | no finding |
| ci/checks/lib/_pvc_backup.py | finding |
| ci/checks/loadbalancer | no finding |
| ci/checks/manifest-lint | no finding |
| ci/checks/product-ci | no finding |
| ci/checks/pvc-backup | no finding |
| ci/checks/secret-scan | finding |
| ci/ci-local-verify | finding |
| ci/rule-checks.txt | no finding |
| ci/run-checks | no finding |
| ci/tests/ansible_role_proof.py | no finding |
| ci/tests/conftest.py | no finding |
| ci/tests/fixtures/ansible-list-tasks/hosts.yml | no finding |
| ci/tests/fixtures/digest-pin/tag-without-digest.yaml | no finding |
| ci/tests/fixtures/gpu-assert/hosts.yml | no finding |
| ci/tests/fixtures/gpu-assert/play.yml | no finding |
| ci/tests/fixtures/loadbalancer/extra-service.yaml | no finding |
| ci/tests/fixtures/manifest-lint/missing-memory-limit.yaml | no finding |
| ci/tests/fixtures/node-disk/case.yml | no finding |
| ci/tests/fixtures/node-disk/refusals.yml | no finding |
| ci/tests/fixtures/node-disk/service.yml | no finding |
| ci/tests/fixtures/schemas/apiextensions.k8s.io/customresourcedefinition_v1.json | no finding |
| ci/tests/fixtures/schemas/helm.cattle.io/HelmChartConfig_v1.json | no finding |
| ci/tests/fixtures/schemas/volsync.backube/replicationdestination_v1alpha1.json | no finding |
| ci/tests/fixtures/schemas/volsync.backube/replicationsource_v1alpha1.json | no finding |
| ci/tests/test_ansible_operator_tags.py | no finding |
| ci/tests/test_app_dns.py | no finding |
| ci/tests/test_backup_enrollment.py | finding |
| ci/tests/test_base_os.py | no finding |
| ci/tests/test_bootstrap_secrets.py | no finding |
| ci/tests/test_cert_manager.py | no finding |
| ci/tests/test_cli_permissions.py | finding |
| ci/tests/test_contract_md.py | no finding |
| ci/tests/test_core_names.py | no finding |
| ci/tests/test_digest_pin.py | no finding |
| ci/tests/test_env_schema.py | no finding |
| ci/tests/test_flux_entry.py | finding |
| ci/tests/test_flux_operator.py | no finding |
| ci/tests/test_flux_operator_install.py | no finding |
| ci/tests/test_install_deps.py | no finding |
| ci/tests/test_inventory_roles.py | no finding |
| ci/tests/test_k3s.py | no finding |
| ci/tests/test_kubeconfig.py | no finding |
| ci/tests/test_loadbalancer.py | no finding |
| ci/tests/test_manifest_lint.py | no finding |
| ci/tests/test_node_disk.py | no finding |
| ci/tests/test_pci_gpu.py | no finding |
| ci/tests/test_prep_node.py | finding |
| ci/tests/test_product_ci.py | no finding |
| ci/tests/test_prune_guards.py | no finding |
| ci/tests/test_pvc_backup.py | no finding |
| ci/tests/test_renovate.py | no finding |
| ci/tests/test_restic_rest_server.py | finding |
| ci/tests/test_restore.py | finding |
| ci/tests/test_testapp.py | no finding |
| ci/tests/test_traefik.py | finding |
| ci/tests/test_verify_p0a.py | no finding |
| ci/tests/test_verify_p0b.py | no finding |
| docs/CONTRACT.template.md | no finding |
| docs/README.md | finding |
| docs/code-delivery/CONTRACT.md | finding |
| envs/env-00/inventory/hosts.yml | no finding |
| envs/env-00/values/apps.yaml | finding |
| envs/env-00/values/cluster-vars.yaml | finding |
| envs/env-00/values/hermes.yaml | no finding |
| envs/env-01/README.md | finding |
| envs/env-01/ca.crt | no finding |
| envs/env-01/flux/flux-instance.yaml | no finding |
| envs/env-01/flux/generated/cluster-vars-configmap.yaml | no finding |
| envs/env-01/flux/kustomization.yaml | no finding |
| envs/env-01/flux/kustomizations/apps.yaml | finding |
| envs/env-01/flux/kustomizations/platform-configs.yaml | finding |
| envs/env-01/flux/kustomizations/platform-controllers.yaml | finding |
| envs/env-01/flux/kustomizations/services.yaml | finding |
| envs/env-01/flux/platform-configs/kustomization.yaml | finding |
| envs/env-01/inventory/hosts.yml | finding |
| envs/env-01/secrets/README.md | finding |
| envs/env-01/secrets/backups-restic.sops.yaml | no finding |
| envs/env-01/secrets/cert-manager-ca.sops.yaml | no finding |
| envs/env-01/secrets/grafana-admin.sops.yaml | no finding |
| envs/env-01/secrets/hermes-code-factory-api.sops.yaml | no finding |
| envs/env-01/secrets/hermes-code-factory-dashboard.sops.yaml | no finding |
| envs/env-01/secrets/hermes-ops-api.sops.yaml | no finding |
| envs/env-01/secrets/hermes-ops-dashboard.sops.yaml | no finding |
| envs/env-01/values/apps.yaml | no finding |
| envs/env-01/values/cluster-vars.yaml | no finding |
| envs/env-01/values/hermes.yaml | finding |
| infra/ansible/README.md | finding |
| infra/ansible/ansible.cfg | no finding |
| infra/ansible/group_vars/all.yml | no finding |
| infra/ansible/roles/base-os/defaults/main.yml | no finding |
| infra/ansible/roles/base-os/handlers/main.yml | no finding |
| infra/ansible/roles/base-os/tasks/main.yml | no finding |
| infra/ansible/roles/base-os/templates/20auto-upgrades.j2 | no finding |
| infra/ansible/roles/base-os/templates/50unattended-upgrades.j2 | no finding |
| infra/ansible/roles/base-os/templates/chrony.conf.j2 | no finding |
| infra/ansible/roles/base-os/templates/journald-jarvis.conf.j2 | no finding |
| infra/ansible/roles/base-os/templates/resolved-stub.conf.j2 | no finding |
| infra/ansible/roles/flux-operator/defaults/main.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/chart_pin.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/helm_cli.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/instance.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/main.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/operator.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/rebuild_hold.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/sops_age.yml | no finding |
| infra/ansible/roles/flux-operator/templates/backup-rebuild-hold.yaml.j2 | no finding |
| infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2 | finding |
| infra/ansible/roles/flux-operator/templates/helm-upgrade.cmd.j2 | no finding |
| infra/ansible/roles/flux-operator/templates/sops-age-secret.yaml.j2 | no finding |
| infra/ansible/roles/k3s/defaults/main.yml | finding |
| infra/ansible/roles/k3s/handlers/main.yml | no finding |
| infra/ansible/roles/k3s/tasks/guard.yml | no finding |
| infra/ansible/roles/k3s/tasks/labels.yml | no finding |
| infra/ansible/roles/k3s/tasks/main.yml | finding |
| infra/ansible/roles/k3s/tasks/token.yml | no finding |
| infra/ansible/roles/k3s/templates/agent-config.yaml.j2 | no finding |
| infra/ansible/roles/k3s/templates/config.yaml.j2 | finding |
| infra/ansible/roles/k3s/templates/k3s.service.j2 | no finding |
| infra/ansible/roles/k3s/templates/label-patches.json.j2 | no finding |
| infra/ansible/roles/kubeconfig/tasks/main.yml | finding |
| infra/ansible/roles/node-disk/handlers/main.yml | no finding |
| infra/ansible/roles/node-disk/tasks/guards.yml | finding |
| infra/ansible/roles/node-disk/tasks/k3s-service.yml | no finding |
| infra/ansible/roles/node-disk/tasks/main.yml | finding |
| infra/ansible/roles/node-disk/templates/k3s-mount.conf.j2 | no finding |
| infra/ansible/roles/node-disk/vars/main.yml | finding |
| infra/ansible/site.yml | finding |
| infra/ansible/tasks/assert-gpu.yml | finding |
| infra/inventory/roles.yml/constructed.yml | no finding |
| infra/inventory/roles.yml/groups.yml | no finding |
| platform/app-dns/CONTRACT.md | no finding |
| platform/app-dns/coredns-custom.yaml | no finding |
| platform/app-dns/helmrelease.yaml | no finding |
| platform/app-dns/helmrepository.yaml | no finding |
| platform/app-dns/kustomization.yaml | no finding |
| platform/app-dns/namespace.yaml | no finding |
| platform/backups/CONTRACT.md | finding |
| platform/backups/controller/helmrelease.yaml | no finding |
| platform/backups/controller/helmrepository.yaml | no finding |
| platform/backups/controller/kustomization.yaml | no finding |
| platform/backups/controller/namespace.yaml | no finding |
| platform/backups/controller/volumesnapshot-crd.yaml | excluded: vendored CRD, header checked against external-snapshotter v8.4.0 only |
| platform/backups/enrollment.yaml | finding |
| platform/backups/etcd-snapshots.yaml | no finding |
| platform/backups/kustomization.yaml | no finding |
| platform/backups/namespace.yaml | no finding |
| platform/backups/rest-server.yaml | no finding |
| platform/cert-manager/CONTRACT.md | finding |
| platform/cert-manager/config/internal-ca.yaml | no finding |
| platform/cert-manager/config/kustomization.yaml | no finding |
| platform/cert-manager/controller/helmrelease.yaml | no finding |
| platform/cert-manager/controller/helmrepository.yaml | no finding |
| platform/cert-manager/controller/kustomization.yaml | no finding |
| platform/cert-manager/controller/namespace.yaml | no finding |
| platform/flux-operator/CONTRACT.md | no finding |
| platform/flux-operator/helmrelease.yaml | no finding |
| platform/flux-operator/kustomization.yaml | no finding |
| platform/flux-operator/oci-repository.yaml | no finding |
| platform/kustomization.yaml | no finding |
| platform/kyverno/CONTRACT.md | no finding |
| platform/kyverno/helmrelease.yaml | no finding |
| platform/kyverno/helmrepository.yaml | no finding |
| platform/kyverno/kustomization.yaml | no finding |
| platform/kyverno/namespace.yaml | no finding |
| platform/kyverno/rbac.yaml | no finding |
| platform/testapp/CONTRACT.md | finding |
| platform/testapp/kustomization.yaml | no finding |
| platform/testapp/namespace.yaml | no finding |
| platform/testapp/testapp.yaml | no finding |
| platform/traefik/CONTRACT.md | finding |
| platform/traefik/helmchartconfig.yaml | finding |
| platform/traefik/kustomization.yaml | no finding |
| pytest.ini | no finding |
| renovate.json | no finding |
| requirements-dev.txt | no finding |
| rulebooks/coding-agents.md | finding |
| schema/CONTRACT.md | finding |
| schema/__init__.py | no finding |
| schema/env.py | finding |
| services/kustomization.yaml | finding |
| services/placeholder.yaml | finding |
| tools/bootstrap-secrets | finding |
| tools/install-deps | finding |
| tools/lib/_sops.py | no finding |
| tools/lib/render_cluster_vars_configmap.py | finding |
| tools/prep-node | finding |
| tools/restore | finding |
| tools/seal | no finding |
| tools/verify/p0a | finding |
| tools/verify/p0b | no finding |
| tools/versions.env | finding |

SOPS metadata (no ciphertext recorded): all seven `envs/env-01/secrets/*.sops.yaml` files have one age recipient, `encrypted_regex` `^(data|stringData)$`, a mac, and encrypted stringData leaves. `backups-restic.sops.yaml` records sops 3.13.3, the same as `tools/versions.env`. The other six record 3.10.2. That is a version stamp inside the file, not a plaintext leak. The generated ConfigMap matches `python3 tools/lib/render_cluster_vars_configmap.py --env env-01` (`diff` was empty).

## 10. Proposed PR sequence

Impact order. Sizes are relative to this repo, not calendar time. Do not mix a behaviour change into the doc PR.

1. **S. Fix prep-node's checker (F1).** Point it at the real env-schema entry and test a bad edit. One file plus a test.
2. **S. Scan `${…}` (F8).** One check, one seeded typo, wired through `ci/rule-checks.txt`. Independent of F1.
3. **S. Docs only, one PR.** Deploy page and limits (F13, F5's warning, F9, F10, F20, F22, F24, section 8 prerequisites, F17's "later" marks, F6's reissue sentence in the cert-manager contract). No manifest edits.
4. **S. Data-path prefix check (F4).** Schema rejects `backup_path` and `local_path_data` outside `/var/lib/jarvis` when any host has `data_disk`. Document the constant. Leave env-01, which has no data disk, valid.
5. **S. Guardrail tidy (F11, F19, F25).** HelmChartConfig image scan, one pytest that runs the existing cli-deny dry-run, and a shared `SCAN_ROOTS` import. Seeded cases stay.
6. **M. Restore safety (F2, F16).** Context check, interrupt prints the same undo list, test double returns a Deployment and expects a scale. Own PR: it changes a data-moving tool.
7. **S. Verify enrollment (F3, F23).** P0a (or a sibling) lists bound PVCs with no ReplicationSource, and prints that owner lines are not automatic. Keep `failurePolicy: Ignore`.
8. **S. Kubeconfig per env (F15).** Own small PR before a second env exists.
9. **S. kubectl minor test (F18).** Assert `tools/versions.env` matches the non-example `k3s_version` minor. Leave the etcd cron and `cluster.local` until an env needs them.
10. **S. install-deps apt update hint or command (F26).**
11. **M. First service, when it exists (F14, P2).** Per-service Kustomization, secret wiring, and the hermes/apps pipe. Not before that service.
12. **M. arm64 k3s checksums (F10, P4).** Only when a non-amd64 node is in scope.
13. **L. Second control plane (F9, P5).** Own design. Do not start it from this audit.

P3's mount-as-env-key can replace step 4 if a real disk is not under `/var/lib/jarvis`. P1 (generate the flux entry) can replace the file-list part of step 3 once a second env is being added. P6 is the decision not to add a PR.
