# Wrap-up audit: judge report (v2, final foundation report)

Commit audited: `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1` (tag `v0.1.0`). Phase plan: `gordoncooper/project-plans` main `771158d` (`jarvis-alphacut/{framework,plan}.md`), read in full where cited. Judge: Claude Opus 5.5. v1 of this report is in this branch's git history (commit `314cc87`).

**Inputs, v1:** three auditors (A = Grok 4.7, B = GPT-5.6 Sol, C = Claude Opus 5.5), their cross-checks, and the baseline logs.

**Inputs, v2:**
- `rejections-review.md` (R): a recheck of v1's rejections and downgrades;
- `forward-readiness.md` (F): a review against the phase plan;
- `live-checks.md`: Operator's read-only results for J12 and J43.

I checked every R and F claim used below against the code or the plan myself, at `file:line`. Where I disagree, section 10b says so.

**What I ran (v1, still valid):**
- `make deps`: failed on empty apt lists, then passed after `apt-get update` (J32).
- `OWNER_NAME="Gordon Cooper" make ci-verify`: all 13 checks pass. Pytest: 482 passed, 1 failed (the chrony handler, J31).
- The same suite with `envs/env-01` removed, in a scratch worktree: identical result.
- The chrony fix proven in scratch (switching to `ansible.builtin.systemd` makes the test pass).
- A schema harness on scratch copies.
- Probes of the URL validator and the command-guard matcher (reported by category only).
- `kustomize build` of an empty kustomization: exit 0.

**v2 additions:**
- greps and reads for every R and F claim;
- the upstream VolSync v0.17.0 ReplicationSource CRD (`raw.githubusercontent.com/backube/volsync/v0.17.0/config/crd/bases/…`), which has `moverTolerations`, `moverSecurityContext`, `moverAffinity` and `moverServiceAccount`;
- the Flux Operator FluxInstance docs;
- the primary sources for the two external claims (section 8c).

All scratch copies are removed. Nothing but this file is committed.

---

## 0. Changes since v1

| Change | Count | Ids |
| --- | --- | --- |
| Findings added | 15 | J50–J64 (the two misses R found, F's seven fix-now items, and R's reversals that had no id) |
| Re-ranked up | 5 | J08 medium→high; J10 medium→high; J11 medium→high; J47 low→medium; J22 rescoped (the per-host part moves to J60) |
| v1 verdicts reversed | 7 | C F25 and C10 (now J54); C T20 (J56); C4 and C5 (J55); the dropped C F21 agent restart (J57); the dropped namespace baseline (J51) |
| v1 verdicts partly reversed and folded in | 14 | C F14 (J52), C F22 (J53), C F32 and B F19 (J32, J05), C F30 (J55), J47, J08 timing, J10 timing, J46 trigger, J16 scope (delete, not optional), D2 (J58), D4 (J59), D5 (section 4), J09 scope |
| Corrections to v1's own text | 3 | J09: the "add a verify check" fix already exists at `tools/verify/run:331-345`, so it is removed. J16: `code_delivery`, `build_agent` and `llm_provider_host` were already optional (`schema/CONTRACT.md:13`); only the `hermes.yaml` and `apps.yaml` files are required. G1 no longer touches `schema/env.py` |
| Open questions closed | 2 | J12: `local-path` is `WaitForFirstConsumer` (live). J43: benign drill noise (live), now closed |
| Decisions | 14 | D1–D13 revised; D14 is new |

---

## 1. Verdict

**Shape.** The foundation is small, coherent and well guarded. The node → cluster → Flux → platform chain is clear, and CI is strong: 13 rule checks, about 480 tests, kustomize builds, Ansible localhost runs and a prune guard. Day-to-day logical restore is the best-built part. The weak parts are about **losing things**:
- the disk (J01);
- the k3s token (J02);
- the restic password and the age key (J03, J04).

The first hour of a new adopter is also weak (J05, J06, J07). Looking forward, the weak part is **trust between tenants**, which the next three phases need and which costs most to retrofit:
- one restic credential in every namespace (J08);
- Flux applying everything as cluster-admin (J50);
- no namespace security baseline (J51);
- no secrets path for services (J11).

**Top three risks.**
1. **A dead disk loses everything** (J01, critical). No off-host copy exists, and data-disk hosts can't even add a separate backup disk.
2. **A rebuilt control plane can't use the etcd copy, and no drill runs the real rebuild path** (J02, high).
3. **The first untrusted workload (Hermes ops reads email, plan p2-09) lands in a cluster where any namespace can prune every backup and Flux applies anything as cluster-admin** (J08 and J50, high). Each gets more expensive with every PVC and HelmRelease that P1–P4 add.

**How close to "others can deploy it".** One focused round. Waves 1–2 of section 7b, about 10 PRs, mostly S–M, give a safe single-node adopter path. Wave 3 (off-host backup and the trust boundary) has to land before P2 deploys Hermes. Multi-node needs G2b (placement and rolling agents) as well. Section 7c is the checklist.

**Fit with "Where this is going".** It fits well; no redesign is needed. These are the forward costs:
- the tenancy items above;
- a schema that bakes in a cursor-only build agent and pre-consumer Hermes validators (J16);
- a node-label scheme tied to the DNS suffix, with no taint for dedicated GPU or backup nodes (J52);
- per-env Flux wiring that copies core layout into every instance (J58, D14).

Three plan choices differ from Gordon's stated vision (section 8b), and Gordon should confirm them, not the next audit.

---

## 2. Ranked findings

Ids are stable from v1; J50–J64 are new. Who found each: A, B, C (auditors), R (rejections review), F (forward-readiness review), or judge. "Render" means a kustomize render change; "Ansible" means an Ansible run change. Each fix lists the PR that carries it (section 7b).

### Critical

#### J01 — A dead disk destroys data and every backup; no off-host copy exists, and data-disk hosts can't add a separate backup disk
- **Severity / size / PR:** critical / L / G5a.
- **What breaks:**
  - PVC data, restic repos and the etcd snapshot copies share one disk (`platform/backups/CONTRACT.md:187-188`).
  - The single rest-server runs on a hostPath (`platform/backups/rest-server.yaml:27,65`).
  - With `data_disk: true`, `infra/ansible/roles/node-disk/tasks/main.yml:76` and `:128` refuse a `backup_path` outside the data mount, so the contract's own escape ("unless `backup_path` is on another disk") is impossible.
- **Questions:** 6, 13, 23, 24.
- **Found by:** A F1, B F01, C F03. The node-disk contradiction is judge-only. All cross-checks agree.
- **Forward cost:** the first data worth keeping (Hermes memory, the Google refresh token) arrives in P2 (F, section C).
- **Fix:**
  - A `restic copy` CronJob in `platform/backups/` from each local repo (including the etcd repo) to an adopter-set repo `offsite_restic_repository`. Per F, allow **any restic backend** (`rest:`, `s3:`, `sftp:`, `rclone:`) so an existing cloud-drive subscription works.
  - New SOPS spec `backups-offsite` (password plus backend env).
  - verify requires a fresh off-host snapshot when the key is set.
  - node-disk accepts a declared `backup_disk_mount` as well as the data mount.
