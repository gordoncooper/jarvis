# Review: PR #148 (rulebook standards)

Head `f158c79`, base `a755052`. Checked against the brief and the plan's merge and timebox rules (project-plans `framework.md`, R36 and R35). Line numbers are the rulebook's at head. CI was still running at the time of review.

## Verdict: FIX (one BUG, then GO)

## Checks

1. **Section: landed as written.** It matches the round-3 text word for word apart from "The reviewer". It sits after "Stay lean".
2. **Plan-code removals: one changes meaning (BUG below).** The rule-id labels, `D34's` and `(R35)` all go without losing a rule; each line still states its rule. The timebox line (`:49`) is accurate, short and owner-agnostic. Its "done means evidence" sentence matches the coding agent's half of R35, and the live-run half is already at `:23`.
3. **Plan-codes check: correct.** `bash ci/checks/plan-codes` passes and `test_plan_codes.py` has 9 passing tests at head. With `rulebooks/` added back to `_EXCLUDED_PREFIXES`, `test_plan_codes_in_rulebooks_fail` fails (`[] == [...]`), so the test guards the change.

## BUG

**`:43`, the merge rule allows agent merges the plan reserves for the owner.**
- As written, a gated or code-factory PR only "needs the repo owner's admin-approved label first", after which an agent may merge it. The plan says the owner merges PRs the admin gate holds, and code-factory PRs stay draft until the owner labels and merges them.
- It also drops two things the gate depends on: the label counts only if the owner added it after the latest push (every push removes it), and an agent never adds it.
- That second gap matters most for a Claude Code agent run locally under the owner's GitHub login, because a label it adds would count as the owner's.

Suggested text:

> Until branch protection requires the admin-gate check, an agent may merge its own PR when required checks pass. After that, a PR that touches a protected path, removes a Namespace, PVC or StorageClass, or comes from the code-factory GitHub App needs an `admin-approved` label the repo owner added after the latest push, and the owner merges it. Never add that label yourself, even when you run under the owner's account. Merge only after the reviewer has ticked every Ready to merge box that applies at the current head (a box marked "(n/a)" counts as met). A brief's "don't merge" always wins.

## NITs

- **`:13`, open question on the plan-codes one-liner.** Yes, widen it. The check now scans every Markdown file except `AGENTS.md` and `.github/`, so "Plan codes stay out of Markdown docs, READMEs and `docs/` (not `AGENTS.md` or `.github/`)" says what it enforces.
- **"code-factory app", open question.** Keep it. The code factory is product machinery, so an adopter's copy has it too. Write "GitHub App" so it isn't read as one of the user's apps; this is already in the suggested text above.
- **`:17`, open question on the deferred line.** Dropping the phase name is right, but "a later phase" gives an agent no trigger. Write "don't add a model-name rule to `core-names` until a brief asks for it."
- **`:44`.** "deny list blocks many of these" should now be "the deny lists block many of these".
- **`_plan_codes.py`, follow-up only.** The `.github/` prefix is unreachable, because the walk already skips dot-directories, as the PR notes. Delete it in a later PR or leave it; not this one.
