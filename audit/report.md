# Wrap-up audit: judge report

Commit audited: `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1`. Judge: Claude Opus 5.5. Inputs: three findings files (A = Grok 4.7, B = GPT-5.6 Sol, C = Claude Opus 5.5), their three cross-checks, and the baseline summary and logs. No code was changed. Every scratch experiment ran in `git archive` copies or in a throwaway worktree, and was removed afterwards.

**What I ran myself**

- `make deps`. The first run failed (`E: Package 'python3-venv' has no installation candidate`, exit 2) because the VM's apt lists were empty. It passed after `sudo apt-get update`, which reproduces J32.
- `OWNER_NAME="Gordon Cooper" make ci-verify`. All 13 `ci/checks` passed; gitleaks scanned 192 commits with no leaks. Pytest gave 482 passed and 1 failed (`test_chrony_template_notifies_restart_only_when_changed`). PID 1 on this VM is `tini`, not systemd.
- The same suite in a worktree with `envs/env-01` removed. All checks passed (exit 0). Pytest again gave 482 passed and 1 failed, the same chrony test.
- The chrony fix, proven in scratch. With the three `ansible.builtin.service:` handlers in `base-os/handlers/main.yml` switched to `ansible.builtin.systemd:`, the failing test passes (`1 passed`). I reverted the change.
- A schema harness (outside the repo) that copies HEAD, makes an env `lab` from env-00, then mutates it and runs `validate_env_tree`. Results are cited per finding.
- Direct probes of `_validate_repo_url` and the command-guard matcher (results by category only), and `kustomize build` of an empty `resources: []` kustomization (exit 0).
- Code reads for every critical and high finding and every delete-list item, cited as `file:line`.

---

## 1. Verdict

**Shape.** The foundation is small, coherent and well guarded. The node → cluster → Flux → platform chain is clear. CI is strong: 13 rule checks, about 480 tests, kustomize builds of both envs, an Ansible localhost run, and a prune guard. Day-to-day logical restore (PVC from restic, with a hold and fail-closed behaviour) is the best-built part. The weak parts are about **losing things**: the disk, the k3s token, the age key, and the restic password. They are also about the **first hour of a new adopter**: the documented new-env order fails, and the schema misses the values that most often go wrong.

**Top three risks**

1. **A dead disk loses everything** (J01, critical). Backups, etcd snapshot copies and data share one disk. With a data disk, Ansible actively refuses a separate backup mount. Nothing is ever copied off the host.
2. **A rebuilt control plane cannot use the etcd copy, and nothing proves the real rebuild path** (J02, high). The k3s server token is random, unsealed and on the root disk. Meanwhile `make drill` passes and reads as "recovery works".
3. **Key and credential custody can destroy backups through documented steps** (J03 and J04, high). The only written restic rotation step orphans every repository. The only age-key recovery step regenerates secrets and throws away the live restic password the cluster still holds.

**How close it is to deployable by others.** Close for a careful, technical adopter on one node who reads the code. It is not there for the "little effort" adopter. The documented new-env order exits 1 after prep-node has already changed the host (J05). A wrong Flux URL needs a manual `kubectl apply` to fix (J06). Repo URLs with tokens are accepted (J07). The template lacks the secrets README the docs send you to (J34). OS patching is off with no runbook (J10). I estimate one focused round of S/M PRs (the PRs labelled A, B, C and D in section 7) to reach "deployable by others on one node". Multi-node also needs J12 and J22.

**Fit with "Where this is going".**

- *Good fit:* the layout, the per-env folder and schema, SOPS in Git, the LAN-only stance, and per-PVC enrollment by policy.
- *Awkward fit, needed before any of the three next areas lands:*
  - services and apps can't decrypt secrets or be chosen per env (J11);
  - every namespace gets the same restic credential and privileged movers (J08), which is unacceptable once outside apps or the code factory run untrusted code;
  - GPUs are a node label only, with no runtime and no per-card capacity (J15);
  - the schema requires a cursor-only `build_agent` and a Hermes file (J16), which ties the product to today's build tool.

None of this needs a redesign. All of it needs to land before the first consumer, not with it.

---

## 2. Ranked findings (merged)

The ids are the judge's (J01…J49). Auditor ids: A = Grok (`F1…F21`), B = GPT (`F01…F25`), C = Claude (`F01…F39`, `T01…T20`). "Render" means a kustomize render change; "Ansible" means an Ansible run change.

### Critical

#### J01 — Disk loss destroys data and every backup; no off-host copy exists, and data-disk hosts can't add a separate backup disk
- **Severity / size:** critical / L.
- **Who is hurt:** every adopter on one node, and multi-node adopters whose backup node dies. One disk failure loses all PVC data, the restic repositories and the etcd snapshot copies together.
- **Where:**
  - `platform/backups/CONTRACT.md:187-188`: "Backups live on the same host as the data unless `backup_path` is on another disk. They cover mistakes and rollback, not disk loss."
  - `platform/backups/rest-server.yaml:27,65`: a single rest-server on the `role.<suffix>/backup` node, using a hostPath.
  - `infra/ansible/roles/node-disk/tasks/main.yml:76` ("Refuse local_path_data or backup_path outside the data mount") and `:128` ("Refuse backup_path on another mount"). With `data_disk: true`, `backup_path` must be on the same mount as the data, so the contract's escape clause ("unless `backup_path` is on another disk") is impossible on data-disk hosts.
- **Questions:** 6, 13, 23, 24.
- **Found by:** A F1, B F01, C F03. All three cross-checks confirmed it. No auditor noticed that node-disk forbids the escape the contract offers; that part is judge-only.
- **Evidence (judge):** the lines above. I read the asserts at `node-disk/tasks/main.yml:76-135`: `that: backup_path == node_disk_mount or backup_path is match('^' ~ node_disk_mount ~ '/')`, then `findmnt -n -T {{ backup_path }}` must equal `node_disk_mount`.
- **Fix:**
  - *Approach:* add a second, off-host restic target and copy to it. Two ways:
    - Option A: a VolSync second ReplicationSource per PVC, pointing at an adopter-configured restic URL (S3-compatible, or rest-server on another LAN host).
    - Option B, smaller: a CronJob running `restic copy` from the local rest-server to an adopter-configured repo, for the PVC repos and the etcd repo.
  - *Config:* add an optional `offsite_restic_url` plus credentials in a new secret spec. When it is set, verify requires a fresh off-host snapshot.
  - *Ansible:* allow `backup_path` on a dedicated mount (`backup_disk` or similar) instead of refusing it. Keep the refusal for paths that are neither on the data mount nor on a declared backup mount.
  - *Docs:* update the contract Limits.
  - *Files:* `platform/backups/` (new CronJob or a second source rule in `enrollment.yaml`), `schema/env.py`, `tools/bootstrap-secrets`, `tools/verify/run`, `infra/ansible/roles/node-disk/tasks/main.yml`, `platform/backups/CONTRACT.md`.
  - *Tests:* a schema test that the off-host URL is optional and https-or-s3 only; a kustomize-render test that the copy job exists only when configured; an Ansible localhost test that a declared backup mount is accepted and an undeclared one is refused; a verify unit test for off-host freshness.
  - *Done check:* `make ci-verify` passes on env-00 with and without the key, and the render diff shows the new job only when the key is set.
  - *We lose:* simplicity, and the "one rest-server" picture.
- **Changes:** render yes, Ansible yes. Needs maintainer decision D1 (which target).

### High

#### J02 — A rebuilt control plane cannot use the etcd copy (unsealed k3s token), and the real rebuild path is never drilled
- **Severity / size:** high / M.
- **Who is hurt:** anyone rebuilding a server node. k3s encrypts bootstrap data and secrets with a key derived from the server token. The token is random per install and is never written to Git or to the backups. `make drill` passes and is easy to read as "disaster recovery proven".
- **Where:**
  - `infra/ansible/roles/k3s/tasks/token.yml:21-35` slurps `/var/lib/rancher/k3s/server/node-token` only to hand it to agents.
  - `infra/ansible/roles/k3s/templates/config.yaml.j2` has `secrets-encryption: true` and no `token:`.
  - `infra/ansible/group_vars/all.yml:6`: `k3s_data_dir: /var/lib/rancher/k3s` is on the root disk.
  - `platform/backups/CONTRACT.md:170-192`: the reset is same-node and needs the original token; "An older snapshot that exists only in the restic copy has no owner-run path yet."
  - The designed rebuild path is fresh `site.yml`, then the rebuild hold, then `make restore-all` (`CONTRACT.md:75-147`). It does not need etcd, but no drill or test runs it end to end.
