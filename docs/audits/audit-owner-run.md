# Audit: owner-run dry read (audit-owner-run)

Read of `main` at `55894600`. Nothing here was run against a host. Local checks were `ansible-inventory --list` for `envs/env-01`, `kubectl wait --help` (default `--timeout=30s`), the Ansible 13.1.0 / ansible-core 2.20.10 modules that `make deps` installs, and the Flux operator v0.60.0 and kustomize-controller sources for naming and retry behaviour.

`env-01` is one host, the inventory hostname, roles `control_plane`, `workers`, `storage`, `backup`, `edge`, `ssh_user: ubuntu`, no `data_disk`. The seven `envs/env-01/secrets/*.sops.yaml` files are already in git.

## Findings

### 1. `make verify` crashes before it can pass

**Where:** `tools/verify/p0a` lines 32–33 and 77–82; `Makefile` lines 22–27; `tools/install-deps` lines 1–2; `infra/ansible/roles/k3s/tasks/main.yml` lines 72–80; `infra/ansible/roles/kubeconfig/tasks/main.yml` lines 3–5 and 43–48.

**What:** `make verify` runs `python3 tools/verify/p0a`, which executes bare `kubectl` and bare `ansible-inventory`. `make deps` installs Ansible only under `.venv/bin` and installs sops, age, gitleaks, kustomize, and kubeconform under `~/.local/bin`. It does not install kubectl. The k3s role copies the binary to `/usr/local/bin/k3s` and does not add a `kubectl` symlink. `subprocess.run` does not catch a missing binary, so the owner gets a Python traceback. The kubeconfig role writes `~/.kube/config` for the site.yml runner's `HOME`, and verify uses kubectl's default kubeconfig.

**Fix:** Teach `make deps` to install a pinned `kubectl` on the controller `PATH`, and have `make verify` call `.venv/bin/ansible-inventory` (or prepend `.venv/bin` and `~/.local/bin`). On a missing binary, print that hint and exit 1. Say in `infra/ansible/README.md` that verify runs as the same user that ran site.yml.

**Why:** The documented controller setup ends at `make deps` plus `~/.local/bin`. Following it, the P0a exit check cannot start. A fresh node has `k3s` and still has no `kubectl` command.

**Impact:** high

### 2. First-boot Flux is not waited out, and verify gives up in 30 seconds

**Where:** `infra/ansible/roles/flux-operator/tasks/instance.yml` lines 36–40; `infra/ansible/roles/flux-operator/tasks/operator.yml` lines 31–46; `tools/verify/p0a` lines 40–57; `envs/env-01/flux/kustomizations/platform-controllers.yaml` line 7, `platform-configs.yaml` line 7, `services.yaml` line 7, `apps.yaml` line 7.

**What:** site.yml waits until the Flux Operator deployment is Available and `fluxinstances.fluxcd.controlplane.io` is Established, applies `FluxInstance`, and returns. The GitRepository, Kustomizations, and HelmRelease appear only after that instance has pulled the Flux controller images. verify then `kubectl get`s `kustomizations.kustomize.toolkit.fluxcd.io`, `gitrepositories.source.toolkit.fluxcd.io`, and `helmreleases.helm.toolkit.fluxcd.io`. A missing CRD fails the check immediately. An empty list fails with "no Kustomization, GitRepository, or HelmRelease". Once objects exist, `kubectl wait` is called with no `--timeout`, and kubectl's default is 30s. The four child Kustomizations set `interval: 10m` and do not set `retryInterval`. In current kustomize-controller, a failed reconcile (decrypt, build, apply) is retried at `interval`. `dependsOn` waits use the controller's dependency interval instead, which is shorter.

**Fix:** After the FluxInstance apply, wait for `GitRepository/flux-system`, the root and four child Kustomizations, and `HelmRelease/flux-operator` to be Ready, with a timeout of at least 10m. Pass the same `--timeout` from verify, and retry the `get` until the CRDs exist. Set `retryInterval` on those Kustomizations to a minute or less.

**Why:** On a first boot the controller images and the Git clone are still in progress when site.yml exits. verify then fails, and a decrypt or apply error sits for ten minutes. Re-running verify immediately repeats the failure. Nothing in the playbook or the verify output says how long to wait.

**Impact:** high

### 3. The rollback drill has no procedure

**Where:** `docs/code-delivery/CONTRACT.md` line 60; `tools/verify/p0a` line 135.

**What:** The code-delivery contract says the P0a exit for repo hosting is "every Flux object Ready; rollback drill restores prior state (`p0a-16`)". verify prints a blank "p0a-16 rollback-drill log:" line. No README, Makefile target, or script says what to change, what command to run, or which output shows the previous state restored.

**Fix:** Add the drill next to the site.yml command in `infra/ansible/README.md`: the object to change, the command, the Ready condition that means the previous state is back, and the log line to paste. Point the checklist line at that section.

**Why:** The owner cannot produce the log the exit check asks for. The step is a name with no action.

