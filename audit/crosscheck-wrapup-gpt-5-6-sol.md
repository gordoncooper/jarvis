# Cross-check: wrap-up audit — GPT-5.6 Sol

Audit target: `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1`

Inputs:

- `findings-wrapup-grok-4-7.md` (findings G-F01–G-F21)
- `findings-wrapup-claude.md` (findings C-F01–C-F39 and C-T01–C-T20)
- `audit/findings-wrapup-gpt-5-6-sol.md` (findings O-F01–O-F25)

“G”, “C”, and “O” prefixes below disambiguate the three reports. The target tree is unchanged by this audit; only audit Markdown exists after the target commit.

## Method and reproduced evidence

I read every other-auditor finding and delete item, then checked the cited source and nearby tests. I used no SSH, live `kubectl`, SOPS decryption, age private key, or host under `bench.lan`. Command-guard weaknesses are named only by category.

Evidence labels used in the verdict tables:

| Evidence | Independently reproduced result |
| --- | --- |
| E01 — recovery boundary | `platform/backups/CONTRACT.md:174-192` says the reset uses the local k3s snapshot, not restic; a rebuilt node needs the original server token; same-node restore is the scope; same-host backups do not cover disk loss; an older restic-only etcd snapshot has no owner-run path. `infra/ansible/roles/k3s/tasks/token.yml:23` reads `/var/lib/rancher/k3s/server/node-token`; `platform/backups/etcd-snapshots.yaml:80` backs up `/snapshots` only. |
| E02 — age/restic custody | `.sops.yaml` has one recipient per env rule and the seven env-01 ciphertexts each have one recipient in metadata. `tools/bootstrap-secrets:261-268` creates a fresh random repository password and REST password. The only rotation sentence, `envs/env-01/secrets/README.md:25`, says to delete and regenerate the file. |
| E03 — prep-node ordering | `tools/prep-node:968-986` performs SSH/key, sudo, hostname, package, local env, and age-key work before `run_env_schema`. `docs/deploy.md:36-37` puts prep-node before adding a missing SOPS creation rule. |
| E04 — duplicated env facts | `envs/env-00/flux/flux-instance.yaml:24-27` contains url/ref/path independently of cluster-vars. `schema/env.py` does not compare them or the generated ConfigMap. The comparisons exist only in `ci/tests/test_flux_entry.py:183-191,228-229`. |
| E05 — owner/template/docs | `schema/env.py:535-539` rejects different owners across non-example envs. `envs/env-00` has no `secrets/README.md`, while `docs/deploy.md:77` points to that file. `docs/README.md:29-34` lists env-01 docs, not the template equivalent. |
| E06 — command guards | The checked-in matcher is case-sensitive (`ci/checks/lib/_cli_permissions.py:12`), while the Secret-read deny corpus covers only two case forms. A safe helper probe reproduced an unblocked mixed-case resource kind. The lists also have no category rule for cluster-writing repo entrypoints or reading the generated admin kubeconfig. No usable bypass string is recorded here. |
| E07 — backup enrollment/trust | `platform/backups/enrollment.yaml:12` is `failurePolicy: Ignore`; lines 47-53 copy one global restic credential into every generated Secret; lines 96-108 mark every enrolled PVC namespace for privileged movers. `platform/kyverno/rbac.yaml:12-15` grants cluster-wide Secret get/list/watch/create/update/patch/delete. |
| E08 — service/app path | Only the two `platform-configs` Kustomizations contain `decryption:`. Services/apps always use the main `flux-system` source and `./services`/`./apps`. Searches found `apps_git_repo_url` and `app_hosting_enabled` only in env/schema/test surfaces, not reconciliation. |
| E09 — controller ordering | `envs/*/flux/kustomizations/platform-controllers.yaml` waits for cert-manager and Kyverno only. `platform/kustomization.yaml` also installs VolSync, but the next stage can start before its HelmRelease is Ready. |
| E10 — GPU/topology | `infra/ansible/tasks/assert-gpu.yml` checks only NVIDIA PCI vendor `0x10de`; no `nvidia.com/gpu`, device plugin, GPU Operator, or `RuntimeClass` exists. Schema allows exactly one control plane and only amd64. |
| E11 — OS/time | `50unattended-upgrades.j2` has an empty `Allowed-Origins`; `20auto-upgrades.j2` disables unattended upgrades; `base-os/tasks/main.yml:92` stops the timer. No patch procedure was found. Every Chrony pool gets `nts` in `chrony.conf.j2:8-9`. |
| E12 — placement/labels | `tools/restore:561-579` creates the binder without node selector, affinity, or tolerations. `label-patches.json.j2` prefixes role labels with `role.<dns_suffix>/`; all in-tree selectors substitute that same value. |
| E13 — key/token/CA scope | `sops_age.yml:69` reads the entire configured key file into the cluster Secret. `bootstrap-secrets:132-152` creates a CA without name constraints. Agents receive `/server/node-token`. Official k3s docs confirm the server token can join servers or agents and decrypts bootstrap data; a distinct `agent-token` cannot join servers. |
| E14 — exposure/reset semantics | The LoadBalancer check permits only Traefik and App DNS; App DNS watches Ingress; no TCP/UDP entrypoints are configured. Official k3s docs say bare `--cluster-reset` resets etcd to one member without restoring a snapshot; they do **not** say it erases the existing datastore. |
| E15 — bootstrap/maintenance | Existing Helm is tested only for path existence before the pinned download block. Flux Operator CRD/rollout waits are inside the “operator Deployment absent” branch. Fresh k3s config/unit changes notify a play-end restart after the explicit first start. The Makefile has no site target. |
| E16 — day-zero/health/timeouts | Verify requires `lastSuccessfulTime` for the 12-hour etcd-copy CronJob; a fresh install has none. Verify checks Flux, one Secret, labels, backup timestamps, and testapp—not general node/pod health. Shared `kubectl` subprocesses have neither process nor request timeouts. The Secret check retrieves the full object JSON. |
| E17 — complexity/cruft | Counts: `tools/prep-node` 1,006 lines, `_cli_permissions.py` 899, `test_flux_operator.py` 1,304. Placeholder files are the only resources in their kustomizations. Several explicit contract roots are absent. The data-disk host variable and storage role remain documented product inputs, not safely dead. |
| E18 — CI/check behavior | CI checkout has no `fetch-depth: 0`; gitleaks scans the available repository. Kubeconform uses network schema locations. Raw manifest checks do not render Helm post-renderers. `ci/run-checks` stops on first failure. Local verify has no env-00-only leg. |
| E19 — required verification | Earlier in this audit: `OWNER_NAME="Gordon Cooper" bash ci/run-checks` passed. Full and isolated env-00-only pytest each reported `482 passed, 1 failed` on this non-systemd VM; the same Chrony service-handler test failed. GitHub CI at the target SHA was green. |
| E20 — schema/security probes | Direct validator probes accepted repository URLs with userinfo/query/fragment and accepted a non-DNS `dns_suffix`. `_is_enrollment_policy` returned true for a policy whose generate kind was ReplicationSource but whose match kind was not PVC. |
| E21 — test inspection | Negative branches are absent for verify body/issuer, all bootstrap outputs, checksum call sites, several restore/seal/preflight/prep/drill guards, k3s full-role/unit behavior, age-key mismatch, and base-os/node-disk/GPU/two-control-plane paths. Several tests inspect YAML/source/helper shape rather than executed behavior. |

