# P0a audit: plan versus build

Audited `main` at `8dcdfc1a` (p0a-01 through p0a-18). Queued cleanup items are not repeated. No code was changed for this audit.

## 1. Kubeconfig fetch reads the controller path

**Where:** `infra/ansible/roles/kubeconfig/tasks/main.yml` lines 3–6 and 17–19; `infra/ansible/group_vars/all.yml` line 3. p0a-13. The shared variable landed in #52.

**What:** Group vars define `k3s_kubeconfig_path` as the node kubeconfig (`/etc/rancher/k3s/k3s.yaml`). The role block sets that same name to the controller user's `~/.kube/config`. Block vars win, so `fetch` uses the controller path as the remote source. A localhost debug of this block-vars pattern resolves to the controller kubeconfig, not the group-vars path. Before #52, `fetch` used a separate remote variable. The playbook stops on that failure, so the flux-operator play never runs.

**Change:** Build. `fetch` src stays the group-vars node path. The controller dest gets a different variable.

**Why:** p0a-13 says this role writes only the running user's kubeconfig, which means it has to read the node file. One name cannot be both paths. The plan's split is the right one.

**Impact:** high. The owner `site.yml` installs k3s, fails in this play, and never bootstraps Flux.

## 2. Flux 2.8 does not fail closed on a missing postBuild variable

**Where:** `infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2` (no `spec.kustomize`); `envs/env-01/flux/flux-instance.yaml` lines 7–26; `infra/ansible/roles/flux-operator/defaults/main.yml` line 9 (`2.8.x`). `schema/CONTRACT.md` has no strict-substitution section (nearest text is “Explicit env”, lines 99–101). D45, p0a-14.

**What:** D45 says the FluxInstance turns on kustomize-controller `StrictPostBuildSubstitutions`, so a missing variable fails instead of going empty, and the env schema contract records that plus the `$${...}` escape. Neither FluxInstance sets the patch. On the pinned 2.8 line the gate is opt-in. It became the default in Flux 2.9 (kustomize-controller 1.9.0, 2026-06-17). Nothing in the tree uses a postBuild variable yet, so this `site.yml` does not trip it.

**Change:** Build. Add the feature-gate patch to the Ansible template and the Git FluxInstance (the existing render comparison keeps them aligned), and add the D45 postBuild section to `schema/CONTRACT.md`.

**Why:** The decision stays. Empty substitution is the failure D45 exists to make loud, and P0b is when Ingress, certs, and DNS start using cluster-vars.

**Impact:** high for the start of P0b. A missing or mistyped variable becomes an empty string and the object can still be Ready. It does not stop the P0a owner run.

## 3. site.yml with only the env inventory skips the server and targets the node as an agent

**Where:** `infra/ansible/site.yml` plays at lines 27, 37, 50, 63, 77, and 89; `infra/ansible/ansible.cfg` (no mismatch setting); `infra/ansible/README.md` lines 6–12. D42, p0a-12, p0a-13.

**What:** `ansible-playbook site.yml --list-hosts` with only the env-01 hosts file exits 0. It warns that `data_disk` and `control_plane` do not match. The server, label, kubeconfig, and flux-operator plays match no hosts. The agent play matches the only host. A real run would apply base-os, skip the server, then run the agent role on that host.

**Change:** Build. On the opening play, before any change, assert that the `control_plane` group contains exactly one host. Do not turn on a global host-pattern mismatch error: env-01's `data_disk` and agent plays are supposed to match nobody and exit 0 (p0a-12).

**Why:** The plan's two-inventory command stays. The second file is what creates the groups. Without a loud check, a missed `-i` is a warning, then a failure inside the agent role, after base-os has already changed the node.

**Impact:** medium. The README command is the right one. Missing the second inventory does not install k3s and does not fail at the start.

## 4. D42's no-edge fallback is not what the build enforces

