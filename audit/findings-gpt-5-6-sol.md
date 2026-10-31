# Whole-repo audit — GPT-5.6 Sol

Audit target: `5561280d2c57e0b7d0d7d1a8c2eaa1a95cf9bdd8` (215 tracked files, 17,511 lines). This was a code-only review: no host, cluster, private key, or decrypted secret was accessed.

Verification:

```text
$ OWNER_NAME="Gordon Cooper" make ci-verify
10 checks passed; 201 tests passed in 41.43s

$ diff upstream-v8.4.0-volumesnapshot-crd vendored-crd
only local difference: kustomize.toolkit.fluxcd.io/prune: disabled
```

The first `make deps` attempt failed because this VM's apt indexes were absent. After `apt-get update`, the required dependency install and full verification passed. That was an environment issue, not a repository finding.

## 1. Big picture

The strongest part of the repository is its defensive intent. Pins are unusually thorough, SOPS ciphertext is kept in Git, the age bootstrap refuses to overwrite a differing live key, k3s refuses an unmarked foreign installation, disk formatting needs two confirmations, generated Flux input is drift-tested, and the full suite builds every Flux path and validates native Kubernetes objects. The example env is a useful schema canary. Those pieces should stay.

The main weakness is that several safety claims are stronger than their implementation. Most urgently, `restore-all` can return success and remove the rebuild hold after restoring nothing. The backup design also gives every mover credentials that can mutate every repository, stores the only backup on the same local hardware in the current deployment, and does not preserve the k3s token required for rebuilt-node etcd recovery. Green CI does not catch these outcomes; in one case it explicitly tests the unsafe behavior as expected.

Three structural themes recur across the repo:

1. **Bootstrap works for one known machine, but lifecycle does not.** Adding/removing a data disk, changing server/agent roles, removing a node, changing the App DNS ClusterIP, or losing the single node are not safe convergence paths. The schema deliberately requires exactly one control-plane and backup host, and k3s rejects non-amd64 nodes.
2. **Checks model configuration instead of proving behavior.** Several CI rules silently accept common Helm shapes or overly broad identities; CRD “strict” schemas allow arbitrary specs; the command guard tests a 605-line local clone of one Cursor parser. The phase verifiers can exit zero with required evidence missing.
3. **Future product configuration landed before its consumers.** Every env must supply app, Hermes, provider, and app-repo values that no runtime component reads. Validation of those future values is shallow, while all host, platform, and future-product values are exported into one cluster-wide substitution map. This is already hard to explain and will become a broad reconciliation/blast-radius boundary when services and apps arrive.

The foundation is credible for repeatedly rebuilding the current disposable single-node env, but not yet for recovering that household from hardware loss or for handing a fresh copy to another adopter. The smallest high-value sequence is: make restore fail closed, establish off-node recoverability, fix the broken first-run path, then define explicit node/storage lifecycle operations before adding services.

## 2. Ranked findings

| Rank | ID | Summary | Type | Confidence |
|---:|---|---|---|---|
| 1 | F01 | Snapshotless `restore-all` removes the rebuild hold and exits zero | correctness / safety | high |
| 2 | F02 | Rebuild hold deletion is non-atomic and can fail after protection is gone | safety | high |
| 3 | F03 | Backups are same-hardware live copies and cannot recover a rebuilt node as promised | correctness / safety | high |
| 4 | F04 | Any PVC mover credential can mutate every PVC and etcd repository | safety | high |
| 5 | F05 | Node, role, and disk lifecycle changes are not safe convergence operations | safety / brittle | high |
| 6 | F06 | `prep-node` always invokes a nonexistent validator after making changes | correctness | high |
| 7 | F07 | The destructive disk guard knowingly misses secondary root Btrfs members | safety | high |
| 8 | F08 | Platform dependency and backup enrollment gates fail open | correctness / brittle | high |
| 9 | F09 | Four CI rules accept seeded violations of the rules they claim to enforce | correctness / safety | high |
| 10 | F10 | “Strict” manifest validation silently skips documents and permits arbitrary CRD specs | correctness | high |
| 11 | F11 | Phase verifiers can report success without required owner or restore evidence | correctness | high |
| 12 | F12 | Ansible can mutate before validation, retain stale config, and misclassify API errors | brittle | high |
| 13 | F13 | Secret tooling and CA lifecycle boundaries are incomplete | safety / brittle | high |
| 14 | F14 | One control-plane and one local backup host block HA and node-loss adaptation | rigid | high |
| 15 | F15 | Generic hardware roles actually require amd64 and NVIDIA | rigid | high |
| 16 | F16 | DNS, ingress, and CA contracts do not define safe change or multi-tenant boundaries | brittle / rigid | high |
| 17 | F17 | Backup cost and concurrency grow once per PVC on one schedule and one server | rigid | high |
| 18 | F18 | Env config is global, over-published, partly unused, and shallowly validated | complexity / rigid | high |
| 19 | F19 | Apps/services have one coupled source chain; the configured apps repo is unused | rigid / complexity | high |
| 20 | F20 | A fresh adopter cannot reach a deployment from repository docs alone | complexity | high |
| 21 | F21 | The command guard validates a version-pinned parser clone, not the actual engine | safety / complexity | high |
| 22 | F22 | Dependency and test structure create avoidable drift and false maintenance cost | brittle / complexity | high |
| 23 | F23 | Kubeconfig bootstrap overwrites the operator's default multi-cluster config | safety / complexity | high |

## 3. Findings

### F01 — Snapshotless restore removes the rebuild hold

- **Location:** `tools/restore:684-692,839-870`; `ci/tests/test_restore.py:583-589`; `platform/backups/CONTRACT.md:49`.
- **Problem:** `--all` treats “no eligible snapshots” as skippable, appends that PVC to `done`, deletes the cluster-wide rebuild hold, unpauses it, and exits zero. The contract says the hold is deleted only after every restore succeeds.
- **Evidence:**
  ```text
  pause demo/drill
  restore demo/drill as-of 2026-10-29T00:00:09Z
  SKIP no snapshot demo/drill
  HOLD lifted
  return=0 hold_deleted=True
  ```
- **Effect:** an empty recreated PVC can become the next backup state and age out the last usable history while automation reports successful recovery.
- **Proposed direction:** a held repository must restore successfully or require an explicit, repository-specific retire/accept-empty acknowledgement. Record `done` only after confirmed restoration. This simpler invariant replaces the current “skip, then infer safety later” path.
- **Compromise:** genuinely new, non-held empty PVCs need a separate explicit policy.
- **Risk / blast radius:** every repository named in `backup-rebuild-hold`; touches restore tests, the backup contract, and P0b recovery evidence.
- **Type / confidence:** correctness and destructive safety; **high**.

### F02 — Hold deletion is non-atomic