**Impact:** high

### 4. Fresh-node access and the age key are stated as end states, not steps

**Where:** `infra/ansible/README.md` lines 5–7; `envs/env-01/secrets/README.md` line 15; `envs/env-01/values/cluster-vars.yaml` line 7; `envs/env-01/inventory/hosts.yml` lines 4–7; `infra/ansible/roles/flux-operator/tasks/sops_age.yml` lines 23–47; `Makefile` lines 14–17.

**What:** site.yml SSHes to the inventory hostname as `ubuntu` and becomes root. The Ansible README requires that name to resolve on the controller and the node, key auth, passwordless sudo, and a seeded `known_hosts`. No task and no command creates the SSH server, the user, the sudoers rule, the hosts entry, or the keyscan. `make deps` does not install `make` or `git`, and a fresh Ubuntu Server image does not include them, so the documented `make` entry points have nothing to run when the controller is that new install. The secrets README says the age private key stays offline and that encryption does not need it. The Ansible README says `SOPS_AGE_KEY_FILE` must be set on the controller for the first flux-operator run. The role reads that env var and the file it points at. It never opens `.sops.yaml`, so a key that is not the recipient already in that file is installed, and Flux SOPS fails later.

**Fix:** Add a short "before the first run" block to `infra/ansible/README.md`: `git` and `make` on the controller, OpenSSH on the node, user `ubuntu` with an authorized key and `NOPASSWD` sudo, the inventory hostname resolving on both sides, and `ssh-keyscan` into the controller `known_hosts`. State that `SOPS_AGE_KEY_FILE` on the controller is required for the first site.yml, that there is no fixed path, and that the file must be the private half of the recipient in `.sops.yaml`. Drop the "keep it offline" sentence for that first run, or qualify it as "offline except on the controller for the first flux-operator run".

**Why:** A wiped node has none of the SSH end state, and the two READMEs disagree about where the private key is during bootstrap. The flux-operator play is last, so the owner discovers the missing key after base-os and k3s have already run. A mismatched key fails at decrypt time, inside the ten-minute retry from finding 2.

**Impact:** medium

### 5. A green verify does not check the checklist, and the checklist asks for the wrong text

**Where:** `tools/verify/p0a` lines 128–143; `tools/bootstrap-secrets` lines 222–226; `tools/ci-rule-checks.txt`.

**What:** After the kubectl checks, verify prints blank lines for a second site.yml "nothing changed", a second bootstrap-secrets "nothing changed", a seeded bad PR for each of `cli-deny`, `contract-md`, `core-names`, `env-schema`, and `secret-scan`, a Renovate PR, and the rollback log. The exit code ignores those lines. bootstrap-secrets does print `bootstrap-secrets: all files present; nothing changed` when every file exists and `sops` is on `PATH`. site.yml never prints "nothing changed"; the signal is the recap `changed=0`. The seeded-PR and Renovate lines are prior CI evidence, and this rebuild does not create them. The rollback line is finding 3.

**Fix:** Make the site.yml line say `changed=0` in the PLAY RECAP, and say the `data_disk` and agent plays skip on env-01. Label the seeded-PR and Renovate lines as existing CI links, or drop them from this node exit check. Keep the process exit non-zero until the rollback log from finding 3 is recorded, or stop printing lines the command does not check.

**Why:** An owner who stops at exit 0 has not done the idempotence or rollback proof. An owner who hunts for the words "nothing changed" in the playbook output will not find them.

**Impact:** medium

## Fine

- Play order is usable: base-os, k3s server, label reconcile, kubeconfig, then flux-operator creates `flux-system/sops-age` before it applies FluxInstance. Flux operator v0.60.0 names that sync `GitRepository` after the namespace, `flux-system`, which matches the child `sourceRef`.
- `node-name` in the k3s server config is the inventory hostname, so the OS hostname can differ and verify's node-name check still matches.
- env-01 has no `data_disk` and no agents. Those plays match no hosts. The Ansible README says so. `ansible-inventory --list` on `envs/env-01/inventory` returns the inventory hostname with all five roles.
- Second-run idempotence holds in this Ansible: ansible-core 2.20 `set_fact` does not mark changed, probes set `changed_when: false`, flux bootstrap tasks are skipped once the deployment, secret, and FluxInstance exist, and the label task skips when the patch is empty. bootstrap-secrets does not overwrite existing files.
- `ansible==13.1.0` bundles `community.general`, `kubernetes.core` 6.2.0 (`kubernetes>=24.2.0`), and `ansible.posix`. The labels task installs `python3-kubernetes` on the node; Ubuntu's candidate here is 30.1.0.
- The k3s node wait is 30 retries × 5s after systemd has already reported the service started. Helm bootstrap uses `--wait --timeout 5m` and the operator rollout wait is 300s. Those cover the operator pod. The gap is finding 2, after FluxInstance.
- verify prints secret key names and not values (`RESTIC_PASSWORD` for `platform-backups/backups-restic`).

