# Jarvis audit: adjudicated report

Judge: Claude Opus 5.5. Commit: `5561280d2c57e0b7d0d7d1a8c2eaa1a95cf9bdd8`.
Inputs: auditor **A** = Grok 4.7 (`findings-grok-4-7.md`, F1–F26, P1–P6), auditor **B** = GPT-5.6 Sol (`findings-gpt-5-6-sol.md`, F01–F23), both cross-checks, and the operator's live baseline (`baseline-SUMMARY.md` plus the two `ansible-check-*.log` files).

How I checked: I ran `make deps` (after `sudo apt-get update`, an environment fix), then `OWNER_NAME="Gordon Cooper" make ci-verify` (10 checks, 201 tests, all pass). Seeded experiments ran in a throwaway `git worktree` at the same SHA, which has since been removed. Restore behaviour was reproduced in-process with the `_Cluster` fake from `ci/tests/test_restore.py`. I never touched a node, a live cluster, an age key, or decrypted any `*.sops.yaml`; secret facts below come from plaintext SOPS metadata only.

Severity: **critical** = can lose data or report a false recovery; **high** = unsafe or blocks a normal change; **medium** = brittle or costly at the next growth step; **low** = clarity.
"Render" = output of `kustomize build` for any Flux path against the baseline `SHA256SUMS.txt`. "Check mode" = `site.yml --check --diff` against env-01 (baseline: `failed=1` at kubeconfig; with `--skip-tags kubeconfig`, `changed=2`).

---

## 1. Verdict

Jarvis is in better shape than most homelab repos: one GitOps path (k3s → Flux Operator → four layered Kustomizations), SOPS with an `encrypted_regex`, a schema-checked env folder, pinned digests with Renovate, a real restore tool with a rebuild hold, and CI checks aimed at agent authors. The structure (`infra/ platform/ services/ apps/ envs/`) is right and should stay.

The weak point is not shape but **proof**. Several safety paths report success without evidence: restore can lift the rebuild hold having restored nothing; the exit checks return 0 on a regex match or with owner items left; four CI checks accept a seeded bad value; and enrollment is fail-open with no live check. The second theme is that **build-process artefacts leaked into the product**: phase names (`p0a`, `p0b`), plan codes (R/D/OQ), the AI-agent vendor as a *required* config value, history in CONTRACTs, and a drill script that lives in a PR description.

**Top three risks**
1. **False recovery** (J1, J7): `make restore-all` on a rebuilt cluster with an empty or wrong backup repo lifts the hold and exits 0, and backups resume over the empty volumes. The verify scripts would not catch it.
2. **Live data exposure and a broken dry-run on the node** (J3, J4): every real Ansible run widens the local-path data root from 0700 to 0755, which exposes every PVC (each volume dir is 0777) to any local user. The kubeconfig role makes `--check` unusable, so nobody can preview a run safely.
3. **Guardrails that look stronger than they are** (J6, J5): unknown `${...}` keys, extra LoadBalancers in allow-listed paths, a decoy enrollment policy, and probe-less charts all pass CI. Meanwhile, `prep-node` crashes at its final step.

**How close to adoptable:** about two focused PR waves away. Nothing is architecturally wrong. An adopter today hits: a crashing `prep-node`; no single "fresh copy → running cluster" page; amd64, NVIDIA and single-control-plane limits that are not written down; env-tree files that must be hand-copied (four Flux Kustomizations plus a core component list); and docs that point to a private plan repo. Waves 1–2 of the PR sequence (section 9) fix the safety items; wave 3 makes it adoptable.

---

## 2. Ranked findings

### J1. Restore can report success and lift the rebuild hold with nothing restored
- **Type / severity:** correctness, safety. **Critical.**
- **Found by:** B (F01, F02). A found the neighbouring restore issues (J2) but not this one, and A's cross-check confirmed both B items.
- **Evidence (reproduced):** `tools/restore:855-861`:
  ```
  except NoSnapshot:
      _line(f"SKIP no snapshot {namespace}/{pvc}")
  done.append((namespace, pvc))
  ...
  if all_pvcs and hold is not None:
      _lift_hold(...)
  ```
  A held cluster whose repo has no snapshot for `demo/drill`, run through the `_Cluster` fake from `test_restore.py`, prints `SKIP no snapshot demo/drill` then `HOLD lifted`, returns rc 0, and the hold ConfigMap is deleted. `_lift_hold` (`:759-805`) also deletes the hold *before* it has confirmed that every ReplicationSource resumed, so a failure part-way leaves no hold and paused sources.
- **Effect if left:** after a rebuild with a wrong or empty `RESTIC_REPOSITORY`, a typo'd env, or a lost bucket, the hold lifts and the next backup run snapshots empty volumes over the history (retention eventually prunes the good ones). This is the one path that can silently destroy the only copy.
- **Suggested fix:** in `--all` mode, `NoSnapshot` is a failure unless the PVC is named in `--retire`; `done` holds only actually-restored PVCs. Lift the hold only after every source shows unpaused, and print one summary line (`restored=N skipped=M retired=K`). Tests go in `ci/tests/test_restore.py`: held + missing snapshot gives rc≠0 and the hold remains; held + `--retire` for that PVC gives rc 0; a resume failure keeps the hold. **Done check:** `pytest ci/tests/test_restore.py`.
- **Fix risk:** tool only. No render change, no check-mode change.

### J2. Restore runs against the ambient kube context, quiesces two kinds, and loses its undo plan on Ctrl-C
- **Type / severity:** safety. **High.**
- **Found by:** A (F2, F16). B's cross-check partly confirmed it ("dangerous"; it did not repeat the ambient-context point). Merged.
- **Evidence:**
  - `Makefile:38-40` runs `python3 tools/restore --app "$(APP)"` with no `ENV`, no `--context` and no confirmation, against whatever `~/.kube/config` points at. J4 makes that file whichever cluster Ansible touched last.
  - `_quiesce` (`tools/restore:409-429`) scales only Deployments and StatefulSets. CronJobs, Jobs and DaemonSets keep writing during a `copyMethod: Direct, enableFileDeletion: True` restore (`destination_body`).
  - The undo lines print only in `except Fail:` (`:709`). A `KeyboardInterrupt` injected at the ReplicationDestination apply in the fake printed no undo lines (`undo lines printed? False`), leaving workloads scaled to 0 and sources paused with no instructions.
  - `AS_OF=` *does* reach the tool: command-line make variables are exported (verified with a toy Makefile). That part of A's F2 is withdrawn.
- **Effect if left:** a restore aimed at the wrong cluster deletes live files. A CronJob can write into a volume mid-restore. An interrupted run leaves an app down with no recovery hint.
- **Suggested fix:** require `ENV=` in `make restore`/`restore-all`. The tool derives the expected API server from `envs/<env>` (or a per-env kubeconfig, J4), refuses on mismatch, and passes `--context`/`--kubeconfig` to every `kubectl` call. Suspend CronJobs (`spec.suspend`) alongside scaling, and refuse when a non-owned Pod mounts the PVC. Catch `BaseException` around the mutation section to print the undo plan, then re-raise. Tests: wrong-context refusal, CronJob suspend/resume, KeyboardInterrupt prints undo. **Done check:** `pytest ci/tests/test_restore.py`.
- **Fix risk:** tool plus Makefile. No render or check-mode change.