- **Location:** `tools/restore:759-804`; `ci/tests/test_restore.py:570-580`.
- **Problem:** `_lift_hold` deletes the ConfigMap before removing pause annotations and proving every ReplicationSource exists and is unpaused.
- **Evidence:** the existing test requires a failing run with `cluster.hold_deleted is True` after a source disappears.
- **Effect:** any post-delete API/source failure leaves the safety barrier gone, potentially with only some PVCs resumed.
- **Proposed direction:** preflight every expected source; after deletion, recreate the exact hold on any failure before returning. Model hold removal as a rollback-capable state transition.
- **Compromise:** deletion must precede actual unpause, so this cannot be perfectly atomic.
- **Risk / blast radius:** cluster-wide recovery; restore state-machine tests and rebuild-hold semantics.
- **Type / confidence:** safety; **high**.

### F03 — Backups do not provide hardware-loss recovery

- **Location:** `platform/backups/CONTRACT.md:29-30,44-50,59`; `platform/backups/rest-server.yaml:63-67`; `infra/ansible/roles/k3s/templates/config.yaml.j2:10-12`.
- **Problem:** current PVC and etcd backups are local hostPath data, commonly on the same disk as the source; Direct copies run against live files; rebuilt-node etcd recovery needs the original k3s token, which is not escrowed.
- **Evidence:** the contract says same-disk copies are “not disk-failure protection” and rebuilt-node restore requires the original token while only same-node restore is in scope.
- **Effect:** loss of the single node/disk can remove source data, backup data, etcd snapshots, and required recovery material together. Live database files may also be application-inconsistent.
- **Proposed direction:** define off-host/offline replication, escrow the k3s token and recovery credentials separately, and add application-native dumps/quiesce or validated crash-consistent snapshots. This replaces “backup equals local rollback copy” with an explicit recovery objective.
- **Compromise:** another storage target, credential boundary, monitoring, and application-specific hooks.
- **Risk / blast radius:** all persistent services and full-cluster rebuild; backup manifests, bootstrap secrets, Ansible recovery, and P0b drills.
- **Type / confidence:** correctness and safety; **high**.

### F04 — One credential controls every backup repository

- **Location:** `platform/backups/enrollment.yaml:34-51`; `platform/backups/rest-server.yaml:1-3`; `platform/backups/etcd-snapshots.yaml:40-61`; `platform/backups/CONTRACT.md:32-34`.
- **Problem:** every per-PVC Secret copies the same restic password and REST credentials; rest-server has neither private repositories nor append-only writers, and etcd uses the same account.
- **Evidence:** repository paths differ, but authentication does not. Any holder can address another path and prune or delete it.
- **Effect:** compromise of one privileged mover or generated Secret compromises all PVC and etcd recovery history.
- **Proposed direction:** use per-repository/prefix-scoped credentials, or separate append-only writers from a tightly controlled maintenance/prune identity.
- **Compromise:** credential issuance and rotation become more complex; append-only storage needs a separate retention job.
- **Risk / blast radius:** every backup repository and disaster recovery; enrollment policy, bootstrap secret shape, rest-server, restore tooling.
- **Type / confidence:** safety; **high**.

### F05 — Node lifecycle is not convergent

- **Location:** `infra/ansible/roles/node-disk/tasks/main.yml:45-93`; `infra/ansible/roles/k3s/tasks/main.yml:7-98`; `infra/ansible/roles/k3s/tasks/guard.yml:10-50`; `infra/ansible/roles/k3s/tasks/labels.yml:7-28`.
- **Problem:** adding a data disk can mount an empty filesystem over active `/var/lib/jarvis`; removing it performs no cleanup. Agent/server transitions install the desired unit without rejecting or disabling the opposite one. Inventory removal does not drain/delete/revoke the Kubernetes node.
- **Evidence:** disk mount precedes any service dependency; the k3s guard records env but not mode; label reconciliation filters only current inventory nodes.
- **Effect:** role or storage edits can hide live data, start conflicting services after reboot, or leave retired nodes joined with credentials and schedulable workloads.
- **Proposed direction:** make normal convergence reject lifecycle transitions. Add explicit quiesce/migrate/decommission operations with mode markers and rollback checks.
- **Compromise:** node replacement remains an owner operation; silently inferring deletion from inventory would be more dangerous.
- **Risk / blast radius:** node additions, retirement, control-plane replacement, root-to-disk migration; Ansible role tests and operational docs.
- **Type / confidence:** safety and brittle lifecycle; **high**.

### F06 — `prep-node` cannot complete

- **Location:** `tools/prep-node:1002-1013,1075-1087`; absent path `tools/checks/env-schema`.
- **Problem:** after changing a remote node and env files, `prep-node` executes a validator from a directory that does not exist; the check lives at `ci/checks/env-schema`.
- **Evidence:**
  ```text
  FileNotFoundError: [Errno 2] No such file or directory:
  '/workspace/tools/checks/env-schema'
  ```
  No test references `run_env_schema`.
- **Effect:** the documented first-node workflow always ends with a traceback after partial local and remote mutation.
- **Proposed direction:** call the schema API or canonical check entry point, and add a full successful orchestration test that reaches “Recorded the host.”
- **Compromise:** a subprocess integration test is slower than current helper-only tests.
- **Risk / blast radius:** every greenfield adopter; prep-node, env renderer, and onboarding docs.
- **Type / confidence:** correctness; **high**.

### F07 — Root-disk protection misses a destructive layout

- **Location:** `infra/ansible/roles/node-disk/tasks/guards.yml:10-24`; `infra/ansible/roles/node-disk/tasks/main.yml:51-72`; `infra/ansible/README.md:54`.
- **Problem:** the root guard checks the reported root source and its `lsblk` parents, but explicitly does not enumerate other members of a multi-device Btrfs root.
- **Evidence:** the README names this gap; a confirmed secondary member then reaches `wipefs -a` and forced filesystem creation.
- **Effect:** a plausible wrong disk selection can destroy part of the root filesystem despite the “refuse root disk” guarantee.
- **Proposed direction:** fail closed whenever root is multi-device Btrfs unless all members are enumerated and rejected.
- **Compromise:** some legitimate Btrfs adopters need a manual migration path.
- **Risk / blast radius:** whole host; disk fixtures and root-device discovery.
- **Type / confidence:** destructive safety; **high**.

### F08 — Platform ordering and enrollment fail open