- **Questions:** 4, 13, 15, 24.
- **Found by:** A F1 and B F01 (both critical, merged with disk loss), C F04 (high). The cross-checks agree. B's framing ("the drill can pass although recovery is not possible") is the most precise. I split the token from J01 because its fix is small and independent.
- **Evidence (judge):** the lines above; `token:` does not appear in `config.yaml.j2`.
- **Fix:**
  - *Approach:* generate a k3s server token and a separate agent token in `tools/bootstrap-secrets` as a SOPS secret spec. Template `token:` and `agent-token:` into the server config, and have agents join with the agent token (this also fixes J19).
  - *Docs:* write a "Rebuild a dead node" runbook in `platform/backups/CONTRACT.md` Operations: `site.yml` → hold → `restore-all`. Label the etcd copy "same-node rollback only" until an owner-run path exists.
  - *Files:* `tools/bootstrap-secrets`, `infra/ansible/roles/k3s/{tasks/token.yml,templates/config.yaml.j2,templates/agent-config.yaml.j2}`, `schema/env.py` (new required secret file for new envs only, with a migration note for env-01), `platform/backups/CONTRACT.md`.
  - *Tests:* render the config templates and assert that `token:` comes from the decrypted value and is never logged (`no_log`). Assert that agents get `agent-token` only.
  - *Done check:* the pytest k3s template tests pass, and `grep -n token: infra/ansible/roles/k3s/templates/config.yaml.j2` matches.
  - *We lose:* setting a token on an existing cluster must equal the current token, or k3s refuses to start. env-01 needs its current token sealed first; that is the migration step and needs the maintainer.