### J3. Ansible resets the local-path data root to 0755 on every run (live finding 2)
- **Type / severity:** safety, brittle. **High.**
- **Found by:** judge only (live baseline). Neither auditor could see it; neither flagged the mode in code.
- **Evidence:** `ansible-check-env-01.log:162-167`:
  ```
  -    "mode": "0700",
  +    "mode": "0755",
  changed: [bench.lan] => (item=/var/lib/jarvis/local-path)
  ```
  Two roles own the same directory, both at 0755: `infra/ansible/roles/k3s/tasks/main.yml:37-49` (`local_path_data`, `backup_path`, `backup_path/etcd-snapshots`) and `roles/node-disk/tasks/main.yml:81-88` (`local_path_data`, `backup_path/etcd-snapshots`). The thing resetting it on the node is k3s itself: the bundled local-path-provisioner `setup` script runs `mkdir -m 0777 "$VOL_DIR"` and `chmod 700 "$VOL_DIR/.."` on each provision (upstream k3s `manifests/local-storage.yaml`). Each PVC dir is therefore 0777, and the 0700 parent is the only barrier.
- **Effect if left:** after every `site.yml` run, any local user or compromised non-root process on the node can read and write every PVC (mail, calendar, tokens) until the next provision flips it back. The check-mode diff also never reaches `changed=0`, which breaks the PR template's own idempotence box.
- **Suggested fix:** let one role own each path (node-disk owns the mount and the dirs under it; k3s stops creating `local_path_data`). Set `local_path_data` to `0700`, and `backup_path` and `etcd-snapshots` to `0700` too (etcd snapshots contain the whole datastore). Add a role-proof test asserting the mode and that only one role manages each path. **Done check:** `pytest ci/tests/test_k3s.py` plus a second real run showing `changed=0` for that task.
- **Fix risk:** Ansible only. This **changes the check-mode result** (one fewer `changed`). No render change. The rest-server pod runs as `runAsUser: 0` (`platform/backups/rest-server.yaml:56`), so 0700 on `backup_path` does not break it.

### J4. The kubeconfig role fails in check mode and overwrites the operator's `~/.kube/config` (live finding 1)
- **Type / severity:** correctness, safety. **High.**
- **Found by:** check-mode failure, judge only (live). Clobbering and the fixed URL shape: both (A F15, B F23), and both cross-checks confirmed.
- **Evidence:** `ansible-check-env-01.log:241-244` shows `dest: "{{ k3s_kubeconfig_staging.path }}/k3s.yaml"` then `fatal: [bench.lan]: FAILED!` (the error itself is hidden by `no_log`). Cause: `roles/kubeconfig/tasks/main.yml:8-12` uses `ansible.builtin.tempfile`, which is skipped in check mode, so `.path` is undefined at `:20`. The run aborts before the Flux Operator play. `:43-48` then copies to `$HOME/.kube/config` with `backup: true`, which replaces any other cluster's context and leaves timestamped backups of a cluster-admin credential.
- **Effect if left:** nobody can preview `site.yml`, and the baseline shows the operator already needs `--skip-tags kubeconfig`. A second env, or any other cluster, silently replaces the operator's context, which feeds straight into J2.
- **Suggested fix:** replace tempfile + fetch + replace + copy with `slurp` of `k3s_kubeconfig_path` (works in check mode), then one `copy content=` using `regex_replace` of the server URL to `~/.kube/jarvis-<env>.yaml` (mode 0600, no `backup`, `no_log`). Fail if the server line did not change. Document `KUBECONFIG=~/.kube/jarvis-<env>.yaml` and use it from `tools/restore`, `tools/verify/*` and the flux-operator role. Tests: the role-proof asserts there is no `tempfile`, the dest path contains the env name, and there is no `backup:`. **Done check:** `pytest ci/tests/test_flux_operator.py`, and offline `ansible-playbook --check` against a localhost fixture if one exists.
- **Fix risk:** Ansible and tools. Check mode goes from `failed=1` to passing (`changed` count unchanged). No render change. The operator's existing `~/.kube/config` is no longer written; call that out in the PR.

### J5. `prep-node` crashes at its last step, so an adopter's first command fails
- **Type / severity:** correctness. **High.**
- **Found by:** both (A F1, B F06), and both cross-checks confirmed.
- **Evidence:** `tools/prep-node:1002-1004` runs `repo / "tools" / "checks" / "env-schema"`; `ls tools/` has no `checks/`, while `ci/checks/env-schema` exists. Both auditors reproduced the `FileNotFoundError`. It is not a `PrepError`, so the user gets a traceback after their env files are already written.
- **Effect if left:** the first adopter step ends in a traceback, and the env-schema gate inside prep-node has never run.
- **Suggested fix:** call `ci/checks/env-schema`, or better, import `schema.env` and validate in-process (one place). Catch `OSError` as `PrepError`. Test: run `run_env_schema` against `envs/env-00` in `ci/tests/test_prep_node.py` (no mock of the path). **Done check:** `pytest ci/tests/test_prep_node.py`.
- **Fix risk:** tool only. No render or check-mode change.

### J6. Five CI rules accept a seeded bad value
- **Type / severity:** brittle (guardrail). **High.**
- **Found by:** both (A F8, F11, F19; B F09, F10). B's split `image.repository`/`tag` claim is **refuted** (A's cross-check, and I re-checked it). Everything else is confirmed.
- **Evidence:** each seed was added to the worktree, then CI was run and the seed reverted. Every seed below passed CI:

  | Seed | Expected catcher | Result |
  |---|---|---|
  | ConfigMap `data.host: "${not_a_cluster_var}.${dns_suffix}"` listed in `services/kustomization.yaml` | schema / kustomize build | `ci/run-checks` all pass; `pytest ci/tests` **201 passed** |
  | Extra `type: LoadBalancer` Service under `platform/traefik/` | `loadbalancer` | `[]` (allow-list is by directory prefix) |
  | Decoy `ClusterPolicy` replacing `platform/backups/enrollment.yaml` | `pvc-backup` | `[]` (`_enrollment_policy_listed` accepts any ClusterPolicy) |
  | HelmRelease with `values: {}`; HelmChartConfig with a tag-only image | `manifest-lint`, `digest-pin` | `[]` (chart values and HelmChartConfig are not inspected) |
  | A YAML document that is a list, not a mapping | all scanners | `[]` (`load_documents` skips non-dict docs silently) |
  | HelmRelease `values.image: {repository, tag}` | `digest-pin` | **flagged**, so B's claim is refuted |

  Flux's `StrictPostBuildSubstitutions` would reject the unknown key only at reconcile time on the cluster, after merge.
