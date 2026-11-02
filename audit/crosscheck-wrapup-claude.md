# Wrap-up cross-check (Claude Opus 5.5)

This file cross-checks `findings-wrapup-gpt-5-6-sol.md` (GPT) and `findings-wrapup-grok-4-7.md` (Grok) against SHA `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1`. Ids prefixed `G-` are GPT's, `K-` are Grok's, and `C-` are mine (from `audit/findings-wrapup-claude.md`).

**How the evidence was produced.** All evidence below is reproduced by me in this round, in the env-00-only scratch clone `../wrapup-claude-env00`. Experiments live in `audit-scratch/test_crosscheck.py` and `audit-scratch/test_seed_env.py` there, and were run with `pytest -q -s`. The headless policy allows only `pytest`, `kustomize` and read-only git/grep. There was no live cluster.

**Experiment output.** Each line is quoted verbatim from `pytest -q -s …/audit-scratch/test_crosscheck.py` and `…/test_seed_env.py`:

```
URL userinfo: errors=[] owner=owner
URL query-token: errors=[] owner=owner
URL fragment: errors=[] owner=owner
URL port: errors=[] owner=owner
no_sops_creation_rule: REJECTED
    .sops.yaml: expected exactly one creation rule for envs/home/secrets/, found 0
two_envs_two_owners: REJECTED
    repo owner handle must be the same across non-example envs: home=adopter, lab=other
cursor guard secret-read verb=get case=lower: allowed=False
cursor guard secret-read verb=get case=upper: allowed=False
cursor guard secret-read verb=get case=title: allowed=False
cursor guard secret-read verb=get case=inner-upper-1..5: allowed=True   (all five)
cursor guard secret-read verb=describe case=inner-upper-1..5: allowed=True (all five)
flux_url_not_edited: ACCEPTED
stale_configmap_after_value_change: ACCEPTED
bad_dns_suffix: ACCEPTED
```

Two more results, run directly:
- `kustomize build audit-scratch/empty-kust` (a kustomization with `resources: []`) exits 0 with empty output.
- The clean env-00-only run in round 1 was `483 passed in 304.84s`.

---

## 1. Verdicts on GPT's findings