- **Changes:** Ansible yes, render no. Decision D2 (migrate env-01's live token, or apply to new envs only).

#### J03 — The documented restic rotation orphans every existing repository
- **Severity / size:** high / S (docs and a guard), M (a real rotation procedure).
- **Who is hurt:** any adopter who follows `envs/env-01/secrets/README.md:25`: "To rotate `backups-restic`, delete that file and run the command again." `tools/bootstrap-secrets:261-268` (`_restic_string_data`) mints a new `RESTIC_PASSWORD` and REST password. Every existing repository is still encrypted with the old password, so after Flux syncs, every backup and restore fails, and old history is unreadable unless someone kept the old value.
- **Questions:** 15, 13.
- **Found by:** A F2 (high), B F03 (high), C F02 (critical). The cross-checks converge on high. **High is right**: the damage is recoverable while the old ciphertext is still in Git history and the age key exists, so it is not certain data loss.
- **Evidence (judge):** both lines above, read directly. `bootstrap()` at `:342-364` regenerates any missing file without a warning.
- **Fix:**
  - *Now (S):* replace the sentence with "Do not rotate by deleting the file." Make `bootstrap-secrets` refuse to regenerate `backups-restic` when the env already has `flux-system` restic repos (or a git-tracked old file), unless `--rotate-restic` is passed.
  - *Later (M):* a real rotation runs `restic key add` with the new password and `restic key remove` for the old one on every repo (a Job per repo via the rest-server), then reseals. The REST htpasswd user can be rotated separately.
  - *Files:* `tools/bootstrap-secrets`, `envs/env-01/secrets/README.md`, `platform/backups/CONTRACT.md` (Operations), a new `envs/env-00/secrets/README.md` (J34).
  - *Tests:* a bootstrap-secrets test that deleting only the restic file and re-running refuses without the flag.
  - *Done check:* the pytest case.
  - *We lose:* nothing.
- **Changes:** none (tool and docs only).

#### J04 — The age key has one copy with no offline backup, and the documented recovery throws away the live secrets
- **Severity / size:** high / S (docs), M (custody).
- **Who is hurt:** an adopter who loses the controller. Each `.sops.yaml` rule has exactly one recipient. The private key lives on the controller and in the cluster Secret `flux-system/sops-age`. `infra/ansible/README.md:76` says that if both copies are lost, "the old secret values are gone"; accept the prep-node swap prompt and run `make bootstrap-secrets`.
  - **Judge-only:** when the controller is lost but the cluster still runs, the decrypted `backups-restic` and `cert-manager-ca` Secrets are still in the cluster and can be resealed to a new key. Following the doc instead regenerates them, which strands every restic repo (J03) and replaces the CA that every client trusts.
- **Questions:** 15, 12, 24.
- **Found by:** B F16 (medium), C F01 (critical). B's cross-check calls C's critical "too strong without simultaneous cluster and key loss", and I agree. A covers only the adjacent mismatch prompt (A F6, J33). Nobody saw the re-seal path.
- **Evidence (judge):** `.sops.yaml` has one env-01 rule with one recipient; `infra/ansible/README.md:76`; `tools/prep-node:880-908` (the swap leads to `delete_env_secrets`).
- **Fix:**
  - *Docs:* write a short "Age key custody" section in a new `envs/env-00/secrets/README.md` and in `docs/deploy.md`: make a second recipient (an offline key on paper or USB), add it to the env's `.sops.yaml` rule, and run `sops updatekeys`. Replace `infra/ansible/README.md:76` with an ordered recovery: if the cluster is alive, export and reseal the live Secrets to the new key; only if the cluster is gone too, regenerate, accepting the loss of restic history.
  - *Check:* `schema/env.py` warns (or the env schema requires, per decision D3) at least two recipients per env rule.
  - *Tests:* a schema test for the recipient count.
  - *Done check:* pytest.
  - *We lose:* nothing.
- **Changes:** none. Decision D3 (require two recipients or only document it).

#### J05 — prep-node changes the host before validating the env, and the documented order always fails validation
- **Severity / size:** high / S–M.
- **Who is hurt:** every new adopter on their first host. `docs/deploy.md:36-37` says to run prep-node (step 2) and only then add the `.sops.yaml` rule (step 3). `tools/prep-node` `run()` (`:950-992`) installs the login key, sets up sudo, the hostname, the Ansible user and packages, offers to turn SSH passwords off, edits the env and syncs the age key, and only then calls `run_env_schema`. So the documented path exits 1 after the host has changed.
- **Questions:** 9, 10.
- **Found by:** A F3, B F02. C missed it, and C's cross-check admits "they are closer". All cross-checks confirmed it.
- **Evidence (judge):** the harness case `prep_node_doc_order_no_rule_yet` fails with "expected exactly one creation rule for envs/lab/secrets/, found 0". The call order is as read above.
- **Fix:**
  - Run the schema first, in a "pre-host" mode that skips only checks that depend on prep-node's own outputs (the recipient and the age key). Fail before any SSH.
  - Either have prep-node add the missing creation rule itself (it already prints the public key and rewrites an existing recipient), or swap steps 2 and 3 in `docs/deploy.md`. Prefer the first, since it is one fewer manual step.
  - *Files:* `tools/prep-node`, `schema/env.py` (a pre-host mode flag), `docs/deploy.md`.
  - *Tests:* a prep-node test that runs against a stub SSH and an env with a schema error, and asserts that zero SSH calls happen.
  - *Done check:* pytest.
  - *We lose:* nothing.
- **Changes:** none.

#### J06 — Duplicated env facts are unchecked: the FluxInstance URL, ref and path and the generated ConfigMap can drift, and a wrong FluxInstance needs a manual kubectl apply
- **Severity / size:** high / S.
- **Who is hurt:** adopters who copy env-00. `envs/env-00/flux/flux-instance.yaml` has its own sync URL and ref (`example-owner`, `refs/heads/example`), independent of `cluster-vars.yaml`. The schema doesn't compare them. `infra/ansible/roles/flux-operator/tasks/instance.yml:21-28` applies the FluxInstance only `when: not flux_instance_present`, so once the cluster is up a wrong URL can't be fixed through Git. The fix is the manual `kubectl apply` in `infra/ansible/README.md:74`, a click-ops step. The generated `cluster-vars-configmap.yaml` can also go stale.
- **Questions:** 4, 9, 10.
- **Found by:** A F5 (high) and A F9 (medium), C F11 (medium), B E04. The cross-checks agree it is real. A's high is right, because recovery needs a manual cluster write.
- **Evidence (judge):** the harness cases `flux_instance_url_ref_left_as_example` and `stale_configmap_dns_suffix_changed` both PASS validation. The comparisons exist only in tests (`ci/tests/test_flux_entry.py:183-191,228-229`, per B; read).
- **Fix:**
  - In `schema/env.py`, compare FluxInstance `spec.sync.url`, `ref` and `path` against `flux_git_repo_url`, `flux_git_ref` and `envs/<env>/flux`.
  - Re-render the ConfigMap in memory and compare it with the committed file.
  - *Files:* `schema/env.py`, `ci/tests/test_env_schema.py`.
  - *Done check:* both harness cases fail with a clear message.
  - *We lose:* nothing.
- **Changes:** none.

#### J07 — Repo URL validation accepts embedded credentials, token queries and fragments
- **Severity / size:** high / S.
- **Who is hurt:** an adopter who pastes `https://user:token@…` or `…?token=…` into `flux_git_repo_url` or `apps_git_repo_url`. The value is committed in plaintext, published in the cluster-vars ConfigMap and copied into the FluxInstance.
- **Where:** `schema/env.py:512` (`_validate_repo_url`) calls `_validate_https_url` (`:542`), which checks only the scheme and the host.
- **Questions:** 10, 17.
- **Found by:** B F20 only. A and C missed it. The cross-checks confirmed it.
- **Evidence (judge):** a direct call returned `errors=[]` for userinfo, a token query, a fragment and a non-standard port.
- **Fix:** reject userinfo, query and fragment. Keep port and path. *Files:* `schema/env.py`, `ci/tests/test_env_schema.py`. *Done check:* parameterized pytest cases. *We lose:* nothing (private repos need a Flux `secretRef`, J18, not credentials in the URL).
- **Changes:** none.

### Medium

#### J08 — Backup trust boundary: one restic credential in every namespace, cluster-wide Secret RBAC for Kyverno, and privileged movers everywhere
- **Severity / size:** medium now, **high before the first outside app or the code factory** / L.
- **Who is hurt:** any workload that can read Secrets in its own namespace can read and prune every other namespace's backup history. The rest-server header says "No --append-only… No --private-repos".
- **Where:**
  - `platform/backups/enrollment.yaml:50` copies the global `RESTIC_PASSWORD` into each PVC namespace.
  - `:96-108` labels every PVC namespace `volsync.backube/privileged-movers: "true"`.
  - `platform/kyverno/rbac.yaml:12-15` grants cluster-wide Secret get, list, watch, create, update, patch and delete.
  - `platform/backups/rest-server.yaml:2-3,56` (runs as root).
- **Questions:** 17, 22, 23.
- **Found by:** A F16, B F04 and B F22 (high), C F16. All cross-checks confirmed it. This is a known deferral (per-namespace credentials, privileged-mover labelling).
- **Evidence (judge):** the lines above.
- **Fix:**
  - Per-namespace restic credentials, generated by a Kyverno generate rule from a per-namespace random value, or one repo password per namespace in SOPS.
  - rest-server `--private-repos` with one htpasswd user per namespace.
  - Narrow Kyverno RBAC to Secrets with a fixed name via `resourceNames` where Kyverno allows it.
  - Privileged movers only for namespaces that opt in with a label.
  - *Files:* `platform/backups/{enrollment.yaml,rest-server.yaml,CONTRACT.md}`, `platform/kyverno/rbac.yaml`, `tools/bootstrap-secrets`, `tools/restore`.
  - *Tests:* render tests for RBAC scope and labels.
  - *Done check:* kustomize render and pytest.
  - *We lose:* a migration of existing repo passwords (rotation per J03).
- **Changes:** render yes. Decision D4 (timing).

#### J09 — Enrollment fails open, and `pvc-backup` accepts a policy that no longer matches PVCs
- **Severity / size:** medium / S.
- **Who is hurt:** a PVC created while the Kyverno admission controller is down gets no ReplicationSource and no backup, silently. `generateExisting` covers only resources that existed when the policy was created.
- **Where:** `platform/backups/enrollment.yaml:12` (`failurePolicy: Ignore`), `:41,:72`. `ci/checks/lib/_pvc_backup.py` (`_is_enrollment_policy` doesn't read `match` or `exclude`).
- **Questions:** 6, 17, 18.
- **Found by:** A F11, B F12, C T01 (high; A's and B's cross-checks confirmed it with a probe: B E20). I downgrade T01 to medium: it is a CI hole, not a runtime bug.
- **Evidence (judge):** `failurePolicy: Ignore` at line 12, read directly. B's probe result is consistent with my reading of `_is_enrollment_policy`.
- **Fix:**
  - Keep `Ignore`, since `Fail` would block every PVC while Kyverno is down.
  - Add a `make verify` check that every PVC without `backup: none` has a ReplicationSource.
  - Make `_pvc_backup.py` require a PVC `match` kind.
  - *Files:* `tools/verify/run`, `ci/checks/lib/_pvc_backup.py`, tests.
  - *Done check:* pytest with a wrong-kind policy fixture that fails `pvc-backup`, and a verify unit test for an orphan PVC.
  - *We lose:* nothing.
- **Changes:** none.

#### J10 — OS security updates are off, and there is no patching procedure
- **Severity / size:** medium / S–M.
- **Who is hurt:** every adopter. Nodes never get security updates unless the adopter knows to patch by hand.
- **Where:** `infra/ansible/roles/base-os/templates/20auto-upgrades.j2:5` (`Unattended-Upgrade "0"`); `50unattended-upgrades.j2:3` (empty `Allowed-Origins`); `base-os/tasks/main.yml:70` ("Keep unattended-upgrades package policy inert"). There is no runbook. The baseline check-mode log shows the live host still carries the old "Deliberate departure from framework §4 unattended upgrades" comment, which the template has since dropped.
- **Questions:** 6, 14.
- **Found by:** C F10 (high), B E11. A missed it. I rank it medium rather than high: endpoints are LAN or tailnet only, and the choice is deliberate. It is still a real gap for a product.
- **Fix:**
  - Either enable security-only unattended upgrades with `Automatic-Reboot "false"` and a node "reboot required" check in verify, or write a patch runbook: drain, `apt upgrade`, reboot, one node at a time.
  - *Files:* the base-os templates and tasks, `infra/ansible/README.md`, `tools/verify/run`.
  - *Done check:* the base-os pytest asserts the chosen policy.
- **Changes:** Ansible yes (if enabling). Decision D5.

#### J11 — Services and apps have no secrets path and no per-env selection; `apps_git_repo_url` is accepted and ignored
- **Severity / size:** medium (blocks phases 1–3) / M.
- **Who is hurt:** the first real service or app. Only `platform-configs` decrypts. `services` and `apps` have no `decryption:` and always build `./services` and `./apps` from the main repo, so every env gets every service. `apps_git_repo_url` and `app_hosting_enabled` reach only the schema and tests.
- **Where:** `envs/env-00/flux/kustomizations/platform-configs.yaml:16` (the only `decryption`); `services/placeholder.yaml`; `apps/placeholder.yaml`.
- **Questions:** 21, 22, 23.
- **Found by:** A F15, B F05 (high), C F07 and C F08 (high). The cross-checks agree. It is medium now because there are no consumers. It must land before the first service.
- **Fix:**
  - Add `decryption` to the services and apps Kustomizations.
  - Give each env a list of enabled services: per-service Flux Kustomizations under `envs/<env>/flux/kustomizations/services/` (this is the known deferral "per-service Flux Kustomizations at the second service").
  - Wire `apps_git_repo_url` to a second GitRepository, or drop the key until it is used.
  - Write `docs/adding-a-service.md`.
  - *Files:* `envs/env-00/flux/kustomizations/{services,apps}.yaml`, `schema/env.py`, `services/`, `apps/`, docs.
  - *Done check:* a kustomize-render test of env-00 with one sample SOPS secret under services.
- **Changes:** render yes. Decision D6 (the shape of app hosting).

#### J12 — The restore binder pod has no node placement
- **Severity / size:** medium (multi-node only) / M.
- **Who is hurt:** multi-node adopters on a rebuild. k3s `local-path` uses `WaitForFirstConsumer`, so the binder pod's node fixes the PV's node. `tools/restore:561-580` (`_binder_body`) sets no `nodeSelector`, affinity or tolerations. A workload pinned to a role node (GPU, storage) can then never schedule.
- **Questions:** 4, 11, 13.
- **Found by:** C F05 (high). B confirmed (E12). I downgrade to medium: single-node is unaffected, and the fix is local.
- **Evidence (judge):** `_binder_body` read directly; the pod spec has only `restartPolicy`, `containers` and `volumes`.
- **Fix:** copy `nodeSelector`, `affinity` and `tolerations` from the mounting workload's pod template, or accept `--node`. *Files:* `tools/restore`, `ci/tests/test_restore.py`. *Done check:* a unit test asserting that the binder body carries the workload's selector.
- **Changes:** none.

#### J13 — On a fresh install, `make verify` and `make drill` fail for up to 12 hours
- **Severity / size:** medium / S.
- **Who is hurt:** a new adopter following `docs/deploy.md`, which ends with verify. `tools/verify/run:370-372` raises "etcd snapshot copy has no successful scheduled run" until the CronJob (`5 */12 * * *`, `platform/backups/etcd-snapshots.yaml:11-12`) has run once.
- **Questions:** 9, 16.
- **Found by:** C F06 (high), B E16. I downgrade to medium: it fails closed with a clear message, but the docs never mention it.
- **Fix:** document "first verify may need the etcd copy to have run". Better, have verify report "pending first run" (a warning, exit 0) while the CronJob is younger than one schedule interval. *Files:* `tools/verify/run`, `docs/deploy.md`. *Done check:* a verify unit test.
- **Changes:** none.

#### J14 — There is no adopter upgrade or release-intake procedure
- **Severity / size:** medium / M (docs).
- **Who is hurt:** every adopter after the first month. Renovate PRs land in this repo; an adopter's copy has no written way to take a new Jarvis release (merge upstream, resolve env changes, re-run the schema). The k3s upgrade path (bump `k3s_version` and `k3s_sha256`, then `site.yml`, one node at a time) is also unwritten.
- **Questions:** 12, 14.
- **Found by:** A F13, B F07, C F21. All cross-checks agree.
- **Fix:** write `docs/upgrade.md` covering pulling upstream (git remote, merge, schema, CI), k3s, Flux Operator and chart bumps. State that release notes are the merged PR titles (no changelog file). *Done check:* the doc links pass the docs check.
- **Changes:** none.

#### J15 — GPUs are a label plus a vendor assert, with no runtime and no per-card capacity
- **Severity / size:** medium (blocks voice and LLM work) / L.
- **Where:** `infra/ansible/tasks/assert-gpu.yml` checks only PCI vendor `0x10de`. There is no device plugin, RuntimeClass or `nvidia.com/gpu`. The schema rejects per-host GPU keys.
- **Questions:** 11, 22, 23.
- **Found by:** A F14, B F06 (high), C F13. All agree. It is a known deferral.
- **Evidence (judge):** the harness case `per_host_gpu_memory_key` fails with "unknown key".
- **Fix (its own PR, later):** the NVIDIA container toolkit in a base-os GPU task, the device plugin (or GPU Operator) under `platform/gpu/`, a RuntimeClass, and an optional per-host `gpu_memory_gb` used for labels. Mixed cards then schedule by label or extended resource.
- **Changes:** render yes, Ansible yes.

#### J16 — The schema requires config for products that don't exist yet, and ties the product to today's build tool
- **Severity / size:** medium / M.
- **Where:**
  - `hermes.yaml` is required (harness case `no_hermes_yaml` fails).
  - `build_agent` allows only `cursor` (harness case `build_agent_other` fails), which conflicts with "nothing in Jarvis may depend on the tools used to build it".
  - `llm_provider_host` and `code_delivery` are required.
  - `bootstrap-secrets` always generates Grafana and Hermes secrets that Flux never applies.
- **Questions:** 5, 7, 22.
- **Found by:** A F17 and A F19, B F10, C F27. The cross-checks agree on the direction and split on "delete" versus "make optional".
- **Fix:** make `hermes.yaml`, `build_agent`, `llm_provider_host`, `code_delivery` and `apps_git_repo_url` optional, validated only when present (this matches the maintainers' "keys with no consumer are optional" choice, which `hermes.yaml` currently breaks). Stop generating unused secret files for new envs; keep env-01's. *Files:* `schema/env.py`, `tools/bootstrap-secrets`, `ci/tests/test_env_schema.py`, env-00 values. *Done check:* the harness case `no_hermes_yaml` passes.
- **Changes:** none (the ConfigMap allow-list is unchanged unless keys are dropped). Decision D7.

#### J17 — A second env must share env-01's GitHub owner
- **Severity / size:** medium / S.
- **Where:** `schema/env.py:535-539` (`_cross_env_owner_errors`). The harness case `clone_keeps_env01_adds_lab_other_owner` fails with "repo owner handle must be the same… env-01=gordoncooper, lab=lab-owner". The docs don't say to delete env-01 first. The harness case `env01_dir_deleted_rule_left_plus_lab_rule` shows that a leftover env-01 `.sops.yaml` rule is tolerated.
- **Questions:** 5, 9.
- **Found by:** A F4 (high). B and C confirmed it in their cross-checks. I downgrade to medium: deleting `envs/env-01` (which an adopter should do anyway) clears it.
- **Fix:** document "delete `envs/env-01/` and its `.sops.yaml` rule" in `docs/deploy.md` step 0. Fail when a `.sops.yaml` rule names a missing env. Keep or drop the owner rule per D8. *Done check:* harness-style pytest.
- **Changes:** none.

#### J18 — Flux sync requires a public repo
- **Severity / size:** medium / M.
- **Where:** `docs/deploy.md` Prerequisites: "A Git repository Flux can fetch over https". The FluxInstance has no `pullSecret` or `secretRef` path.
- **Why it matters:** an adopter's env folder holds the inventory, internal hostnames and the CA certificate. Many will want a private fork.
- **Questions:** 5, 9.
- **Found by:** C F09 (high). B confirmed it as a real constraint. I rank it medium because public works, and secrets are encrypted.
- **Fix:** an optional SOPS secret for a read-only deploy token, wired as FluxInstance `sync.pullSecret` by the flux-operator role. *Files:* the flux-operator role, `tools/bootstrap-secrets`, `schema/env.py`, docs.
- **Changes:** Ansible yes. Decision D9.

#### J19 — Agents join with the full server token
- **Severity / size:** medium / S.
- **Where:** `infra/ansible/roles/k3s/tasks/token.yml:21-35` gives agents `/server/node-token`. k3s's server token also lets a host join as a server and decrypt bootstrap data; a separate `agent-token` cannot join servers.
- **Questions:** 17.
- **Found by:** C F18. B confirmed it (E13, k3s docs).
- **Fix:** ships with J02 (`agent-token:`).
- **Changes:** Ansible yes.

#### J20 — The internal CA has no name constraints
- **Severity / size:** medium / S (new envs), M (for env-01: CA rotation).
- **Where:** `tools/bootstrap-secrets:132-152` creates a CA without `nameConstraints`. Adopters install this CA on phones and laptops, so a leaked CA key can mint certificates for any domain.
- **Questions:** 17.
- **Found by:** C F17. B confirmed it (E13).
- **Fix:** add a critical `permitted` name constraint for `dns_suffix` (and any IP SANs used). *Tests:* a pytest case on the generated certificate. *We lose:* the CA can't serve other suffixes.
- **Changes:** none for new envs; existing envs need rotation. Decision D10.

#### J21 — The command guard's Secret-read deny is case-sensitive, and some categories are uncovered
- **Severity / size:** medium / S.
- **Where:** `.cursor/cli.json` deny entries and `ci/checks/lib/_cli_permissions.py` (`match_glob` uses `fnmatchcase`, and the docstring says it "does not prove the client"). By category only:
  - mixed-case spellings of the Secret resource noun are not denied for read verbs;
  - there is no category entry for repo entrypoints that write to the cluster, or for reading the generated admin kubeconfig (C F36).
  - `.claude/settings.json` has no CI check.
- **Questions:** 17.
- **Found by:** A F7 (high), C F36 (low). B said "no defect in cli.json", which is **wrong** (both other cross-checks refute it).
- **Evidence (judge):** the matcher probe with the repo's `cli.json` allowed most of the 64 case variants of the noun, for both read verbs. No strings are recorded here.
- **Fix:** `cli.json` is frozen, so this is decision D11. The non-frozen part: add case-insensitive variants to the corpus in `_cli_permissions` tests so that the check reports the gap, and add a parity check for `.claude/settings.json`.
- **Changes:** none.

#### J22 — prep-node overwrites the env-wide `ssh_user` on every node
- **Severity / size:** medium / S.
- **Where:** `tools/prep-node:730-738`: `apply_env_edits` calls `set_yaml_scalar(cluster, "ssh_user", ssh_user)` on `cluster-vars.yaml`, an env-wide key. Preparing a second node with a different login user rewrites it for all nodes.
- **Questions:** 10, 11.
- **Found by:** C F12. B confirmed it.
- **Evidence (judge):** the line read above.
- **Fix:** refuse a different `ssh_user` when one is already set, or allow a per-host `ansible_user` in `hosts.yml` (the schema rejects it today: harness case `per_host_ansible_host_key` fails). *Done check:* a prep-node unit test.
- **Changes:** none, or Ansible yes if a per-host user is added.

#### J23 — `dns_suffix` is not validated as a DNS name
- **Severity / size:** medium / S.
- **Where:** `schema/env.py` accepts `Home_Lab` (harness case `bad_dns_suffix_Home_Lab` passes). The value becomes a label prefix (`role.<suffix>/…`) and the certificate zone, so a bad value fails late, at `site.yml` or in cert-manager.
- **Questions:** 10.
- **Found by:** C F35 (low), B E20.
- **Fix:** an RFC 1123 lowercase-subdomain check. *Done check:* pytest.
- **Changes:** none.

#### J24 — VolSync is not in the controller health gate
- **Severity / size:** medium / S.
- **Where:** `envs/env-00/flux/kustomizations/platform-controllers.yaml:15-23` health-checks only cert-manager and Kyverno. VolSync (`platform/backups/controller`) is in the same stage, and `platform-configs` (which holds ReplicationSources via Kyverno) can start first.
- **Questions:** 4.
- **Found by:** B F21 (high). B's cross-check revisits it; the others didn't contest. I downgrade to medium: Flux retries until the CRDs exist, so this costs noise and time on first install, not correctness.
- **Fix:** add the VolSync HelmRelease to `healthChecks` in both envs. *Done check:* the existing healthChecks test with VolSync added.
- **Changes:** render yes (Flux objects).

#### J25 — Diagnosis is thin: verify skips node and pod health, and most CONTRACTs lack Operations and Limits
- **Severity / size:** medium / M.
- **Where:** `tools/verify/run` checks Flux, one Secret, labels, backup timestamps and testapp. It checks nothing about NotReady nodes or crash-looping pods. Of the 10 `CONTRACT.md` files, only `platform/backups` (both sections) and `platform/cert-manager` (one) have the optional sections. Runbooks sit in `infra/ansible/README.md:72-76` and `envs/env-01/secrets/README.md`.
- **Questions:** 16.
- **Found by:** A F18, B F15 and B F18, C F26.
- **Fix:** a verify check for node Ready and pods not in CrashLoopBackOff or Pending for more than 10 minutes, plus the CONTRACT text in section 3, Q16. *Done check:* verify unit tests, and `ci/checks/contract-md`.
- **Changes:** none.

#### J26 — CI's gitleaks scan sees one commit
- **Severity / size:** medium / S.
- **Where:** `.github/workflows/ci.yml` checkout has no `fetch-depth`, and the default is 1. A secret added and then removed inside a PR's commits is not scanned.
- **Questions:** 17, 18.
- **Found by:** C T06. A's cross-check: partly. B confirmed it (E18).
- **Evidence (judge):** the workflow read directly.
- **Fix:** `fetch-depth: 0` on the job that runs the secret scan. *Done check:* a CI run shows "N commits scanned" with N greater than 1.
- **Changes:** none.

#### J27 — Safety branches with no failing test
- **Severity / size:** medium / M.
- **Where:**
  - verify's certificate-chain and body branches (C T02);
  - the bootstrap plaintext test covers only 2 specs (C T03);
  - install-deps checksum call sites (C T04);
  - manifest checks read raw files rather than rendered output (C T05);
  - restore, seal, drill and preflight guards (C T09);
  - the k3s unit template is never rendered (C T16);
  - the `sops-age` mismatch refusal (C T17);
  - base-os, node-disk and GPU skip paths (C T18).
- **Questions:** 18.
- **Found by:** C. B confirmed it broadly (E21). A confirmed T01, T05 and T10 and the rest "partly". I accept them as one medium finding. C ranked T02, T03, T16 and T17 high; I downgrade them, since each is a missing test, not a runtime bug.
- **Fix:** one test per branch, each seeded so that removing the guard fails the test (the list in B's cross-check, "Test/check fix directions", is usable as-is). *Done check:* pytest.
- **Changes:** none.

#### J28 — The Flux Operator bootstrap trusts any existing Helm binary and skips its readiness waits on a re-run
- **Severity / size:** medium / S.
- **Where:** the flux-operator role's `helm_cli.yml` checks only that `helm` exists before the pinned download. The CRD and rollout waits in `operator.yml` sit inside the "operator Deployment absent" branch, so a re-run after a partial first run skips them.
- **Questions:** 4.
- **Found by:** B F23, C F24 and C F38, B E15.
- **Fix:** compare `helm version --short` with the pin and re-download on a mismatch; run the waits unconditionally (they are idempotent). *Done check:* the flux-operator localhost tests.
- **Changes:** Ansible yes.

#### J29 — Operator kubectl calls have no timeout
- **Severity / size:** medium / S.
- **Where:** `tools/lib/_env.py:44` calls `subprocess.run([... "kubectl" ...])` with no `timeout` and no `--request-timeout`. verify, drill and restore hang when the API is unreachable.
- **Questions:** 16.
- **Found by:** B F24.
- **Evidence (judge):** line 44 read directly.
- **Fix:** add `--request-timeout=30s` plus a subprocess timeout that maps to a clean failure. *Done check:* a unit test with a sleeping fake kubectl.
- **Changes:** none.

### Low

| id | Finding | Where | Found by | Fix (size) |
| --- | --- | --- | --- | --- |
| J30 | The drill's etcd check races the snapshot schedule, and with no `pipefail` it reports "checksum mismatch" when the file is missing. Fails closed. | `tools/drill:289-291` (`restored=$(sha256sum … \| cut …)`), parser `:640-650`; schedules `0 */12` vs `5 */12` | judge only (baseline run 1, 12:03), C F31 | Check `[ -f "/restore/snapshots/$name" ]` and print "snapshot not yet in restic copy"; pick the newest name from `restic ls latest`, not the local dir (S) |
| J31 | The base-os handlers use `ansible.builtin.service`, which detects the init system from PID 1, so the chrony test fails on non-systemd controllers. **Ruled a portability bug.** | `infra/ansible/roles/base-os/handlers/main.yml:3,11,19`; `ci/tests/test_base_os.py:228` | B F08, C T15 (C's 483/483 was on systemd) | Switch to `ansible.builtin.systemd` (the k3s role already does). Proven in scratch: 1 passed (S). No production change |
| J32 | `make deps` runs apt install without `apt-get update`, and silently replaces user binaries | `tools/install-deps` | A F21, B F19, C F33 | Run `apt-get update` once before install, and warn before replacing a binary on PATH (S) |
| J33 | An age-key mismatch offers to delete the env's secrets | `tools/prep-node:880-908` | A F6 (high) | Downgraded: the prompt defaults to No (`confirm` `[y/N]`, `:63-68`). Replace the delete with a refusal that points to J04's runbook (S) |
| J34 | env-00 lacks `secrets/README.md`, and `docs/deploy.md:77` and `docs/README.md` point at env-01 files | `docs/deploy.md:77`, `docs/README.md` | A F8 (high), B F09, C F29 | Add `envs/env-00/secrets/README.md` (generic, with J03 and J04 text); point docs at env-00 (S). Downgraded: a missing doc, not broken behaviour |
| J35 | The whole age key file is copied into the cluster, including other envs' keys if the file holds several | `infra/ansible/roles/flux-operator/tasks/sops_age.yml:69` | C F15 | Read only the identity whose public key matches the env's `.sops.yaml` recipient (S, Ansible) |
| J36 | verify fetches the full backup Secret JSON | `tools/verify/run` `_check_secret` | B F25 | Use `-o jsonpath='{.data}'` key names only (S) |
| J37 | The pinned Kyverno is tested upstream only on k8s 1.33–1.35; k3s is 1.36.5 | `platform/kyverno/CONTRACT.md:24`; `k3s/defaults/main.yml:7` | B F11 (medium) | Downgraded: documented; the live sweep is healthy. Track in the Renovate PR (S) |
| J38 | Chrony is NTS-only; networks blocking 4460/tcp lose time sync | `base-os/templates/chrony.conf.j2:8-9` | C F20 (medium) | Optional `chrony_nts: true` default with a documented off switch (S, Ansible). Downgraded: a fixable default |
| J39 | About 30 plan or decision codes remain in core code comments outside `envs/env-01` (e.g. `apps/placeholder.yaml:1`, `infra/ansible/site.yml:4`, `ci/checks/*:2`, `platform/*/helmrelease.yaml`) | `git grep` count, section 6 | A F20, C F28 | Remove the codes, and extend `_plan_codes.py` to non-Markdown files (S) |
| J40 | Tests of source shape, a dead assertion, helper self-tests | `ci/tests/test_restore.py:652`; `test_flux_operator.py`; `test_env_shapes.py:40-56` | B F17, C T07, T10, T19 | Delete the dead assertion; replace shape snapshots only where a behaviour test keeps the invariant (M) |
| J41 | `ci/run-checks` stops at the first failure; core-names skips root files; listed contract roots that don't exist pass silently | `ci/run-checks` (`set -e`); `ci/checks/lib/_contract_md.py:9-17` | C T11, T12, T13 | Collect all failures before exiting; scope note; see the delete list (S) |
| J42 | Regenerating only the CA secret keeps the old `ca.crt` | `tools/bootstrap-secrets` `bootstrap()` | C T14 | Refuse when `ca.crt` doesn't match the new certificate (S) |
| J43 | Kyverno logs a "persist generated resource provenance" PolicyError, then succeeds on retry | baseline `06-kyverno-bg-errors.log` | judge only | The RS was generated and the drill passed. Needs a live log check (section 11); no fix now |
| J44 | The address-change `--cluster-reset` step sits next to the snapshot restore with no cross-link | `infra/ansible/README.md:72` | A F10 (medium) | Downgraded: per k3s docs a bare reset keeps the data. Add one line distinguishing it from a snapshot restore (S) |
| J45 | k3s restarts by handler right after its first start | k3s role | C F39 | Flush handlers before the first start, or skip the notify on first install (S, Ansible) |
| J46 | Non-HTTP apps (TCP/UDP) can't be exposed | traefik HelmChartConfig; `ci/checks` LoadBalancer allow-list | C F19 (medium) | Downgraded until an outside app needs it; record it under Limits in `platform/traefik/CONTRACT.md` (S) |
| J47 | verify checks backup freshness, not restorable bytes | `tools/verify/run` | A F12 (medium) | Downgraded: the drill checks bytes; this is the known deferral "automatic restore validation" (section 4) |
| J48 | Old base-os comment still on the live host | baseline check-mode log | judge only | Goes away at the next `site.yml`; no fix |
| J49 | `bootstrap-secrets` ignores `age_key_file`, while `site.yml` reads it, so the two tools can use different keys | `envs/env-01/secrets/README.md:15` ("`tools/bootstrap-secrets` does not read `age_key_file`") | C F34 | Read `age_key_file` from the env when `SOPS_AGE_KEY_FILE` is unset (S); goes in G1 |

---

## 3. Answers by question

1. **Reliable, flexible, practical, lean?**
   - The platform slice is lean: about six components, digest-pinned.
   - Not lean: the schema's required future config (J16) and the test and checker weight (J40, `_cli_permissions.py` at 899 lines).
   - Not reliable on loss: J01–J04.
2. **Glue a native approach would replace?**
   - The duplicated Flux URL and ConfigMap should be generated or checked (J06).
   - The node-side Helm CLI could become a pinned download only (J28).
   - prep-node (about 1,000 lines) is large but mostly justified. Fix its bugs (J05, J22) rather than rewrite it.
   - Renovate regex managers and the verify cron evaluator stay; no proven replacement exists (section 9).
3. **Too complex for what it buys?**
   - `_cli_permissions.py` and its tests model one client (decision D11).
   - `test_flux_operator.py` meta-tests (J40).
   - Trim these, and accept the risk that guard drift shows up later.
4. **Brittle?**
   - J06 (duplicated facts), J24 (health gate), J28 (Helm and waits), J30 (drill race), J12 (multi-node binder), J37 (Kyverno skew).
   - Known: `ci/tests/test_env_schema.py` hard-codes env-00's service IPs.
5. **Too strict?**
   - The cross-env owner rule (J17).
   - The cursor-only `build_agent`, required `hermes.yaml` (J16).
   - No per-host user or GPU keys (J22, J15).
   - Public-repo only (J18).
   - Exactly one control plane and amd64 only are documented limits (decision D12), not bugs.
6. **Real gaps?** Off-host backup (J01), the token and rebuild runbook (J02), key custody (J04), OS patching (J10), the services secrets path (J11), the GPU runtime (J15), the upgrade procedure (J14).
7. **Cruft?** The placeholders, absent contract roots, dead arm64 branches, duplicate restore helpers, the dead assertion and plan codes (section 6, J39, J40).
8. **Clean product?**
   - Core identity is clean (core-names passes).
   - Plan codes remain in about 30 code comments (J39).
   - `docs/README.md` leads with env-01, AGENTS and rulebooks links (J34).
9. **Clone to verify and drill from docs alone?** Not yet. The documented order fails (J05), the secrets README is missing (J34), the FluxInstance is easy to get wrong (J06), and verify is red for up to 12 hours (J13). After the PRs labelled C and D in section 7, yes for one node.
10. **Easy env prep with early clear errors?** The schema is good, but it runs after host mutation (J05), and misses the URL credentials (J07), the FluxInstance drift (J06) and DNS-name validity (J23).
11. **Single and multi-node, mixed GPUs?**
    - All inventory shapes render (the harness cases `single_node_all_roles` and `two_control_planes` behave as designed).
    - Multi-node restore has J12, and the `ssh_user` overwrite is J22.
    - GPUs aren't usable (J15).
12. **Unwritten knowledge?** The rebuild runbook (J02), restic rotation (J03), age custody and the re-seal path (J04), upgrades (J14), OS patching (J10), the first-run etcd copy (J13), deleting env-01 when cloning (J17), the cluster-reset distinction (J44).
13. **Real loss and disk death?** PVC restore from a surviving backup host works and is drilled. Disk death on one node loses everything (J01). A rebuilt server can't use the etcd copy (J02).
14. **Upgrades?** Renovate bumps pins in this repo only. There is no adopter path to take releases or roll k3s (J14), and OS patching is off (J10).
15. **Rotation and offline key?**
    - The CA has a documented rotation, but J42 applies.
    - The restic rotation is unsafe (J03).
    - Age key rotation and an offline backup are missing, and the recovery doc is lossy (J04).
16. **Can they tell what broke?** Partly: J25, J29. CONTRACT Operations and Limits needs:
    - `platform/backups`: has both; add the rebuild runbook (J02), restic rotation (J03), and the first-run etcd copy (J13).
    - `platform/cert-manager`: has Operations; add Limits ("one internal CA, no ACME, no name constraints until J20").
    - `platform/flux-operator`: Operations for the FluxInstance re-apply (from `infra/ansible/README.md:74`) and the upgrade path; Limits: "FluxInstance applied once by Ansible".
    - `platform/kyverno`: Operations for what to do when admission is down (J09); Limits: the version-skew note.
    - `platform/traefik`: Limits "HTTP(S) only" (J46).
    - `platform/app-dns`: Limits "router forwarding of `dns_suffix` is an adopter prerequisite".
    - `platform/configs`: Limits "decrypts only platform configs" (until J11).
    - `platform/testapp`: none needed.
    - `schema`: Operations "how to add a key, and the ConfigMap allow-list".
    - `docs/code-delivery`: Limits for the planned rows (it already marks them).
    - Buried steps: `infra/ansible/README.md:72-76` (cluster-reset, the Flux re-apply, age recovery) and `envs/env-01/secrets/README.md` (rotation) belong in the matching contracts.
17. **Leaks or over-trust?** J07 (URL credentials), J08 (backup trust boundary), J19 (agent token), J20 (CA scope), J21 (guard case gap), J26 (gitleaks depth), J35 (whole key file), J36 (Secret payloads). No default credentials or open ports were found. The LoadBalancer allow-list is Traefik and App DNS only.
18. **Test gaps worth filling?** J27, J09 (T01), J26, J41. The known accepted weak spots stay accepted.
19. **Code for little benefit?** J40, plus the `_cli_permissions` size (D11). Keep the kustomize-build and Ansible localhost tests.
20. **Behaviour tests, fresh env-00-only copy?** Mostly behaviour (Ansible localhost runs, renders, and stubbed kubectl). env-00-only: all checks pass, and pytest gives 482/483, with the only failure the non-portable chrony handler (J31). GitHub CI is green.
21. **A clean place for new apps and services?** Location, certs and DNS: yes (Ingress plus the internal CA plus k8s-gateway). Backups: yes, automatic. Secrets, per-env selection and health checks: no (J11, J25).
22. **Blocks or constraints?** Shared backup credentials and privileged movers (J08), no GPU runtime (J15), the cursor-only `build_agent` (J16), HTTP-only exposure (J46). The code-factory sandbox is not in scope here and is not blocked.
23. **What every consumer needs?** A decrypting, per-env deploy path (J11); per-namespace backup credentials (J08); health and alerting (J25, deferred observability); off-host backup (J01).
24. **Deferred, must not be lost:** see section 4.

---

## 4. Deferred, must not be lost

| Item | Recorded in the repo today | Pick up by |
| --- | --- | --- |
| Off-host backup copy | `platform/backups/CONTRACT.md:187-188` (Limits) | Before any adopter treats Jarvis as primary storage (J01) |
| Owner-run path to etcd snapshots that exist only in restic | `platform/backups/CONTRACT.md` Limits | With J02 |
| Rebuild-node runbook and drill | **Not recorded** | With J02 |
| Per-namespace backup credentials | **Not recorded** (the shared credential is described, not as a deferral) | Before the first outside app or the code factory (J08) |
| VolSync privileged-mover labelling per namespace | `platform/backups/CONTRACT.md:60` describes it as current behaviour only | With J08 |
| Automatic restore validation | **Not recorded** | After J01 (J47) |
| Backup-failure alerts | **Not recorded**; placeholder only (`services/placeholder.yaml:1`, "observability") | With alerting |
| Offline age-key backup | Only "Keep the private key offline" (`docs/deploy.md:9`) | Before any further real env (J04) |
| Restic and age-key rotation procedure | **Not recorded**; the existing text is unsafe (J03) | Now |
| `make verify` trusting the internal CA explicitly | **Not recorded** | With J25 |
| Backup scale controls at the first large volume | Partly in `platform/backups/CONTRACT.md` | First volume over about 50 GB |
| Per-service Flux Kustomizations at the second service | **Not recorded** | With J11, before the first service |
| Per-arch k3s checksums at the first arm64 env | **Not recorded** (arm64 Helm branches exist, section 6) | First arm64 env, D12 |
| "Planned" rows in CONTRACTs (code-delivery admin-gate, registry, Forgejo, the issue queue) | `docs/code-delivery/CONTRACT.md:5-26`, plus planned notes in traefik, testapp and schema | Code-factory phase |
| Observability, Hermes, code-factory and sandbox homes | Placeholders (`services/placeholder.yaml`, `apps/placeholder.yaml`) and `EXPLICIT_COMPONENT_ROOTS` entries for paths that don't exist | Move to a `docs/roadmap.md` (or issues) when the placeholders go (section 6) |
| GPU runtime | **Not recorded** as deferred | Before voice or LLM work (J15) |
| Known test weak spots (the healthChecks timeout, the chart resources lint, webhook rename, tag parser, `--skip-tags`, the env-00 flux-substitute gap, hard-coded service IPs) | Maintainer notes only, **not in the repo** | Record in `ci/README` or issues; pick up when touching each test |

---

## 5. What's good and should stay

- The `infra/platform/services/apps/envs` layout, the per-env folder plus schema, and the generated ConfigMap allow-list.
- SOPS in Git with `ca.crt` committed; the LAN-only, internal-CA stance.
- Kyverno per-PVC enrollment with a `backup: none` opt-out, and restore that fails closed with a rebuild hold. This is the best-designed part.
- `make drill` proving PVC bytes and the etcd copy end to end (fix its wording, J02 and J30, not its substance).
- The CI guardrails: secret scan, env schema, command guard check, core-names, kustomize build, manifest lint, prune guard, contract-md. All run in about 3 minutes and pass on an env-00-only tree.
- Digest-pinned images and checksum-pinned binaries, with Renovate and no stability hold (owner decides).
- Ansible that only checks disks and network, never formats them; per-env kubeconfig files.
- The AppArmor-aware chrony validation, `no_log` on secret-bearing tasks, and the 0600 and owner check on the age key.

---

## 6. Delete list (reproduced)

| Item | Evidence | Safe? |
| --- | --- | --- |
| `services/placeholder.yaml`, `apps/placeholder.yaml` → `resources: []` | Both are the only resources in their kustomizations. An empty `resources: []` kustomization builds with exit 0 (scratch). Comments: "D9: placeholder until observability and Hermes land in later phases", "…wired in P4a" | Yes, with a roadmap entry (section 4). Flux prunes the two ConfigMaps in `default`; check that the prune-guard test still passes |
| `EXPLICIT_COMPONENT_ROOTS` entries for missing paths: `docs/build-agent`, `services/hermes/deploy/code-factory`, `…/sandbox`, `…/ops`, `services/observability` | `ci/checks/lib/_contract_md.py:9-17`; each path tested with `[ -e ]`: all MISSING | Yes, re-add each with its component |
| arm64 branches in `infra/ansible/roles/flux-operator/tasks/helm_cli.yml:5-16` and `defaults/main.yml:14-16` | Preflight and schema allow amd64 only, so the branches are unreachable | Yes, unless D12 chooses arm64 soon (then keep and add k3s per-arch checksums) |
| Duplicate `_reason` and `_not_found` in `tools/restore:98-123` | Identical to `tools/lib/_env.py:57-66` (`reason`, `not_found`) | Yes, import the shared ones |
| Dead assertion `ci/tests/test_restore.py:652` | The second `capsys.readouterr()` after line 651 is always empty, so `"HOLD kept" not in …` can't fail | Yes, or capture once and assert both |
| Plan codes in about 30 non-Markdown core lines (J39) | `git grep` for D/R/P/§ codes in comments outside `*.md`, the plan-code check itself and `envs/env-01`: 32 lines | Yes; comments only |
| Grafana and Hermes secret generation for **new** envs | `tools/bootstrap-secrets` spec list; no Flux path applies them | Decision D7 (the maintainers chose to keep them); keep env-01's files either way |

Refuted as deletes:
- the `data_disk` host variable and the `storage` role are live (B's cross-check is right);
- the Renovate regex managers and the verify cron helpers are live;
- the `test_env_shapes.py:40-56` helper self-tests are cheap mutation proofs of `_assert_shape`, so keep them.

---

## 7. Proposed fix PRs, in impact order

| # | PR | Findings | Size | Depends on | Render / Ansible | Decision first? |
| --- | --- | --- | --- | --- | --- | --- |
| A | Secret custody docs and guards: safe restic rotation, age recovery and re-seal, offline recipient, env-00 secrets README | J03, J04, J34, J33, J42, J49 | S–M | none | no / no | D3 (two recipients required?) |
| B | Seal the k3s token, add the agent token, write the rebuild runbook | J02, J19, J45 | M | A (the secrets README) | no / **yes** | D2 (env-01 migration) |
| C | New-env path: validate before host, check the FluxInstance and ConfigMap, URL and DNS rules, `ssh_user`, the env-01 deletion step | J05, J06, J07, J23, J22, J17 | M | none | no / no | D8 (owner rule) |
| D | Fresh install green: verify's first-run etcd copy, the drill race message | J13, J30 | S | none | no / no | no |
| E | Off-host backup copy and a separate backup mount | J01 | L | B (runbook text), D | **yes** / **yes** | **D1** |
| F | Base-os: patch policy, chrony handler portability, NTS switch, apt update in deps | J10, J31, J38, J32 | S–M | none | no / **yes** | D5 |
| G | Flux and controllers: VolSync health gate, Helm pin and waits | J24, J28 | S | none | **yes** (Flux objects) / **yes** | no |
| H | Backup trust boundary: per-namespace credentials, scoped RBAC, opt-in privileged movers | J08, J09 | L | E (same files), A (rotation) | **yes** / no | D4 |
| I | Services and apps landing: decryption, per-env selection, apps source, docs | J11, J16 | M | C (schema) | **yes** / no | D6, D7 |
| J | Operator tools: kubectl timeouts, verify node and pod health, Secret fetch, binder placement | J29, J25 (verify part), J36, J12 | M | D (verify) | no / no | no |
| K | CI and tests: gitleaks depth, seeded safety tests, `pvc-backup` match, run-checks collects all, dead assertion and shape trims | J26, J27, J09 (check part), J40, J41 | M | none | no / no | no |
| L | Docs and CONTRACT Operations and Limits, upgrade guide, plan codes, cruft deletes | J14, J25 (docs), J39, J44, J46, J37, section 6 | M | after A–K touch their docs | no / no | no |
| M | GPU runtime and per-host capacity | J15 | L | C (schema), F (base-os) | **yes** / **yes** | D12-adjacent; design |
| — | Command guard case gap | J21 | S | none | no / no | **D11** (cli.json frozen) |
| — | Private-repo Flux sync | J18 | M | B | no / yes | D9 |
| — | CA name constraints | J20 | S | A | no / no | D10 |

## 7b. Fix PRs by area

Each group is one coding agent working in one place. The files listed are the ones the group owns; no other group edits them, except where the landing order says so.

| Group | Area | Findings | Files and directories | Size | Render / Ansible |
| --- | --- | --- | --- | --- | --- |
| G1 | **Secrets and keys** | J03, J04, J20, J33, J34, J42, J49 | `tools/bootstrap-secrets`, `envs/env-00/secrets/README.md` (new), `envs/env-01/secrets/README.md`, the age section of `infra/ansible/README.md`, the `sync_age_key` and `delete_env_secrets` functions in `tools/prep-node`, the recipient check in `schema/env.py`, `ci/tests/test_bootstrap_secrets.py` | M | no / no |
| G2 | **k3s and cluster identity** | J02, J19, J45 | `infra/ansible/roles/k3s/`, the token spec in `tools/bootstrap-secrets`, the rebuild runbook in `platform/backups/CONTRACT.md` (Operations), `ci/tests/test_k3s.py` | M | no / **yes** |
| G3 | **New-env path (schema, prep-node, deploy doc)** | J05, J06, J07, J17, J22, J23, J16 | `schema/env.py`, `tools/prep-node` (`run`, `apply_env_edits`), `docs/deploy.md`, `envs/env-00/values/`, `ci/tests/test_env_schema.py`, `ci/tests/test_prep_node.py` | M; split into **G3a** (J05, J06, J07, J22, J23, J17: validation and safety) and **G3b** (J16: optional future keys, needs D7) | no / no |
| G4 | **Ansible node prep** | J10, J31, J38, J32 | `infra/ansible/roles/base-os/`, `tools/install-deps`, `Makefile` (deps), `ci/tests/test_base_os.py` | S–M; split J31 and J32 (routine) from J10 (policy, D5) if D5 is slow | no / **yes** |
| G5 | **Backups and restore, data plane** | J01, J08, J09 (policy part), J12, J30 | `platform/backups/`, `platform/kyverno/rbac.yaml`, `tools/restore`, `tools/drill`, the `backup_path` asserts in `infra/ansible/roles/node-disk/tasks/main.yml` | L; **two PRs**: **G5a** off-host copy plus the backup mount (J01; risky, D1) and **G5b** trust boundary (J08, J09 policy; risky, D4). Put the routine J12 and J30 in **G5c** (S), which can land first | **yes** / **yes** (G5a) |
| G6 | **Flux and controllers** | J24, J28, J11, J18, J35* | `envs/*/flux/kustomizations/`, `infra/ansible/roles/flux-operator/`, `services/`, `apps/`, `platform/configs/` | M; two PRs: **G6a** J24, J28, J35 (routine) and **G6b** J11 and J18 (needs D6 and D9) | **yes** / **yes** |
| G7 | **Operator tools runtime (verify, lib)** | J13, J25 (verify part), J29, J36, J47 (later) | `tools/verify/`, `tools/lib/_env.py`, `ci/tests/test_verify.py` | M | no / no |
| G8 | **CI and tests** | J21 (the non-frozen part), J26, J27, J09 (`_pvc_backup.py`), J40, J41 | `.github/workflows/ci.yml`, `ci/checks/`, `ci/run-checks`, `ci/tests/` (the test files of other groups excluded) | M | no / no |
| G9 | **Docs, CONTRACTs and cruft** | J14, J25 (contract text), J39, J44, J46, J37, section 6 deletes | `docs/` (except `deploy.md`), every `CONTRACT.md` except backups Operations, the `infra/ansible/README.md` sections other than age, comment-only edits elsewhere, `ci/checks/lib/_contract_md.py` roots, the placeholders | M | **yes** (placeholders removed) / no |
| G10 | **GPU** | J15 | `platform/gpu/` (new), the GPU task in base-os, the GPU keys in `schema/env.py` | L | **yes** / **yes** |

\* J35 lives in `infra/ansible/roles/flux-operator/tasks/sops_age.yml`, so it goes in G6a, not G1, to keep that role in one group.

**Landing order**

1. **Wave 1, in parallel (no shared files):**
   - G1, the secrets and key text and guards;
   - G3a, new-env validation;
   - G4, routine node prep (J31 and J32 first);
   - G5c, binder and drill;
   - G6a, Flux gates;
   - G7, verify and lib;
   - G8, CI, because seeded tests for code that other groups change go into those groups' own test files.
2. **Wave 2:**
   - G2 after G1 (both add a spec to `tools/bootstrap-secrets`; G1 also writes the README G2 links to);
   - G3b after G3a (same `schema/env.py`) and after D7;
   - G6b after G3a (the schema keys for the apps source and the pull secret) and after D6 and D9.
3. **Wave 3:**
   - G5a after G2 (the runbook text in the backups CONTRACT) and D1;
   - G5b after G5a (same `enrollment.yaml` and `rest-server.yaml`) and after G1 (rotation, needed to migrate passwords) and D4.
4. **Wave 4:** G9 last, because it edits the CONTRACT and README text every other group may touch. It re-bases cheaply because it is text only.
5. **G10** whenever GPU work starts, after G3a (schema) and G4 (base-os).

Shared-file hot spots: `tools/bootstrap-secrets` (G1, then G2, then G5b), `schema/env.py` (G3a, then G3b, then G6b and G10), `platform/backups/CONTRACT.md` (G2, then G5a, then G5b, then G9), `tools/prep-node` (G1 owns the age functions, and G3a owns `run` and `apply_env_edits`; different functions, so they can run in parallel if the agents are told so, otherwise G1 first).

**Where the area grouping and the impact list disagree, and how I'd resolve it**

- **J01 (critical) sits in G5a, which lands in wave 3.** I'd keep it there, but not let it wait: take decision D1 now, and start G5a as soon as G2's runbook text has merged. Until then, ship a one-line Limits change in G1's README ("keep an off-host copy of `backup_path` yourself") so the risk is stated where adopters read it.
- **J02 (high) waits for G1.** G1 is S–M and has no decision blocking it except D3, so the wait is short. If D2 (env-01 token migration) is slow, ship G2 for new envs only and migrate env-01 later.
- **J13 (fresh verify red) sits in G7, a routine group, while the adopter path (G3a) is high-impact.** Both are wave 1, so there is no conflict. Land them together before announcing "deployable by others".
- **J09's check (G8) and policy (G5b) are split by area.** The check part is independent and lands in wave 1; the policy part waits for G5b.
- **The `_cli_permissions` and command-guard work (J21) is low-priority by area but medium by impact.** It is blocked by D11, not by order.

---

## 8. Decisions for the maintainers

| # | Decision | Options | Recommendation | Cost |
| --- | --- | --- | --- | --- |
| D1 | Off-host backup target | (a) `restic copy` CronJob to an adopter-set restic URL (S3-compatible or a LAN rest-server); (b) a second VolSync source per PVC; (c) document only | **(a)**, optional key, required by verify when set | (a) one CronJob and one secret; (b) doubles mover load; (c) leaves J01 critical |
| D2 | Seal the k3s token for env-01 | (a) seal env-01's current token (read it once from the node by the owner, encrypt); (b) new envs only | **(a)**, done by the owner, not an agent (it needs node access) | (a) one owner step; (b) env-01 stays unrecoverable on rebuild |
| D3 | Require two age recipients per env | (a) schema error; (b) warning plus docs | **(b) now, (a) before the first adopter release** | (a) blocks single-key envs |
| D4 | When to split backup credentials (J08) | (a) now; (b) before the first outside app or the code factory (known deferral) | **(b)**, but put it in the roadmap with that trigger | Reversing later needs rotation (J03) for every repo |
| D5 | OS patching (reverses an earlier choice) | (a) security-only unattended upgrades, no auto-reboot, plus a verify warning; (b) a manual runbook | **(a)** | (a) rare package surprises on k3s nodes; (b) relies on the adopter |
| D6 | App-hosting shape | (a) per-service Flux Kustomizations in this repo; (b) a separate apps GitRepository (`apps_git_repo_url`); (c) both | **(a) now, (b) when outside apps land** | (b) needs a second source and deploy key |
| D7 | Future config and secrets (reverses "grafana and hermes secrets stay") | (a) optional keys, no generation for new envs; (b) keep as is | **(a)**; `build_agent` must not be cursor-only in product | (a) a small schema migration; (b) the product depends on the build tool |
| D8 | Cross-env owner rule | (a) drop it; (b) keep it and document deleting env-01 | **(b)** plus failing on stale `.sops.yaml` rules | (a) loses a check against mixed-owner trees |
| D9 | Private repos | (a) optional pull secret; (b) public only (document why) | **(a)** | One more SOPS spec |
| D10 | CA name constraints | (a) new envs only; (b) also rotate env-01's CA | **(a)**, then (b) at the next planned CA rotation | (b) every client re-trusts the CA |
| D11 | Command guard (`cli.json` frozen) | (a) unfreeze for the case-insensitive Secret category only; (b) keep frozen and accept it | **(a)**: a one-category change, with the checker corpus updated in the same PR | (b) leaves a known guard gap for build agents |
| D12 | Topology profiles (HA control plane, arm64) | (a) keep as documented limits; (b) plan arm64 for mixed home labs | **(a)** for now; revisit arm64 if adopters ask ("a mix of different nodes" may include arm) | (b) per-arch checksums and images everywhere |
| D13 | Shrink `_cli_permissions.py` (a guardrail) | (a) keep; (b) shrink to safety categories plus a parity check | **(b)**, keeping a category corpus as the cheaper proof | Loses detailed matcher cases |

---

## 9. Rejected findings

- C F14 (role labels prefixed with `dns_suffix`): taste. All selectors substitute the same value, and changing the suffix is a rebuild anyway.
- C F22 (Renovate regex managers): live update automation with no proven replacement. The "missing pins" part was not reproduced.
- C F23 (prep-node is large): no failure named beyond J05 and J22, which are kept.
- C F25 (no `make site` target): convenience only.
- C F32 (tools split across interpreters): taste.
- C F37 (verify's own cron evaluator): live logic, no replacement proven.
- C F30 deletes of the `data_disk` group and the `storage` role: refuted; both are live inputs.
- C T08 as a finding: it is a guardrail-size choice; moved to decision D13.
- C T20 (kubeconform network schemas): not reproduced; four CRD schemas are vendored.
- B F13 (single control plane) and B F14 (amd64-only): documented limits with no named use from the direction; moved to decision D12.
- B F15 (observability placeholders) as a separate finding: merged into J25 and section 4.
- B "no defect in `cli.json`": refuted (J21).
- A F10 harm claim (cluster-reset destroys data): refuted by the k3s docs; kept as low doc item J44.
- A "nothing safe to delete": refuted (an empty kustomization builds; section 6).
- A "cut no tests": refuted by the dead assertion and the shape tests (J40).

---

## 10. Audit quality

| Auditor | Accepted as-is | Downgraded | Rejected or refuted | Notes |
| --- | --- | --- | --- | --- |
| A (Grok 4.7), 21 findings | 15 (F1, F2, F3, F5, F9, F11, F13–F21) | 6 (F4 and F7 to medium; F6, F8, F10, F12 to low) | 0 findings; 2 positions ("nothing safe to delete", "cut no tests") | Best on the adopter path: F3, F4, F5, F9 were unique or decisive. False positives: none outright, but F6 and F8 were rated high for a defaulted-No prompt and a missing doc. Missed URL credentials, OS patching, binder placement, CI depth, the agent token and CA scope |
| B (GPT-5.6 Sol), 25 findings | 12 (F01, F02, F03, F07, F10, F12, F16, F17, F18, F20, F23, F24) | 10 (F04, F05, F06, F21, F22 to medium; F08, F09, F11, F19, F25 to low) | 3 (F13, F14 to decisions; F15 merged); 1 position ("no cli.json defect") | Broadest on security and runtime: F20, F23, F24, F25 unique. The best framing of disk loss ("the drill can pass"). False positive: the cli.json verdict. Missed the cross-env owner rule and the stale ConfigMap, and treated OS patching as evidence only |
| C (Claude Opus 5.5), 39 F and 20 T | 36 | 15 (F01, F02 to high; F05, F06, F07, F08, F09, F10, T01, T02, T03, T16, T17 to medium; F19, F20 to low) | 8 (F14, F22, F23, F25, F32, F37, T08, T20), plus the F30 deletes of live code | Deepest line-level coverage (binder, token, agent token, CA scope, `ssh_user`, k3s restart, the seeded test gaps). Missed J05 (the prep-node order), which both others found. Inflated severities more often than A or B (two criticals that are high, five test highs that are medium). Its delete list over-reached on live code. I applied the same bar; no extra credit |

**Maintainer notes, found or missed**

- *Off-host copy, etcd restic-only path, per-namespace credentials:* found by all three.
- *Offline age-key backup:* B and C.
- *Rotation procedure:* all three.
- *Privileged movers:* B and C.
- *Automatic restore validation:* A only (F12).
- *Backup-failure alerts:* B (F15) only, as observability.
- *verify trusting the CA explicitly:* none.
- *Backup scale controls:* none.
- *Per-service Kustomizations:* C (F08).
- *Per-arch checksums:* B (F14) indirectly.
- *Known test weak spots:*
  - the env-00 flux-substitute gap: none;
  - hard-coded service IPs: none;
  - the chart resources lint: none;
  - the webhook rename stall: none;
  - the tag parser: C (T19) partly.

**What all three missed (judge only)**

- node-disk refuses a separate backup mount on data-disk hosts, contradicting the contract's own escape clause (J01);
- the age-key recovery doc discards live in-cluster secrets that could be resealed (J04);
- the drill race's misleading "checksum mismatch" without pipefail (J30, from the baseline);
- the Kyverno provenance retry warning (J43);
- the live host's stale base-os comment (J48).

---

## 11. Concerns and questions

- **Source of truth.** The standing user rule names Gitea (`git.lan/jarvis/cluster`) as jarvis-cluster's source of truth. This repo's remote is GitHub `gordoncooper/jarvis`, and its PRs merge there, so I opened the PR on GitHub. Please confirm that the rule doesn't apply to this repo.
- **Kyverno provenance warning (J43).** It needs a live look at the background-controller logs over a day. I can't tell offline whether it is upstream noise or a conflict between two generate rules writing the same resource.
- **J02 migration.** Sealing env-01's existing k3s token needs one owner step on the node. I did not and could not check the token's current value.
- **local-path binding mode (J12).** I relied on k3s's default `local-path` StorageClass being `WaitForFirstConsumer` (upstream default); no repo file overrides it. A live `kubectl get sc` would confirm it.
- **The chrony ruling (J31).** The ruling rests on PID 1 detection in the service module. It was proven by the scratch switch to `ansible.builtin.systemd`, which also passes on this non-systemd VM. CI on GitHub (systemd runners) should stay green; run CI on the fix PR to confirm.
- **The brief.** No conflicts with the code. The "known older failures: none" line holds; the only failure is J31.
