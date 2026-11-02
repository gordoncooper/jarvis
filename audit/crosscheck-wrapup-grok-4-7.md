# Cross-check — wrap-up audit (Grok 4.7)

Target: `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1`, plus this branch's findings commit. Read-only on product code. Compared:

- `audit/findings-wrapup-grok-4-7.md` (this auditor, F1–F21)
- GPT-5.6 Sol findings (F01–F25, no F26) and its delete list
- Claude findings (F01–F39, T01–T20) and its delete list

Evidence below was re-read or re-run this round unless a line says the seed was not repeated. No SSH, no live kubectl, no SOPS decrypt, no age key. Command-guard notes stay by category. No bypass string.

## 1. Verdicts

### Shared issues

**Disk loss, server token, and what the drill proves.** GPT F01, Claude F03 and F04, Grok F1. **Confirmed.** Severity stays critical on the token gap. GPT's "false recovery" is a step past the contract: PVC drill behavior is real; the etcd half is a checksum of a restic copy, and the contract already says that is not disk-loss recovery.

Reproduced:

- `tools/drill` picks the newest file under the snapshot mount and runs `restic restore latest` into an `emptyDir`, then compares checksums (`tools/drill` around the etcd pod command). No `cluster-reset` and no token read.
- `platform/backups/CONTRACT.md` says the reset uses a file under `backup_path/etcd-snapshots`, does not use restic, and a rebuilt node needs the original k3s server token. Limits say backups cover mistakes and rollback, not disk loss.
- `infra/ansible/roles/k3s/tasks/token.yml` reads `/var/lib/rancher/k3s/server/node-token`. Claude's path `server/token` is wrong. The finding direction is right.
- `platform/backups/rest-server.yaml` is a root hostPath of the restic repos on the single backup node. `docs/deploy.md` says backups are not disk loss.

**Restic rotation.** GPT F03, Claude F02, Grok F2. **Confirmed.** High, not Claude's critical. Git history still holds the old ciphertext if the age key remains.

- `envs/env-01/secrets/README.md` says to rotate `backups-restic`, delete that file and run bootstrap again.
- `tools/bootstrap-secrets` `_restic_string_data()` mints a new `RESTIC_PASSWORD` and new server credentials. Bootstrap does not open existing repositories.

**prep-node mutates before the schema.** GPT F02, Grok F3. **Confirmed.** High.

- `tools/prep-node` `run()` installs keys, sudo, hostname, and packages, then `apply_env_edits` and `sync_age_key`, and only then `run_env_schema`.
- `docs/deploy.md` new-env step 2 is prep-node. The `.sops.yaml` rule is step 3, so a copy fails the later schema check after the host has changed. GPT's line range 35–38 is the same section.

**Flux sync URL and generated ConfigMap are second copies.** Claude F11, Grok F5 and F9, and the duplication half of GPT F09. **Confirmed.**

- `envs/env-00/flux/flux-instance.yaml` sync url is `https://github.com/example-owner/example-cluster`.
- `schema/env.py` `_validate_flux_env_paths` checks `envs/<name>/` path strings and returns immediately for the example env. It does not compare the FluxInstance url or ref to `flux_git_repo_url` / `flux_git_ref`.
- `schema/env.py` has no reference to `cluster-vars-configmap`. A stale generated ConfigMap is not a schema error.

**`apps_git_repo_url` is unused, and services/apps do not decrypt.** GPT F05, Claude F07 and F08, Grok F15. **Confirmed** as a fact. Severity medium, not GPT's high. Nothing consumes it yet. That is deferred wiring (question 24), not a broken current path.

- `envs/env-00/flux/kustomizations/apps.yaml` and `services.yaml` use `sourceRef` `flux-system` and paths `./apps` and `./services`. Neither has `decryption`.
- `platform-configs.yaml` is the one with `decryption.provider: sops`.
- Schema still requires hermes and apps values. No manifest reads them.

**GPU role is a PCI check and a label.** GPT F06, Claude F13, Grok F14. **Confirmed** facts. Severity medium. README Requirements already say NVIDIA only, and only on the `gpu` role. `infra/ansible/tasks/assert-gpu.yml` checks vendor `0x10de`. There is no device plugin, driver, or VRAM key. Do not treat a missing GPU stack as a defect in today's foundation. Document the limit where a new adopter looks (deploy), which Grok F14 asked for. GPT's high fits only if the repo claimed usable GPU capacity. It does not.

**No adopter upgrade or release intake.** GPT F07, Claude F21, Grok F13. **Confirmed.** Medium.

- `docs/deploy.md` "Dependency updates" is the Renovate sentence. k3s updates are not that path.
- `prune: disabled` occurs 25 times under yaml (Claude said about 23). The count is 25. The "do not prune these" set is large, and a Jarvis upgrade into a fork is hand-merge work.
- k3s tasks notify `Restart k3s` and also start the service in the same play (`infra/ansible/roles/k3s/tasks/main.yml`, `handlers/main.yml`). See Claude F39.

**Enrollment fails open.** GPT F12, Grok F11. **Confirmed.** Medium. `platform/backups/enrollment.yaml` sets `webhookConfiguration.failurePolicy: Ignore`. The contract states it. `generateExisting: true` can repair later. Data written in the gap is unprotected until then.

**Shared restic credentials, privileged movers, cluster-wide Secret verbs.** GPT F04 and F22, Claude F16, Grok F16. **Confirmed** facts. Medium for one trusted owner. High before any untrusted tenant or code-factory namespace. GPT's high is the future-tenant reading.

