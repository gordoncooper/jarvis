# Agents

How anyone works on this repository, a person or a coding agent. [docs/charter.md](docs/charter.md) says what to build and what's decided; this file says how. If a rule here looks wrong for the task, say so before working around it. Never depart from one silently.

## Hard stops

- **No merges, tags, force-pushes or pushes to `main`.** The owner merges every pull request and pushes every tag.
- **No secrets.** Never read, print or commit a credential: `.env` files, keys, tokens. A value that differs per deployment is configuration, not a literal in code.
- **No real systems.** No commands against production hosts, databases or accounts. The owner, or someone they choose, runs those.
- **Nothing personal in the code.** No real hostnames, addresses, user names or account IDs in code, docs or tests. Examples use `example.com` and the documentation address ranges.

`.claude/settings.json` blocks many of these. It's a speed bump: the real control is that the agent's user holds no credential that reaches anything real.

## How code is written

- **One home per fact.** Derive a value where it's needed. Never keep two copies equal with a check, a test or a regex, and never keep a hand-kept list of things the code can find for itself.
- **Read the real thing.** Ask the data, the tool or the API. Don't match by name prefix, shape or pattern when something can say for sure.
- **Use the upstream tool as it is.** Don't re-implement or wrap what a dependency already does, even where your own version would be stricter.
- **Fail closed.** An input the code doesn't recognise is refused, with a message that says what to do. It's never guessed at.
- **One way to extend each thing.** When a part takes additions, they all come in by the same route. Don't invent a second route for one case.
- **Build for what exists.** Nothing for a user, feature or scale that isn't here yet. Ideas go in the PR body as notes.
- **Subtract first.** If deleting something gets the same result, delete it. A new script, wrapper, cache, check or workflow names what it replaces.
- **The old way goes in the same PR.** No fallback, alias, shim or second path, until there are outside users to protect.
- **Tests check behaviour,** not wording, file names, config shape or CI layout. A guard that protects data or a secret gets a test that makes it fail. A test lives with the code it covers.
- **Docs say what is, not how it got here.** History goes in CHANGELOG and the PR, and the reason for surprising code goes in a comment beside it. A doc changes with its code, in the same PR.

## Behaviour is measured

A refactor changes no test's expected result. When an expected result does change, behaviour changed: the PR's **Behaviour** section lists it, and so does CHANGELOG if a user can see it. Where the project generates output (files, a schema, an API), the PR shows that output's diff between `main` and the head.

## Gates

A gate is a required check, the CI job layout, a time limit, or what a check covers. Never change one in the PR that has to pass it. A gate change is its own PR, titled `Gate: …`, and the owner decides it.

## Workflow

- **One session, one batch.** A batch is one step of the current work: one to three PRs, from a fresh `main`, each on its own branch `agent/<topic>`, none stacked on another. When they're open, report and stop.
- **Only the main agent writes.** Subagents may search, read and review.
- **Before pushing,** run `bash ci/check`. CI at the head commit is the record. After two failures on the same problem, stop and report.
- **Before calling it ready,** read the whole diff for leftovers: debug output, commented-out code, TODOs, personal values. Retitle the PR to what landed. From then on its scope is frozen, and new ideas go to a follow-up.
- **Another session reviews** each PR with [docs/review.md](docs/review.md). The owner merges on green CI and an APPROVE.

## Report

The PR body follows `.github/pull_request_template.md`, opens with the lines `bash ci/net-lines` prints, and stays under about 400 words; the diff carries the detail. A hand-off to the owner ends with two short parts:

- **For you:** exactly what the owner must do, where and when, or "nothing beyond merging".
- **Next:** the one next step, written as the prompt for the next session.
