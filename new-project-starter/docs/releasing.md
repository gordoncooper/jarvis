# Releasing

Releases are tags, `v<major>.<minor>.<patch>`. Only a release PR leads to one, and a tag never moves.

## Cut a release

1. **Sweep first.** The batch before a release takes the charter's sweep list.
2. **Open the release PR.** It renames `## Unreleased` in `CHANGELOG.md` to `## <tag>`, opens a new empty `## Unreleased` above it, and sets any version the docs name to the tag. Nothing else.
3. **Try the upgrade before the tag.** Where the project has a real install or deployment, move it to the release PR's checkout and follow the **Upgrading** notes as written. Fix the notes wherever the real move needs a step they don't list.
4. **Merge, then tag the merge commit:**

   ```bash
   git tag -a <tag> <sha> -m "<tag>"
   git push origin <tag>
   ```

5. **A broken release gets a new patch,** fixed on `main`. The broken tag stays.

## Versions

- **Patch:** fixes, with nothing a user must do.
- **Minor:** new behaviour. Anything a user must do goes under **Upgrading**.
- **Major:** a break in what users rely on, from 1.0 on. Before 1.0, a minor may break things, and its **Upgrading** says how to move.
