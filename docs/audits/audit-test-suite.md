# Audit: test suite (audit-test-suite)

The suite is 106 collected tests. Most fixture tests encode a real rule. Three checks stay green on inputs they are supposed to reject. The runtime cost is two container tests, not the fixture file.

Checked at `14b4f0d4`. Pytest here: 104 non-container tests in 53s, 2 container tests in 98s (81s and 17s). `tools/run-checks` was 1.4s. No playbook was run against a host, and no kubectl command was sent to a cluster. Matcher results below are from `command_allowed` on `.cursor/cli.json`.

## Misses

### cli-deny stays green while age decrypt and several kubectl writes are allowed

**Where:** `tools/_cli_permissions.py:77` (`fnmatchcase`), `tools/_cli_permissions.py:156-194` (dry-run list), `.cursor/cli.json:15-72`, `tools/tests/test_cli_permissions.py:22-58`

**What:** Deny entries are case-sensitive, and several kubectl verbs match only when the subcommand is the first argument. `kubectl get Secret` and `kubectl get SECRET` are allowed; `kubectl get secret` is denied. `kubectl -n kube-system rollout restart`, `kubectl -n kube-system drain`, and `kubectl -n default label` are allowed; the same commands with the verb first are denied. `age -d` is denied, but `age -i <key> -d <file>` and `bash -c 'age -d …'` are allowed. There is no `*age*` wrapper pattern, unlike `sops`. The dry-run list and the live-tree pytest copies use only the shapes that already match, so `tools/checks/cli-deny` prints `dry-run OK`.

**Fix:** Match kubectl resource tokens case-insensitively, and treat a verb as present anywhere in the argument list for every denied kubectl and flux subcommand (the `*kubectl*delete*` style already does this for some verbs). Deny `age` decrypt when `-d` or `--decrypt` appears anywhere, including under `bash -c` and `sh -c`. Add those command lines to `DRY_RUN_MUST_BLOCK`. Delete the five live-tree tests in `test_cli_permissions.py` (`test_kubectl_apply_blocked_kubectl_get_allowed`, `test_helm_and_ssh_blocked`, `test_flux_writes_blocked_reads_allowed`, `test_age_key_path_read_blocked`, `test_sops_commands_blocked`). Keep `test_matches_shell_command_args_split`, `test_deny_beats_allow`, and `test_minimal_deny_list_in_tmp`, and extend that fixture so the new dry-run lines fail on a short deny list.

**Why:** R34 is this deny list. An agent can read a Secret and decrypt with the age CLI while the check is green. Flag-before `drain` and `rollout restart` are cluster writes the same list means to block.

**Impact:** high

### Flux schema test skips the CRDs it claims to check

**Where:** `tools/tests/test_flux_entry.py:31-34` and `tools/tests/test_flux_entry.py:241-273`

**What:** The schema URL under `fluxcd/flux2` `v2.8.3/schemas/` does not resolve. `platform/flux-operator` and `envs/<env>/flux` are validated with `-ignore-missing-schemas`. On the current tree that is 0 of 2 resources valid for the operator layer (HelmRelease and OCIRepository skipped) and 1 of 6 valid for the env flux entry (ConfigMap only; FluxInstance and four Kustomizations skipped). A Kustomization with `spec: 12` exits 0 under those flags. The same document fails against a schema location that resolves. The live manifests do validate when the schema is found (6/6 and 2/2).

**Fix:** Point `-schema-location` at a template that resolves for Kustomization, HelmRelease, OCIRepository, and FluxInstance. `https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json` does. Drop `-ignore-missing-schemas` for those layers and fail the test if kubeconform skips a resource. Keep the sops-secret strip on the platform-configs overlay so ciphertext is not schema-checked as a Secret.

**Why:** A broken Flux manifest merges. The test name says the layer was schema-checked. Reconcile is the first place it fails.

**Impact:** high

### The sops test passes when a secret value is plaintext

**Where:** `tools/tests/test_flux_entry.py:185-191`, `tools/checks/secret-scan:1-14`

**What:** `test_backups_restic_secret_stays_sops_encrypted_in_git` requires the substrings `sops:`, the sops ENC marker, and `RESTIC_PASSWORD` anywhere in one file. A plaintext password next to a leftover ENC mac satisfies it. gitleaks (the secret-scan check, default rules, no config) did not flag a low-entropy password in that shape. It does flag a real age private key and a high-entropy token. No test plants a secret and expects `secret-scan` to exit non-zero, and the other `envs/<env>/secrets/*.sops.yaml` files are not read by any test.