- `enrollment.yaml` copies `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, and `RESTIC_REST_PASSWORD` into every enrolled namespace and sets `volsync.backube/privileged-movers: "true"`.
- `rest-server.yaml` says no `--append-only` and no `--private-repos`, and runs as root on the hostPath.
- `platform/kyverno/rbac.yaml` `kyverno:backup-generate` allows get/list/watch/create/update/patch/delete on all Secrets.

**Future hermes/apps config and unused bootstrap secrets.** GPT F10, Claude F27, Grok F17 and F19. **Confirmed** facts. Do not delete them as the only fix. See the delete-list verdicts and question 24.

**CONTRACT Operations and Limits.** GPT F18, Grok F18, Claude section 4a. **Confirmed** that most contracts stop at the five required headings and that the procedures people need (restic rotation, token, first backup, upgrade order) sit elsewhere. GPT's low is light. Medium is closer, because the wrong restic sentence is the operator's only rotation doc. The per-contract "what it should say" lists are reasonable. They are not all equally urgent.

**Template has no secrets README, and the doc index points at env-01.** Claude F29, GPT F09 (that half), Grok F8. **Confirmed.** Grok's high is closer than Claude's low: `docs/deploy.md` sends the adopter to "that env's `secrets/README.md`", and `envs/env-00` has no such file. `docs/README.md` Env docs links only `envs/env-01`.

**Age key: one recipient, thin recovery, unsafe mismatch prompt.** GPT F16, Claude F01, Grok F6. **Partly confirmed.**

- Confirmed: `.sops.yaml` has one age recipient. There is no written `sops updatekeys` rotation. `tools/prep-node` `sync_age_key`, on a recipient mismatch, asks to set the recipient and delete that env's encrypted secrets, and on yes it deletes them.
- Refuted in part: Claude's "no offline procedure". `infra/ansible/README.md` Recovery says to back up the key file and, if the file is lost while `flux-system/sops-age` still exists, copy `age.agekey` back before deleting anything. That is short. It is not absent. GPT's "incomplete" is the right size. The delete prompt is the high part (Grok F6).

### GPT findings not covered above

**F08 local ci-verify is not portable. Confirmed.** Medium. This auditor's first-round `OWNER_NAME="Gordon Cooper" make ci-verify` on this non-systemd controller was `1 failed, 482 passed`: `test_chrony_template_notifies_restart_only_when_changed`, `Could not find the requested service chrony: host`. The same failure showed up in an env-00-only worktree. This round did not re-run the full suite. The product code is unchanged. Claude T15's `483 passed` was not reproduced here. Grok's first write-up treated this as the environment only. That was wrong. The handler proof calls `ansible.builtin.service` on the controller. GitHub's green run is a different host shape. GPT is right.

**F09 new-env path. Partly confirmed.** The missing template README, "first host" only, and manual Flux copies are real (see shared items). The "eight-host example" is wrong. `envs/env-00/inventory/hosts.yml` has seven hosts: `cp-1`, `wk-1` through `wk-4`, `st-1`, `bk-1`.

**F11 Kyverno versus its tested Kubernetes range. Confirmed** as a documented skew, not a known break. `platform/kyverno/CONTRACT.md` says upstream tests v1.19 on Kubernetes 1.33–1.35, the chart does not refuse 1.36, and the module builds against client-go v0.36. The pin is k3s `v1.36.5+k3s1`. Medium.

**F13 exactly one control plane. Partly confirmed.** The fact is true (`README.md` Requirements, schema singleton). It is a stated limit, not an accidental bug. Do not open an HA profile from this audit.

**F14 amd64-only. Partly confirmed.** Same. README requires amd64. `tools/install-deps` still has arm64 CLI hashes. That is a stated limit.

**F15 no continuous diagnosis. Confirmed** as a later gap. Traefik and testapp say Gatus is planned. `services/placeholder.yaml` is the observability stand-in. `make verify` is operator-invoked. Medium as a missing landing contract, not a broken current check. Do not build a stack as the audit fix.

**F17 tests that protect shape. Partly confirmed.** Low.

- `ci/tests/test_env_shapes.py` `test_gpu_mix_without_the_gpu_role_fails` and `test_secondary_disk_without_data_disk_fails` only raise `AssertionError` from `_assert_shape`. Confirmed. Safe to delete.
- `test_secret_scan_rejects_planted_age_key` (`test_flux_entry.py`) plants a key in a throwaway git repo and runs gitleaks. It is a second gitleaks invocation. It is not a no-op re-run of `ci/checks/secret-scan` on the product tree. Keep it.
- The cited base-os and flux-operator ranges include source-shape checks. This round did not re-score every assertion. Low value is plausible. Not a reason to delete the behavioral Ansible tests around them.

**F19 controller prerequisites. Confirmed.** Medium for apt and Python. Low for `make`.

- `tools/install-deps` `_try_apt_install` runs `apt-get install` with no `apt-get update` (Grok F21).
- Installed `ansible-core` metadata on this machine reports `Requires-Python: >=3.12`. Docs do not say that.
- `tools/prep-node` `_TOOLS` is `ssh`, `ssh-keygen`, `git`, `make`. No subprocess invokes `make`. `age-keygen` is called from `age_public_key` and is not in `_TOOLS`.

**F20 repository URLs accept credentials. Confirmed.** High is fair: `schema/CONTRACT.md` says the URL has no secret, and the check does not enforce that. Size S.

This round called `_validate_repo_url` for `flux_git_repo_url`:

- `https://user:pass@github.com/owner/repo` → `errors=[]`, owner `owner` (userinfo `user`)
- `https://github.com/owner/repo?token=abc` → `errors=[]`, owner `owner`
- `https://github.com/owner/repo#frag` → `errors=[]`, owner `owner`

`_validate_https_url` only requires scheme `https` and a non-empty netloc. `repo_owner_handle` only checks path depth.

**F21 Flux advances before VolSync is Ready. Confirmed** the gate. Severity medium, not high. The admission webhook already fails open, and `generateExisting` can catch up. The window is still real and the fix is small.

- `envs/env-00/flux/kustomizations/platform-controllers.yaml` `healthChecks` names HelmRelease `cert-manager` and `kyverno` only.
- `ci/tests/ansible_role_proof.py` says platform-controllers waits for those two and no other object.
- `platform/kustomization.yaml` includes `backups/controller`. That HelmRelease is named `volsync`.
- `platform-configs` `dependsOn` is only `platform-controllers`.

**F23 existing Helm binary is trusted. Confirmed.** Medium. `infra/ansible/roles/flux-operator/tasks/helm_cli.yml` stats `helm_binary_path` and installs the pinned archive only `when: not flux_operator_helm_stat.stat.exists`. No version or digest check of an existing binary. Same fact as Claude F24.

**F24 kubectl has no process timeout. Confirmed.** Medium. `tools/lib/_env.py` `kubectl()` uses `subprocess.run` with no `timeout=`. Some callers pass kubectl `--timeout=10s` (Flux list in `tools/verify/run`). Secret reads and many other calls do not. A stuck process never returns to an outer poll deadline.

**F25 verify fetches the restic Secret body. Confirmed** the fact. Severity low, not medium. The kubeconfig holder can already read the secret. The extra exposure is the full object in the controller process.