- **Location:** `envs/env-01/flux/kustomizations/platform-controllers.yaml:9-13`; `platform-configs.yaml:14-15`; `platform/backups/enrollment.yaml:7-10`; `platform/kustomization.yaml:3-10`.
- **Problem:** `platform-configs` depends on a Kustomization that has neither `wait: true` nor health checks, so HelmRelease objects can be “Ready” before their CRDs/controllers. Backup admission explicitly uses `failurePolicy: Ignore`.
- **Evidence:** Flux documents that `dependsOn` waits for the dependency's Ready condition and recommends health checks/wait when CRDs/controllers must be operational first. The repository comment deliberately avoids HelmRelease health checks.
- **Effect:** first boot/upgrades can apply Certificate, ClusterPolicy, and backup objects before dependencies are usable; PVCs can be admitted without enrollment during Kyverno failure.
- **Proposed direction:** health-check concrete HelmReleases/deployments/CRDs and fail closed for mandatory enrollment, with an explicit emergency override.
- **Compromise:** a public chart outage can block dependent configuration or PVC creation; that is an honest dependency failure.
- **Risk / blast radius:** first deployment, controller upgrades, every new PVC; Flux entry, Kyverno, cert-manager, VolSync tests.
- **Type / confidence:** correctness and brittle ordering; **high**.

### F09 — CI rules accept seeded violations

- **Location:** `ci/checks/lib/_manifest_lint.py:37-51,90-118`; `_digest_pin.py:76-106`; `_loadbalancer.py:16,26-40`; `_pvc_backup.py:92-116`.
- **Problem:** Helm R5 fields are checked only if present; split `repository`/`tag` image fields are missed; any LoadBalancer in two directories is allowed; any listed ClusterPolicy satisfies enrollment presence.
- **Evidence:**
  ```text
  unrelated allowed-dir LoadBalancer: []
  empty Helm values R5: []
  split image fields R33: []
  unrelated backup policy: []
  ```
- **Effect:** services/apps can land without resources/probes or digest pins, an unrelated LAN LoadBalancer can pass, and the backup check can claim enrollment exists when it does not.
- **Proposed direction:** use exact object identities for narrow allow-lists, minimum policy semantics, and chart-aware rendering or explicit per-chart declarations for R5/R33.
- **Compromise:** rendering charts adds cache/schema maintenance; exact allow-lists need updates on intentional renames.
- **Risk / blast radius:** network exposure, supply chain, resource safety, backup guarantee; four checks and their seeded tests.
- **Type / confidence:** correctness and safety; **high**.

### F10 — Validation gives false confidence

- **Location:** `ci/checks/lib/_manifests.py:13-42`; `ci/tests/fixtures/schemas/volsync.backube/replicationsource_v1alpha1.json:1-11`; `replicationdestination_v1alpha1.json:1-11`; `apiextensions.k8s.io/customresourcedefinition_v1.json:1-11`.
- **Problem:** non-empty YAML that is not a mapping is silently ignored by three checks. The custom “strict” schemas allow arbitrary `spec` properties for the most recovery-sensitive CRDs.
- **Evidence:** a `[]` manifest produced no manifest-lint, digest, or LoadBalancer violation. Each schema sets `spec.additionalProperties: true`.
- **Effect:** malformed deployment files and misspelled VolSync/CRD fields can pass the checks presented as strict validation.
- **Proposed direction:** reject non-mapping deployment documents and vendor/generated OpenAPI schemas from the exact pinned CRDs.
- **Compromise:** controller upgrades must update schemas; genuine data-only YAML needs explicit exclusion.
- **Risk / blast radius:** all manifest checks, especially backup and restore resources.
- **Type / confidence:** correctness; **high**.

### F11 — Exit checks can pass without proof

- **Location:** `tools/verify/p0a:210-233`; `tools/verify/p0b:126-154,158-218`; `ci/tests/test_verify_p0a.py:82-87`.
- **Problem:** both phase “exit checks” return zero while mandatory owner checklist items remain. P0b treats any post-deletion line containing “restore” as success ordering and accepts repeated snapshot-labelled hashes without source/restored labels.
- **Evidence:** a transcript containing a restore-failure line and duplicated source-only checksum text returned a 64-character “recorded checksum.” The P0a test requires exit zero plus “owner items remain.”
- **Effect:** automation can mark an incomplete phase or failed restore drill as passed.
- **Proposed direction:** separate `automatic-check` from `exit-attestation`; the latter fails until structured, exact proof records are present.
- **Compromise:** human checks need a small attestation format rather than arbitrary transcripts.
- **Risk / blast radius:** phase gates and disaster-recovery confidence; verify tests and operator workflow.
- **Type / confidence:** correctness; **high**.

### F12 — Ansible does not consistently fail closed or converge

- **Location:** `infra/ansible/site.yml:1-30`; `roles/flux-operator/tasks/operator.yml:2-47`; `sops_age.yml:2-44`; `roles/base-os/tasks/main.yml:46-66`.
- **Problem:** complete variable/architecture validation occurs after base OS mutation; Flux/secret lookups treat every error as absence; an existing operator is not checked for availability; changed Chrony config has no restart notification.
- **Evidence:** `instance.yml` correctly distinguishes NotFound, but operator and secret lookups use `failed_when: false`. The Chrony template has no handler.
- **Effect:** malformed runs partially mutate hosts, transient API/RBAC failures trigger bootstrap paths, crash-looping Flux can appear installed, and NTP changes remain inactive until reboot.
- **Proposed direction:** one preflight validates all hosts/vars before mutation; classify NotFound explicitly; always verify readiness; notify validated reload/restart handlers.
- **Compromise:** stricter reruns stop sooner during control-plane outages.
- **Risk / blast radius:** every Ansible run and Flux bootstrap; role proof fixtures and idempotence checks.
- **Type / confidence:** brittle convergence; **high**.

### F13 — Secret lifecycle boundaries are incomplete

- **Location:** `tools/seal:24-52`; `tools/bootstrap-secrets:336-360`; `tools/lib/_sops.py:24-37`; `infra/ansible/roles/flux-operator/tasks/sops_age.yml:26-63`; `envs/env-01/secrets/cert-manager-ca.sops.yaml:1-7`.
- **Problem:** env/name path components are not constrained to the requested env subtree; the age key is checked for existence/content but not safe ownership/mode; unlike the restic bootstrap Secret, the CA Secret lacks prune protection.
- **Evidence:** `_sops` confines writes to the repository but not `envs/<env>/secrets`; no traversal/name tests exist. The CA metadata has no prune-disabled annotation.
- **Effect:** a bad identifier can target another in-repo subtree, an overly readable private key is accepted, and a path move/removal can delete the live CA key until explicitly recovered.
- **Proposed direction:** validate identifiers and resolved parents, enforce regular-file ownership/mode, and give irreplaceable bootstrap objects explicit retention/rotation semantics.
- **Compromise:** intentional rotations/deletions require an explicit operation.
- **Risk / blast radius:** all encrypted bootstrap material and internal TLS; bootstrap/seal tests, SOPS metadata, prune guards.
- **Type / confidence:** safety and brittle lifecycle; **high**.

### F14 — Cardinality hard-codes a non-HA topology