**Fix:** One test loads every `envs/*/secrets/*.sops.yaml`, requires `sops` metadata, and requires every `data` and `stringData` leaf to be an ENC value. Add a temp-repo fixture that runs `tools/checks/secret-scan` (or the same gitleaks invocation) against a planted age private key and expects a non-zero exit. Do not plant that key in this repo.

**Why:** R25 is plaintext secrets out of git. The only structural test of a committed secret accepts a decoy, and a broken or no-op gitleaks invocation still passes on a clean tree.

**Impact:** high

### StrictPostBuildSubstitutions is not locked

**Where:** `infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2:27-35`, `tools/tests/test_flux_entry.py:130-155`, `schema/CONTRACT.md:103-105`

**What:** The contract says the FluxInstance sets `StrictPostBuildSubstitutions=true`, so a missing postBuild variable fails the reconcile. The test only checks that the committed file equals the rendered template. Deleting the feature gate from both stays green. Nothing checks that a `${name}` in a manifest is a key in that env's `cluster-vars.yaml`. No manifest uses `${…}` today.

**Fix:** On the rendered FluxInstance, assert the kustomize-controller arg is present. When a manifest uses `${name}` (and not `$${`), fail unless `name` is a key in that env's cluster-vars. Fold the assert into the existing render test.

**Why:** Without the gate, a typo substitutes an empty string and Flux reconciles. The equality test does not notice if the template and the committed file drop the gate together.

**Impact:** medium

### The k3s checksum is not what any test reads

**Where:** `schema/CONTRACT.md:46`, `infra/ansible/roles/k3s/tasks/main.yml:72-80`, `tools/tests/test_env_schema.py:167-173`

**What:** The contract says `k3s_sha256` is verified on install. The role's `get_url` sets `checksum: sha256:{{ k3s_sha256 }}`. The only test rejects a non-hex value in the env schema. Removing the `checksum` key from the task stays green.

**Fix:** Assert the `get_url` task's `checksum` is `sha256:{{ k3s_sha256 }}`. One YAML read, same shape as the kubeconfig path test, until a local download fixture exists.

**Why:** A pin that is only a hex string does not check the binary. A dropped checksum installs whatever the URL returns.

**Impact:** medium

### `.sops.yaml` creation rules are unchecked

**Where:** `schema/CONTRACT.md:119`, `.sops.yaml:1-4`

**What:** The contract says one age creation rule per non-example env, keyed on `envs/<env>/secrets/`, and no rule for the example env. No test or check reads the repo's `.sops.yaml`. `test_bootstrap_secrets.py` writes its own throwaway rules. Deleting the committed rule stays green until `bootstrap-secrets` or `seal` runs.

**Fix:** In env-schema, parse `.sops.yaml` and require exactly one `path_regex` per non-example env under `envs/<env>/secrets/`, and none for the example env. One fixture with a missing rule and one with an example-env rule.

**Why:** A new env, or a deleted recipient, merges. The first encrypt is the failure, or sops encrypts with the wrong rule.

**Impact:** medium

### The kubeconfig test never runs the server rewrite

**Where:** `tools/tests/test_kubeconfig.py:12-24`, `infra/ansible/roles/kubeconfig/tasks/main.yml:25-33`

**What:** The only kubeconfig test loads the task YAML and checks that `fetch.src` is `{{ k3s_kubeconfig_path }}` and that the task file does not redefine that variable. The replace that rewrites `https://127.0.0.1:6443` to the inventory hostname is not rendered or run. Deleting that task stays green.

**Fix:** Run the replace task against a fixture kubeconfig on localhost and assert the server URL is the inventory hostname. Drop the string compare of `fetch.src`, or keep it only as the assertion that the role does not override the group_vars path.

**Why:** The fetched file points at localhost. From the controller, the cluster API is unreachable. The suite still says kubeconfig passed.

**Impact:** medium

## Cuts

### Replace the two container tests with local plays

**Tests:** `test_flux_operator_check_mode_idempotent_when_done` (`tools/tests/test_flux_operator.py:199-248`, 81s) and `test_k3s_guard_refuses_active_k3s_without_marker` (`tools/tests/test_k3s.py:307-323`, 17s).

**Replace with:** A localhost play that imports the flux-operator role in check mode against the existing fake `k3s` binary, and asserts the second recap is `changed=0`. The prep play's base-os run is what costs the 81s; the role under test never needs systemd once the fake `kubectl` answers. For the guard, import `guard.yml` with `k3s_binary_path`, `k3s_data_dir`, and `jarvis_marker_path` pointed at empty temp paths and a `systemctl` on `PATH` that prints `active`. Assert the fail message. Then delete the systemd image helpers in `tools/tests/ansible_role_proof.py`, the `docker info` gate in `tools/ci-local-verify:12-15`, and the `slow` job in `.github/workflows/ci.yml:39-58`.

