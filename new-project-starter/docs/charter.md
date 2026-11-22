# Charter

Every session on this project starts from this file. Only the owner changes what's decided. A session edits only **Where things stand**, once, at the end of its batch.

<!-- Fill in each section that has a comment, and delete the comment as you do. Keep the file to what later work needs. -->

## What this is

<!-- Two to four sentences: what it is, who it's for, and what it does for them. Then one line on what it is not. -->

## Done when

<!-- The few outcomes that mean the first release is done, each one checkable. Keep the three below unless you have a reason not to. -->

1. **Day 1:** someone with only the README gets from a clean machine to a working result, and CI runs those same steps.
2. **Fast feedback:** `ci/check` runs in under 3 minutes.
3. **Everything pinned:** every dependency, tool and action is pinned, and Renovate moves the pins.

## How decisions are made

These are the tests a choice must pass; [AGENTS.md](../AGENTS.md) has how code is written.

- **Derive, don't sync.** Each fact has one source.
- **Use the upstream tool as it is.**
- **Build for a user that exists.**
- **Subtract first.**
- **Weigh before building.** The gain must beat the cost in code, setup steps and upkeep. Each option set aside gets one line in the PR.
- **Nothing heavy or home-brewed** where a plain, maintained tool does the job.
- **Say what moved.** Every departure from this charter goes in the PR, with what it gives up.

## Decided

<!-- One entry per decision, newest last:
- **Topic** (decided YYYY-MM-DD). The decision, in a sentence or two. Set aside: the option not taken, and why.
-->

## How the work runs

- **Batches.** The owner starts each builder session with one batch of the current step ([AGENTS.md](../AGENTS.md), Workflow). Renovate PRs are handled between batches, not during one.
- **The standing reviewer prompt,** the same every time, so no PR is missed:

  > Review every open PR that has no APPROVE at its current head. Skip Renovate PRs whose commits are all Renovate's own, and a PR whose only commits since its APPROVE are clean **Update branch** merges of `main`. Use `docs/review.md`, against `docs/charter.md`. For a PR with an earlier review, check that the changes it asked for are done, then review only the commits since. Post one review comment per PR, then reply with one line per PR: verdict, head commit, merge order.

- **Renovate:** patches, digests and pins merge themselves overnight once every check is green. Minors and majors wait for the owner, who clicks **Update branch** and merges each one on green. One that stays red after a re-run goes to a session to fix or close.
- **Follow-ups are tagged with where they land:** a step in Next, their own named trigger, or the sweep. Before each release, one batch takes the sweep list and fixes or deletes each item. If the list passes 10, the next batch is the sweep.
- **Releases:** only a release PR leads to a tag ([releasing.md](releasing.md)).
- **Audits,** before a major release: one session per question (does the code guess? does each switch work both ways? what's left over?). An audit reports and fixes nothing. The owner picks which findings to fix. A finding that only moves code, without removing lines or risk, is dropped.
- **Size is measured one way:** <!-- the commands, e.g. test lines: git ls-files 'tests/*' | xargs cat | wc -l; doc words: git ls-files '*.md' | xargs cat | wc -w -->. Targets go under Watch, and growth needs a reason.

## Where things stand

*Updated by each batch. Last updated YYYY-MM-DD (what changed, in one line).*

**Released:** none yet.

**In flight:** nothing.

**Next, in order:**

1. <!-- The next step, then the ones after it. -->

**Follow-ups,** by where they land:

- *Their own trigger:* none. <!-- - **Item** (where it came from), when <the trigger>. -->
- *The sweep:* empty.

**Watch** (no task; check when a batch touches the area): <!-- the size targets, and anything uncertain -->

**Deferred,** each with what brings it back: none.

**Deleted** (don't bring these back without a new reason): none.

**Open decisions for the owner:** none.
