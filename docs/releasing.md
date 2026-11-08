# Releasing core

Core ships as tags (`v<major>.<minor>.<patch>`). An env runs exactly one: `core_git_ref` in
`envs/<env>/values/cluster-vars.yaml` and the core `GitRepository` in `flux/sync.yaml` name
`refs/tags/<tag>`. Nothing an env runs follows `main`.

## Cut a release

1. On `main`, rename `## Unreleased` in `CHANGELOG.md` to `## <tag>` and open a new empty
   `## Unreleased` above it. A minor release lists every env change under **Upgrading**.
2. Merge that, then tag the merge commit and push the tag:

   ```bash
   git tag -a <tag> <sha> -m "<tag>"
   git push origin <tag>
   ```

3. `.github/workflows/release.yml` publishes the GitHub release with that section as its notes.
   A tag with no section fails the workflow; add the section and re-run it.
4. The tag also runs `e2e` with the cluster's core source at the tag itself. A red run means the
   release is broken: fix it on `main` and cut a patch release.

## Move an env to a release

1. Follow the release's **Upgrading** notes in the instance repository.
2. Set `core_git_ref` and the core source's `spec.ref.name` to `refs/tags/<tag>`, and merge.
   env-schema checks the two match.
3. Run operator commands from a core checkout of that tag (`git checkout <tag>`). `make site`,
   `verify`, `drill` and `restore` refuse any other commit, uncommitted changes, or an env that
   follows a branch (`tools/pin-check`). `ALLOW_UNPINNED=1` runs anyway, with a warning, for a
   deliberate run from a branch.
