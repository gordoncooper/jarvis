# Reviewing a pull request

For a session that did not build the PR. Read the brief or charter you were given, then [coding-agents.md](coding-agents.md), then the whole diff. Answer each question yes or no, with one line of evidence (file and line).

1. **Size.** Does it add more lines than it removes in tests, docs, tooling or Python? If yes, does the body tie the growth to something a user gets?
2. **Second home.** Does any fact now live in two places, kept equal by a check, a test or a regex?
3. **Upstream copy.** Does it re-implement or wrap what git or one of the tools under "Use the upstream tool as it is" in coding-agents.md already does? Name the code.
4. **Text tests.** Does a new or changed test check wording, file names, config shape or CI layout instead of behaviour?
5. **Gate moved.** Does it change a required check, the CI job layout, a time limit, a phase gate, or what a check covers?
6. **Speculative.** Does it build for a consumer, adopter or feature that does not exist yet?
7. **Deviation.** Does it depart from the brief or charter? If so, does the body say what was given up?
8. **Deletion.** Could the same result come from deleting something? Name it.
9. **Operator steps.** After this PR, does a new env, a node, or a Renovate merge take more steps than before?
10. **Leftovers.** Plan or rule codes, env-specific literals, dead code, commented-out experiments.

**Verdict.** APPROVE only when 2, 3, 4, 5 and 6 are "no" and 1 and 7 are answered. A "yes" on 5 is fine only in a PR titled `Gate: …`. Otherwise CHANGES, as a numbered list, smallest fix first.

The PRs are opened under the owner's account, and GitHub does not let an account approve its own PR, so post the verdict as a review comment. Write it to a file, starting with APPROVE or CHANGES, then post it through the REST API, which works where `gh pr review` (GraphQL) is blocked:

```bash
gh api repos/{owner}/{repo}/pulls/<number>/reviews -f event=COMMENT -F body=@verdict.md
```