- `tools/verify/run` `_check_secret` runs `kubectl get secret backups-restic -n platform-backups -o json` and only checks that `data` is a non-empty mapping.

### Claude findings not covered above

**F05 restore binder has no nodeSelector. Partly confirmed.** `tools/restore` `_binder_body` sets restart policy, one container, and the PVC volume. No `nodeSelector`. `platform/backups/CONTRACT.md` already says that pod "has no nodeSelector". So this is written behavior, not a surprise. On multi-node local-path it can bind an unbound claim on whichever node the pod lands. Medium as a limit to state next to restore, not high as a contradiction.

**F06 fresh verify and drill fail until the first etcd copy. Confirmed.** High is fair against the deploy sentence. This auditor missed it.

- `tools/verify/run` fails with `etcd snapshot copy has no successful scheduled run` unless `status.lastSuccessfulTime` is a string, then checks it is recent.
- `platform/backups/etcd-snapshots.yaml` schedule is `5 */12 * * *`. The k3s default cron is `0 */12 * * *` (`infra/ansible/roles/k3s/defaults/main.yml`).
- `docs/deploy.md` says `make verify` produces PASS lines and exits 0. After install, that is false until the first successful copy, which can be hours.

**F09 public Git is required. Partly confirmed.** `schema/CONTRACT.md` already says `flux_git_repo_url` is a public Git remote. It is a stated limit. Deploy does not repeat it in the new-env steps. Low doc gap, not a high defect.

**F10 unattended-upgrades origins are empty and the upgrade timer is stopped. Partly confirmed.** The template says the package is present but inactive and that upgrades are run manually (`infra/ansible/roles/base-os/templates/50unattended-upgrades.j2`, tasks disable `apt-daily-upgrade.timer`). That is deliberate. The missing patch runbook is the real gap, shared with the upgrade findings. Do not treat the empty origins as a bug.

**F12 prep-node overwrites env-wide `ssh_user`. Confirmed** behavior. `apply_env_edits` writes `ssh_user` in `cluster-vars.yaml` from the node just prepared. The schema has one `ssh_user`. A later node with a different login user replaces it. Low, because one user is the model. Worth one sentence in the prep-node doc.

**F14 role labels are prefixed with the DNS suffix. Partly confirmed.** The prefix is how node roles are separated. It is intentional. Outside charts must use it. Document it. Do not change it from this audit.

**F15 the whole age key file is copied into `flux-system/sops-age`. Partly confirmed.** `sops_age.yml` uses `lookup('file', sops_age_key_file)` and the Secret template stores that text as `age.agekey`. Flux decryption needs the key. Copying the file is the design. Low only if a file might contain extra keys. Not a finding on its own. The single recipient and the delete prompt are the findings.

**F17 internal CA has no nameConstraints. Confirmed** as hardening, low. `tools/bootstrap-secrets` `-addext` sets `basicConstraints`, `keyUsage`, and `subjectKeyIdentifier`. No `nameConstraints`. The CA key is already the trust root.

**F18 agents receive the server node token. Partly confirmed.** `token.yml` reads `node-token` and `agent-config.yaml.j2` sets `token` from it. That is k3s's join token. Not a separate defect.

**F19 only Traefik and App DNS LoadBalancers, Ingress-only. Partly confirmed.** Stated shape of this foundation. Put it in the Traefik and App DNS Limits. Not a bug.

**F20 chrony is NTS-only. Confirmed** as strictness, low. `chrony.conf.j2` is `pool {{ pool }} iburst maxsources 4 nts`. Recovery text says NTS only. Keep it and say the pools must speak NTS. Do not drop NTS from this audit.

**F22 thirteen Renovate regex managers. Confirmed** the count (`"customType": "regex"` appears 13 times in `renovate.json`). Replacing them with the native Flux manager is a judgment, not a defect. Low.

**F23 prep-node should be rewritten as Ansible. Partly confirmed.** The file is about a thousand lines, and the schema-after-mutation bug is real (GPT F02). A rewrite is not the required fix. Python runs before the node has Ansible. Fix the order. Leave the rewrite alone.

**F24 Helm on the node, install only if missing, arm64 branch. Confirmed** with GPT F23. The arm64 checksum branch is dead on the supported amd64 nodes. Low for the dead branch. Medium for the unpinned existing binary.

**F25 no `make site` target. Confirmed.** Low. Deploy points at the ansible README command. A target would help. Not a correctness bug.

**F26 verify does not check node Ready or pod health. Confirmed.** `_check_nodes` compares inventory names, role labels, and the edge LoadBalancer label. It does not read Ready. The check list is Flux, the restic Secret's presence, those labels, backup freshness, and the testapp fetch. Medium as a diagnosis gap.

**F28 plan codes in non-markdown. Partly confirmed.** `ci/checks/lib/_plan_codes.py` scans Markdown, README names, and `docs/`. It does not scan code. Grok F20 only flagged placeholders and the doc index. Claude is right that the scanner's scope is narrower than the cleanliness rule. This round did not re-count "~41 files". Low.

**F30 cruft list. Partly confirmed.**

- `infra/inventory/roles.yml/groups.yml` says `site.yml` does not target `data_disk` and that node-disk reads the host var. The group is still how the flag becomes a group. Schema CONTRACT describes it. Do not delete it as unused.
- `storage` is a label only (`schema/CONTRACT.md`). Confirmed. Deleting the role from the allowed set is a product choice, not dead code removal.
- Placeholders: see the delete list. Do not delete the objects.
- `EXPLICIT_COMPONENT_ROOTS` includes `docs/build-agent`, hermes deploy paths, and `services/observability`. Those directories are the deferred homes. The check reports roots that exist and lack a contract, so missing names do not fail CI. Keep the names until a roadmap replaces them.
- Arm64 Helm metadata: dead on amd64. Low. See F24.
- `tools/restore` local helpers versus `tools/lib/_env.py`: both exist (`reason`, `not_found`). Duplication is real and small. Optional.

**F31 drill versus the five-minute copy offset. Confirmed.** Low. k3s cron `0 */12 * * *`, copy cron `5 */12 * * *`. The drill checksums the newest local file against `restic restore latest` of that name. Between minute 0 and minute 5 the newest local file may not be in the latest restic snapshot. A five-minute flake window, twice a day. Not high.