- **Location:** `schema/env.py:72-78,674-681`; `schema/CONTRACT.md:88-98`; `infra/ansible/site.yml:14-25`; `platform/backups/rest-server.yaml:25-29`.
- **Problem:** every env must have exactly one control-plane and one backup host; all other nodes are agents; backup storage is pinned to that one host.
- **Evidence:** schema and Ansible both enforce singleton control plane; there is no multi-server join path or backup failover.
- **Effect:** adding nodes improves capacity but not control-plane or backup availability. Loss of either singleton role is an outage requiring manual reconstruction.
- **Proposed direction:** keep single-node as a profile, but allow an odd-sized server set and a separately designed replicated/off-host backup target when those future uses are selected.
- **Compromise:** HA etcd and replicated storage are materially more complex; do not make them mandatory for small installs.
- **Risk / blast radius:** more nodes, node loss, other households; schema, inventory, k3s role, labels, backups.
- **Type / confidence:** rigid; **high**.

### F15 — Hardware-neutral names hide hard constraints

- **Location:** `infra/ansible/roles/k3s/tasks/main.yml:28-32`; `infra/ansible/tasks/assert-gpu.yml:22-31`; `schema/env.py:46-51`; `tools/versions.env:1-29`.
- **Problem:** controller tools support amd64 and arm64, but the cluster schema has one amd64 k3s checksum and the role rejects non-amd64. The generic `gpu` role asserts NVIDIA vendor ID only.
- **Evidence:** explicit `architecture == x86_64` and vendor `0x10de` assertions.
- **Effect:** common ARM home-lab nodes and non-NVIDIA accelerators cannot be represented despite the product's heterogeneous-hardware goal.
- **Proposed direction:** use per-architecture checksums selected from facts; either rename the role `nvidia_gpu` or make accelerator capabilities explicit.
- **Compromise:** each supported architecture/vendor needs a tested path; unsupported hardware should still fail clearly.
- **Risk / blast radius:** other people's deployments and mixed nodes; schema, pins, downloads, inventory, GPU scheduling.
- **Type / confidence:** rigid; **high**.

### F16 — DNS, ingress, and CA boundaries are underspecified

- **Location:** `platform/app-dns/helmrelease.yaml:35-68`; `platform/traefik/helmchartconfig.yaml:13-30`; `platform/cert-manager/config/internal-ca.yaml:1-22`; related contracts.
- **Problem:** App DNS watches all Ingresses while Traefik is the default class; HTTP remains enabled without a global redirect; any authorized Certificate requester can reference the cluster-wide CA; `app_dns_cluster_ip` drives an immutable Service field with no migration guard.
- **Evidence:** no ingress-class filter appears in k8s-gateway config, `isDefaultClass: true`, and a `ClusterIssuer` has no approval policy. CoreDNS immediately forwards to the configured ClusterIP.
- **Effect:** future namespace users can claim internal names or CA issuance rights; a ClusterIP edit can leave the zone pointing at an address Helm cannot apply.
- **Proposed direction:** require/filter the intended class and HTTPS policy, restrict issuer approval by namespace/SAN, monitor/rotate the root, and declare ClusterIP immutable or provide staged migration.
- **Compromise:** policy controllers and CA rotation add operations; a single-user profile can remain permissive if stated.
- **Risk / blast radius:** every LAN/tailnet endpoint and future user/service; DNS, Traefik, cert-manager, policy tests.
- **Type / confidence:** brittle and rigid; **high**.

### F17 — Backup work scales without controls

- **Location:** `platform/backups/enrollment.yaml:80-93`; `platform/backups/rest-server.yaml:14-54`; `platform/backups/CONTRACT.md:11-13`.
- **Problem:** every PVC/StatefulSet ordinal gets a source with `cacheCapacity: 1Gi`, the same cron, no mover resources, and one small rest-server replica.
- **Evidence:** the generated spec has one identical schedule and no `moverResources`; rest-server is one `Recreate` replica with a 128Mi memory limit.
- **Effect:** adding services creates synchronized storage, network, API, and server load; backup traffic can disrupt applications and itself.
- **Proposed direction:** set mover requests/limits, stagger or queue schedules, cap concurrency, and capacity-plan/monitor the server and cache volumes.
- **Compromise:** scheduling and quotas become env/component configuration.
- **Risk / blast radius:** services/apps and more PVCs; enrollment policy, schema, resource checks, backup health.
- **Type / confidence:** rigid; **high**.

### F18 — Env config is global, premature, and shallow

- **Location:** `schema/env.py:29-70,557-611`; `tools/lib/render_cluster_vars_configmap.py:34-47`; `envs/env-01/flux/generated/cluster-vars-configmap.yaml:8-34`.
- **Problem:** base deployment requires unused `apps_git_repo_url`, LLM/provider, Hermes, and app values. Hermes/apps validation mostly checks container types. The renderer validates nothing itself and exports every key, including `ssh_user`, k3s checksum, and controller-local `age_key_file`.
- **Evidence:** repository searches find no runtime consumer for `apps_git_repo_url` or `llm_provider_host`; app/Hermes values appear only in env/schema/tests. The generated ConfigMap contains all host-only values.
- **Effect:** adopters must invent unavailable future dependencies, malformed future config can pass, and broad substitutions expose controller layout and invite accidental variable collisions.
- **Proposed direction:** component-owned optional schemas/config maps activated only when a component exists; render an explicit platform substitution allow-list after validating the selected env.
- **Compromise:** more than one small config artifact and conditional validation.
- **Risk / blast radius:** second env, services/apps, local LLMs, other hardware; schema, generator, Flux substitutions, core-names.
- **Type / confidence:** complexity and rigid configuration; **high**.

### F19 — Product layers are coupled before they exist

- **Location:** `envs/env-01/flux/kustomizations/services.yaml:4-19`; `apps.yaml:4-19`; `apps/placeholder.yaml`; `services/placeholder.yaml`.
- **Problem:** all services share one Kustomization, all apps wait on it, and apps use the main `flux-system` GitRepository even though `apps_git_repo_url` promises a separate source.
- **Evidence:** both sourceRefs name `flux-system`; `apps_git_repo_url` has no runtime reference. The only deployed objects are placeholder ConfigMaps.
- **Effect:** one broken service can block every app; app publishing cannot target its configured repo without changing the source topology; restore suspension is layer-wide.
- **Proposed direction:** when the first real workloads land, use a dedicated apps source and split only operationally independent domains/components with explicit health dependencies.
- **Compromise:** more Flux objects; avoid one Kustomization per trivial object.
- **Risk / blast radius:** services, apps, deploy failure isolation, restores, second repo.
- **Type / confidence:** rigid and complexity; **high**.

### F20 — No repository-only adopter path

