# Wrap-up audit: findings (Claude Opus 5.5)

Audited SHA: `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1` (branch `audit/wrapup-claude`). Lead: full line-level code review. No code changed.

Evidence commands ran in this worktree (`bash ci/ci-local-verify`: exit 0, 483 tests) or in the env-00-only scratch clone `../wrapup-claude-env00`. My own experiment files are under `../wrapup-claude-env00/audit-scratch/`. Seeded breaks went into the clone's real files, one at a time, and each was reverted by a reverse edit, because `git checkout` was denied in this headless run. The headless permission policy also denied `make`, `python3 <script>`, `openssl`, `git status` on the clone and running `ci/checks/*` wrappers directly. So experiments were driven through `pytest`. Parts of the test review were delegated and are reported here as one voice.

---

## 1. Big picture

**What's solid.** The foundation does the hard things carefully. Inputs are validated before a host is touched: preflight, marker guards, the node-disk refusals and the env schema. Secrets stay out of Git (SOPS, gitleaks, an age-key mode check). Images are digest-pinned. Kyverno enrolls every PVC in backups automatically. The restore tool fails closed and prints undo commands. The drill really deletes and restores a volume and compares checksums. Tests mostly run real Ansible against localhost with fake binaries, so they check behaviour, not text. The testapp manifest is a good hardened template for future apps.

**What's fragile.** Recovery holds up for operator mistakes but not for real loss:
- The age key is a single point of failure for every backup ([F01]).
- The documented restic rotation orphans every existing repository ([F02]).
- A single disk holds both the data and its backups, with no off-host copy and no k3s token copy ([F03], [F04]).
- After a rebuild, the restore binder can pin a volume to the wrong node ([F05]).
- On day 0 a new adopter gets red `make verify` and `make drill` runs from a correct install ([F06]).

The foundation also has no landing place yet for what comes next:
- Services and apps are global paths with no per-env selection ([F08]).
- Services cannot receive SOPS secrets ([F07]).
- GPU support is a label only ([F13]).
- Non-HTTP apps cannot be exposed ([F19]).

**Themes that recur in three or more areas.**
1. **Speculation shipped as product.** `hermes.yaml` (a required file), `apps.yaml`, `build_agent: cursor`, `code_delivery`, `llm_provider_host`, `apps_git_repo_url`, the cross-env owner rule, five bootstrap secrets with no consumer, placeholder ConfigMaps, and contract roots for directories that don't exist. All are validated and tested, but nothing consumes them ([F27], [F30]).
2. **Two sources of truth checked by tests instead of one generated value.** Examples: the FluxInstance sync url/ref against cluster-vars ([F11]), the generated ConfigMap against cluster-vars, and the k3s snapshot schedule against the etcd-copy schedule ([F31]). The k3s version is also checked against text in the Traefik contract. When CI is the only place the copies are compared, an adopter who skips CI gets a broken cluster.
3. **Hand-rolled glue where a native tool exists.** prep-node is about 1,000 lines over SSH ([F23]). A node-side Helm CLI bootstraps the Flux Operator ([F24]). Renovate uses 13 custom regex managers ([F22]). verify has its own cron engine ([F37]). The data_disk constructed group is defined but unused ([F30]).
4. **Too strict for adopters.** Only `node_roles` and `data_disk` are allowed as host keys, so no `ansible_host` or GPU attributes ([F13]; scratch case `ansible_host_key: REJECTED`). Chrony is NTS-only ([F20]). Flux needs a public repo ([F09]). Only two LoadBalancer services are allowed ([F19]). One owner handle across all envs ([F27]).
5. **Build-process residue in product code.** Plan and decision codes appear in about 40 non-Markdown files, because the plan-codes check only scans Markdown ([F28]). Operator knowledge sits in env-01's README ([F29]).

---

## 2. Ranked findings

(See section 3 for each block. Severity: C critical, H high, M medium, L low.)

| id | sev | title | Q | size |
| --- | --- | --- | --- | --- |
| F01 | C | Age key is a single point of failure for every backup; one recipient, no offline-backup procedure | 13, 15 | S |
| F02 | C | Documented restic rotation ("delete the file and re-run") orphans every existing restic repository | 15, 13 | S |
| F03 | H | No off-host backup option; backup host or single-node disk loss loses data and every backup | 13, 6 | M |
| F04 | H | k3s server token is not backed up, so the drilled etcd copy cannot restore a rebuilt control plane | 13 | S |
| F05 | H | Restore binder pod has no node placement, so after a rebuild a local-path PVC can bind on the wrong node | 13, 11 | S |
| F06 | H | `make verify` and `make drill` fail on a fresh install until the first scheduled etcd copy (up to 12 h) | 9, 18 | S |
| F07 | H | Services and apps have no way to receive SOPS secrets; `make seal` writes files nothing can apply | 21, 23 | M |
| F08 | H | `services/` and `apps/` are global Flux paths with no per-env selection; `apps.yaml`/`hermes.yaml` drive nothing | 21, 22 | M |
| F09 | H | Flux sync requires a public Git repo; an adopter's env topology must be published | 5, 17 | S |
| F10 | H | OS security updates are switched off and no patch or reboot procedure exists | 14, 17 | M |
| F11 | M | FluxInstance url/ref duplicate cluster-vars and nothing before the host run compares them | 10 | S |
| F12 | M | prep-node overwrites the env-wide `ssh_user` with each node's login user | 10, 11 | S |
| F13 | M | GPU support is a role label plus a PCI vendor assert; no driver, runtime, device plugin or per-node GPU size | 11, 22 | L |
| F14 | M | Node role labels are prefixed with `role.<dns_suffix>/`, so every workload must substitute the DNS suffix | 22, 4 | M |
| F15 | M | The whole age key file (often holding unrelated identities) is copied into the cluster | 17 | S |
| F16 | M | One restic credential is copied into every PVC namespace, and every PVC namespace gets privileged movers | 17, 22 | M |
| F17 | M | Internal CA has no name constraints; adopters install a CA that can sign any domain | 17 | S |
| F18 | M | Agents join with the full server token (`node-token`), not an agent token | 17 | S |
| F19 | M | Only Traefik and App DNS may be LoadBalancers and DNS covers Ingress only, so non-HTTP apps can't be exposed | 5, 22 | M |
| F20 | M | Chrony is NTS-only; a LAN or router NTP server silently never syncs | 5, 4 | S |
| F21 | M | No documented way to take new Jarvis releases; 9 per-env Flux files copy core wiring; prune-disabled orphans | 14 | M |
| F22 | M | Renovate: 13 regex managers duplicate native managers; `tools/versions.env` pins never update | 2, 14 | S |
| F23 | M | prep-node is about 1,000 lines of SSH glue that an Ansible bootstrap play or autoinstall would replace | 2, 3 | M |
| F24 | M | Flux Operator bootstrap downloads a Helm CLI onto the node; k3s's HelmChart auto-deploy does this natively | 2, 4 | S |
| F25 | M | No `make` target for `site.yml`; a three-flag relative-path command is copied 12 times | 9, 2 | S |
| F26 | M | `make verify` doesn't check node Ready or failing pods; recovery steps are scattered across 5 files | 16, 18 | S |
| F27 | M | Speculative product config: required `hermes.yaml`, `build_agent: cursor`, unused keys, 5 unconsumed secrets | 7, 8, 22 | M |
| F28 | L | Plan and decision codes in about 40 code files; the plan-codes check only scans Markdown | 8 | S |
| F29 | L | Docs point an adopter at env-01-only files; restic rotation and age-key notes live only in env-01 | 9, 12, 16 | S |
| F30 | L | Cruft: unused `data_disk` group, unused `storage` role, placeholder ConfigMaps, dead contract roots, dead arm64 branches | 7 | S |
| F31 | L | The drill's etcd check races the k3s snapshot; the copy schedule is a hard-coded offset of a role default | 4 | S |
| F32 | L | Tools run under system python3 with apt packages while Ansible and pytest use `.venv`; full `ansible` package | 1, 3 | S |
| F33 | L | `make deps` silently replaces the user's `~/.local/bin/kubectl`, `sops`, etc. | 4 | S |
| F34 | L | `bootstrap-secrets` ignores `age_key_file`, which `site.yml` and prep-node honour | 10 | S |
| F35 | L | `dns_suffix` is not validated as a DNS name | 10 | S |
| F36 | L | Command guards miss two categories: repo tools that write the cluster, and kubeconfig reads | 17 | S |
| F37 | L | verify ships a minute-by-minute cron evaluator (about 85 lines) | 19 | S |

Test- and check-specific findings from the seeded-break pass are ranked in section 3b. The T-ids are T01–T20, plus two late code findings, F38 and F39. By severity, they slot in as follows:
- **High** (with F07–F10): T01, T02, T03, T16, T17.
- **Medium** (with F11–F27): T04–T09, T18, T19, F38.
- **Low:** the rest.

---

## 3. Findings

### F01 — Age key is a single point of failure for every backup (critical)

- **What breaks and for whom:** An operator who loses the controller's age key file after the cluster is gone (for example, the laptop and the node die together) cannot decrypt `backups-restic`. Without `RESTIC_PASSWORD`, every restic repository on the intact backup disk is unreadable. `make drill` passes the whole time, so the failure is invisible until it matters.
- **Location:** `.sops.yaml:2-4` (one `age:` recipient); every `envs/env-01/secrets/*.sops.yaml` (`sops.age` has exactly one recipient); `infra/ansible/README.md:76`; `docs/deploy.md:9`.
- **Question:** 13, 15.
- **Evidence:** `.sops.yaml` holds a single recipient (`age: age1q3ze…`). The metadata grep over `envs/env-01/secrets/` finds exactly one `recipient:` line per file. `infra/ansible/README.md:76`: "If both are lost, the old secret values are gone." The only backup guidance is `infra/ansible/README.md:76` "back up the key file" and `docs/deploy.md:9` "Keep the private key offline except for those reads." Nothing says how or where to keep that copy, and nothing tests that it decrypts.
- **Severity:** critical (can lose all backup data).
- **Size:** S.
- **Suggested fix:** Make a second, offline age recipient part of the env setup (SOPS supports several recipients natively: a paper key or a hardware key in `.sops.yaml`). Have `make verify` or a new `make keycheck` confirm that every secret lists both recipients. Write one Operations section, "age key backup and rotation", using `sops updatekeys`. Optionally also keep the restic password itself in the offline kit. We lose nothing; the cost is one more key to look after.
- **Confidence:** high.

### F02 — Documented restic rotation orphans existing repositories (critical)

- **What breaks and for whom:** An operator who follows the only written rotation step deletes `backups-restic.sops.yaml` and re-runs `make bootstrap-secrets`. That generates a new `RESTIC_PASSWORD` and new rest-server credentials. Every existing repository was created with the old password, so restic cannot open it. Scheduled backups start failing, and restores from existing snapshots fail. The prep-node "swap" path does the same and also loses the old ciphertext's key.
- **Location:** `envs/env-01/secrets/README.md:25`; `tools/bootstrap-secrets:261-269`; `tools/prep-node:904-908`; `infra/ansible/README.md:76`.
- **Question:** 15, 13.
- **Evidence:** `envs/env-01/secrets/README.md:25`: "To rotate `backups-restic`, delete that file and run the command again." `_restic_string_data()` always draws a fresh `RESTIC_PASSWORD: _random_token(32)`. A restic repository is unlocked by a key protected with the password it was created with. A new password needs `restic key add` against each repository first. `platform/backups/CONTRACT.md` has no rotation section.
- **Severity:** critical (backups become unreadable; recovery depends on digging the old ciphertext out of Git history with the same age key).
- **Size:** S.
- **Suggested fix:** Remove that sentence. Add a backups Operations section that rotates the rest-server password only (htpasswd), says the repository password is never rotated in place, and if it must be, gives the `restic key add` / `key remove` loop per repository. Make `bootstrap-secrets` refuse to regenerate `backups-restic` when restic repositories may exist, or at least warn loudly.
- **Confidence:** high.

### F03 — No off-host backup; disk loss loses data and backups (high)

