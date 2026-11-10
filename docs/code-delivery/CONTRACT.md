# CONTRACT: Code delivery provider

One contract for what core needs from the code host, so core and app hosting depend on this file, not on a vendor name. The only provider is `github`. No env setting selects it.

## Offers

| Capability | What it provides | GitHub surface |
| --- | --- | --- |
| **Repo hosting** | Authoritative Git remotes for `jarvis` and `jarvis-apps`. Flux `GitRepository` sources read only from here. | Repos under the account that owns `jarvis` and `jarvis-apps`. |
| **CI** | Workflow runs on the provider's hosted runners. The default branch requires a green `ci` check. | GitHub Actions (`.github/workflows/ci.yml`). The `ci` job runs the checks and pytest. |
| **PR open and merge** | Pull requests for all changes. Coding agents open them with a user token; the repo owner merges them unless a task brief explicitly lets the agent. | GitHub Pull Requests. |

## Expects

- Both repos exist on the provider before env values reference them.
- Flux reconciles cluster state from Git only. Agents do not write to the cluster directly (the deny lists in `.claude/settings.json` and `.cursor/cli.json`).
- Git holds no plaintext secret.

## Health

Not a single daemon. Health means:

- Flux can fetch default branches and reports `GitRepository` and `Kustomization` Ready.
- The required check `ci` is configured on `main` and passes on the latest commit.

## Config

There is no env setting. Provider-specific URLs, org, and tokens are bootstrap or workflow secrets, not duplicated in core.
