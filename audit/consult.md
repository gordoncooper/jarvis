# Consult: wave-1 briefs before kickoff

Judge's consult on the wave-1 pack (INDEX, nine briefs, the decisions memo and the fix plan), checked against `main` at `a7550522` (tag `v0.1.0`), project-plans `main` at `771158d`, and my v2 report (`audit/report.md`). I re-checked every line reference I quote here at that commit. Upstream claims cite tag-pinned sources.

**Short answer:** wave 1 can launch once the brief edits in section 6 are made. Nine are small text changes. None needs a new decision from Gordon except the voice wording, which only affects DOCS. Four briefs, as written today, would make their agent stop or turn CI red for reasons the brief didn't foresee: G3a-1, G4, G6a and G7. The D9 plan holds only in part: the FluxInstance `sync` block really has no `verify` field, but gating only a separate core source leaves the root sync ungated, and the root sync can rewrite that core source.

---

## 1. Per brief

### DOCS (project-plans): change needed, small

- **Done check, grep list.** The term `same owner` has zero hits in all three files at `771158d`. The owner rule is worded as "owner handle" (`decisions.md:361-374`). Replace `same owner` with `owner handle`, or the done check proves nothing for D8.
- **Appendix A, plan choice 1.** The memo's suggested answer, which Gordon "confirmed as suggested" at 4:35 PM (memo, plan-choices table row 1), was "move voice into the chat-bot plan as a v1 over HTTPS (Telegram voice notes, a dashboard WebSocket)". The brief records "voice stays deferred" instead (INDEX conflict 12). These are different plan outcomes: one adds a P2 step, the other doesn't. Don't launch DOCS with either wording until Grok Bot confirms which one Gordon meant (see Questions).
- **"Comment on" bullet 2 (instance repo signed tags).** Don't leave this open for the agent to decide. Section 2 below shows the gate is ineffective unless the instance repo's root sync is gated too. Replace the bullet with the rule: "Flux verifies the root sync and the core source; both deploy only signed tags." If Gordon disagrees, that becomes a Stop-and-ask item.
- **Appendix A item 3.** "A keep-safe list ... holds the age key, the off-host backup password and ... the lane token's expiry". Add "and the deploy-signing public key's fingerprint, plus where the signing key lives" once D9 is built. Losing the signing key stops every deploy (section 5).
- Everything else matches the memo: D-numbering from `D48` (the last entry is `D47`, `decisions.md:399`), the step anchors (`p2-07` at `plan.md:724`, `p2-09` at `:747`, `p2-10` at `:759`), and R25 at `framework.md:268`.

### G1 secrets and keys (C47, release check): change needed

- **Missing `.sops.yaml`.** The brief's creation-rule add covers only `current is None` (`tools/prep-node:894`). When `.sops.yaml` is absent, `sync_age_key` returns earlier with "No .sops.yaml; recipient was not changed." (`:885-888`). That is the adopter's state after G3a-1's new step 0 removes env-01's rule (which leaves an empty `creation_rules`), and it is the product repo's normal state after G11, since env-00 has no rule. Fix: "When `.sops.yaml` is missing or has no rule for the env, write the one creation rule with the same `path_regex` and `encrypted_regex` shape as today's file." Add the acceptance case "no `.sops.yaml` at all: one rule is created; a second run changes nothing."
- **`envs/env-00/secrets/README.md` contradicts a contract line.** `schema/CONTRACT.md:104` says env-00 "has no `secrets/`". Fix: add that sentence to G1's map ("say the folder holds only this README"), and add `schema/CONTRACT.md` (that line only) to the collision list with G3a-1 and G4.
- **The new test-decrypt `make` target is a new decrypt entrypoint.** Add a line: "Name it so the command guard can deny it to coding agents by category; G8-guard adds it to the corpus." Then add it to G8-guard's brief (see below).
- **`envs/env-01/secrets/README.md:13`** describes the apt `python3-yaml` and `python3-bcrypt` installs that G4 changes. G1 rewrites this README, so G1 should own the line. Tell G4 not to touch it, and have G1 word it neutrally ("`make deps` installs what the tools need").
- The other references check out: `:132-152`, `:261-268` (`_restic_string_data`), `bootstrap()` at `:343-364`, `test_bootstrap_secrets.py:372-377`, `infra/ansible/README.md:76`, `envs/env-01/secrets/README.md:15,25`. The tier and model are right.

### G3a-1 validate first (C46, release check): change needed (it would turn CI red)