**F32 system Python versus `.venv`. Partly confirmed, not a defect.** The Makefile runs `python3 tools/verify/run` (and drill, restore, bootstrap, seal) and uses `.venv` for pip and Ansible. `install-deps` apt-installs PyYAML. The split is intentional.

**F33 `make deps` writes `~/.local/bin`. Refuted** as a defect. `docs/deploy.md` says the pinned CLIs go there. Replacing an older pin is the install.

**F34 bootstrap ignores `age_key_file`. Confirmed.** Medium. `envs/env-01/secrets/README.md` says `tools/bootstrap-secrets` does not read `age_key_file` and that `SOPS_AGE_KEY_FILE` must be set. `tools/lib/_sops.py` `encrypt_secret_to_file` calls `sops encrypt` with no key path. Sops then uses its own default. prep-node records `age_key_file` and does not export it for the later make target.

**F35 `dns_suffix` is not checked as a DNS name. Confirmed.** `schema/env.py` only requires a non-empty string for `dns_suffix` (the loop over `jarvis_env`, `dns_suffix`, `time_zone`, and the other scalars). `_validate_bare_hostname` is applied to `llm_provider_host`, not `dns_suffix`. `_HOSTNAME` does not allow `_`, but it is not used here, so a value such as `Home_Lab` passes that check. The suffix is interpolated into role labels and DNS. Medium.

**F36 command guard, categories only. Partly confirmed.** No command is written here.

- Secret-read deny entries in `.cursor/cli.json` and `.claude/settings.json` are two case spellings of the same substring. The in-repo model matches with `fnmatch.fnmatchcase` (`ci/checks/lib/_cli_permissions.py` `match_glob`). The classifier lowercases before it looks for that substring (`_is_kubectl_secret`). A mixed-case kind is in the must-block class and is not covered by those two entries. This is Grok F7. GPT's "no real safety bug" is wrong relative to that model. The guard is for build agents, not the cluster's trust boundary.
- Cluster-mutating `make` targets are not named in the deny list. The list names kubectl, helm, flux, ssh, sops, and age-key reads.
- Kubeconfig reads are not in the read-deny entries. Those entries are the age-key paths.

**F37 verify's cron walker. Confirmed.** Low. `tools/verify/run` loops `for _ in range(2 * 366 * 24 * 60)`. `croniter` or the CronJob status would be smaller. The walker is not wrong by itself.

**F38 Flux Operator re-run skips CRD and rollout waits. Confirmed.** Medium. `operator.yml` puts "Wait for FluxInstance CRD" and "Wait for Flux Operator deployment rollout" inside `when: not (flux_operator_deployed | bool)`. A Deployment that exists but is not Ready skips both waits. This auditor missed it.

**F39 first k3s start is followed by a handler restart. Confirmed.** Low. Config, binary, and unit tasks notify `Restart k3s`. The same play then enables and starts the service. Handlers run at the end of the play, so a fresh install starts and then restarts. Agents included.

### Claude test findings (T01–T20)

Seeded pytest counts were not repeated this round. Verdicts are from the cited source.

| Id | Verdict | Why |
| --- | --- | --- |
| T01 | confirmed | `_is_enrollment_policy` returns true when any rule generates a `ReplicationSource`. It does not read `match` or `exclude`. A policy that no longer matches PVCs still satisfies the check. High is fair for that hole. Size S. |
| T02 | confirmed | `ci/tests/test_verify.py` `_run` stubs fetch and issuer. The issuer stub returns success. A grep of that file finds no `verify_return_error`. Removing the chain or body check would not be forced red by a test this file contains. Medium is closer than high: the standing tool can still fail in production. The CI hole is real. |
| T03 | partly | The plaintext assertion loops `cert-manager-ca` and `backups-restic` only, and only `stringData` values start with `ENC[`. Confirmed. High is strong: `.sops.yaml` encrypts `data` and `stringData` by design, and a label is not one of those fields. Medium. |
| T04 | partly | `verify_sha256` is called from `_install_binary` before the binary is copied. The age tarball is extracted before that binary hash. This round did not delete the calls and re-run pytest. The untested-guard claim matches the shape of `test_install_deps.py` being small. Medium. |
| T05 | confirmed | `ci/checks/lib/_manifests.py` says charts are not rendered and walks raw YAML. A kustomize patch or HelmRelease postRenderer is outside that walk. Medium. |
| T06 | partly | `.github/workflows/ci.yml` checkout step sets no `fetch-depth`. Checkout's default is a depth of 1. `secret-scan` runs gitleaks over the checkout. This round did not run a PR-shaped gitleaks. Medium, on the documented default. |
| T07 | partly | Several platform tests assert exact YAML values. This round did not re-seed them. The rulebook prefers behavior over shape. Medium as a maintenance cost. Do not delete the kustomize-build and Ansible localhost tests in the same sweep. |
| T08 | partly | `_cli_permissions.py` is a large model of one client and says it does not prove the client. `.claude/settings.json` has no check under `ci/`. Confirmed. Medium as cost. Do not delete it: it is what makes Grok F7 visible. A thinner matcher is the fix. |
| T09 | partly | The cited guards exist in restore, drill, seal, prep-node, and preflight. This round did not flip them and re-run pytest. Treat the "untested safety branch" claim as plausible, not re-proven. Medium if a later PR samples one guard each. |
| T10 | confirmed | `ci/tests/test_restore.py` reads `capsys.readouterr()` and then asserts `"HOLD kept" not in` a second read. The second read is empty, so the assertion cannot fail. Low. |
| T11 | partly | `load_documents` skips a scan root that is not a directory and skips non-UTF-8. `_component_root_present` returns false when the directory is missing, and `missing_contract_roots` reports roots that exist without a contract. A missing listed root does not fail. Confirmed. Low. |
| T12 | confirmed | `ci/run-checks` uses `set -e`, so the first failing check hides the rest. The wrappers are thin and repeated. Low. |
| T13 | confirmed | `_is_scanned` allows README names and paths under infra, platform, schema, services, tools, ci, docs, rulebooks, and `.github/`. It skips `Makefile`, `renovate.json`, `AGENTS.md`, `CLAUDE.md`, `.cursor/`, and `.claude/`. `.github/` is scanned, except `CODEOWNERS`. Low. |
| T14 | confirmed | `bootstrap-secrets` writes `ca.crt` when `spec.filename == CA_SECRET_FILENAME and not ca_root.exists()`. Regenerating the CA secret alone leaves the old `ca.crt`. Low. The rotation doc says to delete both. The tool does not enforce it. |
| T15 | partly | The missing local env-00-only switch is real: `ci/ci-local-verify` runs the full tree. The `483 passed` result is **refuted** on this controller. See GPT F08. `require_cli` skipping missing CLIs locally is a separate, smaller gap. |
| T16 | partly | `ci/tests/test_k3s.py` renders `config.yaml.j2` and `agent-config.yaml.j2` and loads `tasks/main.yml` as YAML. It does not name `k3s.service.j2` (no match in `ci/tests`). So the unit template is untested, and `main.yml` is not executed. "Never runs and the unit template is never rendered" is right for the unit. It is too broad for the server and agent config templates. Medium, not high. |
| T17 | partly | The overwrite refusal is in `sops_age.yml` and the fail message says the secret may be the only copy of the age key. `test_sops_age_tasks_use_no_log` looks for the string `age.agekey` in task YAML. That is not a rendered-Secret test and not a "different key refuses" test. This round did not delete the assert and re-run. Medium. |
| T18 | partly | The cited tasks exist (apt timer, node-disk path checks, `when: 'gpu' in group_names`). This round did not remove them and re-run. Medium if true. Not re-proven. |
| T19 | partly | `test_flux_operator.py` is large and includes play-order and README-tag helpers (`test_play_before_the_fact_assert_play_fails` and the wrapped-tag tests are the kind named). Behavioral stub tests in the same file are worth keeping. Medium as a trim, not a reason to delete the file. |
| T20 | partly | `test_flux_entry.py` runs kubeconform. Four CRD schemas are vendored under `ci/tests/fixtures/schemas/`. This round did not prove a network fetch. Low, and only if the test locations are remote. |