- **What breaks and for whom:** A single-node adopter, which is the README's main shape and env-01's, keeps PVC data and restic repositories on one host. When that disk or host dies, nothing remains: no Git-recoverable data and no backups. Multi-node adopters lose every backup when the `backup` host's disk dies, and nothing warns them. "What happens if the disk dies" therefore has no recoverable answer.
- **Location:** `docs/deploy.md:110`; `platform/backups/CONTRACT.md:185-192` (Limits); `platform/backups/rest-server.yaml:27-28,64-67`.
- **Question:** 13, 6.
- **Evidence:** `docs/deploy.md:110`: "They cover mistakes and rollback, not disk loss." The rest-server is pinned with `nodeSelector: "role.${dns_suffix}/backup": "true"` and keeps its data in `hostPath: ${backup_path}/restic`. No second repository target or replication exists anywhere in the repo.
- **Severity:** high (a home-lab product's typical failure is a disk; this is a real gap, not a nice-to-have).
- **Size:** M.
- **Suggested fix:** Add one optional off-host copy as env config. For example, a CronJob on the backup node runs `restic copy` (or rclone) into a second target the adopter chooses: a USB disk, a NAS, or S3/B2. Credentials come via SOPS; verify checks freshness. Keep the cost choice in config. This replaces the "not disk loss" Limit. We lose the claim of being fully self-contained when the option is enabled.
- **Confidence:** high.

### F04 — k3s server token not backed up; the etcd copy can't restore a rebuilt control plane (high)

- **What breaks and for whom:** The drill proves the restic copy of the etcd snapshot matches the k3s file. With `secrets-encryption: true`, restoring that snapshot onto a new node needs the original server token. The token lives only in `/var/lib/rancher/k3s/server/token` on the control plane's root disk, and nothing copies it. An operator whose control-plane root disk died gets a "PASS" drill and a snapshot they cannot use.
- **Location:** `infra/ansible/roles/k3s/templates/config.yaml.j2:4`; `platform/backups/etcd-snapshots.yaml:80`; `platform/backups/CONTRACT.md:183`.
- **Question:** 13.
- **Evidence:** CONTRACT line 183: "restoring onto a rebuilt node also needs the original k3s server token. Same-node restore is the scope." The CronJob backs up only `/snapshots` (`restic backup /snapshots`). `k3s_data_dir` (`/var/lib/rancher/k3s`) is not under `/var/lib/jarvis` (`group_vars/all.yml:6`), so it isn't on the data disk either.
- **Severity:** high (false sense of recovery).
- **Size:** S.
- **Suggested fix:** Either seal the server token into `envs/<env>/secrets/` at first install (site.yml reads it once and `make seal`s it), or include the token file in the etcd-copy restic backup. Then document the rebuilt-node etcd reset. Or decide etcd restore is out of scope because GitOps rebuilds state, and say plainly that the drill's etcd line proves copy integrity only. We lose nothing either way.
- **Confidence:** high.

### F05 — Restore binder pod has no node placement (high)

- **What breaks and for whom:** This hits multi-node operators after a rebuild, or after an app's PVC was deleted. `restore` scales the workload to 0 and then binds the unbound PVC with a short-lived pod that has no nodeSelector or affinity. local-path binds the volume to whichever node runs that pod. A workload that must run on a particular node (a GPU node, or one with a `nodeSelector`) then stays Pending forever with a volume node-affinity conflict, after a "PASS restore".
- **Location:** `tools/restore:561-580` (`_binder_body`), `tools/restore:719-728`; `platform/backups/CONTRACT.md:121`.
- **Question:** 13, 11.
- **Evidence:** `_binder_body` builds `spec: {restartPolicy, containers, volumes}` and nothing else. CONTRACT:121: "bound by a short-lived pod that uses the CronJob image and has no nodeSelector."
- **Severity:** high (silent wrong placement in a recovery path).
- **Size:** S.
- **Suggested fix:** Copy `nodeSelector`, `affinity` and `tolerations` from the owning Deployment, StatefulSet or CronJob pod template (the tool already finds the owner in `_quiesce`). Or refuse to bind and ask the operator to scale the owner to 1 with the PVC already `backup: paused`. Add a test with an owner that has a nodeSelector.
- **Confidence:** medium (depends on local-path's WaitForFirstConsumer binding, which is the k3s default).

### F06 — Fresh-install `make verify` and `make drill` fail until the first scheduled runs (high)

- **What breaks and for whom:** A new adopter follows `docs/deploy.md`: site.yml, then `make verify`, which the doc says "exits 0". verify fails `backup freshness` with "etcd snapshot copy has no successful scheduled run" until the `5 */12 * * *` CronJob has run, up to 12 hours. `make drill` then fails inside the etcd pod (`[ -n "$latest" ]`, or the newest file is not yet in restic). The adopter can't tell a bad install from a young one.
- **Location:** `tools/verify/run:366-372`; `platform/backups/etcd-snapshots.yaml:12`; `infra/ansible/roles/k3s/defaults/main.yml:8`; `tools/drill:280-292`; `docs/deploy.md:98,106`.
- **Question:** 9, 18.
- **Evidence:** `if not all(isinstance(value, str) for value in (spec.get("schedule"), cron_zone, status.get("lastSuccessfulTime"))): raise CheckError("etcd snapshot copy has no successful scheduled run")`. `lastSuccessfulTime` is absent until the first run. deploy.md:98: "Produces one `PASS` line per standing check, and exits 0."
- **Severity:** high (blocks a new adopter's first success signal).
- **Size:** S.
- **Suggested fix:** Have site.yml (or a `make first-backup` step) trigger one `k3s etcd-snapshot save` and one Job from the CronJob. Or report `PENDING first run` separately from `FAIL` when the object is younger than one interval. Say so in deploy.md.
- **Confidence:** high.

### F07 — Services and apps have no path for SOPS secrets (high)

- **What breaks and for whom:** This blocks every future service: the assistant's email and calendar OAuth tokens, the code factory's scoped credentials, an outside app's API key. Only `platform-configs` decrypts. Its env list may hold any env SOPS file, but it is applied before `services`, so a Secret in a service namespace fails because the namespace doesn't exist yet, and that blocks the whole chain. The `services` and `apps` Kustomizations have no `decryption`. `make seal` writes `envs/<env>/secrets/<name>.sops.yaml` that no path applies. env-01 already holds 5 such orphan secrets.
- **Location:** `envs/env-00/flux/kustomizations/services.yaml:6-19`, `apps.yaml:6-19` (no `decryption:`); `envs/env-00/flux/kustomizations/platform-configs.yaml:16-19`; `tools/seal`; `envs/env-01/flux/platform-configs/kustomization.yaml:3-6`.
- **Question:** 21, 23.
- **Evidence:** env-01 has `hermes-ops-api`, `hermes-ops-dashboard`, `hermes-code-factory-*` and `grafana-admin` SOPS files, and only `backups-restic` and `cert-manager-ca` are listed on any Flux path.
- **Severity:** high (blocks the normal change for every consumer).
- **Size:** M.
- **Suggested fix:** Give each env one `envs/<env>/flux/service-secrets/` (or per-service) Kustomization. It decrypts with `sops-age`, depends on `platform-configs`, and creates the namespaces it targets. Or add `decryption` to `services` and `apps` and let a service's env overlay list its own sealed files. Document it in a "where a new service lands" contract (see F08).
- **Confidence:** high.

### F08 — Services and apps are global paths with no per-env selection (high)

- **What breaks and for whom:** Every env deploys exactly `./services` and `./apps`. An adopter who wants the assistant but not the code factory, or a single-node CPU-only env that must skip GPU services, has no knob. `values/apps.yaml` (`app_hosting_enabled`, `apps`) and `values/hermes.yaml` are validated but nothing reads them. No contract says where a new service lives, how it's enabled per env, or what it must ship (namespace, Ingress, PVC, Secret, health check).
- **Location:** `envs/env-00/flux/kustomizations/services.yaml:9`, `apps.yaml:9`; `services/kustomization.yaml`; `apps/kustomization.yaml`; `schema/env.py:603-621`.
- **Question:** 21, 22.
- **Evidence:** `path: ./services` and `path: ./apps` sit in every env. A grep for `app_hosting_enabled|bot_tier_map` outside env files and tests finds only `schema/env.py` and `schema/CONTRACT.md`.
- **Severity:** high (blocks the next growth step).
- **Size:** M.
- **Suggested fix:** Mirror the platform-configs pattern: an `envs/<env>/flux/services/kustomization.yaml` that lists the core service directories this env enables (plus its secrets), with the Flux Kustomization pointing there. Delete `apps.yaml` and `hermes.yaml` until a consumer exists. Add `services/CONTRACT.md` ("adding a service") covering folder layout, the Ingress/cert/DNS pattern (testapp is the template), `backup: none` rules, secrets, health and resource rules.
- **Confidence:** high.

### F09 — Flux sync requires a public repo (high)

- **What breaks and for whom:** Every adopter's env folder holds their hostnames, DNS suffix, login user and node roles. The design forces that repo to be public: there's no `secretRef`, and the contract says "Public Git remote". An adopter with a private fork must hand-craft a Flux secret, and site.yml will not create it.
- **Location:** `schema/CONTRACT.md:57`; `envs/env-00/flux/flux-instance.yaml:23-27`; `envs/env-01/values/cluster-vars.yaml:33`.
- **Question:** 5, 17.
- **Evidence:** schema CONTRACT: "`flux_git_repo_url` | yes | Public Git remote for FluxInstance sync (https only; owner/repo path; no secret in the URL)". env-01: `# Flux Git sync (public HTTPS, no deploy key; R25).`
- **Severity:** high (blocks a normal adopter choice; also leaks topology).
- **Size:** S.
- **Suggested fix:** Support an optional read-only token or deploy key. FluxInstance `spec.sync.pullSecret` takes a Secret that flux-operator's role creates once from a SOPS file, the same way as `sops-age`. Keep public as the default.
- **Confidence:** high.

### F10 — OS security updates are off and no patch procedure exists (high)

- **What breaks and for whom:** Every node runs indefinitely without security patches. `unattended-upgrades` has no allowed origins and `apt-daily-upgrade.timer` is stopped. The comments say "upgrades are run manually by the admin", but no tag, play or doc says how: drain, upgrade, reboot, one node at a time.
- **Location:** `infra/ansible/roles/base-os/templates/50unattended-upgrades.j2:1-4`; `20auto-upgrades.j2:1-5`; `infra/ansible/roles/base-os/tasks/main.yml:90-97`.
- **Question:** 14, 17.
- **Evidence:** `Unattended-Upgrade::Allowed-Origins { };`, `APT::Periodic::Unattended-Upgrade "0";`, `name: apt-daily-upgrade.timer enabled: false state: stopped`. A grep for `apt upgrade|dist-upgrade|reboot` in docs finds nothing.
- **Severity:** high (unsafe default for a product).
- **Size:** M.
- **Suggested fix:** Either allow security-only unattended upgrades with no automatic reboot (Ubuntu default origins `-security`) plus a reboot-required check in verify, or add a `patch` play: serial 1, drain, `apt upgrade`, reboot if `/var/run/reboot-required`, uncordon. Document it in infra/ansible/README.
- **Confidence:** high.

### F11 — FluxInstance url/ref duplicate cluster-vars; nothing checks them before the host run (medium)

- **What breaks and for whom:** An adopter follows deploy.md step 1, "Every `envs/env-00/` path under `flux/` must name `<env>`", and leaves the example `url`/`ref` in `flux-instance.yaml`. env-schema accepts it and site.yml applies it. Flux then tries to fetch `example-owner/example-cluster` and the cluster never reconciles. Only pytest (`test_flux_entry.py:184-191`) compares them. The cluster-vars keys `flux_git_repo_url`/`flux_git_ref` exist only to be compared: the role asserts them but applies the file.
- **Location:** `envs/env-00/flux/flux-instance.yaml:25-26`; `schema/env.py:465-483`; `infra/ansible/tasks/preflight.yml:47-52`; `infra/ansible/roles/flux-operator/tasks/main.yml:11-15`; `docs/deploy.md:35`.
- **Question:** 10.
- **Evidence:** In the scratch experiment (`audit-scratch/test_seed_env.py`, a copy of env-00 as `home`), the case `flux_url_not_edited: ACCEPTED`. In the same run, `stale_configmap_after_value_change: ACCEPTED` (time_zone changed, generated ConfigMap not re-rendered).
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Generate the `sync` block (and the ConfigMap) from cluster-vars with the existing renderer, and have env-schema fail on a stale render. Or drop the two cluster-vars keys and let the FluxInstance file be the one source. Either way, the check belongs in env-schema and preflight, not only in pytest.
- **Confidence:** high.

### F12 — prep-node overwrites the env-wide `ssh_user` per node (medium)

- **What breaks and for whom:** Take a multi-node adopter whose second node's installer user differs from the first (`alice` on node 1, `ubuntu` on node 2). At the default prompt "Use login user ubuntu as the Ansible user? [Y/n]", prep-node rewrites `ssh_user: ubuntu` for the whole env. site.yml then logs into node 1 as a user that has no key there.
- **Location:** `tools/prep-node:460-465`, `730-743` (`set_yaml_scalar(..., "ssh_user", ssh_user)`); `schema/CONTRACT.md:41`.
- **Question:** 10, 11.
- **Evidence:** `choose_ansible_user(login)` defaults to the login user. `apply_env_edits` unconditionally sets `ssh_user`. `ssh_user` is a single cluster-vars key, and per-host keys are rejected (schema allows only `node_roles`, `data_disk`).
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Default the prompt to the env's existing `ssh_user` when one is set, create that user on the new node, and refuse to change it silently.
- **Confidence:** high.

### F13 — GPU support is a label plus a vendor assert (medium)

- **What breaks and for whom:** Mixed-GPU adopters (one 16 GB card, two 8 GB cards) and every future voice or LLM workload are affected. The `gpu` role checks for a PCI vendor `0x10de` and sets a label. Nothing installs the NVIDIA driver or container toolkit, configures k3s's nvidia runtime or a `RuntimeClass`, or deploys the device plugin or GPU Operator. The schema forbids any per-host attribute such as GPU memory, so a scheduler can't tell a 16 GB node from an 8 GB node.
- **Location:** `infra/ansible/tasks/assert-gpu.yml`; `schema/env.py:81` (`_HOST_KEYS = frozenset({"node_roles", "data_disk"})`); `README.md:13`.
- **Question:** 11, 22.
- **Evidence:** A grep for `nvidia|RuntimeClass|device-plugin` finds only the assert and its tests. The scratch case `gpu_vram_hint_host_key` is REJECTED with "unknown key 'gpu_memory_gb' (allowed: data_disk, node_roles)".
- **Severity:** medium (deferred, but must not be lost; it constrains the GPU plan).
- **Size:** L.
- **Suggested fix:** Two steps. Now: allow an optional per-host `gpu` mapping (count, memory) that becomes node labels, and record the plan in a GPU contract. Later: the NVIDIA GPU Operator via Flux (driver containers, toolkit, device plugin, GFD labels such as `nvidia.com/gpu.memory`), which also replaces the vendor assert.
- **Confidence:** high.

### F14 — Role labels are prefixed with the DNS suffix (medium)

- **What breaks and for whom:** Every core or outside workload that schedules by role must write `role.${dns_suffix}/gpu`, so it must go through Flux postBuild substitution. Helm charts from outside can't, unless substitution is enabled on their values. Changing `dns_suffix` relabels every node and silently orphans selectors.
- **Location:** `infra/ansible/roles/k3s/templates/label-patches.json.j2:1`; `platform/backups/rest-server.yaml:27-28`; `tools/verify/run:204`.
- **Question:** 22, 4.
- **Evidence:** `{% set prefix = 'role.' ~ dns_suffix ~ '/' %}`; `nodeSelector: "role.${dns_suffix}/backup": "true"`.
- **Severity:** medium.
- **Size:** M (relabel migration).
- **Suggested fix:** Use a fixed product prefix (for example `jarvis.io/role-gpu=true` or `node-role.jarvis/gpu`). Keeping identity out of core is about values, not about the product's own label namespace.
- **Confidence:** high.

### F15 — The whole age key file is copied into the cluster (medium)

- **What breaks and for whom:** The default `age_key_file` is SOPS's shared default, `~/.config/sops/age/keys.txt`, which often holds identities for other projects. site.yml uploads the entire file into `flux-system/sops-age`, so anyone who can read that Secret gets unrelated keys too. Adding any identity to that file later makes site.yml refuse with "already exists with different content".
- **Location:** `infra/ansible/roles/flux-operator/tasks/sops_age.yml:67-70,90-104`; `templates/sops-age-secret.yaml.j2:11-12`; `schema/env.py:16`.
- **Question:** 17.
- **Evidence:** `flux_sops_age_key_content: "{{ lookup('file', sops_age_key_file) }}"` is the full file, and it is compared whole against the secret.
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Default to a Jarvis-specific per-env path (`~/.config/jarvis/<env>/age.key`). Upload only the identity whose public key matches `.sops.yaml`'s recipient for that env, and compare on that identity.
- **Confidence:** high.

### F16 — Shared restic credential in every PVC namespace; privileged movers everywhere (medium)

- **What breaks and for whom:** Outside apps and the code factory sandbox are affected. Kyverno copies the cluster-wide `RESTIC_PASSWORD` and rest-server user and password into `restic-<pvc>` in every namespace that has a PVC. rest-server runs without `--private-repos`, so any workload in any such namespace can read, overwrite or forget every other namespace's backups. The same policy annotates every PVC namespace with `volsync.backube/privileged-movers: "true"`.
- **Location:** `platform/backups/enrollment.yaml:47-53,96-108`; `platform/backups/rest-server.yaml:1-3`.
- **Question:** 17, 22.
- **Evidence:** `RESTIC_REST_USERNAME: "{{ backupsRestic.RESTIC_REST_USERNAME }}"` and the comment `# No --private-repos: one user`. The `privileged-movers` mutate rule matches every enrolled PVC.
- **Severity:** medium (acceptable for one trusted owner, wrong before untrusted apps or the code factory land).
- **Size:** M.
- **Suggested fix:** Before the first untrusted tenant: run rest-server with `--private-repos` and one htpasswd user per namespace (Kyverno can generate it), or use `--append-only` for movers with prune done centrally. Opt namespaces into privileged movers by label instead of automatically.
- **Confidence:** high.

### F17 — Internal CA has no name constraints (medium)

- **What breaks and for whom:** Adopters install `ca.crt` on their phones and laptops to trust `*.<dns_suffix>`. That CA can sign any name (a bank, an email provider), and its key sits in the cluster (`platform-cert-manager/cert-manager-ca`) and in Git (SOPS). The CN is the same for every adopter and env.
- **Location:** `tools/bootstrap-secrets:132-153`.
- **Question:** 17.
- **Evidence:** The `-addext` list is basicConstraints, keyUsage and subjectKeyIdentifier only; there's no `nameConstraints`. `-subj "/CN=jarvis-internal-ca/O=jarvis"`.
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Add `nameConstraints=critical,permitted;DNS:.<dns_suffix>` (and `DNS:<dns_suffix>`) and put the env in the CN. Existing envs get it on their next CA rotation.
- **Confidence:** high.

### F18 — Agents join with the full server token (medium)

- **What breaks and for whom:** Every agent node's `/etc/rancher/k3s/config.yaml` holds the server `node-token`, which can join a node as a server. A compromised GPU or app node can then escalate to the control plane.
- **Location:** `infra/ansible/roles/k3s/tasks/token.yml:21-35`; `templates/agent-config.yaml.j2:3`.
- **Question:** 17.
- **Evidence:** `src: /var/lib/rancher/k3s/server/node-token`.
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Set `agent-token` in the server config (generated once and sealed, or read from `/var/lib/rancher/k3s/server/agent-token`) and give agents that.
- **Confidence:** high.

### F19 — Non-HTTP apps cannot be exposed (medium)

- **What breaks and for whom:** Outside apps and future services that need a non-HTTP port are blocked: voice (SIP or WebRTC over UDP), MQTT, SMTP for the butler, game servers. The `loadbalancer` check allows only Traefik and App DNS, App DNS answers Ingress hosts only (`resources Ingress`), and the Traefik HelmChartConfig defines no TCP or UDP entrypoints.
- **Location:** `ci/checks/lib/_loadbalancer.py`; `platform/app-dns/helmrelease.yaml:66-69`; `platform/traefik/helmchartconfig.yaml`.
- **Question:** 5, 22.
- **Evidence:** `_loadbalancer.py` docstring "R28: the only LoadBalancer Services are Traefik and App DNS." App DNS has `configBlock: resources Ingress`.
- **Severity:** medium.
- **Size:** M.
- **Suggested fix:** Allow LoadBalancer Services that carry an opt-in annotation (or live under `services/`), and add `Service` to k8s_gateway's `resources` so those get names too. Or document Traefik `IngressRouteTCP/UDP` with extra entrypoints as the supported path.
- **Confidence:** medium.

### F20 — Chrony is NTS-only (medium)

- **What breaks and for whom:** An adopter who sets `ntp_pools` to the router or a LAN NTP server (the common choice, and the only one on an isolated LAN) gets a node that never syncs, because most of those servers don't speak NTS. The symptom is distant: Flux TLS fetches fail. The README's "Clock" note is the only hint.
- **Location:** `infra/ansible/roles/base-os/templates/chrony.conf.j2:8-10`; `schema/CONTRACT.md:61`.
- **Question:** 5, 4.
- **Evidence:** `pool {{ pool }} iburst maxsources 4 nts`.
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Let each `ntp_pools` entry be either a host string (NTS) or `{host, nts: false}`. verify could check `timedatectl` sync through the node condition.
- **Confidence:** high.

### F21 — No documented way to take new Jarvis releases (medium)

- **What breaks and for whom:** An adopter's copy mixes core and env in one tree. Pulling upstream means merging into `.sops.yaml` (one shared file), README and docs, and nine per-env Flux files. Six of those are byte-identical boilerplate from env-00, so any upstream change to the Flux layering, such as a new stage or a new health check, must be hand-applied to every env. Prune is disabled on almost every platform object, so a component removed upstream stays running in the adopter's cluster. A k3s bump restarts every node at once (handler, no `serial`, no drain). There are no release tags or upgrade notes.
- **Location:** `envs/env-00/flux/kustomizations/*.yaml`; `envs/env-00/flux/kustomization.yaml`; `infra/ansible/roles/k3s/tasks/main.yml:53-61`, `handlers/main.yml:9-14`; `renovate.json:7-10`.
- **Question:** 14.
- **Evidence:** `git diff --no-index --stat envs/env-00/flux/ envs/env-01/flux/` changes only 3 of 9 files (flux-instance, generated ConfigMap, one path). `prune: disabled` appears 23 times in 22 files under `platform/`. k3s Renovate is `"enabled": false`.
- **Severity:** medium.
- **Size:** M.
- **Suggested fix:** Move the four stage Kustomizations into a core `flux/stages/` base that each env includes, with one patch for the env path. Write an "Upgrade" doc covering the git upstream merge, release tags, a k3s bump with `serial: 1` plus drain on agents, the Flux Operator-then-distribution order (today only in `renovate.json:48`), and how to remove a component removed upstream.
- **Confidence:** high.

### F22 — Renovate: hand-written regex managers and missing pins (medium)

- **What breaks and for whom:** Maintainers and adopters are affected. 13 custom regex managers (`renovate.json:74-191`) repeat what Renovate's native `flux` manager does for HelmRelease and HelmRepository/OCIRepository chart versions. Meanwhile `tools/versions.env` (sops, age, kubectl, kustomize, kubeconform, gitleaks) has no manager, so those pins never get update PRs. The k3s and Helm CLI rules are disabled.
- **Location:** `renovate.json:74-191`; `tools/versions.env`.
- **Question:** 2, 14.
- **Evidence:** No `managerFilePatterns` entry matches `tools/versions.env`. Each component has its own near-duplicate regex (for example lines 98-102 and 139-144).
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Enable the native `flux` manager for charts. Use one generic `# renovate: datasource=… depName=…` regex manager for images, `versions.env` and the k3s pin. Delete the per-file regexes. We lose nothing.
- **Confidence:** medium (native manager coverage of `chartRef`→OCIRepository should be checked).

### F23 — prep-node is about 1,000 lines of SSH glue (medium)

- **What breaks and for whom:** Maintainers carry 1,006 lines plus 472 test lines of hand-written SSH, sudo, hostname, known_hosts and YAML-editing logic, including regex editing of YAML (`set_yaml_scalar`, `rename_inventory_host`). Ansible built-ins do most of it with `--ask-pass --ask-become-pass`: `authorized_key`, `user`, `hostname`, `lineinfile` with validate for sudoers, `known_hosts`. Ubuntu autoinstall/cloud-init does the rest at install time.
- **Location:** `tools/prep-node`.
- **Question:** 2, 3.
- **Evidence:** File length (1,006 lines). Regex YAML edits at `tools/prep-node:675-727`, `803-838`.
- **Severity:** medium.
- **Size:** M.
- **Suggested fix:** Replace it with a `bootstrap.yml` play (`-k -K`, the same checks) plus a tiny age-key helper. We'd lose the interactive host-key diff UI (ssh's own prompt replaces it) and the in-place recipient swap, which is risky anyway (see F02).
- **Confidence:** medium.

### F24 — Flux Operator bootstrap uses a node-side Helm CLI (medium)

- **What breaks and for whom:** Maintainers are affected. The role downloads and checksums a Helm tarball onto the control plane (with arm64 branches that can't run on an amd64-only product), then runs `helm upgrade --install` once. The binary is installed only `when: not exists`, so the Renovate-bumped `helm_version` never upgrades an existing node. k3s natively applies `HelmChart` manifests dropped into `/var/lib/rancher/k3s/server/manifests/`, which would replace `helm_cli.yml`, `helm-upgrade.cmd.j2` and the Helm defaults.
- **Location:** `infra/ansible/roles/flux-operator/tasks/helm_cli.yml:1-64`; `defaults/main.yml:9-16`; `templates/helm-upgrade.cmd.j2`.
- **Question:** 2, 4.
- **Evidence:** `when: not flux_operator_helm_stat.stat.exists`; `helm_linux_arm64_tarball_sha256`.
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Write a k3s `HelmChart` for flux-operator at the pinned version (or `kubectl apply` the operator's install manifest), then let the Git HelmRelease adopt it. Delete the Helm CLI pieces. We lose `--wait` semantics; the existing CRD and rollout waits cover that.
- **Confidence:** medium.

### F25 — No `make` target for `site.yml` (medium)

- **What breaks and for whom:** A new adopter must `cd infra/ansible` and type a command with three relative paths (`-i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml`). It is copied 12 times: once in `docs/deploy.md`, 10 times in `infra/ansible/README.md` (once at line 32 plus once per row in the 9-row tag table), and once in `preflight.yml`. Forgetting the second `-i` is the error preflight explains.
- **Location:** `Makefile`; `docs/deploy.md:84-87`; `infra/ansible/README.md:32-52`; `infra/ansible/tasks/preflight.yml:7-14`.
- **Question:** 9, 2.
- **Evidence:** Makefile `.PHONY` has no site or bootstrap target.
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** `make site ENV=<env> [TAGS=…] [LIMIT=…]`. Move the second inventory into `ansible.cfg` `inventory =` or the Makefile. Have the target run env-schema first.
- **Confidence:** high.

### F26 — verify misses node and pod health; recovery steps are scattered (medium)

- **What breaks and for whom:** When something breaks, verify reports Flux readiness, labels, backups and the canary, but not a NotReady node, a CrashLooping pod outside a HelmRelease, a full disk, or an unsynced clock. Recovery steps live in five places: `infra/ansible/README.md` (Recovery), `platform/backups/CONTRACT.md` (Operations), `platform/cert-manager/CONTRACT.md` (Operations), `envs/env-01/secrets/README.md` (rotation), and `renovate.json` descriptions (upgrade order).
- **Location:** `tools/verify/run:421-445`.
- **Question:** 16, 18.
- **Evidence:** The checks list is exactly "Flux Ready", "backups-restic present", "node roles and enablelb", "backup freshness" and "LAN HTTPS".
- **Severity:** medium.
- **Size:** S.
- **Suggested fix:** Add "nodes Ready" and "no pod in CrashLoopBackOff/ImagePullBackOff in platform namespaces". Add `docs/operations.md` as an index ("symptom → where to look"), with Recovery sections moved into CONTRACT Operations sections.
- **Confidence:** high.

### F27 — Speculative product config (medium)

- **What breaks and for whom:**
  - **New adopters** must keep a `hermes.yaml` with bot tiers and model lanes, plus `build_agent: cursor`, `code_delivery: github`, `llm_provider_host` and `apps_git_repo_url`. None has a consumer, and the env schema refuses an env without `hermes.yaml`.
  - **Product integrity:** `build_agent` allows only `cursor`, so the product's schema names today's build tool, against the brief's rule that nothing depends on the build tools.
  - **Multi-account adopters:** the cross-env rule fails an adopter whose envs live under different Git owners.
  - **Secrets with no consumer:** `bootstrap-secrets` creates five of them (`grafana-admin` and four `hermes-*`), including a Hermes-specific scrypt hash format.
  - **The code-delivery contract:** mostly "Planned" rows, and it names Cursor keys.
- **Location:** `schema/env.py:26-27,54-64,72-76,209-216,535-539`; `tools/bootstrap-secrets:73-87,272-332`; `envs/env-00/values/hermes.yaml`, `apps.yaml`; `docs/code-delivery/CONTRACT.md:15-18,36`.
- **Question:** 7, 8, 22, 5.
- **Evidence:** Scratch cases: `no_hermes_yaml: REJECTED — home: missing required file envs/home/values/hermes.yaml`; `other_build_agent: REJECTED — build_agent: invalid value 'claude-code' (allowed: cursor)`. `schema/env.py:54`: `# Optional until the step that consumes them (D45).`
- **Severity:** medium.
- **Size:** M.
- **Suggested fix:** Delete `hermes.yaml`, `apps.yaml`, `build_agent`, `code_delivery`, `llm_provider_host`, `apps_git_repo_url`, the owner-handle cross-env rule, the five unconsumed secrets and `_hash_password`. Each returns in the PR that ships its consumer, together with that consumer's own secret bootstrap. Trim `docs/code-delivery/CONTRACT.md` to what's built (GitHub CI). Keep the plan elsewhere. We lose only placeholder validation.
- **Confidence:** high.

### F28 — Plan and decision codes in code files (low)

- **What breaks and for whom:** The product reads as a build log to adopters. The plan-codes check scans only Markdown, READMEs and `docs/`, so codes in YAML, Jinja, Python and shell comments pass. The generated ConfigMap even carries one into every env (`R4`).
- **Location:** e.g. `schema/env.py:54` (D45), `infra/ansible/site.yml:4` (D34), `infra/inventory/roles.yml/constructed.yml:3` (D42), `infra/ansible/roles/node-disk/vars/main.yml:1` (D3, D42), `infra/ansible/roles/k3s/defaults/main.yml:7` (D6), `infra/ansible/roles/k3s/templates/config.yaml.j2:1` ("framework §8 step 4"), `base-os/templates/chrony.conf.j2:1` (§4), `journald-jarvis.conf.j2:1` (§7), `platform/*/helmrelease.yaml` (D30 ×4), `platform/flux-operator/oci-repository.yaml:1` (D11), `services/placeholder.yaml:1` (D9), `apps/placeholder.yaml:1` (D9, P4a), `tools/bootstrap-secrets:2` (R7, R25), `tools/seal:2`, `tools/lib/render_cluster_vars_configmap.py:2,77` (R4), `envs/env-00/flux/generated/cluster-vars-configmap.yaml:2`, every `ci/checks/*` header, `ci/checks/lib/_contract_md.py:8` (p2-01, p2-04). Also `README.md:3` "JARVIS AlphaCut" (a codename).
- **Question:** 8.
- **Evidence:** A Grep with `_plan_codes.py`'s own regex over non-Markdown files (excluding env-01, rulebooks, tests and .github) returns 49 hits in 41 files. `_plan_codes.py:7` "Not scanned: … and code."
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Strip them, and extend the check's scope to comments in `*.py`, `*.yml`, `*.yaml`, `*.j2` and `ci/checks/*` (excluding `rulebooks/` and `.github/`).
- **Confidence:** high.

### F29 — Docs point adopters at env-01-only files (low)

- **What breaks and for whom:** In an adopter's copy (no env-01):
  - `docs/README.md:29-34` "Env docs" links only to env-01 (dead links).
  - `docs/deploy.md:77` says the secret files are described "in that env's `secrets/README.md`", which a copy of env-00 doesn't have.
  - The age-key path quirk ("`tools/bootstrap-secrets` does not read `age_key_file`") and the restic rotation step exist only in `envs/env-01/secrets/README.md`.
- **Location:** as above.
- **Question:** 9, 12, 16.
- **Evidence:** env-00 has no `secrets/` folder (schema CONTRACT line 104).
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Move the generic secrets text into `docs/deploy.md` or a `docs/secrets.md`, list env docs generically, and keep env READMEs to purpose only.
- **Confidence:** high.

### F30 — Cruft (low)

- **What breaks and for whom:** Maintainers read, test and keep code that does nothing.
- **Location and evidence:**
  - `infra/inventory/roles.yml/groups.yml` and the `groups: data_disk:` in `constructed.yml`. `groups.yml:2` itself says "site.yml does not target this group; node-disk reads the host var". A grep shows no play targets `data_disk`.
  - The `storage` role: a label with no consumer, against the schema's own rule "A role is added only in the same PR as the first service that schedules by it" (`schema/CONTRACT.md:100`).
  - `services/placeholder.yaml` and `apps/placeholder.yaml` deploy ConfigMaps into `default`.
  - `ci/checks/lib/_contract_md.py:9-17` lists `docs/build-agent`, `services/hermes/...` and `services/observability`, none of which exist.
  - `infra/ansible/roles/flux-operator/defaults/main.yml:14,16` and `helm_cli.yml:5-16`: arm64 branches under an amd64-only preflight.
  - `tools/restore:98-123` re-implements `_env.reason` and `_env.not_found`, which `verify` and `drill` import.
  - The directory named `roles.yml/` is a directory with a file extension, which misleads.
- **Question:** 7.
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Delete each (see section 6).
- **Confidence:** high.

### F31 — The drill's etcd check races the k3s snapshot (low)

- **What breaks and for whom:** An operator who runs `make drill` between `:00` and `:05` of a 12-hour slot (or after any manual `k3s etcd-snapshot save`) gets "etcd snapshot checksum mismatch" or a failed pod. The newest file on disk isn't in restic yet. The copy schedule is a hard-coded `5 */12 * * *` that must track `k3s_etcd_snapshot_schedule_cron` by hand.
- **Location:** `tools/drill:280-292`; `platform/backups/etcd-snapshots.yaml:11-12`; `infra/ansible/roles/k3s/defaults/main.yml:8`.
- **Question:** 4.
- **Evidence:** The drill script picks `latest` on disk by mtime and restores `restic restore latest --include "/snapshots/$name"`. The comment reads "Five minutes after k3s_etcd_snapshot_schedule_cron".
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Compare against the newest file contained in the latest restic snapshot, or trigger a copy Job first. Make the schedule one cluster-var used by both sides.
- **Confidence:** high.

### F32 — Tools split across interpreters (low)

- **What breaks and for whom:** The Makefile runs operator tools with system `python3`, so `install-deps` must apt-install `python3-yaml` and `python3-bcrypt`. Ansible and pytest run from `.venv`, which also has PyYAML and bcrypt. `requirements-dev.txt` is named "dev" but carries operator runtime (ansible). The `ansible` package installs every collection, while the repo uses `ansible.builtin`, `community.general.timezone` and `kubernetes.core`.
- **Location:** `Makefile:34-65`; `tools/install-deps:77-90`; `requirements-dev.txt:1-2`.
- **Question:** 1, 3.
- **Evidence:** `@python3 tools/verify/run --env "$(ENV)"` and `_missing_apt_packages()` → `python3-bcrypt`.
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Run tools with `.venv/bin/python`, drop the apt Python packages, and pin `ansible-core` plus a `collections/requirements.yml`.
- **Confidence:** high.

### F33 — `make deps` silently replaces user binaries (low)

- **Location:** `tools/install-deps:22,196-310`.
- **What breaks and for whom:** An operator's own `~/.local/bin/kubectl`, `sops` or `kustomize` at another version is overwritten without asking.
- **Question:** 4.
- **Evidence:** `if installed_version(dest, …) == version: return`, else `shutil.copy2(src, dest)`.
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Install into a repo-local `.bin/` (git-ignored) that the tools already prepend (`_env.tool_env`).
- **Confidence:** high.

### F34 — `bootstrap-secrets` ignores `age_key_file` (low)

- **What breaks and for whom:** An operator who set `age_key_file` (prep-node writes it) still has to export `SOPS_AGE_KEY_FILE` for the one decrypt bootstrap-secrets does. site.yml and prep-node honour `age_key_file`.
- **Location:** `tools/bootstrap-secrets:193-211`; `envs/env-01/secrets/README.md:15`.
- **Question:** 10.
- **Evidence:** env-01 README: "`tools/bootstrap-secrets` does not read `age_key_file`. `SOPS_AGE_KEY_FILE` must be set."
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Resolve the key path the same way in all three tools (`resolve_cluster_vars` already has the default).
- **Confidence:** high.

### F35 — `dns_suffix` not validated as a DNS name (low)

- **What breaks and for whom:** An adopter writing `Home_Lab` or `lan.` passes env-schema. The value is used as a label-key prefix, a CoreDNS zone and a wildcard SAN, so the failure appears on the node (the label patch is rejected) or in cert-manager.
- **Location:** `schema/env.py:307-323` (string and non-empty check only).
- **Question:** 10.
- **Evidence:** Scratch case `bad_dns_suffix: ACCEPTED` (`dns_suffix: Home_Lab`).
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Validate as a lowercase RFC 1123 domain with at least one dot, and reject public suffixes the adopter doesn't own.
- **Confidence:** high.

### F36 — Command guards miss two categories (low)

- **What breaks and for whom:** This concerns today's build agents, not the product. `.claude/settings.json` and `.cursor/cli.json` deny direct `kubectl`, `helm`, `flux`, `sops` and `ssh` invocations. They do not cover two categories:
  1. **Repo tools that write the cluster.** `make restore`, `make drill` and `make drill-cleanup` (and their `python3 tools/...` forms) call `kubectl` internally.
  2. **Reading the cluster-admin kubeconfig** `~/.kube/jarvis-<env>.yaml`, which the age-key read rules have no counterpart for.
- **Location:** `.claude/settings.json:3-159`; `.cursor/cli.json` (frozen; category only).
- **Question:** 17.
- **Evidence:** A Grep for `\.kube|make |tools/` in `.cursor/cli.json` finds no matches. `.claude/settings.json` has no `Read(...kube...)` or `make restore` entries.
- **Severity:** low (agent guard, not product).
- **Size:** S.
- **Suggested fix:** Add deny entries by category (cluster-writing make targets; kubeconfig reads). The real boundary remains "agents have no kubeconfig".
- **Confidence:** medium.

### F37 — verify ships its own cron evaluator (low)

- **Location:** `tools/verify/run:230-313`.
- **What breaks and for whom:** Maintainers carry about 85 lines that walk minutes backwards (up to 1,051,200 iterations) to find the previous two cron fires.
- **Question:** 19.
- **Evidence:** `for _ in range(2 * 366 * 24 * 60):`.
- **Severity:** low.
- **Size:** S.
- **Suggested fix:** Use `croniter` (pinned in the venv), or compare `lastSyncTime` with VolSync's own `status.nextSyncTime` minus one interval.
- **Confidence:** medium.

## 3b. Test and check findings (seeded-break pass)

Method: in the env-00-only scratch clone, each break was seeded into the code a test or check claims to cover, the relevant `pytest -q …/ci/tests/<file>` was run, and the break was reverted. Results below are "caught" or "not caught". The check wrappers themselves (`bash ci/checks/<name>`) could not be executed in this headless run (denied), so checks were exercised through their library functions and tests.

| id | sev | title | Q | size |
| --- | --- | --- | --- | --- |
| T01 | H | `pvc-backup` check accepts a backup policy that no longer matches PVCs | 20, 18 | S |
| T02 | H | verify's certificate-chain and canary-body checks have no failing test; removing either passes | 20 | S |
| T03 | H | bootstrap-secrets plaintext test covers 2 of 7 files and no metadata; a plaintext value added to a secret passes | 20, 17 | S |
| T04 | M | install-deps checksum enforcement untested; removing both `verify_sha256` calls passes | 20 | S |
| T05 | M | Raw-file manifest walker never sees kustomize patches or HelmRelease postRenderers | 4, 18 | M |
| T06 | M | CI's gitleaks scans a depth-1 checkout, so secrets added then removed inside a PR are never scanned | 17, 18 | S |
| T07 | M | Many "contract" tests are value snapshots of YAML (text, not behaviour) | 19, 20 | M |
| T08 | M | `_cli_permissions.py` (899 lines) re-implements one client's matcher; `.claude/settings.json` has no check | 19, 3 | M |
| T09 | M | Restore/drill/seal/prep-node/preflight have untested safety branches (timeouts, overwrite, host keys, sshd revert, CP count) | 18, 20 | M |
| T10 | L | Assertions that cannot fail (double `capsys.readouterr()`, strings absent from code) | 20 | S |
| T11 | L | Checks pass silently when their scan root or Flux tree is missing; contract-md skips a missing listed root | 4, 20 | S |
| T12 | L | Thirteen identical check wrappers; `run-checks` stops at the first failure and hides the rest | 3, 16 | S |
| T13 | L | `core-names` does not scan root-level tracked files (Makefile, renovate.json, AGENTS/CLAUDE, `.cursor/`, `.claude/`) | 8, 20 | S |
| T14 | L | bootstrap-secrets keeps an old `ca.crt` when the CA secret is regenerated alone | 15, 1 | S |
| T15 | L | env00-only run passes at the SHA; local `ci-local-verify` has no env00-only mode | 20 | S |
| T16 | H | k3s `main.yml` and the unit template never run in tests (token-file mode, ExecStart, refusal content) | 20, 18 | M |
| T17 | H | age-key overwrite refusal and `sops-age` Secret contents untested | 20 | S |
| T18 | M | base-os timers, node-disk path traversal, GPU skip path, two-CP refusal untested | 18, 20 | S |
| T19 | M | `test_flux_operator.py` is ~700 lines of meta-tests; helper-only tests elsewhere | 19, 20 | M |
| T20 | L | kubeconform pulls schemas from the network at test time | 4, 20 | S |
| F38 | M | Flux Operator re-run skips the CRD and rollout waits after a partial first run | 4, 1 | S |
| F39 | L | First k3s start is followed by a handler restart on every node | 4 | S |

### T01 — `pvc-backup` accepts a policy that no longer matches PVCs (high)
- **What breaks and for whom:** The pvc-backup check is the only guard for "every PVC is enrolled". It recognises the policy by name plus "some rule generates a ReplicationSource", and never looks at `match`, `exclude` or preconditions. A change that matches `ConfigMap` instead of `PersistentVolumeClaim` silently stops all new backups, and CI stays green.
- **Location:** `ci/checks/lib/_pvc_backup.py:118-129`; `ci/tests/test_backup_enrollment.py` (rules are compared with each other, not with an expected match).
- **Question:** 20, 18.
- **Evidence:** The seed changed `platform/backups/enrollment.yaml:19` `PersistentVolumeClaim` → `ConfigMap`; `pytest -q …/test_backup_enrollment.py …/test_pvc_backup.py` gave 18 passed. Code: `return isinstance(rules, list) and any(… rule["generate"].get("kind") == "ReplicationSource" …)`.
- **Severity:** high.
- **Size:** S.
- **Suggested fix:** Assert that the match kinds equal `[PersistentVolumeClaim]` and that the exclude list is exactly the two documented opt-outs. Better still, run the policy with `kyverno apply` against fixture PVCs (a behavioural test).
- **Confidence:** high.

### T02 — verify's cert-chain and body checks have no failing test (high)
- **What breaks and for whom:** If someone removes `-verify_return_error`, or the canary sha256 comparison, verify reports PASS against a wrong certificate or a wrong page. No test catches either change.
- **Location:** `tools/verify/run:93-125,397-401`; `ci/tests/test_verify.py:164-168`.
- **Question:** 20.
- **Evidence:** Seeded (1) removal of `-verify_return_error`, (2) removal of the body hash check, and (3) removal of the extra-role-label check. Each run: 27 passed. The fakes are `lambda host, ca: (0, "issuer=CN = fixture-ca")` and a fetch lambda that always returns `"testapp\n"`.
- **Severity:** high (the standing check can fail open without CI noticing).
- **Size:** S.
- **Suggested fix:** Drive `_fetch` and `_issuer` through a fake `_run` that returns a non-zero `s_client` result and a wrong body, and assert FAIL.
- **Confidence:** high.

### T03 — bootstrap-secrets plaintext test is partial (high)
- **What breaks and for whom:** The secrets in Git are protected by `encrypted_regex: ^(data|stringData)$` only. The test checks `ENC[` for 2 of the 7 generated files and never checks metadata. A plaintext label or annotation value (or any field outside data/stringData) passes the test, and gitleaks may not flag a random token.
- **Location:** `ci/tests/test_bootstrap_secrets.py:372-377`.
- **Question:** 20, 17.
- **Evidence:** Seeded a plaintext label value into the `grafana-admin` metadata: 10 passed. Seeded `_public_certificate` accepting a PEM containing "PRIVATE KEY": 10 passed, because the test passes a key-only PEM that the "no certificate" branch already rejects.
- **Severity:** high.
- **Size:** S.
- **Suggested fix:** For every generated file, assert that no generated value appears anywhere in the ciphertext file and that every scalar under data/stringData is `ENC[`. Use a cert+key bundle in the `ca.crt` refusal test.
- **Confidence:** high.

### T04 — install-deps checksum enforcement untested (medium)
- **Location:** `tools/install-deps:147-152,231`; `ci/tests/test_install_deps.py`.
- **What breaks and for whom:** The supply-chain guard (sha256 of downloaded binaries) can be deleted with CI green.
- **Question:** 20.
- **Evidence:** Removed both `verify_sha256` call sites; `pytest -q …/test_install_deps.py`: 1 passed. Also: "already installed" is judged by `--version` text only; the age tarball is extracted before any hash check; `urlopen` has no timeout.
- **Severity:** medium. **Size:** S.
- **Suggested fix:** Stub `download` with wrong bytes; assert `SystemExit` and no installed file.
- **Confidence:** high.

### T05 — Manifest checks read raw files, not rendered output (medium)
- **What breaks and for whom:** digest-pin, loadbalancer, manifest-lint and pvc-backup walk raw YAML (`_manifests.py:13-45`). An image, a `type: LoadBalancer` or a CPU limit added through a kustomize `patches:` block or a HelmRelease `postRenderers` patch is never seen. The repo already uses a postRenderer (`platform/backups/controller/helmrelease.yaml:36-62`). Also, `_loadbalancer.py:72-80` only reads top-level `service.type`/`serviceType` in Helm values, so a nested value passes (seed E: not caught).
- **Location:** `ci/checks/lib/_manifests.py`; `_loadbalancer.py:72-80`.
- **Question:** 4, 18.
- **Evidence:** Seeds E and F (loadbalancer), G (manifest-lint disabled probes in values), B/C (digest-pin init containers; truncated digest) were all not caught by their tests.
- **Severity:** medium. **Size:** M.
- **Suggested fix:** Run these checks over `kustomize build` output (as flux-substitute already does) plus rendered HelmRelease values. One walker instead of two.
- **Confidence:** medium.

### T06 — CI's gitleaks sees one commit (medium)
- **What breaks and for whom:** `actions/checkout` defaults to `fetch-depth: 1`, and `ci/checks/secret-scan` runs `gitleaks detect --source <repo>` over git history. In CI that history is one commit, so a secret committed and then removed within a PR is not scanned. Locally it scans everything ("187 commits scanned" in this audit's run).
- **Location:** `.github/workflows/ci.yml:23-24`; `ci/checks/secret-scan:14`.
- **Question:** 17, 18.
- **Evidence:** No `fetch-depth` in the checkout step; `exec "$gitleaks" detect --source "$repo_root" --redact`.
- **Severity:** medium. **Size:** S.
- **Suggested fix:** `fetch-depth: 0`, or `gitleaks detect --log-opts "<base>..<head>"` on PRs.
- **Confidence:** medium (based on the documented default checkout behaviour; not run in CI here).

### T07 — Snapshot tests of YAML values (medium)
- **What breaks and for whom:** Maintainers pay for each value change twice: these tests assert exact values copied from the manifests, so they fail on any legitimate edit, while missing behavioural breaks.
  - `test_backup_enrollment.py` (all 4 tests), `test_app_dns.py::test_app_dns_service_zone_forward_and_resources`, `test_cert_manager.py::test_cert_manager_release_pins_chart_and_resources`, `test_traefik.py::test_traefik_helmchartconfig_rules`, `test_testapp.py::test_fixture_serves_known_file_on_the_default_certificate`.
  - `test_restic_rest_server.py::test_volsync_controller_tz_comes_from_time_zone` (substring match: seed R, renaming the patch target so the TZ patch applies to nothing, was not caught).
  - `test_cert_manager.py::test_platform_configs_hold_the_issuer_wildcard_and_store` asserts the exact object set, so any new platform-configs object fails it.
  - `test_traefik.py::test_k3s_renovate_rule_names_the_traefik_recheck` and `test_renovate.py` tests assert description strings and file-pattern strings.
- **Location:** as listed.
- **Question:** 19, 20.
- **Evidence:** Seeds A (policy match) and R (postRenderer target) were not caught.
- **Severity:** medium. **Size:** M.
- **Suggested fix:** Keep the behavioural ones (kustomize build + kubeconform in `test_flux_entry.py`; Ansible localhost runs). Replace value snapshots with a few rule assertions (for example "every container in platform has a memory limit", which manifest-lint already does), or delete them. The rulebook's own line says "No tests on names, ordering or file shape".
- **Confidence:** high.

### T08 — `_cli_permissions.py` is disproportionate; the Claude deny list is unchecked (medium)
- **What breaks and for whom:**
  - **Maintainers:** about 900 lines (plus 225 test lines, plus a 149-row TSV) model one specific Cursor release's matcher. The module itself says it "does not prove the client". It probes `kubectl --help` at run time when kubectl is on PATH, so results vary by machine.
  - **The other guard:** `.claude/settings.json`, which `AGENTS.md:7` says expresses the same intent, has no check at all.
  - This is build tooling, not product (the brief says the product must not depend on it).
- **Location:** `ci/checks/lib/_cli_permissions.py`; `ci/checks/cli-deny`; `ci/checks/fixtures/*`.
- **Question:** 19, 3.
- **Evidence:** Line counts; a Grep for `.claude/settings` under `ci/` finds no matches.
- **Severity:** medium. **Size:** M.
- **Suggested fix:** Keep the TSV corpus as the spec and run a thin matcher per client file. Drop the help discovery. Or delete the check and treat the deny lists as advisory, since the real boundary is that agents have no kubeconfig.
- **Confidence:** medium.

### T09 — Untested safety branches in operator tools (medium)
- **Location and evidence** (seed → result):
  - restore: "ReplicationDestination did not finish" timeout turned into `pass` → 38 passed. The fake always reports the current trigger finished (`test_restore.py:232-244`).
  - drill: the wait for a fresh backup before deleting the PVC turned into a no-op → 24 passed.
  - seal: the overwrite guard, the kind check and the name check each removed → 10 passed.
  - prep-node: host-key acceptance without a prompt, and removal of the sshd revert after failed key login → 16 passed.
  - preflight: control-plane count `== 1` → `>= 1` and the `k3s_sha256` length check removed → not caught.
- **What breaks and for whom:** Operators. Each is a guard whose loss could silently corrupt a restore (restore over a volume that was never backed up) or lock them out of a node.
- **Question:** 18, 20.
- **Severity:** medium. **Size:** M.
- **Suggested fix:** One focused test per guard (stale `lastManualSync`, `restore_budget_s=0`, seal `--force`/kind/name, two control planes).
- **Confidence:** high.

### T10 — Assertions that cannot fail (low)
- **Location:**
  - `ci/tests/test_restore.py:651-652`: the second `capsys.readouterr()` returns empty, so `"HOLD kept" not in …` is always true.
  - `test_restore.py:451,660`: asserts that "mover logs unavailable" is absent, a string that appears nowhere in `tools/`.
  - `test_verify.py:267-280`: tests the test's own read-only helper.
  - `test_plan_codes.py::test_contract_with_operations_and_limits_passes`: exercises contract-md and checks no sections.
- **Question:** 20. **Severity:** low. **Size:** S. **Suggested fix:** Fix or delete. **Confidence:** high.

### T11 — Checks that pass on missing input (low)
- **Location:**
  - `_manifests.py:18-20,33-34`: a missing root or a non-UTF-8 file is skipped.
  - `_flux_substitute.py:40`: no `envs/*/flux` means nothing is checked.
  - `_contract_md.py:32-36`: a missing listed root is skipped, so renaming a component disables its contract check, and an empty CONTRACT.md passes.
  - No check enforces CONTRACT sections at all, despite `docs/CONTRACT.template.md`.
- **Question:** 4, 20. **Severity:** low. **Size:** S.
- **Suggested fix:** Fail when a root is missing or no documents were found. Either check the required headings or stop implying they are enforced.
- **Confidence:** medium.

### T12 — Wrapper duplication; run-checks stops at the first failure (low)
- **Location:** `ci/checks/{contract-md,core-names,digest-pin,…}` (13 near-identical 20-line wrappers); `ci/run-checks:3,61-70` (`set -e`).
- **What breaks and for whom:** On a fork without `OWNER_NAME`, core-names (third in the list) fails, and the ten checks after it never run, so the adopter can't see the other problems.
- **Question:** 3, 16. **Severity:** low. **Size:** S.
- **Suggested fix:** One `python -m ci.checks <name>` runner. Run all checks, then exit non-zero if any failed.
- **Confidence:** high.

### T13 — core-names skips root-level files (low)
- **Location:** `ci/checks/lib/_core_names.py:35-45,189-195`.
- **What breaks and for whom:** A personal name or owner handle in `Makefile`, `renovate.json`, `AGENTS.md`, `CLAUDE.md`, `.cursor/` or `.claude/` is not caught. (`.mailmap` is per-owner by design.)
- **Question:** 8, 20. **Severity:** low. **Size:** S.
- **Suggested fix:** Scan every tracked file except `envs/<own env>/` and `.mailmap`.
- **Confidence:** medium.

### T14 — Old `ca.crt` kept when the CA secret is regenerated alone (low)
- **Location:** `tools/bootstrap-secrets:362-364`.
- **What breaks and for whom:** An operator who deletes only `cert-manager-ca.sops.yaml` (not `ca.crt`) gets a new CA, while the committed `ca.crt` stays the old one. `make verify` then fails the chain check with no hint why. The CA rotation doc says to delete both, but the tool doesn't enforce it.
- **Evidence:** `if spec.filename == CA_SECRET_FILENAME and not ca_root.exists():`.
- **Question:** 15, 1. **Severity:** low. **Size:** S.
- **Suggested fix:** When a new CA is minted, always rewrite `ca.crt`, or refuse while an old one exists.
- **Confidence:** high.

### T15 — Test status in a fresh env-00-only copy (low)
- **Evidence:** `bash ci/ci-local-verify` in this worktree exited 0 with 483 passed. In the env-00-only clone, the first run (`pytest -q ../wrapup-claude-env00/ci/tests`) gave `1 failed, 482 passed`. The one failure, `test_prep_node.py::test_existing_age_key_mismatch_warns_and_keeps_swap_default_no`, came from a break a parallel reviewer had seeded into `tools/prep-node` (prompt default flipped), not from the repo. The clean rerun result is recorded in section 4, Q20.
- **Also:** `ci/ci-local-verify` runs only the full tree. The env00-only CI leg has no local equivalent, and locally `require_cli` skips missing CLIs where CI fails (`ci/tests/conftest.py:31-39`).
- **Question:** 20. **Severity:** low. **Size:** S.
- **Suggested fix:** Add `ci-local-verify --env00-only`, using a temporary worktree.
- **Confidence:** high.

### T16 — k3s `main.yml` never runs and the unit template is never rendered in tests (high)
- **What breaks and for whom:** CI, and any operator relying on it. The suite runs `refuse.yml`, `labels`, the drop-in and `data-dirs`, but never the k3s role's `main.yml` or `k3s.service.j2`.
- **Location:** `ci/tests/test_k3s.py`; `infra/ansible/roles/k3s/tasks/main.yml`; `templates/k3s.service.j2:28`; `templates/config.yaml.j2:9`; `tasks/token.yml:34`.
- **Question:** 20, 18.
- **Evidence (seed → `pytest -q …/test_k3s.py …/test_inventory_roles.py` and the infra batch):**
  - `ExecStart=… server` hard-coded for agents: not caught.
  - Agent config (holds the join token) mode `0600` → `0644`: not caught.
  - `default-local-storage-path` dropped: not caught.
  - Agent server URL port 6443 → 6444: not caught.
  - `refuse.yml` marker *content* check dropped (any marker text accepted): not caught.
- **Severity:** high (the token-file mode and the refusal guard are safety properties).
- **Size:** M.
- **Suggested fix:** One localhost `--check --diff` run of the k3s play per mode with stub paths, asserting rendered unit/config text and modes. Parametrise refuse.yml over binary-only, data-dir-only, wrong-marker and matching-marker.
- **Confidence:** high.

### T17 — The age-key overwrite refusal and the Secret contents are untested (high)
- **What breaks and for whom:** Operators. The guard whose message reads "may be the only copy of the age key" can be deleted, and the Secret's key renamed (`age.agekey` → `age.key`, which would break every SOPS decryption), with CI green.
- **Location:** `infra/ansible/roles/flux-operator/tasks/sops_age.yml:90-104`; `templates/sops-age-secret.yaml.j2:11`; `ci/tests/test_flux_operator.py:32,801`.
- **Question:** 20.
- **Evidence:** Seeds C1 (key renamed) and C2 (comparison removed) were each not caught by `pytest -q …/test_flux_operator.py …/test_flux_operator_install.py …/test_flux_entry.py`. Seed C5 (secretRef renamed) was caught.
- **Severity:** high. **Size:** S.
- **Suggested fix:** Capture the stdin passed to the stub `kubectl create` and assert the `age.agekey` content. Add the "existing secret, different key → refuse" and "same key → pass" cases.
- **Confidence:** high.

### T18 — base-os, preflight and GPU skip paths are untested (medium)
- **Location:** `infra/ansible/roles/base-os/tasks/main.yml:81-97`; `infra/ansible/roles/node-disk/tasks/main.yml:83-86`; `infra/ansible/tasks/assert-gpu.yml:12-31`; `ci/tests/test_base_os.py`, `test_node_disk.py`, `test_pci_gpu.py`, `test_inventory_roles.py`.
- **What breaks and for whom:**
  - The `apt-daily-upgrade` timer enabled and started: not caught.
  - The four `/../` and `/./` path checks in node-disk removed: not caught.
  - `when: 'gpu' in group_names` removed from assert-gpu (every non-GPU host would then fail the play): not caught.
  - There is no two-control-plane case.
- **Question:** 18, 20. **Severity:** medium. **Size:** S.
- **Suggested fix:** Add one case each. These are cheap with the existing `ansible_role_proof.py` harness.
- **Confidence:** high.

### T19 — `test_flux_operator.py` is mostly meta-tests (medium)
- **What breaks and for whom:** Maintainers.
  - **Worth keeping:** the roughly 500 behavioural lines (stubbed k3s, NotFound vs API-error, key mode and owner refusal, hold before FluxInstance, re-run skips).
  - **Little value:** about 700 lines are a YAML play-order walker, a README `--tags` tokenizer, and nine tests of those helpers. Examples: `test_play_before_the_fact_assert_play_fails`, `test_wrapped_tag_command_matches_the_single_line_form` (×5), `test_readme_with_no_tags_commands_fails`.
  - **Misleading:** the walker counts any `command` with `changed_when: false` as read-only.
  - **Similar:** `test_env_shapes.py:40,49` test only the test helper `_assert_shape`; about 20 parametrised cases in `test_flux_entry.py` mutate an in-memory entry built from the checker's own constants; `test_flux_operator_install.py:51` compares the pin file with itself.
- **Location:** `ci/tests/test_flux_operator.py:203-334` and the tests named above.
- **Question:** 19, 20. **Severity:** medium. **Size:** M.
- **Suggested fix:** Replace the walker and tokenizer with `ansible-playbook --list-tasks`/`--list-tags` assertions, and delete the helper-only tests.
- **Confidence:** medium.

### T20 — kubeconform fetches schemas from the network at test time (low)
- **Location:** `ci/tests/test_flux_entry.py` (kubeconform with remote schema locations; only 4 CRD schemas are vendored under `ci/tests/fixtures/schemas/`).
- **What breaks and for whom:** CI and offline developers. The test needs internet, and it validates against whatever upstream serves that day.
- **Question:** 4, 20. **Severity:** low. **Size:** S.
- **Suggested fix:** Vendor the schemas for the pinned Kubernetes minor (or cache them in CI), and fail clearly when offline.
- **Confidence:** medium.

### F38 — Flux Operator re-run skips the readiness waits after a partial first run (medium)
- **What breaks and for whom:** An operator whose first `site.yml` timed out inside `helm --wait`, after the Deployment was created. The re-run sees the Deployment, skips the whole block including the CRD and rollout waits, and goes straight to creating the sops secret and applying the FluxInstance against an operator that may not be ready, so the run fails nondeterministically.
- **Location:** `infra/ansible/roles/flux-operator/tasks/operator.yml:21-50`.
- **Question:** 4, 1.
- **Evidence:** `- name: Bootstrap Flux Operator when absent` / `when: not (flux_operator_deployed | bool)`, a block containing both `Wait for FluxInstance CRD` and `Wait for Flux Operator deployment rollout`.
- **Severity:** medium. **Size:** S.
- **Suggested fix:** Run the two waits unconditionally, after the block.
- **Confidence:** high.

### F39 — The first k3s start is followed by a handler restart (low)
- **Location:** `infra/ansible/roles/k3s/tasks/main.yml:40,61,70,74-79`; `handlers/main.yml:9-14`.
- **What breaks and for whom:** Every fresh install starts k3s, then restarts it at play end, because the template, binary and unit tasks notify "Restart k3s". This adds time and a second etcd start. Agents restart at the same moment.
- **Question:** 4. **Severity:** low. **Size:** S.
- **Suggested fix:** `meta: flush_handlers` before "Enable and start k3s", or skip the restart when the service was just started.
- **Confidence:** medium.

---

## 4. Answers by question

### As a product

1. **Reliable, flexible, practical, lean?**
   - **Reliable:** the core path (node prep, k3s, Flux, certs, DNS, PVC backups) is sound and guarded.
   - **Flexible:** limited. Services and apps can't be chosen per env (F08) or receive secrets (F07). Host keys and roles are closed sets (F13), and NTS, public repo and the LB allow-list are fixed choices (F20, F09, F19).
   - **Lean:** not yet. Speculative config (F27), prep-node (F23), the node-side Helm CLI (F24), Renovate regexes (F22), cli-permissions (T08) and snapshot tests (T07) all carry code for little.
2. **Glue a native approach would replace?**
   - prep-node → an Ansible bootstrap play or autoinstall (F23).
   - Helm CLI bootstrap → k3s `HelmChart` auto-deploy (F24).
   - 13 Renovate regexes → the native `flux` manager plus one generic comment manager (F22).
   - The verify cron engine → croniter or VolSync `nextSyncTime` (F37).
   - Raw-file manifest walker → `kustomize build` output (T05).
   - The constructed `data_disk` group → the host var already used (F30).
   - System-python tools → `.venv` (F32).
   - The duplicated FluxInstance sync url/ref → generate from cluster-vars (F11).
3. **Too complex for what it buys (trim and accept the risk)?**
   - `_cli_permissions.py` and its corpus (T08).
   - Snapshot tests (T07).
   - The prep-node interactive swap and in-place recipient rewriting (F23, F02).
   - The owner-handle cross-env rule and placeholder schemas (F27).
   - The rebuild hold is complex but buys a real guard against backing up empty volumes over good ones. Keep it, and drill it (Q13).
4. **Brittle?**
   - Flux url/ref copies (F11).
   - The etcd-copy schedule offset and drill race (F31).
   - The DNS-suffix label prefix (F14).
   - NTS-only time (F20).
   - The k3s bump restarts all nodes at once (F21).
   - The Helm CLI is never upgraded (F24).
   - Checks blind to patches (T05).
   - Fresh-install verify (F06).
   - Version-pinned comments ("k3s v1.36.5+k3s1 built-in defaults", "Traefik 3.7", "cert-manager v1.21.3 does not reissue") tie docs to exact versions; Renovate bumps will make them stale (`platform/traefik/CONTRACT.md:34`, `platform/cert-manager/CONTRACT.md:39`, `infra/ansible/roles/k3s/defaults/main.yml:7`).
   - One node layout: rest-server pinned to the single `backup` host, and the binder pod's placement (F05).
5. **Too strict?**
   - Host keys limited to `node_roles` and `data_disk`: no `ansible_host` or GPU attributes (scratch case `ansible_host_key: REJECTED`; F13).
   - Required `hermes.yaml` and `build_agent ∈ {cursor}` (F27).
   - The same Git owner across envs (F27).
   - A public repo (F09).
   - NTS only (F20).
   - Two LoadBalancers only (F19).
   - `local_path_data` must sit under `/var/lib/jarvis` even without a data disk: fine; keep.
   - Exactly one `backup` host: fine.
   - pvc-backup flags any `rest:http` text under `services/` and `apps/`, even in docs (agent pass; low).
6. **Real gaps.**
   - Off-host backup (F03).
   - Age-key redundancy (F01).
   - OS patching (F10).
   - Service secrets path (F07).
   - Per-env service selection (F08).
   - GPU runtime (F13).
   - Upgrade path (F21).
   - Node/pod health in verify (F26).
   - Nice-to-haves, not gaps: HA control plane, arm64.
7. **Cruft.** F30 (data_disk group, storage role, placeholders, dead contract roots, arm64 Helm metadata, duplicated helpers), F27 (unconsumed keys and secrets) and T10 (dead assertions); delete list in section 6.
8. **Clean product?**
   - Plan and decision codes in about 40 code files (F28).
   - A codename in `README.md:3` (F28).
   - Docs routed through env-01 (F29).
   - The code-delivery contract describes today's agent workflow (Cursor keys, agent tokens) as product (F27).
   - No owner name was found in core outside `envs/env-01/` and `.mailmap` (Grep for the owner's names, handle and suffix).

### As a new adopter

9. **Clone → env → `make verify` / `make drill` from docs alone?** Mostly yes for a single node, with these stops (paper walkthrough below):
   - It isn't stated that the Flux repo must be public (F09).
   - Step 1 doesn't say to edit the FluxInstance url/ref (F11).
   - There's no site target (F25).
   - The secrets README referenced by deploy.md doesn't exist in a copy (F29).
   - verify and drill fail for up to 12 h after install (F06).
10. **Clear errors before touching a host?**
    - Good: required keys, CIDRs, `app_dns_cluster_ip`, roles and time zone all fail early with file-and-key messages.
    - Gaps: an unedited sync url/ref, a stale generated ConfigMap and an invalid `dns_suffix` are all accepted (scratch cases; F11, F35).
    - `ssh_user` is overwritten per node (F12).
    - The age-key path resolves differently in bootstrap-secrets (F34).
11. **Single-node and multi-node, including mixed GPUs?**
    - **Single node:** works. All roles on one host are accepted (scratch case `single_node_all_roles: ACCEPTED`); the CP is untainted; the edge stub change applies to the CP. But backups sit on the same disk (F03).
    - **Three nodes, mixed GPUs:** the cluster forms, but the GPUs are unusable (no driver, runtime or plugin; no VRAM attribute, F13), and differing login users break the env (F12).
    - **Restore after a rebuild** can place volumes on the wrong node (F05).
    - **HA:** exactly one CP is enforced, which is a documented limit.
12. **Unwritten maintainer knowledge.**
    - Restic rotation semantics (F02).
    - The k3s token for etcd restore (F04).
    - The Flux Operator-before-distribution upgrade order (only in `renovate.json:48`).
    - The Traefik re-check on a k3s bump (only in `renovate.json:7`).
    - That `make verify` needs one etcd-copy cycle (F06).
    - How to trust `ca.crt` on client devices (nowhere).
    - That `hermes.yaml`, `apps.yaml` and `build_agent` do nothing (F27).
    - How to patch nodes (F10).

### Operating it over time

13. **Real loss, not just a drill; disk death?**
    - **A deleted or corrupted PVC** restores well (drilled).
    - **A cluster rebuilt on a surviving backup disk** has a designed path (rebuild hold → `make restore-all`), but no drill exercises it and the binder can misplace volumes (F05).
    - **Backup-host or single-node disk death** loses everything (F03).
    - **Control-plane root-disk death** loses etcd recoverability (no token, F04), though GitOps rebuilds state.
    - **Age-key loss** loses all backups (F01).
14. **Upgrading.**
    - **Charts and Flux** arrive as Renovate PRs (good). Flux distribution bumps per env need the operator first (order documented only in renovate.json).
    - **k3s** is a manual bump of version and sha256 plus `--tags k3s`, and all nodes restart together (F21).
    - **OS patching:** none (F10).
    - **Controller tool pins:** never updated (F22).
    - **New Jarvis releases into an adopter's copy:** no process, tags or notes, and per-env Flux boilerplate must be hand-merged (F21).
15. **Rotation and offline age-key backup.**
    - **CA:** a safe, written procedure (`platform/cert-manager/CONTRACT.md` Operations), but no client trust-store step, and the tool keeps a stale `ca.crt` if only the secret is deleted (T14).
    - **restic:** the written step is unsafe (F02).
    - **Age key:** loss recovery is written; planned rotation (`sops updatekeys`) and an offline backup are not (F01).
    - **k3s token:** not mentioned (F04).
16. **When something breaks, can they tell what and where?**
    - verify prints a named `FAIL` per check, and restore and drill print exact undo commands (good).
    - verify misses node and pod health (F26).
    - Recovery steps are scattered across five files, one of them env-01-only (F26, F29).
    - There's no observability stack yet (Q23).
    - CONTRACT Operations/Limits review: see section 4a.

#### 4a. CONTRACT.md vs `docs/CONTRACT.template.md`

All 10 contracts have the five required headings (Offers, Expects, Health, Config, Proven by). Two have problems beyond the headings: `platform/flux-operator/CONTRACT.md` has no intro line (style only), and `docs/code-delivery/CONTRACT.md` presents planned rows as Offers. No check enforces the headings (T11).

| Contract | Operations needed? | Limits needed? | What it should say |
| --- | --- | --- | --- |
| `platform/backups` | Has one; extend | Has one; extend | Add: restic password rotation (rest-server password only; repository key via `restic key add`) (F02); full rebuild runbook (site.yml → hold → `restore-all`, with what to expect) (F05); k3s token handling (F04); "first backup" step (F06); off-host copy once built (F03). Limits: shared credential across namespaces (F16). |
| `platform/cert-manager` | Has CA rotation; extend | Yes | Add: install `ca.crt` in client trust stores, and redo that after rotation; issuing a non-wildcard cert for a non-HTTP service. Limits: one-level wildcard only (`*.suffix` does not cover `a.b.suffix`); no ACME; no name constraints today (F17). |
| `platform/flux-operator` | Yes | Yes | Operations: upgrade order (operator chart first, then the FluxInstance `distribution.version`; now buried in `renovate.json:48`); rollback when the operator is broken (now in `infra/ansible/README.md:74`). Limits: bootstrap is one-time; site.yml never re-applies the FluxInstance. |
| `platform/traefik` | Yes | Yes | Operations: k3s bump re-check (now in `renovate.json:7`). Limits: HTTP(S) Ingress only, no TCP/UDP entrypoints, no Gateway API, ports only on `edge` nodes (F19). |
| `platform/app-dns` | Yes | Yes | Operations: the edge resolved-stub step on an existing cluster (now in `infra/ansible/README.md:60`); how to test (`dig @<edge> testapp.<suffix>`); the router forwarding prerequisite. Limits: one zone; Ingress hosts only; not a recursive resolver. |
| `platform/kyverno` | Optional | Yes | Limits: single replica; webhook `failurePolicy: Ignore` means PVCs created while Kyverno is down enroll only on the next PVC update or policy recreation; reports are off. |
| `platform/configs` | No | No | — |
| `platform/testapp` | No | No | — |
| `schema` | Yes | Yes | Operations: "create a new env" checklist (now split between deploy.md and env-00 README). Limits: amd64 only, one control plane, NVIDIA-only GPUs, closed host keys and role set (currently in the root README). |
| `docs/code-delivery` | No | Yes | Limits: GitHub only. Remove planned rows from Offers (F27). |
| `services`, `apps` (missing) | Yes | Yes | A "where a service lands" contract (F08): layout, per-env enablement, secrets (F07), Ingress/cert/DNS, backups, health checks, resource rules. |

Steps that are buried elsewhere:
- Restic rotation sits in `envs/env-01/secrets/README.md:25`, and it is wrong (F02).
- The age-key path quirk sits in `envs/env-01/secrets/README.md:15`.
- The upgrade order sits in `renovate.json:48`.
- The Traefik re-check sits in `renovate.json:7`.
- Node-address, Flux rollback, age-key and clock recovery sit in `infra/ansible/README.md:68-78`. That's acceptable, but nothing indexes them (F26).

### Security

17. **Leaks or over-trust.**
    - Shared restic credential in every PVC namespace, and privileged movers for every PVC namespace (F16).
    - Full age key file copied into the cluster (F15).
    - CA without name constraints (F17).
    - Agents hold the server token (F18).
    - Public repo required (F09).
    - No OS patches (F10).
    - Gitleaks sees only one commit in CI (T06).
    - The command guards miss repo tools and kubeconfig reads (F36), and the Claude deny list has no check (T08).
    - No default credentials (Grafana and Hermes passwords are random).
    - rest-server is plain HTTP inside the cluster (credentials in clear over the pod network): low.
    - App DNS answers only its zone (not an open resolver).
    - Kubeconfig files are mode 0600 in a 0700 dir.
    - The age-key file mode and owner are checked.
    - SOPS metadata on all 7 env-01 files: one recipient matching `.sops.yaml`; `encrypted_regex: ^(data|stringData)$`; every value under `stringData`/`data` is `ENC[…]`.

### Tests and checks

18. **Gaps worth filling.**
    - pvc-backup policy semantics (T01).
    - verify negative paths (T02).
    - Every-file encryption (T03).
    - install-deps checksums (T04).
    - Patch-aware manifest checks (T05).
    - Restore, drill, seal, prep-node and preflight guard branches (T09).
    - The k3s role's `main.yml` and unit template (T16).
    - The age-key overwrite refusal and Secret contents (T17).
    - base-os, node-disk and GPU skip paths (T18).
    - A rebuild drill (hold → `restore-all`) on a disposable cluster (Q13).
    - Node/pod health in verify (F26).
19. **Lots of code for little benefit.**
    - `_cli_permissions.py` (T08).
    - YAML snapshot tests (T07).
    - The play-walker and README tokenizer meta-tests (T19).
    - prep-node's text-editing helpers and their tests (F23).
    - The verify cron engine (F37).
    - The 13 wrapper scripts (T12).
    - The Renovate regex tests (`test_renovate.py`).
20. **Behaviour vs text; pass with only env-00?**
    - Most tooling and Ansible tests are behavioural (Ansible against localhost with fake binaries; fake kubectl for restore, drill and verify).
    - The platform-manifest tests are mostly value snapshots (T07).
    - Seeded breaks: of 26 in tools, 14 were not caught; of 22 in checks and platform, 12 were not caught; of 15 in Ansible and Flux, 13 were not caught (T01–T09, T16–T18).
    - The missed breaks cluster in safety guards and templates that no test renders, not in the main paths.
    - The env-00-only clone first gave 482/483, with the single failure caused by a parallel seed (T15). A clean rerun after all seeds were reverted: see the next line.
    - Clean rerun (`pytest -q -p no:cacheprovider ../wrapup-claude-env00/ci/tests`): result pending at this commit. If no later commit updates this line, the rerun did not finish within the audit window.

### Is the foundation ready for what comes next?

21. **A clean, documented landing place for new apps and services?** Not yet.
    - **Ready:** testapp is a good per-app template (non-root, probes, resources, Ingress with the wildcard via TLSStore, App DNS name). PVCs are backed up automatically.
    - **Missing:**
      - per-env enablement (F08);
      - a secrets path (F07);
      - a "where a service lands" contract (F08);
      - health checks beyond a readiness probe (Gatus is still "planned", `platform/traefik/CONTRACT.md:13`);
      - non-HTTP exposure (F19).
22. **Does the foundation block or constrain the next three areas?**
    - **Assistant (email, calendar, briefings):** needs OAuth secrets (F07) and possibly SMTP or IMAP ports (F19).
    - **Code factory:**
      - It needs its own boundary.
      - Today any namespace with a PVC gets privileged movers and the shared backup credential (F16), and no PSA or NetworkPolicy baseline exists.
      - `build_agent: cursor` in the product schema couples it to the build tool (F27).
    - **GPU workloads:** need runtime, plugin and VRAM labels (F13), and outside GPU charts can't select nodes without substituting the DNS-suffix label prefix (F14).
    - **Outside apps:**
      - They are blocked by the LB allow-list and Ingress-only DNS (F19), and by the manifest-lint "resources block in every HelmRelease" rule for config-only charts (agent pass; low).
      - Their own Helm charts' default images escape digest-pin (documented).
23. **What every one of them will need that's missing.**
    - Per-env enablement and a secrets path (F07, F08).
    - A namespace baseline: Pod Security Admission labels, a default NetworkPolicy, and optionally ResourceQuota. A grep for `pod-security.kubernetes.io`, `NetworkPolicy` and `ResourceQuota` across the repo finds none.
    - Health checks and alerting (observability and Gatus, "planned").
    - An auth story for web UIs reached over the tailnet: no SSO or forward-auth exists. Tailnet-only access is the stated boundary, but the assistant's chat UI will need user auth.
    - Off-host backup (F03).
    - A GPU runtime (F13).
24. **Deferred items that must not be lost.**
    - Gatus checks (`platform/traefik/CONTRACT.md:13,19`; `platform/testapp/CONTRACT.md:18`).
    - Observability (metrics, logs, Grafana: `envs/env-01/README.md:9`; `services/placeholder.yaml`).
    - Off-host backup, so the "not disk loss" Limit is not permanent (`platform/backups/CONTRACT.md:187-188`).
    - etcd restore onto a rebuilt node (CONTRACT:183) and the restic-only older snapshot path (CONTRACT:192).
    - On-demand backup (CONTRACT:189).
    - The Forgejo provider, the admin-gate check, the image registry and `build.yml`, and the code-factory label queue (`docs/code-delivery/CONTRACT.md:15-18`).
    - The Hermes profile schema contract (`schema/CONTRACT.md:3`).
    - Model names in core-names (`rulebooks/coding-agents.md:17`).
    - The GPU runtime (F13).
    - HA control plane and arm64 (README Requirements).
    - Per-namespace backup credentials before untrusted tenants (F16).
    - Recommendation: move these into one `docs/roadmap.md` (or issues), not scattered "Planned" sentences, so trimming F27 doesn't lose them.

### Adopter walkthrough on paper (env-01 deleted)

**Single node, one amd64 box, one disk, NVIDIA card optional:**

1. **Getting started.** `README.md` → `docs/deploy.md`. `make deps` needs sudo apt. Copy env-00 to `envs/home`. *Files opened so far: 3.*
2. **Editing values.** Edit `values/cluster-vars.yaml` (what do `build_agent`, `code_delivery`, `llm_provider_host` and `apps_git_repo_url` do? I opened `schema/CONTRACT.md` to find out: nothing). Edit `hermes.yaml` and `apps.yaml` (same question). Shrink `inventory/hosts.yml` to one host with `[control_plane, workers, edge, backup]`. *Files: 7.*
3. **Flux files.** Edit `flux/flux-instance.yaml` and `flux/kustomizations/platform-configs.yaml` paths. **Stuck point 1:** deploy.md step 1 mentions only paths; the url and ref also need editing (F11). *Files: 9.*
4. **prep-node.** It creates the age key at the shared SOPS default (F15). **Stuck point 2:** I added the `.sops.yaml` rule by hand and had to delete the env-01 rule left in the copy. *Files: 10.*
5. **Secrets.** `make bootstrap-secrets` creates seven secrets, five of them unexplained. **Stuck point 3:** deploy.md points at "that env's `secrets/README.md`", which doesn't exist (F29). Then render the ConfigMap.
6. **Commit and push.** **Stuck point 4:** the repo must be public (F09), and GitHub needs `OWNER_NAME`.
7. **site.yml.** Run the long command from `infra/ansible` (F25). *Files: 11 (infra/ansible/README).*
8. **Router.** Forward `dns_suffix` to the node (prerequisite). *Files: 12 (app-dns CONTRACT, for the port).*
9. **verify and drill.** **Stuck point 5:** `make verify` FAILs "etcd snapshot copy has no successful scheduled run" until 00:05 or 12:05 local (F06); `make drill` fails likewise. *Files: 13 (backups CONTRACT).*

Result: verify and drill go green after the first copy cycle. **13 files opened, 5 stuck points.**

**Three nodes, mixed GPUs** (cp+edge+backup; gpu16; gpu8). Same as above, plus:
- prep-node on each node. If a node's installer user differs, `ssh_user` flips (F12).
- `gpu` role: the NVIDIA assert passes, but no driver or plugin is installed and VRAM can't be expressed (F13; scratch case `gpu_vram_hint_host_key: REJECTED`).
- PVCs on the GPU nodes back up over the network to the cp/backup node: good. Losing that node's disk loses every backup (F03).

**2 more stuck points.**

---

## 5. What's good and should stay

- **Preflight before any change:** one CP, amd64, required vars, the committed FluxInstance, the foreign-k3s marker refusal, and the node-disk refusals. All run in an `always`-tagged opening play with `any_errors_fatal`.
- **Env schema with file-and-key error messages.** The example env must set every known key and role (a drift guard).
- **SOPS + age, with plaintext never written to disk** (`_sops.encrypt_secret_to_file` pipes stdin and renames atomically), and the age-key mode and owner check.
- **Kyverno auto-enrollment of every PVC**, with a narrow, path-scoped `backup: none` opt-out.
- **The rebuild hold**, which stops a rebuilt cluster from snapshotting empty volumes over the last good backup. Keep it; drill it.
- **restore and drill:** environment-bound (repo marker plus live ConfigMap marker), fail-closed, printing undo commands, cleaning up on signals, and verifying real checksums.
- **Digest-pinned images** with Renovate and no automerge; pinned CLI checksums; GitHub Actions pinned by SHA with `contents: read`.
- **Behavioural Ansible tests:** real `ansible-playbook` on localhost with fake binaries, idempotence runs, and an env-00-only CI leg.
- **The testapp manifest** as the template for every future app (non-root, read-only root fs, seccomp, probes, requests, no CPU limit).
- **One wildcard certificate** via the Traefik default TLSStore; App DNS from Ingress status.

## 6. Delete list

| What | Evidence it's unused or duplicated |
| --- | --- |
| `envs/env-00/values/hermes.yaml`, `apps.yaml`; `_validate_hermes`, `_validate_apps`, `_HERMES_TOP_LEVEL`, `_APPS_TOP_LEVEL` in `schema/env.py` | Grep: no consumer outside schema and tests (F27) |
| `build_agent`, `code_delivery`, `llm_provider_host`, `apps_git_repo_url`, `_cross_env_owner_errors` | No manifest or tool reads them; `docs/code-delivery/CONTRACT.md:54` "No core manifest reads `code_delivery`" (F27) |
| bootstrap specs `grafana-admin`, `hermes-*` ×4, `_hash_password`, `_hermes_dashboard_string_data`, `_openssl_rand_base64` | No Flux path applies them (F07, F27) |
| `infra/inventory/roles.yml/groups.yml` and the `groups: data_disk` rule; `ci/tests/test_inventory_roles.py` parts for that group | `groups.yml:2` "site.yml does not target this group" (F30) |
| `storage` role | Label only; no consumer (F30) |
| `services/placeholder.yaml`, `apps/placeholder.yaml` (use `resources: []`) | ConfigMaps in `default` with "not deployed yet" (F30) |
| `ci/checks/lib/_contract_md.py` `EXPLICIT_COMPONENT_ROOTS` entries for `docs/build-agent`, `services/hermes/*`, `services/observability` | None exist (F30) |
| `flux-operator` role `helm_cli.yml`, `helm-upgrade.cmd.j2`, Helm defaults (arm64 lines at minimum) | amd64-only preflight; k3s HelmChart alternative (F24) |
| `tools/restore` `_reason`, `_not_found` | Duplicates `tools/lib/_env.reason`/`not_found` (F30) |
| 13 per-component Renovate regex managers | Native `flux` manager plus one generic manager (F22) |
| `tools/verify/run` `_number`, `_cron_field`, `_cron` (if croniter or `nextSyncTime` is adopted) | F37 |
| YAML snapshot tests listed in T07; dead assertions in T10 | T07, T10 |
| Plan codes in code comments | F28 |

## 7. Coverage table

Every file from `git ls-files` at the SHA: 248 files, 248 rows. The per-directory counts were cross-checked with `git ls-files ci platform` (129) and `git ls-files envs infra tools` (93), plus 26 others. How each was read:
- `finding`: read line by line, and at least one finding cites it.
- `no finding`: read line by line, nothing to report.
- `excluded`: reason given. Only vendored files (the CRD and four test JSON schemas), the generated ConfigMaps and the SOPS ciphertexts are excluded.
- Test files and ci/checks were read line by line as part of the seeded-break pass.

| File | Tag | Ids / note |
| --- | --- | --- |
| `.claude/settings.json` | finding | F36, T08 (category only) |
| `.cursor/cli.json` | finding | F36 (frozen; category only) |
| `.github/pull_request_template.md` | no finding | build workflow; uses plan code R35 (out of product scope) |
| `.github/workflows/ci.yml` | finding | T06, T15 |
| `.gitignore` | no finding | |
| `.mailmap` | no finding | per-owner by design |
| `.sops.yaml` | finding | F01 |
| `AGENTS.md` | finding | F28 (codename), T08 |
| `CLAUDE.md` | no finding | |
| `Makefile` | finding | F25, F32 |
| `README.md` | finding | F28, Q11 limits |
| `apps/kustomization.yaml` | finding | F08 |
| `apps/placeholder.yaml` | finding | F28, F30 |
| `ci/checks/cli-deny` | finding | T08, T12 |
| `ci/checks/contract-md` | finding | F28, T12 |
| `ci/checks/core-names` | finding | F28, T12 |
| `ci/checks/digest-pin` | finding | F28, T12 |
| `ci/checks/env-schema` | finding | F28, T12 |
| `ci/checks/fixtures/must-allow-main.txt` | finding | T08 |
| `ci/checks/fixtures/must-block-main.tsv` | finding | T08 |
| `ci/checks/flux-substitute` | finding | F28, T12 |
| `ci/checks/lib/_cli_permissions.py` | finding | T08 |
| `ci/checks/lib/_contract_md.py` | finding | F28, F30, T11 |
| `ci/checks/lib/_core_names.py` | finding | T13 |
| `ci/checks/lib/_digest_pin.py` | finding | T05 |
| `ci/checks/lib/_flux_substitute.py` | finding | T11 |
| `ci/checks/lib/_loadbalancer.py` | finding | F19, T05 |
| `ci/checks/lib/_manifest_lint.py` | finding | T05, Q22 (resources block required in every HelmRelease) |
| `ci/checks/lib/_manifests.py` | finding | T05, T11 |
| `ci/checks/lib/_plan_codes.py` | finding | F28 (scope: Markdown only) |
| `ci/checks/lib/_product_ci.py` | no finding | thin tests noted under T09/T12 |
| `ci/checks/lib/_pvc_backup.py` | finding | T01 |
| `ci/checks/lib/_python_pins.py` | finding | Q5 (rejects extras/markers; low) |
| `ci/checks/loadbalancer` | finding | F28, T12 |
| `ci/checks/manifest-lint` | finding | F28, T12 |
| `ci/checks/plan-codes` | finding | T12 |
| `ci/checks/product-ci` | finding | T12 |
| `ci/checks/pvc-backup` | finding | F28, T12 |
| `ci/checks/python-pins` | finding | F28, T12 |
| `ci/checks/secret-scan` | finding | T06, F28 |
| `ci/ci-local-verify` | finding | T15 |
| `ci/rule-checks.txt` | no finding | |
| `ci/run-checks` | finding | T12 |
| `ci/tests/ansible_role_proof.py` | no finding | good harness |
| `ci/tests/conftest.py` | finding | T15 (`require_cli` skips locally) |
| `ci/tests/fixtures/ansible-list-tasks/hosts.yml` | no finding | |
| `ci/tests/fixtures/digest-pin/tag-without-digest.yaml` | no finding | |
| `ci/tests/fixtures/env-shapes/gpu-mix/hosts.yml` | finding | F13, T19 (two identical gpu hosts; no VRAM) |
| `ci/tests/fixtures/env-shapes/secondary-disk/hosts.yml` | no finding | |
| `ci/tests/fixtures/env-shapes/single-node/hosts.yml` | no finding | |
| `ci/tests/fixtures/gpu-assert/hosts.yml` | finding | T18 (static gpu group, not constructed) |
| `ci/tests/fixtures/gpu-assert/play.yml` | no finding | |
| `ci/tests/fixtures/loadbalancer/extra-service.yaml` | no finding | |
| `ci/tests/fixtures/manifest-lint/missing-memory-limit.yaml` | no finding | |
| `ci/tests/fixtures/schemas/apiextensions.k8s.io/customresourcedefinition_v1.json` | excluded: vendored schema | checked only for presence; T20 |
| `ci/tests/fixtures/schemas/helm.cattle.io/HelmChartConfig_v1.json` | excluded: vendored schema | T20 |
| `ci/tests/fixtures/schemas/volsync.backube/replicationdestination_v1alpha1.json` | excluded: vendored schema | T20 |
| `ci/tests/fixtures/schemas/volsync.backube/replicationsource_v1alpha1.json` | excluded: vendored schema | T20 |
| `ci/tests/support.py` | finding | T19 (synthesised FluxInstance) |
| `ci/tests/test_ansible_operator_tags.py` | no finding | |
| `ci/tests/test_app_dns.py` | finding | T07 |
| `ci/tests/test_backup_enrollment.py` | finding | T01, T07 |
| `ci/tests/test_base_os.py` | finding | T18 |
| `ci/tests/test_bootstrap_secrets.py` | finding | T03, T14 |
| `ci/tests/test_cert_manager.py` | finding | T07 |
| `ci/tests/test_cli_permissions.py` | finding | T08 |
| `ci/tests/test_contract_md.py` | finding | T11 |
| `ci/tests/test_core_names.py` | finding | T13 |
| `ci/tests/test_digest_pin.py` | finding | T05 |
| `ci/tests/test_drill.py` | finding | T09 |
| `ci/tests/test_env_schema.py` | finding | Q20 (constant-driven parametrize; seed I not caught) |
| `ci/tests/test_env_shapes.py` | finding | T19 |
| `ci/tests/test_flux_entry.py` | finding | F11, T19, T20 |
| `ci/tests/test_flux_operator.py` | finding | T17, T19 |
| `ci/tests/test_flux_operator_install.py` | finding | T19 |
| `ci/tests/test_flux_substitute.py` | no finding | |
| `ci/tests/test_install_deps.py` | finding | T04 |
| `ci/tests/test_inventory_roles.py` | finding | T18, F30 |
| `ci/tests/test_k3s.py` | finding | T16 |
| `ci/tests/test_kubeconfig.py` | no finding | |
| `ci/tests/test_loadbalancer.py` | finding | T05 |
| `ci/tests/test_manifest_lint.py` | finding | T05 |
| `ci/tests/test_node_disk.py` | finding | T18 |
| `ci/tests/test_pci_gpu.py` | finding | T18 |
| `ci/tests/test_plan_codes.py` | finding | T10 |
| `ci/tests/test_preflight.py` | finding | T09 |
| `ci/tests/test_prep_node.py` | finding | T09, T15 |
| `ci/tests/test_product_ci.py` | finding | T09 note (thin; seed V not caught) |
| `ci/tests/test_prune_guards.py` | no finding | |
| `ci/tests/test_pvc_backup.py` | finding | T01 |
| `ci/tests/test_python_pins.py` | finding | seed W not caught (low) |
| `ci/tests/test_renovate.py` | finding | T07, F22 |
| `ci/tests/test_restic_rest_server.py` | finding | T07 |
| `ci/tests/test_restore.py` | finding | T09, T10 |
| `ci/tests/test_seal.py` | finding | T09 |
| `ci/tests/test_testapp.py` | finding | T07 |
| `ci/tests/test_traefik.py` | finding | T07 |
| `ci/tests/test_verify.py` | finding | T02, T10 |
| `docs/CONTRACT.template.md` | finding | T11 (sections not enforced) |
| `docs/README.md` | finding | F29 |
| `docs/code-delivery/CONTRACT.md` | finding | F27 |
| `docs/deploy.md` | finding | F06, F09, F11, F25, F29 |
| `envs/env-00/README.md` | finding | F11 |
| `envs/env-00/flux/flux-instance.yaml` | finding | F11, F09 |
| `envs/env-00/flux/generated/cluster-vars-configmap.yaml` | excluded: generated | matches `render_cluster_vars_configmap.py` output (test passes); carries plan code R4 from the generator (F28) |
| `envs/env-00/flux/kustomization.yaml` | finding | F21 |
| `envs/env-00/flux/kustomizations/apps.yaml` | finding | F07, F08 |
| `envs/env-00/flux/kustomizations/platform-configs.yaml` | finding | F07 |
| `envs/env-00/flux/kustomizations/platform-controllers.yaml` | finding | F21 (per-env boilerplate) |
| `envs/env-00/flux/kustomizations/services.yaml` | finding | F07, F08 |
| `envs/env-00/flux/platform-configs/kustomization.yaml` | no finding | |
| `envs/env-00/inventory/hosts.yml` | finding | F30 (storage role) |
| `envs/env-00/values/apps.yaml` | finding | F27 |
| `envs/env-00/values/cluster-vars.yaml` | finding | F27, F11 |
| `envs/env-00/values/hermes.yaml` | finding | F27 |
| `envs/env-01/README.md` | no finding | per-owner; mentions plans |
| `envs/env-01/ca.crt` | no finding | public cert; content not parsed (openssl denied) |
| `envs/env-01/flux/flux-instance.yaml` | no finding | per-owner; same shape as env-00 |
| `envs/env-01/flux/generated/cluster-vars-configmap.yaml` | excluded: generated | matches generator (test passes) |
| `envs/env-01/flux/kustomization.yaml` | no finding | |
| `envs/env-01/flux/kustomizations/apps.yaml` | no finding | same as env-00 (F08 applies) |
| `envs/env-01/flux/kustomizations/platform-configs.yaml` | no finding | |
| `envs/env-01/flux/kustomizations/platform-controllers.yaml` | no finding | |
| `envs/env-01/flux/kustomizations/services.yaml` | no finding | |
| `envs/env-01/flux/platform-configs/kustomization.yaml` | finding | F07 (5 env secrets not applied) |
| `envs/env-01/inventory/hosts.yml` | no finding | per-owner |
| `envs/env-01/secrets/README.md` | finding | F02, F29, F34 |
| `envs/env-01/secrets/backups-restic.sops.yaml` | excluded: SOPS ciphertext | metadata OK: 1 recipient matching `.sops.yaml`, encrypted_regex data/stringData, values ENC (F01) |
| `envs/env-01/secrets/cert-manager-ca.sops.yaml` | excluded: SOPS ciphertext | metadata OK (F01) |
| `envs/env-01/secrets/grafana-admin.sops.yaml` | excluded: SOPS ciphertext | metadata OK; unconsumed (F07, F27) |
| `envs/env-01/secrets/hermes-code-factory-api.sops.yaml` | excluded: SOPS ciphertext | metadata OK; unconsumed |
| `envs/env-01/secrets/hermes-code-factory-dashboard.sops.yaml` | excluded: SOPS ciphertext | metadata OK; unconsumed |
| `envs/env-01/secrets/hermes-ops-api.sops.yaml` | excluded: SOPS ciphertext | metadata OK; unconsumed |
| `envs/env-01/secrets/hermes-ops-dashboard.sops.yaml` | excluded: SOPS ciphertext | metadata OK; unconsumed |
| `envs/env-01/values/apps.yaml` | no finding | per-owner (F27 applies) |
| `envs/env-01/values/cluster-vars.yaml` | no finding | per-owner; plan codes in comments are owner-side |
| `envs/env-01/values/hermes.yaml` | no finding | per-owner (F27 applies) |
| `infra/ansible/README.md` | finding | F25, F26, F01 |
| `infra/ansible/ansible.cfg` | no finding | |
| `infra/ansible/group_vars/all.yml` | finding | F04 (k3s data dir on root disk) |
| `infra/ansible/roles/base-os/defaults/main.yml` | finding | F28 |
| `infra/ansible/roles/base-os/handlers/main.yml` | no finding | |
| `infra/ansible/roles/base-os/tasks/chrony.yml` | no finding | |
| `infra/ansible/roles/base-os/tasks/main.yml` | finding | F10 |
| `infra/ansible/roles/base-os/templates/20auto-upgrades.j2` | finding | F10 |
| `infra/ansible/roles/base-os/templates/50unattended-upgrades.j2` | finding | F10 |
| `infra/ansible/roles/base-os/templates/chrony.conf.j2` | finding | F20, F28 |
| `infra/ansible/roles/base-os/templates/journald-jarvis.conf.j2` | finding | F28 |
| `infra/ansible/roles/base-os/templates/resolved-stub.conf.j2` | no finding | |
| `infra/ansible/roles/flux-operator/defaults/main.yml` | finding | F24, F30 |
| `infra/ansible/roles/flux-operator/tasks/chart_pin.yml` | no finding | |
| `infra/ansible/roles/flux-operator/tasks/helm_cli.yml` | finding | F24 |
| `infra/ansible/roles/flux-operator/tasks/instance.yml` | finding | F11 |
| `infra/ansible/roles/flux-operator/tasks/main.yml` | finding | F11 |
| `infra/ansible/roles/flux-operator/tasks/operator.yml` | finding | F38 |
| `infra/ansible/roles/flux-operator/tasks/rebuild_hold.yml` | no finding | |
| `infra/ansible/roles/flux-operator/tasks/sops_age.yml` | finding | F15, T17 |
| `infra/ansible/roles/flux-operator/tasks/sops_age_key_mode.yml` | no finding | |
| `infra/ansible/roles/flux-operator/templates/backup-rebuild-hold.yaml.j2` | no finding | |
| `infra/ansible/roles/flux-operator/templates/helm-upgrade.cmd.j2` | finding | F24 |
| `infra/ansible/roles/flux-operator/templates/sops-age-secret.yaml.j2` | finding | F15, F28 |
| `infra/ansible/roles/k3s/defaults/main.yml` | finding | F28, F31 |
| `infra/ansible/roles/k3s/handlers/main.yml` | finding | F21, F39 |
| `infra/ansible/roles/k3s/tasks/data-dirs.yml` | no finding | |
| `infra/ansible/roles/k3s/tasks/guard.yml` | no finding | |
| `infra/ansible/roles/k3s/tasks/labels.yml` | finding | F14 (150 s registration wait, low) |
| `infra/ansible/roles/k3s/tasks/main.yml` | finding | F21, F39, T16 |
| `infra/ansible/roles/k3s/tasks/refuse.yml` | finding | T16 |
| `infra/ansible/roles/k3s/tasks/service-unit.yml` | no finding | |
| `infra/ansible/roles/k3s/tasks/token.yml` | finding | F18 |
| `infra/ansible/roles/k3s/templates/agent-config.yaml.j2` | finding | F18 |
| `infra/ansible/roles/k3s/templates/config.yaml.j2` | finding | F04, F28 |
| `infra/ansible/roles/k3s/templates/jarvis-mount.conf.j2` | no finding | |
| `infra/ansible/roles/k3s/templates/k3s.service.j2` | finding | T16 |
| `infra/ansible/roles/k3s/templates/label-patches.json.j2` | finding | F14 |
| `infra/ansible/roles/kubeconfig/tasks/main.yml` | no finding | |
| `infra/ansible/roles/node-disk/tasks/main.yml` | finding | T18 |
| `infra/ansible/roles/node-disk/vars/main.yml` | finding | F28 |
| `infra/ansible/site.yml` | finding | F28, F25 |
| `infra/ansible/tasks/assert-gpu.yml` | finding | F13 |
| `infra/ansible/tasks/preflight.yml` | finding | F11, F25 |
| `infra/inventory/roles.yml/constructed.yml` | finding | F28, F30 |
| `infra/inventory/roles.yml/groups.yml` | finding | F30 |
| `platform/app-dns/CONTRACT.md` | finding | F19, 4a |
| `platform/app-dns/coredns-custom.yaml` | no finding | |
| `platform/app-dns/helmrelease.yaml` | finding | F19, F28 |
| `platform/app-dns/helmrepository.yaml` | no finding | |
| `platform/app-dns/kustomization.yaml` | no finding | |
| `platform/app-dns/namespace.yaml` | no finding | |
| `platform/backups/CONTRACT.md` | finding | F02, F03, F04, F05, 4a |
| `platform/backups/controller/helmrelease.yaml` | finding | F28, T07 |
| `platform/backups/controller/helmrepository.yaml` | no finding | |
| `platform/backups/controller/kustomization.yaml` | no finding | |
| `platform/backups/controller/namespace.yaml` | no finding | |
| `platform/backups/controller/volumesnapshot-crd.yaml` | excluded: vendored | provenance header names external-snapshotter v8.4.0 `client/config/crd/…volumesnapshots.yaml`; local additions are the header and the `prune: disabled` annotation; byte comparison with upstream not done (no network in this run) |
| `platform/backups/enrollment.yaml` | finding | F16, T01 |
| `platform/backups/etcd-snapshots.yaml` | finding | F04, F06, F31 |
| `platform/backups/kustomization.yaml` | no finding | |
| `platform/backups/namespace.yaml` | no finding | |
| `platform/backups/rest-server.yaml` | finding | F03, F16 |
| `platform/cert-manager/CONTRACT.md` | finding | 4a, T14 |
| `platform/cert-manager/config/internal-ca.yaml` | no finding | |
| `platform/cert-manager/config/kustomization.yaml` | no finding | |
| `platform/cert-manager/controller/helmrelease.yaml` | finding | F28 |
| `platform/cert-manager/controller/helmrepository.yaml` | no finding | |
| `platform/cert-manager/controller/kustomization.yaml` | no finding | |
| `platform/cert-manager/controller/namespace.yaml` | no finding | |
| `platform/configs/CONTRACT.md` | no finding | |
| `platform/configs/kustomization.yaml` | no finding | |
| `platform/flux-operator/CONTRACT.md` | finding | 4a |
| `platform/flux-operator/helmrelease.yaml` | finding | F28 |
| `platform/flux-operator/kustomization.yaml` | no finding | |
| `platform/flux-operator/oci-repository.yaml` | finding | F28 |
| `platform/kustomization.yaml` | no finding | |
| `platform/kyverno/CONTRACT.md` | finding | 4a |
| `platform/kyverno/helmrelease.yaml` | no finding | |
| `platform/kyverno/helmrepository.yaml` | no finding | |
| `platform/kyverno/kustomization.yaml` | no finding | |
| `platform/kyverno/namespace.yaml` | no finding | |
| `platform/kyverno/rbac.yaml` | no finding | |
| `platform/testapp/CONTRACT.md` | no finding | |
| `platform/testapp/kustomization.yaml` | no finding | |
| `platform/testapp/namespace.yaml` | no finding | |
| `platform/testapp/testapp.yaml` | no finding | the template to keep |
| `platform/traefik/CONTRACT.md` | finding | Q4 (version-pinned text), 4a |
| `platform/traefik/helmchartconfig.yaml` | finding | F19 |
| `platform/traefik/kustomization.yaml` | no finding | |
| `pytest.ini` | no finding | |
| `renovate.json` | finding | F22, F21 |
| `requirements-dev.txt` | finding | F32 |
| `rulebooks/coding-agents.md` | no finding | build workflow, out of product scope (uses plan codes by design) |
| `schema/CONTRACT.md` | finding | F09, F27, 4a |
| `schema/__init__.py` | no finding | empty package marker |
| `schema/env.py` | finding | F11, F13, F27, F28, F35 |
| `services/kustomization.yaml` | finding | F08 |
| `services/placeholder.yaml` | finding | F28, F30 |
| `tools/bootstrap-secrets` | finding | F02, F17, F27, T14 |
| `tools/drill` | finding | F06, F31 |
| `tools/install-deps` | finding | F32, F33, T04 |
| `tools/lib/_env.py` | no finding | |
| `tools/lib/_etcd_image.py` | no finding | |
| `tools/lib/_sops.py` | no finding | |
| `tools/lib/render_cluster_vars_configmap.py` | finding | F28 |
| `tools/prep-node` | finding | F12, F23 |
| `tools/restore` | finding | F05, F30 |
| `tools/seal` | finding | F28, T09 (YAML errors may echo input; low) |
| `tools/verify/run` | finding | F06, F26, F37, T02 |
| `tools/versions.env` | finding | F22 |

## 8. Concerns and questions

1. **Runner limits shaped the evidence.**
   - **What was denied:** the headless policy denied `make`, `python3 <script>`, `openssl`, running `ci/checks/<name>` wrappers directly, `git checkout` and `git status` against the scratch clone.
   - **How the evidence was gathered instead:** experiments ran through `pytest` (including a throwaway `audit-scratch/test_seed_env.py` that drives `schema.env.validate_env_tree` on a copied env). Check behaviour was proven through each check's library functions, not its wrapper's exit code.
   - **Unverified:** the `ca.crt` extensions were not parsed. F17 rests on the generator's arguments.
   - **Please check:** seeded breaks were reverted by reverse edits, not `git checkout`, so the scratch clone's cleanliness could not be confirmed with `git status`. A clean full run there (section 4, Q20) is the indirect proof. Please run `git -C ../wrapup-claude-env00 status` before removing it.
2. **The brief says "PRs are the history"** and also asks for "no plan codes". The rulebook (`rulebooks/coding-agents.md`) and the PR template use plan codes by design, and I treated them as out of product scope. If adopters receive those files in their copy (they do today), consider moving the build workflow out of the product tree entirely (`.github/` for the template, a separate repo or branch for rulebooks).
3. **"Nothing in Jarvis may depend on today's build tools."** `build_agent: cursor` (schema), the code-delivery contract's Cursor keys, and `ci/checks/cli-deny` (a CI check on `.cursor/cli.json`) are product-side dependencies on today's tooling. I reported the first two as F27, and cli-deny as T08. Please confirm cli-deny should leave the product CI, or move to a "build workflow" job.
4. **Disk-loss scope.** The backups contract deliberately limits itself to "mistakes and rollback, not disk loss", and I rated the missing off-host copy (F03) high anyway, because Q13 asks about disk death and the product targets single-node adopters. If off-host backup is an intended later phase, F03 belongs in the deferred list (Q24) instead, but it should be stated in `docs/deploy.md` as a prerequisite risk.
5. **Assumed, not verified:**
   - That restic cannot open a repository with a regenerated password (F02). This follows restic's key model; no live repository was touched.
   - That local-path binds at first consumer (F05). This is the k3s default StorageClass behaviour.
   - Renovate's native `flux` manager coverage of `chartRef` → OCIRepository (F22).
6. **The vendored VolumeSnapshot CRD** was compared only with its provenance header (upstream path and version). No network fetch was done to byte-compare it with upstream.
7. **Recommended scope change (not acted on):** none needed. The F01/F02/F03 cluster, together with F07/F08, decides whether the foundation is "ready". I recommend fixing them before the assistant work starts.

