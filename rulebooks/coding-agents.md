# Coding agents

Rules here are prose only for what `tools/checks/` and `.cursor/cli.json` cannot enforce (R39). When a check or deny entry already covers a rule, this file names it in one line and does not restate it.

## Automated guards (do not duplicate)

- **R34 — Git is the source of truth; no direct cluster or secret tooling:** `tools/checks/cli-deny` and `.cursor/cli.json`.
- **R3 / R28 — Core stays env-agnostic (no env names, DNS suffixes, or operator-specific strings in core):** `tools/checks/core-names`.
- **Env schema (values under `envs/<env>/`, including `values/nodes.yaml`):** `tools/checks/env-schema`.
- **R2 — Each component folder has `CONTRACT.md`:** `tools/checks/contract-md`.
- **R25 — No plaintext secrets in Git:** `tools/checks/secret-scan`.

## Stay lean

- Build only what the plan step's done check needs, nothing speculative.
- Tests check behavior or encode a rule. Don't copy templates, hard-code task lists, or re-run checks CI already runs.
- Add container tests only when no cheaper proof exists. A live run is the proof where there is one.
- No local tooling that duplicates a hosted service.
- Prefer deleting code to adding it. Report any PR over about 300 changed lines (excluding deletions) and explain why.
- Hardening ideas and edge cases go in the report as notes, not code.

## Review

- One fix round per PR; after that, only real bugs block, and everything else becomes a note.
- Reviewers also block an unexplained file move and the removal of a check for a rule already in force.
- Small low-risk PRs merge on reviewer approval plus green CI.
- Docs, plan, design, infra, and risky PRs go to Gordon first (R36 adds `admin-gate` from the P2 exit).

## Workflow

- Branch `agent/<topic>` from `main`. One PR per task, or per batch of related plan steps that touch the same area; keep unrelated or risky changes in separate PRs. No stacked PRs. Open the PR **ready for review**, not draft.
- Merging follows R36 and the review rules above. Merge only after the Ready to merge boxes are ticked; if a task brief says to leave the PR unmerged, do that.
- Never touch real hosts: no SSH, no secret decryption, no reading the age private key (deny list blocks many of these; still treat this as a hard stop).
- Env-specific values (hostnames, node roles, data disk by-id paths, owner/repo names, repo URLs, the node SSH user `ssh_user` per D34) live only in `envs/<env>/values/` and inventory under `envs/<env>/`. Never put them in core or role defaults.
- LAN IPs and user IDs never appear in plaintext, envs/ included. Keep them out, or encrypt them under `envs/<env>/secrets/` (R25).
- Names and home paths of whoever runs the tooling never appear anywhere in the repo; the repo must work from any controller node. `tools/checks/core-names` flags home-directory paths.
- Before every push, run `make ci-verify` locally (full suite, container tests included, nothing skipped). Push once when it is green — no probe pushes to see what CI says. Squash WIP into one commit per push. After two failures on the same problem, stop and report.
- If the task timebox passes before the done checks pass, stop and report the task as failed, with what's done and what's left (R35).
- Before reporting done, review the **whole** diff for leftovers from earlier attempts (debug prints, commented experiments, wrong env literals).

## Report

When handing off or finishing a task, include:

- PR link
- Head commit SHA
- CI status
- `git diff --stat` (base → head)
- Open questions (surface them; do not decide silently)

Before merge, complete the **Ready to merge** checkboxes in `.github/pull_request_template.md`.
