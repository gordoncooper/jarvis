# Prompts

The prompts that start each kind of session. Paste them as they are, filling in the `<…>` parts. Every session starts from the charter, and every hand-off ends with **For you** and **Next** ([AGENTS.md](../AGENTS.md)).

## Getting started

Three sessions, in this order, once the README's setup steps are done.

**1. The advisor,** once, in a session that will stay with the project. The advisor keeps the charter, writes each session's prompt and reviews each batch's results, but writes no code:

> You're the advisor on `<project>`: you keep its charter, write the prompt for each session from `docs/prompts.md`, and check each batch against the charter. You don't write code. Read the charter, `AGENTS.md`, `docs/review.md` and `docs/prompts.md`. Then interview me, one question at a time, until you can fill in the charter's What this is and Done when, and its first Decided entries: the stack, and any other choice a wrong guess would make expensive. For each choice, give me your recommendation, with one line for each option you set aside. Then write Next as small steps, each one batch of one to three PRs, starting with the stack batch. Write the charter, and end with **For you** and **Next**.

**2. The stack batch,** the first builder:

> First batch: set the stack. Read the charter and `AGENTS.md`. From a fresh `main`, in one PR: fill in `ci/check` with the stack's format, lint and test commands; install the tools in `.github/workflows/ci.yml`, each pinned (a version plus a lockfile or a checksum); set the paths in `ci/net-lines`; and add one real test that runs real code, such as a `--version` or a health check. Fill in the README's Quickstart with commands that work now. Nothing else. Update Where things stand, and end with **For you** and **Next**.

**3. The first review,** with the standing reviewer prompt below. From then on, it's builder, review, merge, for each step in Next.

## Standing prompts

**Builder,** one batch:

> Run the next batch in the charter: `<the step>`. Read the charter and `AGENTS.md` first. `<What the step must do and must not do, and anything the last review asked for.>` From a fresh `main`, in one to three PRs. Re-read the charter right before you write it, and change only Where things stand. End with **For you** and **Next**.

**Reviewer,** the same every time, so no PR is missed:

> Review every open PR that has no APPROVE at its current head. Skip Renovate PRs whose commits are all Renovate's own, and a PR whose only commits since its APPROVE are clean **Update branch** merges of `main`. Use `docs/review.md`, against the charter. For a PR with an earlier review, check that the changes it asked for are done, then review only the commits since. Post one review comment per PR, then reply with one line per PR: verdict, head commit, merge order.

**Renovate sweep,** for a Renovate PR that stays red after one re-run, or a major version:

> Renovate sweep: for each open Renovate PR that is a major version, or whose checks still fail after one re-run, find out whether the update itself needs a change. If it does, push one fix commit to that PR's branch, and say it needs the standing review before it merges. If the update is wrong for this project, close it with a one-line reason, and add a Renovate rule only if it would come back. Reply with one line per PR: merge, fixed or closed.

**Release,** for `<tag>`:

> Release batch for `<tag>`. Read the charter, `AGENTS.md` and `docs/releasing.md`. From a fresh `main`, first take the charter's sweep list: fix or delete each item. Then open the release PR exactly as `docs/releasing.md` says. Before opening it, try its Upgrading notes as far as you can without touching a real system (a dry run, a validation, or an install of the previous release in CI), and fix the notes wherever the move needs a step they don't list. Update Where things stand, and end with **For you** and **Next**.

**Audit pass,** read-only, one question per session:

> Audit pass `<n>`: `<the question>`. Read the charter and `AGENTS.md`. From a fresh `main`, answer the question with findings. Give each one where it is (file and line), what it is, why it matters, and a fix that deletes or derives rather than adds, sized S, M or L. Anything that only moves code is not a finding. Report only: change no code and open no PR. Write the findings to `<the audit doc>`, then update Where things stand. Stop.

**Operator,** for steps on a real system, which only the owner approves:

> `<Owner>` approves these steps on `<system>`, and only these: `<numbered steps, each with what it should show>`. Log every command and its output to `<path>`. Change nothing else. If a step shows something other than what's expected, stop and report it; don't fix it. Reply with one line per step.