- **Location:** `README.md:1-5`; `docs/README.md:1-15`; `envs/env-01/README.md:23-31`; `infra/ansible/README.md:1-37`.
- **Problem:** the root is five lines and links to `https://github.com/<owner>/project-plans/...`. A new env guide says “add a flux tree” but does not define it. The end-to-end sequence is spread across at least six files.
- **Evidence:** on paper, a fresh adopter must infer repo variables, age setup, node preparation, Flux bootstrap, CA trust, and readiness from separate contracts and owner-specific env docs.
- **Effect:** a full-copy adopter cannot reach a first deployment using only the obvious repository entry point.
- **Proposed direction:** one tested “new household” runbook and a scaffold/validator for env + Flux shape; link out only for rationale, not required build steps.
- **Compromise:** the runbook must be maintained as an executable contract.
- **Risk / blast radius:** every new engineer, second env, and fork; docs, Make targets, prep-node, schema.
- **Type / confidence:** complexity; **high**.

### F21 — Command-guard assurance is self-referential

- **Location:** `ci/checks/lib/_cli_permissions.py:1-12,570-605`; `ci/checks/cli-deny:25-39`; `.cursor/cli.json`.
- **Problem:** a 605-line local parser mirrors one dated Cursor-agent build, documents a coverage category it does not fully model, and then “dry-runs” only itself.
- **Evidence:** neither check invokes the actual permission engine. Broad patterns also intentionally block harmless text, increasing pressure to weaken the list.
- **Effect:** CI can stay green while the runtime parser changes; the guard creates both false confidence and false positives.
- **Proposed direction:** make an integration test through the actual pinned permission engine authoritative; retain a small category-level policy suite rather than a shell parser clone.
- **Compromise:** this depends on a stable CLI test interface and pinned runner version.
- **Risk / blast radius:** agent-only protection against cluster writes and secret reads. No working bypass is recorded here.
- **Type / confidence:** safety assurance and complexity; **high**.

### F22 — Reproducibility and test structure drift

- **Location:** `requirements-dev.txt:1-4`; `.github/workflows/ci.yml:25`; `ci/tests/test_flux_operator.py:61-92,490-529`; tree-rescan tests in `test_digest_pin.py`, `test_loadbalancer.py`, `test_manifest_lint.py`; `renovate.json:159-161`.
- **Problem:** three Python dependencies are open-ended despite “pinned” CI wording; a fresh-cluster test accepts a failed play; tests duplicate whole-tree checks and exact play ordering; test modules import helpers from other test modules; Renovate still targets removed `platform/backups/test/`.
- **Evidence:** the suite is 6,410 lines versus about 6,900 lines of non-test Python/YAML logic, and several assertions encode file shape prohibited by `rulebooks/coding-agents.md`.
- **Effect:** unrelated refactors break tests while real fail-open paths survive; dependency behavior changes without a repo commit.
- **Proposed direction:** lock the Python environment, require successful behavior fixtures, move shared helpers to support code, and delete redundant tree/shape tests and stale rules.
- **Compromise:** lock refreshes become routine maintenance and tests assert less internal shape.
- **Risk / blast radius:** all CI and contributor workflows; no runtime behavior.
- **Type / confidence:** brittle and complexity; **high**.

### F23 — Kubeconfig bootstrap clobbers operator state

- **Location:** `infra/ansible/roles/kubeconfig/tasks/main.yml:3-49`.
- **Problem:** every run installs Jarvis credentials at `$HOME/.kube/config`, replacing an existing multi-cluster config; `backup: true` accumulates credential-bearing copies.
- **Evidence:** destination is fixed to `~/.kube/config`; there is no merge or opt-in replacement.
- **Effect:** an adopter can lose current contexts and leave extra credential files on the controller.
- **Proposed direction:** write `~/.kube/jarvis-<env>.yaml` and make merge/replacement an explicit operator action.
- **Compromise:** commands need `KUBECONFIG` or an explicit merge step.
- **Risk / blast radius:** controller workstation/account, especially multiple envs; kubeconfig role and docs.
- **Type / confidence:** safety and complexity; **high**.

## 4. Better-approach proposals

| Proposal | Future use served | Pros | Cons / compromise | Size |
|---|---|---|---|---|
| Transactional restore state machine | services, apps, rebuilt clusters | One fail-closed invariant; resumable recovery; exact audit trail | More explicit states and rollback tests | M |
| Off-host recovery tier with separately escrowed token/keys | node or disk loss, other households | Meets rebuild-from-repo+env+key goal; real disaster recovery | Additional storage, credentials, monitoring, restore drills | L |
| Explicit node/storage lifecycle commands | more nodes, replacements, disk migration | Prevents normal convergence from performing destructive transitions | Owner-run procedures remain necessary | L |
| Controller/config health gates | first boot, upgrades, more controllers | Dependencies mean operational readiness, not object creation | External chart outage blocks dependents | M |
| Component-owned env schemas and Flux boundaries | services, apps, second env, local models | Smaller config surfaces, failure isolation, no fake future values | More small artifacts and dependencies | L |
| Env scaffold plus repository-only runbook | second env, other people's deployments | Fewer copied paths; testable onboarding | Generator/runbook maintenance | M |
| Capability profiles for architecture, accelerator, HA, backup | different hardware, more nodes | Honest support matrix without forcing HA on small installs | More test matrix; unsupported combinations must fail clearly | L |
| Runtime permission-engine integration | AI-agent authoring workflow | Removes parser clone and false confidence | Depends on Cursor exposing stable test behavior | M |

## 5. Keep list

- SOPS ciphertext-only Git workflow and metadata validation.
- Refusal to overwrite a differing live `sops-age` Secret.
- k3s environment marker that rejects foreign/unmarked installations.
- Disk double confirmation, stable by-id requirement, label adoption, and current root-parent checks (extend; do not replace).
- Example `env-00` as the every-key/every-role schema canary.
- Strict unknown-key rejection and generated ConfigMap drift test.
- Digest pins, checksum-verified CLI downloads, Renovate grouping, and pinned GitHub actions.
- Full Kustomize builds, native Kubernetes schema validation, duplicate-object detection.
- Seeded gitleaks age-key test and atomic ciphertext writes.
- Prune guards for controller CRDs/namespaces and `backups-restic`.
- PVC enrollment default with explicit, narrow `backup: none` allow-list.
- Vendored VolumeSnapshot CRD: it matches upstream v8.4.0 except the intentional prune guard.
- Read-only host/cluster verification behavior and output redaction.

## 6. Delete list