| GPT id | Verdict | Evidence I reproduced | Agreement with severity, size and fix; overlaps |
| --- | --- | --- | --- |
| G-F01 drill passes without disk-loss recovery | **confirmed** | `platform/backups/CONTRACT.md:183` "restoring onto a rebuilt node also needs the original k3s server token. Same-node restore is the scope." There is no token copy anywhere. `etcd-snapshots.yaml:80` backs up only `/snapshots`. `tools/drill` has no `token` or `cluster-reset` (Grep). | Same issue as K-F1 and C-F03/C-F04. I accept **critical**: a passing drill that implies recovery fits the brief's "report a false recovery". I raise C-F04 to critical (section 2). |
| G-F02 prep-node mutates the host before full validation | **confirmed** | `tools/prep-node:964-983` do the SSH, sudo, hostname, package, sshd and env edits; `run_env_schema` runs only at `:986`. Experiment `no_sops_creation_rule: REJECTED`. That is exactly the state after deploy.md step 2 and before step 3 (`docs/deploy.md:36-37`), so following the doc in order always ends with prep-node exiting 1 after changing the host. | Same as K-F3. Agree **high**, size S. I missed this. |
| G-F03 restic rotation strands repositories | **confirmed** | `envs/env-01/secrets/README.md:25` "To rotate `backups-restic`, delete that file and run the command again"; `tools/bootstrap-secrets:261-269` makes a fresh `RESTIC_PASSWORD`. | Same as K-F2 and C-F02. Agree **high**. I lower mine from critical, because the old ciphertext stays in Git history and decrypts with the same age key. |
| G-F04 shared mutable backup credentials | **confirmed** | `enrollment.yaml:47-53` copies `RESTIC_PASSWORD` and `RESTIC_REST_*` into every `restic-<pvc>`. `rest-server.yaml:2-3` "No --append-only … No --private-repos". `rest-server.yaml:55-56` `runAsUser: 0`. | Same as C-F16. GPT's **high** is fair once untrusted tenants (outside apps, code factory) land; today, with one trusted owner, it is medium. Fix: per-namespace htpasswd users with `--private-repos`, prune centralised. |
| G-F05 `apps_git_repo_url` accepted but ignored | **confirmed** | A Grep for `apps_git_repo_url` finds only `schema/`, tests, `_core_names.py:132` and env values. `envs/env-00/flux/kustomizations/apps.yaml:9` is `path: ./apps` with `sourceRef: GitRepository/flux-system`. | Part of C-F27/C-F08; also K-F15. Severity: high as a readiness gap, but the fix is a decision (remove the key, or wire a second source), size S–M. |
| G-F06 GPU role is not usable capacity | **confirmed** | `assert-gpu.yml:23-31` checks only for vendor `0x10de`. A Grep for `nvidia\|RuntimeClass\|device-plugin` finds nothing outside the assert and tests. | Same as K-F14 and C-F13. Agree high-as-gap, L. |
| G-F07 no adopter upgrade/release process | **confirmed** | `docs/deploy.md:112-114` is the whole updates section. `renovate.json:7-10` has the k3s rule `"enabled": false`. | Same as K-F13 and C-F21. Agree medium. |
| G-F08 local verification not portable | **partly confirmed** | The base-os handler uses `ansible.builtin.service` (`roles/base-os/handlers/main.yml:18-24`). The test stubs only `systemctl` on PATH (`ci/tests/test_base_os.py:238-259`), so the `service` module's own manager detection decides the result. On this controller, which runs systemd, `bash ci/ci-local-verify` gave 483/483 and the env-00-only clone gave 483/483. GPT and Grok both saw `1 failed` on non-systemd VMs. | **Confirmed as environment-dependent, not as always failing.** Low–medium. Fix: switch the handlers to `ansible.builtin.systemd_service`, which the k3s role already uses, so the PATH stub is authoritative. The "no local env-00-only leg" part matches my T15. |
| G-F09 new-env path gaps | **confirmed** | `docs/deploy.md:36` "Run `tools/prep-node` for the first host"; `:77` "that env's `secrets/README.md`" (env-00 has no `secrets/`). The env-shape fixtures are not linked from the docs. | Overlaps K-F8, C-F29 and C-F11. Agree medium. |
| G-F10 future config and secrets before consumers | **confirmed** | `schema/env.py:209-216` requires `hermes.yaml`; `tools/bootstrap-secrets:297-330` has five unconsumed specs; `services/placeholder.yaml`, `apps/placeholder.yaml`. Seed case `no_hermes_yaml: REJECTED`. | Same as C-F27 and K-F19. Agree medium. |
| G-F11 Kyverno outside its tested Kubernetes range | **confirmed (as documented risk)** | `platform/kyverno/CONTRACT.md:24` "Upstream tests v1.19 on Kubernetes 1.33–1.35. … The chart does not refuse 1.36." The pin is `v1.36.5+k3s1`. | I read this and didn't flag it, which was a miss. Medium is fair. The fix needs either a live test or a pin choice (section 4). |
| G-F12 enrollment fails open | **confirmed** | `enrollment.yaml:11-12` `failurePolicy: Ignore`. | Same as K-F11 (C covered it only in 4a). Medium. Nuance: a PVC that binds after Kyverno recovers is still enrolled by the bind UPDATE (preconditions on `volumeName`), so the gap is "created and bound while Kyverno is down". verify catches it on the next run. |
| G-F13 single control plane | **confirmed (documented limit)** | `README.md:12`; `preflight.yml:3-6`. | Disagree on severity: it is a deliberate limit (README), so **low**. It belongs in Limits, as GPT's own fix also suggests. |
| G-F14 amd64-only | **confirmed (documented limit)** | `README.md:11`; `preflight.yml:25-28`. | **Low**, same reasoning. Dead arm64 branches exist (C-F30). |
| G-F15 diagnosis is manual | **confirmed** | `tools/verify/run:421-445` is a five-check list; Gatus is "planned" (`platform/traefik/CONTRACT.md:13`). | Same as C-F26. Medium. |
| G-F16 age-key rotation and offline backup missing | **confirmed** | `.sops.yaml:2-4` has one recipient; all 7 env-01 files have one `recipient:` (Grep). `infra/ansible/README.md:76` "If both are lost, the old secret values are gone." | Same as C-F01. GPT says medium; I said critical. **I now say high**: losing everything needs two losses (the key file and the cluster `sops-age`), but the remedy is cheap and the impact is total. |
| G-F23 existing Helm binary trusted | **confirmed** | `roles/flux-operator/tasks/helm_cli.yml:18-24` installs only `when: not flux_operator_helm_stat.stat.exists`. | Same as C-F24. Medium; the preferred fix removes the Helm CLI. |
| G-F24 kubectl calls have no timeout | **confirmed** | `tools/lib/_env.py:44-51` `subprocess.run([...kubectl...], check=False)` with no `timeout`; a Grep for `timeout\|request-timeout` in `_env.py` finds 0. `restore`, `drill` and `verify` all route kubectl through it. | Missed by me. Medium, S (one `--request-timeout` plus `timeout=`). |
| G-F25 verify fetches the full backup Secret | **confirmed** | `tools/verify/run:155-172` runs `get secret backups-restic -o json` and checks only `data` is non-empty. | **Low**: the operator already holds a cluster-admin kubeconfig and the payload isn't printed. Easy fix: `-o jsonpath='{.data}'` keys only, or `kubectl get secret … -o name`. |
| G-F19 controller prerequisites | **confirmed** (all four parts) | (1) A Grep for `apt-get update\|update_cache` in `tools/install-deps` finds 0. (2) `.venv/…/ansible_core-2.21.6.dist-info/METADATA:28` `Requires-Python: >=3.12`, with no controller Python minimum in the docs. (3) `tools/prep-node:36` `_TOOLS = (…, "make")`, and no prep-node subprocess runs `make`. (4) `age-keygen` is not in `_TOOLS`; it is invoked at `:763` and `:786` after the host work, and a missing binary raises an uncaught `FileNotFoundError` (no `try` around `subprocess.run`), i.e. a traceback, not a `PrepError`. | Overlaps K-F21. Medium. Item 4 is slightly worse than GPT says (traceback). |
| G-F20 repo URL accepts credentials | **confirmed** | Experiment: userinfo, `?access_token=`, fragment and port all give `errors=[]`. Code `schema/env.py:542-552` checks only scheme and netloc. | Missed by me. Agree **high** given the public-repo design (C-F09): a password in userinfo is not a gitleaks-recognisable token. S. |
| G-F21 Flux advances before VolSync is Ready | **partly confirmed** | `envs/env-00/flux/kustomizations/platform-controllers.yaml:15-23` `healthChecks` list only `cert-manager` and `kyverno`; `ci/tests/ansible_role_proof.py:105` "waits for these two HelmReleases and no other object"; `platform/kustomization.yaml:7` includes `backups/controller`. | The gate omission is real; I missed it. The impact is overstated: on first install no workload PVC exists before `services`. The realistic effect is a `platform-configs` reconcile racing the VolSync CRDs, so it retries. **Medium**, S: add the `volsync` HelmRelease to `healthChecks`. Whether Kyverno rejects the policy while `ReplicationSource` is unmapped is not settled offline (section 5). |
| G-F22 Kyverno cluster-wide Secret RBAC; privileged movers everywhere | **confirmed** | `platform/kyverno/rbac.yaml:12-15` grants `secrets` all verbs with no `resourceNames`; `enrollment.yaml:96-108` mutates every PVC namespace. | Same as K-F16 and C-F16. Medium (high once tenants exist). RBAC can't be scoped by name for `create` or by name prefix, so narrowing must come from fewer enrolling namespaces (opt-in label), not from RBAC. |
| G-F17 tests that protect shape | **partly confirmed** | Confirmed: `test_env_shapes.py:40-54` only exercise `_assert_shape` (same as my T19); the parser and tag tests in `test_flux_operator.py` (my T19); `test_base_os.py:144-190` mines YAML. **Not a duplicate:** `test_flux_entry.py:475-508` plants an age key in a fixture repo and checks gitleaks catches it. `ci/checks/secret-scan` scans the real tree, which has no planted key, so the two don't overlap. That test checks gitleaks' default rules, not repo code: low value, but not "re-running a check". | Low. |
| G-F18 contracts need Operations/Limits | **confirmed** | Only `platform/backups/CONTRACT.md` (Operations and Limits) and `platform/cert-manager/CONTRACT.md` (Operations) have optional sections (Grep `^## Operations\|^## Limits`). | Same as K-F18 and C-4a. Low–medium. |