### Delete lists

**GPT**

| Item | Verdict |
| --- | --- |
| `apps/placeholder.yaml`, `services/placeholder.yaml` | **Partly.** They are the only objects in those trees. Deleting them without a replacement makes the Flux kustomizations empty. Phase words in the comments can go. The objects stay, or become `resources: []` in the same PR that changes the kustomization contract. |
| Grafana/Hermes specs in `bootstrap-secrets` until a consumer | **Partly.** No Flux path applies them. Confirmed. Deleting them from `env-01` as audit cleanup discards the owner's already generated secrets. Stop generating them for a new env, or document them. Do not delete the env-01 ciphertext here. |
| Mandatory `hermes.yaml` / `apps.yaml` validation | **Partly.** Nothing outside schema, envs, and tests reads them. Confirmed. Removing the requirement with no other home loses question 24. Move the contract to a roadmap and then stop requiring the files. Do not delete first. |
| Planned rows in `docs/code-delivery/CONTRACT.md` | **Partly.** Presenting planned rows as Offers is misleading. Relabel them as not built. Do not drop the list. |
| `test_flux_entry.py` planted gitleaks test | **Refuted** as a delete. It is a planted-key proof, not a duplicate scan of the product tree. |
| `test_env_shapes.py` helper self-tests | **Confirmed.** They only prove `_assert_shape` raises. Schema tests cover role shape. Safe to delete. |
| YAML and tag parser assertions in the cited base-os and flux-operator ranges | **Partly.** Some are shape checks. Delete only the ones that do not execute a play. This round did not mark line-by-line. |

GPT also says do not delete the VolumeSnapshot CRD or hand-edit generated ConfigMaps. **Confirmed.** Leave both.

**Claude**

| Item | Verdict |
| --- | --- |
| `hermes.yaml`, `apps.yaml`, and their validators | Same as GPT. **Partly.** Not until a roadmap holds the contract. |
| `build_agent`, `code_delivery`, `llm_provider_host`, `apps_git_repo_url`, cross-env owner check | **Partly.** Unused by manifests. The cross-env owner rule is doing work: a second env in a clone that still has env-01 must share that GitHub owner (Grok F4). That rule is harsh. It is not unused. Relax it or document it. Do not delete it in silence. |
| Grafana/Hermes bootstrap specs | Same as GPT. **Partly.** |
| `data_disk` group and `roles.yml/groups.yml` | **Refuted.** The file says site.yml does not target the group. Node-disk still uses the host var, and the group is the inventory construction the schema describes. |
| `storage` role | **Partly.** It is a label with no play. Removing it from the allowed set is a product cut, not dead-code deletion. |
| Placeholders | **Refuted** as a straight delete. See GPT. |
| Dead `EXPLICIT_COMPONENT_ROOTS` entries | **Partly.** The directories are absent, so CI does not fail. The names are the deferred homes. Replace with a roadmap before deleting the tuple entries. |
| `helm_cli.yml` and arm64 Helm defaults | **Refuted** as "delete the role". The unpinned existing binary should be checked (GPT F23). The arm64 branch can go while the product is amd64-only. The bootstrap still needs a Helm CLI today. |
| `tools/restore` `_reason` / `_not_found` | **Confirmed** as optional deduplication. Small. |
| 13 Renovate regex managers | **Refuted** as a delete-now. They are what bumps the pins. Replacing them is a separate change with a test that versions still move. |
| verify cron helpers, if croniter is adopted | **Partly.** Only delete them in the PR that switches the clock. |
| Snapshot tests (T07) and dead assertions (T10) | **Partly** for the snapshot set. **Confirmed** for the dead `HOLD kept` assertion. |
| Plan codes in code comments | **Partly.** The scanner does not see them. Stripping comments is fine. Do not pretend the check already enforces it. |

## 2. This auditor's file, revisited

Confirmed by the others, and still right: F1 (disk and token), F2 (restic rotation), F3 (prep-node order), F4 (cross-env owner, they treat it as too strict rather than unused), F5 and F9 (Flux URL and stale ConfigMap), F6 (delete-on-mismatch; they underweighted the prompt and overweighted "no procedure"), F8 (missing secrets README), F11 (fail-open enrollment), F13 (no upgrade path), F15 (no service secret path; they rate the apps URL higher than I would), F16 (Secret delete plus the shared restic facts), F17 and F19 (unused required config and unused secrets), F18 (contract ops and limits).

Contradicted:

- F7, the mixed-case Secret-read gap. GPT said the command guard had no real bug. The in-repo matcher is case-sensitive and the deny entries are two case spellings. F7 stands. Claude F36 is the same category, plus make targets and kubeconfig reads. No bypass string in either file from this round.
- The chrony failure. The first findings file called it the controller, not the product. GPT F08 is right. It is a portability bug. Claude's green 483 was not reproduced here.

