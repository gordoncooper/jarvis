# Project name

One paragraph: what this is, who it's for, and what it does for them.

## Quickstart

The fewest commands that take a clean machine to a working result. CI runs these same commands, so they can't drift.

## Working on it

- [docs/charter.md](docs/charter.md): what this project is, what's decided, and where it stands.
- [AGENTS.md](AGENTS.md): how anyone, a person or an agent, changes the code.
- [docs/review.md](docs/review.md): how a pull request is reviewed.
- [docs/prompts.md](docs/prompts.md): the prompts that start each kind of session.
- [docs/releasing.md](docs/releasing.md): how a release is cut.

Before pushing, run `bash ci/check`. CI runs the same script.

## License

Name the license here, with its text in `LICENSE`.

---

## Starting a project from this folder

Delete this section once the steps are done.

This folder is a blank repository's first commit: the rules, checks and templates that keep a codebase clean from its first line. It holds no code and assumes no language.

1. **Copy it in,** dot files included: `cp -R new-project-starter/. <new-repo>/`.
2. **Fill in the blanks:**
   - this README: the name, the paragraph and the Quickstart;
   - `docs/charter.md`: What this is, and Done when;
   - `ci/check`: your stack's format, lint and test commands (it fails until you do);
   - `ci/net-lines`: your product and test paths;
   - `renovate.json`: your time zone;
   - a `LICENSE` file.
3. **Set the repository up on GitHub:**
   - a ruleset on `main`: changes only through pull requests, the `ci` check required, branches up to date before merging, no force-pushes or deletion;
   - one merge method (merge commits keep each PR's reviewed commits);
   - auto-merge off, since Renovate merges its own PRs;
   - private vulnerability reporting on (Security tab), and secret scanning with push protection where your plan has it;
   - the Renovate app installed on the repository.
4. **Start the sessions** in [docs/prompts.md](docs/prompts.md), Getting started: the advisor fills in the charter with you, then the first builder sets the stack. That first PR holds your stack's tools, pinned, in `ci/check` and the workflow, with one real test, and nothing else.

Using a coding agent other than Claude Code? Add its deny list beside `.claude/settings.json`, saying the same things, and change both in one PR.

Keep the charter in one place: here in `docs/charter.md`, where each batch updates Where things stand in its own last PR, or as a Project doc if you keep plans out of the repository (then point the links in AGENTS.md and `docs/review.md` at it). Not both.

### What each file guards against

| File | What it keeps from going wrong |
| --- | --- |
| `AGENTS.md`, `CLAUDE.md` | Each session inventing its own rules; an agent merging, tagging or touching a secret |
| `docs/charter.md` | Decisions re-argued in every session; work drifting from the goal; follow-ups lost |
| `docs/review.md` | The builder grading its own work; growth, guessing and second copies slipping in |
| `docs/prompts.md` | Each session started a little differently, so steps get skipped |
| `.github/pull_request_template.md`, `ci/net-lines` | Unexplained growth; behaviour changing without anyone saying so |
| `ci/check`, `.github/workflows/ci.yml` | Checks that differ between a laptop and CI; a secret committed, even one deleted later |
| `renovate.json` | Pins going stale, or dependencies changing under you unpinned |
| `.claude/settings.json` | An agent merging, force-pushing or reading a credential by accident |
| `CHANGELOG.md`, `docs/releasing.md` | Releases nobody can upgrade to safely; tags that move |
| `.editorconfig`, `.gitattributes`, `.gitignore` | Whitespace and line-ending noise in diffs; secrets and local files committed |
| `SECURITY.md` | Vulnerabilities reported in public |