- **Effect if left:** agents (the reason these checks exist) can merge a manifest that stalls a Flux layer, an extra exposed LB, or a no-op enrollment policy, and CI reports green.
- **Suggested fix:**
  - (a) A new check (or extend `env-schema`) that collects every `${name}` in every Flux path and asserts it is a key of the generated cluster-vars, or in the HelmRelease-`substitute: disabled` set.
  - (b) The LoadBalancer allow-list keyed on `(kind, namespace, name)`, not path.
  - (c) `pvc-backup` requires the policy by name plus its generate rule's `kind: ReplicationSource`.
  - (d) A non-mapping document is an error.
  - (e) `manifest-lint` requires an explicit `values.resources` key (it may be `{}` with a comment) on HelmReleases, and `digest-pin` reads HelmChartConfig `valuesContent`.

  Each gets a seeded-failure test in its existing test file (the PR template already demands this). Share one scan-root helper (J18). **Done check:** `OWNER_NAME=... make ci-verify` plus the new seeded tests.
- **Fix risk:** CI only. No render change. (e) may require adding `resources` to existing HelmReleases, which **changes the render**; land it separately if so.

### J7. Backup enrollment and the exit checks pass without proof
- **Type / severity:** safety. **High.**
- **Found by:** both. Enrollment fail-open: A F3, B F08. Exit checks: A F23 (p0a), B F11 (p0a and p0b). B's own cross-check withdrew its "use `failurePolicy: Fail`" direction, and A's keeps `Ignore`. **Decision: keep `Ignore`** (a Kyverno outage must not block PVC creation) and add a runtime proof.
- **Evidence:**
  - `platform/backups/enrollment.yaml:10` has `failurePolicy: Ignore`, and nothing checks that every bound PVC has a ReplicationSource.
  - `tools/verify/p0a` exits 0 printing "automatic checks passed; N owner items remain" (the baseline shows 14 owner items, exit 0).
  - `tools/verify/p0b` proves a restore by regex over a free-text transcript (`_RESTORE = r"\brestore\b"`, `_SNAPSHOT = r"etcd-snapshot-\S+"`, checksum lines). A seeded transcript containing `restore failed: no data` plus a repeated hash returned `recorded_checksum` = that hash, i.e. pass.
  - The drill script itself is "in the PR description" (`platform/backups/CONTRACT.md:51`), not in the repo.
- **Effect if left:** a PVC created during a Kyverno blip is never backed up, and nothing says so. The exit checks certify a restore that did not happen.
- **Suggested fix:** replace `tools/verify/p0a` and `p0b` with one `make verify ENV=<env>` that is read-only and fail-closed:
  - every Flux object is Ready;
  - every bound PVC outside an allow-list has a ReplicationSource with a recent successful sync;
  - the etcd snapshot is younger than 2× its schedule;
  - the `wildcard-tls` chain validates against `envs/<env>/ca.crt`;
  - no rebuild hold is present (unless the caller asks for one);
  - non-zero exit if anything is unchecked.

  Move the restore drill into the repo as `make drill ENV=` (scratch namespace → write payload → force sync → delete → restore → compare checksum, all by the tool and not by transcript parsing). Tests: fixture-driven unit tests per check, plus one "owner item left gives rc≠0" test. **Done check:** `pytest ci/tests/test_verify*.py`.
- **Fix risk:** tools, Makefile, docs. No render change. Removes the `PHASE=` interface; docs and `rulebooks` must follow.

### J8. All backups share one credential, one writable server and one disk
- **Type / severity:** safety. **High** (data durability).
- **Found by:** credential: B (F04). Same-disk, no off-host copy: both (A F5, B F03). Both confirmed. A's cross-check rejected B's wording "as promised"; I agree the docs do not promise hardware-loss recovery, but the stated goal (rebuildable "including restoring backed-up data") does.
- **Evidence:**
  - `platform/backups/enrollment.yaml:48-51` copies one `RESTIC_PASSWORD` and REST user into every per-PVC Secret.
  - `rest-server.yaml:2-3` says "No --append-only … No --private-repos: one user".
  - `envs/env-01/inventory` has no `data_disk`, so `backup_path` sits on the same root disk as `local_path_data`.
- **Effect if left:** any namespace that can read its own backup Secret can delete or rewrite every other app's repository, and a single disk loss takes the live data and the backups together. Services from the code factory make the first point real.
- **Suggested fix:** (1) Now, small: document in `platform/backups/CONTRACT.md` and the adopter guide that backups are same-host unless `backup_path` is on another disk, and make `prep-node` warn when it is. (2) Then, M: rest-server `--private-repos` with one htpasswd user per namespace generated by Kyverno from a per-namespace secret (or per-PVC restic passwords derived per repo), so one namespace's credential cannot touch another's repo. (3) Later, L: an off-host copy tier (see better approaches, B3).
- **Fix risk:** (2) changes the render (enrollment and rest-server) and needs a migration of existing repos; it is its own PR. (1) is docs only.

### J9. The env flux tree carries core component lists and hand-copied boilerplate
- **Type / severity:** rigid, complexity. **Medium-high** (it hurts the moment services land).
- **Found by:** judge (structure). A P1 proposed generating the env flux entry; B F19 noted the services/apps coupling. Neither noticed that the *core platform component list* lives in env-01.
- **Evidence:**
  - `envs/env-01/flux/platform-configs/kustomization.yaml` lists `../../../../platform/backups`, `cert-manager/config` and `platform/testapp` plus the env secrets. Every new platform component must be added to every env.
  - The four `envs/env-01/flux/kustomizations/*.yaml` are hand-written with prune/substitute/dependsOn blocks.
  - `envs/env-01/flux/flux-instance.yaml` duplicates `roles/flux-operator/templates/flux-instance.yaml.j2`; the Flux version `2.8.8` is pinned in both (`defaults/main.yml:10`, `flux-instance.yaml:10`), held together by `test_flux_entry::test_git_flux_instance_matches_ansible_template`. That is a patch on a patch.
  - The env-01 README "New env" step 5 just says "Add a flux/ tree".
- **Effect if left:** a second env, or the first service, means copying four to six files by hand. A forgotten line in one env silently drops a platform component there. Every Flux bump touches two files plus a test.
- **Suggested fix:** move the component list to core (`platform/configs/kustomization.yaml` listing backups, cert-manager config and testapp), so the env layer lists only `platform/configs` plus its own secrets. Make Ansible apply the committed `envs/<env>/flux/flux-instance.yaml` instead of templating its own copy, which drops the template, the second pin and the sync test. Add a check that each non-example env has exactly the four standard Kustomizations, with the paths the schema expects. Do not build a generator yet (see B1). **Done check:** `make ci-verify`, and a render diff showing identical objects.
- **Fix risk:** the render path changes; **objects should be byte-identical** if the component order is preserved. Verify against the baseline `SHA256SUMS.txt`, sorted by object. Prune guards (J11) must cover the move: a path move is exactly the race `test_prune_guards.py` exists for. Check mode: the flux-operator play changes its task (template → copy), so expect `changed=0` only if the content is identical.