- **The stale-rule check breaks CI's env-00-only copy.** `.github/workflows/ci.yml:26-40` runs `git rm` on every env except env-00 but keeps `.sops.yaml`, which still names `envs/env-01/secrets/`. With D8's "a rule for a missing env fails", `ci/checks/env-schema` fails in the `env00-only` matrix leg on the first push. Fix: add `.github/workflows/ci.yml` (the env00-only step only) to the map, and have that step also remove `.sops.yaml` when no non-example env remains. An absent file is valid for an env-00-only tree (`schema/env.py:148-150`). Add `ci.yml` to the collision list with G8, which edits the checkout step on different lines.
- **Pre-host mode must also skip the ConfigMap staleness compare.** In the documented order, step 1 edits the values and step 5 renders the ConfigMap (`docs/deploy.md:35,39`), and prep-node itself re-renders it at the end (`tools/prep-node:849-860`). A pre-host run that compares the ConfigMap fails every fresh copy, the same class of bug J05 fixes. The brief's stop rule ("stop if pre-host needs more than skipping recipient and age-key checks") will then halt the agent. Fix: "Pre-host skips the recipient, age-key and ConfigMap-render checks; the full run at the end checks all three."
- **Line reference.** `run_env_schema` is defined at `:911` and called at `:986`. Say "called at `:986`, after the host edits", so the agent changes the call order and not the function.
- **Zero SSH calls.** `ensure_host_key` (`:231`) runs `ssh-keyscan` before any session. State that the schema runs before `ensure_host_key`, not just before `open_session`.
- Checked: `_TOOLS` at `:36`; `age_public_key` runs `age-keygen` at `:761-771`; `schema/env.py:512,535-539,542` and the call at `:136`; `schema/CONTRACT.md:17,58,142`.

### G4 node prep (CS, release check): change needed (one item would break a clean controller)

- **Don't drop apt `python3-yaml`.** The brief moves the tools to `.venv/bin/python` via the `Makefile` only. But three groups of entry points still run system `python3` with PyYAML:
  - the `ci/checks/*` scripts, 12 of which run system `python3`; the schema-based ones import PyYAML (for example `ci/checks/env-schema` runs `python3 -` and imports `schema.env`, which imports `yaml`);
  - `tools/prep-node`, which `docs/deploy.md:64` invokes directly through its `#!/usr/bin/env python3` shebang;
  - the render command at `docs/deploy.md:39`.

  `ubuntu-latest` happens to ship PyYAML, so CI stays green, while a fresh controller fails `bash ci/run-checks` and prep-node. Fix: "apt keeps `python3-yaml` and `python3-venv` (and `openssl`); drop only `python3-bcrypt`, whose one user is `tools/bootstrap-secrets` through `make`. Moving ci/checks and the directly run tools to the venv is a later PR." The brief's own stop condition ("a tool that runs before the venv exists") hints at this, but the agent won't find it from the `Makefile` alone.
- **`chrony_nts` needs the example env.** The schema fails unless env-00 sets every known key (`ci/tests/test_env_schema.py:516-523`, contract `:104`). Add `envs/env-00/values/cluster-vars.yaml` (the one key) to the map. Say the key goes in `_CLUSTER_VARS_OPTIONAL` (`schema/env.py:55-64`) and stays out of `CLUSTER_VARS_PUBLISHED` (`tools/lib/render_cluster_vars_configmap.py:26`).
- **Line references.** These are correct: `main.yml:90` is the "Disable automatic package upgrades timer" task; `:81` keeps `apt-daily.timer` enabled; `handlers:3,11,19`, `chrony.conf.j2:9`, `Makefile:34-65`, `install-deps:77-90` (the apt probe), `:147-152` and `:231` (the checksum calls). Note in the brief that `ntsdumpdir` (`chrony.conf.j2:6`) is harmless with NTS off.
- **Pending-reboot warning.** Keep it in Ansible. Add one sentence to the PR's expectations: the warning appears only when someone runs `site.yml` (section 5).
- The tier and model are right.

### G5c restore, drill, integrity (now Opus): change needed (the brief is still written for Cursor)

