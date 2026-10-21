# Audit: audit-fresh-env

Walkthrough of a new env from this repo alone, read from `main`. No env was added.

Sketch used while reading: three nodes; the controller is the control-plane node; login user `ops` with a home directory other than the image default; a dns suffix and git remote that differ from env-01; data disks on the control plane and the worker.

## Findings

### 1. Kubeconfig fetch never reads the k3s kubeconfig

**Where:** `infra/ansible/roles/kubeconfig/tasks/main.yml` lines 3–22. The on-node path is `infra/ansible/group_vars/all.yml` line 3.

**What:** The block redefines `k3s_kubeconfig_path` to the controller HOME plus `/.kube/config`, and the fetch uses that string as the remote source. That shadows `/etc/rancher/k3s/k3s.yaml` for every task in the block, including the copy back onto the controller. On ansible-core 2.20 the rendered source is the home path. A fresh node does not have that file, and the later replace only rewrites `127.0.0.1`, which is the address inside the k3s file this task never opens.

**Fix:** Keep a separate variable for the runner destination under HOME. Fetch `src` from the group-vars path.

**Why:** This play sits immediately before flux-operator. Ansible drops a failed host from later plays, so k3s can be up and Flux never installed. The destination should follow the controller user, including a non-default home. Today that same value is the remote source, so the fetch looks for a path k3s does not write.

**Impact:** high

### 2. Other nodes cannot join unless names already resolve, and the seed cannot say how to connect

**Where:** `infra/ansible/roles/k3s/tasks/token.yml` line 15; `infra/ansible/roles/k3s/templates/k3s.service.j2` line 23; `infra/ansible/site.yml` lines 9 and 51; `schema/env.py` lines 472–481; `infra/ansible/ansible.cfg` lines 6–9.

**What:** Agents join `https://` plus the control-plane inventory hostname plus `:6443`. No task writes resolver data, and any host key besides `node_roles` and `data_disk` is rejected, so an address or a connection override cannot be stored. `TimeoutStartSec=0` plus a systemd start that waits until the unit is active means an unresolved name hangs the play. The controller still SSHes to every inventory hostname as `ssh_user` and becomes root, including when the controller is that control-plane node. The Ansible config never sets host-key handling.

**Fix:** State the preconditions in `infra/ansible/README.md`: every inventory hostname resolves on the controller and on every node, `ssh_user` has key auth and passwordless sudo, and known_hosts is already seeded. Before starting an agent, fail if the control-plane name does not resolve, and give the unit a finite start timeout. Use a local connection for the host that is this machine; the schema will not accept that as a host setting.

**Why:** env-01 has no second node, so the agent play matches nothing and this never runs. Name service for the env is not part of bootstrap; the playbook is what has to join the nodes. A two- or three-node first run either hangs or stops on SSH, and the repo does not say what had to exist first.

**Impact:** high

### 3. The only new-env instruction copies a tree that cannot belong to the new env

**Where:** `envs/env-01/README.md` line 18; `tools/bootstrap-secrets` lines 209–211; `.sops.yaml` lines 1–3; `envs/env-01/flux/kustomizations/platform-configs.yaml` line 8; `schema/CONTRACT.md` lines 7–11 and 96–97; `tools/tests/test_flux_entry.py` lines 118–143.

**What:** The env README says to copy the env-01 folder. The copy includes SOPS files and a flux tree. bootstrap-secrets skips a secret that already exists, and `.sops.yaml` has a creation rule only for env-01, which `validate_env_tree` never checks. The committed FluxInstance is drift-checked against cluster-vars, but `spec.path` on the platform-configs Kustomization is not, so it can keep targeting env-01 after CI is green. The example env has no `flux/` and no secrets, and the schema's required set is four files.

**Fix:** Replace the copy sentence with a create-from-empty procedure: new values and inventory, one new `.sops.yaml` age rule, no copied secrets, `make bootstrap-secrets`, render the ConfigMap, and a flux tree whose git paths use the new env name. Fail env-schema when a non-example env lacks that rule, or when any `envs/<name>/` path under its `flux/` is not its own name.

**Why:** Copied secrets stay encrypted to the original recipient, so the new cluster cannot decrypt them. A stale platform-configs path makes the new cluster reconcile env-01's overlay, including that env's restic secret. The four-file schema never produces a flux entry, and the later test error is a missing file rather than a list of what to add.

**Impact:** high

### 4. Two new data disks cannot be wiped in one run, and the other node drops out of k3s

**Where:** `infra/ansible/roles/node-disk/tasks/guards.yml` lines 16–22; `infra/ansible/README.md` line 21; `infra/ansible/site.yml` lines 26–35.

**What:** `jarvis_wipe_confirm` must equal one `hostname:disk-id`. Every other host that has a data disk and does not already carry the jarvis-data label fails that assert. The matching host continues into `wipefs`. Failed hosts are removed from later plays. Nothing gathers the set of disks that still need a format before the first wipe.

**Fix:** Accept a list of `hostname:disk-id` pairs. Before any `wipefs`, require that list to equal the set of hosts that would format, and fail with that set if it does not.

**Why:** A new env whose control plane and worker both have empty data disks cannot finish the documented `site.yml` command. One disk can be wiped in a run that still exits failed, and the other node never reaches k3s. env-01 declares no data disk, so this path does not run there. The example inventory declares several.

