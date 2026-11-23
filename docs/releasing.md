# Releasing core

Core ships as tags (`v<major>.<minor>.<patch>`). An env runs exactly one: `core_git_ref` in
`envs/<env>/values/cluster-vars.yaml` and the core `GitRepository` in `flux/sync.yaml` name
`refs/tags/<tag>`. Nothing an env runs follows `main`.

## Cut a release

1. On `main`, rename `## Unreleased` in `CHANGELOG.md` to `## <tag>` and open a new empty
   `## Unreleased` above it. A minor release lists every env change under **Upgrading**, and until
   v0.9.0 a patch release may list them too. In the
   same PR, set `JARVIS_VERSION` in [quickstart.md](quickstart.md)'s choices to `<tag>`.
2. Merge that, then tag the merge commit and push the tag:

   ```bash
   git tag -a <tag> <sha> -m "<tag>"
   git push origin <tag>
   ```

3. `.github/workflows/release.yml` publishes the GitHub release with that section as its notes.
   A tag with no section fails the workflow; add the section and re-run it.
4. The tag also runs `e2e` with the cluster's core source at the tag itself, and `quickstart`:
   the Quickstart's commands as written, installing the tag. A red run means the release is
   broken: fix it on `main` and cut a patch release.

## Move an env to a release

1. Follow the release's **Upgrading** notes in the instance repository, up to the move.
2. Set `core_git_ref` to `refs/tags/<tag>`, run `jarvis render`, which writes the core source's
   ref from it, and merge. Renovate's core PR does both.
3. Wait until `jarvis verify` passes on the new tag before any step on the cluster, an
   **Upgrading** step included. For up to one Flux interval after the merge, Flux reads Ready
   on the old core's content; `verify` fails until every layer has applied the new tag.

Every jarvis command runs the release the env pins, from a cached checkout of the tag when the
installed CLI is not it, and refuses an env that follows a branch. `jarvis --core <path>` runs a
local checkout anyway, with a warning, for a deliberate run of unreleased core.
