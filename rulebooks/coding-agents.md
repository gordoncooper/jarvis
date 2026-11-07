# Coding agents

Rules here are prose only for what `ci/checks/`, `.cursor/cli.json`, and `.claude/settings.json` cannot enforce. When a check or deny entry already covers a rule, this file names it in one line and does not restate it. A task brief overrides this file on how to open or stack a PR.

## Automated guards (do not duplicate)

- **Git is the source of truth; cluster reads only, and no secret tooling:** `ci/checks/cli-deny`, `.cursor/cli.json`, and `.claude/settings.json`.
- **Probes, memory and CPU, stdout:** `ci/checks/manifest-lint`. Gatus stays a later check.
- **Core stays env-agnostic and identity-free:** `ci/checks/core-names`. Its spec is the identity section of `schema/CONTRACT.md`; read it there, not here. LoadBalancer Services are `ci/checks/loadbalancer`.
- **Images the repo adds are digest-pinned:** `ci/checks/digest-pin`.
- **Env schema (values under `envs/<env>/`):** `ci/checks/env-schema`.
- **Every Flux Kustomization and HelmRelease names a service account the root sync applies, only the platform layer runs as the platform account, and refs stay in-namespace:** `ci/checks/flux-identity`.
- **Each component folder has `CONTRACT.md`:** `ci/checks/contract-md`.
- **Plan codes stay out of Markdown docs, READMEs and `docs/` (not `AGENTS.md` or `.github/`):** `ci/checks/plan-codes`. Code and comments carry none either; no check enforces that.
- **No plaintext secrets in Git:** `ci/checks/secret-scan`.
- **Product trees do not use `ci/`:** `ci/checks/product-ci`.
- **The backup ClusterPolicy enrolls every PVC, or the PVC is `backup: none`:** `ci/checks/pvc-backup`.
- **Deferred on purpose:** don't add a model-name rule to `core-names` until a brief asks for it.

## Stay lean

- Build only what the plan step's done check needs, nothing speculative. This caps scope, not quality: what you do build is clean, consistent with the code around it, and uses the tool's own idioms (an Ansible module over shell where one exists, no dead code, no placeholder scripts, no errors swallowed, one source of truth per value).
- Tests check behavior or encode a rule. Each test names the rule it checks. No tests on names, ordering or file shape. Don't copy templates or hard-code task lists. Don't add a test that only runs a `ci/checks/` entry against the tree CI already scans; behavior tests use fixtures.
- Add container tests only when no cheaper proof exists. A live run is the proof where there is one. If the done check is a live run, attach that run and a server-side dry-run from the head SHA before asking for review. If you can't run them, say so.
- No local tooling that duplicates a hosted service.
- Prefer deleting code to adding it. Before calling a PR ready, list what you'd delete: tests over ~30 lines, and custom scripts where native Ansible, k3s or Flux would do.
- Hardening ideas and edge cases go in the report as notes, not code.

## Before you call it ready

The reviewer checks these on deep reviews; no check can. In the PR, say how the change meets each one it touches.

- **Validate first.** Every input check runs before the first host, cluster or file change. Documented steps are tested in the documented order.
- **Re-runs converge or refuse.** A second run changes nothing. A step that would regenerate, overwrite or delete something other state depends on (a key, a password, a CA, a setting already on a host) refuses without an explicit flag and has a written way back.
- **Smallest blast radius.** Each credential, service account and RBAC rule covers only what its user needs; say what one compromised pod can reach. A shared credential changes for all its users at once or not at all. Nothing creates the identity it runs as.
- **Every guard has a failing case.** A check, validator, refusal or assert you add or touch has a seeded test that makes it fail on the thing it guards.
- **Any layout, any time.** Placement, ordering and schedules work on one node and on mixed multi-node with taints. A new schedule reports "not run yet" instead of failing. One bad host stops only itself unless it must stop all.
- **Docs move with the code.** A new expectation, port, permission or operator step goes in the component's CONTRACT in the same PR. Docs say what is true now.

## Workflow

- Start from a fresh checkout of current `main`; never build on another agent's branch. Use the branch prefix the task or forge requires, otherwise `agent/<topic>`. One PR per task, or per batch of related plan steps that touch the same area; keep unrelated or risky changes in separate PRs. No stacked PRs, except throwaway PRs a brief asks for. Open the PR ready for review unless the brief says draft.
- Subagents may search, read and review; only the main agent edits, commits and pushes.
- Once review is requested, scope is frozen. New ideas go to a follow-up PR.
- The repo owner merges every PR. An agent merges only when its brief explicitly says to, and then only after required checks pass and the reviewer has ticked every Ready to merge box that applies at the current head (a box marked "(n/a)" counts as met). Once branch protection requires the admin-gate check, a PR that touches a protected path, removes a Namespace, PVC or StorageClass, or comes from the code-factory GitHub App also needs an `admin-approved` label the repo owner added after the latest push, and the owner merges it whatever the brief says. Never add that label yourself, even when you run under the owner's account.
- Never touch real hosts: no SSH, no secret decryption, no reading the age private key (the deny lists block many of these; still treat this as a hard stop).
- A value that differs per deployment, including disks, sizes and node roles, is an env setting under `envs/<env>/`, not an Ansible role default and not a literal in core. A schema default the contract documents, such as `ssh_user`, stays in the schema. The repo must run from any controller node under any user.
- LAN IPs and numeric user IDs never appear in plaintext, envs/ included. Keep them out, or encrypt them under `envs/<env>/secrets/`. One exception: `lan_cidrs` and the CIDR in a `lan-target` line may be plaintext in the env repo, including the render that carries them (`flux/generated/apps.yaml`). The exception covers those fields only: it does not allow new LAN IPs, user names or host names in public PRs or docs.
- If the brief or this file looks wrong for the task, say so with your reasoning before working around it. Don't silently follow it or silently depart from it.
- CI runs the checks and the full suite once; the product holds only the `env-00` sample. Before pushing run `bash ci/run-checks` and only the tests near your change; CI at the head SHA is the test record. Push once when that is green — no probe pushes to see what CI says. Squash unpushed WIP into one commit per push; don't rewrite commits already on the remote. After two failures on the same problem, stop and report.
- A timebox written only in the plan is not a stop. If the brief sets one and it passes before the done checks pass, stop and report the task as failed, with what's done and what's left. "Done" means evidence attached: CI or test output and the commit.
- Before reporting done, review the **whole** diff for leftovers from earlier attempts (debug prints, commented experiments, wrong env literals), and retitle the PR to what actually landed.

## Report

When handing off or finishing a task, include:

- PR link, head commit SHA, CI status
- What you ran to verify and the result
- Any departure from the brief, and why
- If the PR is over about 300 changed lines (excluding deletions), say why
- Open questions (surface them; do not decide silently)

Leave the **Ready to merge** checkboxes from `.github/pull_request_template.md` unticked. The reviewer ticks them on approval.