**GPT delete list**

| Item | Verdict | Evidence |
| --- | --- | --- |
| `apps/placeholder.yaml`, `services/placeholder.yaml` | **confirmed** | `kustomize build` of `resources: []` exits 0 with empty output. Flux applying an empty build was not run live (section 5). |
| Grafana/Hermes bootstrap specs | **confirmed** | No Flux path lists them (env-01 `platform-configs/kustomization.yaml:3-6`). |
| Mandatory `hermes.yaml`/`apps.yaml` validation | **confirmed** | Seed `no_hermes_yaml: REJECTED`; no consumer. |
| Planned detail in `docs/code-delivery/CONTRACT.md` | **confirmed** | `:15-18` rows are "Planned"; `:54` "No core manifest reads `code_delivery`". |
| `test_flux_entry.py:475-508` | **partly confirmed** | Not a duplicate (see G-F17). Delete it or move it as a fixture into the secret-scan check's own tests; either is fine. |
| `test_env_shapes.py:40-54` | **confirmed** | They test only the helper. |
| Shape assertions in `test_base_os.py:144-190`, `test_flux_operator.py:487-552` | **confirmed** | They match my T19 and the round-1 text-only list. |
| Keep the VolumeSnapshot CRD | **agree** | `volumesnapshot-crd.yaml:2` "The operator Owns this kind and exits if it is unmapped." |

## 2. Verdicts on Grok's findings

| Grok id | Verdict | Evidence I reproduced | Agreement with severity, size and fix; overlaps |
| --- | --- | --- | --- |
| K-F1 dead disk not recoverable | **confirmed, plus one nuance I missed** | Same as G-F01 and C-F03/C-F04. Grok adds that with `data_disk: true`, `backup_path` must equal or sit under `/var/lib/jarvis` (`roles/node-disk/tasks/main.yml:80-82`), and that a separate mount at `backup_path` is refused (`:120-135`: `findmnt -T backup_path` must equal `node_disk_mount`). So "unless `backup_path` is on another disk" (`docs/deploy.md:110`) is impossible on a `data_disk` host. | Agree critical (as G-F01). The fix must also relax node-disk to allow a second mount for `backup_path`. |
| K-F2 restic rotation | **confirmed** | as G-F03 | high |
| K-F3 prep-node order fails the schema | **confirmed** | Experiment `no_sops_creation_rule: REJECTED`; `tools/prep-node:894-896` prints, and does not write, a missing rule. | Same as G-F02. high, S. |
| K-F4 second env must share env-01's owner | **confirmed** | Experiment `two_envs_two_owners: REJECTED … home=adopter, lab=other`; `schema/env.py:535-539`. | I had the rule in C-F27 but missed the adopter consequence: a fork that keeps `env-01` can't add its own env. **High** for adopters, S. Fix: delete the rule (my preference), or document "delete env-01 first". |
| K-F5 FluxInstance url unchecked | **confirmed** | Seed `flux_url_not_edited: ACCEPTED`. | Same as C-F11. Grok says high, I said medium. **Grok is closer**: the result is a cluster that never reconciles, discovered only after `site.yml`. I raise C-F11 to high. |
| K-F6 key-mismatch prompt deletes the env's secrets | **confirmed (code)** | `tools/prep-node:897-908`: warns "may belong to another env", then `confirm(...)` → `delete_env_secrets`. | Agree it's real. Severity **medium**, not high: the prompt defaults to No (`confirm` default `False`), and deleted ciphertext is still in Git history (decryptable with the right key). I mentioned this path inside C-F02/C-F23 without a finding of its own. |
| K-F7 Secret-read guard misses some case spellings | **confirmed (in-repo matcher)** | The experiment loads `.cursor/cli.json` into `_cli_permissions.command_allowed`. All-lower, all-upper and title-case Secret reads are denied; every spelling with an uppercase letter inside the noun is allowed, for both `get` and `describe`. Category: *case variation inside the resource noun*. No string is recorded. `.claude/settings.json:25-28` holds the same two spellings, so it is likely affected too, but its matcher semantics are not in the repo. | GPT said "none was found"; **Grok is right**, and my C-F36 missed it. Severity **medium** (build-agent guard, not the product; the real boundary is "no kubeconfig for agents"). Exploitability assumes kubectl accepts mixed-case resource names (section 5). |
| K-F8 secrets README not in the template | **confirmed** | `docs/deploy.md:77`; env-00 has no `secrets/`; `docs/README.md:33-34` links only env-01. | Same as C-F29 and G-F09. Grok says high; I'd say **medium** (a doc dead end, though it carries the key-path rule). |
| K-F9 stale ConfigMap passes the schema | **confirmed** | Seed `stale_configmap_after_value_change: ACCEPTED`. | Same as C-F11's second half. Medium. |
| K-F10 address-change `cluster-reset` drops the datastore | **refuted (harm), partly confirmed (doc split)** | `infra/ansible/README.md:72` uses `--cluster-reset` without `--cluster-reset-restore-path`. In k3s, a bare `--cluster-reset` makes the node the sole etcd member **while keeping its data**, which is the documented fix for a changed address. It does not drop the datastore. This is k3s behaviour, not provable from repo code (section 5). | The README is correct for its case. A cross-link to the restore form is harmless. Low. |
| K-F11 enrollment fails open | **confirmed** | as G-F12 | medium |
| K-F12 verify checks timestamps, not restorability | **confirmed (fact), low** | `tools/verify/run:348-351,373-376`. verify is read-only by contract; the drill is the integrity proof. | **Low.** A periodic `restic check --read-data-subset` CronJob would be the right home, not verify. |
| K-F13 no upgrade procedure | **confirmed** | as G-F07 | medium |
| K-F14 single-node/GPU not a documented setup | **confirmed** | as G-F06. Fixtures `ci/tests/fixtures/env-shapes/single-node/hosts.yml` are not linked from `docs/deploy.md`. | medium |
| K-F15 no decrypting path for services | **confirmed** | `services.yaml` and `apps.yaml` have no `decryption:`. | Same as C-F07. I rate it high (it blocks every consumer); Grok says medium. |
| K-F16 Kyverno role can delete every Secret | **confirmed (fact); fix partly wrong** | `rbac.yaml:12-15`. | Grok's fix "scope to generated secret names / drop delete" won't work as written. RBAC `resourceNames` can't express `restic-*` and doesn't apply to `create`, and `synchronize: true` generate needs `delete` to remove secrets when a PVC goes. Medium. Narrow by opting namespaces in instead. |
| K-F17 `backup: none` only under trees that don't exist | **confirmed** | `ci/checks/lib/_pvc_backup.py:18-21` allows only `services/observability/victoria-{metrics,logs}/`, and neither directory exists. | Missed by me as a separate item. Medium for the first cache-like volume. Fix: allow `backup: none` anywhere with a required reason annotation, or a short allow-list in config. |
| K-F18 contracts lack Operations/Limits | **confirmed** | as G-F18 | low–medium |
| K-F19 bootstrap writes unconsumed secrets | **confirmed; disagree on fix** | as G-F10 | Grok says keep them behind a flag; I and GPT say delete. **Delete** is right: they are random tokens with no state, regenerable in the PR that adds the consumer. Keeping them costs every adopter five unexplained secrets. |
| K-F20 plan codes in placeholders; doc index leads with the owner env | **confirmed, but undercounted** | Grok cites 2 files. A Grep with the check's own regex over non-Markdown core files finds 49 hits in 41 files (C-F28). `docs/README.md:10-11,33-34`. | low |
| K-F21 apt install without update | **confirmed** | as G-F19(1) | low |