## Appendix

- `get_url` for the k3s binary and the Helm tarball keeps the module default socket timeout of 10s and sets no retries (`infra/ansible/roles/k3s/tasks/main.yml` lines 72–80, `infra/ansible/roles/flux-operator/tasks/helm_cli.yml` lines 33–38).
- The apt tasks use the module default `lock_timeout` of 60s (`infra/ansible/roles/base-os/tasks/main.yml` lines 11–19).
- `_load` calls `nodes_proc.check_returncode()` (`tools/verify/p0a` lines 88–89), so a failed `kubectl get nodes` raises and the checklist is not printed.
- `bootstrap()` calls `require_sops()` before it looks at the files (`tools/bootstrap-secrets` lines 205–211), so a missing `~/.local/bin` on `PATH` errors instead of printing "nothing changed".

## Unsure

- The repo says "controller" and also "re-runs site.yml on the controller node". It does not say whether that machine is the inventory hostname or another computer. The sequence below works for either, as long as the inventory hostname resolves from the machine that runs Ansible.
- Ubuntu release is not pinned. `ansible==13.1.0` requires Python >= 3.12, which Ubuntu Server 24.04 and later provide.
- `python3-kubernetes` is in Ubuntu universe. This worker's archive has it. A minimal image with universe disabled would fail the label task. Not confirmed for the installer default.
- `chronyd -p` on the rendered NTS config was not run. This worker's chronyd cannot read a file outside `/etc/chrony`, and sudo is unavailable here.
- Kubelet reservations (`cpu=500m` plus `cpu=500m`, `memory=1Gi` plus `memory=1Gi`) might starve a very small node. The node size is not in the repo.
- No playbook was executed, so the `changed=0` conclusion is from the Ansible 2.20 modules and the task conditions, not from a recap.

## Owner sequence

Run from a clean checkout of `main`. Flux syncs `flux_git_repo_url` at `flux_git_ref` from cluster-vars, so the checkout and that ref need to be the same commit. Secrets for env-01 are already committed. The first `bootstrap-secrets` and the first site.yml do not depend on each other. Putting secrets first matches `envs/env-01/README.md`. That order is not fixed anywhere for a rebuild.

1. **Guess.** On the fresh node: install OpenSSH, create user `ubuntu` with your key in `authorized_keys` and `NOPASSWD:ALL` sudo, and make the inventory hostname resolve on the node. The repo states the end state (`infra/ansible/README.md` line 5, `ssh_user` in cluster-vars) and gives none of these commands.
2. **Guess.** On the controller: install `git` and `make`, clone this repo, and `ssh-keyscan -H <inventory hostname>` into `~/.ssh/known_hosts`. Check `ssh <ssh_user>@<inventory hostname> sudo -n true`. Same gap as step 1. `make` and `git` are not in `make deps`.
3. `export PATH="$HOME/.local/bin:$PWD/.venv/bin:$PATH"` then `make deps`. Check `command -v sops` and `command -v ansible-inventory`. The `~/.local/bin` half is in the secrets README. The `.venv/bin` half is required by verify and is not written down (finding 1).
4. `export SOPS_AGE_KEY_FILE=<path to the age private key>`. Check the public half matches the recipient in `.sops.yaml` (`age-keygen -y "$SOPS_AGE_KEY_FILE"`). The variable is documented. The path and the recipient check are not (finding 4).
5. `make bootstrap-secrets ENV=env-01`. Check the line `bootstrap-secrets: all files present; nothing changed`. No new files, nothing to commit.
6. From `infra/ansible`:

```bash
../../.venv/bin/ansible-playbook site.yml \
  -i ../../envs/env-01/inventory/hosts.yml \
  -i ../inventory/roles.yml \
  -e jarvis_cluster_vars=../../envs/env-01/values/cluster-vars.yaml
```

Check the recap `failed=0`. The `data_disk` and agent plays skip. `~/.kube/config` exists and its server is `https://<inventory hostname>:6443`. This is the README command with `<env>` filled in.

7. Run the same site.yml again. Check the recap `changed=0` and `failed=0`. The checklist's phrase "nothing changed" is not what the playbook prints (finding 5).
8. `make bootstrap-secrets ENV=env-01` again. Check the same "nothing changed" line.
9. **Guess / blocked by finding 1.** Install kubectl onto the controller `PATH` (the repo does not), stay the same user as step 6, then `make verify PHASE=p0a ENV=env-01`. Check four PASS lines: `flux Ready`, `secret platform-backups/backups-restic keys=RESTIC_PASSWORD`, `node roles`, `enablelb`. Today this fails until Flux has finished coming up, and the fail can be a traceback or a 30s timeout (findings 1 and 2). Wait and re-run is the only recovery, and a failed reconcile waits about 10 minutes.
10. **No command in the repo.** The rollback drill (finding 3). Do not invent the steps. The checklist line cannot be filled from this tree.
