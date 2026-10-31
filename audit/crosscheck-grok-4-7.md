# Cross-check — Grok 4.7 against GPT-5.6 Sol

Both audits read `5561280d2c57e0b7d0d7d1a8c2eaa1a95cf9bdd8`. This pass is read-only: no code edits, no decrypt, no SSH, no live cluster. The other file is the GPT-5.6 Sol audit. “Mine” is `audit/findings-grok-4-7.md`.

Reproduced here: the restore fake cluster, the four CI seeds, `load_documents` on a list, `recorded_checksum` on a failure transcript, `run_env_schema`, SOPS metadata keys (no ciphertext), ConfigMap key names (no values), `seal` path joins (no write), `kustomize build` of an empty kustomization, an unset `OWNER_NAME` scan, and the line counts below. Quoted “theirs” blocks are their evidence. “Mine” is this pass.

## 1. Verdicts on the other file

### F01 — Snapshotless restore-all lifts the hold

**Confirmed.**

Theirs:

```text
SKIP no snapshot demo/drill
HOLD lifted
return=0 hold_deleted=True
```

Mine, using `ci/tests/test_restore.py`’s `_Cluster` with `hold=["ns-demo/pvc-drill"]` and `_NO_SNAPSHOT_LOG`:

```text
pause demo/drill
restore demo/drill as-of 2026-10-29T00:00:09Z
SKIP no snapshot demo/drill
HOLD lifted
RESTORE_ALL_NOSNAP_HOLD return 0 hold_deleted True
```

`tools/restore:855-870` catches `NoSnapshot`, still appends the PVC to `done`, then calls `_lift_hold` when `--all` and a hold exist. `platform/backups/CONTRACT.md:49` says the hold is deleted only after every restore has succeeded. The existing `test_restore_all_skips_a_volume_with_no_snapshot` does not set a hold, so CI never locks this outcome. My F2 missed this success path.

**Fix:** S3.1.

### F02 — Hold deletion is non-atomic

**Confirmed.**

`tools/restore:781-805` deletes ConfigMap `kube-system/backup-rebuild-hold` and only then resumes sources. `ci/tests/test_restore.py:571-580` requires `hold_deleted is True` when the source disappears after that delete and the run returns non-zero. A later API error cannot put the hold back. Their “existing test requires `cluster.hold_deleted is True`” matches that test.

**Fix:** S3.1.

### F03 — Backups do not provide hardware-loss recovery

**Partly confirmed.** Same-disk copies, Direct live copies, and the missing k3s token are in the tree. The contract does not promise rebuilt-node recovery.

Theirs: the contract says same-disk copies are not disk-failure protection, and rebuilt-node restore needs the original token while only same-node restore is in scope.

Mine:

```text
platform/backups/CONTRACT.md:46
While the rest-server's data sits on the same disk, this copy adds retention and dedup, not disk-failure protection.
```

```text
platform/backups/CONTRACT.md:59
k3s secrets-encryption is on, so restoring onto a rebuilt node also needs the original k3s server token; same-node restore is the stated scope.
```

`platform/backups/rest-server.yaml:63-67` is a hostPath at `${backup_path}/restic`. `infra/ansible/roles/k3s/templates/config.yaml.j2` turns secrets-encryption on. Enrollment uses `copyMethod: Direct` and no application hook (`enrollment.yaml:85`). A live Direct copy can be crash-consistent only. env-01 sets `backup_path` under `/var/lib/jarvis` and has no `data_disk`, so that hostPath is on the root disk. That matches my F5.

The sentence “cannot recover a rebuilt node as promised” overstates the contract. The contract already limits that case. There is still no escrow of the token and no off-host copy.

**Fix:** S3.2 for the confirmed part (say the limit where an adopter looks, keep the token out of Git). An off-host tier is their proposal P2, not this fix.

### F04 — One credential controls every backup repository

**Confirmed.**