**Why:** Both proofs are "a command result makes an assert fail or a skipped block stay unchanged." A local fake already does that for sops-age (`test_existing_sops_age_skips_key_when_unset`). The container adds an image build and `apt-get`.

### Stop re-scanning the tree CI already scans

**Tests:** the five `test_cli_permissions.py` functions named in the first miss, and `test_env_tree_passes` (`tools/tests/test_env_schema.py:92-94`).

**Replace with:** nothing new. `tools/checks/cli-deny` and `tools/checks/env-schema` already run on the tree in `tools/run-checks`. The fixture tests stay.

**Why:** `rulebooks/coding-agents.md` says not to add a test that only runs a `tools/checks/` entry against the tree CI already scans.

### Build each Flux layer once

**Test:** `test_flux_layers_do_not_render_duplicate_objects` (`tools/tests/test_flux_entry.py:211-228`).

**Replace with:** the same identity check inside `test_flux_layer_kustomize_build_and_schema_validate`, after that test stops skipping CRDs. One `kustomize build` per layer.

**Why:** The duplicate-object test builds the same four layer paths the schema test builds again. It was not in the slowest 20, so this is a small saving. The schema fix above is the reason to touch the test.

### Drop the kubectl create string search

**Test:** `test_sops_age_secret_task_uses_kubectl_create_not_apply` (`tools/tests/test_flux_operator.py:311-314`).

**Replace with:** the local fake `k3s` play, with the secret absent, asserting the logged argv contains `kubectl create` and not `kubectl apply`.

**Why:** A comment that contains `kubectl create` and a task that applies another way passes the substring check. The fake binary records the command that actually ran.

### Derive inventory expectations from the inventory

**Tests:** `test_env_00_inventory_graph_shows_role_groups` (`tools/tests/test_inventory_roles.py:17-57`) and `test_agent_play_host_pattern` (`tools/tests/test_k3s.py:181-186`).

**Replace with:** expected groups computed from the example env's `node_roles` plus `data_disk is defined`, and agent-play hosts compared to every inventory host outside `control_plane`. Keep both tests.

**Why:** `_EXPECTED_GROUPS` is a second copy of the example inventory, and `hosts (6):` / `hosts (0):` is Ansible's print format for today's host count. A real group bug should still fail; an inventory edit should not.

## Fine

- node-disk refusals (`test_disk_refusal_cases`, 13s) are one play over fixture cases, including the longer nvme name, LVM, and a foreign label. Keep it. It is the slowest non-container test and it is the proof.
- GPU vendor assert, env-schema rejection fixtures, core-names fixtures, and contract-md fixtures each plant a bad tree and expect a failure.
- `test_bootstrap_secrets.py` checks a second run is byte-identical, the restic manifest shape, and that a failed sops leaves no file.
- `test_site_yml_role_order_and_ssh_user` and `test_kubeconfig_tag_runs_only_the_kubeconfig_role` encode play order and the tag filter. One test each.
- `test_verify_p0a_wrong_role_and_no_flux_objects` covers a wrong role label, a missing HelmRelease, and the secret value staying out of stdout.

## Appendix

- `test_base_os_templates_encode_manual_upgrade_policy` passes `2G` itself and asserts `SystemMaxUse=2G`, so it does not read the role default (`infra/ansible/roles/base-os/defaults/main.yml:3`). Four separate `ansible` template runs are 6.6s.
- `test_k3s_config_template_encodes_p0a12_rules` sets snapshot cron and retention and never asserts them (`tools/tests/test_k3s.py:61-72` only checks that `etcd-snapshot-dir:` is present).
- `test_verify_p0a_wrong_role_and_no_flux_objects` never feeds an enablelb mismatch, so `_check_enablelb` can return 0 and the test still passes.
- The k3s guard replacement should keep the one active-service case. Wrong marker contents and binary-without-service are the same assert; they are notes, not more tests.

## Unsure

- Resource-kind case is the kubectl bypass I would fix. I did not check whether a capitalised verb (`APPLY`) is accepted by kubectl, and I did not run any of the allowed command lines.
- `rulebooks/coding-agents.md` leaves LAN IPs and numeric user IDs as prose under R39. I did not treat a missing IP scan as a miss.

106 collected tests now, 98 after the four deletions (five live cli tests, `test_env_tree_passes`, the duplicate kustomize test, the kubectl-create string test) with the two container tests replaced in place. Pytest was 151s here; the container pair is 98s of that, and the local replacements should be a few seconds, so the suite drops by about a minute and a half.