- **Header.** Change it to "Claude Code on the bastion, **Opus**", with review tier **deep** (the parent pre-reviews the CronJob). The new CronJob is the first object that touches every backup repository at once, and a wrong schedule or lock blocks nightly backups in every namespace. That is the same risk class as G5a and G5b, which Gordon also put on Opus.
- **Worker mechanics.** Replace the `git -C ~/jarvis worktree add …` start with G6a's "You're in your own worktree on branch `fix/g5c-restore-drill`". Swap "Set `OWNER_NAME`…" for "already set in your environment". Add G6a's **Headless run**, **Tests** and **PR** bullets, which the Claude briefs carry and G5c lacks.
- **State the lock fact; don't leave it to discovery.** At the pinned restic `0.19.1`, `restic check` takes an exclusive lock unless `--no-lock` is set ([`cmd/restic/cmd_check.go:242-245`](https://github.com/restic/restic/blob/v0.19.1/cmd/restic/cmd_check.go#L242-L245)). Tell the agent to choose between (a) a schedule window plus a lock-wait budget, or (b) `--no-lock` with its false-positive risk during a concurrent prune, and to justify the choice.
- **Staleness needs slack.** "Fails when its last success is older than one interval" will flap for a weekly job that runs a few minutes late. Make it "older than one interval plus a grace you choose and justify". The first interval is G7's WARN (pending first run).
- Checked: `tools/restore:98-123,561-580`; `tools/drill:289-291` (the restore line in the etcd script) and `:650`; `tools/verify/run:331-351`; `ci/tests/test_restore.py:651-652` (the second `readouterr()` really is always empty).

### G6a Flux lockdown (CO, deep): change needed (settle the unknown in the brief, fix one live-check trap)