`enrollment.yaml:47-50` copies `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, and `RESTIC_REST_PASSWORD` from Secret `backups-restic` into every PVC secret. The repository URL differs. `rest-server.yaml:1-3` says there is no `--append-only` and no `--private-repos`. `etcd-snapshots.yaml:40-61` uses the same Secret and appends `host-etcd-snapshots`. One rest-server user can open every PVC repo and the etcd repo. I did not file this.

**Fix:** S3.3.

### F05 — Node lifecycle is not convergent

**Confirmed.**

- `node-disk/tasks/main.yml:74-79` mounts `LABEL=jarvis-data` at `node_disk_mount` (`/var/lib/jarvis` in `vars/main.yml:2`) with no empty-directory check. env-01’s `local_path_data` and `backup_path` are under that path. Adding a data disk later mounts over the live tree.
- The data-disk play is `hosts: data_disk` (`site.yml`). Leaving the group runs no unmount.
- `k3s/tasks/main.yml:6-9` and `:83-92` install and enable `k3s` or `k3s-agent` and never stop the other unit. `guard.yml:64-69` writes only `jarvis_env` into the marker.
- `labels.yml:27-28` patches nodes whose names are in `groups['all']`. Nothing in `infra/ansible` drains or deletes a Node.

**Fix:** S3.4.

### F06 — prep-node calls a missing validator

**Confirmed.** Same defect as my F1.

Theirs: `FileNotFoundError` for `/workspace/tools/checks/env-schema`, and no test references `run_env_schema`.

Mine:

```text
MISSING tools/checks/env-schema
PRESENT ci/checks/env-schema
FileNotFoundError 2 'tools/checks/env-schema'
```

`tools/prep-node:1002-1013` runs that missing path after `apply_env_edits` and `sync_age_key` (`:1076-1086`). The handler catches `PrepError` only. `rg run_env_schema ci/tests` has no matches.

**Fix:** S3.5.

### F07 — Root guard misses other Btrfs members

**Confirmed.** This is one line of the gap in my F7, and it is the one that reaches `wipefs -a`.

`infra/ansible/README.md:54` says the root check uses `findmnt`, `readlink`, and `lsblk -nso NAME,TYPE` parents, then: “Not checked: … and other disks in a multi-device btrfs filesystem.” `guards.yml:19-25` asserts only `node_disk_root_dev`, `node_disk_root_follow`, and `node_disk_root_disks` from that `lsblk`. `main.yml:51-53` then runs `wipefs -a` when the disk is not already `jarvis-data`. A second Btrfs member is not in that parent list, so a confirmed wipe can format it.

**Fix:** S3.6.

### F08 — Platform ordering and enrollment fail open

**Confirmed** as behavior. The fail-open policy is deliberate; the missing ready-wait is also deliberate.

`platform-controllers.yaml` says it sets no HelmRelease `healthCheck` so a chart pull does not stall `platform-configs`. `platform-configs.yaml:14-15` only `dependsOn` that kustomization. `enrollment.yaml:9-10` sets `failurePolicy: Ignore`. `ci/tests/test_backup_enrollment.py` asserts Ignore. Flux Ready on the controller kustomization does not mean the Kyverno or cert-manager webhook is serving. A PVC can bind while admission is failing.

Their “fail closed for mandatory enrollment” is a design choice, not the only reading of the contract. The contract documents Ignore. My F3 and F12 match the facts. I still would not flip Ignore to Fail in the first fix.

**Fix:** S3.7.

### F09 — Four CI rules accept seeded violations

**Partly confirmed.** Three seeds match. The split `image.repository` / `image.tag` seed does not.

Theirs:

```text
unrelated allowed-dir LoadBalancer: []
empty Helm values R5: []
split image fields R33: []
unrelated backup policy: []
```

Mine, temp tree, scanners imported from `ci/checks/lib`:

```text
SPLIT_IMAGE ['HelmRelease demo/demo: image nginx is not pinned by digest']
SIBLING_KEYS []
EMPTY_HELM_R5 []
ALLOWED_DIR_LB []
DECOY_POLICY []
```

- A Service `type: LoadBalancer` under `platform/traefik/` produces no `scan_loadbalancer` hit. `ALLOW_PREFIXES` is the directory (`_loadbalancer.py:17`).
- A HelmRelease with `values: {}` produces no `scan_manifest_lint` hit. `_walk_values` checks `resources` only when that key is present (`_manifest_lint.py:101-109`).
- A HelmRelease `values.image` map with `repository: nginx` and `tag: "1.27"` is a digest-pin violation. `_image_spec_pinned` (`_digest_pin.py:111-122`) treats that map as an image. Their “split repository/tag image fields are missed” does not hold for that shape.
- `repository` and `tag` as siblings of `values`, not under `image`, produce no hit. The checker’s contract is image fields, so that seed is outside the rule.
- `platform/backups/kustomization.yaml` listing only `decoy.yaml` (`kind: ClusterPolicy`, name `not-the-backup-policy`) makes `scan_pvc_backup` return `[]`. `_enrollment_policy_listed` (`_pvc_backup.py:93-116`) returns true for any ClusterPolicy in a listed file.

**Fix:** S3.8 for the three confirmed holes. No change for the image map that already fails.

### F10 — Non-mappings are skipped; VolSync spec is open

**Confirmed** for both mechanisms. The open `spec` is intentional and still skips field names.

`load_documents` (`_manifests.py:40-42`) appends a document only when it is a dict. A file whose document is a list produced `LIST_DOC_COUNT 0` and `LIST_ERRORS []` for manifest-lint, digest-pin, and loadbalancer, because all three call `load_documents`.

`replicationsource_v1alpha1.json:4-10` sets the root `additionalProperties: false` and `spec.additionalProperties: true`. The destination schema matches. The file says this overrides a catalog schema that predates `moverVolumes`. A misspelled restic key still passes. `customresourcedefinition_v1.json` is the same shape and its description says kubernetes-json-schema has no v1.36 document. Calling that CRD stand-in strict overstates it.

**Fix:** S3.9.

### F11 — Phase verifiers exit 0 without proof

**Confirmed.** P0a matches my F23. The P0b checksum hole is new.

`tools/verify/p0a:228-233` returns 0 after printing owner items. `ci/tests/test_verify_p0a.py:82-87` requires that exit 0 and the line `automatic checks passed; N owner items remain`.

`tools/verify/p0b:136-154` sets `restored` when any later line matches `\brestore\b`. `etcd_checksum_matches` (`:125-133`) is true when one snapshot-labelled line contains the same hash twice. Mine, transcript lines `sha256  /data/payload`, `persistentvolumeclaim "drill" deleted`, `restore failed: no data`, the same volume line, and one `etcd-snapshot-1` line with the hash twice:

```text
P0B_RECORDED <64 hex chars>
P0B_ETCD_ONLY True
```

`p0b:213-218` then returns 0 while `_print_checklist` still reports one owner item. `ci/tests/test_verify_p0b.py:85` locks that exit.

**Fix:** S3.10.

### F12 — Ansible mutates before some checks, and some lookups mean “absent”

**Confirmed.**

- `site.yml:3-28` asserts control-plane count and the GPU task before other plays. `k3s/tasks/main.yml:15-31` asserts `k3s_version`, `k3s_sha256`, and `architecture == x86_64` only inside the k3s role, after the base-os play has installed packages.
- `operator.yml:2-16` and `sops_age.yml:2-15` use `failed_when: false` and treat a non-zero `kubectl` rc as absence, which starts the bootstrap block. `instance.yml:8-14` fails the play unless the text contains `NotFound`. Their contrast is right.
- The Available wait in `operator.yml:40-47` is inside `when: not (flux_operator_deployed | bool)`. An existing Deployment is not checked for Available.
- `base-os/tasks/main.yml:47-54` installs `chrony.conf` with a validate command and no `notify`. `handlers/main.yml` restarts journald and resolved only. `state: started` (`:59-62`) does not restart a running chrony.

**Fix:** S3.11.

### F13 — Secret path, key mode, and CA prune

**Partly confirmed.** The path join, the missing mode check, and the missing CA prune annotation are real. The write stays inside the repo. Grafana and the four Hermes secrets also lack the annotation; they are not on a Flux path yet.

`tools/seal:43` builds `envs / env / secrets / f"{name}.sops.yaml"`. Resolved joins, no write:

```text
env=../ci name=evil -> ci/secrets/evil.sops.yaml
env=env-00 name=../../ci/not-a-secret -> envs/ci/not-a-secret.sops.yaml
```

`_sops.py:35-37` then `relative_to(repo_root)`, which rejects a path outside the repo and accepts both of these. `sops_age.yml:36-56` asserts the key file exists and is non-empty. It does not assert mode or owner.

Metadata only (annotation keys, not values or ciphertext): `backups-restic.sops.yaml` has `kustomize.toolkit.fluxcd.io/prune`; `cert-manager-ca.sops.yaml` has metadata keys `name` and `namespace` only. `envs/env-01/flux/platform-configs/kustomization.yaml` applies that CA secret, so a path move can prune it. The other five secrets also lack the annotation and are not in that kustomization.

**Fix:** S3.12.

### F14 — Exactly one control plane and one backup host

**Confirmed.** Same constraint as my F9.

`schema/env.py` singleton roles and `site.yml:14-20` require one `control_plane`. Agents are `all:!control_plane`. `rest-server.yaml:27-28` pins the Deployment to `role.${dns_suffix}/backup`. There is no server-join path. Adding a worker does not add a second etcd member or a second rest-server.

**Fix:** S3.13. Document the ceiling. Do not build a second control plane in that PR.

### F15 — amd64 and NVIDIA are hard requirements

**Confirmed.** Same facts as my F10 and F22.

`k3s/tasks/main.yml:28-31` asserts `architecture == x86_64` and `:72-79` downloads the amd64 `k3s` asset with `k3s_sha256`. `assert-gpu.yml` requires PCI vendor `0x10de` on a `gpu` host. `tools/versions.env` has arm64 checksums for controller tools. The schema has one k3s checksum.

**Fix:** S3.13 (one support-matrix paragraph). Arch-specific checksums wait until an arm node exists.

### F16 — DNS, ingress, and CA change boundaries

**Partly confirmed.** The Git config has no class filter, no HTTP redirect, no issuer approval, and no ClusterIP migration. Those are the current single-tenant shape, which `envs/env-01/README.md:7` states. They are not a broken multi-tenant control.

`platform/app-dns/helmrelease.yaml:64-68` sets `resources Ingress` and `loadBalancerAddressPreference ip` and no ingress class. `coredns-custom.yaml:16` forwards the zone to `${app_dns_cluster_ip}`. `helmrelease.yaml:37-38` sets that Service `clusterIP` from the env key. Kubernetes will not change an existing Service `clusterIP`; this repo has no migration step. `platform/traefik/helmchartconfig.yaml:14-17` sets `isDefaultClass: true` and does not set a web-to-websecure redirect. `internal-ca.yaml:3-9` is a cluster-wide `ClusterIssuer` with no approval policy.

**Fix:** S3.14. State the single-tenant boundary and the ClusterIP rule. Do not add a multi-tenant policy in this foundation.

### F17 — One schedule, one rest-server, 1Gi cache per PVC

**Confirmed** as a scaling ceiling, not as a current outage.

`enrollment.yaml:80-91` sets one `${backup_schedule}`, `cacheCapacity: 1Gi`, and no `moverResources`. `rest-server.yaml:15-17` is one replica, strategy `Recreate`, memory limit `128Mi`. The contract (`CONTRACT.md:11-13`) says each StatefulSet ordinal gets its own source. I called the etcd cron acceptable until measured; this PVC fan-out is the part that shows up with the second claim.

**Fix:** S3.15. Add mover resources in the policy. Leave schedule staggering until a second PVC exists.

### F18 — Env values are global, unused, and shallow

**Confirmed.** Overlaps my F14. The ConfigMap export of controller-only keys is sharper than my write-up.

`schema/env.py:557-610` checks Hermes and apps as containers and enums. It does not require each bot to appear in `bot_tier_map`. `render_cluster_vars_configmap.py:41-46` writes every `resolve_cluster_vars` key. `resolve_cluster_vars` (`schema/env.py:99-106`) fills `ssh_user` and `age_key_file` and does not call `validate_env_tree`. The generated env-01 ConfigMap contains these keys (values not printed): `age_key_file`, `ssh_user`, `k3s_sha256`, `apps_git_repo_url`, `llm_provider_host`, `backup_path`, `local_path_data` (26 keys). `rg` finds `apps_git_repo_url` and `llm_provider_host` in schema, env files, core-names, tests, and that ConfigMap. No Flux source or workload reads them.

**Fix:** S3.16.

### F19 — services and apps are one chain, and the apps repo is unused

**Confirmed.** Same seam as my F14.

`envs/env-01/flux/kustomizations/services.yaml:9-15` and `apps.yaml:9-15` both use GitRepository `flux-system`. Apps `dependsOn` services. Both set `prune: true`. `apps/placeholder.yaml` and `services/placeholder.yaml` are the only objects. `apps_git_repo_url` has no sourceRef.

**Fix:** S3.16, in the PR that adds the first real workload. Do not split the chain before that.

### F20 — No single adopter path

**Confirmed.** Same gap as my F13.

`README.md` is a short pointer at `https://github.com/<owner>/project-plans`. `docs/README.md` says the plan stays outside. `envs/env-01/README.md:23-31` says create an env empty and “add a flux/ tree” without the file list. `infra/ansible/README.md` has the playbook order and the tag table. The command sequence is split across those files plus secrets README and the schema contract.