**Grok delete list**

| Item | Verdict | Evidence |
| --- | --- | --- |
| "Nothing on disk is safe to delete; deleting the placeholders breaks the Flux paths" | **refuted at the kustomize level** | `resources: []` builds with exit 0. Flux reconciling an empty build is expected to succeed but was not run live. |
| "Keep the five secret specs" | **disagree** | See K-F19. |
| "`EXPLICIT_COMPONENT_ROOTS` entries are a deferred index, not dead" | **disagree** | `_contract_md.py:32-36` silently skips a listed root that doesn't exist, so the list enforces nothing until the directory appears. It is a planning note in code. Track it in issues or a roadmap. |
| `.claude/settings.json` "not reviewed" | **note** | It has no CI check (round-1 T08). K-F7's category likely applies to it too. |

**Where GPT and Grok contradict each other:**
- `.cursor/cli.json` defect: Grok is right (experiment above).
- Placeholders deletable: GPT is right.
- Tests to cut: GPT is right. Grok's "none" is contradicted by the round-1 seeded-break results (T07, T19).
- Unused secrets: GPT is right (delete).
- Restic rotation severity: both say high; I now agree.

## 3. My own file, revisited

**Confirmed by another file:**

| My id | Confirmed by |
| --- | --- |
| C-F01 | G-F16 |
| C-F02 | G-F03, K-F2 |
| C-F03, C-F04 | G-F01, K-F1 |
| C-F07 | K-F15 |
| C-F08 | G-F05 (part) |
| C-F11 | K-F5, K-F9 |
| C-F13 | G-F06, K-F14 |
| C-F16 | G-F04, G-F22, K-F16 |
| C-F21 | G-F07, K-F13 |
| C-F24 | G-F23 |
| C-F26 | G-F15 |
| C-F27 | G-F10, K-F19, K-F17 (providers part), K-F4 (owner rule) |
| C-F28 | K-F20 (narrower) |
| C-F29 | G-F09, K-F8 |
| C-4a | G-F18, K-F18 |
| C-T19 | G-F17 |

**Severity changes I now make:**
- C-F04 (no token; drill implies recovery): high → **critical**, following G-F01 and the brief's "false recovery" definition.
- C-F01 (age key, single point of failure): critical → **high**. It needs two independent losses; the impact is still total.
- C-F02 (restic rotation): critical → **high**. Git history keeps the old ciphertext.
- C-F11 (unchecked Flux url/ref): medium → **high**, following K-F5. The failure surfaces only after `site.yml`.
- C-F36 (command guards): it remains a category note, but it **missed** a concrete in-category defect (K-F7). My earlier claim implied the direct-kubectl coverage was sound, and it isn't for case variants.

**Contradicted:**
- C-T15 says the suite passes in a fresh env-00-only copy. That holds on a systemd controller (483/483 here), but G-F08 and Grok's concern show 482/483 on non-systemd controllers. Both are right; the test is host-dependent (fix in section 4, P13).