| Item | Evidence | Why it can go |
|---|---|---|
| `apps/placeholder.yaml` and `services/placeholder.yaml` | Only referenced by their two Kustomizations; no consumer | Empty Kustomizations are sufficient until real resources land; avoids fake cluster objects |
| Removed `platform/backups/test/` Renovate pattern | Directory does not exist; old test bundle is documented as removed | Dead update rule |
| Root-tree rescan tests in `test_digest_pin.py`, `test_loadbalancer.py`, `test_manifest_lint.py` | CI already runs those exact scanners against the same tree | Duplicate execution, not behavioral proof |
| Exact eight-play ordering assertions in `test_flux_operator.py` | Tag/dependency behavior has separate tests; rulebook forbids ordering/file-shape tests | Locks implementation shape without proving safety |
| `envs/env-01/README.md` one-time `volsync-test` cleanup paragraph, after owner cleanup | It describes removed historical state | Build docs should not carry completed migration history |

Do not delete the VolumeSnapshot CRD, encrypted future secrets, or component CONTRACTs merely because their consumers are not present yet; those require a deliberate scope/config decision, not file cleanup.

## 7. Ready for growth?

- **Services hurt first:** one `services` Kustomization is the failure, suspension, and substitution boundary for every future service. Split only independent operational domains as they land.
- **Apps hurt next:** `apps_git_repo_url` is unused and the apps Kustomization reads this repository. Add the real source before app publishing exists.
- **PVC growth hurts immediately:** every claim adds a synchronized cache/mover against one small server. Add resources/concurrency before the second stateful service.
- **Nodes add capacity, not resilience:** singleton control-plane/backup roles and local paths remain single points of failure.
- **Second env is copy-driven:** at least the values, inventory, SOPS rule, generated map, Flux instance, four Kustomizations, and platform-config path must be assembled manually. A scaffold is the smallest prevention.
- **More users are not isolated:** namespace users can affect internal DNS names, ClusterIssuer use, and shared backup credentials. State the single-tenant boundary now; add policy before multi-user access.

## 8. Ready for other people?

Ranked adopter blockers and smallest fixes:

1. **Broken `prep-node`:** point it at the canonical validator and test the whole happy path.
2. **No complete entry point:** add one greenfield runbook with exact command order and readiness checks.
3. **No hardware-loss recovery:** distinguish rollback from disaster recovery and provide one supported off-host path.
4. **Unused required config:** make future component config optional until enabled.
5. **Hardware assumptions:** publish the amd64/NVIDIA/single-server support matrix, then add capability profiles only when tested.
6. **Manual env Flux tree:** provide a scaffold that emits all env-owned files and validates no stale env name.
7. **Controller side effects:** preserve default kubeconfig and validate all Ansible preconditions before changes.

External prerequisites (keep outside the cluster):

- Home router forwards the env DNS suffix to App DNS on edge node(s).
- Home router provides Tailscale/tailnet remote access; no cluster ingress is internet-facing.
- Every client trusts the env's committed internal CA root.
- Inventory hostnames resolve from the controller and every node; TCP/UDP 53 are available on edge nodes.
- Ubuntu Server, SSH key access, passwordless sudo, and currently amd64 cluster nodes.
- Outbound access to Git, image registries, Helm/OCI repositories, and pinned binary release hosts.
- GitHub repositories/settings for the current provider, including `OWNER_NAME` and required checks.
- A backed-up age private key, restic credentials, CA material, and (after F03) k3s token.

## 9. Coverage table

`excluded` means the file was checked only against its source/metadata as required.