**Fix:** S3.17.

### F21 — The command guard dry-run is a local parser

**Partly confirmed.** The checker never calls a Cursor binary. The coverage helper is a substring list. The dry-run corpus is still a real allow/deny test of this parser, which my F25 measured.

`_cli_permissions.py:1-12` says it mirrors cursor-agent `2026.10.22-27dcf5f`. `cli-deny:14` calls `run_dry_run` and `validate_deny_coverage` in this process. `validate_deny_coverage:585-605` checks that the deny text contains `kubectl`, `helm`, `ssh`, `scp`, `sops`, and `flux`. It does not execute those tools. `command_allowed:342-344` returns true when no Shell allow entry exists. No Cursor permission engine is in the repo, so “CI can stay green while the runtime parser changes” is true and cannot be closed by importing a binary this tree does not pin.

**Fix:** S3.18. Keep the corpus. Do not block the fix on a Cursor test interface this repo does not have.

### F22 — Pins, a tolerated play failure, duplicate tree scans, a dead Renovate pattern

**Partly confirmed.**

- `requirements-dev.txt` is `pytest>=8.0`, `PyYAML>=6.0`, `ansible==13.1.0`, `bcrypt>=4.0`. Three ranges are open. `.github/workflows/ci.yml:26` titles the step “Install pinned CLIs and Python test deps”. The CLIs are pinned in `tools/versions.env`. The Python ranges are not.
- `ci/tests/test_flux_operator.py:521-522` allows `proc.returncode != 0` when the output contains `chown failed`. The test does not accept an arbitrary failure. Their “accepts a failed play” is this `chown` branch.
- `test_digest_pin.py:108`, `test_loadbalancer.py:83`, and `test_manifest_lint.py:197` assert the scanner returns `[]` on the repo. `ci/run-checks` already runs those scanners. The same files also hold seeded fixture tests.
- Several tests import helpers from other test modules (`test_backup_enrollment.py` imports `_kustomize_build` from `test_flux_entry`; `test_core_names.py` imports from `test_env_schema`).
- `renovate.json:159-161` matches `platform/backups/test/.+\\.yaml`. That directory is absent.
- `helm_cli.yml:18-24` skips the checksummed download when `helm_binary_path` already exists. The F22 prose does not mention this; the coverage table does. The task text is real.
- Test Python is 6410 lines (35 files), matching their count. Non-test `.py`/`.yml`/`.yaml`, excluding `ci/tests`, `.venv`, `audit`, and `*.sops.yaml`, is 5710 lines across 97 files. Their “about 6,900” is the same order of magnitude.