### J10. Disk and network steps can hide or disable live state
- **Type / severity:** safety. **Medium-high.**
- **Found by:** both (A F4, F7, F21; B F05, F07), all confirmed.
- **Evidence:**
  - `roles/node-disk` mounts `node_disk_mount` (`/var/lib/jarvis`) without checking that the mountpoint is empty, and runs `wipefs` when the label is absent. Multi-device Btrfs members, "mounted elsewhere" and "has partitions" are not checked, though the role README lists them.
  - `tools/prep-node:613-616` moves every other `/etc/netplan/*.yaml` to `.bak`.
  - Agent↔server transitions do not disable the opposite unit (B F05).
- **Effect if left:** adding a data disk to a running node mounts an empty filesystem over live PVC data (it looks like data loss). A second NIC or a Wi-Fi config is silently disabled by prep-node.
- **Suggested fix:**
  - node-disk refuses to mount over a non-empty `/var/lib/jarvis` unless an explicit migrate flag is given (and documents the migrate steps);
  - `lsblk -J`-based guards for holders, mounts, partitions and Btrfs devices before `wipefs`;
  - prep-node writes its own netplan file with a higher priority and leaves the others alone (or lists them and asks);
  - the k3s role fails when the opposite unit is enabled.

  Tests in `test_node_disk*.py`/`test_prep_node.py` with lsblk fixtures. **Done check:** pytest.
- **Fix risk:** Ansible and tools. No render change. Check mode unchanged on env-01 (no data disk).

### J11. Secret lifecycle: the CA is not prune-guarded, the guard list is hand-synced, and seal paths are unconstrained
- **Type / severity:** safety. **Medium-high.**
- **Found by:** CA prune: B (F13). Seal path, key mode: B (F13). Hand-synced guard list: neither (A explicitly kept the list as is). The unused secrets: B saw them and kept them as future; A said nothing to delete.
- **Evidence:**
  - `envs/env-01/secrets/backups-restic.sops.yaml:7` has `kustomize.toolkit.fluxcd.io/prune: disabled`, while `cert-manager-ca.sops.yaml` metadata has none (plaintext metadata, `encrypted_regex: ^(data|stringData)$`).
  - `ci/tests/test_prune_guards.py:20-43` is a hand-written `_PROTECTED` set that includes `backups-restic` but not `cert-manager-ca`, the ClusterIssuer or the `pvc-backup` ClusterPolicy.
  - `tools/seal` builds `envs/<env>/secrets/<name>.sops.yaml` with no segment validation, and `tools/lib/_sops.py` only enforces "inside the repo".
- **Effect if left:** moving or renaming the platform-configs path (J9) prunes the live CA, and every client that pinned it breaks until someone re-applies. The CA ciphertext is recoverable from Git, as B's cross-check corrected. A bad `--env`/`--name` can write a SOPS file into another repo subtree.
- **Suggested fix:** derive the protected set instead of hand-listing it: every object in `platform-controllers`/`platform-configs` that is a Namespace, CRD, Secret, HelmRelease or ClusterPolicy must carry `prune: disabled`, minus an explicit short allow-list. Annotate `cert-manager-ca`, which needs a re-seal: `bootstrap-secrets` must add the annotation for new envs, and env-01 can be re-sealed by the owner. Validate `env`/`name` against `^[a-z0-9-]+$` and that the resolved parent is `envs/<env>/secrets`. Require the age key file to be 0600 and owned by the runner. **Done check:** `pytest ci/tests/test_prune_guards.py ci/tests/test_seal*.py`.
- **Fix risk:** **changes the render** (one annotation on the CA Secret, plus any newly guarded objects). The owner must re-seal env-01; agents cannot (no keys).

### J12. Ansible validates after it mutates, and treats lookup errors as "absent"
- **Type / severity:** brittle. **Medium.**
- **Found by:** B (F12), confirmed by A's cross-check. The arch assert placement is also in A F10.
- **Evidence:**
  - `site.yml`'s first assert play checks only `control_plane == 1` and GPU; the arch and var asserts are in `roles/k3s/tasks/main.yml:30-31`, after `base-os` has already changed the host.
  - `failed_when: false` in `flux-operator/tasks/operator.yml:9` and `sops_age.yml:9`; any API error there takes the bootstrap path.
  - The chrony template has no restart handler.
- **Effect if left:** a run on an arm64 box half-configures it before failing. A transient API error re-bootstraps Flux or the SOPS secret.
- **Suggested fix:** move all host and var asserts (arch, required vars, disk vars) into the first play. Classify NotFound explicitly (as `instance.yml` already does). Add the chrony handler. **Done check:** `pytest ci/tests/test_flux_operator.py ci/tests/test_k3s.py`.
- **Fix risk:** Ansible only. Check mode: the same `changed` count.

### J13. Hardware ceilings are hard-coded and undocumented: amd64, NVIDIA, one control plane
- **Type / severity:** rigid. **Medium** (the adopter blocker is the missing statement, not the limit).
- **Found by:** both (A F9, F10, F22, P5; B F14, F15), confirmed. Both cross-checks agree: document now, do not build HA.
- **Evidence:**
  - `roles/k3s/tasks/main.yml:30-31` asserts `x86_64` and downloads the amd64 asset with one checksum.
  - GPU detection hard-codes vendor `0x10de`.
  - `site.yml` asserts `control_plane | length == 1`, and `config.yaml.j2` has `cluster-init: true`.
- **Effect if left:** a Pi or Apple-silicon adopter fails mid-run, and an AMD/Intel GPU node is silently "no GPU".
- **Suggested fix:** state the three limits in the root README "Requirements" section and in the schema's error messages (the schema rejects arm64 or unsupported GPU up front, from inventory facts declared in `envs/<env>/inventory`). Per-arch checksums (`k3s_sha256: {amd64, arm64}`) only when an arm64 env exists. **Done check:** schema tests for the rejected cases.
- **Fix risk:** docs plus schema. No render or check-mode change.

### J14. Core config requires build-process and unconsumed keys, and the ConfigMap exports everything
- **Type / severity:** rigid, complexity. **Medium.**
- **Found by:** unconsumed Hermes/apps config: both (A F14, B F18). `build_agent` and the full ConfigMap export: B partly (`age_key_file`, `ssh_user` exported). `build_agent: cursor` being required: **judge only**.
- **Evidence:**
  - `schema/env.py:34-54` requires `code_delivery`, `build_agent`, `apps_git_repo_url` and `llm_provider_host`; `:295` validates `build_agent` against an enum whose only value is `cursor`.
  - `git grep` outside `envs/ ci/ schema/ docs/` finds no runtime consumer for `build_agent`, `llm_provider_host` or `code_delivery`, and only core-names uses `apps_git_repo_url`.
  - `tools/lib/render_cluster_vars_configmap.py` publishes every resolved key (`ssh_user`, `age_key_file`, `k3s_sha256`, …) into the cluster.
  - `schema/env.py` checks `local_path_data` is absolute but not `backup_path`; neither is checked to sit under `node_disk_mount`.
  - `envs/env-01/values/hermes.yaml` has `bots: []` alongside a five-bot `bot_tier_map`.