## Verdicts on Grok findings

Severity disagreements do not refute the underlying claim.

| Other id | Verdict | Reproduced evidence and qualification | Fix |
| --- | --- | --- | --- |
| G-F01 | **Confirmed** | E01. This is the same core issue as O-F01 and C-F03/C-F04. “Critical” is reasonable only if the product is expected to survive disk loss; the contract explicitly limits it to logical/same-node recovery. | r01 |
| G-F02 | **Confirmed** | E02. I rate it high, not critical: the old ciphertext and Git history can retain the old password while the age key survives, but the documented operation is unsafe. | r02 |
| G-F03 | **Confirmed** | E03. Host mutation precedes complete schema validation. | r03 |
| G-F04 | **Confirmed** | E05. A different-owner env cannot coexist with env-01, and deploy docs do not tell an adopter to remove/replace env-01 first. I rate the break medium because a single-owner fork can proceed after that deletion. | r04 |
| G-F05 | **Confirmed** | E04. Env-schema accepts stale Flux sync url/ref and generated ConfigMap; pytest is too late for an adopter who runs site.yml directly. | r05 |
| G-F06 | **Partly confirmed** | The destructive branch exists and the warning itself says the key may belong to another env. It requires an explicit confirmation and is the intended last-resort key-loss path, so “deletes on mismatch” overstates it. The confirmation is still too weak for irreversible ciphertext deletion. | r06 |
| G-F07 | **Confirmed** | E06. This contradicts my first report’s statement that no `.cursor/cli.json` defect was found; I missed it. | r07 |
| G-F08 | **Confirmed** | E05. | r08 |
| G-F09 | **Confirmed** | E04. | r05 |
| G-F10 | **Partly confirmed** | E14. The two commands are easy to confuse and need explicit purposes, but bare cluster reset resets membership; the report’s claim that it “drops the datastore” is not supported by k3s documentation. | r09 |
| G-F11 | **Confirmed** | E07. `generateExisting` may repair later, but the product rule is temporarily violated and continuous detection is absent. | r10 |
| G-F12 | **Partly confirmed** | E16. Verify intentionally proves freshness, while `make drill` proves a restored copy. The check is not false, but its output/docs should not imply repository integrity or disaster recovery. | r11 |
| G-F13 | **Confirmed** | No adopter release intake or coordinated k3s/Flux/chart procedure exists. | r12 |
| G-F14 | **Confirmed** | E10. Fixtures prove inventory/schema shape, not usable Kubernetes GPU capacity. | r13 |
| G-F15 | **Confirmed** | E08. Platform-configs technically can list future secrets after namespaces exist, but there is no documented/per-service decrypting overlay and the apps repo setting is inert. | r14 |
| G-F16 | **Confirmed** | E07. Whether Kyverno needs `delete` for generate cleanup was not proven live; the grant is nonetheless cluster-wide and must be treated as a trust boundary. | r15 |
| G-F17 | **Partly confirmed** | Premature Hermes/apps inputs and the impossible current `backup: none` prefixes are real. Rejecting unimplemented providers is correct; loosening all schema constraints would reduce fail-closed behavior. | r16 |
| G-F18 | **Confirmed** | Operations/limits are absent from most component contracts and important procedures are scattered. | r17 |
| G-F19 | **Confirmed** | Five generated secrets have no Flux consumer. Do not delete existing ciphertext blindly; stop generating them until their consumers land or document them as deferred. | r16 |
| G-F20 | **Confirmed** | Plan/decision codes in product YAML and the owner-env-first docs index are both reproduced. Low severity. | r18 |
| G-F21 | **Confirmed** | `install-deps` invokes apt install without update. It failed on this VM’s empty/stale lists and succeeded after an update; not every Ubuntu image will reproduce it. | r19 |

## Verdicts on Claude product findings

