# Cross-check — GPT-5.6 Sol against Grok 4.7

Target: `5561280d2c57e0b7d0d7d1a8c2eaa1a95cf9bdd8`.

This is one read-only cross-check pass. I did not use SSH, a live cluster, `kubectl`, secret decryption, or an age key. “Confirmed” means I independently reproduced the material claim; “partly confirmed” identifies the narrower claim the code proves. Suggested fixes are directions for later coding PRs, not changes made here.

## 1. Reproduced evidence

These are the main command results used below. Source quotations in the verdicts are additional evidence.

### E1 — broken `prep-node` checker

```text
$ test ! -e tools/checks/env-schema && echo tools/checks/env-schema=MISSING
tools/checks/env-schema=MISSING
$ test -x ci/checks/env-schema && echo ci/checks/env-schema=EXECUTABLE
ci/checks/env-schema=EXECUTABLE
```

`tools/prep-node:1002-1007` calls `[repo / "tools" / "checks" / "env-schema"]`; `run_env_schema` has no test reference. Calling it produced:

```text
FileNotFoundError: [Errno 2] No such file or directory:
'/workspace/tools/checks/env-schema'
```

### E2 — restore behavior

`tools/restore:180-187` sets `copyMethod: Direct` and `enableFileDeletion: True`. `tools/restore:709-722` catches `Fail`, deletes only the binder pod, prints what remains, and re-raises; it does not catch `KeyboardInterrupt`. The Make target supplies `APP`, not an env or context. The contract explicitly says failure remains quiesced, which is a deliberate fail-closed choice.

An independent restore harness for a held PVC with no snapshot printed:

```text
SKIP no snapshot demo/drill
HOLD lifted
return=0 hold_deleted=True
```

### E3 — backup and disk boundaries

`platform/backups/enrollment.yaml:7-10` says:

```yaml
webhookConfiguration:
  failurePolicy: Ignore
```

The same policy has `generateExisting: true` at lines 39 and 70, so missed enrollment should catch up when Kyverno is healthy. Lines 44-51 copy the same three credential values into every generated Secret. `infra/ansible/roles/node-disk/vars/main.yml:2` fixes the mount at `/var/lib/jarvis`; schema validation only requires `backup_path` and `local_path_data` to be absolute. `envs/env-01/inventory/hosts.yml` deliberately has no `data_disk`, while `platform/backups/CONTRACT.md` states same-disk storage is not disk-failure protection and a rebuilt-node etcd restore needs the original token.

### E4 — scanner boundaries

Temporary manifests run through the real scanner functions produced:

```text
non_mapping digest= [] lint= []
helmchartconfig digest= [] lint= []
helmrelease_missing_probes digest= [] lint= []
arbitrary_split_image digest= [] lint= []
canonical_image_map digest=
  ['services/x/x.yaml: HelmRelease x: image nginx is not pinned by digest']
```

A manifest containing `${not_a_cluster_var}` also produced no substitution-specific violation. `_manifests.py:40-42` appends only mapping YAML documents. `_pvc_backup.py:92-116` accepts any listed `ClusterPolicy`, not specifically `pvc-backup`. `_loadbalancer.py:16` allows either whole directory prefix rather than exact objects.

### E5 — fixed topology and pins

```text
infra/ansible/site.yml:17:
  groups['control_plane'] | default([]) | length == 1
infra/ansible/roles/k3s/templates/config.yaml.j2:3: cluster-init: true
infra/ansible/roles/k3s/tasks/main.yml:30:
  ansible_facts['architecture'] == 'x86_64'
infra/ansible/tasks/assert-gpu.yml:29:
  select('equalto', '0x10de')
```

Versions and schedules are currently aligned but separately maintained:

```text
tools/versions.env:33: kubectl_version=1.36.5
envs/env-01/values/cluster-vars.yaml:16: k3s_version: v1.36.5+k3s1
infra/ansible/roles/k3s/defaults/main.yml:8: "0 */12 * * *"
platform/backups/etcd-snapshots.yaml:12: "5 */12 * * *"
```