Missed, and now accepted:

- Claude F06, verify stays red until the first etcd copy. Real, and it contradicts `docs/deploy.md`.
- Claude F38, operator re-run skips the waits.
- GPT F20, URL userinfo, query, and fragment.
- GPT F21, VolSync is not in the controller health gate. Medium, not high.
- GPT F23, existing Helm is not checked.
- GPT F24, no kubectl process timeout.
- Claude F31, the five-minute etcd copy race. Low.
- Claude F34, bootstrap does not read `age_key_file`.
- Claude F35, `dns_suffix` is only a non-empty string.
- Claude F28, plan-code scan skips code. F20 was too narrow.

Still mine, and not refuted: F10, the bare `k3s server --cluster-reset` in `infra/ansible/README.md` Recovery, next to a different procedure that passes `--cluster-reset-restore-path`. F12, verify treats a fresh timestamp as health and does not compare snapshot bytes. The others' drill and first-run findings are adjacent, not the same check.

Severity I would not move: restic rotation stays high. Disk and token stay critical. Single control plane and amd64 stay stated limits, not defects. Shared restic stays medium until an untrusted tenant exists.

## 3. Answers by question, where the three differ

1. GPT is harder on backup trust and calls the drill false recovery. The PVC drill is real. The etcd half is not disaster recovery, and the contract already says so. The token hole is still critical. Claude's "core path is sound, disk loss is the gap" is the closer summary.
2. Claude would replace prep-node, the Helm CLI, the Renovate regexes, and the cron walker. Most of that glue is doing a job. The Flux URL duplicate and the schema-after-SSH order are the glue to fix. GPT and this auditor are closer.
3. GPT and Claude would delete the hermes schema and the snapshot tests. Delete the helper self-tests and the dead assertion. Keep the hermes contract until a roadmap holds it. This auditor is closer on the schema. They are closer on the low-value tests.
4. Claude adds NTS, the label prefix, the etcd race, and the all-node k3s restart. Those are real and smaller than the Flux URL copy and the token. Take the union, with Claude's extras at low or medium.
5. One control plane and amd64 are requirements, not accidents. NTS and public Git are stated and should be repeated in deploy. The too-strict part that matters is required hermes, apps, and `build_agent` before a consumer. This auditor is closer on HA and arm64. Claude is closer on the unused required keys.
6. The shared gap list is right: off-host backup, the token, a secrets path, upgrades, and no GPU runtime yet. OS patching is a missing procedure. The inert unattended-upgrades policy is deliberate.
7. Do not delete placeholders or the VolumeSnapshot CRD. Unused secret generation can stop for a new env. The `data_disk` group is not cruft. Shape self-tests can go.
8. Claude is closer on plan codes in code. All three agree owner identity stays in env-01, `.mailmap`, and `OWNER_NAME`.
9. "Not yet" was too strong. A careful single-node adopter can finish. They will get stuck on the missing secrets README, the FluxInstance url, and a verify that stays red for up to twelve hours. Claude's paper walk is closer.
10. The late schema check is the main hole. Add GPT's URL credentials and Claude's `dns_suffix`, `ssh_user`, and `age_key_file`. All four are real.
11. Single node is supported. Mixed GPU means labels and a PCI vendor check, not allocatable GPUs. Medium, not GPT's high. HA is the documented one-control-plane limit.
12. Take the union: restic rotation, the token, Flux operator before distribution, the first etcd copy, `age_key_file` versus `SOPS_AGE_KEY_FILE`, and how to patch a node.
13. The PVC path is a real restore. Disk death of the control plane is not, because the token is not in the restic copy and the drill never resets etcd. Call that a hole in the stated limit, not a drill that lies about PVCs.
14. No release intake (GPT) plus all-node restart and no patch procedure (Claude). Both.
15. Restic rotation is unsafe. CA rotation is written. Age recovery is short, not missing. GPT is closer than Claude on the age procedure.
16. verify names its failures and still misses node Ready, pod health, and a hung kubectl. Continuous Gatus is later work. Take GPT F24 and Claude F26 together.
17. The product issues are the shared restic credential and the URL check that accepts a secret. verify's full Secret JSON is low. The command-guard case gap is real and is build tooling. GPT's "no cli bug" is the line to drop. Claude's full-file age copy and the k3s join token are design.
18. Fill T01, a negative verify cert and body test, the VolSync health gate, the Helm pin, and a hermetic chrony handler. A disposable cluster reset is out of scope for CI until the token story exists.
19. Cut the env-shape helper tests and the dead capsys assertion. Keep the planted-key gitleaks test and the cli-permissions check. The 900-line matcher can get thinner without going away.
20. On this controller the suite is 482 passed and 1 failed, full tree and env-00-only. Claude's 483 was not reproduced. GitHub can still be green. Many tests execute fakes. A real slice only reads YAML.
21. testapp is a good template. There is still no secrets path and no per-env service switch. Not ready. All three land here. GPT's high on the unused apps URL is early.
22. Do not block the next areas on HA or arm64. Do constrain them on secrets, shared backup credentials, and the absence of a GPU runtime. The DNS-suffix label prefix is a contract an outside chart must follow, not a bug.
23. Every next area needs a secrets path and the off-host backup limit to stop being permanent. A namespace baseline and Gatus are the next platform, not this audit's patch.
24. Keep the deferred list. Claude's single roadmap is the right home. Then stop requiring hermes and apps files. Deleting the schema first loses the list. GPT's "remove the stubs" and "do not lose the commitments" have to be done in that order.

## 4. Suggested fixes

Direction only. One PR per row is enough. Do not mix the roadmap move with the backup trust change.

**Token and disk loss (GPT F01, Claude F03, F04, Grok F1).** Either back up `node-token` with the etcd snapshot to a target that is not the control-plane root disk, and say so in `platform/backups/CONTRACT.md` Operations, or leave the product logical-loss-only and say that in `docs/deploy.md` and the drill output in the same words as the Limits. Files: `platform/backups/CONTRACT.md`, `docs/deploy.md`, `tools/drill` (wording only, unless a token copy is added), `infra/ansible/roles/k3s/tasks/token.yml` if the copy is built. Tests: a drill unit test that the etcd path does not claim a cluster reset; if a token copy is added, a test that the manifest or script names `node-token` and not a guessed path. Done: `pytest` on the drill tests, and `rg` shows the contract, deploy, and drill using the same sentence. Offline only.