**Impact:** high

### 5. Every site.yml re-run needs the age private key the secrets doc says to keep offline

**Where:** `infra/ansible/roles/flux-operator/tasks/sops_age.yml` lines 17–44; `envs/env-01/secrets/README.md` line 15; `envs/env-01/README.md` line 16.

**What:** The key-file asserts and the file read are not limited to the first create. A later run still requires `SOPS_AGE_KEY_FILE` on the controller and stops if the contents differ. The secrets README says encryption does not need the private key and that the owner keeps it offline. The env README says to re-run `site.yml` after merge, and that run includes the flux-operator play.

**Fix:** If `flux-system/sops-age` already exists and `SOPS_AGE_KEY_FILE` is unset, leave the secret and continue. If the variable is set, keep the mismatch assert. Say in the ansible README that the private key has to be on the controller for the first flux-operator run.

**Why:** An admin who follows both READMEs cannot re-run the playbook. The assert text is clear when it fires. The written steps are what cause the failure. The ansible README also never mentions the variable for the first run.

**Impact:** medium

### 6. Dropping the constructed inventory turns every node into an agent

**Where:** `infra/ansible/site.yml` lines 37–61; `infra/ansible/roles/k3s/tasks/token.yml` line 6; `infra/inventory/roles.yml`.

**What:** Role groups exist only when `infra/inventory/roles.yml` is the second inventory. Without it, `--list-hosts` still exits 0, the server, label, kubeconfig, and flux plays match no hosts, and `all:!control_plane` matches every host. The agent role then fails with `object of type 'dict' has no attribute 'control_plane'`, after base-os and after the env marker is written.

**Fix:** In the first play, assert that the `control_plane` group contains exactly one host, and name `infra/inventory/roles.yml` in the fail message.

**Why:** The playbook README shows the second `-i`. The schema also describes `inventory/hosts.yml` as the inventory, and a run that passes only that file looks successful at list time. The error does not name the missing file. A one-node env is not spared: that node is targeted as an agent and no server is installed.

**Impact:** medium

### 7. Workers never get the kubelet settings, and backup and storage do not place data

**Where:** `infra/ansible/roles/k3s/templates/agent-config.yaml.j2` lines 1–4; `infra/ansible/roles/k3s/templates/config.yaml.j2` lines 13–16; `schema/CONTRACT.md` lines 84–93. No play in `infra/ansible/site.yml` targets `backup`, `storage`, or `workers`.

**What:** Kubelet reserved and eviction values are written only into the server config. Agent config sets the server URL, the token, and the node name. `backup` and `storage` become labels and nothing else. Etcd snapshots go to `backup_path` on the control plane. A data disk on the backup or storage node is not where those snapshots, or any current workload, land.

**Fix:** Render the same three kubelet arguments into the agent template. In the schema role section, state that `backup` and `storage` are labels only and that etcd snapshots stay on the control plane. Different node sizes still need a schema change: inventory rejects any host key beyond `node_roles` and `data_disk`, so a per-node reservation cannot be set.

**Why:** env-01's only node is the server, so the agent template never applies. A worker in the new env keeps k3s defaults and will not reserve or evict the way cluster-vars says. The example inventory puts a data disk on the backup host; that disk does not receive etcd snapshots.

**Impact:** medium

## Fine

- Core paths do not embed env-01's hostname, dns suffix, or repo owner. `tools/checks/core-names` enforces that, and the role defaults take version, CIDRs, and repo URLs from cluster-vars.
- Missing keys, unknown roles, and a missing `control_plane`, `backup`, or `edge` host fail env-schema with the field name and the allowed set.
- A gpu host with no NVIDIA device fails in the first play, before base-os changes the node. The edge role is the only one that sets the k3s load-balancer label. A one-node inventory skips the agent play.
- For an env that already has a flux tree, the committed FluxInstance and the generated cluster-vars ConfigMap are drift-checked against the template and the renderer. The agent token handoff registers on the first agent host, which is where the next task reads it (ansible-core 2.20).

## Appendix

- `pod_cidr`, `service_cidr`, `time_zone`, and `k3s_version` are only checked as non-empty strings, so a bad CIDR survives CI and fails after base-os, and after a wipe if one was confirmed.
- `make deps` installs Ansible into `.venv/bin`. `infra/ansible/README.md` invokes `ansible-playbook` as if it were on `PATH`.
- base-os and the node-label task use apt. The k3s role rejects non-amd64 with a clear assert. Neither limit is stated next to the playbook command.
- FluxInstance has no git credentials. A private remote never syncs. The cluster-vars comment says the URL is public, and nothing checks that before `site.yml`.
- A fork's CI stays red until the `OWNER_NAME` repository variable is set. The check says it is unset. The root README does not.
- The data-disk mount point is fixed in `infra/ansible/roles/node-disk/vars/main.yml`. The role does not reject `backup_path` or `local_path_data` outside that mount.

## Unsure

- A second non-example env must use the same git owner handle as env-01. The error names both envs. A new owner who leaves env-01's URLs in place cannot point the new env at another account. That may be the rule you want.
- k3s state stays on the root disk when a data disk is declared. The role only waits for the jarvis paths, so this may be deliberate. A small root disk still fills up with images and etcd.
