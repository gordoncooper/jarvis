# CONTRACT: Code delivery provider

One contract for six capabilities so core, the code-factory, and app hosting depend on this file, not on a vendor name. An env may select a provider in `envs/<env>/values/cluster-vars.yaml`; only built providers pass env-schema validation. Manifests do not branch on `code_delivery` until a second provider is built.

**Built provider:** `github` (per `<env>` in `cluster-vars.yaml`). **Planned second provider:** Forgejo. There is no Forgejo implementation in this repo.

## Offers

Callers may rely on these six capabilities from the active provider. Rows marked planned are not built yet.

| Capability | What it provides |
| --- | --- |
| **Repo hosting** | Authoritative Git remotes for `jarvis` and `jarvis-apps`. Flux `GitRepository` sources read only from here. |
| **CI** | Workflow runs on the provider's hosted runners. The default branch requires a green `ci` check. The planned `admin-gate` check will also be required on protected paths. |
| **Issue and label queue** | Planned. Issues and labels on `jarvis-apps` for the code-factory work protocol (`code-factory:ready`, `code-factory:hard`, `code-factory:failed`). |
| **PR open and merge** | Pull requests for all changes. Routine jarvis work: coding agents open and merge via a user token when checks pass. Planned: the code-factory lane job never opens PRs; a publish job opens a **draft** PR with a short-lived App token. A human admin merges code-factory PRs. |
| **Approval-label check** | Planned. `ci/checks/admin-gate` as a required check. It fails risky PRs unless the `admin-approved` label was added by the human admin's own account. Every push strips that label. It runs on `pull_request_target`, reads metadata only, and never checks out PR code. A label added by the coding-agent account fails. |
| **Image registry** | Planned. An OCI registry for images CI builds. App images are public packages so clusters pull without a registry secret. The workflow is `build.yml`. |

### GitHub mapping (only implementation)

| Capability | GitHub surface |
| --- | --- |
| Repo hosting | Repos under the account that owns `jarvis` and `jarvis-apps`. |
| CI | GitHub Actions (`.github/workflows/ci.yml`). The required check is `ci`. |
| Issue and label queue | Planned. GitHub Issues and the labels API on `jarvis-apps`. |
| PR open and merge | GitHub Pull Requests. A coding-agent user token for agent PRs. Planned: a code-factory GitHub App for draft PRs and checker check runs. |
| Approval-label check | Planned. Branch protection plus the required check `admin-gate`. |
| Image registry | Planned. GHCR, published by `build.yml`. |

## Expects

- When set, `code_delivery` is `github` in `values/cluster-vars.yaml`, validated by the env schema (`schema/CONTRACT.md`). Omitting it is valid.
- Both repos exist on the provider before env values reference them.
- Flux reconciles cluster state from Git only. Agents do not write to the cluster directly (`ci/checks/cli-deny`).
- Credentials for the planned code-factory jobs stay outside CI: the lane job holds a Cursor key and a read-only GitHub token; the publish job holds a short-lived App token; the builder has no push credentials (`ci/checks/secret-scan`).

## Health

Not a single daemon. Health means:

- Flux can fetch default branches and reports `GitRepository` and `Kustomization` Ready.
- The required check `ci` is configured on `main` and passes on the latest commit.
- Planned: `admin-gate` is required on `jarvis` and `jarvis-apps`.

## Config

Env selection uses **lowercase snake_case** in `envs/<env>/values/cluster-vars.yaml`:

| Field | Required | Notes |
| --- | --- | --- |
| `code_delivery` | no | When set, `github` only. `forgejo` is the planned second provider. Any other value fails env-schema validation. |

No core manifest reads `code_delivery` until a second provider is built. Provider-specific URLs, org, and tokens are bootstrap or workflow secrets, not duplicated in core.

## Proven by

- `ci/checks/env-schema` rejects a `code_delivery` value other than `github`.
- `.github/workflows/ci.yml` is the `ci` check. It runs every `ci/checks/` rule.
- Planned: `admin-gate` fails a protected-path PR without `admin-approved` from the human admin account, passes after that label, and fails for a bot, an App, or the coding-agent account.
- Planned: `build.yml` publishes the demo app as a public package, and the cluster pulls it by digest.
- Planned: a `code-factory:ready` issue becomes a draft PR with a green `ci` check and a `checker` check run.