**Missed by me, now confirmed:**
- G-F02/K-F3: prep-node mutates the host before validating.
- G-F20: credential-bearing URLs pass validation.
- G-F21: VolSync is not in the health gate.
- G-F24: kubectl calls have no timeout.
- G-F25: verify fetches the full backup Secret.
- G-F19: Python ≥3.12, apt update, the unused `make` requirement, and age-keygen not preflighted.
- G-F11: Kyverno version skew.
- G-F12/K-F11: fail-open enrollment, which I only had in 4a.
- K-F4: the env-01 owner lock-in.
- K-F6: the delete prompt.
- K-F7: the case-variant guard gap.
- K-F17: `backup: none` prefixes.
- K-F1's node-disk nuance.

**Raised by me only, and not contradicted:**
- Fresh-install verify and drill fail (C-F06).
- Restore binder placement (C-F05).
- Public repo required (C-F09).
- OS patching off (C-F10).
- `ssh_user` overwrite (C-F12).
- DNS-suffix label prefix (C-F14).
- Whole age key file uploaded (C-F15).
- CA without name constraints (C-F17).
- Agents get the server token (C-F18).
- LoadBalancer allow-list (C-F19).
- NTS-only (C-F20).
- Renovate (C-F22).
- prep-node size (C-F23).
- No site target (C-F25).
- F30–F39.
- T01–T20.

Neither other auditor seeded breaks, so T01–T20 are unopposed, not independently confirmed.

**Findings of mine I now think are wrong:** none outright. C-F22's claim that Renovate's native `flux` manager covers `chartRef` → OCIRepository is still unverified; I keep its confidence at medium.

## 4. Answers by question: where the three differ

| Q | Who is closer, and why |
| --- | --- |
| 2 | GPT says "most glue is justified" and Grok says "keep custom tools". Both underrate the size of prep-node and the Renovate regex managers (C-F22, C-F23). All three agree the duplicated Flux url and ConfigMap should be generated or checked. **Mine is closer**, with GPT's caveat that the AppArmor-aware chrony validate is good glue. |
| 3, 7 | All three agree on the speculative config. On deletion, GPT and I are right and Grok's "nothing safe to delete" is refuted (empty kustomize build). |
| 5 | GPT treats one control plane and amd64 as findings; Grok and I treat them as documented limits. **Limits** is right (README states them). The real "too strict" items are K-F4 (owner lock-in), K-F17, C-F09/C-F19/C-F20, and C-F13's host-key lock. |
| 9, 10 | All found the env-00 secrets README gap and the url copy. Grok and GPT found the decisive one I missed: deploy.md's step order guarantees prep-node exits 1 after mutating the host (G-F02/K-F3). **They are closer.** |
| 11 | All agree: shapes render; GPUs are not usable. Mine adds restore placement on multi-node (C-F05) and the `ssh_user` overwrite (C-F12). The answers are complementary. |
| 13 | All agree disk loss is unrecoverable. GPT's framing ("the drill can pass": critical) is the most precise. Grok's K-F10 harm claim is refuted. |
| 15 | All agree: restic rotation is unsafe and age rotation/offline backup is missing. Severity converges on high. |
| 16 | GPT adds kubectl hangs (G-F24); I add the missing node and pod health checks (C-F26). The answers are complementary. |
| 17 | GPT adds URL credentials (G-F20); Grok adds the guard case gap (K-F7). GPT's "no defect in cli.json" is **wrong**. I add the CA name constraints, the agent token and the whole age file (C-F15/F17/F18). Taken together, the union is the answer. |
| 19 | Grok's "none to cut" is contradicted by the seeded-break evidence (T07, T19). GPT and I are closer. |
| 20 | The three runs differ by controller. It is 483/483 on systemd (mine) and 482/483 on non-systemd (GPT, Grok). One host-dependent test (G-F08) explains it. |
| 21–23 | All agree there is no secrets path and no per-env enablement. GPT adds VolSync gating (G-F21). Grok rates the secrets path medium; I keep high because every consumer needs it. |
| 24 | Grok wants to keep the placeholders, contract roots and secret specs as "deferred markers". GPT and I want them moved to tracked work. **GPT and I are closer**: deferred items belong in a roadmap or issues, not in validated code. |

Questions 1, 4, 6, 8, 12, 14 and 18 differ only by which items each auditor found; the union is the answer.

## 5. Fixes (PR-sized, covering every confirmed finding)

Each package lists its findings, approach, files, tests and done check. "C-" ids are mine, including the T-ids.

**P1 — Age key resilience and rotation.** C-F01, G-F16, C-F15, C-F34, K-F6.
- **Approach:** require a second, offline age recipient per env in `.sops.yaml`. Add a documented `sops updatekeys` rotation. The flux-operator role uploads only the identity whose public key matches the env recipient, and defaults `age_key_file` to `~/.config/jarvis/<env>/age.key`. bootstrap-secrets resolves the key path like site.yml does. prep-node's delete-secrets prompt requires typing the env name.
- **Files:** `schema/env.py` (`_validate_sops_creation_rules` requires ≥2 age recipients), `infra/ansible/roles/flux-operator/tasks/sops_age.yml`, `templates/sops-age-secret.yaml.j2`, `tools/bootstrap-secrets`, `tools/prep-node`, `infra/ansible/README.md`, a new Operations section in `platform/flux-operator/CONTRACT.md`.
- **Tests:** schema rejects a single-recipient rule; the stubbed flux-operator play receives only the matching identity (capture `kubectl create` stdin); a prep-node test where a "no" or a wrong env name keeps the files.
- **Done check:** `pytest ci/tests/test_env_schema.py ci/tests/test_flux_operator.py ci/tests/test_prep_node.py` green, plus a seeded single-recipient `.sops.yaml` failing `ci/checks/env-schema`.

**P2 — Safe restic credential rotation.** C-F02, G-F03, K-F2.
- **Approach:** delete the env-01 sentence. bootstrap-secrets refuses to regenerate `backups-restic` unless a `--new-restic-repositories` flag is given. Add a backups Operations section: rotate the rest-server password via htpasswd; change the repository password only via a `restic key add`/`key remove` Job per repository.
- **Files:** `envs/env-01/secrets/README.md`, `tools/bootstrap-secrets`, `platform/backups/CONTRACT.md`.
- **Tests:** in `test_bootstrap_secrets.py`, a missing `backups-restic` file next to an existing `cert-manager-ca` refuses without the flag.
- **Done check:** that test, plus a Grep that the "delete that file and run" text is gone.

