# Security and secrets audit

Studied `main` at `a3b13f49`. No hosts, no playbook runs, no age key, no decrypted secrets. Local checks: the repo's shell-permission matcher, the node-disk guard on localhost fixtures, and a gitleaks probe of dummy files outside the repo.

## Findings

### 1. Cursor deny list still allows SOPS decrypt and secret reads

**Where:** `.cursor/cli.json` (deny list), `tools/_cli_permissions.py` (`DRY_RUN_MUST_BLOCK`, `DRY_RUN_MUST_BLOCK_READS`), `tools/checks/cli-deny`

**What:** `cli-deny` stays green while decryption and secret reads are still allowed. The dry-run only requires a block of `sops decrypt`, `sops -d` as the start of the arguments, `ssh` as the first token, and a Read of `.config/sops/age/`. Bare `sops` on a ciphertext file decrypts it (that is sops' default open path). `sops exec-env` is allowed. `-d` after another flag does not match the `-d*` rule. `kubectl get secret` is allowed because only mutating verbs are denied. A shell whose first token is `bash` is allowed for sops, age, ssh, scp, and helm; kubectl and flux are the only families with a full-line `*tool*` pattern. Printing the age key from the shell is allowed: the key rule is Read-only, and only for `.config/sops/age/` or a `*.agekey` name.

**Fix:** Deny every `sops` invocation except encrypt, and add full-line `*sops*`, `*age*`, `*ssh*`, `*scp*`, and `*helm*` patterns the way kubectl is written. Deny `kubectl get secret` and `kubectl get secrets`. Deny shell arguments that contain the age key path. Put those commands in `DRY_RUN_MUST_BLOCK` so the check fails until the patterns match.

**Why:** That age key decrypts every file under `envs/env-01/secrets/`, and the flux-operator role loads the same key into the cluster. An agent on a controller that has the key or a kubeconfig can print either one, and CI still passes.

**Impact:** high

### 2. `--diff` prints the admin kubeconfig

**Where:** `infra/ansible/roles/kubeconfig/tasks/main.yml` lines 17–46 (fetch, replace, copy). None of them set `no_log`.

**What:** `/etc/rancher/k3s/k3s.yaml` is the cluster admin client key. The copy action and the replace module put the whole file in the task diff. `no_log` is what blanks that diff. The first run rewrites `https://127.0.0.1:6443`, so the replace diff is the entire kubeconfig, not one line. The age-key tasks and the node-token template set `no_log`. These three do not.

**Fix:** Set `no_log: true` on the fetch, the replace, and the copy.

**Why:** `--diff` is a normal playbook run. The admin kubeconfig ends up in the terminal scrollback and in any log of that output.

**Impact:** high

### 3. Root-disk guard misses device-mapper, btrfs, and `/dev/root`

**Where:** `infra/ansible/roles/node-disk/tasks/guards.yml` lines 2 and 11–15

**What:** "Refuse the root disk" compares `findmnt`'s source string to the resolved kernel disk. A localhost run of that guard, with both `wipe_confirmed: true` and a matching `jarvis_wipe_confirm`, still passed for root sources `/dev/mapper/ubuntu--vg-ubuntu--lv`, `/dev/nvme0n1p2[/]`, and `/dev/root` when the candidate disk was `/dev/nvme0n1`. The two-flag check itself holds: a longer id, a prefixed hostname, a missing confirm string, and a string `"true"` all still refuse. The existing tests only use kernel partition paths (`/dev/nvme0n1p3`, `/dev/sda2`).

**Fix:** Resolve `/` to its parent disk (device numbers, and `lsblk` PKNAME for mapper and `/dev/root`) and compare that disk to `node_disk_path`. Do that before either confirmation can allow `wipefs`.

**Why:** The root assert is the check that refuses the OS disk even when both confirmations name it. On a device-mapper root, which is Ubuntu's default installer layout and the apt-based layout this role targets, that assert does not fire, and `wipefs` runs on the OS disk.

**Impact:** high

### 4. A disk that already has `jarvis-data` can be the mount after a wipe

**Where:** `infra/ansible/roles/node-disk/tasks/guards.yml` lines 6–10; `infra/ansible/roles/node-disk/tasks/main.yml` lines 32–60

**What:** The label check only fails when two devices already carry `jarvis-data`. One foreign label is ignored. A localhost run with the target disk unlabeled, `/dev/sdb1` labeled, and both confirmations set, was allowed through. The mount task then always uses `LABEL=jarvis-data`, not the partition just formatted. A different kernel name does not match the target regex, so a USB is not selected as the wipe device that way. Without both confirmations the wipe is still refused.

**Fix:** Fail when any `jarvis-data` device is not the resolved disk or its partition, including when both confirmations are set. Mount the partition path that was formatted.

**Why:** A leftover jarvis disk or USB attached during a confirmed wipe is a legal mount source for `/var/lib/jarvis`. Local-path data and etcd snapshots follow that mount, and `wipefs` has already erased the disk that was confirmed.

**Impact:** high

### 5. Flux controllers track `2.8.x`

**Where:** `infra/ansible/roles/flux-operator/defaults/main.yml` line 9; `infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2` line 9; `envs/env-01/flux/flux-instance.yaml` line 9

**What:** `spec.distribution.version` is `2.8.x`. The operator treats that as a floating minor, and Flux reconciles this FluxInstance from Git. `cluster.multitenant` is `false`, so multi-tenancy lockdown is off for those controllers. The chart tag `0.60.0` and the k3s binary checksum are pinned; the controller images are not.

**Fix:** Pin an exact Flux release in the role default, the template, and the Git FluxInstance together.

**Why:** A new 2.8 patch rolls into the cluster on reconcile with no commit in this repo. Those controllers are the GitOps root and they decrypt `flux-system/sops-age`.

**Impact:** medium

## Checked and fine

- Committed `envs/env-01/secrets/*.sops.yaml` values are `ENC[AES256_GCM,…]`. `.sops.yaml` encrypts `data` and `stringData` to the age recipient. Age-key tasks set `no_log`, creation is `kubectl create` (not apply), and a different existing `sops-age` key is refused.
- The k3s binary and the Helm CLI archive check sha256. Actions are pinned by commit. The workflow token is `contents: read` only.
- `run-checks` exits non-zero when a check is missing or not executable.
- `ansible.cfg` does not turn off host-key checking. Become is sudo. The kubeconfig copy is mode `0600`, k3s server and agent configs are mode `0600`, and the node-token template is `no_log`. No Role or ClusterRole is committed in this repo.

## Appendix

- Stock gitleaks flags a high-entropy `admin_password` / `api_server_key` and does not flag a short `admin_password`. There is no repo gitleaks config.
- `requirements-dev.txt` installs `pytest` and `PyYAML` without hashes. `ansible==13.1.0` has no hash.
- `platform/flux-operator/oci-repository.yaml` pins chart tag `0.60.0` with no OCI digest. The helm upgrade command uses that tag.
- `tools/install-deps` `install_age` extracts the GitHub tarball before it hashes the binaries.
- `env-schema` does not check that `.sops.yaml` has a creation rule for every non-example env.
- `node-disk` and `k3s` create the data, backup, and etcd-snapshot directories mode `0755`.
- `kubectl logs` stays allowed.

## Not sure

- Cursor's own matcher may be stricter than `tools/_cli_permissions.py` for a `bash -c` wrapper. Bare `sops`, `sops exec-env`, and `kubectl get secret` are gaps in the deny entries, so they do not depend on that.
- With two `jarvis-data` labels, `mount` may error instead of choosing the USB. `wipefs` has already run, and the mount source is still the label.
- Flux controller RBAC is created by the operator chart, which is not in this repo. `multitenant: false` only shows that lockdown is off.