| Other id | Verdict | Reproduced evidence and qualification | Fix |
| --- | --- | --- | --- |
| C-F01 | **Partly confirmed** | E02. One recipient and no tested offline-copy procedure are real. “Every backup is lost” requires simultaneous loss of the controller key and the cluster Secret; while the cluster is alive, its key can be recovered. Critical overstates the ordinary case. | r20 |
| C-F02 | **Confirmed** | E02; same as G-F02/O-F03. Severity high rather than critical. | r02 |
| C-F03 | **Confirmed** | E01. Same-host and single backup-host disk loss are explicit limits, but still a product gap. | r01 |
| C-F04 | **Confirmed** | E01/E13. The token must be backed up with the datastore for rebuilt-node restore. | r01 |
| C-F05 | **Confirmed** | E12. The outcome depends on k3s local-path WaitForFirstConsumer behavior, so the proposed test should prove that assumption. | r21 |
| C-F06 | **Confirmed** | E16. It is a day-zero signaling defect; I rate medium rather than high. | r11 |
| C-F07 | **Confirmed** | E08. | r14 |
| C-F08 | **Confirmed** | E08. Global paths provide no per-env service selection. | r14 |
| C-F09 | **Partly confirmed** | Public Git is a deliberate documented first-release constraint and env topology contains no secret by design. Private-repo support is absent, but “forces unsafe publication” and high severity overstate the code-only evidence. | r22 |
| C-F10 | **Confirmed** | E11. Disabling automatic updates can be a valid availability choice only with a patch/reboot runbook, which is absent. | r23 |
| C-F11 | **Confirmed** | E04; same as G-F05. | r05 |
| C-F12 | **Confirmed** | `choose_ansible_user` defaults to each login user and `apply_env_edits` unconditionally rewrites the one env-wide `ssh_user`. | r24 |
| C-F13 | **Confirmed** | E10; same as O-F06/G-F14. | r13 |
| C-F14 | **Partly confirmed** | E12. The coupling exists, but all current workloads substitute the label consistently and `dns_suffix` is a set-once identity. This is a low-severity extension constraint, not a current scheduling failure. | r25 |
| C-F15 | **Confirmed** | E13. A shared default age file can disclose unrelated identities to the cluster. | r26 |
| C-F16 | **Confirmed** | E07; same trust boundary as O-F04/O-F22. | r27 |
| C-F17 | **Confirmed** | E13. The CA can sign outside the internal suffix. Client support for critical name constraints must be tested before rollout. | r28 |
| C-F18 | **Confirmed** | E13 and official k3s docs: the default server token can join servers or agents and grants administrator-equivalent power. | r29 |
| C-F19 | **Partly confirmed** | E14. The checked-in product has no non-HTTP exposure path; Traefik can support TCP/UDP if configured, so “cannot” is a current configuration gap, not a platform impossibility. | r30 |
| C-F20 | **Confirmed** | E11. NTS-only is secure for public pools but excludes common LAN/router NTP. | r31 |
| C-F21 | **Confirmed** | Same adopter release/upgrade gap as O-F07/G-F13. “Prune-disabled orphans” needs component-specific migration care, not a global flip to prune. | r12 |
| C-F22 | **Partly confirmed** | Tool pins in `tools/versions.env` lack Renovate coverage. The 13 custom managers are repetitive, but native Flux-manager coverage of this exact `chartRef`/OCI shape was not settled from the repo. | r32 |
| C-F23 | **Partly confirmed** | E17 proves size and hand-written SSH/YAML editing. An Ansible replacement may not preserve the safe interactive host-key and draft-env workflow, so “native replacement” is a design proposal, not a demonstrated defect. | r33 |
| C-F24 | **Partly confirmed** | E15 confirms stale/unverified existing Helm (O-F23). Replacing bootstrap with k3s HelmChart could create adoption/circular-ownership issues and needs a spike. | r34 |
| C-F25 | **Confirmed** | The long site command is duplicated and no Make target wraps/preflights it. Usability issue; medium is high for the impact. | r35 |
| C-F26 | **Confirmed** | E16. Verify omits general node/pod health and the recovery index is scattered. | r36 |
| C-F27 | **Confirmed** | E08/E17. Required future config and five unused generated secrets precede consumers. | r16 |
| C-F28 | **Confirmed** | Decision/plan codes occur in code/YAML comments that the Markdown-only checker excludes. Low severity. | r18 |
| C-F29 | **Confirmed** | E05; same as G-F08/O-F09. | r08 |
| C-F30 | **Partly confirmed** | Placeholders, missing-root index entries, duplicate helpers, and amd64-dead Helm branches are candidates. The data-disk host variable/group and storage role are documented product surfaces, not proven dead. Helm bootstrap and Renovate managers cannot simply be deleted. | r37 |
| C-F31 | **Confirmed** | The drill selects newest on-disk snapshot while the restic copy runs five minutes after the independent k3s schedule, so a manual/new snapshot can race it. Low severity. | r38 |
| C-F32 | **Confirmed** | Operator tools use system Python/apt packages while Ansible/pytest use the venv. This is deliberate today but duplicates runtimes and dependency ownership. | r39 |
| C-F33 | **Partly confirmed** | Mismatched binaries in `~/.local/bin` are replaced intentionally. “Silently” overstates it, but overwriting user-owned tool paths is a real low-severity surprise. | r40 |
| C-F34 | **Confirmed** | `bootstrap-secrets` does not resolve `age_key_file`; env-01 docs require a separate `SOPS_AGE_KEY_FILE` export. | r41 |
| C-F35 | **Confirmed** | E20. Non-DNS values pass and later reach labels, certificates, and DNS. | r42 |
| C-F36 | **Confirmed** | E06, category only. Defense in depth is incomplete even though the primary rule remains “agents receive no kubeconfig.” | r07 |
| C-F37 | **Confirmed** | Verify carries an 85-line minute-walking cron evaluator. This is maintainability, not a correctness finding. | r43 |
| C-F38 | **Confirmed** | E15. A partial first install that created the Deployment can skip CRD/rollout waits on rerun. | r44 |
| C-F39 | **Confirmed** | E15. Fresh installation starts k3s and then executes the notified restart at play end. Low severity but avoidable. | r45 |

## Verdicts on Claude test/check findings