### E6 — controller/config ordering and future config

`platform-controllers.yaml:12` deliberately omits HelmRelease health checks. `platform-configs.yaml:14-15` has only `dependsOn: platform-controllers`. Required `apps.yaml` and `hermes.yaml` values are validated but have no runtime consumer. Both app and service Flux entries use `sourceRef.name: flux-system`, and `apps_git_repo_url` is unused. The configured future artifacts are absent:

```text
ci/checks/admin-gate=absent
services/hermes=absent
docs/build-agent=absent
```

### E7 — kubeconfig, restore owners, and verification

`infra/ansible/roles/kubeconfig/tasks/main.yml:25-48` rewrites exactly `https://127.0.0.1:6443`, then copies to `~/.kube/config` with `backup: true`. Current k3s emits that URL, so the URL concern is a future compatibility guard; clobbering the default file is current behavior.

`tools/restore:409-429` scales only Deployments and StatefulSets. `ci/tests/test_restore.py:126-127` returns no objects for both kinds. `tools/verify/p0a:228-233` returns zero after printing that owner items remain. A synthetic P0b transcript containing `restore failed` and duplicated source-only hash lines still returned:

```text
recorded_checksum=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

### E8 — disk and network destructive paths

`node-disk/tasks/main.yml:51-72` runs `wipefs -a` and forced filesystem creation after the existing confirmations. `infra/ansible/README.md:54` explicitly says mounted-elsewhere, foreign/non-empty mountpoints, partition contents, and secondary multi-device Btrfs members are not checked. `tools/prep-node:613-616` moves every other `/etc/netplan/*.yaml` to `.bak`.

### E9 — docs and local verification

No `OWNER_NAME` reference exists in `Makefile`, `ci/ci-local-verify`, or `infra/ansible/README.md`, although `core-names` correctly fails when it is unset. The operator-tag table shows partial commands; line 60 later says to pass normal inventory and vars. The root README points to `<owner>/project-plans`, and the new-env note says “add a flux tree” without defining its files.

### E10 — empty Kustomization and duplicate structural tests

The other audit says the placeholder ConfigMaps are load-bearing because their Flux paths would be empty. I removed no repo file; I tested the claimed Kustomize constraint in a temporary directory:

```text
$ kustomize build <dir-containing-Kustomization-with-resources-empty>
empty_kustomization_rc=0
```

Deleting only the resource files while leaving their names in `kustomization.yaml` would fail, but an intentionally empty resources list builds. Separately, root-tree rescan tests run scanners over the same tree that `ci/run-checks` scans, and `test_flux_operator.py` locks exact play order. `rulebooks/coding-agents.md:22` says not to add tests on ordering/file shape or tests that only rerun a check against the tree.

### E11 — dependency setup

On this VM, before refreshing apt metadata:

```text
apt-cache policy python3-venv
Candidate: (none)
apt-get install failed (exit 100)
```

After `apt-get update`, `tools/install-deps` succeeded. The script's apt branch has `apt-get install` but no update.

## 2. Verdicts on the other audit's findings

Each confirmed portion includes a coding-agent handoff: approach; files; tests; and an offline/CI done check.

### F1 — `prep-node` calls a missing checker: **confirmed**

Evidence: E1 and `tools/prep-node:1002-1013,1075-1087`. The failure occurs after remote and local mutation and escapes the `PrepError` handler.

**Suggested fix:** invoke the canonical schema API or `ci/checks/env-schema`; touch `tools/prep-node` and `ci/tests/test_prep_node.py`; add full orchestration fixtures for success and schema failure, including diff output; done when those tests pass and a mocked happy path reaches `Recorded the host`.

### F2 — restore is ambient, destructive, and interrupt handling is incomplete: **partly confirmed**

Evidence: E2. Ambient context, file deletion, and missing interrupt undo output are confirmed. The implication that a normal `Fail` should resume workloads is refuted by the contract and by recovery safety: auto-resuming a partially copied volume would fail open.

**Suggested fix:** bind restore to an explicit env/per-env kubeconfig and verify the cluster env marker before mutation; route `Fail` and interrupt through one reporting path while preserving quiescence; touch `tools/restore`, `Makefile`, the backup contract, and restore tests; add wrong-context, interrupt, and partial-copy fixtures; done when all refuse or print exact undo state without auto-resume.

### F3 — enrollment fails open with no runtime verification: **partly confirmed**

Evidence: E3. The silent runtime gap is confirmed; `generateExisting` is an intended catch-up mechanism. I do not confirm that `failurePolicy: Ignore` is itself wrong: changing it to `Fail` trades the gap for PVC-creation outages.

**Suggested fix:** add a read-only runtime check that every bound non-opt-out PVC has its generated Secret and ReplicationSource, preferably in P0a; touch `tools/verify/p0a`, its tests, and backup/P0a contracts; include missing, paused, and opted-out fixtures; done when the verifier fails for an unenrolled bound PVC and passes after generated objects appear.

### F4 — fixed disk mount is unrelated to configurable data paths: **confirmed**

Evidence: E3. A valid env can place both paths outside `/var/lib/jarvis`.

**Suggested fix:** make the mount path one documented env value, or enforce both paths beneath the one product constant whenever `data_disk` is set; touch `schema/env.py`, schema contract/tests, node-disk vars/templates, and docs; seed mismatched and no-data-disk envs; done when schema rejects only unsafe layouts and the role's mount/drop-in derive from the same value.

### F5 — the shipped backup cannot recover root-disk loss: **confirmed**

Evidence: E3. Same-disk source, restic, etcd snapshots, and absent token escrow are explicitly documented.

**Suggested fix:** define a supported off-host/offline target and separately escrow the k3s token and recovery credentials; touch backup manifests/contracts, env schema/docs, Ansible recovery flow, and P0b fixtures; test that a serious profile cannot resolve backup storage to the source failure domain and that rebuilt-node inputs are enumerated; done with an offline recovery fixture plus the documented live drill left as reviewer evidence.

### F6 — CA replacement does not trigger leaf reissuance: **partly confirmed**

Evidence: `envs/env-01/secrets/README.md:27-31` states the behavior and manual deletion; `platform/cert-manager/CONTRACT.md` omits it. I did not reproduce cert-manager v1.21.3 behavior against a live controller, so the operational claim remains upstream/live evidence rather than independently proven behavior.

**Suggested fix:** first put the known rotation sequence and availability window in `platform/cert-manager/CONTRACT.md`; if automation is desired, make CA rotation an explicit operation that reissues `wildcard-tls`; update `test_cert_manager.py` to require the contract/trigger invariant; done when an offline manifest test proves the trigger relationship and the documented live chain check is explicit.

### F7 — destructive disk guards omit mounted/foreign/Btrfs cases: **confirmed**

Evidence: E8 and the repository's own “Not checked” list.

**Suggested fix:** fail before confirmation if the disk or children are mounted anywhere, contain foreign filesystems/partitions, or are an unenumerated Btrfs member; touch node-disk guards/tasks, refusal fixtures, tests, and docs; add mounted-child, foreign-signature, and multi-device-root cases; done when each fixture stops before `wipefs` and a clean disk still reaches the format plan.

### F8 — no CI check enforces `${...}` keys: **confirmed**

Evidence: E4. Strict Flux substitution detects this only after merge.

**Suggested fix:** add one deployment-aware scanner that extracts unescaped substitutions only from Flux-substituted roots and checks them against the schema's known rendered keys; touch `ci/checks`, `ci/rule-checks.txt`, shared scanner code, and seeded tests; include unknown, known, `$${...}`, and substitution-disabled fixtures; done when `make ci-verify` rejects the unknown key before a cluster reconcile.

### F9 — exactly one server: **confirmed**

Evidence: E5 plus `schema/env.py:674-681`. This is a supported-topology ceiling, not an accidental count.

**Suggested fix:** document the singleton server/backup profile now rather than adding a fake HA flag; when node-loss HA is required, use a separate design for odd server membership, joining/replacement, token handling, and backup placement; touch schema/deploy contracts now, and schema/Ansible/k3s tests only in that later PR; done now when adopter docs and schema report the same ceiling.

### F10 — k3s nodes are amd64-only: **confirmed**

Evidence: E5.

**Suggested fix:** add architecture-keyed checksums and select the correct release asset from Ansible facts; touch env schema/values, k3s role, Renovate policy, and k3s tests; seed amd64, arm64, missing-hash, and unsupported-arch cases; done when both supported architectures verify their own checksums and unsupported ones fail before mutation.

### F11 — chart boundaries weaken digest/probe checks: **confirmed**

Evidence: E4. Nuance: a canonical HelmRelease `image: {repository, tag}` map is detected; HelmChartConfig and arbitrary sibling fields are not, and missing Helm probes cannot be inferred from arbitrary values.

**Suggested fix:** scan parseable HelmChartConfig image values and introduce explicit chart-level declarations for images/probe expectations where templates are unavailable; touch both scanners and fixture tests, plus Traefik's contract if the k3s pin is accepted as its image provenance; done when seeded chart image drift fails without pretending arbitrary Helm values are rendered pods.

### F12 — Flux dependency does not mean controller readiness: **confirmed**

Evidence: E6. The omission is deliberate, but the resulting first-install/upgrade race is real.

**Suggested fix:** health-check only controllers/webhooks required by dependent configs, or explicitly contract the retry window where availability is preferred; touch env Flux Kustomizations, relevant controller contracts, and `test_flux_entry.py`; add readiness dependency fixtures; done when configs cannot report ready before required admission/webhook endpoints, or the accepted race is consistently documented and verified.

### F13 — no repository-only first-deploy path: **confirmed**

Evidence: E9 and E6.

**Suggested fix:** add one repository-owned, tested adopter runbook and a scaffold or generated file list for a new env's Flux entry; touch root/docs READMEs, schema contract, and a small scaffold/test if generated; test links, required file output, and identity-free examples; done when a clean offline walkthrough identifies every input and `make ci-verify` validates the generated tree.

### F14 — Hermes/apps config and secrets are premature and shallow: **confirmed**

Evidence: E6. Validation currently proves shapes that no deployed consumer uses.

**Suggested fix:** make component config optional and component-owned until enabled; with the first component, define its ConfigMap/Secret pipe and then validate its real schema; touch `schema/env.py`, env values/bootstrap, component Flux entry, and schema/Flux tests; done when a foundation-only env needs no future values and an enabled component cannot omit required runtime fields.

### F15 — kubeconfig has one destructive destination and one rewrite shape: **partly confirmed**

Evidence: E7. Default kubeconfig replacement and credential-bearing backups are confirmed. The rewrite is narrow, but it matches current k3s output; an alternate URL is a compatibility risk, not a current failure.

**Suggested fix:** write a per-env kubeconfig and make default-file merge/replacement explicit; assert exactly one server URL was rewritten; touch kubeconfig role/defaults, Make wrappers/docs, and kubeconfig tests; seed existing multi-context, expected URL, and unexpected URL cases; done when existing `~/.kube/config` is unchanged and tools select the intended env file.

### F16 — restore handles only two owner kinds and tests scale none: **confirmed**

Evidence: E7.

**Suggested fix:** preflight all PVC-mounting pods and owners before scaling; support selected controllers or reject unsupported ones before mutation; make the fake return a real Deployment/StatefulSet and assert down/up behavior; touch `tools/restore` and `test_restore.py`; done when unsupported Job/DaemonSet/bare-Pod fixtures fail before quiescing and supported owners restore their replica counts.

### F17 — contracts describe absent work: **confirmed**

Evidence: E6: `admin-gate`, Gatus, Hermes deployment, and build-agent roots are absent while contracts speak as though some exist.

**Suggested fix:** separate implemented guarantees from explicitly deferred offers and remove phase history from proof sections; touch the affected contracts and contract-check tests; add assertions that named required checks/paths exist; done when every present-tense proof target resolves in the tracked tree.

### F18 — related pins are split: **partly confirmed**

Evidence: E5. The values are currently aligned, and the etcd-copy schedule has a drift test. This is maintenance brittleness, not a current runtime defect; `cluster.local` is a deliberately fixed profile today.

**Suggested fix:** add a k3s/kubectl compatibility assertion and keep fixed cadence/domain documented until an env needs configurability; touch version/schema tests and contracts, not runtime config prematurely; seed compatible/incompatible versions; done when drift fails CI and fixed-profile values have one declared owner.

### F19 — manifest roots are duplicated: **confirmed**

Evidence: `_manifests.py:9-10` and `_pvc_backup.py:21-23` define the same roots/skips.

**Suggested fix:** import the shared constants while leaving backup-only roots local; touch `_pvc_backup.py` and focused tests; compare scanner output before/after on fixtures; done when all checks scan the same common roots and `make ci-verify` is behaviorally unchanged.

### F20 — `OWNER_NAME` prerequisite is undocumented locally: **confirmed**

Evidence: E9. The check's fail-closed behavior is correct.

**Suggested fix:** document the environment/repository variable in `make help` and the adopter runbook without a personal default; touch `Makefile` and docs; add a help-text assertion only if this repo treats help as an interface; done when a fresh user sees the prerequisite before running verification and unset still fails.

### F21 — static netplan disables unrelated files: **confirmed**

Evidence: E8. The prompt and `netplan try` reduce, but do not remove, multi-interface risk.

**Suggested fix:** identify files that configure the selected interface and only replace/conflict-manage those; retain backup and `netplan try`; touch `tools/prep-node`, prep-node tests, and networking docs; use multi-NIC/same-NIC/rejection fixtures; done when an unrelated interface file remains active and rejected changes restore exact originals.

### F22 — `gpu` means NVIDIA: **confirmed**

Evidence: E5.

**Suggested fix:** either rename the capability to `nvidia_gpu` now or add explicit accelerator vendor/capability only with the first consuming service; touch schema/inventory/assert task and PCI tests; done when naming, scheduler labels, and accepted hardware express the same support matrix.

### F23 — P0a returns success with owner work outstanding: **confirmed**

Evidence: E7. This matches my F11.

**Suggested fix:** separate an automatic readiness command from a final attestation command, bind both to an env marker, and include runtime backup enrollment; touch P0a, Make targets/contracts, and verifier tests; seed outstanding, complete, wrong-env, and unenrolled states; done when only the clearly named automatic command can pass with owner attestations outstanding.

### F24 — tag examples omit required arguments: **partly confirmed**

Evidence: E9. The table commands are incomplete, but prose immediately below says to pass the same inventory and vars as a normal run.

**Suggested fix:** replace partial commands with suffixes to append to the canonical command, or show complete commands; touch only `infra/ansible/README.md`; optionally run a docs command-shape test; done when every copyable command names both inventories and cluster vars.

### F25 — guardrail coverage has category gaps: **partly confirmed**

Evidence: the command guard is a local, version-specific parser clone with default-allow categories; gitleaks uses pinned upstream rules. I confirm the parser-assurance problem. I do not confirm a repository secret-scan defect merely because generic detectors cannot detect every encoding, and duplicating the full corpus in pytest would not independently validate the parser.

**Suggested fix:** make an integration test against the actual pinned permission engine authoritative if it exposes a stable interface; otherwise pin/version the modeled semantics and keep category fixtures. Add custom gitleaks rules only for a concrete missed repository secret shape; touch `_cli_permissions.py`, CLI config/check/tests, and possibly gitleaks config; done when the runtime engine and modeled policy agree on the category corpus.

### F26 — apt metadata is not refreshed: **confirmed**

Evidence: E11.

**Suggested fix:** run one `apt-get update` immediately before the conditional install, while preserving command failure; touch `tools/install-deps` and its tests; use a fake apt log to assert update-before-install and no update when packages are present; done when the stale-index fixture succeeds and an update failure remains fatal.

## 3. Verdicts on the other audit's proposals

### P1 — generate/scaffold the env Flux entry: **confirmed**

Evidence: E6/E9; the only complete tree is env-01 and the new-env instructions do not define it.

Direction: prefer a scaffold or fully generated output with a drift test, while keeping the per-env SOPS resource list explicit data. Touch the generator/scaffold, env docs, Flux templates, and fixture tests; done when a second synthetic env can be created and validated without copying env-01 identity.

### P2 — one Flux Kustomization per service plus selective health: **partly confirmed**

Evidence: services/apps are currently single prune/reconcile boundaries, and E6 confirms missing controller readiness. Mechanically creating one Kustomization per trivial object would add noise.

Direction: split only independently operated/failing components as they land, and health-check only dependencies later configs call. Test failure isolation and dependency readiness; done when one component's bad fixture does not block or prune an unrelated component.

### P3 — tie disk, paths, and rebuild story together: **confirmed**

Evidence: E3. This is the smallest coherent correction to F4/F5.

Direction: use one schema-owned storage placement model with explicit “same failure domain” semantics and token escrow requirements. Test data-disk, root-disk test profile, and off-host recovery profiles; done when every profile's recovery claim follows from validated placement.

### P4 — architecture-specific checksums: **confirmed**

Evidence: E5. It serves realistic mixed home hardware.

Direction: key checksums by normalized architecture and fail before download for missing pins; tests and done check are those under F10.

### P5 — leave one control plane for now: **partly confirmed**

Evidence: E5 confirms the present singleton is internally consistent. Waiting is appropriate, but the availability ceiling must be explicit and the later design must cover membership, token recovery, and an odd server count.

Direction: documentation now; a separate large design only when node-loss HA is an actual requirement. Done now when schema and adopter docs state the same supported profile.

### P6 — keep router DNS and Tailscale outside the cluster: **confirmed**

Evidence: this is an explicit audit-brief requirement and the manifests expose only LAN/tailnet-oriented services.

Direction: document router forwarding, tailnet access, and client CA trust as external prerequisites; no component or dependency. A docs check should ensure the adopter runbook names all three.

## 4. Verdicts on the other audit's delete-list claims

### “Nothing tracked should be deleted as unused”: **refuted**

Other evidence: its search found every current file referenced or intentionally reserved. Reproduced counterevidence: E10 shows placeholder objects are not required by Kustomize, the Renovate rule still names removed `platform/backups/test/`, root-tree rescan tests duplicate the CI tree scan, and exact play-order tests violate the repository's own test rule. Those are concrete deletion candidates, even though deletion must update references in the same PR.

### Placeholder ConfigMaps are load-bearing: **partly confirmed**

Other evidence: `apps/kustomization.yaml` and `services/kustomization.yaml` currently reference the files, so deleting only the files breaks builds. Counterevidence beside it: E10 shows `resources: []` builds successfully. The ConfigMaps are not operationally load-bearing; a deletion PR must remove both files and their resource entries.

### `envs/env-00` should stay: **confirmed**

Evidence: schema tests use it as the every-key/every-role, no-secrets example. It is a useful canary, not a deployable env. No deletion suggested.

### Future `EXPLICIT_COMPONENT_ROOTS` entries should stay: **confirmed**

Evidence: `_contract_md.py` deliberately ignores absent paths and begins enforcing `CONTRACT.md` when they appear. This is a small future-root policy, not dead runtime code. If names change before those components land, update the list then.

### The unused `storage` role should stay: **partly confirmed**

Evidence: schema/inventory deliberately support it, but no scheduler consumes it today. Keeping it is a reasonable declared capability; calling it load-bearing would be wrong. Reassess with the first storage service, and test that its selector matches the schema role.

### `platform/testapp` should stay until replaced: **confirmed**

Evidence: it is the current ingress/DNS/TLS canary and has dedicated tests. Delete it only in the PR that installs an equivalent end-to-end health target and moves those tests.

### `${backup_path}/volsync-test` is outside Git: **confirmed**

Evidence: it appears only as an owner cleanup instruction in the backup contract. It is not a tracked delete-list item.

### “No duplicate test file should be deleted”: **refuted**

Other evidence: the audit viewed exact-order and prune assertions as useful rules. Counterevidence beside it: E10 and `rulebooks/coding-agents.md:22` explicitly reject exact ordering/file-shape tests and tests that merely rerun a check over the tree. Delete root-rescan cases after retaining fixture behavior tests; replace exact play ordering with dependency/behavior assertions.

## 5. Reassessment of my findings

“Missed” means the other audit did not identify the material issue; it does not weaken the independently reproduced finding.

| My finding | Other audit | Reassessment |
|---|---|---|
| F01 snapshotless restore lifts hold | missed | Confirmed by E2; unchanged. |
| F02 hold deletion is non-atomic | missed | Confirmed: hold is deleted before source unpause is proven. |
| F03 same-hardware recovery gap | confirmed by F5/P3 | Confirmed. |
| F04 shared backup credentials | missed | Confirmed by E3. |
| F05 node/role/disk lifecycle | partly confirmed by F4/F7/F9 | Confirmed; transition and decommission cases were missed. |
| F06 broken `prep-node` | confirmed by F1 | Confirmed. |
| F07 disk guard gaps | confirmed by F7 | Confirmed. |
| F08 fail-open readiness/enrollment | confirmed by F3/F12 | Finding stands, but my “use `Fail`” direction was too categorical; use runtime enrollment/readiness unless outage policy explicitly chooses fail-closed admission. |
| F09 seeded CI gaps | partly confirmed by F8/F11 | Confirmed with narrower wording: canonical HelmRelease image maps work; arbitrary sibling fields, chart config, probes, broad allow-lists, and policy identity remain gaps. |
| F10 validation skips documents/CRD fields | missed | Confirmed. Full Kustomize tests may catch referenced malformed files, so the effect is scanner false confidence, not universal CI acceptance. |
| F11 phase verifiers lack proof | partly confirmed by F23 | Confirmed; P0b false-positive proof was missed. |
| F12 Ansible fail-closed/convergence gaps | missed | Confirmed. |
| F13 secret/CA lifecycle boundaries | partly confirmed by F6 | Confirmed with correction: CA ciphertext is recoverable from Git with the age key; missing prune retention risks outage/lifecycle surprise, not necessarily permanent key loss. |
| F14 singleton topology | confirmed by F9/P5 | Confirmed; my original direction was too eager. Document the ceiling now and defer HA implementation until needed. |
| F15 amd64/NVIDIA hardware limits | confirmed by F10/F22 | Confirmed. |
| F16 DNS/ingress/CA boundaries | missed | Confirmed. |
| F17 backup scaling/concurrency | missed | Confirmed. |
| F18 global/unused/shallow env config | confirmed by F14/F18 | Confirmed. |
| F19 coupled product source/boundaries | confirmed by F14/P2 | Confirmed. |
| F20 no adopter path | confirmed by F13 | Confirmed. |
| F21 parser-clone assurance | confirmed by F25 | Confirmed; generic gitleaks limitations are not a separate concrete defect. |
| F22 dependency/test drift | partly confirmed by F19/F25/F26, contradicted on deletion | Confirmed; the “no redundant tests” conclusion is wrong for tree rescans/exact order. |
| F23 kubeconfig clobber | confirmed by F15 | Confirmed; alternate URL risk is secondary. |

### Suggested fixes for confirmed findings from my file

Where the other audit confirms the same issue, its detailed handoff above applies: my F03→other F5; F06→F1; F07→F7; F08→F3/F12 with the correction above; F09→F8/F11 plus exact-object/policy checks; F14→F9; F15→F10/F22; F18→F14; F19→P2 plus a real apps source; F20→F13; F21→F25; F23→F15. The remaining confirmed findings need these fix PRs:

- **F01:** make “held and not restored” fatal unless a repository-specific retire/accept-empty acknowledgement exists. Touch `tools/restore`, backup contract, P0b, and restore tests. Test held/no-snapshot, unheld/new PVC, retired repo, and successful restore. Done when no-snapshot keeps the hold and returns nonzero.
- **F02:** preflight every expected source before deleting the hold and recreate the exact hold if any post-delete unpause fails. Touch restore state handling/tests and the contract. Test source disappearance and API failure at each transition. Done when every failure either retains or restores the barrier.
- **F04:** issue repository/prefix-scoped writer credentials or separate append-only writers from prune/maintenance identity. Touch enrollment, bootstrap secret shape, rest-server/storage auth, restore tooling, contracts, and tests. Seed cross-repository access denial. Done when one PVC mover cannot list, mutate, or prune another PVC/etcd repository.
- **F05:** reject mode, disk, and inventory-removal transitions during normal convergence; provide explicit migrate/decommission operations. Touch k3s/node-disk guards, labels, docs, and role fixtures. Test server↔agent, root↔data-disk, and removed-node cases. Done when ordinary `site.yml` stops before mutation and explicit workflows prove drain/revoke/migrate state.
- **F10:** reject non-mapping documents in deployment roots and vendor/generate exact pinned CRD schemas. Touch `_manifests.py`, schema fixtures, and scanner/Kustomize tests. Seed list/scalar YAML and misspelled VolSync fields. Done when each fails and intentional data-only YAML is explicitly excluded.
- **F11:** define structured proof records and separate automatic checks from final attestation for both P0a/P0b. Touch verifier tools/contracts/tests. Seed failure text, duplicated source hashes, missing owner items, and valid source/restored pairs. Done when only exact labeled evidence passes.
- **F12:** run all-host variable/architecture preflight before mutating plays; distinguish API NotFound from transport/RBAC errors; verify controller readiness; add Chrony notification. Touch `site.yml`, Flux/base-os roles, handlers, and Ansible fixtures. Done when malformed inputs make zero changes, API errors fail, and second run is idempotent.
- **F13:** constrain env/name path components and resolved parents, enforce private-key regular-file ownership/mode, and define CA Secret retention/rotation. Touch seal/bootstrap/SOPS helpers, Ansible secret task, CA manifest/contracts, and tests. Seed traversal, symlink/mode, prune, and rotation cases. Done when no write escapes the selected env and unsafe key metadata fails before use.
- **F16:** declare immutable/migrated App DNS ClusterIP behavior, intended ingress-class/HTTPS policy, CA requester boundaries, and rotation. Touch app-dns/Traefik/cert-manager manifests/contracts and tests. Seed wrong class, unauthorized namespace/SAN, HTTP policy, and ClusterIP change cases. Done when each supported boundary is enforced or explicitly single-tenant.
- **F17:** add mover resources, schedule staggering/concurrency control, and server/cache capacity signals before the next stateful service. Touch enrollment/rest-server/schema/contracts and backup tests. Model multiple PVCs. Done when generated jobs have bounded resources and cannot all start on one identical schedule without an explicit limit.
- **F22:** lock Python dependencies, remove stale Renovate paths and redundant tree/order tests, require successful behavior fixtures, and move shared test helpers to support code. Touch requirements/workflow, Renovate, and affected tests. Done with a clean locked install, fixture-based failure proofs, no duplicate whole-tree scans, and full verification green.

## 6. Net result

The audits strongly agree on the broken adopter path, `prep-node`, same-disk recovery limits, disk safety gaps, fixed hardware/topology, shallow future config, controller readiness, kubeconfig clobbering, and weak final attestations. The other audit adds useful concrete operator/documentation findings. My audit adds the highest-risk missed restore-hold failures, credential blast radius, Ansible/secret lifecycle failures, backup scaling, and DNS/CA boundaries.

The two material corrections to my first report are:

1. Do not change backup admission to `failurePolicy: Fail` by default; add runtime enrollment verification/readiness and make availability policy explicit.
2. Do not call the encrypted CA bootstrap object irrecoverable; Git ciphertext plus the age key can recover it. The missing retention policy still creates avoidable outage and lifecycle risk.