**Fix:** S3.19 for the dead Renovate pattern, the three tree-rescan asserts, the `chown` branch, and the Helm stat. Leave the cross-test helper move and the Python lock as a follow-up in the same fix description.

### F23 — Kubeconfig replaces `~/.kube/config`

**Confirmed.** Same role as my F15. `backup: true` is an extra I understated.

`kubeconfig/tasks/main.yml:25-29` rewrites only `https://127.0.0.1:6443`. `:43-48` copies to `$HOME/.kube/config` with `mode: "0600"` and `backup: true`. Ansible `backup: true` leaves the previous file beside the destination. A second env overwrites the same path.

**Fix:** S3.20.

### Proposals

| Proposal | Verdict | Why |
| --- | --- | --- |
| Transactional restore state machine | **Confirmed** | Matches F01, F02, and the destructive path in my F2. One restore PR. |
| Off-host recovery tier with escrowed token | **Partly confirmed** | Serves node or disk loss. The contract already refuses to call the local copy disk-failure protection. Escrow the token outside Git. Do not build the tier until a second disk exists. |
| Explicit node and storage lifecycle commands | **Confirmed** | Matches F05. Inventory removal must not imply node deletion. |
| Controller and config health gates | **Partly confirmed** | `dependsOn` is not webhook Ready. Health on every HelmRelease reintroduces the chart-pull stall the controllers file avoids. Gate Kyverno and cert-manager only. |
| Component-owned env schemas and Flux boundaries | **Confirmed** | Serves the first service and a second env. Land it with that service, not before. |
| Env scaffold plus a repository-only runbook | **Confirmed** | Serves a second household. The runbook stays in this repo. Router DNS forwarding and Tailscale stay outside the cluster. |
| Capability profiles | **Partly confirmed** | The honest matrix (amd64, NVIDIA or no `gpu` host, one server) is a paragraph. Profiles and HA wait for a tested node. |
| Runtime permission-engine integration | **Partly confirmed** | The local parser is not cursor-agent. This repo has no pinned engine to call. Keep the corpus and close the category gaps in S3.18. |

### Delete list

| Item | Verdict | Evidence |
| --- | --- | --- |
| `apps/placeholder.yaml` and `services/placeholder.yaml` | **Partly confirmed** | Each kustomization lists only that file. `kustomize build` of a kustomization with `resources: []` exits 0 and writes 0 bytes. Deleting the files while the resources list still names them breaks the build. The file comments (D9) say they stay until apps and services exist. They are the objects Flux applies today. Remove them in the PR that adds the first real resource. |
| `platform/backups/test/` Renovate pattern | **Confirmed** | `renovate.json:159-161` names that directory. `platform/backups/test` is absent. |
| Root-tree rescan asserts in the three scanner tests | **Partly confirmed** | The three `scan_*(REPO) == []` asserts duplicate `ci/run-checks`. The files also contain the seeded failures. Delete the three asserts only. |
| Eight-play order assert in `test_flux_operator.py` | **Partly confirmed** | `test_site_yml_role_order_and_ssh_user` locks eight `hosts` values (`test_flux_operator.py:63-72`). That is a shape lock. The safety fact is that the first play has no roles and runs before `base-os`. Replace the full list; do not drop the safety fact. |
| env-01 README `volsync-test` paragraph | **Refuted** as a delete now | Their condition is “after owner cleanup.” `envs/env-01/README.md:21` and `platform/backups/CONTRACT.md:60` still say `${backup_path}/volsync-test` is on the node and is removed by hand. Deleting the paragraph now hides that instruction. |

## 2. My findings against this file

