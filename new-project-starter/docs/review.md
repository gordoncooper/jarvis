# Reviewing a pull request

For a session that didn't build the PR. Read [the charter](charter.md), then [AGENTS.md](../AGENTS.md), then the whole diff. Answer each question yes or no, with one line of evidence (file and line).

1. **Size.** Does it add more lines than it removes in tests, docs or tooling? If so, does the body tie the growth to something a user gets?
2. **Second home.** Does any fact now live in two places, kept equal by a check, a test, a regex or a hand-kept list? Does the old way still exist beside the new one?
3. **Upstream copy.** Does it re-implement or wrap what a dependency or tool already does? Name the code.
4. **Guessing.** Does it match by name, prefix or shape where it could read the real thing, or guess where it could refuse?
5. **Text tests.** Does a new or changed test check wording, file names, config shape or CI layout instead of behaviour?
6. **Gate moved.** Does it change a required check, the CI job layout, a time limit, or what a check covers?
7. **Speculative.** Does it build for a user, feature or scale that doesn't exist yet?
8. **Behaviour.** Does a test's expected result, an output, a flag or an API change without the PR's Behaviour section listing it?
9. **Deviation.** Does it depart from the charter? If so, does the body say what was given up?
10. **Deletion.** Could the same result come from deleting something? Name it.
11. **Leftovers.** Debug output, commented-out code, TODOs, personal or environment-specific values.

**Verdict.** APPROVE only when 2 to 8 and 11 are "no", and 1, 9 and 10 are answered. A "yes" on 6 is fine only in a PR titled `Gate: …`. Otherwise CHANGES, as a numbered list, smallest fix first.

Post the verdict as a review comment that starts with APPROVE or CHANGES. When PRs are opened under the owner's account, GitHub won't let that account approve its own PR, so the comment is the record:

```bash
gh api repos/{owner}/{repo}/pulls/<number>/reviews -f event=COMMENT -F body=@verdict.md
```