**P3 — Disaster-recovery boundary.** C-F03, C-F04, G-F01, K-F1, K-F12.
- **Approach:** seal the k3s server token at first install, read once by site.yml into `envs/<env>/secrets/k3s-token.sops.yaml`; or include `/var/lib/rancher/k3s/server/token` in the etcd-copy backup. Add an optional off-host restic copy CronJob (env config: target URL and SOPS credentials) with a verify freshness check. Relax node-disk so `backup_path` may be its own mount. Optionally add a weekly `restic check` CronJob. Rename the drill's etcd line "etcd copy integrity" until a reset drill exists.
- **Files:** `infra/ansible/roles/k3s/…` or `flux-operator` (token read), `platform/backups/etcd-snapshots.yaml`, new `platform/backups/offsite-copy.yaml`, `schema/env.py` (optional `backup_offsite_*` keys), `infra/ansible/roles/node-disk/tasks/main.yml:120-135`, `tools/verify/run`, `tools/drill`, `platform/backups/CONTRACT.md`, `docs/deploy.md:110`.
- **Tests:** node-disk accepts a separate mount at `backup_path` (fake `findmnt`); schema accepts and validates the offsite keys; verify fails a stale offsite copy (fake kubectl); the drill output names "copy integrity".
- **Done check:** `pytest ci/tests/test_node_disk.py ci/tests/test_verify.py ci/tests/test_drill.py ci/tests/test_env_schema.py`.

**P4 — Restore binder placement.** C-F05.
- **Approach:** in `tools/restore`, `_binder_body` copies `nodeSelector`, `affinity` and `tolerations` from the owning Deployment, StatefulSet or CronJob found in `_quiesce`.
- **Tests:** in `test_restore.py`, an owner with a nodeSelector leads to a binder with the same selector.
- **Done check:** `pytest ci/tests/test_restore.py`.

**P5 — Service and app landing zone.** C-F07, C-F08, K-F15, G-F05, G-F10, K-F19, C-F27, C-F30 (placeholders).
- **Approach:**
  - Add per-env `envs/<env>/flux/services/kustomization.yaml` and `…/apps/kustomization.yaml`, which list enabled core directories and that env's sealed secrets.
  - The `services` and `apps` Flux Kustomizations point there and get `decryption: sops-age`.
  - Each service owns its namespace manifest.
  - Remove `apps_git_repo_url` (or wire a second GitRepository when apps move out).
  - Remove `hermes.yaml`, `apps.yaml`, `build_agent`, `code_delivery`, `llm_provider_host`, the five unconsumed secret specs, `_hash_password` and the placeholders (`resources: []`).
  - Add `services/CONTRACT.md` ("adding a service").
- **Files:** `envs/env-00/flux/**`, `envs/env-01/flux/**`, `schema/env.py`, `schema/CONTRACT.md`, `tools/bootstrap-secrets`, `services/`, `apps/`, `docs/code-delivery/CONTRACT.md`, `ci/tests/test_flux_entry.py`, `test_env_schema.py`, `test_bootstrap_secrets.py`.
- **Tests:** flux-entry builds the new per-env paths with kubeconform; a sealed fixture secret in a service namespace renders on the decrypting path; schema rejects the removed keys.
- **Done check:** `bash ci/ci-local-verify`, then the env-00-only pytest run.

**P6 — Env validation before any host change.** G-F02, K-F3, C-F11, K-F5, K-F9, C-F35, G-F20, K-F4, C-F12, G-F19.
- **Approach:**
  - env-schema compares the FluxInstance `spec.sync` with cluster-vars and fails on a stale generated ConfigMap (call `render_document`).
  - It validates `dns_suffix` as lowercase RFC 1123 with a dot.
  - It rejects URL userinfo, query, fragment and port.
  - It drops the cross-env owner rule.
- prep-node:
  - Runs the schema before SSH, tolerating only "no creation rule yet", and writes the creation rule when it creates or reads the key.
  - Defaults the Ansible user to the env's existing `ssh_user`.
  - Preflights `age-keygen` and drops the `make` requirement.
- install-deps runs `apt-get update` once before install and states Python ≥3.12.
- **Files:** `schema/env.py`, `tools/prep-node`, `tools/install-deps`, `docs/deploy.md`, `README.md`.
- **Tests:** the scratch cases `flux_url_not_edited`, `stale_configmap_after_value_change`, `bad_dns_suffix`, the four URL forms and `two_envs_two_owners` become pytest cases with the flipped expectation (rejected, rejected, rejected, rejected, accepted); a prep-node test asserts no SSH happens on a schema error; a prep-node test keeps an existing `ssh_user`.
- **Done check:** `pytest ci/tests/test_env_schema.py ci/tests/test_prep_node.py ci/tests/test_install_deps.py`.

**P7 — Fresh-install signal.** C-F06, C-F31.
- **Approach:** site.yml's last play runs `k3s etcd-snapshot save` once and creates a Job from the `etcd-snapshots` CronJob once Flux has applied it. verify reports `PENDING first run` (not FAIL) for objects younger than one interval. The drill compares against the newest file inside the latest restic snapshot. The copy schedule becomes one cluster-var shared with the k3s role.
- **Files:** `infra/ansible/roles/flux-operator/` (or a new `first-backup` task), `tools/verify/run`, `tools/drill`, `platform/backups/etcd-snapshots.yaml`, `infra/ansible/roles/k3s/defaults/main.yml`.
- **Tests:** verify with a CronJob that has no `lastSuccessfulTime` and was created 1 h ago gives PENDING with exit 0 (fake kubectl); the drill fake with an older restic `latest` still passes.
- **Done check:** `pytest ci/tests/test_verify.py ci/tests/test_drill.py`.