**Where:** `envs/env-01/inventory/hosts.yml` line 7 (that host's roles include `edge`); `schema/CONTRACT.md` lines 84 and 89; `schema/env.py` lines 510–511; `infra/ansible/roles/k3s/templates/label-patches.json.j2` lines 3 and 12–22; `tools/verify/p0a` lines 117–124; `tools/tests/test_k3s.py` lines 215–217. D42 (edge paragraph and the env-01 bullet). p0a-18's commit message is “require an edge host”.

**What:** D42 says env-01 has no `edge` role, and that ports 80/443 and 53 land on `edge` or on `control_plane` when no host is edge. The schema rejects an env with no edge host. The label task and `make verify` set and check `enablelb` only on hosts whose roles include `edge`. The test asserts that with no edge hosts, `control_plane` does not get `enablelb`. p0a-11a's done checks do not require an edge host. p0a-18 added that requirement in the build.

**Change:** Plan. Update D42's env-01 bullet and the fallback sentence: an env must declare at least one `edge` host, and `enablelb` follows that role only. Leave the build. It matches the schema, the labels, and verify, and it is what the first `site.yml` will apply.

**Why:** The fallback D42 describes is unreachable. The schema forbids that shape, and the template does not implement it. Editing the inventory to match D42 before the owner run would drop `enablelb` and leave P0b's LoadBalancers with nowhere to land. If the fallback is still wanted, the build has to grow it before anyone removes `edge` from this host.

**Impact:** medium. This owner run matches the inventory and passes verify. The trap is a later edit that “corrects” the inventory to the decision text.

## 5. A refused wipe does not print the disk's model, size, and partitions

**Where:** `infra/ansible/roles/node-disk/tasks/guards.yml` lines 16–22; `infra/ansible/roles/node-disk/tasks/main.yml` (no read of model, size, or partitions); `tools/tests/fixtures/node-disk/case.yml` lines 19–22. D42 wipe safety, p0a-11b done check.

**What:** The plan says the role stops and prints the disk's model, size, and partitions when the run-time confirmation is missing. The fail message is the host, the disk id, and the confirmation value. The test asserts the substring `refusing to wipe`, so CI stays green without those facts.

**Change:** Build. Gather model, size, and partitions before the refusal and put them in the fail message. Point the test at those facts.

**Why:** The done check is unmet. The by-id path is not the identifying print D42 requires before a wipe. env-01 has no data disk, so this run does not hit it.

**Impact:** low. It matters on the first node that declares a data disk, not on this owner run or the start of P0b.

## Appendix

- `infra/ansible/README.md` still says a second data-disk label is not checked; `guards.yml` now refuses more than one. Mounted-elsewhere and a non-empty mount point are still unchecked (D42).
- D3's default data paths are not applied or written as defaults in the schema. `local_path_data` and `backup_path` are required keys. Only `ssh_user` is filled by `resolve_cluster_vars`, and `site.yml` loads the raw file, so an omitted `ssh_user` passes CI and then fails Ansible (D34). env-01 sets it.
- The journald cap and the etcd snapshot schedule and retention live in role `defaults/` (D6 and D46 say `vars/`). `tools/tests/test_base_os.py` and `tools/tests/test_k3s.py` pass `2G` and the cron in from the test, so they stay green if the role values change.
- `tools/tests/test_flux_entry.py` validates with Kubernetes 1.31 schemas while the pinned k3s is 1.36.
- `infra/ansible/roles/base-os/templates/20auto-upgrades.j2` still calls upgrades-off a departure from framework §4. p0a-11 already requires it.

## Unsure

- Whether the Ubuntu 26.04 `python3-kubernetes` apt package can patch nodes on the pinned k3s. The label task is the first use, and the container tests skip the k3s tag.
- This audit did not run `site.yml` or anything on the live node. The inventory finding is from `ansible-playbook --list-hosts` on this worker. The kubeconfig finding is from variable precedence on localhost, not a fetch.