**Restic rotation (GPT F03, Claude F02, Grok F2).** Replace the delete-and-bootstrap sentence. Document rest-server password rotation separately from `restic key add`, and point at git history as the recovery for a password already lost. Files: a template `secrets/README` (see below), `platform/backups/CONTRACT.md`, `envs/env-01/secrets/README.md`. Tests: a doc or contract test that the rotate sentence does not say "delete that file and run the command again" as the repository-key procedure. Done: that test, offline.

**prep-node order (GPT F02, Grok F3).** Run `validate_env_tree` before SSH, then again after the edits. A draft mode may ignore the hostname and recipient that prep-node is about to write. Do not rewrite prep-node as Ansible in this PR. Files: `tools/prep-node`, `docs/deploy.md`. Tests: a prep-node test with an invalid unrelated value fails before the session opens. Done: `pytest ci/tests/test_prep_node.py`.

**Flux URL and ConfigMap drift (Claude F11, Grok F5, F9).** Compare FluxInstance `spec.sync.url` and `ref` to the cluster-vars keys for every non-example env, and compare the generated ConfigMap bytes to `render_document`. Files: `schema/env.py`, `ci/tests/test_env_schema.py`. Tests: url left as `example-owner` fails; a one-line values change without re-render fails. Done: those two tests.

**Secrets README in the template (Claude F29, GPT F09, Grok F8, Claude F34).** Add `envs/env-00/secrets/README.md` that describes bootstrap, `SOPS_AGE_KEY_FILE` versus `age_key_file`, and the corrected restic sentence. Point `docs/deploy.md` and `docs/README.md` at the template, and keep env-01 as the owner's copy. Files: those three. Tests: env-schema or a tiny path test that env-00 has the README deploy names. Done: the test, and a grep that deploy does not say only env-01.

**Cross-env owner (Grok F4, Claude F27).** Document that every non-example env in one clone shares one GitHub owner, or exempt `env-01` when the task is a fresh adopter clone. Files: `schema/env.py`, `schema/CONTRACT.md`, `docs/deploy.md`. Tests: the current two-owner failure stays or is narrowed, with a test for the chosen rule. Done: `pytest` on env schema.

**Age mismatch prompt (Grok F6, GPT F16).** On mismatch, refuse to delete secrets unless a separate flag is passed. Print the README recovery (copy `age.agekey` back) first. A two-recipient rotation doc can be a follow-up, not this PR. Files: `tools/prep-node`, `infra/ansible/README.md`. Tests: existing mismatch test expects no delete on the default path. Done: `pytest ci/tests/test_prep_node.py`.

**URL secrets (GPT F20).** Reject userinfo, query, fragment, and a non-default port in `_validate_repo_url`. Files: `schema/env.py`, `ci/tests/test_env_schema.py`. Tests: the three URLs from section 1 fail; a normal `https://github.com/owner/repo` still passes. Done: that pytest.

**`dns_suffix` (Claude F35).** Validate it with the hostname rule, or a narrower DNS-label rule, and reject `_`. Files: `schema/env.py` and its tests. Done: `Home_Lab` fails and `bench.lan` passes.