- **Effect if left:** this contradicts "must not depend on the AI agents that build it". An adopter must declare an AI vendor to deploy a cluster. Unused keys get invented values, host-side settings leak into cluster-readable config, and a relative `backup_path` produces a hostPath of the wrong shape.
- **Suggested fix:** remove `build_agent` from the env schema (it belongs in `rulebooks/`, if anywhere). Make `code_delivery`, `apps_git_repo_url`, `llm_provider_host` and `hermes.*` optional until the PR that adds their consumer; that PR adds the validation. Render only an explicit allow-list of cluster keys into the ConfigMap (host-only keys stay in Ansible vars). Add the absolute-path check for `backup_path`, and a "both paths under `node_disk_mount`" check when a data disk is declared. **Done check:** `pytest ci/tests/test_env_schema.py` and an env-00 schema pass.
- **Fix risk:** **changes the render** (the cluster-vars ConfigMap loses host-only keys). J6(a) must land first so no manifest silently referenced a dropped key.

### J15. Versions and schedules repeated outside their one pinned place
- **Type / severity:** brittle. **Medium.**
- **Found by:** etcd cron, kubectl minor, `cluster.local`: A (F18). Open Python ranges: B (F22). The restic digest in `tools/restore`: **judge only**. The Flux double pin is in J9.
- **Evidence:**
  - `tools/restore:43` and `platform/backups/etcd-snapshots.yaml:29` hold the same `restic/restic@sha256:136600b6…`. Renovate's restic manager only matches `etcd-snapshots.yaml`, so a bump updates one of them.
  - `etcd-snapshots.yaml:11-12` hard-codes `"5 */12 * * *"`, "five minutes after" `roles/k3s/defaults/main.yml:8`.
  - `.github/workflows/ci.yml:20,26` says "pinned", but `requirements-dev.txt` has `pytest>=8.0`, `PyYAML>=6.0`, `bcrypt>=4.0`.
- **Effect if left:** the restore bind pod runs a different restic than the backups; changing the etcd schedule in env values silently desynchronises the copy job; CI can change behaviour without a commit.
- **Suggested fix:** `tools/restore` reads the image from the rendered `etcd-snapshots.yaml` (or a shared `tools/versions.env` entry with a Renovate rule). Put the etcd schedule in cluster-vars with both consumers substituting it. Pin `requirements-dev.txt` exactly (Renovate pip manager). **Done check:** a test asserting a single source for each.
- **Fix risk:** the etcd schedule move changes the render only if the value changes (it should not). Check-mode unchanged.