| Mine | Other file | Relation |
| --- | --- | --- |
| F1 prep-node missing checker | F06 | Confirms. I do not retract it. |
| F2 restore deletes files, uses ambient kubeconfig, leaves workloads scaled down | F01, F02 | They confirm restore is dangerous and add the hold-lift success path I missed. They do not mention `enableFileDeletion`, the missing `ENV`, or `KeyboardInterrupt`. Those three still stand (`tools/restore:182-187`, `Makefile` restore target has no `ENV`, `except Fail` at `:709` does not catch `KeyboardInterrupt`). |
| F3 enrollment `failurePolicy: Ignore`, no live PVC check | F08, F09 | Confirms the policy. Their decoy-ClusterPolicy hole is extra and confirmed. I do not retract “keep Ignore” as the first change. |
| F4 mount path is `/var/lib/jarvis` | F05 | They confirm a later data disk can cover that path. They do not say the schema never checks `backup_path` and `local_path_data` are under it. That part stands. |
| F5 shipped env is same-disk; token is not in Git | F03 | Confirms the facts. Their “as promised” is the part I do not adopt. See F03. |
| F6 new CA does not reissue `wildcard-tls` | F13, F16 | Missed. Nothing they wrote contradicts `envs/env-01/secrets/README.md:27`. I do not retract it. Confidence stays medium. |
| F7 wipefs when the README’s extra checks are skipped | F07 | Confirms the Btrfs member. The rest of the README list (mounted elsewhere, foreign mount, partition contents) is still unchecked. I do not retract the wider list. |
| F8 no scanner requires `${…}` to be a cluster-vars key | — | Missed. A quoted unknown `${not_a_cluster_var}` still produces no digest-pin or manifest-lint hit. I do not retract it. |
| F9 one control plane | F14 | Confirms. |
| F10 k3s amd64 only | F15 | Confirms. |
| F11 digest pin and probes stop at the chart boundary | F09 | Confirms empty Helm values and the HelmChartConfig gap in my file. Their split `image.repository`/`tag` claim is refuted. I do not retract F11. |
| F12 platform-configs does not wait for Ready controllers | F08 | Confirms. |
| F13 no path to a first deploy | F20 | Confirms. |
| F14 Hermes, apps, and their secrets never enter the cluster | F18, F19 | Confirms. Their ConfigMap export of `age_key_file` and `ssh_user` is a real addition. |
| F15 kubeconfig path and `127.0.0.1` rewrite | F23 | Confirms. I understated `backup: true`. |
| F16 quiesce is only Deployment and StatefulSet | — | Missed. `tools/restore:409-429` still lists only those kinds. I do not retract it. |
| F17 contracts describe admin-gate, Gatus, and a second code host | F18, F20 | They call the prose future-state. They do not list those missing files. The contracts are still ahead of the tree. I do not retract it. |
| F18 kubectl minor, etcd cron, and `cluster.local` are separate pins | F22 | They mention version relationships and do not test the kubectl minor against `k3s_version`. I do not retract it. |
| F19 duplicated manifest scan roots | — | Missed. `_manifests.py:9-10` and `_pvc_backup.py:21-23` still copy the same tuples. I do not retract it. |
| F20 `make ci-verify` has no `OWNER_NAME` | — | Missed. They tagged `ci/ci-local-verify` as no finding. Unset `scan_core_names` raises `OwnerNameUnset` and the message does not contain the owner value. `Makefile` `ci-verify` only runs `ci/ci-local-verify`. I do not retract it. |
| F21 prep-node moves other netplan files aside | — | Missed. `tools/prep-node:613-616` still renames other `/etc/netplan/*.yaml` to `.bak`. I do not retract it. |
| F22 GPU vendor `0x10de` | F15 | Confirms. |
| F23 P0a exits 0 with owner items left | F11 | Confirms, and adds the P0b transcript hole. |
| F24 tag examples omit `-i` and `-e` | F20 | Their adopter finding cites the Ansible README and does not cite the tag table. `infra/ansible/README.md:44-46` still shows `site.yml --tags` without inventory or `jarvis_cluster_vars`. I do not retract it. |
| F25 command-guard categories and stock gitleaks | F21 | They confirm the guard is a local parser. They do not mention default-allow when no Shell allow entry exists, the mixed-case gap at `_cli_permissions.py:388-390`, or the absence of `.gitleaks.toml`. I do not retract those categories. No bypass string belongs in a fix. |
| F26 `install-deps` does not `apt-get update` | Their verification paragraph | **Contradicts my ranking.** Both of us saw the first `make deps` fail on this VM, then succeed after `apt-get update`. Their call is the better one: that failure needed an empty apt index. `tools/install-deps:97-101` still runs `apt-get install` with no update, and base-os uses `update_cache: true`. I overstated a VM failure as a repo defect. I withdraw F26 as a ranked finding. A one-line update is optional and is not in the fix list. |

My delete list said the placeholders are load-bearing and that removing them breaks the kustomization. That is true while `resources` names them. An empty `resources: []` builds. I was too absolute. I still would not delete them before a real resource replaces them.

My keep list (schema gate, core-names, SOPS stdin encrypt, one-shot Flux, k3s foreign-install guard, two inventories, backup opt-out prefixes, digest pins on pod images) is compatible with their keep list. I do not retract those keeps.

## 3. Suggested fixes

Each fix is one PR unless the size says to split. Done checks are offline. They assume `make deps` has already succeeded. Export `OWNER_NAME` to the repository variable before a done check; the pytest lines use that value and do not embed it.

### S3.1 — Restore fails closed (their F01, F02; my F2, F16)

**Approach.** `restore-all` records a PVC in `done` only after a mover log shows a restore. A `NoSnapshot` while a rebuild hold exists is a failure: do not lift the hold, do not unpause. `_lift_hold` checks that every non-retired repo has a PVC and a ReplicationSource before it deletes the ConfigMap. If a later resume fails, it recreates the ConfigMap with the same `data.repos` and exits non-zero. `make restore` and `make restore-all` require `ENV`, and the tool refuses to run unless the current context’s API server host matches the inventory control-plane hostname. `enableFileDeletion: true` stays; it is how the copy matches the snapshot. On `Fail` and on `KeyboardInterrupt`, print the same undo lines. If a DaemonSet or a Job pod mounts the PVC, fail before the copy; keep scaling only Deployments and StatefulSets. Update `platform/backups/CONTRACT.md` so the hold sentence matches this.

**Files.** `tools/restore`, `Makefile`, `platform/backups/CONTRACT.md`, `ci/tests/test_restore.py`.

**Tests.** Hold plus no snapshot returns non-zero and `hold_deleted` is false. A source that disappears after the delete causes the fake client to see a recreate of the hold. A DaemonSet pod mount fails the run. A `KeyboardInterrupt` in the fake still prints the undo lines. Missing `ENV` returns non-zero.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_restore.py`

### S3.2 — Say what a lost disk does and does not restore (their F03; my F4, F5)

**Approach.** In the deploy note from S3.17, state three facts: a `data_disk` is formatted at `/var/lib/jarvis`; no `data_disk` means restic shares the OS disk; etcd restore onto a rebuilt node needs the original k3s server token, kept with the age key and not in Git. Add a schema check that `backup_path` and `local_path_data` are absolute paths under `/var/lib/jarvis` until the mount path becomes an env key. Do not add a token secret.

**Files.** `schema/env.py`, `schema/CONTRACT.md`, `ci/tests/test_env_schema.py`, and the new deploy note.

**Tests.** A `backup_path` of `/srv/restic` fails validation. env-01’s current paths still pass.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_env_schema.py`

### S3.3 — Separate backup credentials (their F04)

**Approach.** Give each repository its own rest-server user and restic password. The PVC generate rule stops copying the shared `RESTIC_PASSWORD` and `RESTIC_REST_*` keys. The etcd CronJob uses a different user. rest-server runs with `--private-repos`. Keep one bootstrap Secret as the map of those users, still SOPS-encrypted, still not printed. This is its own PR. Do not mix it with S3.1.

**Files.** `platform/backups/enrollment.yaml`, `platform/backups/rest-server.yaml`, `platform/backups/etcd-snapshots.yaml`, `platform/backups/CONTRACT.md`, `tools/bootstrap-secrets`, `ci/tests/test_backup_enrollment.py`, `ci/tests/test_restic_rest_server.py`, `ci/tests/test_bootstrap_secrets.py`.