**P8 — Backup trust boundary.** C-F16, G-F04, G-F22, K-F16, G-F12, K-F11, K-F17, G-F21.
- **Approach:**
  - rest-server gets `--private-repos` and one htpasswd user per namespace, generated by Kyverno from a per-namespace random secret; prune happens in a central CronJob.
  - `privileged-movers` becomes opt-in via a namespace label.
  - Add the `volsync` HelmRelease to the `platform-controllers` healthChecks.
  - Keep `failurePolicy: Ignore`, but have verify/alerting flag unenrolled bound PVCs (already done).
  - Allow `backup: none` anywhere with a required `backup-reason` annotation.
- **Files:** `platform/backups/{enrollment,rest-server}.yaml`, `platform/kyverno/rbac.yaml`, `envs/*/flux/kustomizations/platform-controllers.yaml`, `ci/tests/ansible_role_proof.py:105-109`, `ci/checks/lib/_pvc_backup.py`, `platform/backups/CONTRACT.md`.
- **Tests:** `kyverno apply` of the policy against fixture PVCs (the generated secret holds a per-namespace user); `_pvc_backup` accepts `backup: none` plus a reason and rejects it without one; flux-entry asserts the three health checks.
- **Done check:** `pytest ci/tests/test_backup_enrollment.py ci/tests/test_pvc_backup.py ci/tests/test_flux_entry.py`.

**P9 — Security hardening.** C-F17, C-F18, C-F09, C-F10.
- **Approach:**
  - The CA gets `nameConstraints` (permitted `.<dns_suffix>`) and the env in its CN.
  - Agents get a separate `agent-token`.
  - FluxInstance supports an optional `sync.pullSecret`, created once from a SOPS file like `sops-age`.
  - Security-only unattended upgrades with no automatic reboot, plus a `patch` play (serial 1, drain, reboot if required).
- **Files:** `tools/bootstrap-secrets`, `infra/ansible/roles/k3s/{tasks/token.yml,templates/*.j2}`, `infra/ansible/roles/flux-operator/tasks/`, `infra/ansible/roles/base-os/templates/50unattended-upgrades.j2`, a new `infra/ansible/patch.yml`.
- **Tests:**
  - `test_bootstrap_secrets.py` parses the generated cert (`cryptography` is in the venv) and asserts the name constraint.
  - A k3s render test asserts the agent config holds the agent token, not `node-token`.
  - `test_base_os.py` asserts the allowed security origin.
  - `ansible-playbook --syntax-check patch.yml`.
- **Done check:** the same pytest files, plus the syntax check.

**P10 — Kubectl timeouts and the verify read surface.** G-F24, G-F25.
- **Approach:** `_env.kubectl` adds `--request-timeout=30s` and `timeout=60`, with a distinct error. `_check_secret` asks only for key names, using a jsonpath or go-template over `.data` that prints keys and never values, and checks that the five expected keys exist.
- **Files:** `tools/lib/_env.py`, `tools/verify/run`.
- **Tests:** a fake kubectl that sleeps produces a timeout failure; a verify test asserts the secret read never requests `-o json`.
- **Done check:** `pytest ci/tests/test_verify.py ci/tests/test_restore.py ci/tests/test_drill.py`.

**P11 — GPU platform boundary.** C-F13, G-F06, K-F14, C-F14.
- **Approach:** first PR (S): allow optional per-host `gpu: {count, memory_gb}` that becomes node labels, a fixed label prefix (`jarvis.io/role-<r>`), and the single-node and three-node GPU inventories in `docs/deploy.md`. Later PR (L): NVIDIA GPU Operator via Flux.
- **Files:** `schema/env.py`, `infra/ansible/roles/k3s/templates/label-patches.json.j2`, `tools/verify/run`, `platform/backups/*.yaml` (selectors), `docs/deploy.md`.
- **Tests:** the scratch case `gpu_vram_hint_host_key` becomes accepted; the label render test covers the new prefix and GPU labels; verify node labels.
- **Done check:** `pytest ci/tests/test_env_schema.py ci/tests/test_k3s.py ci/tests/test_verify.py`.

**P12 — Upgrade and release path.** C-F21, G-F07, K-F13, C-F22, C-F39, G-F11.
- **Approach:**
  - Add `docs/upgrade.md`: fork intake via an upstream remote; release tags; a k3s bump with `serial: 1` and drain on agents; Flux operator-then-distribution order; a k3s↔Traefik recheck; removing components that upstream dropped.
  - Move the four stage Kustomizations into a core base.
  - Add a generic Renovate comment manager covering `tools/versions.env`.
  - Use `meta: flush_handlers` before the k3s start.
  - Pin k3s to the newest minor inside Kyverno's tested range until upgraded, or document the accepted skew in the Kyverno Limits.
- **Files:** `docs/`, `envs/*/flux/`, a new `flux/stages/`, `renovate.json`, `infra/ansible/site.yml`, `roles/k3s/tasks/main.yml`.
- **Tests:** flux-entry builds env paths through the base; `test_renovate.py` asserts that `versions.env` is matched; a k3s play run counts one start and no restart on a fresh run.
- **Done check:** `pytest ci/tests/test_flux_entry.py ci/tests/test_renovate.py ci/tests/test_k3s.py`.

**P13 — Docs and contracts.** C-F29, G-F09, K-F8, G-F18, K-F18, C-4a, C-F25, C-F26, G-F15, K-F10 (cross-link only), C-F28, K-F20, G-F13, G-F14.
- **Approach:**
  - Add a generic `docs/secrets.md`; point the doc index at env-00 and deploy.
  - Add a `make site ENV=` target.
  - Add Operations and Limits per the 4a tables (single CP and amd64 as Limits).
  - Add `docs/operations.md` (symptom → where to look).
  - verify gains node-Ready and crash-loop checks.
  - Strip plan codes from code comments and extend `_plan_codes.py` to `*.py`, `*.yml`, `*.yaml`, `*.j2` and `ci/checks/*`.
  - Make the base-os handlers `ansible.builtin.systemd_service` (G-F08).