| Other id | Verdict | Reproduced evidence and qualification | Fix |
| --- | --- | --- | --- |
| C-T01 | **Confirmed** | E20. `pvc-backup` identifies an enrollment policy by name plus generated kind, not by PVC match semantics. | T01 |
| C-T02 | **Confirmed** | E21. Tests cover fetch failure but not wrong body hash or issuer-command failure. | T02 |
| C-T03 | **Confirmed** | The explicit encrypted-value loop covers only CA/restic outputs and not metadata or the five future specs. Tree-level gitleaks is not a generated-output test. | T03 |
| C-T04 | **Confirmed** | Only the checksum helper mismatch is tested; removing enforcement at download/install call sites would pass. | T04 |
| C-T05 | **Partly confirmed** | E18. Raw-file checks intentionally validate source, but Helm post-rendered/patch output can escape equivalent policy checks. The exact LoadBalancer nesting example was broader than the reproduced gap. | T05 |
| C-T06 | **Confirmed** | E18. Without full history, an add/remove secret sequence before the checked-out tip can evade history scanning. | T06 |
| C-T07 | **Confirmed** | Multiple tests lock descriptions, YAML values, ordering, or source shape contrary to the rulebook. Some snapshots still encode policy; remove only those with an equivalent behavior proof. | T07 |
| C-T08 | **Partly confirmed** | E17. Size alone is subjective and the module explicitly models a best-effort external matcher. The unchecked `.claude/settings.json` parity and complexity cost are real. | T08 |
| C-T09 | **Confirmed** | E21. Important fail-closed branches exist but are not driven. | T09 |
| C-T10 | **Confirmed** | Repeated capture and assertions against strings no implementation emits are dead/diagnostic-only. Low severity. | T10 |
| C-T11 | **Partly confirmed** | Missing manifest/Flux roots can pass and empty contracts have no heading enforcement. Some absent explicit roots are intentionally deferred, so not every skip is a bug. | T11 |
| C-T12 | **Confirmed** | E18. Thirteen near-identical wrappers and stop-first reporting add maintenance and hide later failures. | T12 |
| C-T13 | **Partly confirmed** | Root files are outside core-names’ documented product scan, and some root agent/config files legitimately contain owner/build identity. The scope gap should be explicit; broadening blindly would create false positives. | T13 |
| C-T14 | **Confirmed** | `ca.crt` is written only when missing; deleting/regenerating only the encrypted CA Secret preserves a stale public root. | T14 |
| C-T15 | **Partly confirmed** | Structural claim confirmed: local verify lacks the env-00-only leg and skips missing CLIs outside CI. Exact result is runner-dependent: E19 failed one Chrony test here, while Claude’s systemd-shaped run passed 483. | T15 |
| C-T16 | **Confirmed** | E21. Tests cover fragments/templates but not full k3s main role, service unit execution, on-disk modes/content, or marker/default-local-storage behavior. | T16 |
| C-T17 | **Confirmed** | Existing-different age Secret refusal and exact `age.agekey` payload are not tested. | T17 |
| C-T18 | **Confirmed** | E21. Base OS timer, node-disk traversal, non-GPU skip, and two-control-plane fail paths lack behavior tests. | T18 |
| C-T19 | **Partly confirmed** | The file has extensive tokenizer/play-walker/helper meta-tests. “Mostly” is subjective and some protect real parser behavior; the rulebook still favors replacing them with executable role behavior. | T19 |
| C-T20 | **Confirmed** | E18. Kubeconform depends on upstream network schema locations at test time; only a subset is vendored. | T20 |

## Delete-list cross-check

### Grok delete list

| Candidate | Verdict |
| --- | --- |
| Delete the app/service placeholders | **Refuted as an outright delete.** They are the only listed resources. Remove the phase comments now; use a proven empty kustomization or land the first real resource before deleting the files. |
| Delete the five future SecretSpecs or env-01 ciphertexts | **Partly confirmed.** Stop generating unused specs for new envs until consumers land; do not delete committed ciphertext as audit cleanup. |
| Delete absent `EXPLICIT_COMPONENT_ROOTS` | **Partly confirmed.** They are list entries, not files. Removing them is safe checker cleanup but loses a deferred contract index. |
| Delete `.claude/settings.json` | **Refuted as a product cleanup.** It is a separate agent guard. Either test parity or deliberately retire that workflow; CI non-use alone does not make it safe to delete. |

### Claude delete list

| # | Candidate | Verdict |
| --- | --- | --- |
| 1 | Hermes/apps value files and validators | **Partly confirmed.** No runtime consumer exists, so remove them in a schema migration or make them optional; not an unreviewed file delete because the contract and fixtures require them. |
| 2 | `build_agent`, `code_delivery`, `llm_provider_host`, `apps_git_repo_url`, cross-env owner rule | **Partly confirmed.** The future-only keys should return with consumers. `apps_git_repo_url` and owner handling also feed identity validation; choose the app-source and multi-env ownership model first. |
| 3 | Five future secret specs/helpers | **Confirmed with migration caveat.** Stop generating them for new envs; preserve existing encrypted files until owners explicitly retire them. |
| 4 | `groups.yml`, constructed `data_disk` group, related tests | **Refuted as dead code.** The host variable drives node-disk behavior and the group remains documented inventory output even if no play targets the group. A simplification PR may remove only the redundant derived group and update the contract/tests. |
| 5 | `storage` role | **Refuted as an audit delete.** It is an advertised label-only role. Remove only as an intentional schema/product contraction. |
| 6 | placeholders, replacing resources with `[]` | **Partly confirmed.** Empty kustomizations may be valid, but prove Flux/kustomize behavior before removing the only resources. |
| 7 | absent explicit contract-root entries | **Confirmed as safe checker cleanup**, if deferred commitments move to a roadmap/issue. |
| 8 | Helm CLI pieces/arm64 branches | **Partly confirmed.** arm64-only branches are dead under current preflight. The Helm bootstrap is live and can be removed only after a tested replacement. |
| 9 | duplicate restore `_reason`/`_not_found` | **Confirmed** as a small refactor to shared helpers with existing tests. |
| 10 | 13 Renovate regex managers | **Refuted without replacement.** They are active update automation; replace and prove native/generic coverage before deleting. |
| 11 | verify cron helpers | **Refuted without replacement.** They are live freshness logic; delete only after a proven library/status-based implementation. |
| 12 | YAML snapshots/dead tests | **Partly confirmed.** Delete dead assertions and redundant shape snapshots only where a behavior test or check retains the invariant. |
| 13 | plan codes in code comments | **Confirmed.** Cosmetic removal plus broader comment scanning is safe. |

