# Consult round 3: coding-agent standards

Checked against `rulebooks/coding-agents.md`, `ci/checks/` and `.github/workflows/ci.yml` at `a755052`, and the findings in `audit/report.md`. Line numbers below are the rulebook's unless a file is named.

I agree with a short list that Reviewer checks on deep reviews, with the 24 questions kept as the audit checklist. But four of the seven draft items are already covered, and the draft misses the two patterns behind most of the high findings: re-runs and one-way steps, and validating before change.

## 1. Overlap: what to drop

| Draft item | Already covered by | Call |
|---|---|---|
| Lean and native | "Stay lean" `:21` (tool's own idioms, Ansible module over shell, no dead code, one source of truth) and `:25` (native Ansible, k3s or Flux over custom scripts) | Drop |
| A clean product | `core-names` (names, hosts, suffix, user, repo owner, `bastion`, `/home/`), `plan-codes` (Markdown; code comments come with the plan-codes extension), `secret-scan`, and `:35` (LAN IPs, numeric UIDs) | Drop. Move "docs say what's true now" into the docs line below; nothing checks it |
| An adopter can follow it | `env-schema` checks values in CI, not the order tools run in | Keep, split into "validate first" and "docs move with the code" |
| Works on any layout | `:34` covers per-deployment values only, not placement, ordering or time | Keep, sharpened |
| Tests check behaviour, pass env-00-only | `:22`, `:37`, and the CI matrix (`ci.yml:17-40`) enforces env-00-only | Drop |
| Nothing trusts too much | `secret-scan` covers plaintext; nothing covers RBAC or credential scope | Keep, reframed as blast radius |
| Leave it cleaner | `:21` (no dead code), `:25` (prefer deleting) | Drop; the "write it down" part goes into the docs line |

## 2. What's missing, ranked

These are the failure patterns a coding agent could have caught in its own PR, ranked by how many findings they explain and how severe those are.

1. **Re-runs converge or refuse; one-way steps need an explicit flag and a way back.** Findings: J03, J04, J06, J22, J28, J33, J42, J45, J59, plus round 2's G1 rotation bug. Three of these are high, and this is the single largest pattern.
2. **Validate before change.** Every input check runs before the first host or cluster change, and documented steps are tested in their documented order. Findings: J05, J06, J07, J17, J23, J54. J05 is the clearest case: the documented order always fails.
3. **Smallest blast radius.** Each credential, service account and RBAC rule is scoped to its user. A shared credential changes for all of its users at once or not at all. Nothing creates the identity it runs as. Findings: J08, J11, J19, J20, J35, J36, J50, J51, J64, plus round 2's two bugs (G1's shared password and G6a's self-applied service account).
4. **Every guard has a failing case,** and it fails on the thing it guards. Findings: J09, J26, J27, J40, J41, J56. Today's rule at `:22` requires behaviour tests, but it doesn't say a guard must be shown to fail.
5. **Any layout, any time.** Placement and schedules work on mixed multi-node; a new schedule has a "not run yet" state; one bad host doesn't stop every host. Findings: J12, J13, J30, J52, J57, J63.
6. **Docs move with the code.** Findings: J05 (`deploy.md` order), J25, J34, J44, J62.

Two candidates I left out:
- **Shared helpers that change behaviour for other callers:** no finding shows this. J29 and J49 are helpers missing a behaviour, not helpers changed out from under their callers. The risk is real between parallel PRs, and INDEX's collision table already handles it; it doesn't need a rulebook line.
- **Design gaps a single PR couldn't have caught:** J01, J10, J14, J15 and J18. These stay on the audit checklist.

## 3. Fit

- **Claude Code lane: change.** `:3` and `:7` name only `.cursor/cli.json`, though `.claude/settings.json` is on main and `AGENTS.md` already names both. Change them to "the client deny lists (`.cursor/cli.json`, `.claude/settings.json`)". That wording should land with G8-guard's parity check: add rulebook `:3` and `:7` to G8-guard's allowed files, next to its `AGENTS.md` line.
- **Plan codes and R-numbers: change, later.** The rulebook carries R39, R34, R5, R3, R28, R33, R2, R25, R31, R36, R35, D34 and p2-23. It passes only because `ci/checks/lib/_plan_codes.py` excludes `rulebooks/`. The plan lives outside this repo, so `:32` ("Merging follows R36") and `:38` (R35) are rules an adopter's agent can't read. The other codes are labels on lines that already state their rule. `:17` is plan state, not a rule.
- **Product/instance split: fine until env-01 moves.** `:34-35` and `:37` assume one repo. When it splits, the rulebook needs a line saying which repo a change goes in.

## 4. Landing

Land it as its own small, light-tier PR before wave 1 starts: about ten lines of prose, no live check, green when `bash ci/run-checks` passes.
- Every wave-1 brief names the rulebook as a standing rule. If it lands first, every wave-1 agent builds to the same list that Reviewer checks.
- DOCS edits a different repository, and G8's scope is already fixed. Adding prose to G8 would tie a light change to a Sonnet run that G8-guard waits on.
- No wave-1 brief edits this file, so there's no collision.

If it can't merge before kickoff, don't hold the launch. Give Reviewer the section below as a checklist now and merge it afterwards.

## 5. Proposed section

Insert this after "Stay lean" (it replaces the draft list):

```markdown
## Before you call it ready

Reviewer checks these on deep reviews; no check can. In the PR, say how the change meets each one it touches.

- **Validate first.** Every input check runs before the first host, cluster or file change. Documented steps are tested in the documented order.
- **Re-runs converge or refuse.** A second run changes nothing. A step that would regenerate, overwrite or delete something other state depends on (a key, a password, a CA, a setting already on a host) refuses without an explicit flag and has a written way back.
- **Smallest blast radius.** Each credential, service account and RBAC rule covers only what its user needs; say what one compromised pod can reach. A shared credential changes for all its users at once or not at all. Nothing creates the identity it runs as.
- **Every guard has a failing case.** A check, validator, refusal or assert you add or touch has a seeded test that makes it fail on the thing it guards.
- **Any layout, any time.** Placement, ordering and schedules work on one node and on mixed multi-node with taints. A new schedule reports "not run yet" instead of failing. One bad host stops only itself unless it must stop all.
- **Docs move with the code.** A new expectation, port, permission or operator step goes in the component's CONTRACT in the same PR. Docs say what is true now.
```

## Later

- Replace the plan codes in the rulebook: inline the text of R36 and R35, drop the other labels and `:17`, then remove `rulebooks/` from the plan-codes exemption. This fits with G9's extension of plan-codes to code comments.
- Update `:3` and `:7` for both client deny lists with G8-guard, as in section 3.
- Add a which-repo line when env-01 moves to its own repo.