**Tests.** The policy document does not reference `backupsRestic.RESTIC_PASSWORD` inside the generated PVC secret. The CronJob user and the PVC user differ. The Deployment args include `--private-repos`.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_backup_enrollment.py ci/tests/test_restic_rest_server.py ci/tests/test_bootstrap_secrets.py`

### S3.4 — Reject lifecycle transitions (their F05; my F4)

**Approach.** The node-disk role refuses to mount over a non-empty `/var/lib/jarvis` that is not already the `jarvis-data` filesystem. Removing a host from `data_disk` does not unmount; the role docs say an explicit retire task is required and that task is not implied by inventory. The k3s role refuses to start when the other unit (`k3s` versus `k3s-agent`) is enabled. The marker file stays the env name. Label reconciliation does not delete Nodes that left inventory; the Ansible README says removal is an owner drain, outside `site.yml`.

**Files.** `infra/ansible/roles/node-disk/tasks/main.yml`, `infra/ansible/roles/k3s/tasks/main.yml`, `infra/ansible/README.md`, `ci/tests/test_node_disk.py`, `ci/tests/test_k3s.py`.

**Tests.** A fixture where the mountpoint is non-empty and the label is absent fails before `wipefs`. A server play with `k3s-agent` enabled fails. A label fixture with an extra API node does not emit a delete.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_node_disk.py ci/tests/test_k3s.py`

### S3.5 — Point prep-node at the real schema check (their F06; my F1)

**Approach.** `run_env_schema` runs `ci/checks/env-schema` (or calls `schema.env.validate_env_tree`). A missing interpreter or a failed check becomes `PrepError`, so the existing handler prints `git diff`. Add a test that builds a fixture repo, points the function at it, and expects a schema error when a required key is removed.

**Files.** `tools/prep-node`, `ci/tests/test_prep_node.py`.

**Tests.** One test calls `run_env_schema` on a broken fixture and asserts `PrepError`. One test calls it on a valid fixture and asserts success.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_prep_node.py`

### S3.6 — Fail closed on multi-device Btrfs root (their F07; my F7)

**Approach.** When `findmnt` shows the root fstype is btrfs and the filesystem has more than one device, fail the data-disk play unless every member was enumerated and none of them is `data_disk.id`. Keep the two confirmations. Extend the README “Not checked” list only for the cases still open (mounted elsewhere, foreign mount, partition contents). Add the Btrfs case to `ci/tests/fixtures/node-disk/refusals.yml`.

**Files.** `infra/ansible/roles/node-disk/tasks/guards.yml`, `infra/ansible/README.md`, `ci/tests/fixtures/node-disk/refusals.yml`, `ci/tests/test_node_disk.py`.

**Tests.** A two-device Btrfs fixture whose second member is the data disk fails before wipe. A single-device root still reaches the confirmation assert.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_node_disk.py`

### S3.7 — Wait for the two webhooks; check enrollment after (their F08; my F3, F12)

**Approach.** Add Flux `healthChecks` for the Kyverno admission deployment and the cert-manager webhook deployment on `platform-controllers`, and keep every other HelmRelease without a health check. Leave `failurePolicy: Ignore`. Extend `tools/verify/p0a` so a bound PVC without `backup: none` and without a ReplicationSource fails the automatic section. The owner checklist is no longer an exit 0 by itself; that part is S3.10.

**Files.** `envs/env-01/flux/kustomizations/platform-controllers.yaml`, `tools/verify/p0a`, `ci/tests/test_flux_entry.py`, `ci/tests/test_verify_p0a.py`, `platform/backups/CONTRACT.md`.

**Tests.** The built platform-controllers object lists those two health checks and does not list the Traefik or VolSync HelmRelease. A fake kubectl with a bound PVC and no ReplicationSource makes `p0a` return 1.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_flux_entry.py ci/tests/test_verify_p0a.py`

### S3.8 — Tighten three CI identities (their F09; my F11, F19)

**Approach.** LoadBalancer allow-list matches the Traefik HelmChartConfig and the App DNS HelmRelease by kind and name, not by directory. `pvc-backup` requires the ClusterPolicy named `pvc-backup` and a generate rule that mentions ReplicationSource. Helm values that set a workload container and omit `resources` fail manifest-lint the way a pod spec already does. HelmChartConfig `valuesContent` images go through the digest walker. `_pvc_backup.py` imports `SCAN_ROOTS` and `SKIP_ENV_PREFIXES` from `_manifests.py` instead of copying them. Unknown `${…}` outside a comment fails env-schema or a small manifest walk against `resolve_cluster_vars` keys (my F8).

**Files.** `ci/checks/lib/_loadbalancer.py`, `_pvc_backup.py`, `_manifest_lint.py`, `_digest_pin.py`, `_manifests.py`, `ci/tests/test_loadbalancer.py`, `test_pvc_backup.py`, `test_manifest_lint.py`, `test_digest_pin.py`, `test_env_schema.py`.

**Tests.** The three seeds from this cross-check fail: extra LoadBalancer under `platform/traefik/`, empty Helm `resources`, decoy ClusterPolicy. A HelmChartConfig image without a digest fails. A quoted `${not_a_cluster_var}` fails. The existing seeded failures still fail.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_loadbalancer.py ci/tests/test_pvc_backup.py ci/tests/test_manifest_lint.py ci/tests/test_digest_pin.py ci/tests/test_env_schema.py`

### S3.9 — Reject non-mapping documents; close the VolSync spec (their F10)

**Approach.** `load_documents` records an error for a YAML document that is not a mapping and not null. Empty documents stay valid. The VolSync source and destination schemas set `spec.additionalProperties: false` and list the keys this repo writes (`sourcePVC`, `trigger`, `restic`, `paused`, and the destination fields `tools/restore` applies). The CRD stand-in keeps an open spec and its description says the spec is not validated.

**Files.** `ci/checks/lib/_manifests.py`, `ci/tests/fixtures/schemas/volsync.backube/*.json`, `ci/tests/fixtures/schemas/apiextensions.k8s.io/customresourcedefinition_v1.json`, `ci/tests/test_manifest_lint.py`, `ci/tests/test_flux_entry.py`.

