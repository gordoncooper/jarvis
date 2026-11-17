# Coding agents

How an agent works on core. The brief, and the charter when the session has one, says what to build; this file says how. Rules a check or the deny lists enforce are named once, not restated. If the brief or this file looks wrong for the task, say so before working around it.

## Hard stops

- **No real hosts, clusters or secrets.** No SSH, no `kubectl`, `helm` or `flux` against a cluster, and no `jarvis` command that reaches a host, a cluster or a secret. Never decrypt a file or read the age key, a kubeconfig, an SSH key or k3s's own files. The [deny lists](../ci/README.md#coding-agent-deny-lists) block many forms; treat the rest as blocked too. The real guard is that the agent's user holds none of those credentials.
- **No merges, tags, force-pushes or pushes to `main`.** The owner merges every PR and pushes every tag.
- **Core holds no identity.** No env name, hostname, `dns_suffix`, `ssh_user`, owner handle or personal name ([schema/CONTRACT.md](../schema/CONTRACT.md#identity-values)), and no real LAN address or numeric user ID; examples and tests use made-up or documentation ranges. A value that differs per deployment is an env setting, not a role default or a literal. A finding from one env or operator becomes a general rule or fix, never a special case.

## Guards already in place

The deny lists, `jarvis validate` (env schema, Flux identities, committed renders, secrets), and the scripts listed in `ci/rule-checks.txt`, each described by the first comment in `ci/checks/<name>`. Don't restate their rules in prose or tests.

## Subtract first

- **Net lines lead the PR.** Paste the four lines this prints as the body's first lines. Tooling is everything outside the other three, lockfiles left out. Growth in tests, docs or tooling needs one sentence on what a user gets from it; the reviewer sends back growth without one.

  ```bash
  git fetch -q origin main
  net() { printf '%-8s %s\n' "$1" "$(shift; git diff --shortstat origin/main...HEAD -- "$@")"; }
  net product core addons infra jarvis schema ':!*.md'
  net tests ci/tests
  net docs '*.md'
  net tooling . ':!core' ':!addons' ':!infra' ':!jarvis' ':!schema' ':!ci/tests' ':!*.md' ':!*.lock'
  ```

- **A new mechanism names what it replaces.** A script, wrapper, generator, cache, check or workflow says what it removes, or why the upstream tool can't do the job.
- **Use the upstream tool as it is.** Flux, kustomize, Helm, Kyverno, Renovate, JSON Schema and Ansible modules do their own jobs, even where a custom check would be stricter. Don't model their behaviour in Python.
- **One home per fact.** Derive a value in code. Never keep two copies equal with a check, a test or a regex, and never keep an old message alive so old tests pass.
- **Tests check behaviour.** No tests on wording, file names, config shape, CI layout or docs. A guard that protects data, a host or a secret gets a seeded test that makes it fail; other guards don't need one. A test goes with the code it covers.
- **Build for what exists.** Nothing for a consumer, adopter or feature that isn't here yet. Hardening ideas and edge cases go in the PR body as notes.
- **Prefer deleting.** The PR body says what you deleted and what else you looked at deleting.
- **Contracts are tables.** A component's `CONTRACT.md` says what it offers, what it expects and how its health is checked, and changes with the component in the same PR. Rationale goes in a code comment or the PR, not the contract.

## Safety values

Say in the PR how a change meets each one it touches.

- **Validate first.** Every input check runs before the first host, cluster or file change.
- **Re-runs converge or refuse.** A second run changes nothing. A step that would overwrite something other state depends on (a key, a password, a CA) refuses without an explicit flag and has a written way back.
- **Smallest blast radius.** Each credential, service account and RBAC rule covers only what its user needs. Nothing creates the identity it runs as.
- **Any layout.** Placement, ordering and schedules work on one node and on mixed nodes with taints. A new schedule reports "not run yet" instead of failing.

## Gates

Never change a gate in the PR that has to pass it: a required check, the CI job layout, a time limit, a phase gate, or what a check covers. A gate change is its own PR, titled `Gate: …`.

## Workflow

- **One session, one batch.** A batch is one step of the current work, one to three PRs. Start from a fresh checkout of `main`, on a branch `agent/<topic>`, with no stacked PRs. When the batch's PRs are open, report and stop.
- **Only the main agent writes.** Subagents may search, read and review; only the session's main agent edits, commits and pushes.
- **Before pushing:** `bash ci/run-checks`, `bash ci/lint` and the tests near the change. CI at the head commit is the record. After two failures on the same problem, stop and report.
- **Before calling it ready:** read the whole diff for leftovers (debug output, commented-out experiments, env literals) and retitle the PR to what landed. From then on scope is frozen; new ideas go to a follow-up PR.
- **Review by another session.** A session that did not build the PR reviews it with [review.md](review.md) and posts its verdict as a review comment. The owner merges on green CI and an APPROVE.

## Report

The PR body follows `.github/pull_request_template.md` and stays under about 400 words; the diff carries the detail. When handing off, give each PR's link and merge order, and anything the owner must do: what, where, when and why.
