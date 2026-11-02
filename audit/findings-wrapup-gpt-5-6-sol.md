# Jarvis wrap-up audit — GPT-5.6 Sol

Audit target: `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1`

## Big picture

The foundation is unusually explicit about invariants. Host preparation, k3s ownership, Flux ordering, SOPS boundaries, manifest resources, PVC enrollment, restore quiescence, and environment substitution all have fail-closed checks. The backup tooling is particularly strong for logical loss: it pauses writers, proves a real PVC copy, keeps a rebuild hold, and fails on an empty restore. Core is also clean of owner identity outside the deliberate env, mailmap, and `OWNER_NAME` surfaces.

It is not yet a polished product for a new adopter. Three themes recur. First, the strongest checks prove the configured happy path, not the advertised recovery boundary: `make drill` can pass although a control-plane disk loss cannot be recovered because the drill neither resets etcd nor preserves/tests the k3s server token. Second, future product design has leaked into today's required schema and bootstrap: adopters configure unused Hermes/apps values, generate five unused secrets, and deploy placeholder ConfigMaps, while the one future boundary that matters now—an external apps repository—is accepted but ignored by Flux. Third, operations are split between deploy docs, an env-specific README, Ansible recovery prose, and CONTRACTs. Safe restic and age-key rotation, upstream Jarvis intake, multi-host onboarding, and component troubleshooting are either missing or buried.

Single-node and ordinary multi-node inventories render, and the code supports one NVIDIA-labeled GPU role across multiple workers. That is not mixed-GPU workload support: there is no NVIDIA device plugin, driver policy, allocatable GPU proof, or VRAM-aware placement contract. Multi-node also retains one control plane, one backup host, and local-path storage, so adding nodes does not make cluster state highly available.

The test/check suite has broad, valuable coverage, but it is larger and more structure-coupled than the product it protects. At this SHA GitHub CI is green, while both the full checkout and a genuinely isolated env-00-only worktree fail the same Chrony handler test on a non-systemd controller (`482 passed, 1 failed`). A few tests duplicate checks or test parsers/helpers rather than product behavior and can be removed.

## Ranked findings

| ID | Severity | Class | Finding | Size |
| --- | --- | --- | --- | --- |
| F01 | critical | correctness / false recovery | The disaster drill can pass although control-plane disk-loss recovery is not possible | L |
| F02 | high | safety / onboarding | `prep-node` mutates a host before validating the complete env | S |
| F03 | high | safety / recovery | The only restic rotation instruction replaces the repository password and strands existing repositories | M |
| F04 | high | security / recovery | Every backup repository shares mutable global credentials; one mover compromise reaches all backup history | L |
| F05 | high | future readiness | `apps_git_repo_url` is accepted but ignored; the apps path still reads this repository's placeholder | M |
| F06 | high | future readiness | The GPU role proves PCI vendor presence, not usable Kubernetes GPU capacity | L |
| F07 | medium | operations | There is no adopter process for Jarvis releases or coordinated k3s/Flux/chart upgrades | M |
| F08 | medium | test reliability | Full and env-00-only local verification are not portable and fail on a non-systemd controller | S |
| F09 | medium | onboarding / documentation | The new-env path is manual, omits repeat prep for all hosts, and points to a README the template does not contain | M |
| F10 | medium | complexity / cruft | Required future config, five unused secrets, and placeholder workloads exist before consumers | M |
| F11 | medium | brittleness | The pinned Kyverno release is outside its documented tested Kubernetes range | M |
| F12 | medium | reliability | Mandatory PVC enrollment fails open while Kyverno admission is unavailable | S |
| F13 | medium | too strict / reliability | Exactly one control plane makes every multi-node deployment retain a single cluster-state failure domain | L |
| F14 | medium | too strict | The product is amd64-only even though home-lab adopters commonly mix architectures | L |
| F15 | medium | operations / future readiness | Runtime diagnosis is manual; observability and endpoint checks are only placeholders/plans | L |
| F16 | medium | secret safety | Age-key rotation and a concrete offline recovery-key procedure are missing | M |
| F17 | low | test complexity | Several tests duplicate a rule check or test source shape/helper behavior | S |
| F18 | low | documentation | Eight CONTRACTs need Operations and/or Limits content | S |
| F19 | low | onboarding | `make deps` assumes populated apt indexes and an apt-based controller without documenting either | S |

## Findings

### F01 — The disaster drill can pass although control-plane disk-loss recovery is not possible

- **What breaks and for whom:** An operator can receive `PASS drill cleanup complete` and still be unable to restore cluster state after the control-plane disk dies. The drill restores an etcd snapshot file into an `emptyDir` and compares checksums; it never runs a k3s cluster reset, and no backup preserves or validates the original k3s server token required to decrypt a rebuilt datastore.
- **Location:** `tools/drill:289-316`, `platform/backups/CONTRACT.md:160-183`, `platform/backups/CONTRACT.md:187-192`
- **Question:** 1, 4, 6, 13, 18, 20
- **Evidence:** The contract says, “The tool never … run[s] a cluster reset,” then the reset procedure says, “It does not use restic,” “restoring onto a rebuilt node also needs the original k3s server token,” and “Same-node restore is the scope.” Its Limits add: “They cover mistakes and rollback, not disk loss” and “An older snapshot that exists only in the restic copy has no owner-run path yet.” `rg -n 'token|cluster-reset' tools/drill` returns no match.
- **Severity:** critical
- **Size guess:** L
- **Suggested fix:** Define and prove one real disaster boundary: copy etcd snapshots and the server token to an independent/off-host target, then exercise a disposable k3s reset from those artifacts. If Jarvis intentionally remains logical-loss-only, rename the etcd portion so it cannot be read as a recovery drill and make disk loss a prominent product limitation. The former adds credential custody and destructive-test complexity; the latter gives up disaster recovery.
- **Confidence:** high

### F02 — `prep-node` mutates a host before validating the complete env

- **What breaks and for whom:** A new adopter can install keys/packages, grant passwordless sudo, change a hostname, disable password login, and edit the env before learning that an unrelated cluster value or future provider setting is invalid.
- **Location:** `tools/prep-node:950-992`, `docs/deploy.md:35-38`
- **Question:** 4, 9, 10, 12
- **Evidence:** `run()` calls `install_login_key`, `ensure_sudo`, `configure_hostname`, `install_minimum`, `offer_ssh_passwords_off`, `apply_env_edits`, and `sync_age_key` at lines 968–983. `run_env_schema(REPO_ROOT)` is only called at lines 986–988. The deploy checklist makes prep-node step 2 and does not first run `ci/checks/env-schema`.
- **Severity:** high
- **Size guess:** S
- **Suggested fix:** Run full env validation before opening SSH or making any host/local edits, then validate again after generated edits. This may require a narrow “draft env” mode for the hostname/recipient values prep-node itself supplies.
- **Confidence:** high

