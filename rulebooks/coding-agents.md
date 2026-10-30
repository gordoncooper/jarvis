# Coding agents

Rules here are prose only for what `ci/checks/` and `.cursor/cli.json` cannot enforce (R39). When a check or deny entry already covers a rule, this file names it in one line and does not restate it. A task brief overrides this file on how to open, stack, or stop a PR.

## Automated guards (do not duplicate)

- **R34 — Git is the source of truth; no direct cluster or secret tooling:** `ci/checks/cli-deny` and `.cursor/cli.json`.
- **R5 — Probes, memory and CPU, stdout:** `ci/checks/manifest-lint`. Gatus stays a later check.
- **R3 / R28 — Core stays env-agnostic and identity-free:** `ci/checks/core-names`. Its spec is the identity section of `schema/CONTRACT.md`; read it there, not here. LoadBalancer Services are `ci/checks/loadbalancer`.
- **R33 — Images the repo adds are digest-pinned:** `ci/checks/digest-pin`.
- **Env schema (values under `envs/<env>/`):** `ci/checks/env-schema`.
- **R2 — Each component folder has `CONTRACT.md`:** `ci/checks/contract-md`.
- **R25 — No plaintext secrets in Git:** `ci/checks/secret-scan`.
- **Product trees do not use `ci/`:** `ci/checks/product-ci`. `tools/verify` may read `ci/rule-checks.txt`.
- **R31 — The backup ClusterPolicy enrolls every PVC, or the PVC is `backup: none`:** `ci/checks/pvc-backup`.
- **Deferred on purpose; don't add early:** model names in core (R28) return to `core-names` in **p2-23**.

## Stay lean

- Build only what the plan step's done check needs, nothing speculative. This caps scope, not quality: what you do build is clean, consistent with the code around it, and uses the tool's own idioms (an Ansible module over shell where one exists, no dead code, no placeholder scripts, no errors swallowed, one source of truth per value).
- Tests check behavior or encode a rule. Each test names the rule it checks. No tests on names, ordering or file shape. Don't copy templates or hard-code task lists. Don't add a test that only runs a `ci/checks/` entry against the tree CI already scans; behavior tests use fixtures.
- Add container tests only when no cheaper proof exists. A live run is the proof where there is one. If the done check is a live run, attach that run and a server-side dry-run from the head SHA before asking for review. If you can't run them, say so.
- No local tooling that duplicates a hosted service.
- Prefer deleting code to adding it. Before calling a PR ready, list what you'd delete: tests over ~30 lines, and custom scripts where native Ansible, k3s or Flux would do.
- Hardening ideas and edge cases go in the report as notes, not code.

## Workflow

- Start from a fresh checkout of current `main`; never build on another agent's branch. Use the branch prefix the task or forge requires, otherwise `agent/<topic>`. One PR per task, or per batch of related plan steps that touch the same area; keep unrelated or risky changes in separate PRs. No stacked PRs, except throwaway PRs a brief asks for. Open the PR ready for review unless the brief says draft.
- Once review is requested, scope is frozen. New ideas go to a follow-up PR.
- Merging follows R36. In addition, merge only after the Ready to merge boxes are ticked, and if a task brief says to leave the PR unmerged, do that.
- Never touch real hosts: no SSH, no secret decryption, no reading the age private key (deny list blocks many of these; still treat this as a hard stop).
- A value that differs per deployment, including disks, sizes and node roles, is an env setting under `envs/<env>/`, not an Ansible role default and not a literal in core. A schema default the contract documents (D34's `ssh_user`) stays in the schema. The repo must run from any controller node under any user.
- LAN IPs and numeric user IDs never appear in plaintext, envs/ included. Keep them out, or encrypt them under `envs/<env>/secrets/` (R25).
- If the brief or this file looks wrong for the task, say so with your reasoning before working around it. Don't silently follow it or silently depart from it.
- Before every push, run `make ci-verify` locally (full suite, container tests included, nothing skipped). Push once when it is green — no probe pushes to see what CI says. Squash unpushed WIP into one commit per push; don't rewrite commits already on the remote. After two failures on the same problem, stop and report.
- A timebox written only in the plan is not a stop. If the brief sets one and it passes before the done checks pass, stop and report the task as failed, with what's done and what's left (R35).
- Before reporting done, review the **whole** diff for leftovers from earlier attempts (debug prints, commented experiments, wrong env literals), and retitle the PR to what actually landed.

## Report

When handing off or finishing a task, include:

- PR link, head commit SHA, CI status
- What you ran to verify and the result
- Any departure from the brief, and why
- If the PR is over about 300 changed lines (excluding deletions), say why
- Open questions (surface them; do not decide silently)

Before merge, complete the **Ready to merge** checkboxes in `.github/pull_request_template.md`.