| File | Tag |
|---|---|
| `.cursor/cli.json` | finding (F21) |
| `.github/pull_request_template.md` | no finding |
| `.github/workflows/ci.yml` | finding (F22) |
| `.gitignore` | no finding |
| `.mailmap` | no finding |
| `.sops.yaml` | no finding (metadata/rules checked) |
| `AGENTS.md` | no finding |
| `Makefile` | no finding |
| `README.md` | finding (F20) |
| `apps/kustomization.yaml` | finding (F19/delete) |
| `apps/placeholder.yaml` | finding (delete) |
| `ci/checks/cli-deny` | finding (F21) |
| `ci/checks/contract-md` | no finding |
| `ci/checks/core-names` | no finding |
| `ci/checks/digest-pin` | finding (F09) |
| `ci/checks/env-schema` | no finding |
| `ci/checks/lib/_cli_permissions.py` | finding (F21) |
| `ci/checks/lib/_contract_md.py` | no finding |
| `ci/checks/lib/_core_names.py` | no finding |
| `ci/checks/lib/_digest_pin.py` | finding (F09) |
| `ci/checks/lib/_loadbalancer.py` | finding (F09) |
| `ci/checks/lib/_manifest_lint.py` | finding (F09) |
| `ci/checks/lib/_manifests.py` | finding (F10) |
| `ci/checks/lib/_product_ci.py` | no finding |
| `ci/checks/lib/_pvc_backup.py` | finding (F09) |
| `ci/checks/loadbalancer` | finding (F09) |
| `ci/checks/manifest-lint` | finding (F09) |
| `ci/checks/product-ci` | no finding |
| `ci/checks/pvc-backup` | finding (F09) |
| `ci/checks/secret-scan` | no finding |
| `ci/ci-local-verify` | no finding |
| `ci/rule-checks.txt` | no finding |
| `ci/run-checks` | no finding |
| `ci/tests/ansible_role_proof.py` | no finding |
| `ci/tests/conftest.py` | no finding |
| `ci/tests/fixtures/ansible-list-tasks/hosts.yml` | no finding |
| `ci/tests/fixtures/digest-pin/tag-without-digest.yaml` | no finding |
| `ci/tests/fixtures/gpu-assert/hosts.yml` | no finding |
| `ci/tests/fixtures/gpu-assert/play.yml` | no finding |
| `ci/tests/fixtures/loadbalancer/extra-service.yaml` | finding (F09 coverage gap) |
| `ci/tests/fixtures/manifest-lint/missing-memory-limit.yaml` | finding (F09 coverage gap) |
| `ci/tests/fixtures/node-disk/case.yml` | no finding |
| `ci/tests/fixtures/node-disk/refusals.yml` | finding (F07 missing case) |
| `ci/tests/fixtures/node-disk/service.yml` | no finding |
| `ci/tests/fixtures/schemas/apiextensions.k8s.io/customresourcedefinition_v1.json` | finding (F10) |
| `ci/tests/fixtures/schemas/helm.cattle.io/HelmChartConfig_v1.json` | no finding |
| `ci/tests/fixtures/schemas/volsync.backube/replicationdestination_v1alpha1.json` | finding (F10) |
| `ci/tests/fixtures/schemas/volsync.backube/replicationsource_v1alpha1.json` | finding (F10) |
| `ci/tests/test_ansible_operator_tags.py` | no finding |
| `ci/tests/test_app_dns.py` | no finding |
| `ci/tests/test_backup_enrollment.py` | finding (F08) |
| `ci/tests/test_base_os.py` | finding (F12 missing reload case) |
| `ci/tests/test_bootstrap_secrets.py` | no finding |
| `ci/tests/test_cert_manager.py` | finding (F13/F16 gaps) |
| `ci/tests/test_cli_permissions.py` | finding (F21) |
| `ci/tests/test_contract_md.py` | no finding |
| `ci/tests/test_core_names.py` | no finding |
| `ci/tests/test_digest_pin.py` | finding (F09/F22) |
| `ci/tests/test_env_schema.py` | finding (F18 gaps) |
| `ci/tests/test_flux_entry.py` | finding (F08/F10) |
| `ci/tests/test_flux_operator.py` | finding (F12/F22) |
| `ci/tests/test_flux_operator_install.py` | no finding |
| `ci/tests/test_install_deps.py` | finding (F22 narrow coverage) |
| `ci/tests/test_inventory_roles.py` | no finding |
| `ci/tests/test_k3s.py` | finding (F05 transition gaps) |
| `ci/tests/test_kubeconfig.py` | finding (F23) |
| `ci/tests/test_loadbalancer.py` | finding (F09/F22) |
| `ci/tests/test_manifest_lint.py` | finding (F09/F22) |
| `ci/tests/test_node_disk.py` | finding (F05/F07 gaps) |
| `ci/tests/test_pci_gpu.py` | finding (F15) |
| `ci/tests/test_prep_node.py` | finding (F06 missing orchestration) |
| `ci/tests/test_product_ci.py` | no finding |
| `ci/tests/test_prune_guards.py` | finding (F13 missing CA) |
| `ci/tests/test_pvc_backup.py` | finding (F09) |
| `ci/tests/test_renovate.py` | finding (F22 stale rule not caught) |
| `ci/tests/test_restic_rest_server.py` | finding (F17) |
| `ci/tests/test_restore.py` | finding (F01/F02) |
| `ci/tests/test_testapp.py` | no finding |
| `ci/tests/test_traefik.py` | finding (F16) |
| `ci/tests/test_verify_p0a.py` | finding (F11) |
| `ci/tests/test_verify_p0b.py` | finding (F11) |
| `docs/CONTRACT.template.md` | no finding |
| `docs/README.md` | finding (F20) |
| `docs/code-delivery/CONTRACT.md` | finding (F18/F20 future-state prose) |
| `envs/env-00/inventory/hosts.yml` | no finding |
| `envs/env-00/values/apps.yaml` | finding (F18) |
| `envs/env-00/values/cluster-vars.yaml` | finding (F18) |
| `envs/env-00/values/hermes.yaml` | finding (F18) |
| `envs/env-01/README.md` | finding (F20/delete history) |
| `envs/env-01/ca.crt` | no finding (public certificate metadata checked) |
| `envs/env-01/flux/flux-instance.yaml` | finding (F14/F19) |
| `envs/env-01/flux/generated/cluster-vars-configmap.yaml` | excluded: generated; source checked, output matched |
| `envs/env-01/flux/kustomization.yaml` | finding (F18/F19) |
| `envs/env-01/flux/kustomizations/apps.yaml` | finding (F18/F19) |
| `envs/env-01/flux/kustomizations/platform-configs.yaml` | finding (F08) |
| `envs/env-01/flux/kustomizations/platform-controllers.yaml` | finding (F08) |
| `envs/env-01/flux/kustomizations/services.yaml` | finding (F18/F19) |
| `envs/env-01/flux/platform-configs/kustomization.yaml` | finding (F08/F13) |
| `envs/env-01/inventory/hosts.yml` | finding (F03/F14) |
| `envs/env-01/secrets/README.md` | finding (F13/F20) |
| `envs/env-01/secrets/backups-restic.sops.yaml` | excluded: encrypted payload; metadata checked, prune guard present |
| `envs/env-01/secrets/cert-manager-ca.sops.yaml` | finding (F13; encrypted payload excluded, metadata checked) |
| `envs/env-01/secrets/grafana-admin.sops.yaml` | excluded: encrypted payload; metadata checked |
| `envs/env-01/secrets/hermes-code-factory-api.sops.yaml` | excluded: encrypted payload; metadata checked |
| `envs/env-01/secrets/hermes-code-factory-dashboard.sops.yaml` | excluded: encrypted payload; metadata checked |
| `envs/env-01/secrets/hermes-ops-api.sops.yaml` | excluded: encrypted payload; metadata checked |
| `envs/env-01/secrets/hermes-ops-dashboard.sops.yaml` | excluded: encrypted payload; metadata checked |
| `envs/env-01/values/apps.yaml` | finding (F18) |
| `envs/env-01/values/cluster-vars.yaml` | finding (F14/F18) |
| `envs/env-01/values/hermes.yaml` | finding (F18) |
| `infra/ansible/README.md` | finding (F03/F05/F07/F20) |
| `infra/ansible/ansible.cfg` | no finding |
| `infra/ansible/group_vars/all.yml` | no finding |
| `infra/ansible/roles/base-os/defaults/main.yml` | no finding |
| `infra/ansible/roles/base-os/handlers/main.yml` | finding (F12) |
| `infra/ansible/roles/base-os/tasks/main.yml` | finding (F05/F12) |
| `infra/ansible/roles/base-os/templates/20auto-upgrades.j2` | no finding |
| `infra/ansible/roles/base-os/templates/50unattended-upgrades.j2` | no finding |
| `infra/ansible/roles/base-os/templates/chrony.conf.j2` | finding (F12) |
| `infra/ansible/roles/base-os/templates/journald-jarvis.conf.j2` | no finding |
| `infra/ansible/roles/base-os/templates/resolved-stub.conf.j2` | finding (F05 one-way edge transition) |
| `infra/ansible/roles/flux-operator/defaults/main.yml` | finding (F12/F22) |
| `infra/ansible/roles/flux-operator/tasks/chart_pin.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/helm_cli.yml` | finding (F22 unverified pre-existing binary) |
| `infra/ansible/roles/flux-operator/tasks/instance.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/main.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/operator.yml` | finding (F12) |
| `infra/ansible/roles/flux-operator/tasks/rebuild_hold.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/sops_age.yml` | finding (F12/F13) |
| `infra/ansible/roles/flux-operator/templates/backup-rebuild-hold.yaml.j2` | no finding |
| `infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2` | finding (F14/F19) |
| `infra/ansible/roles/flux-operator/templates/helm-upgrade.cmd.j2` | finding (F22) |
| `infra/ansible/roles/flux-operator/templates/sops-age-secret.yaml.j2` | no finding |
| `infra/ansible/roles/k3s/defaults/main.yml` | finding (F03/F14) |
| `infra/ansible/roles/k3s/handlers/main.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/guard.yml` | finding (F05) |
| `infra/ansible/roles/k3s/tasks/labels.yml` | finding (F05) |
| `infra/ansible/roles/k3s/tasks/main.yml` | finding (F05/F12/F15) |
| `infra/ansible/roles/k3s/tasks/token.yml` | finding (F03) |
| `infra/ansible/roles/k3s/templates/agent-config.yaml.j2` | finding (F05) |
| `infra/ansible/roles/k3s/templates/config.yaml.j2` | finding (F03/F14) |
| `infra/ansible/roles/k3s/templates/k3s.service.j2` | no finding |
| `infra/ansible/roles/k3s/templates/label-patches.json.j2` | no finding |
| `infra/ansible/roles/kubeconfig/tasks/main.yml` | finding (F23) |
| `infra/ansible/roles/node-disk/handlers/main.yml` | no finding |
| `infra/ansible/roles/node-disk/tasks/guards.yml` | finding (F07) |
| `infra/ansible/roles/node-disk/tasks/k3s-service.yml` | finding (F05) |
| `infra/ansible/roles/node-disk/tasks/main.yml` | finding (F05/F07) |
| `infra/ansible/roles/node-disk/templates/k3s-mount.conf.j2` | finding (F05) |
| `infra/ansible/roles/node-disk/vars/main.yml` | finding (F05) |
| `infra/ansible/site.yml` | finding (F05/F12/F14) |
| `infra/ansible/tasks/assert-gpu.yml` | finding (F15) |
| `infra/inventory/roles.yml/constructed.yml` | no finding |
| `infra/inventory/roles.yml/groups.yml` | no finding |
| `platform/app-dns/CONTRACT.md` | finding (F16/F20 prerequisites) |
| `platform/app-dns/coredns-custom.yaml` | finding (F16) |
| `platform/app-dns/helmrelease.yaml` | finding (F16) |
| `platform/app-dns/helmrepository.yaml` | no finding |
| `platform/app-dns/kustomization.yaml` | no finding |
| `platform/app-dns/namespace.yaml` | no finding |
| `platform/backups/CONTRACT.md` | finding (F01-F04/F17) |
| `platform/backups/controller/helmrelease.yaml` | finding (F08/F17) |
| `platform/backups/controller/helmrepository.yaml` | no finding |
| `platform/backups/controller/kustomization.yaml` | no finding |
| `platform/backups/controller/namespace.yaml` | no finding |
| `platform/backups/controller/volumesnapshot-crd.yaml` | excluded: vendored; matches upstream v8.4.0 plus prune annotation |
| `platform/backups/enrollment.yaml` | finding (F04/F08/F17) |
| `platform/backups/etcd-snapshots.yaml` | finding (F03/F04/F22 duplicate pin) |
| `platform/backups/kustomization.yaml` | no finding |
| `platform/backups/namespace.yaml` | no finding |
| `platform/backups/rest-server.yaml` | finding (F03/F04/F17) |
| `platform/cert-manager/CONTRACT.md` | finding (F13/F16) |
| `platform/cert-manager/config/internal-ca.yaml` | finding (F16) |
| `platform/cert-manager/config/kustomization.yaml` | no finding |
| `platform/cert-manager/controller/helmrelease.yaml` | finding (F08) |
| `platform/cert-manager/controller/helmrepository.yaml` | no finding |
| `platform/cert-manager/controller/kustomization.yaml` | no finding |
| `platform/cert-manager/controller/namespace.yaml` | no finding |
| `platform/flux-operator/CONTRACT.md` | no finding |
| `platform/flux-operator/helmrelease.yaml` | no finding |
| `platform/flux-operator/kustomization.yaml` | no finding |
| `platform/flux-operator/oci-repository.yaml` | no finding |
| `platform/kustomization.yaml` | finding (F08) |
| `platform/kyverno/CONTRACT.md` | finding (F08) |
| `platform/kyverno/helmrelease.yaml` | finding (F08) |
| `platform/kyverno/helmrepository.yaml` | no finding |
| `platform/kyverno/kustomization.yaml` | no finding |
| `platform/kyverno/namespace.yaml` | no finding |
| `platform/kyverno/rbac.yaml` | finding (F08) |
| `platform/testapp/CONTRACT.md` | no finding |
| `platform/testapp/kustomization.yaml` | no finding |
| `platform/testapp/namespace.yaml` | no finding |
| `platform/testapp/testapp.yaml` | no finding |
| `platform/traefik/CONTRACT.md` | finding (F16) |
| `platform/traefik/helmchartconfig.yaml` | finding (F16) |
| `platform/traefik/kustomization.yaml` | no finding |
| `pytest.ini` | no finding |
| `renovate.json` | finding (F22/delete) |
| `requirements-dev.txt` | finding (F22) |
| `rulebooks/coding-agents.md` | no finding |
| `schema/CONTRACT.md` | finding (F14/F18/F20 future references) |
| `schema/__init__.py` | no finding |
| `schema/env.py` | finding (F14/F18) |
| `services/kustomization.yaml` | finding (F19/delete) |
| `services/placeholder.yaml` | finding (delete) |
| `tools/bootstrap-secrets` | finding (F13/F18 premature secrets) |
| `tools/install-deps` | no finding |
| `tools/lib/_sops.py` | finding (F13) |
| `tools/lib/render_cluster_vars_configmap.py` | finding (F18) |
| `tools/prep-node` | finding (F06/F13) |
| `tools/restore` | finding (F01/F02/F03) |
| `tools/seal` | finding (F13) |
| `tools/verify/p0a` | finding (F11) |
| `tools/verify/p0b` | finding (F11) |
| `tools/versions.env` | finding (F15/F22 duplicated version relationships) |

