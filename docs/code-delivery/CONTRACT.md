# CONTRACT: Code delivery provider

What core needs from the code host, so core depends on this file and not on a vendor. The only provider is `github`; no env setting selects it.

## Offers

| Capability | GitHub surface |
| --- | --- |
| Repo hosting: the Git remotes Flux reads | Repositories for `jarvis`, the instance and `jarvis-apps` |
| CI: a required green `ci` check on the default branch | GitHub Actions (`.github/workflows/ci.yml`) |
| Pull requests for every change; the owner merges | GitHub Pull Requests |

## Expects

| What | Rule |
| --- | --- |
| Repositories | Exist before env values name them |
| Cluster state | From Git only; agents never write to a cluster (the deny lists in `.claude/settings.json` and `.cursor/cli.json`) |
| Secrets | None in plain text in Git; provider tokens are workflow secrets |

## Health

| Check | Pass |
| --- | --- |
| Flux | GitRepositories and Kustomizations Ready |
| `ci` | required on `main`, passing on the latest commit |