- **Files:** `platform/backups/`, `schema/env.py`, `tools/bootstrap-secrets`, `tools/verify/` (via G7's registry), `infra/ansible/roles/node-disk/`, `platform/backups/CONTRACT.md`.
- **Acceptance:**
  - schema accepts an absent key and rejects a malformed one;
  - render shows the CronJob only when set;
  - Ansible localhost: a declared backup mount passes, and an undeclared other mount fails;
  - a verify unit test for off-host freshness;
  - live: one off-host copy completes on bench, and `restic snapshots` on the target lists it.
- **We lose:** the "one rest-server" simplicity. **Changes:** render yes, Ansible yes. **Decision:** D1.

### High: real bugs today

#### J02 — A rebuilt control plane can't use the etcd copy (unsealed k3s token), and the real rebuild path is never drilled
- **Severity / size / PR:** high / M / G2a.
- **What breaks:**
  - `infra/ansible/roles/k3s/tasks/token.yml:21-35` only reads `/var/lib/rancher/k3s/server/node-token` to hand it to agents.
  - `config.yaml.j2` has `secrets-encryption: true` and no `token:`.
  - `group_vars/all.yml:6` puts the data dir on the root disk.
  - `platform/backups/CONTRACT.md:170-192`: the reset is same-node and needs the original token.
  - The real rebuild path (`site.yml`, then hold, then `restore-all`, `CONTRACT.md:75-147`) is never exercised. `make drill` passes and reads as "DR proven".
- **Questions:** 4, 13, 15, 24.
- **Found by:** A F1, B F01, C F04.
- **Fix:**
  - `bootstrap-secrets` mints a server token and an agent token (SOPS spec `k3s-tokens`).
  - The k3s role templates `token:` and `agent-token:`, and agents join with the agent token (J19).
  - Write a "Rebuild a dead node" runbook under `platform/backups/CONTRACT.md` Operations.
  - Label the etcd copy "same-node rollback only" until an owner-run path exists.
- **Files:** `tools/bootstrap-secrets`, `infra/ansible/roles/k3s/{tasks/token.yml,templates/config.yaml.j2,templates/agent-config.yaml.j2}`, `platform/backups/CONTRACT.md`.
- **Acceptance:**
  - template tests assert `token:` comes from the decrypted value under `no_log`, and agents get only `agent-token`;
  - live: env-01's existing token is sealed by the owner before the PR's `site.yml` (an existing cluster refuses a different token);
  - one rebuild rehearsal on bench (a scratch VM or `k3s-uninstall` on a spare node), with restore-all green.
- **Changes:** Ansible yes. **Decision:** D2.

#### J03 — The documented restic rotation orphans every existing repository
- **Severity / size / PR:** high / S / G1.
- **What breaks:** `envs/env-01/secrets/README.md:25` says to delete the file and rerun. `tools/bootstrap-secrets:261-268` then mints a new `RESTIC_PASSWORD`, and every repo is still keyed to the old one.
- **Questions:** 15, 13.
- **Found by:** A F2, B F03, C F02 (C rated it critical; high holds, since the old ciphertext stays in git history).
- **Fix:** replace the sentence. `bootstrap-secrets` refuses to regenerate `backups-restic` while the file is tracked in git, unless `--rotate-restic` is passed. Write the real rotation (`restic key add`, then switch the Secret, then `restic key remove`, per repo, via a Job) in Backups Operations. The migration is needed for J08 anyway.
- **Acceptance:** a pytest case: deleting the file and rerunning refuses without the flag. **Changes:** none.

#### J04 — The age key has one copy with no tested offline backup, and the documented recovery discards live secrets
- **Severity / size / PR:** high / S–M / G1.
- **What breaks:**
  - `.sops.yaml` has one env-01 rule with one recipient.
  - `infra/ansible/README.md:76`: if both copies are lost, "accept the prep-node swap prompt… run `make bootstrap-secrets`".
  - **Judge-only:** with only the controller lost, the cluster still holds the decrypted `backups-restic` and `cert-manager-ca`, which can be resealed to a new key. The doc's path regenerates them, stranding every repo (J03) and replacing the trusted CA.
  - The framework already requires two tested copies before real data (`framework.md:509`). The repo has no procedure for it.
- **Questions:** 15, 12, 24.
- **Found by:** B F16, C F01 (critical→high).
- **Fix:**
  - An "Age key custody" section in a new `envs/env-00/secrets/README.md`: two copies, plus a test decrypt of one secret.
  - An ordered recovery: if the cluster is alive, reseal the live Secrets; regenerate only if the cluster is gone too.
  - Make prep-node's mismatch prompt (J33) refuse and point to this runbook, instead of offering deletion.
- **Acceptance:** a doc-link check, and a prep-node test that a mismatch makes no deletion. **Changes:** none. **Decision:** D3.

#### J05 — prep-node changes the host before validating the env, and the documented order always fails validation
- **Severity / size / PR:** high / S–M / G3a.
- **What breaks:**
  - `docs/deploy.md:36-37` runs prep-node before adding the `.sops.yaml` rule.
  - `tools/prep-node` `run()` (`:950-992`) changes the host and only then calls `run_env_schema`.
  - From R (B F19): prep-node preflights `ssh`, `ssh-keygen`, `git` and `make` (`:36`), but not `age-keygen`, which it runs later (`:761-771`). It never runs `make`.
- **Questions:** 9, 10.
- **Found by:** A F3, B F02; R for the preflight part. C missed it.
- **Evidence:** the harness case `prep_node_doc_order_no_rule_yet` fails with "expected exactly one creation rule… found 0".
- **Fix:**
  - Run the schema first, in a pre-host mode that skips only recipient and age-key checks.
  - prep-node adds the missing creation rule itself.
  - Preflight `age-keygen` and drop `make`.
  - Rewrite `docs/deploy.md` steps 2–3.
- **Acceptance:** a stubbed-SSH test where an env with a schema error makes zero SSH calls, and the doc-order harness passes. **Changes:** none.

#### J06 — Duplicated env facts are unchecked; a wrong FluxInstance can only be fixed by a manual kubectl apply
- **Severity / size / PR:** high / S / G3a.
- **What breaks:**
  - `envs/env-00/flux/flux-instance.yaml:24-27` carries its own URL, ref and path.
  - `infra/ansible/roles/flux-operator/tasks/instance.yml:21-28` applies it only when it is absent.
  - Recovery is `infra/ansible/README.md:74` (a manual apply).
  - The generated `cluster-vars-configmap.yaml` can go stale.
- **Questions:** 4, 9, 10.
- **Found by:** A F5 and A F9, C F11, B E04.
- **Evidence:** the harness cases `flux_instance_url_ref_left_as_example` and `stale_configmap_dns_suffix_changed` both pass validation.
- **Fix:** `schema/env.py` compares the FluxInstance sync with `flux_git_*` and the env path, and compares the committed ConfigMap with an in-memory render.
- **Acceptance:** both harness cases fail with a clear message. **Changes:** none.

#### J07 — Repo URL validation accepts embedded credentials, token queries and fragments
- **Severity / size / PR:** high / S / G3a.
- **What breaks:** `schema/env.py:512,542` checks only the scheme and the host, so a pasted token is committed and published in the ConfigMap.
- **Questions:** 10, 17.
- **Found by:** B F20.
- **Evidence:** the validator returned `errors=[]` for userinfo, query and fragment.
- **Fix:** reject userinfo, query and fragment. **Acceptance:** parameterized pytest. **Changes:** none.

### High: cheap now, expensive to retrofit

#### J08 — Backup trust boundary: one restic credential in every namespace, append-only off, cluster-wide Secret RBAC, privileged movers everywhere
- **Severity / size / PR:** **high** (v1: medium) / L / G5b.
- **What breaks:**
  - `platform/backups/enrollment.yaml:50` copies the global `RESTIC_PASSWORD` into each PVC namespace.
  - `rest-server.yaml:2-3` runs without `--append-only` or `--private-repos`.
  - `platform/kyverno/rbac.yaml:12-15` grants cluster-wide Secret get, list, watch, create, update, patch and delete.
  - `enrollment.yaml:96-108` gives every PVC namespace privileged movers.
- **Why high now (R, F; verified):**
  - The first real PVC is `hermes-ops` (plan p2-09), whose bot reads untrusted email (`framework.md:238`, R13). Code running there can forget and prune every repo.
  - The policy has `kustomize.toolkit.fluxcd.io/force: Enabled` (`enrollment.yaml:8`), and recreating it regenerates every Secret and ReplicationSource (`CONTRACT.md:55,102`). One recreation now, over about two repos, is cheap; four over P2–P4 are not.
- **Questions:** 17, 22, 23.
- **Found by:** A F16, B F04 and B F22, C F16; timing from R and F.
- **Fix (one policy change, final shape):**
  - Per-namespace repo passwords, generated per namespace by Kyverno from a per-namespace random Secret.
  - rest-server `--private-repos` with one htpasswd user per namespace, written by a Kyverno-generated Job or by `bootstrap-secrets` for platform namespaces.
  - Kyverno RBAC narrowed to the generated names wherever Kyverno allows it.
  - `moverTolerations` for J52's taint.
  - Privileged movers only on namespaces labelled for them. Opt-in is safe because unprivileged movers read files the app owns as another UID poorly on hostPath-backed local-path (`rest-server.yaml:54` notes "hostPath ignores fsGroup"). So the default stays privileged until each app proves an unprivileged mover; see 10b.
  - Migrate existing repos with J03's rotation.
- **Files:** `platform/backups/{enrollment.yaml,rest-server.yaml,CONTRACT.md}`, `platform/kyverno/rbac.yaml`, `tools/bootstrap-secrets`, `tools/restore`.
- **Acceptance:**
  - render tests show no generated Secret carries the global password, and rest-server has `--private-repos`;
  - a policy test that the RBAC list has no cluster-wide Secret delete;
  - live: drill green after the migration, and a mover in namespace A can't list namespace B's repo.
- **Changes:** render yes. **Decision:** D4.

#### J50 — Flux applies every Kustomization as cluster-admin (new)
- **Severity / size / PR:** high / S–M / G6a.
- **What breaks:**
  - `envs/*/flux/flux-instance.yaml:20` sets `multitenant: false`.
  - No Kustomization sets `serviceAccountName` (a grep of `envs/` and `platform/` finds none).
  - So anything under `services/` or `apps/`, a future apps repo (D6), or a code-factory PR is applied with the kustomize-controller's cluster-wide rights: ClusterRoles, any Secret, any platform object.
  - The plan's app isolation (`framework.md:392`, "impersonated Flux Kustomization… no cross-namespace source refs") depends on lockdown. The Flux Operator docs: `.spec.cluster.multitenant` "specifies whether to enable Flux multi-tenancy lockdown. By default, it is `false`". With it on, `tenantDefaultServiceAccount` (default `default`) is used by kustomize-controller and helm-controller.
  - Today there are four Kustomizations and five HelmReleases. P1–P3 add about ten more.
- **Questions:** 17, 22, 23.
- **Found by:** R (E1), F (item 5). Missed by all three auditors and v1.
- **Fix:**
  - `multitenant: true`.
  - Every `flux-system` Kustomization and HelmRelease sets an explicit `serviceAccountName`: platform ones get a `flux-platform` SA bound to cluster-admin, and services and apps get per-tenant SAs (G6b).
  - A CI check: every Kustomization and HelmRelease names an SA, and only an allow-list may use the platform SA.
- **Files:** `envs/*/flux/flux-instance.yaml`, `envs/*/flux/kustomizations/*.yaml`, all `platform/**/helmrelease.yaml`, a new `platform/flux-operator/rbac.yaml`, `ci/checks/`.
- **Acceptance:** the CI check fails on a seeded Kustomization without an SA, and the render passes. Live: on bench, flux-system reconciles green after the switch (the docs don't say whether `flux-system` objects without an SA stall; F flags the same need).
- **Changes:** render yes. **Risk:** deep (a mistake stops every reconcile).

#### J11 — Services and apps have no secrets path, no per-env selection and no tenancy rule; `apps_git_repo_url` is accepted and ignored
- **Severity / size / PR:** **high** (v1: medium) / M / G6b.
- **What breaks:**
  - Only `platform-configs` decrypts (`envs/env-00/flux/kustomizations/platform-configs.yaml:16`).
  - `services` and `apps` always build `./services` and `./apps` for every env.
  - Nothing says who may decrypt what: F item 3. Copying `sops-age` into app namespaces would let any app decrypt every env secret.
  - P1 (Grafana admin) and P2 (Hermes, email and calendar credentials) are the next phases, and both need this.
- **Questions:** 21, 22, 23.
- **Found by:** A F15, B F05, C F07 and C F08; tenancy rule from F.
- **Fix:**
  - Per-service Kustomizations under `envs/<env>/flux/kustomizations/services/`, each with `decryption`, `targetNamespace` and its tenant SA (J50).
  - The rule: decryption happens only in `flux-system` with the env key. Tenant secrets live in the instance under `envs/<env>/secrets/<namespace>/` and are applied by a trusted Kustomization. A tenant namespace never gets the age key.
  - `docs/adding-a-service.md`.
  - Drop `apps_git_repo_url` until D6(b) (J16).
- **Acceptance:** an env-00 render with a sample service and a sample SOPS secret, decrypted in that service's namespace only. A CI check that no Kustomization outside `flux-system` sets `decryption`.
- **Changes:** render yes. **Decisions:** D6, D14.

#### J10 — OS security updates are off, and nothing signals staleness
- **Severity / size / PR:** **high** (v1: medium) / S / G4.
- **What breaks:** `base-os/templates/20auto-upgrades.j2:5` (`Unattended-Upgrade "0"`), `50unattended-upgrades.j2:3` (empty `Allowed-Origins`), and `base-os/tasks/main.yml:70` keep nodes unpatched. Combined with J53 (k3s and Helm Renovate disabled), nothing on the node side ever updates or warns. From P2, workloads parse untrusted email and web content (R13); from P4b they run untrusted code. Kernel and container-runtime patches become the boundary.
- **Questions:** 6, 14.
- **Found by:** C F10, B E11; R (timing).
- **Fix:**
  - Security-only unattended upgrades (`${distro_id}:${distro_codename}-security`) with `Automatic-Reboot "false"`.
  - A verify warning for `/var/run/reboot-required`.
  - A drain-and-reboot runbook.
  - A GPU-node note: use Ubuntu's signed `linux-modules-nvidia-*` meta package, so kernel updates bring matching modules (F).
- **Acceptance:** a base-os pytest asserts the origins and no auto-reboot. Live: `site.yml --check` on bench shows only the expected changes.
- **Changes:** Ansible yes. **Decision:** D5.

### Medium

#### J62 — Backup mover network path is undocumented, and the plan's Hermes egress rule would block it (new)
- **Severity / size / PR:** medium / S / G5b.
- **What breaks:** Backups Expects (`platform/backups/CONTRACT.md:15-22`) doesn't say movers need egress to `restic.platform-backups.svc:8000` (`:42`). Plan R13 (`framework.md:238`) gives each Hermes deploy egress of "DNS plus TCP 443… excluding… pod and service ranges", which blocks the mover. The symptom: a backup that never completes.
- **Found by:** F (item 1).
- **Fix:** an Expects line, plus a NetworkPolicy snippet that every namespace with a default-deny policy must include (J51 generates it).
- **Acceptance:** a render test that the generated allow exists in every PVC namespace.

#### J51 — No namespace security baseline: no Pod Security labels, no default-deny NetworkPolicy, no quotas (new id)
- **Severity / size / PR:** medium; **high before P4a or P4b** / M / G6b.
- **What breaks:** a repo grep finds no `pod-security.kubernetes.io`, `NetworkPolicy`, `ResourceQuota` or `LimitRange` in any manifest. The FluxInstance's `networkPolicy: true` covers `flux-system` only. Plan R30 (`framework.md:278`) wants default-deny and a quota per app namespace.
- **Found by:** C (raised in its answers to Q22–23, with no id); R (D3) gave it one.
- **Fix:** a Kyverno generate policy (or the P4a ResourceSet) that gives every non-platform namespace:
  - `pod-security.kubernetes.io/enforce: baseline`, or `restricted` where movers allow (J08);
  - a default-deny NetworkPolicy plus the DNS and backup allows (J62);
  - a default LimitRange.
  - Platform namespaces are labelled explicitly.
- **Acceptance:** a render test; a CI check that every namespace in the render has an enforce label. Live: drill and testapp still green. **Changes:** render yes.

#### J52 — Node placement contract: the role label prefix is tied to `dns_suffix`, and dedicated nodes have no taints (C F14 reversed)
- **Severity / size / PR:** medium / S–M / G2b.
- **What breaks:**
  - `infra/ansible/roles/k3s/templates/label-patches.json.j2:1` builds `role.<dns_suffix>/`.
  - Removal at `:22` matches only the current prefix, so a suffix change leaves old labels behind.
  - Consumers: `rest-server.yaml:28`, `etcd-snapshots.yaml:24`, `tools/verify/run:204`.
  - An outside app (and, under lockdown, a tenant Kustomization, since `substituteFrom` is same-namespace) can't know the suffix.
  - No `node-taint` exists, and no manifest selects `workers`, so Hermes and apps can land on the GPU or backup nodes (F item 4).
- **Questions:** 4, 11, 22.
- **Found by:** C F14 (v1 rejected it as taste: reversed, since the forward cost is real), F.
- **Fix:**
  - A fixed product prefix (for example `node-role.jarvis.io/<role>`).
  - A NoSchedule taint `<prefix>/dedicated=<role>` on any node without `workers`.
  - Tolerations in rest-server, etcd-snapshots, drill pods (`tools/drill:266,271,300,334,368`), the restore binder (J12) and movers (J08).
  - The template removes both the old and new prefixes once.
- **Acceptance:** template tests for a single node (no taint) and a gpu-only host (taint); render tests for tolerations. Live: a one-time relabel of `bench`, and drill green. **Changes:** render yes, Ansible yes.

#### J12 — The restore binder pod has no node placement
- **Severity / size / PR:** medium / S–M / G5c.
- **What breaks:** `tools/restore:561-580` sets no `nodeSelector`, affinity or tolerations. With `WaitForFirstConsumer` (**confirmed live**: `local-path` is the only and default StorageClass), the binder's node fixes the PV's node.
- **Found by:** C F05.
- **Fix:** copy the mounting workload's `nodeSelector`, `affinity` and `tolerations`, plus a `--node` override.
- **Acceptance:** a unit test on the binder body. Multi-node only.

#### J13 — Fresh-install verify and drill fail for up to 12 hours
- **Severity / size / PR:** medium / S / G7.
- **What breaks:** `tools/verify/run:370-372` ("etcd snapshot copy has no successful scheduled run") until the CronJob `5 */12 * * *` (`etcd-snapshots.yaml:11-12`) first runs.
- **Found by:** C F06, B E16.
- **Fix:** "pending first run" (warn, exit 0) while the CronJob is younger than one interval, plus a doc line.

#### J47 — No scheduled `restic check` (v1: low)
- **Severity / size / PR:** medium / S–M / G5c.
- **What breaks:** only the drill reads bytes, and only from a scratch repo. `verify/run:331-351` checks timestamps. Corruption on the single disk (J01) goes unseen until a restore.
- **Found by:** A F12; R (partial).
- **Fix:** a weekly CronJob in `platform-backups` running `restic check --read-data-subset=5%` per repo, with verify reading its last success.
- **Acceptance:** a render test and a verify unit test. Live: the first run is green.

#### J58 — Per-env Flux wiring is copied boilerplate, and core paths ride the instance's sync source (new)
- **Severity / size / PR:** medium / M / G6b.
- **What breaks:**
  - `git diff --no-index --stat envs/env-00/flux envs/env-01/flux`: 3 of 8 files differ, so 5 are copies.
  - `envs/env-00/flux/platform-configs/kustomization.yaml:4-6` pulls `../../../../platform/configs` and the env's secrets into one build.
  - Every upstream change to stages, gates or decryption (J11, J24, J50) must be hand-copied into each adopter env, and a product and instance split (D14) can't cross a relative path.
- **Found by:** C (F21 text), R (D2), F (item 2).
- **Fix:** platform, services and apps Kustomizations reference a named core `GitRepository` (`jarvis-core`), which today has the same URL. Env-owned secrets get their own Kustomization. The stage Kustomizations become a core base with one env-path patch.
- **Acceptance:** an env-00 render is identical except for the source names, and the env-00-only suite passes. **Decision:** D14.

#### J60 — No per-host key mechanism (new)
- **Severity / size / PR:** medium / S / G3a.
- **What breaks:** `schema/env.py:81` allows only `node_roles` and `data_disk` per host. Kubelet reserves are env-wide (`envs/env-01/values/cluster-vars.yaml:25-27`), so a 14 GiB bench and GPU workstations get one size. There is no `node-ip` for multi-NIC hosts and no per-host SSH user (J22). Without one mechanism, J15, J22 and the reserves each invent their own shape.
- **Found by:** F (item 6).
- **Fix:** an allow-listed set of per-host keys (`ansible_user`, the `k3s_kubelet_*` overrides, `node_ip`), with env values as defaults.
- **Acceptance:** schema tests; template tests where a host override wins. **Changes:** Ansible yes.

#### J61 — verify is one module with no notion of enabled components (new)
- **Severity / size / PR:** medium / M / G7.
- **What breaks:** `tools/verify/run` (488 lines) hard-codes its checks. Once services are chosen per env (J11), verify must skip components that are off, or it fails closed. The plan has 15 lines that extend `tools/verify` across P1–P4a.
- **Found by:** F (item 7).
- **Fix:** one check module per component, registered by name, and run for platform plus the env's enabled list.
- **Acceptance:** the existing verify tests pass unchanged, and a disabled component's checks are skipped.

#### J57 — The k3s agent play restarts every agent at once (new id)
- **Severity / size / PR:** medium / S / G2b.
- **What breaks:** `infra/ansible/site.yml:50-60` ("Join k3s agents", `hosts: all:!control_plane`) has no `serial`. The pinned binary download notifies `Restart k3s` (`k3s/tasks/main.yml:53-61`, `handlers/main.yml:9-14`). A pin bump drops every agent together. Single-node is unaffected.
- **Found by:** C (F21 text), R (D1).
- **Fix:** `serial: 1` on the agent play, plus an optional drain and uncordon around the restart.
- **Acceptance:** a play-structure test. Live: a no-op bump on bench.

#### J53 — Nothing signals a stale version: `tools/versions.env` is unmanaged, and k3s and Helm Renovate are disabled (C F22 partly reversed)
- **Severity / size / PR:** medium / S / G8.
- **What breaks:** `renovate.json` has no manager for `tools/versions.env` (`:6-33`: sops, age, gitleaks, kustomize, kubeconform, kubectl). k3s (`renovate.json:6-11`) and the Helm CLI (`:55-59`) are `enabled: false`. The CI secret scanner can age silently.
- **Found by:** C F22, R (E2).
- **Fix:** a regex manager for `versions.env` with a checksum-bump note; replace `enabled: false` with `dependencyDashboardApproval: true` for k3s and Helm.
- **Acceptance:** `renovate-config-validator` passes, and a dry run lists those dependencies.

#### J54 — No `make site` that validates first; `infra/inventory/roles.yml` is a directory (reversed)
- **Severity / size / PR:** medium / S / G3a.
- **What breaks:**
  - The `Makefile` has no site target.
  - Adopters type three relative paths (`docs/deploy.md:84-87`, and 10 copies in `infra/ansible/README.md`); `preflight.yml:13` has to explain a missing second `-i`.
  - `ls infra/inventory/roles.yml` lists `constructed.yml` and `groups.yml`.
- **Found by:** C F25 and C10; R.
- **Fix:** `make site ENV=<env> [TAGS=]` runs the env schema, then `ansible-playbook` with the right `-i` and `-e`. Rename the directory to `infra/inventory/roles/`.
- **Acceptance:** a Makefile test with a stubbed `ansible-playbook`, and the schema failing first. **Changes:** none to the play itself.

#### J14 — No adopter upgrade or release-intake procedure
- **Severity / size / PR:** medium / M / G9.
- **What breaks:** no written way to take Jarvis releases into a copy, roll k3s, or remove a component whose objects are `prune: disabled` (J59).
- **Found by:** A F13, B F07, C F21.
- **Fix:** `docs/upgrade.md`, written after D14: either "bump the core ref" or "merge upstream".

#### J15 — GPUs: a label plus a vendor assert; no runtime
- **Severity / size / PR:** medium / L / G10.
- **What breaks:** `infra/ansible/tasks/assert-gpu.yml` checks only PCI vendor `0x10de`. There is no toolkit, RuntimeClass or device plugin.
- **Found by:** A F14, B F06, C F13.
- **Fix (F notes, verified as reasonable):**
  - Ubuntu signed `linux-modules-nvidia-*-open` (the Blackwell 5060 Ti needs open modules);
  - the container toolkit;
  - a RuntimeClass;
  - the device plugin plus GPU Feature Discovery labels (`nvidia.com/gpu.memory`), which replace a hand-set per-host memory key;
  - time-slicing on the 16 GB card.
- **Must land before the GPU nodes join;** it depends on J52 and J60.

#### J16 — The schema validates future products and ties the product to today's build tool
- **Severity / size / PR:** medium / S / G3b.
- **What breaks:**
  - `values/hermes.yaml` and `values/apps.yaml` are required (`schema/CONTRACT.md:9-10`; harness case `no_hermes_yaml` fails), with about 60 lines of validators (`schema/env.py:566-622`) and no consumer (neither is in the published ConfigMap).
  - `build_agent` accepts only `cursor` (`schema/CONTRACT.md:44`).
  - `bootstrap-secrets` generates Grafana and Hermes secrets nothing applies.
  - The plan itself says p2-07 "adds validation of the hermes.* keys it renders (D45)", so these validators come before their consumer by the plan's own rule.
- **Found by:** A F17 and A F19, B F10, C F27; R (C1, C2: delete).
- **Fix:** delete `hermes.yaml` and `apps.yaml` from the schema, env-00 and env-01, along with their validators; P2 and P4a re-add them with their consumers. Delete the `build_agent` key (it returns as an adapter list when the code factory lands). Stop generating the Grafana and Hermes specs; keep env-01's encrypted files until P1 and P2.
- **Acceptance:** the harness case `no_hermes_yaml` passes, and a grep for `build_agent` in `schema/` finds nothing. **Decision:** D7.

#### J17 — A second env must share env-01's GitHub owner
- **Severity / size / PR:** medium / S / G3a.
- **What breaks:** `schema/env.py:535-539`. The harness case `clone_keeps_env01_adds_lab_other_owner` fails. A stale `.sops.yaml` rule for a deleted env is tolerated.
- **Found by:** A F4.
- **Fix:** a doc step 0 ("delete `envs/env-01/` and its rule"), and fail on a rule naming a missing env. The owner rule's fate follows D8 and D14.

#### J18 — Flux sync requires a public repo
- **Severity / size / PR:** medium / M / G6b.
- **What breaks:** the `docs/deploy.md` prerequisites; there is no pull secret path.
- **Found by:** C F09.
- **Fix:** an optional SOPS `flux-git-auth` wired as FluxInstance `sync.pullSecret`. **Decisions:** D9, D14. The verified fact in section 8c matters here: GitHub Free doesn't enforce branch protection on private repos.

#### J19 — Agents join with the full server token
- **Severity / size / PR:** medium / S / G2a (with J02). `token.yml:21-35`. Found by C F18.

#### J20 — The internal CA has no name constraints
- **Severity / size / PR:** medium / S / G1. `tools/bootstrap-secrets:132-152`. Found by C F17.
- **Fix:** a critical `permitted` constraint for `dns_suffix`, for new envs. **Decision:** D10.

#### J21 — The command guard's Secret-read deny is case-sensitive, and some categories are uncovered
- **Severity / size / PR:** medium / S / G8 (non-frozen part); the frozen part is D11.
- **What breaks (by category only):** `.cursor/cli.json` denies only some case spellings of the Secret resource noun for read verbs. There is no category for cluster-writing repo entrypoints or for reading the admin kubeconfig. `.claude/settings.json` is unchecked.
- **Found by:** A F7, C F36. B's "no defect" is wrong. With Claude Code lanes now plausible (section 8c), the parity check matters more.

#### J22 — prep-node overwrites the env-wide `ssh_user` on every node
- **Severity / size / PR:** medium / S / G3a.
- **What breaks:** `tools/prep-node:730-738`.
- **Found by:** C F12.
- **Fix:** refuse a different value when one is already set; a per-host user comes via J60.

#### J23 — `dns_suffix` is not validated as a DNS name
- **Severity / size / PR:** medium / S / G3a. The harness case `bad_dns_suffix_Home_Lab` passes. Found by C F35, B E20.
- **Fix:** an RFC 1123 lowercase check.

#### J24 — VolSync is not in the controller health gate
- **Severity / size / PR:** medium / S / G6a. `platform-controllers.yaml:15-23`. Found by B F21.
- **Fix:** add the VolSync HelmRelease to `healthChecks`.

#### J25 — Diagnosis is thin: no node or pod health, and most CONTRACTs lack Operations and Limits
- **Severity / size / PR:** medium / M / G7 (verify) and G9 (text). Found by A F18, B F15 and B F18, C F26.
- **Fix:** verify checks node Ready and pods stuck in CrashLoopBackOff or Pending; CONTRACT text per section 3, Q16.

#### J26 — CI's gitleaks scan sees one commit
- **Severity / size / PR:** medium / S / G8. `.github/workflows/ci.yml` checkout has no `fetch-depth`. Found by C T06.
- **Fix:** `fetch-depth: 0` on the scan job.

#### J27 — Safety branches with no failing test
- **Severity / size / PR:** medium / M / G8 (or in each owning group's test file). Found by C T02–T05, T09, T16–T18, and B E21.
- **Fix:** one seeded test per guard. B's cross-check table is usable as-is.

#### J28 — Flux Operator bootstrap trusts any existing Helm binary and skips readiness waits on a re-run
- **Severity / size / PR:** medium / S / G6a. `flux-operator/tasks/helm_cli.yml`, `operator.yml`. Found by B F23, C F24 and C F38.
- **Fix:** compare the version with the pin; run the waits unconditionally. Delete the arm64 branches (section 6).

#### J29 — Operator kubectl calls have no timeout
- **Severity / size / PR:** medium / S / G7. `tools/lib/_env.py:44`. Found by B F24.
- **Fix:** `--request-timeout=30s` plus a subprocess timeout.

#### J09 — `pvc-backup` accepts a policy that no longer matches PVCs (scope corrected)
- **Severity / size / PR:** medium / S / G8.
- **What breaks:** `ci/checks/lib/_pvc_backup.py` `_is_enrollment_policy` ignores `match`. `failurePolicy: Ignore` (`enrollment.yaml:12`) stays: an orphan PVC is **already** caught by verify (`tools/verify/run:331-345`, "no ReplicationSource"). v1's proposed verify check was a duplicate and is removed.
- **Found by:** A F11, B F12, C T01; R (the correction).
- **Fix:** require a PVC match kind in `_pvc_backup.py`, and add a test that the existing verify branch fails when a bound PVC has no source.

### Low

| id | Finding | Where | Found by | Fix (PR) |
| --- | --- | --- | --- | --- |
| J30 | The drill's etcd check races the snapshot schedule; with no pipefail, a missing file reads "checksum mismatch" (fails closed) | `tools/drill:289-291`, `:640-650` | judge (baseline), C F31 | `[ -f ]` check and a clear message; pick the name from `restic ls latest` (G5c) |
| J31 | The base-os handlers use `service`, which is non-portable; **ruled a portability bug** | `base-os/handlers/main.yml:3,11,19` | B F08, C T15 | Switch to `ansible.builtin.systemd` (proven in scratch) (G4) |
| J32 | Controller prerequisites: apt without `update`; tools run with system `python3` (`Makefile:34-57`), so `install-deps:77-90` installs apt `python3-yaml` and `python3-bcrypt` while `.venv` pins the same libraries; the Python ≥ 3.12 minimum (`ansible-core` metadata `Requires-Python: >=3.12`) is undocumented | `tools/install-deps`, `Makefile` | A F21, B F19, C F32, C F33; R | Run the tools with `.venv/bin/python`, keep only `python3-venv` and `openssl` on apt, run `apt-get update`, document Python 3.12 (G4) |
| J33 | Age mismatch offers to delete the env's secrets (the prompt defaults to No) | `tools/prep-node:880-908` | A F6 | Refuse and point to J04's runbook (G1) |
| J34 | env-00 lacks `secrets/README.md`; the docs point at env-01 files | `docs/deploy.md:77`, `docs/README.md` | A F8, B F09, C F29 | Add a generic README (G1); fix the docs index (G9) |
| J35 | The whole age key file is copied into the cluster | `flux-operator/tasks/sops_age.yml:69` | C F15 | Copy only the matching identity (G6a) |
| J36 | verify fetches the full Secret JSON | `tools/verify/run` `_check_secret` | B F25 | Key names only (G7) |
| J37 | Kyverno is tested upstream on 1.33–1.35; k3s is 1.36.5. Mitigated: verify fails on any PVC without a source (`verify/run:343-345`) | `platform/kyverno/CONTRACT.md:24` | B F11 | Add it to the k3s-bump checklist in J14 (G9) |
| J38 | Chrony is NTS-only | `chrony.conf.j2:8-9` | C F20 | `chrony_nts` switch (G4) |
| J39 | About 30 plan or decision codes in core comments | e.g. `apps/placeholder.yaml:1`, `site.yml:4`, `ci/checks/*:2` | A F20, C F28 | Remove them; extend `_plan_codes.py` to non-Markdown (G9) |
| J40 | Shape tests, a dead assertion | `ci/tests/test_restore.py:652`, `test_flux_operator.py` | B F17, C T07, T10, T19 | Delete the dead assertion; trim only with a behaviour test kept (G8) |
| J41 | `run-checks` stops at the first failure; core-names root scope; missing contract roots pass | `ci/run-checks`, `_contract_md.py:9-17` | C T11–T13 | Collect all failures; delete the missing roots (G8) |
| J42 | CA-only regeneration keeps the old `ca.crt` | `tools/bootstrap-secrets` `bootstrap()` | C T14 | Refuse on a mismatch (G1) |
| J43 | Kyverno provenance PolicyError on the drill PVC. **Closed:** live check shows 10 events in 4 days, all on `platform-drill/drill`, all retried within about 1 s, no leftover URs, no rule collision | baseline, live | judge | None. Note it as a known drill log line in Backups Health (G9) |
| J44 | The cluster-reset step has no cross-link to the snapshot restore | `infra/ansible/README.md:72` | A F10 | One line (G9) |
| J45 | k3s restarts right after its first start | k3s role | C F39 | Skip the notify on first install (G2a) |
| J46 | Non-HTTP apps can't be exposed. Trigger: before voice over UDP (SIP or WebRTC) or any TCP outside app | traefik, `_loadbalancer.py` | C F19 | A Limits line now (G9); an opt-in LB annotation at the trigger |
| J48 | Old base-os comment on the live host | baseline | judge | Clears on the next `site.yml` |
| J49 | `bootstrap-secrets` ignores `age_key_file` | `envs/env-01/secrets/README.md:15` | C F34 | Read it when `SOPS_AGE_KEY_FILE` is unset (G1) |
| J55 | Dead `data_disk` **group** and an unconsumed `storage` role (reversed). `groups.yml:2` says "site.yml does not target this group"; no play targets it (`site.yml` hosts: `all`, `control_plane`, `all:!control_plane`). `storage` is allowed at `schema/env.py:79`, no manifest schedules by it, and that breaks `schema/CONTRACT.md:100` ("A role is added only in the same PR as the first service that schedules by it") | as cited | C F30, R | Delete the group, its tests and `storage`; keep the `data_disk` host var (G3b) |
| J56 | CI schema validation needs the network: `ci/tests/test_flux_entry.py:70-77` uses `-schema-location default` and a raw-GitHub catalog (`:40-44`). **Judge-only:** the vendored VolSync schemas set `spec: additionalProperties: true`, so ReplicationSource specs are not validated at all | as cited | C T20 (reversed), judge | Vendor the schemas for the pinned minor; vendor the real VolSync 0.17 CRD schema (G8) |
| J59 | 24 objects under `platform/` are `prune: disabled`, with no removal procedure | `grep -rc "prune: disabled" platform` | R (D4) | Removal steps in `docs/upgrade.md` (G9) |
| J63 | GPU edge cases: the GPU assert runs in the `any_errors_fatal` fact play (`site.yml:9,20`), so an unplugged eGPU stops `site.yml` for every node; `k3s_data_dir` is fixed on root (`group_vars/all.yml:6`) while CUDA images run 5–10 GB | as cited | F (B2, B3) | Warn and drop the GPU label instead of failing; decide disk sizing before the GPU nodes are installed (G10) |
| J64 | Any namespace's Ingress can claim any `*.<suffix>` host (wildcard TLSStore, `internal-ca.yaml:24-34`) | as cited | F (B6) | A ValidatingAdmissionPolicy (fail-closed) binding `app-<name>` to `app-<name>.<suffix>`, before P4a (G6b or P4a) |

---

## 3. Answers by question

1. **Reliable, flexible, practical, lean?**
   - The platform slice is lean.
   - Not lean: the pre-consumer schema (J16), dead inventory pieces (J55), and the test and checker weight (J40, D13).
   - Not reliable on loss: J01–J04.
2. **Glue a native approach would replace?**
   - Duplicated Flux facts (J06) and per-env boilerplate (J58).
   - System-python tools plus apt libraries (J32).
   - The Helm version check (J28).
   - prep-node, the Renovate managers and the verify cron logic stay.
3. **Too complex for what it buys?** `_cli_permissions.py` (D13), the shape tests (J40), and the Hermes and apps validators (J16, delete).
4. **Brittle?**
   - J06, J24, J28, J30, J12, J37;
   - the suffix-bound labels (J52);
   - all agents restarting together (J57);
   - network-bound CI schemas (J56);
   - a fatal GPU assert (J63).
5. **Too strict?**
   - The owner rule (J17).
   - Cursor-only `build_agent` and the required `hermes.yaml` (J16).
   - No per-host keys (J60).
   - Public-repo only (J18).
   - One control plane and amd64 are limits (D12).
6. **Real gaps?** Off-host backup (J01), the token and rebuild path (J02), key custody (J04), patching and update signals (J10, J53), the services path and tenancy (J11, J50, J51), the GPU runtime (J15), upgrades (J14).
7. **Cruft?** The placeholders, missing contract roots, arm64 branches, duplicate restore helpers, the dead assertion, plan codes, the `data_disk` group and the `storage` role (section 6).
8. **Clean product?** Core identity is clean. About 30 plan codes remain (J39). The docs index leads with env-01 (J34). Gordon's env ships in the product (D14).
9. **Clone to verify and drill from docs alone?** Not yet: J05, J34, J06 and J13. After wave 1, yes for one node.
10. **Easy prep with early errors?** The schema is good but runs after host changes (J05), and misses J06, J07 and J23. There is no `make site` gate (J54).
11. **Single and multi-node, mixed GPUs?**
    - Shapes render.
    - Multi-node needs J12, J22, J57 and J60.
    - GPUs need J52, then J15.
    - `local-path` `WaitForFirstConsumer` is confirmed live.
12. **Unwritten knowledge?** Rebuild (J02), restic rotation (J03), age custody and reseal (J04), upgrades and removals (J14, J59), patching (J10), the first-run etcd copy (J13), deleting env-01 (J17), cluster-reset (J44), mover egress (J62).
13. **Real loss and disk death?** PVC restore works and is drilled. Disk death loses everything (J01). A rebuilt server can't use etcd (J02). There is no integrity check (J47).
14. **Upgrades?** Renovate bumps repo pins only. k3s, Helm and `versions.env` give no signal (J53). There is no intake path (J14), and the D14 split makes it a ref bump.
15. **Rotation and offline key?** CA rotation is documented (J42). Restic rotation is unsafe (J03). Age custody is missing, and the recovery doc is lossy (J04).
16. **Can they tell what broke?** Partly: J25, J29, J61. CONTRACT Operations and Limits needs:

    | CONTRACT | Add |
    | --- | --- |
    | `platform/backups` | Operations: the rebuild runbook (J02), restic rotation (J03), the first-run etcd copy (J13), `restic check` (J47); Expects: mover egress (J62); Health: the J43 drill log line |
    | `platform/cert-manager` | Limits: one internal CA, name constraints (J20) |
    | `platform/flux-operator` | Operations: the FluxInstance re-apply (from `infra/ansible/README.md:74`) and the upgrade; Limits: applied once by Ansible; lockdown (J50) |
    | `platform/kyverno` | Operations: what to do when admission is down (verify catches orphans); Limits: version skew |
    | `platform/traefik` | Limits: HTTP(S) only (J46) |
    | `platform/app-dns` | Limits: router forwarding is an adopter prerequisite |
    | `platform/configs` | Limits: decrypts platform only (until J11) |
    | `schema` | Operations: adding a key, the allow-list, per-host keys (J60) |
    | `docs/code-delivery` | It already marks its planned rows |
    | `platform/testapp` | Nothing |
17. **Leaks or over-trust?** J07, J08, J50, J51, J19, J20, J21, J26, J35, J36, J64. There are no default credentials or open ports.
18. **Test gaps worth filling?** J27, J09, J26, J41, J56.
19. **Code for little benefit?** J40, D13, J16.
20. **Behaviour tests, fresh env-00-only copy?** Mostly behaviour. env-00-only: checks green, and pytest at 482/483, the only failure J31. CI is green.
21. **A clean place to land?** Location, certs, DNS and backups: yes. Secrets, per-env selection, tenancy, health and placement: not yet (J11, J50, J51, J61, J52).
22. **Blocks or constraints?** J08, J50, J51 (tenancy); J15, J52, J63 (GPU); J16 (build agent); J46 (non-HTTP); J58 (instance layout).
23. **What every consumer needs?** J11 plus J50 (a decrypting, impersonated, per-env deploy path), J08 and J62 (safe backups), J51 (a namespace baseline), J61 (per-component verify), J01 (off-host).
24. **Deferred, must not be lost:** section 4.

---

## 4. Deferred, must not be lost

| Item | Recorded in the repo today | Pick up by |
| --- | --- | --- |
| Off-host backup copy | `platform/backups/CONTRACT.md:187-188` | **Foundation (G5a)**, before the P2 exit |
| etcd restic-only path; rebuild drill | Backups Limits (partly); the rebuild drill is **not recorded** | Foundation (G2a) |
| Per-namespace backup credentials; mover privilege opt-in | **Not recorded** as a deferral | **Foundation (G5b)**, before p2-09 |
| Automatic restore validation (full restore test) | **Not recorded** | After J47's `restic check`; with alerting (P1) |
| Backup-failure alerts | Placeholder only (`services/placeholder.yaml:1`) | P1 observability |
| Offline age-key copy, tested | `framework.md:509` (plan), not the repo | **Foundation (G1)** |
| `make verify` trusting the internal CA explicitly | **Not recorded** | G7 |
| Backup scale controls | Partly in Backups | First volume over about 50 GB |
| A "rebuildable cache" backup class (model weights, build caches) | **Not recorded**; R31 allows only observability (`framework.md:282`) | Before GPU or code-factory volumes (framework rule change) |
| Per-arch k3s checksums | **Not recorded** | First arm64 node (D12) |
| "Planned" CONTRACT rows (admin-gate, registry, Forgejo, the issue queue) | `docs/code-delivery/CONTRACT.md:5-26` | P2 exit and P4b |
| Hermes, observability and code-factory homes | Placeholders and the missing `EXPLICIT_COMPONENT_ROOTS` entries | Move to `docs/roadmap.md` when deleted (section 6) |
| Web UI auth story (front door or forward-auth) | Plan R27 (`framework.md:272`) relies on built-in logins | Before the first UI without its own login |
| Non-HTTP exposure (UDP for voice) | **Not recorded** | Before voice over UDP (J46) |
| Hostname-claim policy | **Not recorded** | Before P4a (J64) |
| Claude subscription token expiry (if D7 adds a Claude Code lane) | **Not recorded**; the framework says the CA root is "the only scheduled expiry" (`framework.md:509`) | With the first lane using `setup-token` (1-year token) |
| Known test weak spots (maintainer notes) | **Not in the repo** | Record in `ci/README.md` (G8) |

---

## 5. What's good and should stay

- The `infra/platform/services/apps/envs` layout; the per-env folder, schema and ConfigMap allow-list.
- SOPS in Git; LAN-only; the internal CA.
- Kyverno per-PVC enrollment with `backup: none`; restore that fails closed with a rebuild hold; verify catching orphan PVCs (`verify/run:331-345`).
- `make drill` proving bytes for the PVC and etcd paths.
- The CI guardrails (secret scan, env schema, command-guard check, core-names, kustomize build, manifest lint, prune guard, contract-md). Green on an env-00-only tree.
- Digest and checksum pins; Renovate with no stability hold.
- Ansible that only checks disks and network; per-env kubeconfig; the AppArmor-aware chrony validate; `no_log` and 0600 checks on the age key.
- The verify cron evaluator (correct about day-of-month, day-of-week and DST, and tested).

---

## 6. Delete list (reproduced)

| Item | Evidence | PR |
| --- | --- | --- |
| `services/placeholder.yaml`, `apps/placeholder.yaml` → `resources: []` | The only resources in their kustomizations; an empty kustomization builds (exit 0) | G9; roadmap entry |
| `EXPLICIT_COMPONENT_ROOTS` entries for missing paths (`docs/build-agent`, `services/hermes/deploy/{code-factory,code-factory/sandbox,ops}`, `services/observability`) | `ci/checks/lib/_contract_md.py:9-17`; each `[ -e ]` fails | G8 |
| arm64 branches in `flux-operator/tasks/helm_cli.yml:5-16` and `defaults/main.yml:14-16` | Preflight and schema are amd64-only | G6a |
| Duplicate `_reason` and `_not_found` in `tools/restore:98-123` | Identical to `tools/lib/_env.py:57-66` | G5c |
| Dead assertion `ci/tests/test_restore.py:652` | The second `readouterr()` is always empty | G8 |
| Plan codes in about 30 non-Markdown core lines | `git grep` count, 32 lines | G9 |
| `hermes.yaml` and `apps.yaml` with their validators (`schema/env.py:566-622`), the `build_agent` key, Grafana and Hermes generation for new envs | No consumer; p2-07 re-adds Hermes validation with the render | G3b (D7) |
| The `data_disk` Ansible group (`infra/inventory/roles.yml/groups.yml`, the `constructed.yml` rule, `test_inventory_roles.py:101-138`) | `groups.yml:2`: "site.yml does not target this group" | G3b |
| The `storage` role | No consumer; breaks `schema/CONTRACT.md:100` | G3b |

**Kept:** the `data_disk` host var (live at `node-disk/tasks/main.yml:4`), the Renovate managers, the verify cron helpers, and the `test_env_shapes.py:40-56` mutation proofs.

---

## 7. Fix PRs, by impact

Each row points to a PR card in 7b.

| Rank | PR | Covers | Why at this rank |
| --- | --- | --- | --- |
| 1 | G5a | J01 | Critical data loss |
| 2 | G2a | J02, J19, J45 | Rebuild impossible; also the agent token |
| 3 | G1 | J03, J04, J20, J33, J34, J42, J49 | Documented steps destroy backups |
| 4 | G3a | J05, J06, J07, J17, J22, J23, J54, J60 | The adopter's first hour; bad values reach hosts |
| 5 | G5b | J08, J62 | Untrusted workload from p2-09; costs grow per repo |
| 6 | G6a | J50, J24, J28, J35 | Cluster-admin for everything; cheapest with 4 Kustomizations |
| 7 | G6b | J11, J51, J58, J18, J64 | Every P1–P4 consumer needs it |
| 8 | G4 | J10, J31, J32, J38 | Unpatched nodes before untrusted workloads |
| 9 | G2b | J52, J57 | Placement before GPU nodes and services; all agents restart together |
| 10 | G7 | J13, J25 (verify), J29, J36, J61 | A fresh install goes green; verify ready for per-env components |
| 11 | G5c | J12, J30, J47 | Multi-node restore; integrity check |
| 12 | G8 | J09, J21 (non-frozen), J26, J27, J40, J41, J53, J56 | CI holes and update signals |
| 13 | G3b | J16, J55 | Cruft that shapes P2 and P4b |
| 14 | G9 | J14, J25 (text), J37, J39, J43 note, J44, J46, J59, deletes | Docs and cruft |
| 15 | G10 | J15, J63 | Phase-bound: before the GPU nodes join |

## 7b. Fix PRs by area (cards for coding agents)

**Risk tiers:**
- *light*: docs, tests and CI only;
- *release check*: changes tool behaviour or render; needs full CI plus a reviewer's read;
- *deep*: changes cluster, node or backup state; needs a bench live check by Operator and a maintainer review.

### G1 — Secrets and keys
- **Goal:** safe rotation and recovery; key custody; a template secrets README; CA constraints for new envs.
- **Covers:** J03, J04, J20, J33, J34, J42, J49.
- **Files:**
  - `tools/bootstrap-secrets`;
  - `envs/env-00/secrets/README.md` (new) and `envs/env-01/secrets/README.md`;
  - the age-recovery section of `infra/ansible/README.md`;
  - `tools/prep-node` (`sync_age_key` and `delete_env_secrets` only);
  - `platform/backups/CONTRACT.md` (the Operations "Rotate restic" subsection only);
  - `ci/tests/test_bootstrap_secrets.py`, `ci/tests/test_prep_node.py` (the age cases).
- **Acceptance:**
  - deleting `backups-restic` and rerunning refuses without `--rotate-restic`;
  - a CA-only regeneration with a mismatched `ca.crt` refuses;
  - a new CA has a critical `permitted` constraint for the suffix;
  - an age mismatch makes no deletion;
  - `age_key_file` is used when `SOPS_AGE_KEY_FILE` is unset;
  - doc links pass.
- **Size:** M. **Render / Ansible:** no / no. **Risk:** release check. **Live check:** no (a rotation rehearsal runs later in G5b). **Decisions:** D3, D10.

### G2a — k3s identity and rebuild path
- **Goal:** seal the server token and add an agent token; write and rehearse the rebuild runbook.
- **Covers:** J02, J19, J45.
- **Files:** `infra/ansible/roles/k3s/` (tasks `token.yml`, `main.yml` notify, templates `config.yaml.j2` and `agent-config.yaml.j2`), `tools/bootstrap-secrets` (the `k3s-tokens` spec), `schema/env.py` (only the new secret file in the required list, for non-example envs), `platform/backups/CONTRACT.md` (an Operations "Rebuild a dead node" subsection), `ci/tests/test_k3s.py`.
- **Acceptance:** template tests (the token comes from the var under `no_log`; agents get `agent-token`); no restart notify on first install.
- **Live:** the owner seals env-01's current token before merge; `site.yml` on bench shows `changed=0` for k3s config; a rebuild rehearsal succeeds.
- **Size:** M. **Render / Ansible:** no / yes. **Risk:** deep. **Live check:** yes. **Decision:** D2. **After:** G1 (both edit `bootstrap-secrets`).

### G2b — Node placement and rolling agents
- **Goal:** a fixed label prefix, taints for dedicated nodes, matching tolerations, and serial agent restarts.
- **Covers:** J52, J57.
- **Files:** `infra/ansible/roles/k3s/templates/label-patches.json.j2` (plus a new taint template), `infra/ansible/site.yml` (agent play `serial`), `platform/backups/{rest-server.yaml,etcd-snapshots.yaml}` (selectors and tolerations), `tools/verify/run:204` area (or its G7 module), `tools/drill` and `tools/restore` (tolerations), `schema/CONTRACT.md` ("Node roles and labels").
- **Acceptance:** template tests (single node has no taint; a gpu-only host is tainted; the old prefix is removed); render tests (tolerations present); a play test (`serial: 1` on agents).
- **Live:** a one-time relabel of `bench`, drill green.
- **Size:** M. **Render / Ansible:** yes / yes. **Risk:** deep. **Live check:** yes. **After:** G2a (k3s role), G5c and G7 (the same tool files).

### G3a — New-env path: validate before anything touches a host
- **Goal:** schema-first prep-node and `make site`; catch duplicated or bad values; a per-host key mechanism.
- **Covers:** J05, J06, J07, J17, J22, J23, J54, J60.
- **Files:**
  - `schema/env.py`, `schema/CONTRACT.md`;
  - `tools/prep-node` (`run`, `apply_env_edits`, `_TOOLS`);
  - `Makefile` (the `site` target);
  - `infra/inventory/roles.yml/` renamed to `infra/inventory/roles/`, with every reference updated (`docs/deploy.md`, `infra/ansible/README.md` command blocks, `preflight.yml:13`, tests);
  - `infra/ansible/roles/k3s/templates/{config,agent-config}.yaml.j2` (per-host kubelet keys and `node-ip` overrides only);
  - `docs/deploy.md`;
  - `ci/tests/test_env_schema.py`, `ci/tests/test_prep_node.py`, a new `ci/tests/test_make_site.py`.
- **Acceptance:**
  - an env with a schema error makes zero SSH calls;
  - the doc-order harness passes;
  - the FluxInstance and stale-ConfigMap cases fail;
  - URLs with userinfo, query or fragment fail;
  - `Home_Lab` fails;
  - a `.sops.yaml` rule for a missing env fails;
  - a per-host `ansible_user` and `k3s_kubelet_kube_reserved` render on that host only;
  - `make site` runs the schema first.
- **Size:** M (it may split J60 and J54 into a G3a-2 if review is slow). **Render / Ansible:** no / yes (per-host overrides only). **Risk:** release check. **Live check:** no. **Decision:** D8.

### G3b — Schema cleanup
- **Goal:** remove pre-consumer config and dead inventory pieces.
- **Covers:** J16, J55.
- **Files:** `schema/env.py` (`_validate_hermes`, `_validate_apps`, `build_agent`, `storage`), `schema/CONTRACT.md`, `envs/env-00/values/{hermes,apps}.yaml`, `envs/env-01/values/{hermes,apps}.yaml`, `tools/bootstrap-secrets` (the Grafana and Hermes specs for new envs), `infra/inventory/roles/` (the `data_disk` group), `ci/tests/test_inventory_roles.py`, `ci/tests/test_env_schema.py`.
- **Acceptance:** an env without `hermes.yaml` validates; `build_agent` is unknown; no `data_disk` group in the inventory graph; existing env-01 encrypted Grafana and Hermes files are untouched.
- **Size:** S. **Render / Ansible:** no / no. **Risk:** light. **Live check:** no. **Decision:** D7. **After:** G3a (same files).

### G4 — Ansible node prep and controller deps
- **Goal:** security updates on; portable handlers; NTS switch; controller deps from `.venv`.
- **Covers:** J10, J31, J32, J38.
- **Files:** `infra/ansible/roles/base-os/` (templates, tasks, handlers), `tools/install-deps`, `Makefile` (tool invocations `python3` → `.venv/bin/python`), `requirements-dev.txt` (if needed), `infra/ansible/README.md` (the patching runbook section only), `docs/deploy.md` (the prerequisites line for Python 3.12, coordinated with G3a), `ci/tests/test_base_os.py`, `ci/tests/test_install_deps.py`.
- **Acceptance:** the chrony test passes on non-systemd; the unattended-upgrades origins are security-only with no auto-reboot; `make deps` runs `apt-get update` once and needs no apt Python libraries.
- **Live:** `site.yml --check --tags base-os` on bench shows only the expected diffs.
- **Size:** S–M. **Render / Ansible:** no / yes. **Risk:** release check. **Live check:** yes (check mode). **Decision:** D5.

### G5a — Off-host backup copy
- **Goal:** a copy of every repo to an adopter-set restic backend; a declared backup mount.
- **Covers:** J01.
- **Files:** `platform/backups/` (a new `offsite-copy.yaml` CronJob, kustomization), `platform/backups/CONTRACT.md`, `schema/env.py` (`offsite_restic_repository`), `tools/bootstrap-secrets` (the `backups-offsite` spec), `infra/ansible/roles/node-disk/tasks/main.yml`, the G7 verify module for backups.
- **Acceptance:** the schema cases; a render with and without the key; Ansible localhost for the declared and undeclared mounts; a verify unit test.
- **Live:** the first copy completes on bench, and a restore from the off-host target into the drill namespace succeeds.
- **Size:** L. **Render / Ansible:** yes / yes. **Risk:** deep. **Live check:** yes. **Decision:** D1. **After:** G2a, G7.

### G5b — Backup trust boundary (final enrollment shape)
- **Goal:** one policy recreation that settles credentials, privilege, tolerations and the network path.
- **Covers:** J08, J62.
- **Files:** `platform/backups/{enrollment.yaml,rest-server.yaml,CONTRACT.md}`, `platform/kyverno/rbac.yaml`, `tools/bootstrap-secrets`, `tools/restore` (per-namespace credentials), a migration Job manifest under `platform/backups/`.
- **Acceptance:** render tests (no global password in generated Secrets; `--private-repos`; no cluster-wide Secret delete; `moverTolerations`; mover egress allow generated).
- **Live:** a migration of the existing repos with J03's rotation; drill green; a cross-namespace repo read is refused.
- **Size:** L. **Render / Ansible:** yes / no. **Risk:** deep. **Live check:** yes. **Decision:** D4. **After:** G1, G5a, G2b. **Must merge before p2-09.**

### G5c — Restore and drill hardening
- **Goal:** binder placement, a clear drill race message, a scheduled integrity check, shared helpers.
- **Covers:** J12, J30, J47 (and the restore helper delete).
- **Files:** `tools/restore`, `tools/drill`, `platform/backups/restic-check.yaml` (new), `platform/backups/kustomization.yaml`, `ci/tests/test_restore.py`, `ci/tests/test_drill.py`.
- **Acceptance:** the binder copies the workload's placement; a missing etcd file prints "not yet in restic copy"; the restic-check CronJob renders.
- **Live:** drill green; the first `restic check` is green.
- **Size:** M. **Render / Ansible:** yes / no. **Risk:** release check. **Live check:** yes.

### G6a — Flux lockdown and controller gates
- **Goal:** Flux multi-tenancy lockdown with explicit service accounts; the VolSync gate; the Helm pin and waits; a minimal age secret.
- **Covers:** J50, J24, J28, J35.
- **Files:** `envs/*/flux/flux-instance.yaml`, `envs/*/flux/kustomizations/*.yaml`, `platform/**/helmrelease.yaml` (`serviceAccountName`), `platform/flux-operator/rbac.yaml` (new), `infra/ansible/roles/flux-operator/` (`helm_cli.yml`, `operator.yml`, `sops_age.yml`, defaults minus arm64), a new `ci/checks/flux-identity` plus its wrapper, tests.
- **Acceptance:** the CI check fails on a seeded Kustomization without an SA; the healthChecks include VolSync; a Helm version mismatch re-downloads; the waits run on a re-run; the age Secret holds one identity.
- **Live:** bench reconciles green after `multitenant: true`, with every Kustomization Ready.
- **Size:** M. **Render / Ansible:** yes / yes. **Risk:** deep. **Live check:** yes.

### G6b — Service and app landing, tenancy, instance seam
- **Goal:** decrypting, impersonated per-service Kustomizations from a named core source; the namespace baseline; the hostname policy; an optional private sync.
- **Covers:** J11, J51, J58, J18, J64.
- **Files:** `envs/*/flux/` (layout), a new `platform/tenancy/` (Kyverno generate for PSA, default-deny, LimitRange and the backup and DNS allows; a ValidatingAdmissionPolicy for hosts), `services/` and `apps/` (sample service), `schema/env.py` (enabled services list, optional `flux-git-auth`), `tools/bootstrap-secrets` (the optional spec), `docs/adding-a-service.md`, `ci/checks/` (the no-tenant-decryption and namespace-label checks), tests.
- **Acceptance:** an env-00 render has a sample service with its secret decrypted in its own namespace; every rendered namespace has a PSA enforce label and a default-deny policy; a seeded Ingress claiming another app's host is rejected by a policy test; the env-00-only suite passes.
- **Live:** testapp and drill green under the baseline.
- **Size:** L, so **two PRs:** G6b-1 (the J58 seam plus the J11 landing) and G6b-2 (J51 plus J64 baseline; J18 optional). **Render / Ansible:** yes / no (yes for J18). **Risk:** deep. **Live check:** yes. **Decisions:** D6, D9, D14. **After:** G6a, G3a, G5b (the mover egress allow).

### G7 — verify and tool runtime
- **Goal:** per-component verify; fresh-install friendliness; node and pod health; timeouts; no Secret payload fetch.
- **Covers:** J13, J25 (verify), J29, J36, J61.
- **Files:** `tools/verify/` (split into modules), `tools/lib/_env.py`, `ci/tests/test_verify.py`.
- **Acceptance:** the existing verify tests pass; a disabled component is skipped; a young CronJob gives a warning with exit 0; a NotReady node fails; a fake kubectl that sleeps times out cleanly; the Secret check uses key names only.
- **Live:** `make verify` on bench, read-only.
- **Size:** M. **Render / Ansible:** no / no. **Risk:** release check. **Live check:** yes (read-only).

### G8 — CI, tests and update signals
- **Goal:** close the CI holes and make every pin visible to Renovate.
- **Covers:** J09, J21 (non-frozen), J26, J27, J40, J41, J53, J56, the missing contract roots, the dead assertion, and the known weak spots recorded in `ci/README.md`.
- **Files:** `.github/workflows/ci.yml`, `ci/checks/` (`_pvc_backup.py`, `_contract_md.py`, `_cli_permissions.py` corpus, `run-checks`), `ci/tests/` (except files owned by G1, G3a, G4 and G7), `ci/tests/fixtures/schemas/` (vendored), `renovate.json`, a new `ci/README.md`.
- **Acceptance:**
  - a seeded non-PVC enrollment policy fails;
  - the existing verify orphan branch has a failing test;
  - two seeded check failures are both reported;
  - kubeconform runs with the network disabled;
  - the vendored VolSync schema rejects an unknown spec field;
  - `renovate-config-validator` passes and the dashboard lists k3s, Helm and the `versions.env` tools;
  - the gitleaks job scans the full history.
- **Size:** M. **Render / Ansible:** no / no. **Risk:** light. **Live check:** no. **Decision:** D11 (the frozen part), D13.

### G9 — Docs, CONTRACTs, cruft
- **Goal:** upgrade and removal docs, CONTRACT Operations and Limits, plan-code removal, placeholder deletes, a roadmap.
- **Covers:** J14, J25 (text), J37, J39, J43 (Health note), J44, J46, J59, and the section 6 deletes not owned elsewhere.
- **Files:** `docs/` (except `deploy.md`), every `CONTRACT.md` (sections not owned by G1, G2a and G5), `docs/roadmap.md` (new), comment-only edits, `services/placeholder.yaml`, `apps/placeholder.yaml`, `ci/checks/lib/_plan_codes.py`.
- **Acceptance:** contract-md and plan-code checks are green and cover non-Markdown; the prune-guard test passes with the placeholders removed.
- **Size:** M. **Render / Ansible:** yes (placeholders) / no. **Risk:** light. **Live check:** no. **After:** everything else, and D14 for `upgrade.md`.

### G10 — GPU runtime
- **Goal:** usable NVIDIA capacity across mixed cards.
- **Covers:** J15, J63.
- **Files:** a new GPU task in `infra/ansible/roles/base-os/` (or a `gpu` role), `infra/ansible/tasks/assert-gpu.yml`, a new `platform/gpu/` (device plugin, GFD, RuntimeClass, time-slicing config), `schema/env.py` (per-host GPU options via J60).
- **Acceptance:** Ansible localhost with a stubbed PCI; a render; an unplugged GPU warns rather than failing the run.
- **Live:** a CUDA sample pod on each card.
- **Size:** L. **Render / Ansible:** yes / yes. **Risk:** deep. **Live check:** yes. **After:** G2b, G3a, G4.

### Landing order

1. **Decide first:** D1, D2, D4, D5, D6, D7 and D14. They block G5a, G2a, G5b, G4, G6b, G3b and G6b-1 respectively.
2. **Wave 1, in parallel (no shared files):** G1, G3a, G4, G5c, G6a, G7, G8.
   - Shared-file notes: G1 owns the prep-node age functions and G3a owns `run` and `apply_env_edits`, so tell both agents.
   - G4 and G3a both touch `docs/deploy.md` (G4 only the prerequisites line), so land G3a first if they collide.
3. **Wave 2:**
   - G2a after G1 (`bootstrap-secrets`);
   - G3b after G3a (`schema/env.py`);
   - G2b after G2a, G5c and G7 (k3s role and tool files).
4. **Wave 3:**
   - G5a after G2a (Backups CONTRACT) and G7 (verify module);
   - G5b after G5a, G1 and G2b (enrollment file, rotation, tolerations);
   - G6b-1 after G6a and G3a.
5. **Wave 4:** G6b-2 after G6b-1 and G5b (mover egress allow).
6. **Wave 5:** G9 last (text everywhere).
7. **Phase-bound:** G10 before the GPU nodes join, after G2b, G3a and G4.

**Hot spots:**
- `tools/bootstrap-secrets`: G1, then G2a, then G5a, then G5b, then G6b, then G3b.
- `schema/env.py`: G3a, then G2a (one line), then G3b, then G5a, then G6b.
- `platform/backups/CONTRACT.md`: G1 and G2a own subsections, then G5a, then G5b, then G9.

**Where impact and area disagree:**
- **J01 (rank 1) lands in wave 3.** Take D1 now. G5a needs only G2a's CONTRACT text and G7's verify module; give G5a's agent a stub module boundary so it can start in wave 2. G1 adds a one-line "keep your own off-host copy" warning meanwhile.
- **G5b (rank 5) waits for G5a and G2b.** It must still merge before p2-09. If G2b slips, ship G5b without tolerations and add them in G2b (one manifest line).
- **G6a (rank 6) is wave 1 but deep.** Run it in parallel, with a bench live check gating merge, not starting.
- **G2b (rank 9) gates G5b and G10.** Treat it as wave 2's priority.
- **J21 is medium but blocked by D11.** The G8 corpus and parity part lands regardless.

## 7c. Definition of done for the foundation

The foundation is finished when every box below is ticked. G10 is excluded, since it is gated by the GPU nodes joining; its prerequisites G2b and G3a's J60 are included.

- [ ] Fix PRs G1–G9 (including G6b-1 and G6b-2) are merged, with every finding in section 2 closed or explicitly moved to section 4 with a trigger.
- [ ] D1–D14 are answered and recorded in the plan repo.
- [ ] `make ci-verify` is green on the full tree and on an env-00-only copy, on both a systemd and a non-systemd controller, with the network disabled for tests.
- [ ] **Adopter walkthrough:** from a fresh clone with `envs/env-01` deleted, following only `docs/`, a new env passes the schema before any SSH. `make site`, `make verify` and `make drill` are green on a scratch node within one hour of install. (Run by an agent on a scratch VM or bench spare; Operator for the node part.)
- [ ] **Rebuild rehearsal:** on bench, the documented "Rebuild a dead node" path (`site.yml`, then hold, then `restore-all`, using the sealed token) restores the drill and testapp data.
- [ ] **Off-host:** verify shows a fresh off-host snapshot, and one restore from the off-host target succeeded.
- [ ] **Integrity:** the scheduled `restic check` is green on every repo.
- [ ] **Custody:** a second copy of env-01's age key exists offline and has decrypted one secret. A restic rotation rehearsal on the drill repo succeeded.
- [ ] **Tenancy:**
  - FluxInstance `multitenant: true`; every Kustomization and HelmRelease names a service account (CI check);
  - every non-platform namespace has a PSA enforce label and a default-deny policy (CI check);
  - no generated restic Secret holds the global password;
  - rest-server runs with `--private-repos`.
- [ ] **Landing path:** a sample service with a SOPS secret deploys to env-00's render, decrypted only in its namespace. `docs/adding-a-service.md` exists. verify runs only the enabled components.
- [ ] **Placement:** a fixed role-label prefix; dedicated nodes are tainted; all platform pods and tools tolerate correctly; agents restart one at a time.
- [ ] **Updates:** security unattended upgrades are on. Renovate's dashboard shows k3s, Helm and every `tools/versions.env` pin. `docs/upgrade.md` covers release intake, k3s rolls and component removal.
- [ ] **Docs:**
  - every CONTRACT has the Operations and Limits text listed in section 3, Q16;
  - no plan codes in core (check covers non-Markdown);
  - the placeholders are replaced by `docs/roadmap.md`;
  - `docs/README.md` leads with env-00.
- [ ] The section 8b plan choices are confirmed by Gordon, and the plan updated where he says so.

---

## 8. Decisions for the maintainers

| # | Decision | Options | Recommendation | Cost of each, and of reversing | Effect on next phases |
| --- | --- | --- | --- | --- | --- |
| D1 | Off-host backup target | (a) `restic copy` CronJob to any restic backend (rest, s3, sftp, rclone); (b) a second VolSync source per PVC; (c) document only | **(a)**, landed before the P2 exit | (a) one CronJob and one secret; reversing is deleting it. (b) doubles mover load. (c) leaves J01 critical | P2's Hermes memory and OAuth tokens are the first irreplaceable data. rclone lets a paid cloud drive serve as the target |
| D2 | Seal the k3s token for env-01 | (a) the owner seals env-01's current token; (b) new envs only | **(a)**, before the first agent node joins | (a) one owner step on the node. (b) env-01 stays unrebuildable | GPU nodes join with the agent token (J19) |
| D3 | Age-key custody | (a) a second recipient required by the schema; (b) two copies of one key, tested by decryption (framework §8) | **(b) now**, documented with a test decrypt; (a) optional | (a) blocks single-key envs and needs `updatekeys`. (b) is cheap; reversing to (a) later is a `sops updatekeys` pass | P2–P3 add consent-bound credentials (Google, Telegram, xAI), which raises the cost of loss |
| D4 | When to split backup credentials | (a) now, one policy change (G5b); (b) at the first outside app | **(a), before p2-09** | (a) one migration over about two repos. (b) a migration over every P1–P3 repo, with policy recreations in between | The first PVC (hermes-ops) reads untrusted email |
| D5 | OS patching | (a) security-only unattended upgrades with no auto-reboot, plus a verify warning; (b) a manual runbook | **(a) in wave 1** | (a) rare package surprises; reversing is one template. (b) relies on each adopter | GPU nodes use signed nvidia module meta packages so kernel updates stay matched |
| D6 | App-hosting shape | (a) per-service Kustomizations in core, from a named core source, with the tenancy rule (J11); (b) plus a separate apps repo; (c) apps only via the P4a ResourceSet | **(a) now; (b) and (c) at P4a**, with the impersonated SA from J50 | (a) M now; skipping the named source means rewriting G6b at the repo split | Fixes where P1–P3 services and their secrets land |
| D7 | Pre-consumer config | (a) make it optional (v1); (b) **delete** `hermes.yaml` and `apps.yaml` and their validators, `build_agent`, and the Grafana and Hermes generation for new envs; P2 and P4 re-add them with consumers | **(b)** | (b) about 60 validator lines gone; p2-07 already plans to add the Hermes validation with its render. Reversing is re-adding them with the consumer | The code factory may want `claude-code` and `cursor` adapters (section 8c). A cursor-only enum would have blocked that |
| D8 | Cross-env owner rule | (a) drop; (b) keep plus fail on stale rules | **(a) if D14 = split; else (b)** | Small either way | With a split, each instance has one env |
| D9 | Private repos | (a) an optional pull secret; (b) public only | **(a), optional; keep the instance public by default** | Verified: GitHub Free doesn't offer protected branches or rulesets on private repos (section 8c). A private instance on Free loses the R36 admin-gate enforcement; GitHub Pro restores it | Matters once instance repos hold bot profiles and apps lists |
| D10 | CA name constraints | (a) new envs; (b) also rotate env-01 | **(a), and (b) at the next planned CA rotation** | (b) every client re-trusts the CA | Voice and app clients all sit under the suffix, so there is no cost later |
| D11 | Command guard (`.cursor/cli.json` frozen) | (a) unfreeze for the one Secret-read case category plus the two uncovered categories; (b) keep frozen | **(a)** | (b) leaves a known gap | Unattended lanes (P4b) raise the stakes; each adapter needs its own deny list, checked for parity |
| D12 | Topology profiles | (a) single control plane, amd64; (b) arm64 and HA profiles | **(a)**; confirm the GPU hosts are x86 | (b) per-arch checksums and images everywhere | GPU nodes are agents, so one control plane is fine |
| D13 | Shrink `_cli_permissions.py` | (a) keep; (b) category corpus plus a parity check | **(b)** | Loses detailed matcher cases | P4b needs adapter-neutral categories |
| D14 | **Product repo and instance repo** | (a) monorepo as today; (b) build the seam now (named core source, env-owned secrets Kustomization, J58), and split when the adopter release ships; (c) split now | **(b)** | (b) M now, and the move later is mechanical. (a) every adopter merges Gordon's env changes on each upgrade, and bots conflict with core (R19). (c) delays foundation fixes behind a repo move | Decides where bots, the Hermes render (p2-07 writes `envs/env-01/rendered/`), the apps list and secrets live from P2 on, and how J14 upgrades work (a ref bump) |

## 8b. Plan choices to confirm

Gordon should confirm or change these, so the next audit treats them as decided:
1. **Voice is deferred** to "a separate later plan" (`framework.md:539`), while Gordon's vision puts voice in the chat bot. Foundation impact: J46's trigger (UDP exposure) and G10's GPU runtime.
2. **Hosted apps are stateless** (R30, `framework.md:278`), while area 3 likely includes stateful apps. G5b makes stateful apps safe later.
3. **Code-factory lanes run on GitHub-hosted runners** (R42, `framework.md:308`), not "from Jarvis itself". In-cluster lanes need gVisor, a sandbox role with a taint (J52), an egress proxy and rootless builds, all additive.
4. **Lanes authenticate with a Cursor user API key** (R42), while Gordon wants to use Claude Max. A `claude-code` adapter with `claude setup-token` is permitted (section 8c), which is why D7 drops the cursor-only enum.
5. **Hermes uses xAI subscription logins** (`framework.md:509`). Confirm this isn't the "Grok Bots" dependency Gordon wants to avoid. Claude Max can't back it (section 8c).
6. **Repos are public** (R25), and admin-gate relies on branch protection (R36). That works on Free only while public (section 8c); see D9 and D14.
7. **Backups sit on the node's data disk** (R31, `framework.md:282`). D1 adds an off-host copy, and R31 needs a line for it. R31 also allows `backup: none` only for observability; model weights and build caches need a "rebuildable cache" class.

## 8c. External claims, verified from primary sources

**(a) Claude Max token (`claude setup-token`) for headless code-factory lanes: permitted, with limits. For backing a chat bot: not permitted.**
- Claude Code's authentication docs (`code.claude.com/docs/en/authentication`) say: "For CI pipelines, scripts, or other environments where interactive browser login isn't available, generate a one-year OAuth token with `claude setup-token`… This token authenticates with your Claude subscription and requires a Pro, Max, Ultra, Team, or Enterprise plan. It can only make model requests." Also: "Bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`."
- Legal and compliance (`code.claude.com/docs/en/legal-and-compliance`):
  - OAuth "is designed to support ordinary use of Claude Code and other native Anthropic applications".
  - "Advertised usage limits for Pro, Max, and Ultra plans assume ordinary, individual usage of Claude Code and the Agent SDK".
  - The terms do not prevent "an end user from signing in to the unmodified Claude Code binary with their own Claude subscription".
- The Consumer Terms (`anthropic.com/legal/consumer-terms`, §7) forbid automated access "except… where we otherwise explicitly permit it". The setup-token docs are that explicit permission, **for Claude Code**.
- Verdict: running the unmodified Claude Code CLI headless on Gordon's own Max token for his own lanes is permitted. Unattended parallel lanes may exceed "ordinary, individual usage" limits. Each adopter must use their own token.
- **Chat bot:** the same page says developers "should use API key authentication", and Anthropic does not permit third parties "to route requests through Free, Pro, Max, or Ultra plan credentials on behalf of their users". OAuth is for "Claude Code and other native Anthropic applications", and Hermes is neither. Verdict: not permitted for Hermes. Wrapping `claude -p` as a bot backend is at best a grey area under "ordinary use"; don't build on it.

**(b) GitHub Free enforcing branch protection and rulesets on private repos: no.**
- GitHub's plans page (`docs.github.com/en/get-started/learning-about-github/githubs-plans`) lists "Protected branches" and "Code owners" under GitHub Pro's "Advanced tools and insights in private repositories". GitHub Free personal accounts get private repos "with a limited feature set".
- The rulesets docs (GitHub docs, "About rulesets", the availability line as published for GHES 3.12 and 3.13, the same text on github.com): "Rulesets are available in public repositories with GitHub Free and GitHub Free for organizations, and in public and private repositories with GitHub Pro, GitHub Team, and GitHub Enterprise Cloud."
- I could not fetch the github.com "About protected branches" availability banner, as it was stripped by the fetcher. The plans page is the primary source used.

---

## 9. Rejected findings (after the second pass)

- C F23 (prep-node size): its real bugs (J05, J22) are kept; a rewrite would drop the host-key UI and the clock check.
- C F37 (verify cron evaluator): correct and tested.
- C T08 as a finding: a guardrail-size choice; see D13.
- The C6 delete (the whole Helm CLI path in favour of a k3s HelmChart): it would fight the Flux HelmRelease over one release. J28 suffices.
- The C7 delete (the Renovate regex managers): live. J53 fixes the real gap.
- B F13 and B F14 (one control plane, amd64): limits; see D12.
- B F15 as a separate finding: merged into J25 and section 4.
- B "no defect in `cli.json`": refuted (J21).
- A F10 harm claim: refuted by the k3s docs; J44 kept.
- A "nothing safe to delete" and "cut no tests": refuted (section 6, J40).
- C F14 rejection, C T20 rejection, the C4 and C5 refutations, and C F25: **withdrawn**; now J52, J56, J55 and J54.

---

## 10. Audit quality

| Auditor | Accepted as-is | Downgraded | Rejected | Notes |
| --- | --- | --- | --- | --- |
| A (Grok 4.7), 21 | 15 | 6 | 0 (2 positions refuted) | Best on the adopter path (F3, F4, F5, F9). Inflated F6 and F8. Missed URL credentials, patching, binder placement, CI depth, the agent token, CA scope and the tenancy class |
| B (GPT-5.6 Sol), 25 | 12 | 10 | 3 | Broadest on security and runtime (F20, F23–F25). The best DR framing. Wrong on `cli.json`. Missed the owner rule and the stale ConfigMap |
| C (Claude Opus 5.5), 59 | 41 (v1: 36; F14, F22, F25, F32 and T20 reinstated) | 15 | 3 (F23, F37, T08), plus 2 refused deletes (C6, C7) | Deepest line-level coverage. Raised the namespace baseline (answers to Q22–23) and the agent-restart and boilerplate points (F21 text) without ids, which v1 then dropped. Missed J05. Inflated severities most often |

**Maintainer notes, found or missed:** as in v1. All three found off-host, the etcd restic-only path, rotation and per-namespace credentials. None found "verify trusting the CA", "backup scale" or the listed CI weak spots.

**Missed by all three (judge or reviewers):** the node-disk contradiction (judge); the lossy age recovery (judge); the drill race message (judge); Flux cluster-admin (R, F); the mover egress conflict with plan R13 (F); unvalidated VolSync specs in kubeconform (judge, v2).

### 10b. The two second-pass reviews: where I agree and disagree

**rejections-review.md: 36 holds, 14 partial, 7 reverse.** I accept all 7 reversals, after checking each at `file:line`, and accept the 14 partials as folded in above, with these differences:
- **D2 count:** it says "six of the nine per-env Flux files are byte-identical". `envs/env-00/flux` has **8** files and `git diff --no-index --stat` shows 3 differ, so 5 are identical. The conclusion stands.
- **D4 count:** it says 23 `prune: disabled` objects; `grep -rc` sums to **24**. Immaterial.
- **C1 and C2, delete versus optional:** I agree to delete. The stronger evidence is the plan's own p2-07 ("This step adds validation of the hermes.* keys it renders"). The review's "no consumer" grep alone would not settle it, since P2 is the very next phase.
- **E3 / J09:** correct. v1 proposed a verify check that already exists at `tools/verify/run:331-345`. Removed.
- **C F32:** agreed. The concrete harm is the J32 apt failure, not duplicate libraries.

**forward-readiness.md: 7 fix-now items, D14, and D1, D4, D8 and D9 changes.** I accept items 2–7 and the decision changes, with these differences:
- **Item 1, "movers unprivileged by default":** I disagree with making that the default now. local-path PVs are hostPath-backed, and hostPath ignores fsGroup (the repo says so at `rest-server.yaml:54`), so an unprivileged mover may fail to read files an app writes as another UID. The fields exist (verified in the upstream v0.17.0 CRD: `moverSecurityContext`, `moverTolerations`, `moverAffinity`), but the safe default is privileged movers by opt-out label, with unprivileged required only where a namespace enforces `restricted`. That needs a bench test per app. The rest of item 1 (one recreation, per-namespace credentials, tolerations, the egress line) I accept.
- **Item 5, lockdown:** I accept it. The Flux Operator docs confirm `multitenant` defaults to `false` and that `tenantDefaultServiceAccount` (default `default`) applies to kustomize-controller and helm-controller when it is on. The docs don't say how `flux-system`'s own objects behave without an SA, so the live check is required, as F says.
- **Item 7:** it says "eight later plan tasks extend `tools/verify/`". I count 15 mentions of `tools/verify` in `plan.md`, across more than 8 tasks. The conclusion stands.
- **B13 and B14 (Claude Max):** confirmed from primary sources (section 8c), with one addition F omits: the usage-limit caveat ("ordinary, individual usage") for unattended parallel lanes.
- **D9:** F cites framework §9 for "free GitHub doesn't enforce branch protection on private repos". I verified it on GitHub's plans and rulesets docs (section 8c).
- **B3 ("k3s data dir change needs a node reinstall"):** plausible but unverified here; I kept it as a decision point in J63.

---

## 11. Concerns and questions

- **Source of truth.** The standing user rule names Gitea for "jarvis-cluster". This repo's remote and its merged PRs are GitHub `gordoncooper/jarvis`, so the report is on GitHub. Please confirm.
- **J12 and J43 are closed** by the live checks (`WaitForFirstConsumer` confirmed; the provenance error is benign drill noise).
- **Live checks still needed** (each gates its PR, not this report): G6a lockdown reconcile; G2a token sealing and rebuild; G5b repo migration; G2b relabel; G5a first off-host copy; G4 check mode.
- **The plan isn't in this repo.** I read project-plans main `771158d`. A later unmerged plan PR may differ.
- **The brief's branch name** says "audit/judge"; the existing PR #147 branch is `audit/wrapup-judge`. I updated that branch, as instructed ("your existing branch").