## My first report, revisited

| Own id | Cross-check result |
| --- | --- |
| O-F01 | **Confirmed** by G-F01 and C-F03/C-F04. |
| O-F02 | **Confirmed** by G-F03. Claude’s opening claim that all inputs are validated before host touch is contradicted by the call order. |
| O-F03 | **Confirmed** by G-F02 and C-F02. |
| O-F04 | **Confirmed** by C-F16 and the broader G-F16 trust concern. |
| O-F05 | **Confirmed** by G-F15 and C-F08/C-F27. |
| O-F06 | **Confirmed** by G-F14 and C-F13. |
| O-F07 | **Confirmed** by G-F13 and C-F21. |
| O-F08 | **Environment-dependent, not wrong.** Grok reproduced the same Chrony failure; Claude passed on a different runner. The portability finding stands. |
| O-F09 | **Confirmed** by G-F03/G-F08 and C-F25/C-F29. |
| O-F10 | **Confirmed** by G-F17/G-F19/G-F20 and C-F27/C-F30. |
| O-F11 | **Missed by both.** Neither challenged the stated Kyverno/Kubernetes support-range skew. It remains code/doc evidence, not a reproduced live incompatibility. |
| O-F12 | **Confirmed** by G-F11 and Claude’s contract review. |
| O-F13 | **Fact confirmed, priority disputed.** Both other auditors treat one control plane as a documented limit or nice-to-have rather than a current defect. I retain it as a resilience/product-profile gap, not a bug. |
| O-F14 | **Fact confirmed, priority disputed.** Claude calls arm64 a nice-to-have and also identifies dead arm64 Helm branches. Keep it as a product limit, below recovery/security work. |
| O-F15 | **Confirmed** by both reports’ observability/Gatus discussion. |
| O-F16 | **Confirmed** by C-F01/C-F15 and G-F06/G-F08. |
| O-F17 | **Partly contradicted.** Claude supports removing shape/dead tests; Grok recommends cutting none. My narrower list—only duplicate/helper/source-shape tests with retained behavior coverage—is the defensible middle. |
| O-F18 | **Confirmed** by G-F18 and Claude’s component-by-component contract review. |
| O-F19 | **Confirmed** by G-F21 and C-F25/F32/F34. |
| O-F20 | **Missed by both.** Repository URLs with credential-bearing URL components still pass validation. |
| O-F21 | **Missed by both as a ranked finding.** Their backup ordering concerns are adjacent, but neither noticed the missing VolSync readiness health check. |
| O-F22 | **Confirmed** by G-F16 and C-F16. |
| O-F23 | **Confirmed** by C-F24. |
| O-F24 | **Missed by both.** Unbounded `kubectl` calls remain. |
| O-F25 | **Missed by both.** Verify still retrieves full Secret data. |

One non-finding in my first report was wrong: I said no `.cursor/cli.json` safety defect was found. G-F07 is reproducible (E06), and C-F36 adds two separate uncovered categories. That omission is corrected here.

## Answers by question where the reports differ

| Q | Cross-check judgment |
| --- | --- |
| 1 | GPT/Claude are closer on leanness: required future config/secrets and test/helper weight are real. Grok is closer on the small deployed platform slice. |
| 2 | Claude names more replaceable glue, but some replacements are unproven. Keep Kyverno enrollment and Ansible/k3s; first remove duplicated env facts and simplify prep/bootstrap only with behavior parity. |
| 3 | Grok is closest on product deletion: stop unused secret generation but preserve live path resources. My narrow test cuts are safer than Claude’s broad removal list. |
| 4 | My report is closest on breadth (VolSync gate, Helm pin, timeouts); Grok is strongest on stale env facts; Claude adds real day-zero and schedule-race issues. |
| 5 | Grok is closest on the concrete multi-owner failure. Claude adds real public-repo/NTS/LB constraints; my HA/arm64 items are product profiles, not immediate bugs. |
| 6 | All identify disk loss, GPU, secrets, and upgrades. My security/order gaps plus Claude’s patching, binder, and day-zero gaps form the complete answer. |
| 7 | Grok is closest: placeholders cannot simply be deleted. Claude is right about redundant helpers and unused specs, but over-labels documented product surfaces as dead. |
| 8 | All agree core identity is clean. Grok/Claude are right that owner-env docs and plan codes weaken the product front door. |
| 9 | Grok/my “not yet” is closer than Claude’s “mostly yes”: validation occurs after mutation, the template README is missing, Flux copies can drift, and day-zero backup checks fail. |
| 10 | All catch incomplete pre-host validation. My URL-credential probe, Grok’s Flux/ConfigMap drift, and Claude’s DNS validation are independent real gaps. |
| 11 | All agree inventory shapes work and Kubernetes GPU capacity does not. Claude adds the valid restore-binder placement risk. |
| 12 | Claude has the most complete unwritten-knowledge list; Grok/my release, key, secrets, and all-host prep points remain valid. |
| 13 | All agree logical PVC restore is strong and disk/rebuilt-control-plane recovery is not. Claude uniquely catches binder placement and day-zero timing. |
| 14 | All agree Renovate in this source repo is not an adopter release process. Claude also correctly adds OS patching and missing controller-tool updates. |
| 15 | All agree restic rotation is unsafe and age-key custody/rotation is incomplete. Claude’s “critical” age severity is too strong without simultaneous cluster/key loss. |
| 16 | Grok is closest on freshness versus integrity; my timeout/Secret-fetch findings are concrete tool behavior; Claude correctly adds missing node/pod health and scattered runbooks. |
| 17 | Grok is right about the missed case-sensitive Secret guard; Claude is right about uncovered guard categories and key/token/CA trust; my URL and backup-RBAC findings remain independently confirmed. |
| 18 | Claude provides the strongest check-specific test gaps; my product-level disaster/GPU/order proofs remain the higher-value integration tests. |
| 19 | Grok’s “cut none” is too conservative and Claude’s broad cuts are too aggressive. Remove only dead/redundant/shape tests after retaining equivalent behavior coverage. |
| 20 | Results differ by runner, not tree: Claude’s 483 pass and our 482+1 Chrony failure jointly prove the local test is nonportable. GitHub CI is green. |
| 21 | All say “partial.” Claude is strongest on per-env selection; Grok/my reports are strongest on decryption/app-source and VolSync ordering. |
| 22 | My/Claude security-boundary detail is closest: global backup credentials, privileged movers, missing GPU runtime, inert app source, and absent tenant isolation all constrain the next products. |
| 23 | Combined answer: decrypting/per-env app landing, health, narrow namespace/secret trust, release intake, off-host recovery, and GPU resources. No one report alone listed all. |
| 24 | All agree on the deferred set: Gatus/observability, Hermes/code delivery, external apps, GPU runtime, off-host/rebuilt-node recovery, model-name checks, and tenant backup isolation. Removing speculative runtime stubs must not erase these commitments. |