### F03 — The only restic rotation instruction strands existing repositories

- **What breaks and for whom:** An operator following the documented “delete and bootstrap again” instruction gets a new random `RESTIC_PASSWORD`. Existing repository configs remain encrypted with the old password, so all scheduled backups and restores fail until the old secret is recovered from Git history.
- **Location:** `envs/env-01/secrets/README.md:25`, `tools/bootstrap-secrets:260-268`, `tools/bootstrap-secrets:342-361`
- **Question:** 1, 6, 13, 15, 17
- **Evidence:** The README says, “To rotate `backups-restic`, delete that file and run the command again.” `_restic_string_data()` generates both `RESTIC_PASSWORD` and REST server credentials with fresh random tokens. Bootstrap never opens each existing repository to run a restic key migration.
- **Severity:** high
- **Size guess:** M
- **Suggested fix:** Replace the instruction with an atomic two-phase rotation: add/test a new restic key against every repository, rotate server auth separately, update Git, then remove the old key after a restore proof. Keeping the current method is simpler but sacrifices access to retained backup history.
- **Confidence:** high

### F04 — Every backup repository shares mutable global credentials

- **What breaks and for whom:** Compromise of any VolSync mover exposes the global REST server username/password and the common restic repository password. The client can change its repository URL to another namespace/PVC and read, forget, prune, or delete that backup history. A rest-server compromise runs as root over the hostPath containing every repository.
- **Location:** `platform/backups/enrollment.yaml:35-53`, `platform/backups/rest-server.yaml:1-3`, `platform/backups/rest-server.yaml:55-70`, `platform/backups/CONTRACT.md:40-47`
- **Question:** 1, 6, 13, 17, 22
- **Evidence:** Kyverno copies the same `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, and `RESTIC_REST_PASSWORD` from `platform-backups/backups-restic` into every generated secret. The manifest states “No `--append-only`” and “No `--private-repos`”; the contract says movers prune with those credentials. The server is UID 0 with `${backup_path}/restic` mounted read/write.
- **Severity:** high
- **Size guess:** L
- **Suggested fix:** Add a separate immutable/off-host copy or isolate repository credentials per trust boundary, with prune/delete held by a narrower maintenance path. Per-repository auth and immutable retention cost more credential lifecycle and storage.
- **Confidence:** high

### F05 — `apps_git_repo_url` is accepted but ignored

- **What breaks and for whom:** A future app/code-factory operator can configure a valid external apps repository and enable app hosting, but Flux still reconciles `./apps` from the cluster repository and deploys only a placeholder ConfigMap.
- **Location:** `schema/CONTRACT.md:13-17`, `schema/CONTRACT.md:58`, `envs/env-00/flux/kustomizations/apps.yaml:7-13`, `apps/placeholder.yaml:1-8`
- **Question:** 3, 6, 7, 21, 22, 23, 24
- **Evidence:** `rg apps_git_repo_url` finds only env values, schema validation/tests, and identity scanning—no Flux source. The apps Kustomization has `sourceRef: GitRepository/flux-system` and `path: ./apps`.
- **Severity:** high
- **Size guess:** M
- **Suggested fix:** Before the first app consumer, choose and document one source boundary: a second Flux source for the apps repository, or remove `apps_git_repo_url` and keep apps in this repository. A second source adds credentials, ordering, and cross-repo release coordination; a monorepo gives up that isolation.
- **Confidence:** high

### F06 — The GPU role does not provide usable Kubernetes GPU capacity

- **What breaks and for whom:** Assistant voice/model workloads cannot request `nvidia.com/gpu`, prove driver/runtime compatibility, distinguish 8 GB from 16 GB devices, or express single-node versus spread placement. The existing mixed-GPU test only proves inventory groups and labels.
- **Location:** `infra/ansible/tasks/assert-gpu.yml:1-31`, `schema/CONTRACT.md:91-100`, `ci/tests/test_env_shapes.py:24-69`
- **Question:** 1, 6, 11, 18, 21, 22, 23
- **Evidence:** The Ansible task only reads PCI vendor IDs and asserts `0x10de`. The schema only emits a boolean `gpu` node role. `test_env_shape_copy_bootstraps_builds_and_lists_hosts[gpu-mix]` validates Flux, inventory groups, and agent hosts; it never checks a driver, container runtime, device plugin, allocatable resource, or GPU pod.
- **Severity:** high
- **Size guess:** L
- **Suggested fix:** Add one documented GPU platform boundary before GPU consumers: driver ownership, NVIDIA device plugin/operator, allocatable-resource verification, and env-level placement labels for capacity classes. This adds a large vendor-specific lifecycle; without it, stop describing the current role as mixed-GPU support.
- **Confidence:** high

### F07 — There is no adopter upgrade/release process

- **What breaks and for whom:** A fork owner has no documented way to ingest later Jarvis releases while preserving `envs/<env>`, nor a compatibility sequence for k3s, Flux distribution/operator, and charts. Renovate updates dependencies in this source repo, not an adopter's copy of Jarvis.
- **Location:** `docs/deploy.md:112-114`, `schema/CONTRACT.md:51-52`, `renovate.json:4-10`, `platform/flux-operator/CONTRACT.md:16-22`
- **Question:** 1, 4, 6, 12, 14, 16
- **Evidence:** The whole “Dependency updates” section is: “Renovate opens update PRs right away and never merges them.” k3s Renovate updates are explicitly disabled, and no product doc contains a release, fork sync, migration, or supported upgrade-order procedure.
- **Severity:** medium
- **Size guess:** M
- **Suggested fix:** Publish versioned Jarvis releases with migration notes and an adopter intake workflow; define a tested upgrade order and rollback point for k3s, Flux Operator/distribution, CRDs, and charts. This adds release discipline and compatibility maintenance.
- **Confidence:** high

### F08 — Local verification is not portable

- **What breaks and for whom:** `make ci-verify` fails on this fresh non-systemd controller in both the full tree and an isolated env-00-only worktree, even though GitHub's systemd-shaped runner passes. Developers cannot reproduce authoritative CI.
- **Location:** `ci/tests/test_base_os.py:228-339`, `infra/ansible/roles/base-os/handlers/main.yml:18-21`, `.github/workflows/ci.yml:19-38`, `ci/ci-local-verify:10-12`
- **Question:** 4, 9, 18, 20
- **Evidence:** Required run: `OWNER_NAME="Gordon Cooper" make ci-verify` → `FAILED ... test_chrony_template_notifies_restart_only_when_changed`, `1 failed, 482 passed`. Isolated worktree with `envs/env-01` removed and a fresh venv produced the same `1 failed, 482 passed`. Failure: `Could not find the requested service chrony: host`. GitHub run `41514703742` at the same SHA is green. Also, local `ci-verify` has no env-00-only matrix leg.
- **Severity:** medium
- **Size guess:** S
- **Suggested fix:** Make the handler proof hermetic (test a dedicated role handler with a controlled service backend/container), and provide one local target that runs both CI tree shapes. Keeping a live service-module proof requires documenting and provisioning a systemd controller.
- **Confidence:** high

### F09 — The new-env path has template and multi-host gaps

- **What breaks and for whom:** An adopter must manually keep duplicated values and Flux paths aligned, is told to prep only “the first host,” and is sent to “that env's `secrets/README.md`,” which does not exist in the env-00 template they were told to copy.
- **Location:** `docs/deploy.md:31-42`, `docs/deploy.md:77`, `envs/env-00/README.md:1-7`, `docs/README.md:30-34`
- **Question:** 1, 4, 9, 10, 11, 12, 15
- **Evidence:** The paper walkthrough requires edits in seven copied files containing env/example values plus root `.sops.yaml`. `docs/deploy.md:36` says prep-node for “the first host”; `infra/ansible/README.md:5` clarifies one invocation prepares one node. `test ! -e envs/env-00/secrets/README.md` succeeds, while the docs index links only env-01's secrets README.
- **Severity:** medium
- **Size guess:** M
- **Suggested fix:** Add a validated `new-env` scaffold that accepts identity, topology, repository, age recipient, and current pins; render duplicated Flux paths/config; emit a generic secrets/operations README; and list prep-node once per host. The cost is maintaining a small generator.
- **Confidence:** high

### F10 — Future configuration and credentials are required before consumers exist

- **What breaks and for whom:** Every adopter must carry and validate Hermes/apps provider/model fields and generate Grafana/Hermes credentials even though no manifest consumes them. Two placeholder ConfigMaps are reconciled into `default`, creating runtime and documentation cruft.
- **Location:** `schema/CONTRACT.md:3-17`, `schema/env.py:209-210`, `tools/bootstrap-secrets:281-330`, `services/placeholder.yaml:1-8`, `apps/placeholder.yaml:1-8`
- **Question:** 1, 3, 5, 7, 8, 9, 10, 21, 23, 24
- **Evidence:** `values/hermes.yaml` and `values/apps.yaml` are mandatory. Search outside env/schema/tests finds no consumer. Bootstrap creates five future secrets (`grafana-admin` and four Hermes credentials), but each env's platform-configs path applies only `backups-restic` and `cert-manager-ca`. Placeholder comments contain plan/history codes and are the only objects under apps/services.
- **Severity:** medium
- **Size guess:** M
- **Suggested fix:** Remove future value files, secret specs, provider contract detail, and placeholder workloads until the first consumer lands; retain the deferred requirements in tracked work/PR history. This loses early schema design as executable documentation but restores lean onboarding.
- **Confidence:** high

### F11 — Kyverno is outside its stated tested Kubernetes range

- **What breaks and for whom:** Backup enrollment depends on Kyverno admission/background behavior that upstream did not test against the pinned Kubernetes minor. A subtle generate/mutate regression can leave PVCs unprotected.
- **Location:** `platform/kyverno/CONTRACT.md:20-26`, `envs/env-00/values/cluster-vars.yaml:16-18`
- **Question:** 1, 4, 13, 14, 18
- **Evidence:** The contract says “Upstream tests v1.19 on Kubernetes 1.33–1.35” and “The chart does not refuse 1.36.” The example/current pin is k3s `v1.36.5+k3s1`.
- **Severity:** medium
- **Size guess:** M
- **Suggested fix:** Stay within the upstream compatibility range or add a live disposable-cluster compatibility/upgrade test for PVC generation, pause, backup, and restore before accepting the skew. The latter costs real cluster CI.
- **Confidence:** high

### F12 — Mandatory PVC enrollment fails open

- **What breaks and for whom:** During a Kyverno webhook outage, PVC creation/update is admitted before backup objects exist. `generateExisting` may repair it later, but data can be written before the first protected snapshot, violating the “every PVC enrolled or opted out” product rule.
- **Location:** `platform/backups/enrollment.yaml:10-13`, `platform/backups/CONTRACT.md:51-60`
- **Question:** 1, 4, 6, 13, 17
- **Evidence:** The ClusterPolicy sets `webhookConfiguration.failurePolicy: Ignore`. The contract states this explicitly; the standing verify check detects the gap only when an operator runs it.
- **Severity:** medium
- **Size guess:** S
- **Suggested fix:** Use fail-closed admission for namespaces where backup is mandatory, with explicit availability exceptions, or add continuous alerting that makes the temporary unprotected interval visible. Fail-closed trades backup assurance for PVC availability during Kyverno outages.
- **Confidence:** high

### F13 — Multi-node still has a single control-plane failure domain

- **What breaks and for whom:** Adding worker, storage, edge, backup, or GPU nodes does not make the Kubernetes datastore/API highly available. Loss of the sole control plane requires the unsupported disaster path in F01.
- **Location:** `README.md:9-14`, `schema/CONTRACT.md:91`, `schema/env.py:79-83`
- **Question:** 1, 4, 5, 6, 11, 13, 22
- **Evidence:** Product requirements mandate “Exactly one control-plane host”; schema enforces `control_plane` as a singleton. The multi-node GPU fixture also has one control plane.
- **Severity:** medium
- **Size guess:** L
- **Suggested fix:** Make one control plane the lean profile and support an odd three-server HA profile for adopters choosing resilience. This adds etcd quorum, joins, upgrade sequencing, and recovery complexity.
- **Confidence:** high

### F14 — The product is amd64-only

- **What breaks and for whom:** An adopter cannot use common arm64 home-lab nodes even though the dependency installer already handles arm64; the schema has only one amd64 k3s digest.
- **Location:** `README.md:9-14`, `docs/deploy.md:5-18`, `schema/CONTRACT.md:51-52`, `tools/install-deps:38-44`
- **Question:** 1, 4, 5, 11, 22
- **Evidence:** README requires amd64. The k3s digest is explicitly for the amd64 release, while `linux_arch()` supports both amd64 and arm64 for all installed CLIs.
- **Severity:** medium
- **Size guess:** L
- **Suggested fix:** Either state amd64 as a deliberate first release limit or make k3s checksums per architecture and test heterogeneous inventories. Supporting arm64 multiplies image, GPU, and upgrade compatibility work.
- **Confidence:** high

### F15 — Runtime diagnosis is manual and incomplete

- **What breaks and for whom:** Failures between manual `make verify` runs have no continuous endpoint, backup-freshness, metrics, or log signal. Future services have no shared observability landing contract.
- **Location:** `platform/traefik/CONTRACT.md:10-19`, `platform/testapp/CONTRACT.md:15-23`, `services/placeholder.yaml:1-8`, `docs/deploy.md:92-106`
- **Question:** 1, 6, 12, 16, 21, 23, 24
- **Evidence:** Traefik and testapp say Gatus is planned. The services tree says observability is a later phase and deploys only a placeholder. Current docs offer operator-invoked verify/drill, not alerts or a troubleshooting path.
- **Severity:** medium
- **Size guess:** L
- **Suggested fix:** Before multiple services land, define the minimum platform health contract—continuous endpoint checks, backup freshness/failure, Flux reconciliation, node/disk pressure, and where logs are read. This adds resources and operational ownership; it need not become a large observability stack.
- **Confidence:** high

### F16 — Age-key rotation and offline recovery are incomplete

- **What breaks and for whom:** The one age recipient is a single point of loss. Docs say “back up the key file” and “keep [it] offline except for reads,” but do not give a verified offline-copy procedure or an in-place rotation that re-encrypts existing SOPS files while preserving secret values. The role refuses a differing live `sops-age` key.
- **Location:** `.sops.yaml:1-4`, `infra/ansible/README.md:22-26`, `infra/ansible/README.md:74-77`, `infra/ansible/roles/flux-operator/tasks/sops_age.yml:90-102`
- **Question:** 1, 6, 12, 15, 17
- **Evidence:** Metadata check found one age recipient on each of seven SOPS files and valid encrypted data/MAC metadata. Recovery prose says if both file and cluster Secret are lost, old values are gone; its fallback deletes encrypted files and regenerates credentials rather than rotating them.
- **Severity:** medium
- **Size guess:** M
- **Suggested fix:** Document and drill two-recipient rotation: add a new/recovery recipient, re-encrypt all env files without changing plaintext, reconcile/test, then remove the old recipient. Define offline media custody and a periodic decrypt-on-disposable-controller proof. Additional recipients enlarge the key-custody surface.
- **Confidence:** high

### F17 — Some tests protect test/source shape rather than behavior

- **What breaks and for whom:** Refactors carry unnecessary maintenance and give little additional confidence.
- **Location:** `ci/tests/test_flux_entry.py:475-508`, `ci/tests/test_base_os.py:144-190`, `ci/tests/test_flux_operator.py:487-552`, `ci/tests/test_env_shapes.py:40-54`
- **Question:** 3, 18, 19, 20
- **Evidence:** The secret-scan test invokes gitleaks again although `ci/checks/secret-scan` already runs it. The resolved-stub test mines YAML blocks and handler names. Two env-shape negative tests delete fixture fields and only prove the local `_assert_shape` helper raises. Tag tests parse synthetic wrapped/list-task text. This conflicts with `rulebooks/coding-agents.md:21-24`: no tests on names, ordering, or file shape and no pytest that only reruns a check.
- **Severity:** low
- **Size guess:** S
- **Suggested fix:** Delete the duplicate secret-scan proof, self-tests of `_assert_shape`, and source-shape/parser assertions; retain executable playbook, schema, rendered manifest, and failure-fixture behavior tests. The loss is narrow regression coverage for internal helper formatting.
- **Confidence:** high

### F18 — CONTRACT operations and limits are incomplete

- **What breaks and for whom:** Operators must discover procedures and deliberate non-goals in unrelated docs; future contributors can mistake a limitation for an omission.
- **Location:** `docs/CONTRACT.template.md:23-29`; CONTRACTs listed below
- **Question:** 1, 12, 14, 15, 16, 21, 24
- **Evidence:** Only backups and cert-manager use the optional sections.
  - `platform/flux-operator/CONTRACT.md` needs **Operations** for bootstrap/recovery and coordinated operator/distribution upgrades, now buried in `infra/ansible/README.md`; **Limits** should state the one-time bootstrap/circular-recovery boundary.
  - `platform/app-dns/CONTRACT.md` needs **Operations** for adding/removing edge nodes and resolved changes; **Limits** should state router forwarding is an adopter prerequisite and it publishes Ingress only.
  - `platform/configs/CONTRACT.md` needs **Operations** for its documented two-commit path move.
  - `platform/kyverno/CONTRACT.md` needs **Limits** for classic policies, disabled controllers, fail-open admission, and privileged movers.
  - `platform/traefik/CONTRACT.md` needs **Limits** for LAN/tailnet-only exposure, Ingress-only API, and no continuous Gatus check.
  - `platform/testapp/CONTRACT.md` needs **Limits**: canary only, no PVC, no Gatus, no service dependency.
  - `schema/CONTRACT.md` needs **Operations** linking new-env and key rotation; **Limits** for one control plane/amd64 and mandatory future files while they remain.
  - `docs/code-delivery/CONTRACT.md` needs **Limits** separating the only built GitHub/CI capability from planned code-factory capabilities; planned rows should not be presented as offers.
- **Severity:** low
- **Size guess:** S
- **Suggested fix:** Add only the listed operational pointers and explicit non-goals; do not duplicate full runbooks.
- **Confidence:** high

### F19 — `make deps` assumes apt state and controller shape

- **What breaks and for whom:** On a minimal Ubuntu controller with empty package indexes, the documented first command fails. On non-apt Linux it prints an unusable apt command.
- **Location:** `tools/install-deps:76-121`, `tools/install-deps:299-304`, `docs/deploy.md:22-29`
- **Question:** 4, 9, 10, 12, 20
- **Evidence:** First run of `make deps` failed: `Package 'python3-venv' has no installation candidate` and `Unable to locate package python3-bcrypt`. `sudo apt-get update && make deps` then succeeded. `_try_apt_install()` runs `apt-get install` without updating indexes; the script supports Linux only and its fallback always prints an apt command.
- **Severity:** low
- **Size guess:** S
- **Suggested fix:** Document Ubuntu/apt as a controller prerequisite and detect stale indexes with a clear remediation, or provide a non-mutating prerequisite check rather than doing sudo package installation. Automatic `apt-get update` is convenient but changes host package state and adds network latency.
- **Confidence:** high

## Answers by question

1. **Reliable, flexible, practical, lean?** The logical-loss backup, GitOps, SOPS, and host guards are solid. Reliability stops at disk loss (F01), future config is premature (F10), and shared backup trust is broad (F04).
2. **Glue a native approach could replace?** Most command use is justified around k3s/Flux. The main avoidable glue is manual env scaffolding (F09) and custom future placeholders/config; keep the AppArmor-aware Chrony validation.
3. **Too complex?** Required Hermes/apps schemas and credentials have no consumer (F10). Cut source-shape/duplicate tests (F17); accept losing speculative validation until those components land.
4. **Brittle?** Recovery depends on same-disk artifacts/token (F01), Kyverno is version-skewed (F11), local tests depend on systemd (F08), and exact singleton/architecture assumptions constrain topology (F13, F14).
5. **Too strict?** Exactly one control plane and amd64-only are product limits worth making profiles rather than universal rules (F13, F14). Provider/model validation before consumers is also premature (F10).
6. **Real gaps?** Disaster recovery, safe secret rotation, GPU enablement, app source wiring, continuous health, and release intake are real gaps (F01, F03–F07, F15–F16).
7. **Cruft?** Placeholder ConfigMaps, five unused bootstrap secrets, future-only required values, and some duplicate/shape tests can go (F10, F17). Generated and vendored artifacts are not cruft.
8. **Clean product?** Owner identity is correctly confined to allowed locations. Plan/history language remains in placeholder manifests and the planned-heavy code-delivery CONTRACT (F10, F18).
9. **Fresh clone to verify/drill?** The documented sequence is broadly followable on supported hardware, but the template references a missing secrets README, requires seven env-file edits plus `.sops.yaml`, and under-specifies all-host prep (F09). Actual verify/drill require a live cluster and were not run by audit limit.
10. **Clear pre-host validation?** No: prep-node's complete schema check happens after host mutation (F02). A generator/preflight should fail before SSH.
11. **Single/multi-node and mixed GPUs?** One-node and three-node fixture shapes validate and render. Multi-node remains one-control-plane; “mixed GPU” is only NVIDIA PCI presence and labels, not usable/sized GPU resources (F06, F13).
12. **Unwritten maintainer knowledge?** Multi-host prep, generic secret lifecycle, release intake, and controller/systemd assumptions are not in the adopter path (F07–F09, F16, F19).
13. **Restore from real loss?** Logical PVC loss is well handled. Control-plane disk loss is explicitly out of scope, and the drill can pass without the server token or a cluster reset (F01); restic trust/rotation add risk (F03–F04).
14. **Upgrades?** Renovate covers many pins, but there is no adopter release/migration or coordinated upgrade runbook (F07). Kyverno/k3s skew shows why one is needed (F11).
15. **Secret rotation/offline age backup?** CA rotation is good. Restic rotation is unsafe (F03); age rotation and a verified offline backup procedure are absent (F16).
16. **Can operators diagnose failures?** Tools generally fail with useful local messages and CONTRACT Health sections are helpful. There is no continuous health/logging layer or consolidated troubleshooting entry point (F15, F18).
17. **Leaks or excess trust?** Metadata-only checks found all SOPS payload fields encrypted and gitleaks passed. Backup credentials and mutable repositories have an unnecessarily wide blast radius (F04); Kyverno enrollment fails open (F12).
18. **Reasonable test gaps?** Add a real disposable disaster proof, GPU allocatable/pod proof when GPU support lands, and a portable two-shape local CI target (F01, F06, F08). A live Kyverno/k3s compatibility proof would address F11.
19. **Tests to cut?** Remove the duplicate gitleaks pytest, helper self-tests, and YAML/tag-parser shape assertions in F17. Keep restore/drill state-machine tests and fixture-based rule checks.
20. **Behavior and fresh env-00 pass?** Many high-value tests execute fake binaries/state machines and rendered manifests, but several inspect source shape (F17). Fresh full and isolated env-00-only runs each produced `482 passed, 1 failed`; GitHub CI is green because runner shape differs (F08).
21. **Clean place for new services/apps?** The Flux stages and platform contracts are a good skeleton, but there is no consumer-facing component template covering certs, DNS, backups, secrets, health, and the configured apps repository is ignored (F05, F18).
22. **Blocks/awkward constraints?** Missing GPU runtime, one control plane, shared privileged backup machinery, ignored app source, and no code-factory trust boundary are constraints (F04–F06, F13). The current CLI guard is build tooling, not that boundary.
23. **What every future area needs?** A health/observability contract, secret lifecycle, release/update contract, app/service onboarding contract, and—where relevant—GPU resource contract (F06–F07, F15–F18).
24. **Deferred work not to lose?** Preserve as tracked work: Gatus per exposed endpoint; observability; Hermes profile schema; external apps/OCI wiring; `admin-gate`; registry/build workflow; second code-delivery provider; and model-name core checks. F10 recommends removing premature runtime/schema stubs, not losing these commitments.

## What's good and should stay

- Fail-closed Ansible preflight for foreign k3s, disks, architecture, env markers, and immutable ownership.
- One Git/Flux source of truth, strict substitutions, clear stage ordering, and digest-pinned images.
- SOPS encrypted payloads, no plaintext secret finding, guarded age key permissions, and refusal to overwrite a differing live key.
- PVC logical-loss restore: writer quiescence, pause verification, rebuild hold, explicit empty-restore failure, and rollback instructions.
- Behavior fixtures for restore/drill failure paths, schema rejection, command guard categories, manifest resources, and generated ConfigMap drift.
- Clear component Offers/Expects/Health/Config sections and env-specific identity isolation.
- Deliberate router DNS forwarding and Tailscale prerequisites should remain outside cluster automation.

## Delete list

| Candidate | Evidence it is unused or duplicated | What is lost |
| --- | --- | --- |
| `apps/placeholder.yaml`, `services/placeholder.yaml` | They are the only objects in their trees; searches find no consumer by object name. | Two inert “not deployed” ConfigMaps. |
| Future Grafana/Hermes specs in `tools/bootstrap-secrets:298-330` until consumers land | Env Flux applies only backup and CA secrets; searches find no core consumer. | Early credential names and bootstrap fixtures. |
| Mandatory `values/hermes.yaml` / `values/apps.yaml` validation until consumers land | Outside schema/env/tests, no file reads these paths. | Early schema design; retain it in tracked work. |
| Planned implementation detail in `docs/code-delivery/CONTRACT.md` | Five of six capabilities are marked planned and no implementation consumes `code_delivery`. | A design note; preserve deferred requirements in an issue/PR. |
| `ci/tests/test_flux_entry.py:475-508` | Re-runs gitleaks already run by `ci/checks/secret-scan`. | One planted-key integration proof. |
| `ci/tests/test_env_shapes.py:40-54` and shape-only helper assertions | They mutate fixtures solely to prove local `_assert_shape` raises; schema tests already enforce role shape. | Self-test coverage for the test helper. |
| YAML/handler/tag parser assertions in `ci/tests/test_base_os.py:144-190` and `ci/tests/test_flux_operator.py:487-552` | They inspect names/shape or synthetic parser strings rather than execute behavior. | Narrow formatting regression coverage. |

Do **not** delete `platform/backups/controller/volumesnapshot-crd.yaml`; it is vendored because VolSync Direct mode needs the type even without a snapshot controller. Do **not** hand-edit generated cluster-vars ConfigMaps.

## Coverage table

Tags mean the file was inspected directly or through its area/source. `excluded` files were checked only through the named source, as required.

| File | Tag |
| --- | --- |
| `.claude/settings.json` | no finding |
| `.cursor/cli.json` | no finding |
| `.github/pull_request_template.md` | no finding |
| `.github/workflows/ci.yml` | finding |
| `.gitignore` | no finding |
| `.mailmap` | no finding |
| `.sops.yaml` | no finding — recipient/path metadata checked |
| `AGENTS.md` | no finding |
| `CLAUDE.md` | no finding |
| `Makefile` | finding |
| `README.md` | finding |
| `apps/kustomization.yaml` | finding |
| `apps/placeholder.yaml` | finding |
| `ci/checks/cli-deny` | no finding |
| `ci/checks/contract-md` | no finding |
| `ci/checks/core-names` | no finding |
| `ci/checks/digest-pin` | no finding |
| `ci/checks/env-schema` | no finding |
| `ci/checks/fixtures/must-allow-main.txt` | no finding |
| `ci/checks/fixtures/must-block-main.tsv` | no finding |
| `ci/checks/flux-substitute` | no finding |
| `ci/checks/lib/_cli_permissions.py` | no finding |
| `ci/checks/lib/_contract_md.py` | no finding |
| `ci/checks/lib/_core_names.py` | no finding |
| `ci/checks/lib/_digest_pin.py` | no finding |
| `ci/checks/lib/_flux_substitute.py` | no finding |
| `ci/checks/lib/_loadbalancer.py` | no finding |
| `ci/checks/lib/_manifest_lint.py` | no finding |
| `ci/checks/lib/_manifests.py` | no finding |
| `ci/checks/lib/_plan_codes.py` | no finding |
| `ci/checks/lib/_product_ci.py` | no finding |
| `ci/checks/lib/_pvc_backup.py` | no finding |
| `ci/checks/lib/_python_pins.py` | no finding |
| `ci/checks/loadbalancer` | no finding |
| `ci/checks/manifest-lint` | no finding |
| `ci/checks/plan-codes` | no finding |
| `ci/checks/product-ci` | no finding |
| `ci/checks/pvc-backup` | no finding |
| `ci/checks/python-pins` | no finding |
| `ci/checks/secret-scan` | no finding |
| `ci/ci-local-verify` | finding |
| `ci/rule-checks.txt` | no finding |
| `ci/run-checks` | no finding |
| `ci/tests/ansible_role_proof.py` | no finding |
| `ci/tests/conftest.py` | no finding |
| `ci/tests/fixtures/ansible-list-tasks/hosts.yml` | no finding |
| `ci/tests/fixtures/digest-pin/tag-without-digest.yaml` | no finding |
| `ci/tests/fixtures/env-shapes/gpu-mix/hosts.yml` | no finding |
| `ci/tests/fixtures/env-shapes/secondary-disk/hosts.yml` | no finding |
| `ci/tests/fixtures/env-shapes/single-node/hosts.yml` | no finding |
| `ci/tests/fixtures/gpu-assert/hosts.yml` | no finding |
| `ci/tests/fixtures/gpu-assert/play.yml` | no finding |
| `ci/tests/fixtures/loadbalancer/extra-service.yaml` | no finding |
| `ci/tests/fixtures/manifest-lint/missing-memory-limit.yaml` | no finding |
| `ci/tests/fixtures/schemas/apiextensions.k8s.io/customresourcedefinition_v1.json` | excluded: upstream schema fixture; consuming tests checked |
| `ci/tests/fixtures/schemas/helm.cattle.io/HelmChartConfig_v1.json` | excluded: upstream schema fixture; consuming tests checked |
| `ci/tests/fixtures/schemas/volsync.backube/replicationdestination_v1alpha1.json` | excluded: upstream schema fixture; consuming tests checked |
| `ci/tests/fixtures/schemas/volsync.backube/replicationsource_v1alpha1.json` | excluded: upstream schema fixture; consuming tests checked |
| `ci/tests/support.py` | no finding |
| `ci/tests/test_ansible_operator_tags.py` | no finding |
| `ci/tests/test_app_dns.py` | no finding |
| `ci/tests/test_backup_enrollment.py` | no finding |
| `ci/tests/test_base_os.py` | finding |
| `ci/tests/test_bootstrap_secrets.py` | no finding |
| `ci/tests/test_cert_manager.py` | no finding |
| `ci/tests/test_cli_permissions.py` | no finding |
| `ci/tests/test_contract_md.py` | no finding |
| `ci/tests/test_core_names.py` | no finding |
| `ci/tests/test_digest_pin.py` | no finding |
| `ci/tests/test_drill.py` | no finding |
| `ci/tests/test_env_schema.py` | no finding |
| `ci/tests/test_env_shapes.py` | finding |
| `ci/tests/test_flux_entry.py` | finding |
| `ci/tests/test_flux_operator.py` | finding |
| `ci/tests/test_flux_operator_install.py` | no finding |
| `ci/tests/test_flux_substitute.py` | no finding |
| `ci/tests/test_install_deps.py` | no finding |
| `ci/tests/test_inventory_roles.py` | no finding |
| `ci/tests/test_k3s.py` | no finding |
| `ci/tests/test_kubeconfig.py` | no finding |
| `ci/tests/test_loadbalancer.py` | no finding |
| `ci/tests/test_manifest_lint.py` | no finding |
| `ci/tests/test_node_disk.py` | no finding |
| `ci/tests/test_pci_gpu.py` | no finding |
| `ci/tests/test_plan_codes.py` | no finding |
| `ci/tests/test_preflight.py` | no finding |
| `ci/tests/test_prep_node.py` | no finding |
| `ci/tests/test_product_ci.py` | no finding |
| `ci/tests/test_prune_guards.py` | no finding |
| `ci/tests/test_pvc_backup.py` | no finding |
| `ci/tests/test_python_pins.py` | no finding |
| `ci/tests/test_renovate.py` | no finding |
| `ci/tests/test_restic_rest_server.py` | no finding |
| `ci/tests/test_restore.py` | no finding |
| `ci/tests/test_seal.py` | no finding |
| `ci/tests/test_testapp.py` | no finding |
| `ci/tests/test_traefik.py` | no finding |
| `ci/tests/test_verify.py` | no finding |
| `docs/CONTRACT.template.md` | no finding |
| `docs/README.md` | finding |
| `docs/code-delivery/CONTRACT.md` | finding |
| `docs/deploy.md` | finding |
| `envs/env-00/README.md` | finding |
| `envs/env-00/flux/flux-instance.yaml` | no finding |
| `envs/env-00/flux/generated/cluster-vars-configmap.yaml` | excluded: generated; renderer and source values checked |
| `envs/env-00/flux/kustomization.yaml` | no finding |
| `envs/env-00/flux/kustomizations/apps.yaml` | finding |
| `envs/env-00/flux/kustomizations/platform-configs.yaml` | no finding |
| `envs/env-00/flux/kustomizations/platform-controllers.yaml` | no finding |
| `envs/env-00/flux/kustomizations/services.yaml` | no finding |
| `envs/env-00/flux/platform-configs/kustomization.yaml` | finding |
| `envs/env-00/inventory/hosts.yml` | no finding |
| `envs/env-00/values/apps.yaml` | finding |
| `envs/env-00/values/cluster-vars.yaml` | finding |
| `envs/env-00/values/hermes.yaml` | finding |
| `envs/env-01/README.md` | finding |
| `envs/env-01/ca.crt` | no finding |
| `envs/env-01/flux/flux-instance.yaml` | no finding |
| `envs/env-01/flux/generated/cluster-vars-configmap.yaml` | excluded: generated; renderer and source values checked |
| `envs/env-01/flux/kustomization.yaml` | no finding |
| `envs/env-01/flux/kustomizations/apps.yaml` | finding |
| `envs/env-01/flux/kustomizations/platform-configs.yaml` | no finding |
| `envs/env-01/flux/kustomizations/platform-controllers.yaml` | no finding |
| `envs/env-01/flux/kustomizations/services.yaml` | no finding |
| `envs/env-01/flux/platform-configs/kustomization.yaml` | finding |
| `envs/env-01/inventory/hosts.yml` | no finding |
| `envs/env-01/secrets/README.md` | finding |
| `envs/env-01/secrets/backups-restic.sops.yaml` | no finding — metadata only; encrypted fields, one recipient, and MAC checked |
| `envs/env-01/secrets/cert-manager-ca.sops.yaml` | no finding — metadata only; encrypted fields, one recipient, and MAC checked |
| `envs/env-01/secrets/grafana-admin.sops.yaml` | no finding — metadata only; encrypted fields, one recipient, and MAC checked |
| `envs/env-01/secrets/hermes-code-factory-api.sops.yaml` | no finding — metadata only; encrypted fields, one recipient, and MAC checked |
| `envs/env-01/secrets/hermes-code-factory-dashboard.sops.yaml` | no finding — metadata only; encrypted fields, one recipient, and MAC checked |
| `envs/env-01/secrets/hermes-ops-api.sops.yaml` | no finding — metadata only; encrypted fields, one recipient, and MAC checked |
| `envs/env-01/secrets/hermes-ops-dashboard.sops.yaml` | no finding — metadata only; encrypted fields, one recipient, and MAC checked |
| `envs/env-01/values/apps.yaml` | finding |
| `envs/env-01/values/cluster-vars.yaml` | finding |
| `envs/env-01/values/hermes.yaml` | finding |
| `infra/ansible/README.md` | finding |
| `infra/ansible/ansible.cfg` | no finding |
| `infra/ansible/group_vars/all.yml` | no finding |
| `infra/ansible/roles/base-os/defaults/main.yml` | no finding |
| `infra/ansible/roles/base-os/handlers/main.yml` | no finding |
| `infra/ansible/roles/base-os/tasks/chrony.yml` | no finding |
| `infra/ansible/roles/base-os/tasks/main.yml` | no finding |
| `infra/ansible/roles/base-os/templates/20auto-upgrades.j2` | no finding |
| `infra/ansible/roles/base-os/templates/50unattended-upgrades.j2` | no finding |
| `infra/ansible/roles/base-os/templates/chrony.conf.j2` | no finding |
| `infra/ansible/roles/base-os/templates/journald-jarvis.conf.j2` | no finding |
| `infra/ansible/roles/base-os/templates/resolved-stub.conf.j2` | no finding |
| `infra/ansible/roles/flux-operator/defaults/main.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/chart_pin.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/helm_cli.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/instance.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/main.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/operator.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/rebuild_hold.yml` | no finding |
| `infra/ansible/roles/flux-operator/tasks/sops_age.yml` | finding |
| `infra/ansible/roles/flux-operator/tasks/sops_age_key_mode.yml` | no finding |
| `infra/ansible/roles/flux-operator/templates/backup-rebuild-hold.yaml.j2` | no finding |
| `infra/ansible/roles/flux-operator/templates/helm-upgrade.cmd.j2` | no finding |
| `infra/ansible/roles/flux-operator/templates/sops-age-secret.yaml.j2` | no finding |
| `infra/ansible/roles/k3s/defaults/main.yml` | no finding |
| `infra/ansible/roles/k3s/handlers/main.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/data-dirs.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/guard.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/labels.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/main.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/refuse.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/service-unit.yml` | no finding |
| `infra/ansible/roles/k3s/tasks/token.yml` | no finding |
| `infra/ansible/roles/k3s/templates/agent-config.yaml.j2` | no finding |
| `infra/ansible/roles/k3s/templates/config.yaml.j2` | no finding |
| `infra/ansible/roles/k3s/templates/jarvis-mount.conf.j2` | no finding |
| `infra/ansible/roles/k3s/templates/k3s.service.j2` | no finding |
| `infra/ansible/roles/k3s/templates/label-patches.json.j2` | no finding |
| `infra/ansible/roles/kubeconfig/tasks/main.yml` | no finding |
| `infra/ansible/roles/node-disk/tasks/main.yml` | no finding |
| `infra/ansible/roles/node-disk/vars/main.yml` | no finding |
| `infra/ansible/site.yml` | finding |
| `infra/ansible/tasks/assert-gpu.yml` | finding |
| `infra/ansible/tasks/preflight.yml` | no finding |
| `infra/inventory/roles.yml/constructed.yml` | no finding |
| `infra/inventory/roles.yml/groups.yml` | no finding |
| `platform/app-dns/CONTRACT.md` | finding |
| `platform/app-dns/coredns-custom.yaml` | no finding |
| `platform/app-dns/helmrelease.yaml` | no finding |
| `platform/app-dns/helmrepository.yaml` | no finding |
| `platform/app-dns/kustomization.yaml` | no finding |
| `platform/app-dns/namespace.yaml` | no finding |
| `platform/backups/CONTRACT.md` | finding |
| `platform/backups/controller/helmrelease.yaml` | no finding |
| `platform/backups/controller/helmrepository.yaml` | no finding |
| `platform/backups/controller/kustomization.yaml` | no finding |
| `platform/backups/controller/namespace.yaml` | no finding |
| `platform/backups/controller/volumesnapshot-crd.yaml` | excluded: vendored upstream CRD; chart/source pin checked |
| `platform/backups/enrollment.yaml` | finding |
| `platform/backups/etcd-snapshots.yaml` | no finding |
| `platform/backups/kustomization.yaml` | no finding |
| `platform/backups/namespace.yaml` | no finding |
| `platform/backups/rest-server.yaml` | finding |
| `platform/cert-manager/CONTRACT.md` | no finding |
| `platform/cert-manager/config/internal-ca.yaml` | no finding |
| `platform/cert-manager/config/kustomization.yaml` | no finding |
| `platform/cert-manager/controller/helmrelease.yaml` | no finding |
| `platform/cert-manager/controller/helmrepository.yaml` | no finding |
| `platform/cert-manager/controller/kustomization.yaml` | no finding |
| `platform/cert-manager/controller/namespace.yaml` | no finding |
| `platform/configs/CONTRACT.md` | finding |
| `platform/configs/kustomization.yaml` | no finding |
| `platform/flux-operator/CONTRACT.md` | finding |
| `platform/flux-operator/helmrelease.yaml` | no finding |
| `platform/flux-operator/kustomization.yaml` | no finding |
| `platform/flux-operator/oci-repository.yaml` | no finding |
| `platform/kustomization.yaml` | no finding |
| `platform/kyverno/CONTRACT.md` | finding |
| `platform/kyverno/helmrelease.yaml` | no finding |
| `platform/kyverno/helmrepository.yaml` | no finding |
| `platform/kyverno/kustomization.yaml` | no finding |
| `platform/kyverno/namespace.yaml` | no finding |
| `platform/kyverno/rbac.yaml` | no finding |
| `platform/testapp/CONTRACT.md` | finding |
| `platform/testapp/kustomization.yaml` | no finding |
| `platform/testapp/namespace.yaml` | no finding |
| `platform/testapp/testapp.yaml` | no finding |
| `platform/traefik/CONTRACT.md` | finding |
| `platform/traefik/helmchartconfig.yaml` | no finding |
| `platform/traefik/kustomization.yaml` | no finding |
| `pytest.ini` | no finding |
| `renovate.json` | finding |
| `requirements-dev.txt` | no finding |
| `rulebooks/coding-agents.md` | no finding |
| `schema/CONTRACT.md` | finding |
| `schema/__init__.py` | no finding |
| `schema/env.py` | finding |
| `services/kustomization.yaml` | finding |
| `services/placeholder.yaml` | finding |
| `tools/bootstrap-secrets` | finding |
| `tools/drill` | finding |
| `tools/install-deps` | finding |
| `tools/lib/_env.py` | no finding |
| `tools/lib/_etcd_image.py` | no finding |
| `tools/lib/_sops.py` | no finding |
| `tools/lib/render_cluster_vars_configmap.py` | no finding |
| `tools/prep-node` | finding |
| `tools/restore` | no finding |
| `tools/seal` | no finding |
| `tools/verify/run` | no finding |
| `tools/versions.env` | no finding |

Coverage count: 248 tracked files, 248 tagged.

## Concerns and questions

1. The brief requires branch `audit/wrapup-gpt-5-6-sol`, but this run enforces `cursor/*-0d5d`; the audit uses `cursor/audit-wrapup-gpt-5-6-sol-0d5d`. Base SHA, title, draft state, and findings-only diff are unchanged.
2. No live host, cluster, SSH, kubectl, SOPS decrypt, or age private key was used. Questions 9, 11, and 13 were evaluated by docs/code, fixture builds, unit behavior, and an env-00-only scratch worktree; actual `make verify`/`make drill` require hardware and were prohibited.
3. The brief says CI passes at this SHA, and GitHub run `41514703742` confirms that. The required local run does not pass on this non-systemd controller (F08); this is runner dependence, not an Ansible 14 regression.
4. “Restore from real loss” conflicts with the repository's explicit same-node/logical-loss limit. This audit treats that as a product gap because the requested product direction and the drill name imply stronger recovery (F01).
5. The product has not chosen whether apps arrive from a second Flux Git source, OCI artifacts, or this monorepo. F05 proposes choosing one boundary, not a particular implementation.
6. Router forwarding of `dns_suffix` and Tailscale remain adopter prerequisites by design and are not findings.
7. `.cursor/cli.json` was checked only for a real safety defect; none was found. No bypass detail is included.