- **The unknown is already settled from source at the pinned operator `0.62.1`** (`platform/flux-operator/oci-repository.yaml:15`). With `multitenant: true`, the operator appends a patch that sets `serviceAccountName: kustomize-controller` on every generated Kustomization, which is the root sync ([`internal/builder/profiles.go:299-304`](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/internal/builder/profiles.go#L277-L305), applied from [`internal/controller/fluxinstance_controller.go:381-383`](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/internal/controller/fluxinstance_controller.go#L381-L383)). The same profile adds `--no-cross-namespace-refs=true`, `--no-remote-bases=true` (kustomize-controller) and `--default-service-account=<tenantDefaultServiceAccount>`. So the root sync keeps the controller's cluster-wide rights, and a git revert still works while the controllers run. Put these citations in the brief, keep the "verify it yourself" instruction, and drop "stop if it can't be settled". That saves the Opus run its most expensive research step. `--no-remote-bases` is safe: no tracked `kustomization.yaml` uses a remote base.
- **The J35 change will trip the overwrite refusal on bench.** `sops_age.yml:90-104` compares the whole existing `age.agekey` with the whole key file (`:97`). After G6a, the new value is "only the matching identity", while env-01's existing Secret holds the whole file, comment lines included. So the brief's own post-merge `site.yml --tags flux-operator` fails with "may be the only copy of the age key". Fix: "Compare the set of age identities, not raw text. An existing Secret whose identities include the matching one passes and is left as is (no rewrite). A Secret without it refuses with today's message." Add the acceptance case "existing Secret with the full file plus comments passes".
- **Line reference.** `defaults/main.yml:14-16` includes `:15`, the **amd64** archive member. Say `:14` and `:16`, or the agent may delete the line the amd64 path needs.
- **D9 forward note.** Replace "The FluxInstance `sync` block has no verify field, so that gate will sit on a separately defined source" with the section 2 finding: the root sync must be gated too. G6a should note anything that blocks either documented path (a self-managed root `GitRepository`, or a `kustomize.patches` entry on the generated one).
- Checked: `flux-instance.yaml:20`, `platform-controllers.yaml:15-23`, `helm_cli.yml:17-25` (stat-only install), `operator.yml:21-49` (waits inside the "when absent" block), and `sops_age.yml:69`. The VolSync HelmRelease is `flux-system/volsync`.

### G7 verify (C46, release check): change needed

- **The `_env.py` timeout is shared with drill and restore.** `tools/lib/_env.py:36-51` `kubectl()` is imported by `tools/drill` and `tools/restore`, which pass `--wait=true --timeout=60s` on deletes (`tools/drill:105-110`) and run long waits. A default subprocess timeout or a short `--request-timeout` in the shared helper could cut those calls. Fix: "Timeouts are parameters with no change in behaviour for callers that don't pass them, or a default comfortably above any `--timeout=` the caller passes. `ci/tests/test_drill.py` and `test_restore.py` pass unchanged." Those files belong to G5c, so G7 runs them but doesn't edit them.
- **J36 as worded can't be built.** "No values leave the API server" isn't possible with `kubectl get secret`, since every output filter runs client-side. Reword it to my report's intent: "verify's process never receives a value. kubectl is asked for key names only (a keys-only output template), and a fake-kubectl test proves verify never parses `data` values." Change the acceptance line "the Secret check's kubectl call can't return values" to match.
- Checked: `run()` at `:405-462`, `_check_secret` at `:155-170`, the etcd message at `:372`, the role prefix at `:204`, the chain check at `:106`, the canary hash at `:397`, the orphan branch at `:345`, and `etcd-snapshots.yaml:11-12`.

### G8 CI and tests (CS, light): change needed

- **The wider `core-names` scan fails on `.sops.yaml`.** The env folder name is an identity value, allowed only under `envs/**` and `docs/**` (`schema/CONTRACT.md:122`). The root `.sops.yaml` names `envs/env-01/secrets/`, and it is the only tracked file outside `envs/` and `docs/` that names env-01. Fix: "`.sops.yaml` may contain env names in `path_regex` only," with a seeded test.
- **Collision with G3a-1 on `.github/workflows/ci.yml`** (the env00-only step versus the checkout depth). Add it to both briefs and to INDEX.
- **Tier.** Light still fits, but `run-checks` collecting every failure changes the exit path every check relies on. Ask for a seeded test that a failing check's exit code still fails `run-checks`, not just that two messages print.
- Checked: `_pvc_backup.py:118`, `ci.yml:24`, `_contract_md.py:9-17,32-36`, `_manifests.py:18-20,33-34`, `_flux_substitute.py:40`, `_core_names.py:35-45,189-195`, `renovate.json:6-11,55-59`, `test_flux_entry.py:40-44,70-77`.

### G8-guard (CO, deep): change needed, small

- **Add G1's new decrypt target to the corpus**, in the "secret decryption and age-key access" category ("the repo entrypoint G1 adds that test-decrypts a secret"). It lands before this PR, so the corpus can name the real target.
- **Start condition.** It now waits for G6a's and G5c's Opus runs to end and for G8 to merge. See section 3 for the order.
- The rest is right: the category-only wording, the patch-file fallback for `.claude/settings.json`, and no bypass strings.

### INDEX: change needed

- The G5c row changes to CO, deep. The Opus runs become G6a, then G5c, then G8-guard.
- Replace the launch sentence ("two Sonnet runs plus one Opus run at once") with the sequence in section 3.
- Add the new collisions from section 3.
- Close "Still needing Gordon" items 1 (usage is at 3% weekly) and 4 (bench is test-only). Rewrite 7's D9 bullet with section 2.

---

## 2. Decisions applied correctly?

| Decision | In the briefs | Verdict |
| --- | --- | --- |
| D1 off-host copy | Appendix only; wave 3 (G5a) | Correct. Consequences in section 5 |
| D2 token, rehearsal | Appendix; G2a in wave 2 | Correct. My §7c line already says bench; add "never the data disk" |
| D3 custody | G1 | Correct, plus the missing-`.sops.yaml` fix and the guard entry |
| D4 per-namespace credentials | G5c limits | Correct. Add the G5b note (INDEX conflict 7) to G5b's brief |
| D5 patching | G4 | Correct, plus the PyYAML fix |
| D6 service landing | Appendix; G6b-1 | Correct |
| D7 pre-consumer config | Appendix; G3b | Correct; nothing in wave 1 touches it |
| D8 owner rule | G3a-1 | Correct, but CI-breaking without the env00-only fix |
| D9 private repos and signed tags | G6a note, DOCS, INDEX conflict 4 | **Partly wrong**; see below |
| D10 CA scope | G1 | Correct |
| D11/D13 guard | G8-guard | Correct; unfreeze scope as INDEX item 2 reads it |
| D12 topology | G6a arm64 deletes | Correct, plus the `:14,16` fix |
| D14 seam | G6a note, DOCS | Correct for wave 1; G11 is under-scoped (section 4) |
| Plan choices 1–7 | DOCS appendix A | 2–7 match the memo. 1 doesn't (see DOCS) |

INDEX's 12 conflicts: 1, 2, 3, 5, 6, 7, 8, 9, 10 and 11 are resolved correctly, with the additions above for 3 (CI) and 8 (missing file). Number 12 needs Gordon. Number 4 is below.

### D9: does "the gate sits on a separate named source" hold?

**The fact is confirmed.** At the pinned Flux Operator `0.62.1`, `FluxInstance.spec.sync` has exactly these fields: `name`, `interval`, `kind`, `url`, `ref`, `path`, `pullSecret` and `provider`. There is no `verify` ([`api/v1/fluxinstance_types.go:431-483`](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/api/v1/fluxinstance_types.go#L431-L483); the docs' "Sync fields" list in [`docs/api/v1/fluxinstance.md`](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/docs/api/v1/fluxinstance.md#sync-configuration) says the same). The generated `GitRepository` template has no `verify` either ([`internal/builder/templates.go:425-479`](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/internal/builder/templates.go#L425-L479)).

**Flux itself supports the gate at the pinned version.** Flux `2.10.0` ships source-controller `v1.10.1` (flux2 `manifests/bases/source-controller/kustomization.yaml:4`). `GitRepository.spec.verify` supports modes `HEAD`, `Tag` and `TagAndHEAD`, with PGP keys (`.asc`) or SSH keys (`.sshpub`) in a Secret in the same namespace ([source-controller `v1.10.0` `docs/spec/v1/gitrepositories.md`, Verification](https://github.com/fluxcd/source-controller/blob/v1.10.0/docs/spec/v1/gitrepositories.md#verification)). Either key type works, so D9's open "SSH or GPG" question is Gordon's preference, not a constraint.

**The plan doesn't hold as stated.** Gating only a separately named core source doesn't give "Flux deploys only Gordon-signed tags":

- The root sync applies `envs/<env>/flux` (`envs/env-00/flux/kustomization.yaml` lists the FluxInstance and every stage Kustomization).
- That tree defines the core `GitRepository`, its `verify.secretRef`, and every Kustomization with its service account. G6a adds a cluster-admin `flux-platform` account there.
- So anyone who can push to the branch the root sync tracks can remove `verify` from the core source, swap its keys, or add a Kustomization that applies anything as `flux-platform`. After G11 that branch is in a private repo on GitHub Free, which has no branch protection, and the coding-agent identity will have write access to it if agents open PRs there.
- The gate is only real if the root sync is verified as well.

**Two ways to gate the root sync:**

1. **A self-managed root (documented).** Drop `.spec.sync` from the FluxInstance; the field is optional (fluxinstance.md, "Sync configuration"). Ansible applies a root `GitRepository` (with `ref.semver` and `verify.mode: Tag`) plus a root `Kustomization` once, the way it applies `sops-age` today, and from then on they reconcile themselves from the verified tree. This is how `flux bootstrap` works. The public half of the verification key, as a Secret in `flux-system`, is applied by Ansible before the first reconcile.
2. **Patch the generated root (works, but undocumented).** The operator renders `sync.yaml` in the same kustomize build as the user's `.spec.kustomize.patches` ([`templates.go:42-44` and `:164`](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/internal/builder/templates.go#L42-L44); patches come from `spec.kustomize.patches` at [`fluxinstance_controller.go:432-437`](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/internal/controller/fluxinstance_controller.go#L432-L437)). So a patch targeting `kind: GitRepository, name: flux-system` can add `verify` and swap `ref`. The docs describe patches only as "patches to apply to the Flux controllers" ([fluxinstance.md, Kustomize patches](https://github.com/controlplaneio-fluxcd/flux-operator/blob/v0.62.1/docs/api/v1/fluxinstance.md#kustomize-patches)), so an operator upgrade could break this silently, and CI can't render the operator's build.

**My recommendation is option 1, owned by one PR.** Today the gate's owner is scattered: G6a says "G6b-2 and G11", INDEX says "G6b-1 or G11", and the fix plan says G6b-2. Put the whole gate, root and core, in **G11**, since that PR rewrites the root sync anyway. Make G11 a hard prerequisite of P2, which it already is under D14. Nothing real deploys before P2, so the gap between G6b-1 and G11 is acceptable.

**Mode.** Use `Tag`, not `TagAndHEAD`. `TagAndHEAD` also verifies the commit the tag points at. A GitHub web merge is signed by GitHub's web-flow key, which D9 explicitly excludes, so `TagAndHEAD` would reject every web-merged commit unless Gordon signs commits locally too.

---

## 3. Ordering and collisions

### Opus order (one Opus run at a time)

1. **G6a** first. It has the highest rank of the three, and G8 and G8-guard land after it.
2. **G5c** starts when G6a's run ends, while G6a waits for review and its bench live check. It still lands after G7.
3. **G8-guard** starts after G5c's run ends **and** G8 has merged. If G6a's live check slips, G8-guard slips with it; the C47 fallback keeps it moving.

The Cursor runs (DOCS, G7, G1, G3a-1) and the Sonnet runs (G4, G8) start at once. The landing order in INDEX stays, with G5c's row changed. Whether two Sonnet runs alongside one Opus run is acceptable on one Max account is a question (see Questions).

### Collisions missing from INDEX

| File | PRs | Rule |
| --- | --- | --- |
| `.github/workflows/ci.yml` | G3a-1 (env00-only step), G8 (checkout depth) | Different steps; the second to merge rebases |
| `schema/CONTRACT.md:104` | G1 (env-00 `secrets/` README), G3a-1, G4 | One line each; the second rebases |
| `envs/env-00/values/cluster-vars.yaml` | G4 (`chrony_nts`) | Only G4 in wave 1; note it for G3a-2 and G3b |
| `envs/env-01/secrets/README.md:13` | G1 (owns it), G4 (changes the facts it states) | G1 words it neutrally; G4 stays out |
| `tools/lib/_env.py` behaviour | G7 (timeouts) affects G5c's `tools/drill` and `tools/restore` | G7 runs `test_drill.py` and `test_restore.py` unchanged |
| The command-guard corpus | G1 (new decrypt target), G8-guard | G8-guard adds the category row after G1 lands |

### Split or merge

- **No splits in wave 1.** G5c is coherent; its CronJob is the deep part, and it gets the parent's pre-review.
- **Wave 3: land G5b before G5a, or build G5a per namespace from day one.** D1's "copy every repository" needs each repository's password. Built before G5b, the copy job is one more holder of the shared credential that G5b then has to rework. Built after, it is one copy job per namespace with that namespace's credentials, the shape D4 wants anyway. The fix plan has G5b after G5a only because of the enrollment file and the tolerations, and both are one-line rebases.

---

## 4. Gaps

### Definition-of-done items no wave 1–3 PR covers

- **The adopter walkthrough (§7c).** No PR or run owns it. Assign it as a run after G3a-2 (wave 2), on bench, which is now allowed to be torn down. If env-01 is rebuilt rather than migrated (section 5), that rebuild is the walkthrough.
- **The restic rotation rehearsal (§7c Custody).** G1 writes the runbook with "Live check: none". Add an Operator step after G1 merges: rotate the drill repository's password with the runbook, then run `make drill` green.
- **D9 and D14 are missing from §7c.** Add two boxes:
  - "Flux verifies both the root sync and the core source; an unsigned or wrongly signed tag doesn't deploy (seeded on bench)."
  - "env-01 lives in its own private instance repo; the product repo holds no non-example env."

  The second is the fix plan's edit 2, with both sources gated.
- **D10, D3 and D5 artefacts.** Add them to the existing §7c boxes:
  - Updates: "a pending reboot is reported".
  - Custody: "the pass/fail command passed on both copies".
  - Tenancy or Docs: "a new env's CA is name-constrained".
- Wave 4 and later (G6b-2 baseline, G9 docs) cover the rest of §7c. That's fine, but note that §7c can't be ticked at the end of wave 3.

### What jarvis-internal changes

Yes, G6b-1 and G11 should both target it concretely. The repo exists now, so designing against a placeholder wastes the seam.

- **G6b-1 builds the seam for a root in another repo:**
  - Platform paths move to the core source. `platform-controllers` uses `path: ./platform` from the root source (`envs/env-01/flux/kustomizations/platform-controllers.yaml:10,24-26`), which won't exist in the instance repo.
  - The core configs and the env's secrets split into two Kustomizations. `platform-configs` mixes core and env in one kustomize root through a relative path (`envs/env-01/flux/platform-configs/kustomization.yaml:4`, `../../../../platform/configs`). Under lockdown `--no-remote-bases` is on, so a remote base isn't a way out; there have to be two Kustomizations (core configs from the core source, env secrets from the root source).
  - A CI test renders an env tree that sits **outside** the core checkout.
- **The tools seam has no owner.** 22 tracked files under `tools/`, `infra/ansible/`, `schema/` and `ci/checks/` assume `envs/<env>` under the core checkout, among them `tools/prep-node`, `bootstrap-secrets`, `seal`, `verify/run`, `drill`, `restore`, `lib/_sops.py`, `lib/_env.py`, `schema/env.py` and `infra/ansible/group_vars/all.yml`. G11 is sized "M, mechanical" and doesn't include this. Either G6b-1 takes it ("tools take an instance root; default is the core checkout"), or G11 is re-sized L. I'd put it in G6b-1, which already sets the seam.
- **G11 must cover:**
  - the pull secret, which is required for env-01, not optional (a read-only deploy key is the smallest; it goes on the keep-safe list and is bootstrapped by Ansible like `sops-age`);
  - the root-sync gate from section 2;
  - CI in jarvis-internal, which needs the core at the pinned tag to render and validate. Private-repo Actions on GitHub Free draw from the included compute quota (the "$12 USD of included compute usage per month" in my v2 §8c);
  - the switch order: push the tree to jarvis-internal first, then commit the source change in the product repo's env-01 tree. The root sync applies the FluxInstance from git (`envs/env-01/flux/kustomization.yaml`), so a change made in the wrong order points Flux at an empty or wrong tree;
  - removing env-01's `.sops.yaml` rule from the product repo.
- **`core-names` loses its inputs after G11.** It reads identity values from every non-example env in the product repo (`schema/CONTRACT.md:120`). After G11 there are none, so the public repo's leak check guards only the owner's name and the product rules, at exactly the point where agents work on core with env-01 knowledge. G11 should add a core-names run in jarvis-internal's CI against the pinned core checkout, using env-01's values.
- **G3a-1's step 0** ("delete `envs/env-01/`") goes away in G11. The brief already flags it.
- **`schema/CONTRACT.md:57`** says `flux_git_repo_url` is a "Public Git remote". G11, or G6b-1 if it adds the optional pull secret, rewrites that line.

---

## 5. Things Gordon got wrong, or hasn't seen

### Signing only the core source doesn't stop an unsigned deploy

See section 2. Whoever can push to the instance repo's tracked branch controls the core source's `verify` block and every Kustomization's identity. After G11 that's a private repo on GitHub Free with no branch protection, and the coding-agent identity will have write access if agents work there. Both sources have to be verified, or the gate is for show.

### Signed-tag deploys make Gordon the deploy step for everything

After the gate is built:

- Renovate digest bumps, security fixes and every env edit (a new host, a bot profile, a sealed secret) reach the cluster only when Gordon cuts a signed tag. A core upgrade needs two tags: one on core, then one on the instance repo after the core ref bump.
- Every "live check after merge" in later briefs becomes "after merge **and tag**".
- With `ref.semver` and `mode: Tag`, a higher unsigned tag fails verification, and the source stops Ready until that tag is deleted. Flux fails closed, but the cluster stops deploying. Coding agents must never push tags; that belongs in the standing rules once the gate lands. On the public product repo, a tag rule restricting `v*` creation to Gordon may be possible (check plan availability first); the private repo can't have one on Free.
- Losing the signing key stops all deploys until Gordon updates the verification Secret by hand. That is a new Gordon-only break-glass action and a keep-safe item.

### bench is test-only, so env-01 doesn't need migrating

The env-01 key file is the bench key (`envs/env-01/secrets/README.md:15`), and the live checks run `make verify ENV=env-01` on bench. If bench can be torn down, several one-off migrations aimed at env-01 cost more than a rebuild:

- the owner sealing env-01's existing token (D2);
- "env-01 at its next CA rotation" (D10);
- G5b's migration of live repos with rotation;
- G11's in-place source switch.

A fresh env-01, built from jarvis-internal after G6b-1, G5b and G11 and following only `docs/`, gets a constrained CA, a sealed token, per-namespace credentials and a gated root from day one. The rebuild also serves as the adopter walkthrough and the rebuild rehearsal. The runbooks (rotation, reseal) still need one rehearsal each on bench, but not migration code for one disposable env. The 1 TB disk stays untouched either way: `node-disk` never formats (`infra/ansible/roles/node-disk/tasks/` has no `mkfs`; it refuses a data path on `/`). What's on that disk that must survive is a question (see Questions).

### The bastion as env-01's off-host target sits next to the coding agents

Claude Code workers run on the bastion as a user with LAN reach (approach.md, decision 1), and Opus now does the backup PRs from there. If the off-host restic repository is readable or writable by that user, an agent mistake can delete the copy that exists to survive mistakes. Fix it in G5a's brief:

- the target runs as a separate user;
- it is append-only (a rest-server with `--append-only`, or SFTP with restricted rights);
- the coding-agent user can't read its credentials.

Also note the concentration: the bastion is now the controller, the place the age key is used, the kubeconfig holder and the off-host copy. Losing the bastion and bench's disk together loses everything, so D1's "a later cloud copy is a setting change" is the real site-loss answer.

### D1 with D4: the copy job needs every namespace's password

`restic copy` opens both the source and destination repositories. With per-namespace credentials, a single nightly copy job holds every namespace's password, which undoes D4's isolation. Build it per namespace (section 3).

### The pending-reboot warning is pull, not push

Under D5 the warning prints only when someone runs `site.yml`. Nodes install kernel security fixes and keep running the old kernel until a reboot. If nobody runs `site.yml`, nobody is told. That's acceptable for now, but D5's "the owner is told" really means "told when he looks". A push signal (a node-exporter textfile metric plus an alert) belongs with alerting later.

### The D3 command reads the age key, so it must not run as the agent user

The pass/fail command needs the private key on the machine that runs it. On the bastion, Gordon must run it under his own user, with the key file unreadable by the coding-agent user, and remove the second copy afterwards. The new `make` target is also exactly the kind of entrypoint the guard must deny to agents (G1 and G8-guard above).

### The D2 rehearsal on bench proves OS loss, not disk loss

The backups live on the same data disk the rehearsal must not touch (rest-server is a hostPath on the backup node). The rehearsal proves "the OS disk died and the data disk survived". "The node died" in full needs D1's off-host copy and a restore from it, which is §7c's Off-host box. Name the rehearsal honestly in G2a.

### Opus is now six serial PRs, not four

The fix plan budgeted four Opus PRs (G6a, G5b, G6b-1, G8-guard). Gordon's message adds G5c and says G5a "stays" Opus, but the fix plan had G5a on C47. Counting G5a, that's six serial Opus runs: G6a, G5c and G8-guard in wave 1; G5a, G5b and G6b-1 in wave 3. 3% weekly usage is ample. The constraint is wall-clock time: each Opus run blocks the next, and G8-guard also waits on G6a's bench check.

---

## 6. Verdict

**Ready to launch wave 1 after these edits** (all text, none needing a new decision):

1. **G3a-1:** add the env00-only `.sops.yaml` removal in `ci.yml`; pre-host also skips the ConfigMap compare; give the call site as `:986`; the schema runs before `ensure_host_key`.
2. **G1:** create the rule when `.sops.yaml` is missing; add the contract `:104` line; take ownership of `envs/env-01/secrets/README.md:13`; name the decrypt target for the guard.
3. **G4:** keep apt `python3-yaml`; add env-00's `cluster-vars.yaml` to the map; make `chrony_nts` optional and unpublished.
4. **G6a:** cite `profiles.go:299-304` for the root sync; compare `sops-age` by identity set; fix the defaults reference to `:14,16`; reword the D9 note.
5. **G7:** keep caller behaviour stable for the shared `_env.kubectl` timeouts (drill and restore tests pass unchanged); reword J36 to "verify never receives a value".
6. **G8:** allow env names in `.sops.yaml`; add the `ci.yml` collision; add the exit-code seeded test.
7. **G5c:** set the Opus header to tier deep; add the Claude worker mechanics and headless rules; state the restic lock fact; add a staleness grace.
8. **G8-guard:** add the decrypt-target corpus row; update the start condition.
9. **INDEX:** add the Opus order and the new collisions; close the answered items.
10. **DOCS:** fix the `owner handle` grep; settle plan choice 1 first; change the instance-repo gate from a comment bullet to a rule.

G7, G6a and G1 can go as soon as their own edits are in. **DOCS waits only on plan choice 1.** Wave-3 notes (G5b before G5a, the tools seam in G6b-1, G11 re-scoped with the gate, the bastion target's separate user) should go into those briefs when they're written, not now.

---

## Questions for Grok Bot

1. **Plan choice 1:** did Gordon confirm the memo's "move voice into the chat-bot plan as a v1 over HTTPS", or the chat wording "stays deferred, groundwork only"? DOCS can't launch until this is settled.
2. **G5a's model:** the fix plan had C47; Gordon's message says G5a "stays" Opus. Is G5a Opus now?
3. **D9's mechanism:** may G11 drop `.spec.sync` for a self-managed, verified root (my recommendation), or should it use the undocumented patch path? And may the standing rules forbid agents from pushing tags?
4. **env-01:** rebuild fresh from jarvis-internal after the foundation, or migrate in place? Section 5 argues for rebuilding. Either way, what on the 1 TB disk must survive (only today's restic repositories, or other data)?
5. **Claude concurrency:** is "one Opus at a time" also "no Sonnet alongside Opus"? The audit setup ran nothing alongside its Opus run; INDEX plans two Sonnet runs alongside one.
6. **Source of truth:** the standing user rule names Gitea for `jarvis-cluster`, while this work is on the GitHub `jarvis` repo (the memo's flag 6 is still open). Confirm that GitHub is the source of truth for `jarvis` and jarvis-internal.
7. **The bastion backup target:** who sets up the separate user and the append-only backend on the bastion? It's outside the repo, so it's an Operator or Gordon step that no brief owns yet.