Coverage count: **215 / 215 tracked files tagged**.

## 10. Proposed PR sequence

1. **Restore fail-closed fixes** — F01/F02, focused tests and contract correction (**M**).
2. **Backup credential isolation design** — F04, one design PR before implementation; implementation separate (**L**).
3. **Recovery objective and off-host proof** — F03, including token escrow and application-consistency drills (**L**).
4. **Fix `prep-node` and add orchestration coverage** — F06 (**S**).
5. **Disk safety guards** — reject active/non-empty migration and multi-device Btrfs (**M**).
6. **Explicit node mode/decommission workflows** — remaining F05 (**L**).
7. **Controller readiness and enrollment gates** — F08 (**M**).
8. **CI rule seeded gaps** — one PR per rule family: exposure, R5, digest, enrollment/schema (**S/M each**).
9. **Verifier proof format and exit semantics** — F11 (**M**).
10. **Ansible fail-closed convergence** — preflight, API classification, Chrony handler (**M**).
11. **Secret boundary hardening and CA retention** — F13 (**M**).
12. **Adopter runbook + env scaffold** — F20 (**M**).
13. **Component-scoped config and app source** — F18/F19, land with first real service/app (**L**).
14. **Hardware/topology profiles** — F14/F15, only for tested target combinations (**L**).
15. **DNS/CA policy and migration contracts** — F16 before multi-user support (**M/L**).
16. **CI/test simplification and dependency lock** — F21/F22/F23, separate from runtime safety work (**M**).