- **Files:** `docs/`, `Makefile`, every `CONTRACT.md`, `tools/verify/run`, `ci/checks/lib/_plan_codes.py`, `infra/ansible/roles/base-os/handlers/main.yml`, plus the ~41 files carrying codes.
- **Tests:** a plan-codes fixture with a YAML comment code fails; verify with a NotReady node fails; `test_base_os.py` passes on a non-systemd controller.
- **Done check:** `bash ci/run-checks` and `pytest ci/tests/test_plan_codes.py ci/tests/test_verify.py ci/tests/test_base_os.py`.

**P14 — Native replacements.** C-F23, C-F24, G-F23, C-F32, C-F33, C-F37, C-F38.
- **Approach:**
  - prep-node → `infra/ansible/bootstrap.yml` (`-k -K`) plus a small age helper.
  - Flux Operator via a k3s `HelmChart` manifest; delete `helm_cli.yml`.
  - Tools run on `.venv/bin/python`.
  - CLIs go into a repo-local `.bin/`.
  - Use croniter or VolSync `nextSyncTime`.
  - The operator readiness waits run unconditionally.
- **Files:** `tools/prep-node` (to be deleted), `infra/ansible/`, `Makefile`, `tools/install-deps`, `tools/verify/run`, `roles/flux-operator/tasks/operator.yml`.
- **Tests:** `--syntax-check` and `--list-tasks` of `bootstrap.yml`; a flux-operator stub run where the Deployment exists but isn't Available still waits; the verify cron tests are kept.
- **Done check:** `bash ci/ci-local-verify`.

**P15 — Exposure and time.** C-F19, C-F20.
- **Approach:** LoadBalancer Services are allowed with an opt-in annotation; k8s_gateway `resources Ingress Service`; `ntp_pools` entries may be `{host, nts: false}`.
- **Files:** `ci/checks/lib/_loadbalancer.py`, `platform/app-dns/helmrelease.yaml`, `schema/env.py`, `roles/base-os/templates/chrony.conf.j2`.
- **Tests:** loadbalancer accepts an annotated fixture and rejects an unannotated one; the chrony render has no `nts` for a flagged pool.
- **Done check:** `pytest ci/tests/test_loadbalancer.py ci/tests/test_base_os.py ci/tests/test_env_schema.py`.

**P16 — Test quality.** C-T01–T20, G-F17, G-F08.
- **Approach:**
  - Add the missing negative tests listed in round-1 T01–T04, T09, T16–T18 (each names its seed).
  - Delete the helper-only tests and snapshot tests (T07, T19, G-F17).
  - Use `fetch-depth: 0` for gitleaks (T06).
  - `run-checks` runs all checks, then fails (T12).
  - core-names scans root files (T13).
  - Add `ci-local-verify --env00-only` (T15).
  - Vendor the kubeconform schemas (T20).
- **Files:** `ci/tests/*`, `ci/run-checks`, `ci/ci-local-verify`, `.github/workflows/ci.yml`, `ci/checks/lib/_core_names.py`.
- **Done check:** each round-1 seed listed under T01–T04, T09 and T16–T18 now fails its named test (re-seed in a scratch clone), and the full suite is green.

**P17 — Build-agent guard.** K-F7, C-F36, C-T08.
- **Approach:** treat the Secret-read deny entries case-insensitively (the in-repo matcher plus deny-entry generation); add a category corpus row for case variants; run the same corpus against `.claude/settings.json`; add kubeconfig-read and cluster-writing repo-tool categories.
- **Files:** `.cursor/cli.json` (frozen; the owner must decide), `.claude/settings.json`, `ci/checks/lib/_cli_permissions.py`, `ci/checks/fixtures/must-block-main.tsv`.
- **Tests:** a corpus row for the case-variant category is blocked.
- **Done check:** `pytest ci/tests/test_cli_permissions.py`.

**P18 — Small leftovers.** C-F30.
- **Approach:** delete the `data_disk` group and `groups.yml`, the `storage` role, the dead contract roots, the arm64 Helm metadata (if P14 doesn't remove it), and the duplicate restore helpers.
- **Tests:** update `test_inventory_roles.py` and `test_contract_md.py`.
- **Done check:** `bash ci/ci-local-verify`.

## 6. Concerns and questions

1. **Not settled from code.** These need a disposable k3s cluster:
   - Whether k3s `--cluster-reset` without a restore path keeps data (K-F10). I rely on k3s's documented behaviour.
   - Whether kubectl accepts mixed-case resource nouns, which decides whether K-F7 is exploitable or only a matcher defect.
   - Whether Flux reconciles an empty `resources: []` path cleanly (the placeholder deletion).
   - Whether Kyverno 1.19 rejects or accepts the `pvc-backup` policy while the `ReplicationSource` CRD is unmapped (G-F21 impact).
   - Whether Kyverno 1.19 generate behaves correctly on Kubernetes 1.36 (G-F11).
2. **The G-F08 test result depends on the controller**, systemd versus not. The judge should not read the 482/483 versus 483/483 difference as a contradiction in the repo.
3. **K-F7 concerns `.cursor/cli.json`, which the brief marks frozen.** The fix needs an owner decision. I verified it only with the repo's own matcher model (`_cli_permissions.py` says it "does not prove the client").
4. **Severity calibration differs systematically.** GPT and Grok rate documented limits (one CP, amd64, "not disk loss") as findings. I rate the limits low and the missing mitigations high. The judge may want one rule: a documented limit is low unless the product direction (Q13, Q11) requires crossing it.
5. **My round-1 experiments and this round's** are in `../wrapup-claude-env00/audit-scratch/` and can be re-run with `pytest -q -s`. The cross-check took one pass, as instructed.