## Coding-agent-ready fixes

Each row is a separable PR direction. “Done” is intentionally runnable without the real cluster unless the row explicitly calls for disposable-cluster evidence.

| Fix | Approach | Likely files | Tests | Done check |
| --- | --- | --- | --- | --- |
| r01 | Define one supported disaster boundary. Back up the k3s server token with etcd to an independent target; add an owner-run rebuilt-node restore. If deferred, rename the drill’s etcd step to copy-integrity and foreground the limitation. | `platform/backups/*`, k3s role, env secrets/docs, `tools/drill` | Fixture for token artifact and restore command construction; disposable k3s restore when available | Offline test proves snapshot+token bundle and documented restore; `make ci-verify` |
| r02 | Replace delete/regenerate rotation with two-phase restic key addition, validation, Git secret update, and old-key removal; rotate REST auth separately. | backups CONTRACT, secrets docs, optional rotation tool | Fake-restic state machine covering old/new key and rollback | Old and new key open every repository before old removal |
| r03 | Validate all existing env inputs before SSH/local mutation, then validate generated edits again. Provide a narrow draft-env exception only for values prep-node itself creates. | `tools/prep-node`, deploy docs | Assert invalid env makes zero Session/mutation calls | Focused prep-node tests and `make ci-verify` |
| r04 | Either document/remove the shipped non-example env before another owner is added, or scope owner consistency per deployment rather than whole checkout. | deploy docs and/or `schema/env.py` | Fixture with env-01 plus a different-owner adopter env | Chosen multi-env ownership policy passes and is documented |
| r05 | Move Flux sync and generated ConfigMap equivalence into env-schema/preflight, or generate both from one values source. | `schema/env.py`, renderer, preflight, docs | Seed wrong url/ref/path and stale ConfigMap | `ci/checks/env-schema` rejects all four drifts |
| r06 | Require typed env-name confirmation and a recoverable backup/export before changing recipient and deleting ciphertext; separate “wrong key file” from “key irretrievably lost.” | `tools/prep-node`, recovery docs | Refusal, wrong-key, explicit-loss, and interruption cases | No ciphertext deletion on yes/no or mismatched-key flow |
| r07 | Make Secret-read matching case-insensitive by category; add guard categories for cluster-writing repo tools and admin-kubeconfig reads; keep both agent configs in parity tests. | `.cursor/cli.json`, `.claude/settings.json`, matcher/corpus/tests | Mixed-case category tests without publishing bypass strings; parity assertions | `cli-deny` passes and every forbidden category is blocked |
| r08 | Add a generic template secrets README and point deploy/index docs to env-00/generic docs. | `envs/env-00`, `docs/deploy.md`, `docs/README.md` | Link/path check with env-01 absent | env-00-only docs have no dead secret-doc reference |
| r09 | Describe bare reset as membership repair, not restore; put it beside and clearly distinguish snapshot restore. | Ansible README, backups CONTRACT | Documentation assertion only if existing docs tests warrant it | Both commands state purpose and data effect accurately |
| r10 | Fail closed for mandatory-backup namespaces after controller readiness, or add continuous repair/alerting with a documented availability tradeoff. | enrollment policy, Flux gates, contracts | Admission outage/recovery fixture or disposable-cluster proof | PVC cannot remain silently unenrolled beyond chosen policy |
| r11 | Add a first-backup bootstrap action or explicit PENDING state based on object age; label timestamp checks as freshness and keep drill as restore integrity. | verify, deploy docs, backup jobs/contracts | Fresh-object, first-success, stale, corrupt-copy cases | Fresh correct install has an actionable non-failure path |
| r12 | Publish an adopter upgrade/release intake runbook: upstream merge, migrations, pin order, drain/restart order, rollback, and component removal. | `docs/deploy.md` or `docs/upgrade.md`, contracts, Renovate descriptions | Link/pin consistency tests only where behavior-backed | One offline walkthrough updates a copied env without undocumented steps |
| r13 | Define GPU ownership (driver/toolkit), deploy a pinned device plugin/operator, and verify allocatable GPUs; add optional capacity-class labels only when a workload needs them. | Ansible/platform GPU component, schema/contracts, verify | NVIDIA/non-NVIDIA fixtures plus disposable GPU-node proof | A pod requesting `nvidia.com/gpu` runs on each supported class |
| r14 | Add per-env service/app overlays with SOPS decryption after namespaces, choose monorepo versus second Flux source, and document the service landing contract. | env Flux stages, `services/`/`apps/` contracts, schema | env enable/disable, SOPS ordering, source/ref, env-00-only build | Two env fixtures select different services and secrets reconcile in order |
| r15 | Remove unnecessary Secret verbs; restrict enrollment to explicitly trusted namespaces; document and monitor unavoidable Kyverno/privileged-mover trust. | Kyverno RBAC, enrollment policy, contracts | RBAC `can-i`/manifest assertions and excluded namespace fixture | Untrusted namespace cannot receive credential or privileged annotation |
| r16 | Remove/make optional future-only values and stop generating unconsumed secrets; move commitments to roadmap/issues and reintroduce fields with consumers. | schema, bootstrap tool, env template, code-delivery contract | New minimal env and migration fixture; bootstrap file set | Minimal env contains only consumed config/secrets |
| r17 | Add concise Operations/Limits pointers to the ten contracts; centralize the operations index without copying runbooks. | component CONTRACTs, `docs/operations.md` | Contract heading/link checker | Every implemented component exposes its operation and deliberate limits |
| r18 | Remove plan codes from product comments and extend the check to supported code-comment file types while exempting rulebooks/build metadata deliberately. | YAML/Python/Jinja comments, plan-code check/tests, docs index | Positive/negative comment fixtures | Checker rejects new product-code plan tokens |
| r19 | Handle stale apt lists explicitly, state/preflight controller Python/OS, remove unused `make` prep requirement, and preflight `age-keygen`. | `tools/install-deps`, `tools/prep-node`, deploy docs | Empty apt metadata/failure-message and missing-tool fixtures | Fresh supported controller reaches a complete dependency check |
| r20 | Use a per-env age file, add a second offline recovery recipient, document `sops updatekeys` rotation, and test metadata for all env ciphertexts. | `.sops.yaml`, prep/bootstrap/docs/checks | Two-recipient metadata, rotation rollback, env isolation | Both keys independently decrypt a disposable fixture; no private key committed |
| r21 | Bind restore using the owner workload’s scheduling constraints or refuse automatic binding and give an exact manual step. | `tools/restore`, backups CONTRACT | Owner with selector/affinity/toleration and wrong-node fixture | Restored PVC binds where owner can schedule |
| r22 | Support optional Flux pull Secret created from SOPS, while retaining public HTTPS as the zero-secret default. | schema, Flux bootstrap/env manifests, docs | Public/private fixtures; Secret ordering and no URL credentials | Private fixture reconciles without credentials in URL/Git plaintext |
| r23 | Choose security-only unattended updates or a serial drain/patch/reboot play and document rollback/maintenance windows. | base-os role, new patch play, operations docs, verify | apt policy render, serial/drain/reboot-required fixture | Nodes have a documented, tested patch path and report pending reboot |
| r24 | Preserve the existing env-wide SSH user by default; create/validate it on each new node and require explicit migration to change it. | `tools/prep-node`, schema/docs | Two hosts with different login users | Second prep does not rewrite cluster `ssh_user` |
| r25 | Either freeze/document DNS-derived labels as immutable identity or migrate to a stable product prefix with a two-label transition. | k3s labels, selectors, verify, contracts | old/new label migration render | Existing workloads remain schedulable through migration |
| r26 | Extract only the age identity matching the env recipient and use a per-env default path. | flux-operator SOPS tasks/template, prep/schema/docs | Multi-identity input fixture | Cluster Secret contains only the selected identity |
| r27 | Isolate restic credentials per namespace/repository or use append-only movers with centralized prune; opt namespaces into privileged movers. | rest server/bootstrap/enrollment/contracts | Cross-namespace access denial and prune-owner tests | One workload cannot read/delete another repository |
| r28 | Issue an env-specific constrained CA after testing target client support; document migration/trust-store refresh. | bootstrap tool, cert-manager contract/docs | OpenSSL constraint assertions and representative client matrix | CA cannot validate a leaf outside the permitted suffix |
| r29 | Configure a separate agent token and distribute it only to agent nodes; back up the server token with datastore recovery artifacts. | k3s server/agent templates/tasks, secrets/recovery docs | Rendered token separation and permissions tests | Agent token cannot join a server in disposable k3s proof |
| r30 | Choose a supported non-HTTP route: pinned Traefik TCP/UDP entrypoints or annotated Service exposure plus DNS; keep LoadBalancer policy explicit. | Traefik/App DNS/checks/contracts | TCP/UDP fixture, allowed/denied Service cases | A disposable non-HTTP endpoint resolves and connects |
| r31 | Represent each time source with an `nts` choice, default secure public pools to true, and validate synchronization status. | schema, Chrony template, docs/verify | NTS and LAN-NTP render fixtures | Both supported modes synchronize in their fixture environments |
| r32 | Add Renovate coverage for `tools/versions.env`; replace custom managers only after proving identical PR extraction/grouping on all current pins. | `renovate.json`, versions file, tests | Extraction snapshot for every pin | Dry-run reports every managed dependency exactly once |
| r33 | Spike an Ansible bootstrap play against prep-node’s safety contract; retain the current tool unless host-key, no-partial-mutation, and age flows reach parity. | prep-node/bootstrap play/docs/tests | Side-by-side safety scenarios | Replacement passes the same fail-closed matrix before deletion |
| r34 | Verify existing Helm version/digest and replace/refuse mismatch now; separately evaluate k3s HelmChart bootstrap and adoption. | Helm role/defaults/tests | existing-correct, stale, tampered, absent fixtures | Bootstrap never executes unpinned Helm |
| r35 | Add `make site ENV=...` with schema preflight and safe TAGS/LIMIT forwarding. | Makefile, deploy/Ansible docs | Command construction and invalid-env tests | Documented site command has one source and fails before Ansible on bad env |
| r36 | Add node Ready, platform pod failure, disk pressure, and clock checks; create one symptom-to-runbook index. | verify, docs/contracts | Fake kubectl states for each failure | Verify names the failing object/node and runbook |
| r37 | Remove only proven redundancies: duplicate restore helpers, dead arm64 Helm metadata under amd64 profile, optional absent-root entries, and placeholders after an empty-path proof. | listed files/tests/contracts | Existing focused tests plus Flux empty-path build | No runtime/schema surface is removed without migration |
| r38 | Compare the latest snapshot actually present in restic, or trigger/wait for a copy Job before checksum comparison; derive schedules from one value. | drill, etcd-copy manifest, cluster vars/contracts | manual snapshot and :00–:05 race fixtures | Drill passes for newest copied snapshot and reports uncopied snapshot clearly |
| r39 | Choose one Python runtime/dependency source for operator tools, preferably the managed venv, and install only required collections. | Makefile, install-deps, requirements/collections | fresh dependency install and each tool smoke test | No operator tool depends on duplicate apt Python packages |
| r40 | Install managed CLIs into repo-owned `.bin` or require explicit approval before replacing user binaries. | install-deps, env helper/docs | mismatched user binary fixture | User path is untouched by default |
| r41 | Resolve `age_key_file` consistently across prep-node, bootstrap, and Ansible without reading it unless an operation requires it. | bootstrap tool/shared env helper/docs | custom/default key-path fixtures | One env value selects the same path in all tools |
| r42 | Validate lowercase RFC 1123 domain syntax and document whether single-label/private suffixes are allowed. | `schema/env.py`, contract/tests | boundary and invalid-character fixtures | Bad suffixes fail env-schema before host access |
| r43 | Replace cron code only with a pinned small library or controller status field that passes the existing DST/time-zone matrix. | verify, dependency pins, tests | current cron suite plus DST boundaries | Equivalent freshness results with less custom code |
| r44 | Run idempotent CRD and rollout readiness checks on every Flux Operator role run, independent of Deployment existence. | `operator.yml`, role tests | partial-first-install fixture | Rerun waits/fails until CRD and Deployment are Ready |
| r45 | Flush handlers before first service start or avoid notifying restart when installing initial config/unit. | k3s role/handlers/tests | fresh install and changed-config rerun | Fresh install starts once; subsequent config change restarts once |