**Tests.** A file whose only document is `[]` fails manifest-lint, digest-pin, and loadbalancer. A ReplicationSource with `spec.restic.notAField` fails kubeconform. The current enrollment and rest-server documents still pass.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_manifest_lint.py ci/tests/test_digest_pin.py ci/tests/test_loadbalancer.py ci/tests/test_flux_entry.py`

### S3.10 — Exit checks require structured proof (their F11; my F23)

**Approach.** Split the printed checklist from the exit code. `p0a` and `p0b` return 0 only when the automatic checks passed and a separate attestation file lists every owner item as `done` with a non-empty note. `recorded_checksum` requires a line `restore result: ok` after the PVC deletion, and `etcd_checksum_matches` requires two different labels (`k3s file` and `restored file`) with the same hash. A line that merely contains `restore` does not count. Update the backup contract’s P0b steps to those labels.

**Files.** `tools/verify/p0a`, `tools/verify/p0b`, `platform/backups/CONTRACT.md`, `ci/tests/test_verify_p0a.py`, `ci/tests/test_verify_p0b.py`.

**Tests.** The current “owner items remain” cases return 1. The failure transcript from this cross-check returns no checksum. A transcript with the two labels and `restore result: ok` returns the checksum.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_verify_p0a.py ci/tests/test_verify_p0b.py`

### S3.11 — Ansible preflight and lookup errors (their F12)

**Approach.** Move the k3s variable and architecture asserts into the opening assert play, still with `gather_facts` limited to architecture. `operator.yml` and `sops_age.yml` follow `instance.yml`: a non-zero rc is absence only when the output contains `NotFound`; any other error fails the play. When the operator Deployment exists, wait for Available. The chrony template notifies a handler that restarts chrony, and the handler is tagged `time`.

**Files.** `infra/ansible/site.yml`, `infra/ansible/roles/k3s/tasks/main.yml`, `infra/ansible/roles/flux-operator/tasks/operator.yml`, `sops_age.yml`, `infra/ansible/roles/base-os/tasks/main.yml`, `handlers/main.yml`, `ci/tests/test_k3s.py`, `ci/tests/test_flux_operator.py`, `ci/tests/test_base_os.py`.

**Tests.** A localhost play whose fake kubectl exits 1 with `connection refused` does not install the operator and does not create the secret. A fake that prints `NotFound` still bootstraps. A chrony task change records the restart handler. An arm fact fails in the first play.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_k3s.py ci/tests/test_flux_operator.py ci/tests/test_base_os.py`

### S3.12 — Secret identifiers, key mode, CA retention (their F13; my F6)

**Approach.** `seal` and `bootstrap-secrets` accept an env name and a secret name that are each one path segment (`env-` plus digits, and a lowercase secret name). The resolved file must stay under `envs/<env>/secrets`. The sops-age task fails when the key file mode is not `0600`. Add `kustomize.toolkit.fluxcd.io/prune: disabled` to the plaintext metadata of `cert-manager-ca.sops.yaml` and to the bootstrap document for that secret. Do not decrypt. In `platform/cert-manager/CONTRACT.md`, copy the one sentence from `envs/env-01/secrets/README.md:27`: replacing the CA secret does not reissue `wildcard-tls`; delete that Certificate to rotate the leaf.

**Files.** `tools/seal`, `tools/bootstrap-secrets`, `infra/ansible/roles/flux-operator/tasks/sops_age.yml`, `envs/env-01/secrets/cert-manager-ca.sops.yaml` (metadata only), `platform/cert-manager/CONTRACT.md`, `ci/tests/test_bootstrap_secrets.py`, `ci/tests/test_prune_guards.py`, `ci/tests/test_flux_operator.py`.

**Tests.** `seal("../ci", "evil", ...)` exits before `sops` is called. A 0644 key fixture fails the age task. The prune-guard test lists `cert-manager-ca`. A unit test does not need a live cert-manager for the contract sentence; `test_cert_manager.py` asserts the contract file contains the reissue sentence.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_bootstrap_secrets.py ci/tests/test_prune_guards.py ci/tests/test_flux_operator.py ci/tests/test_cert_manager.py`

### S3.13 — Write the hardware ceiling down (their F14, F15; my F9, F10, F22)

**Approach.** One paragraph in the deploy note: one control plane, one backup host, amd64 nodes, a `gpu` host must be NVIDIA `0x10de`, workers are the supported way to add a machine. Do not add a second server, an arm checksum, or a second GPU vendor in this PR.

**Files.** The deploy note from S3.17, `schema/CONTRACT.md`, `infra/ansible/README.md`.

**Tests.** `test_contract_md` or `test_env_schema` already walks contracts. Add an assert that `schema/CONTRACT.md` contains the amd64 and single control-plane sentences so a later edit cannot drop them silently. Skip this assert if it becomes a pure wording lock; the done check is then a grep in the PR evidence.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_contract_md.py ci/tests/test_k3s.py ci/tests/test_pci_gpu.py`

### S3.14 — State DNS and CA boundaries (their F16)

**Approach.** Document, in the App DNS and cert-manager contracts: one tenant; App DNS publishes every Ingress; Traefik is the default class; HTTP is not forced to HTTPS by this repo; any namespace can reference ClusterIssuer `internal-ca`; `app_dns_cluster_ip` is fixed for the life of the Service. Do not add an approval policy or a redirect in this PR.

**Files.** `platform/app-dns/CONTRACT.md`, `platform/cert-manager/CONTRACT.md`, `platform/traefik/CONTRACT.md`.

**Tests.** Contract-md still passes. No new behavior test, because the change is the sentence.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_contract_md.py ci/tests/test_app_dns.py ci/tests/test_traefik.py ci/tests/test_cert_manager.py`

### S3.15 — Mover resources on the generated source (their F17)

**Approach.** Set `moverResources` requests and a memory limit on the generated ReplicationSource, using the same shape as the etcd CronJob (CPU request, memory request, memory limit, no CPU limit). Add a contract sentence that every enrolled PVC fires on `backup_schedule` against one rest-server replica, and that staggering waits for a second PVC.

**Files.** `platform/backups/enrollment.yaml`, `platform/backups/CONTRACT.md`, `ci/tests/test_backup_enrollment.py`.