### J16. Docs and tools carry build-plan history instead of build instructions
- **Type / severity:** complexity (adopter clarity). **Medium.**
- **Found by:** A (F17, contracts ahead of the tree); B partly (the `volsync-test` paragraph, F22's rulebook note). Most of the instances below: **judge only**.
- **Evidence:**
  - `platform/backups/CONTRACT.md` is 70 lines of run-on paragraphs mixing interface with history ("The OQ7 test bundle is removed") and pointing at a drill script "in the PR description".
  - `envs/env-01/README.md` carries one-time cleanup, R/D/p-codes and "admin-gate after P2 exit".
  - `Makefile:11,29-35` exposes `PHASE=p0a|p0b`; `tools/verify/p0a:206` lists `p0a-16` milestones; `docs/code-delivery/CONTRACT.md:60` defines repo hosting by "P0a exit".
  - `README.md:5`, `docs/README.md:5` and `AGENTS.md` point at `https://github.com/<owner>/project-plans`, a private plan repo an adopter cannot see.
  - `rulebooks/coding-agents.md` is dense with R/D codes.
- **Effect if left:** an adopter cannot tell interface from history, and the core docs depend on a repo they don't have.
- **Suggested fix:** each CONTRACT gets a fixed shape: *Provides / Requires / Config keys / Operations / Limits*, with no history and no phase codes. Move history to git log or the plan repo. Put the restore drill in the repo (J7). Root README: one adopter path (J17) and no project-plans link (keep that in `AGENTS.md`/`rulebooks/` only, for agents). **Done check:** extend `contract-md` to reject `\b(P\d|p0[ab]|R\d+|D\d+|OQ\d+)\b` in CONTRACTs and READMEs outside `rulebooks/`.
- **Fix risk:** docs plus one check. No render change.

### J17. No repository-only path from a fresh copy to a running cluster
- **Type / severity:** complexity (adoption). **Medium.**
- **Found by:** both (A F13, F20, F24; B F20), confirmed.
- **Evidence:** the adopter walk is in section 6. It took **14 files** to assemble the order prep-node → bootstrap-secrets → seal → site.yml → verify. `make ci-verify` fails with `OWNER_NAME is unset` and the Makefile never mentions it (`ci-verify-no-owner.log`). The `infra/ansible/README.md:44-46` tag examples omit `-i`/`-e`.
- **Suggested fix:** one `docs/deploy.md` (prerequisites, then numbered commands, then what each produces). `make ci-verify` defaults `OWNER_NAME` to `git config user.name` with a message. Fix the tag examples. **Done check:** a doc-link test that every `make` target named in `docs/deploy.md` exists.
- **Fix risk:** docs plus Makefile. None.

### J18. Duplicated helpers and boilerplate across `ci/`, `tools/` and `schema/`
- **Type / severity:** complexity. **Low-medium.**
- **Found by:** scan roots: A (F19). `_tool_env` and the bash heredoc wrappers: **judge only**.
- **Evidence:**
  - `ci/checks/lib/_manifests.py:9-10` and `_pvc_backup.py:21-23` copy `SCAN_ROOTS`/`SKIP_ENV_PREFIXES`.
  - `_tool_env` is copied in `tools/restore:79`, `tools/verify/p0a:37` and `p0b`.
  - Each of the 10 `ci/checks/*` is a bash wrapper around an inline Python heredoc that repeats the same import and exit boilerplate.
- **Suggested fix:** `pvc_backup` imports `_manifests.iter_manifests`. One `tools/lib/_env.py`. `ci/checks/<name>` becomes `python3 -m ci.checks.lib.<name>` (or a single dispatcher), keeping one file per check for the run list. **Done check:** `make ci-verify`.
- **Fix risk:** CI only. None.

### J19. Tests that lock shape or accept failure
- **Type / severity:** brittle. **Low-medium.**
- **Found by:** B (F22). A's cross-check partly confirmed it and narrowed the deletions. The large-file hotspot: B only.
- **Evidence:**
  - `ci/tests/test_flux_operator.py:61-92` locks all eight `site.yml` plays, while the safety fact is "the first play has no roles and runs before base-os".
  - The fresh-cluster test accepts a non-zero play exit when the output contains `chown failed` (`test_flux_operator.py:~521-522`).
  - The three scanner tests also re-run `scan_*(REPO) == []`, which `ci/run-checks` already does.
  - Test modules import from each other (`test_prune_guards` imports `test_flux_entry._kustomize_build`).
  - Largest files: `test_restore.py` 589, `test_flux_operator.py` 573, `test_env_schema.py` 526.
- **Suggested fix:** replace the order lock with the safety fact; delete the three tree-rescan asserts (keep the seeded cases); move shared helpers to `ci/tests/support.py`; make the fixture require success. **Done check:** pytest.
- **Fix risk:** tests only.

### J20. Platform configs apply before controllers are Ready
- **Type / severity:** brittle (ordering). **Medium-low.**
- **Found by:** both (A F12, B F08), confirmed.
- **Evidence:** `envs/env-01/flux/kustomizations/platform-configs.yaml` has `dependsOn: platform-controllers`, and the controller Kustomization has no `healthChecks`/`wait`. A code comment says a HelmRelease healthCheck would stall on a public chart pull.
- **Effect if left:** on a cold rebuild, ClusterIssuer and ClusterPolicy objects race their CRDs and webhooks. Flux retries, so this is slow, not broken. But the Kyverno enrollment policy can miss PVCs created in that window (J7).
- **Suggested fix:** `healthChecks` on the two webhook Deployments (cert-manager-webhook, kyverno-admission-controller) only, with a timeout. The J7 enrollment check covers the rest. **Done check:** render test asserting the healthChecks.
- **Fix risk:** **changes the render** (env Kustomization spec).

### J21. Backup scale controls are missing
- **Type / severity:** rigid. **Low.**
- **Found by:** B (F17), confirmed.
- **Evidence:** one schedule for all sources; `cacheCapacity: 1Gi` per PVC; no `moverResources`.
- **Suggested fix:** add mover resources and a PVC-annotation override for the schedule when the first large-volume service lands. Not now.
- **Fix risk:** render.

### J22. CA rotation does not reissue the leaf
- **Type / severity:** safety (operational). **Low.**
- **Found by:** A (F6). B's cross-check partly confirmed it.
- **Evidence:** `envs/env-01/secrets/README.md:27-31` holds the reissue caveat and the delete step; `platform/cert-manager/CONTRACT.md:25` does not.
- **Suggested fix:** move it into the CONTRACT's Operations section (J16).
- **Fix risk:** docs.

### J23. The command guard is a local parser clone, so its tests prove self-consistency
- **Type / severity:** brittle (guardrail). **Medium-low.**
- **Found by:** both (A F25, B F21), confirmed.
- **Evidence (by category only):**
  - `ci/checks/cli-deny` and `ci/checks/lib/_cli_permissions.py` re-implement the client's allow/deny matching and test a corpus against that re-implementation, so drift between the clone and the real client cannot show up.
  - A also notes category gaps: default behaviour when no Shell allow entry exists, and case/normalisation handling.
  - The corpus hard-codes secret file names (`grafana-admin`), which ties it to one env's layout.
- **Suggested fix:** keep the guard. Generate the corpus from categories (decrypt any `envs/*/secrets/*.sops.yaml`, read the key directory, and so on) rather than specific file names. Assert deny-by-default for the Shell category. Document that the dry-run proves the policy, not the client. Gitleaks stays.
- **Fix risk:** CI only.

### J24. Stale apt comment on the node (live finding 3)
- **Type / severity:** drift. **Low.**
- **Found by:** judge (live).
- **Evidence:** `ansible-check-env-01.log:50` shows `-# Deliberate departure from framework §4 unattended upgrades until project docs are updated.` The repo removed it in #77 (`48a1ba8`), and the node has not had `site.yml` re-run since.
- **Fix:** none in the repo; the owner re-runs `site.yml` after J4 lands. A second run then shows this task `ok`.

---

## 3. Better approaches (endorsed)

| # | Approach | Replaces | Pros | Cons / what we lose | Size | Future use |
|---|---|---|---|---|---|---|
| B1 | **Core `platform/configs` + env adds secrets only**; Ansible applies the committed env `FluxInstance` | env component lists, the j2 template, the double Flux pin and its sync test (J9) | One place per component; a new env is a copy of four small files; no sync test | One more Kustomization dir; the move needs prune guards (J11) first | S–M | second env, every new platform component |
| B2 | **One `make verify ENV=` + in-repo `make drill ENV=`** | `tools/verify/p0a`, `p0b`, transcript regexes, the drill-in-PR-description, `PHASE=` (J7, J16) | Fail-closed, phase-free, usable by adopters forever; proves enrollment and restore | Loses the phase-gate framing the build plan used; the plan repo must re-point | M | every deployment, every rebuild |
| B3 | **Off-host backup tier**: a second restic target (USB disk, NAS or another node) via a VolSync second source or a `restic copy` CronJob; token escrow documented | same-disk-only backups (J8) | Real hardware-loss recovery | Another target to manage; adopter prerequisite | L | anyone's deployment; "rebuild from repo + key + backups" |
| B4 | **Per-namespace backup credentials** with rest-server `--private-repos` | the shared credential (J8) | Blast radius = one namespace | Migration of existing repos; Kyverno generate gets more complex | M | services and apps from the code factory |
| B5 | **Component-owned optional config**: schema keys are optional until their consumer lands; the ConfigMap publishes an allow-list | required unused keys, `build_agent`, full export (J14) | Lean env folder; no agent vendor in core | Each component PR adds its own schema block | S | services, apps, other people's envs |
| B6 | **Per-env kubeconfig via slurp/copy** | tempfile/fetch/replace/copy to `~/.kube/config` (J4) | Check-mode safe; multi-env safe; restore can bind to an env | Operators must set `KUBECONFIG` or use make targets | S | second env |
| B7 | **Derived prune-guard set** (rule by kind and layer, plus an allow-list) | the hand-written `_PROTECTED` (J11) | New platform objects are protected by default | Occasionally needs an allow-list entry | S | every platform change |
| B8 | **Table-driven `schema/env.py`** (one dict per key: type, required, validator, consumer) | 726 lines of per-key imperative checks | Easier to add keys; consumer field makes "unused" visible | One-time rewrite; risk of behaviour drift (mitigated by the existing 526-line test file) | M | every new service config |
| B9 | **One Flux Kustomization per service** with health checks only on depended-on services (A P2) | the single `services` Kustomization | One failing service stops blocking all; per-service prune | More env files, unless B1's core list pattern is used | M | when the second service lands, not before |

Not endorsed: generating `envs/<env>/flux` from values (A P1) **now**. B1 removes most of the copying with no generator, so revisit only if a third env appears. Per-arch checksums (A P4) wait until an arm64 env exists. HA control plane (B F14's original direction) is deferred; both cross-checks agree.

---

## 4. Keep list

- The layout `infra/ platform/ services/ apps/ envs/<env>/{values,inventory,secrets,flux}` and the four-layer Flux chain.
- SOPS with `encrypted_regex: ^(data|stringData)$` (metadata stays reviewable), `tools/seal` and `_sops.py`'s safe-write approach (after J11's input validation).
- The env schema plus `envs/env-00` as the every-key example env.
- The guardrails: gitleaks secret scan, `core-names` with `OWNER_NAME`, the command guard, `digest-pin`, `manifest-lint`, `loadbalancer`, `pvc-backup`, `contract-md`, kustomize build plus kubeconform, prune-guard test, k3s and flux-operator role tests. Improve them per J6, J11 and J23; do not drop them.
- `tools/restore` with its rebuild hold: the design is right, and only the success criteria are wrong (J1, J2).
- Kyverno-generated per-PVC VolSync enrollment with `failurePolicy: Ignore` (J7 adds proof, not `Fail`).
- The etcd snapshot CronJob copying k3s snapshots into restic.
- `platform/testapp` as the ingress/DNS/TLS canary until a real health target replaces it.
- The `services/` and `apps/` placeholders until the first real resource lands (both cross-checks agree).
- Future-root entries in `_contract_md.EXPLICIT_COMPONENT_ROOTS` (they cost nothing and fire on arrival).
- The `storage` node role (cheap declared capability).
- Renovate custom managers with `# renovate:` comments, and pinned CLI versions in `tools/versions.env`.
- The admin gate using `github.repository_owner`, `.mailmap`, `OWNER_NAME`: per-owner by design and easy to replace.
- The node-disk guard rails that exist (root-disk refusal, double confirmation); extend them per J10.

---

## 5. Delete list

| Item | Evidence (reproduced) | Note |
|---|---|---|
| Renovate VolSync manager pattern `platform/backups/test/.+\.yaml` | `rg -n 'backups/test' renovate.json` finds the pattern; `ls platform/backups/test` → absent | B found it; A's cross-check confirmed it |
| `scan_*(REPO) == []` tree-rescan asserts in `test_digest_pin.py`, `test_loadbalancer.py`, `test_manifest_lint.py` | `ci/run-checks` runs the same scanners over the same tree | Delete only those asserts; keep the seeded cases (A's narrowing accepted) |
| The full eight-play order lock in `test_flux_operator.py:61-92` | `rulebooks/coding-agents.md` bans shape/order tests; the safety fact is one assertion | Replace, do not just delete |
| `bootstrap-secrets` generators for `grafana-admin` and the four `hermes-*` secrets, and their committed ciphertexts in `envs/env-01/secrets/` | `rg 'grafana-admin\|hermes-'` outside `*.sops.yaml` finds only `tools/bootstrap-secrets:290-320`, the secrets README and the guard corpus. No kustomization lists them (`envs/env-01/flux/platform-configs/kustomization.yaml` lists only `backups-restic` and `cert-manager-ca`) | **Judge decision against both auditors** (A: nothing to delete; B: keep future secrets). They are flexibility without a consumer, and the hermes generator embeds a plugin-specific password hash. Re-add each in the PR that deploys its consumer |
| `tools/verify/p0a`, `tools/verify/p0b`, `ci/tests/test_verify_p0b.py`, `PHASE=` in the Makefile | Replaced by B2 | Delete in the same PR that lands `make verify ENV=` |
| `roles/flux-operator/templates/flux-instance.yaml.j2`, the duplicate `flux_distribution_version`, and `test_git_flux_instance_matches_ansible_template` | Duplicate of `envs/<env>/flux/flux-instance.yaml` (J9) | Delete with B1 |
| `envs/env-01/README.md` one-time `volsync-test` cleanup paragraph and `platform/backups/CONTRACT.md:60` | History, not instructions | **Only after** the owner confirms `${backup_path}/volsync-test` is gone (A's cross-check is right that deleting it now hides the instruction) |
| Duplicate `SCAN_ROOTS`/`SKIP_ENV_PREFIXES` in `_pvc_backup.py:21-23` | Identical to `_manifests.py:9-10` | Import instead |
| Duplicate `local_path_data` and `etcd-snapshots` dir tasks in one of `k3s` or `node-disk` | `k3s/tasks/main.yml:37-49` vs `node-disk/tasks/main.yml:81-88` | Part of J3 |

---

## 6. Ready for other people?

**Adopter walk (on paper, amd64 mini-PC plus an arm64 SBC, AMD iGPU, 192.168.50.0/24, Pi-hole as router DNS).** Files opened to reach a planned first deploy: 14 (`README.md`, `docs/README.md`, `AGENTS.md`, `Makefile`, `envs/env-00/*` ×3, `envs/env-01/README.md`, `infra/ansible/README.md`, `tools/prep-node`, `tools/bootstrap-secrets`, `schema/env.py`, `platform/backups/CONTRACT.md`, `platform/cert-manager/CONTRACT.md`).

Stuck points, ranked with the smallest fix:

1. **`prep-node` crashes** (J5): fix the path.
2. **No single deploy page**; the README links to an unreachable plan repo (J16, J17): `docs/deploy.md`.
3. **arm64 node and AMD GPU fail late or silently** (J13): document the limits; the schema rejects them early.
4. **A new env needs hand-copied Flux files plus the core component list** (J9): B1.
5. **Required keys with no meaning to an adopter** (`build_agent: cursor`, `llm_provider_host`, `code_delivery`, hermes bots) (J14): make them optional or remove them.
6. **`make ci-verify` fails on `OWNER_NAME`** (J17): default it or document it.
7. **`site.yml --check` fails**, so no safe preview (J4).
8. **Backups are same-disk by default with no warning** (J8): a doc line and a prep-node warning.
9. **The restore drill isn't in the repo** (J7): `make drill`.

**Prerequisites outside the repo (by design):**
- the router or LAN DNS forwards the env's DNS suffix to the App DNS IP;
- Tailscale (or equivalent) for remote access, on the router;
- an age key kept offline;
- a GitHub (or Gitea) repo Flux can read, plus its token;
- amd64 nodes running Ubuntu Server;
- NVIDIA GPUs if any;
- a second disk or off-host target if hardware-loss recovery is wanted;
- `OWNER_NAME` as a repo variable for CI.

**What will hurt once services and apps land (forward-looking):**
- the single `services` Kustomization: one bad service blocks all (B9);
- the env-owned component list (J9);
- the shared backup credential, which every new namespace inherits (J8);
- the unknown-`${key}` hole, where new services add the most substitutions (J6a);
- probe and resource lint that cannot see chart values (J6e);
- the single backup schedule and cache size (J21);
- required-but-unused config keys that new services will guess at (J14);
- CONTRACTs with no fixed shape, which new components will copy (J16).

---

## 7. Rejected findings

- A F2 (part): "`AS_OF` never reaches restore": command-line make vars are exported; verified with a toy Makefile.
- A F26: `install-deps` lacks `apt-get update`: a stale VM apt cache, not a repo defect (A itself withdrew it).
- B F09 (part): split `image.repository`/`tag` evades digest-pin: refuted by seed (the HelmRelease image map was flagged).
- B F08 direction: "use `failurePolicy: Fail`": rejected; an outage would block PVC creation. Use the runtime proof (J7). B withdrew it.
- B F13 (part): "CA irrecoverable": the ciphertext is in Git; B corrected it itself.
- B F14 direction: build HA control plane now: no named near-term need; document the ceiling instead.
- B F16: DNS/ingress/CA boundaries "underspecified": taste beyond J16/J22; there's no concrete failure.
- B F05 (part): inventory removal should drain/delete nodes automatically: inferring deletion from inventory is more dangerous; kept as a documented operator step.
- A P1 now: generate the env flux tree: B1 solves it without a generator.
- A P4 now: per-arch checksums: no arm64 env yet.
- A F18 (part): `cluster.local` as a pin: a k8s constant, not a version; no realistic change.
- A delete-list "nothing tracked should be deleted": refuted (section 5).
- B delete-list placeholders now: they're the only objects in two Flux layers; delete with the first real resource.

---

## 8. Audit quality

| Hotspot | A (Grok 4.7) | B (GPT-5.6 Sol) |
|---|---|---|
| Backups | Partly: interrupt, quiesce kinds, ambient context, same-disk. **Missed** the snapshotless hold lift (the most severe) and shared creds | Partly: hold lift, non-atomic hold, shared creds, scaling. Missed interrupt/quiesce/context |
| `schema/env.py` | Partly: substitution keys not checked (F8), hermes/apps shallow (F14), disk path relation (F4) | Partly: global, unused, shallow config; ConfigMap exports host keys |
| Command-guard harness | Found (F25), by category | Found (F21), parser-clone point |
| Hand-synced prune guards | **Missed**: kept the list as a "rule" | Partly: CA lacks prune (F13), but did not ask for a derived list |
| Large test files | **Missed**: "no duplicate test should be deleted" | Found (F22): order locks, rescans, failed-play tolerance |
| READMEs as change logs | Partly: contracts describe absent work (F17) | Partly: the `volsync-test` paragraph |
| Reverted-work leftovers | **Missed** | Found the dead Renovate pattern |

**False positives:** A: `AS_OF` (F2 part), apt update (F26, withdrawn). B: the split-image digest bypass (F09 part), "CA irrecoverable" (corrected), and the over-strong `Fail`/HA directions (both withdrawn in its cross-check).

**Strengths:** A was more precise on file and line evidence and on the adopter path (F13, F20, F21, F24). B was stronger on lifecycle and failure semantics (F01, F02, F05, F12) and test-suite hygiene. Both cross-checks were honest and narrowed their own claims.

**Both missed** (found by the judge or the live baseline):
- the kubeconfig check-mode failure;
- local-path 0755 vs k3s 0700 (PVC exposure);
- `build_agent: cursor` required in core config;
- the core component list living in the env flux tree;
- the Flux double pin plus its sync test;
- the restic digest duplicated in `tools/restore` outside Renovate;
- `backup_path` lacking an absolute-path check;
- duplicate dir management in k3s and node-disk;
- the unused grafana/hermes secrets and their generators;
- phase-milestone tooling (`p0a`/`p0b`) and the private-plan links in product docs;
- the drill script living in a PR description;
- the derived-vs-hand-written prune guard set;
- the `ci/checks` heredoc boilerplate and `_tool_env` copies.

---

## 9. Proposed PR sequence

| # | PR | Findings | Size | Depends on | Render change? | Check-mode change? |
|---|---|---|---|---|---|---|
| 1 | **Restore fails closed**: no-snapshot is an error in `--all`; lift the hold last; `ENV=` and a context check; CronJob suspend; undo on interrupt | J1, J2 | M | — | No | No |
| 2 | **Node file modes + check-mode-safe kubeconfig**: local-path/backup dirs 0700 with one owner role; slurp/copy to `~/.kube/jarvis-<env>.yaml` | J3, J4, J24 (rerun) | S | — | No | **Yes**: check mode passes without `--skip-tags`; one fewer `changed` |
| 3 | **prep-node fix + adopter quick wins**: real env-schema call; `OWNER_NAME` default; tag examples | J5, J17 (part) | S | — | No | No |
| 4 | **CI holes closed**: `${key}` check, LB by identity, pvc-backup by name, non-mapping error, HelmChartConfig digest; shared scan roots; dead Renovate pattern; drop rescan asserts | J6 (a–d), J18 (part), J19 (part), delete-list rows 1–2 | M | — | No | No |
| 5 | **Fail-closed `make verify ENV=` + in-repo `make drill`**; delete p0a/p0b | J7, J16 (part) | M | 1, 2 (the kubeconfig path) | No | No |
| 6 | **Ansible preflight**: asserts before mutation, NotFound classification, chrony handler, opposite-unit guard | J12, J10 (k3s part), J13 (asserts) | S–M | 2 | No | Same count; earlier failure on bad hosts |
| 7 | **Disk and netplan guards** | J10 | M | 6 | No | No (env-01 has no data disk) |
| 8 | **Prune guards derived + CA annotated + seal input validation** (the owner re-seals the CA) | J11 | S–M | 4 | **Yes** (annotations) | No |
| 9 | **Core `platform/configs` + Ansible applies the env FluxInstance**; delete the template, the second pin and the sync test | J9, B1 | M | 8 (guards before the path move) | Path changes; **objects should be identical**, verify by sorted-object diff | flux-operator task changes; `changed=0` if content identical |
| 10 | **Env schema slimmed**: drop `build_agent`; optional unused keys; ConfigMap allow-list; `backup_path` checks; delete the grafana/hermes generators and ciphertext | J14, delete-list row 4 | M | 4 (the `${key}` check) | **Yes** (cluster-vars ConfigMap keys) | No |
| 11 | **Pins to one place**: restic digest, etcd schedule via cluster-vars, exact Python pins | J15 | S | 10 | Only if values change (they should not) | No |
| 12 | **Docs as build instructions**: CONTRACT shape plus a phase-code check, `docs/deploy.md`, hardware limits, backup same-disk warning, CA rotation note, private-plan links out of core | J16, J17, J13 (docs), J22, J8 (1) | M | 5, 9 | No | No |
| 13 | **Test hygiene**: replace the order lock, `support.py`, fixture requires success; ci/checks dispatcher | J19, J18 | S–M | 4 | No | No |
| 14 | **Command-guard corpus by category, deny-by-default assertion** | J23 | S | — | No | No |
| 15 | **Per-namespace backup credentials** (rest-server `--private-repos`, migration) | J8 (2), B4 | M–L | 1, 5 | **Yes** | No |
| 16 | **Controller health gating + chart-values lint** (`resources` required) | J20, J6(e) | S–M | 4 | **Yes** | No |
| 17 | **Off-host backup tier** | J8 (3), B3 | L | 15 | Yes | Possibly (new host vars) |

Deferred until a named trigger: per-service Flux Kustomizations (B9, at the second service); backup scale controls (J21, at the first large volume); a table-driven schema (B8, worth folding into PR 10 if it grows); per-arch k3s checksums (an arm64 env).