### Test/check fix directions

| Fix | Approach, files, and test | Done check |
| --- | --- | --- |
| T01 | Strengthen `_pvc_backup.py` policy detection to require PVC match/exclusions/preconditions; add wrong-kind and missing-match fixtures. | Seeded non-PVC policy fails `pvc-backup`. |
| T02 | Drive wrong canary body and issuer/chain command failure in `test_verify.py`. | Both branches fail with the intended message. |
| T03 | Parameterize every bootstrap SecretSpec and assert encrypted payload fields plus safe metadata; add cert+private-key bundle rejection. | Any plaintext generated field fails tests. |
| T04 | Exercise the real download/install call path with wrong bytes, not only `verify_sha256`. | Removing either checksum call fails tests. |
| T05 | Run policy checks on `kustomize build` and representative Helm/post-render output, or explicitly ban unchecked patches. | A patch that introduces a policy violation fails. |
| T06 | Set checkout full history for secret-scan or fetch the PR base/range and scan it explicitly. | Add-then-remove planted secret in PR history is detected. |
| T07 | Replace description/order/value snapshots with rendered behavior/policy assertions; retain snapshots only where no cheaper proof exists. | Rulebook review plus full pytest. |
| T08 | Generate/test deny-list parity from one category source or explicitly retire one client; shrink matcher cases to safety categories. | Both active configs block the same required corpus. |
| T09 | Add behavior cases for restore failure status, seal overwrite/force/kind, prep/drill interruption, and preflight cardinality. | Each guard fails closed under its seeded break. |
| T10 | Delete unreachable assertions and make diagnostics assertions observe an emitted path. | Mutation/removal of the guarded path fails a test. |
| T11 | Make required scan roots explicit and enforce non-empty contract headings; retain an allow-list for deferred roots. | Missing required root/input fails with its path. |
| T12 | Replace wrappers with one parameterized entrypoint and collect all independent check failures before exiting nonzero. | Two seeded failures are both reported. |
| T13 | Document exact identity-scan scope and add only product root files that must remain identity-free. | Owner token in a scoped root fixture fails without agent-doc false positives. |
| T14 | Refuse CA-secret regeneration while `ca.crt` exists with a different cert, or rewrite it atomically from the new Secret. | Secret-only deletion cannot leave stale root. |
| T15 | Add a local env-00-only target and make the Chrony handler proof hermetic across systemd/non-systemd controllers. | Full and env-00-only targets pass on both runner shapes. |
| T16 | Execute the full k3s role with fake binaries and render/assert unit, config modes, marker content, and storage path. | Removing an import/mode/ExecStart fails. |
| T17 | Test existing-different `sops-age` refusal and exact selected-key payload without exposing real key material. | Mismatch makes no write; payload key name/content fixture matches. |
| T18 | Add timer-disable, path traversal, non-GPU skip, and two-control-plane behavior fixtures. | Each seeded safety regression fails. |
| T19 | Replace tokenizer/play-walker meta-tests with executable role/tag behavior where possible; keep parser tests only for parser contracts. | Same safety invariants with less source-shape coupling. |
| T20 | Vendor/pin required schemas or cache by immutable ref with an explicit offline mode. | Flux/kubeconform test passes with network disabled. |

## Concerns and questions

1. No live cluster was used. Binder placement, Kyverno cleanup verb requirements, private Flux authentication, non-HTTP exposure, GPU capacity, and rebuilt-node restore need disposable-cluster proof.
2. Bare k3s `--cluster-reset` resets membership without restoring a snapshot; the other report’s datastore-deletion wording is refuted by official k3s docs. The existing command may still be operationally confusing.
3. K3s docs confirm the server-token risk and distinct agent-token capability, but the exact migration/rotation sequence must be validated against the pinned k3s version.
4. Critical CA name constraints are technically appropriate but client compatibility and wildcard constraint encoding need a representative device test before rotation.
5. Renovate native Flux-manager coverage of this repository’s OCI `chartRef` pattern was not settled from code alone; do not delete custom managers first.
6. The local test disagreement is real runner dependence: this VM cannot execute the Chrony service handler, while Claude’s runner and GitHub CI can. It is not evidence of different source.
7. Command-guard gaps are defense-in-depth findings. The primary control remains that coding agents receive no cluster credentials; this report intentionally omits executable bypass details.