**Tests.** The policy’s restic mover resources include a memory limit and a CPU request and do not set a CPU limit.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_backup_enrollment.py`

### S3.16 — Stop publishing unused env keys; split Flux with the first service (their F18, F19; my F14, F17)

**Approach.** `cluster_vars_configmap` takes an allow-list of keys that platform manifests substitute (`dns_suffix`, `time_zone`, `backup_path`, `backup_schedule`, `backup_retention_*`, `app_dns_cluster_ip`, and the CIDRs the FluxInstance uses). `age_key_file`, `ssh_user`, `k3s_sha256`, `apps_git_repo_url`, and `llm_provider_host` stay in the env file and out of the ConfigMap. Mark Gatus, admin-gate, and the second code host as later in `docs/code-delivery/CONTRACT.md`, `platform/traefik/CONTRACT.md`, and `platform/testapp/CONTRACT.md`. When the first service PR lands, give that service its own Flux Kustomization and apply only the secret it needs. Until that PR, leave the placeholder ConfigMaps in place.

**Files.** `tools/lib/render_cluster_vars_configmap.py`, `envs/env-01/flux/generated/cluster-vars-configmap.yaml` (regenerated), `ci/tests/test_env_schema.py`, the three contracts.

**Tests.** The rendered ConfigMap data keys are exactly the allow-list. env-01’s committed file matches the renderer. A manifest that substitutes `ssh_user` fails the substitution check from S3.8.

**Done check.** `python3 tools/lib/render_cluster_vars_configmap.py --env env-01 | diff -u envs/env-01/flux/generated/cluster-vars-configmap.yaml -` and `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_env_schema.py ci/tests/test_contract_md.py`

### S3.17 — One deploy note (their F20; my F13, F20, F24, F21)

**Approach.** Add one page in this repo. It lists prerequisites that stay outside the cluster (router DNS forward of the env suffix, Tailscale or the router for remote access, Ubuntu, SSH, an age key, two Git repos, amd64). It lists the command order: `make deps`, `prep-node`, `bootstrap-secrets`, commit, `site.yml` with both inventories and `jarvis_cluster_vars`. It lists the env flux files to create and says identity and secrets are never copied. It includes the tag-table commands with `-i` and `-e`. It says `make ci-verify` needs `OWNER_NAME`, and `make help` prints that. It points at `schema/CONTRACT.md` for `k3s_sha256` and `app_dns_cluster_ip`. It says encryption uses the public key in `.sops.yaml` and decryption uses `SOPS_AGE_KEY_FILE` or `age_key_file`. It includes the S3.13 ceiling and the netplan note: a static pin moves other `/etc/netplan/*.yaml` aside. Replace the tag examples in `infra/ansible/README.md:44-46` with the same full command.

**Files.** `docs/` (one new page), `README.md`, `docs/README.md`, `Makefile`, `infra/ansible/README.md`, `ci/tests/test_contract_md.py` if the new page needs a contract, otherwise no new check.

**Tests.** A small test reads `make help` output and asserts it mentions `OWNER_NAME`. The Ansible README tag examples contain `-i` and `jarvis_cluster_vars`.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_contract_md.py` and `make help | grep OWNER_NAME`

### S3.18 — Guard categories without a second parser (their F21; my F25)

**Approach.** Keep `_cli_permissions.py` as the CI stand-in. `command_allowed` returns false when the command is non-empty and the allow list has no Shell entry. Add the mixed-case forms the comment at `:388-390` already names to the deny list in `.cursor/cli.json`, still as categories, not as a bypass recipe. Document in the check docstring that gitleaks has no `.gitleaks.toml` and that the age-key seed is the secret-scan proof. Do not add a Cursor binary.

**Files.** `ci/checks/lib/_cli_permissions.py`, `.cursor/cli.json`, `ci/checks/secret-scan` (comment only if a docstring is the wrong layer; prefer the existing check’s header), `ci/tests/test_cli_permissions.py`.

**Tests.** An allow list with no Shell entry does not allow a non-empty command. The dry-run corpus still passes against `.cursor/cli.json`. The planted age-key test still fails secret-scan.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_cli_permissions.py ci/tests/test_flux_entry.py` and `bash ci/checks/cli-deny` (exits 0 only when the corpus matches the file).

### S3.19 — Drop dead CI weight (their F22; delete-list items that survived)

**Approach.** Delete the `platform/backups/test/` line from `renovate.json`. Delete the three `scan_*(REPO) == []` asserts. In the fresh-cluster flux-operator test, require return code 0, and make the fixture’s chown succeed or skip that play in the fake. When `helm_binary_path` exists, compare its sha256 to the pin and reinstall on mismatch. Pin `pytest`, `PyYAML`, and `bcrypt` to the versions this environment already installed, in a follow-up commit in the same PR only if `pip freeze` is recorded in the PR evidence. Move `_kustomize_build` to `ci/tests/ansible_role_proof.py` or a new `ci/tests/support.py` only if that edit stays smaller than the tests it touches.

**Files.** `renovate.json`, `ci/tests/test_digest_pin.py`, `test_loadbalancer.py`, `test_manifest_lint.py`, `test_flux_operator.py`, `infra/ansible/roles/flux-operator/tasks/helm_cli.yml`, `requirements-dev.txt`.

**Tests.** Renovate config still loads. The seeded scanner tests still fail on bad fixtures. The fresh-cluster play returns 0. A helm fixture whose existing binary hash differs is replaced.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_renovate.py ci/tests/test_digest_pin.py ci/tests/test_loadbalancer.py ci/tests/test_manifest_lint.py ci/tests/test_flux_operator.py`

### S3.20 — Per-env kubeconfig path (their F23; my F15)

**Approach.** Write `$HOME/.kube/jarvis-<env>.yaml` from `jarvis_env`. Do not copy over `$HOME/.kube/config` and do not set `backup: true`. Print the `KUBECONFIG` value. The `127.0.0.1` rewrite stays, and the task fails when the fetched file has no `127.0.0.1:6443` URL so a different API address is not silently left in place.

**Files.** `infra/ansible/roles/kubeconfig/tasks/main.yml`, `infra/ansible/README.md`, `ci/tests/test_kubeconfig.py`.

**Tests.** The rendered destination ends with `jarvis-<env>.yaml`. A fixture whose server URL is not loopback fails. The default config path is absent from the task dest.

**Done check.** `OWNER_NAME="$OWNER_NAME" .venv/bin/pytest -q ci/tests/test_kubeconfig.py`

## 4. What I now think I got wrong

- **F26 is withdrawn** as a ranked defect. The script omits `apt-get update`, and this VM’s empty index made `make deps` fail. A current index installs without that update. Their verification note is the right call.
- **F2 was incomplete,** not wrong. Snapshotless `restore-all` also deletes the rebuild hold and exits 0. Hold deletion is not rolled back.
- **The placeholder delete sentence was too strong.** Empty kustomizations build. The files are still the live Flux objects until a real resource replaces them.
- **F15 understated `backup: true`.** The role keeps previous kubeconfig files next to `~/.kube/config`.

I do not retract F1, F3 through F25, or the decision to keep `failurePolicy: Ignore` until a verify check can see a missed PVC.