**VolSync health gate (GPT F21).** Add HelmRelease `volsync` in `flux-system` (the release object's namespace; the chart target namespace is `volsync`) to `healthChecks` in both env flux files, and update `PLATFORM_CONTROLLER_HEALTH_CHECKS`. Files: `envs/env-00/flux/kustomizations/platform-controllers.yaml`, `envs/env-01/...` the same, `ci/tests/ansible_role_proof.py` or the flux-entry test. Tests: the health-check list includes volsync. Done: `pytest` on the flux-entry test.

**Helm pin (GPT F23, Claude F24).** If the binary exists, check its version against `helm_version` and replace it on mismatch. Drop the arm64 branch only in a separate tiny PR if desired. Files: `helm_cli.yml`, flux-operator tests. Tests: a stubbed existing binary with the wrong version is replaced or refused. Done: the role proof.

**kubectl timeout (GPT F24).** Pass `timeout=` on `subprocess.run` in `tools/lib/_env.py`, and `--request-timeout` on the argv. Map timeout to the tool's existing error type. Files: `_env.py`, drill, restore, verify tests that stub kubectl. Tests: a fake that sleeps past the limit fails the call. Done: those pytest files.

**verify Secret body (GPT F25).** Ask kubectl for key names only (`jsonpath` of `data` keys) and assert the expected key set is present. Files: `tools/verify/run`, `ci/tests/test_verify.py`. Tests: a secret with no data keys fails; a secret with the restic keys and no need to echo values passes. Done: `pytest ci/tests/test_verify.py`.

**First-run verify (Claude F06).** Document that verify and drill stay red until the first `etcd-snapshots` success, and print that sentence from verify when `lastSuccessfulTime` is missing. Optional later: a one-shot Job. Files: `docs/deploy.md`, `platform/backups/CONTRACT.md`, `tools/verify/run`. Tests: missing `lastSuccessfulTime` mentions the first schedule. Done: verify unit test.

**Enrollment fail-open and policy match (GPT F12, Grok F11, T01).** Document the Ignore window in Kyverno Limits. Change the pvc-backup check to require the match kind `PersistentVolumeClaim` and the two excludes. Do not flip to Fail in the same PR unless a break-glass is written. Files: `platform/kyverno/CONTRACT.md` or backups CONTRACT, `ci/checks/lib/_pvc_backup.py`, `ci/tests/test_backup_enrollment.py`. Tests: a policy whose match is `ConfigMap` fails the check. Done: `pytest` those tests and `ci/checks/pvc-backup` on the tree.

**Shared restic trust (GPT F04, F22, Claude F16, Grok F16).** Document the boundary now: one credential, privileged movers, root rest-server, Secret delete on the aggregated role. A later PR can split credentials before untrusted tenants. Do not rewrite enrollment in the doc PR. Files: `platform/backups/CONTRACT.md`, `platform/kyverno/CONTRACT.md`. Tests: contract-md still passes. Done: the new sentences are in those two files and nowhere else is required.

**Upgrade, patches, and the extra k3s restart (GPT F07, Claude F10, F21, F39, Grok F13).** Add `docs/upgrade.md`: k3s pin and sha, Flux operator before distribution, chart PRs, and "patches are manual; unattended-upgrades is inert on purpose". Optionally flush handlers before the first k3s start, or drop the notify when the start task changed the service. Files: `docs/upgrade.md`, `docs/deploy.md`, `docs/README.md`, maybe `roles/k3s/tasks/main.yml`. Tests: a markdown link check if one exists; a k3s unit test if the notify changes. Done: deploy links the new doc.

**Bare cluster-reset wording (Grok F10).** In the ansible README, label the no-path reset as member-URL recovery that discards etcd, and point snapshot restore at the contract path. Files: `infra/ansible/README.md`. Tests: none beyond reading the two commands side by side. Done: the no-path command is in a paragraph that says it does not restore a snapshot.

**Operator waits (Claude F38).** Move the two wait tasks out of the "when absent" block so every run waits. Files: `operator.yml`, `ci/tests/test_flux_operator.py`. Tests: a play where the Deployment exists still contains the wait tasks after the block. Done: that pytest.

**Etcd copy race (Claude F31).** Have the drill compare a file that the latest restic snapshot lists, or document "do not drill in the five minutes after the k3s snapshot". Files: `tools/drill` or the backups contract. Tests: existing drill fakes. Done: drill tests stay green and the contract names the offset.

**Chrony test (GPT F08).** Make the handler proof use a stub service module or a molecule-style backend that does not need a host chrony unit. Files: `ci/tests/test_base_os.py`, maybe the handler. Tests: the same test passes with no chrony unit. Done: that one pytest on a non-systemd host, then `make ci-verify` with `OWNER_NAME` set.

**Controller apt and Python (GPT F19, Grok F21).** `apt-get update` before install, document Python `>=3.12`, drop `make` from `_TOOLS`, add `age-keygen` to the preflight when it will be called. Files: `tools/install-deps`, `tools/prep-node`, `docs/deploy.md`. Tests: prep-node missing-tool test expects `age-keygen` and not `make`. Done: those tests. The apt change is a one-line command; do not add a new package manager.

**verify diagnosis (Claude F26) and cron walker (F37).** Add node Ready to `_check_nodes`. Leave pod health and Gatus for the roadmap. Swap the cron walker only if `croniter` is already a dependency; otherwise leave it. Files: `tools/verify/run`, `ci/tests/test_verify.py`. Tests: a node without Ready fails. Done: that pytest.

**Binder placement (Claude F05).** Document in backups Limits that an unbound restore binds on the scheduled node. A later change can set a nodeSelector from the PV. Not this audit's code change unless the selector is obvious from the claim. Files: `platform/backups/CONTRACT.md`. Done: the sentence is next to the existing "no nodeSelector" line.

**CA `ca.crt` (T14) and nameConstraints (Claude F17).** When a new CA secret is written, refuse if `ca.crt` exists and differs, or rewrite `ca.crt` in the same run. nameConstraints can wait. Files: `tools/bootstrap-secrets`, `ci/tests/test_bootstrap_secrets.py`. Tests: existing `ca.crt` plus a missing CA secret fails or rewrites. Done: that pytest.

**Encryption test (T03) and verify negatives (T02).** Assert `ENC[` for every generated file's data and stringData. Add one verify test with a failing issuer stub and one with a wrong body. Files: the two test files. Done: both pytest files.

**k3s unit template (T16) and age overwrite test (T17).** Render `k3s.service.j2` in `test_k3s.py` and assert `ExecStart` differs for server and agent. Assert the sops-age task refuses when the existing key differs, using the current ansible proof harness. Files: those tests and, only if the assert is actually missing a case, `sops_age.yml`. Done: the new tests fail when the template or the `when` is removed.

**Dead assertion (T10) and env-shape self-tests (GPT F17).** Delete the second `capsys` assertion's impossible check, and delete the two `_assert_shape` self-tests. Files: `test_restore.py`, `test_env_shapes.py`. Done: pytest those files.

**Roadmap instead of deletion (GPT F10, Claude F27, question 24).** Add `docs/roadmap.md` with Gatus, observability, off-host backup, etcd restore from restic, on-demand backup, Forgejo and the code-factory rows, the Hermes profile, GPU runtime, HA, arm64, and per-namespace backup credentials. Then stop requiring hermes and apps values for a new env. Do not delete env-01's generated secrets. Files: `docs/roadmap.md`, `schema/env.py`, `docs/code-delivery/CONTRACT.md` (label planned rows as not offers). Tests: schema tests updated to the new required set; core-names still clean. Done: env-schema pytest and the roadmap linked from `docs/README.md`.

**Command guard (Grok F7, Claude F36).** In the deny lists, cover the Secret-read category case-insensitively, or fold case in the documented client model and add corpus rows for mixed case. Do not add make-target bypasses. Keep kubeconfig as a separate read category if it is in scope for the build agents. Files: `.cursor/cli.json`, `.claude/settings.json`, `ci/checks/lib/_cli_permissions.py`, the must-block fixture. Tests: `ci/checks/cli-deny` and `pytest ci/tests/test_cli_permissions.py`. Done: those two, with no new allow entries.

**Contract sentences (GPT F18, Grok F18).** Add the short Operations and Limits from GPT's list and Claude's 4a, without pasting full runbooks. The restic, token, first-backup, and upgrade-order lines are the ones that matter. Files: the ten CONTRACT.md files. Done: a human read, plus contract-md still passing. No new heading enforcer in this PR (T11 can fail closed on a missing root in its own small PR).

## 5. Concerns and questions

- This round did not re-run `make ci-verify`. The 482/1 chrony result is the first round on this same tree. Claude's 483 was not reproduced. The judge should treat "CI is green" as "GitHub's runner has chrony", not "the test is hermetic".
- Claude's seeded pass counts (T01–T18) were not re-run. Where the verdict says confirmed, the source matches the claim. Where it says partly, the seed was not repeated.
- Claude's k3s token path `server/token` is wrong. The file is `server/node-token`. GPT's "eight-host" example is seven hosts.
- Command-guard findings are categories only. No bypass string belongs in a follow-up PR description either.
- Single control plane, amd64, public Git, NTS, and the inert unattended-upgrades policy are choices. Fixing them means a product change, not a bugfix.
- Nothing here required a limit change. No product file was modified.

## Coverage of the other two files

Every GPT id F01–F25 is in section 1. GPT has no F26. Every GPT delete-list row is in section 1. Every Claude id F01–F39 and T01–T20 is in section 1. Every Claude delete-list row is in section 1.
