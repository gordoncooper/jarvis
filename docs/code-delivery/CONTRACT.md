# CONTRACT: Code delivery provider

One contract for six capabilities so core, the code-factory, and app hosting depend on this file—not on a vendor name (R34). Each env selects a provider in `envs/<env>/values/cluster-vars.yaml`; only built providers pass env-schema validation. Manifests do not branch on `code_delivery` until a second provider ships (§5).

**Built provider:** `github` (per `<env>` in `cluster-vars.yaml`). **Planned second provider:** Forgejo (§9); no Forgejo implementation in this repo.

## Offers

Callers may rely on these six capabilities from the active provider:

| Capability | What it provides |
| --- | --- |
| **Repo hosting** | Authoritative Git remotes for `jarvis` and `jarvis-apps`; Flux `GitRepository` sources read only from here (R34). |
| **CI** | Workflow runs on the provider’s hosted runners; default branch requires a green `ci` check (and `admin-gate` after the P2 exit, R36). |
| **Issue and label queue** | Issues and labels on `jarvis-apps` for the code-factory work protocol (`code-factory:ready`, `code-factory:hard`, `code-factory:failed`; R40). |
| **PR open and merge** | Pull requests for all changes. Routine jarvis work: coding agents open and merge via a user token when checks pass (until P2 exit; R36). Code-factory: lane job never opens PRs; a publish job opens a **draft** PR with a short-lived App token (R42). Human admin merges code-factory PRs (R40). |
| **Approval-label check** | `ci/checks/admin-gate` as a required check from the P2 exit (R36). It fails risky PRs unless the `admin-approved` label was added by the human admin’s own account; every push strips that label. Runs on `pull_request_target`, metadata only, never checks out PR code (D35). |
| **Image registry** | OCI registry for images CI builds. App images are public packages so clusters pull without a registry secret (app hosting, §5). |

### GitHub mapping (only implementation)

| Capability | GitHub surface |
| --- | --- |
| Repo hosting | Repos under the account that owns `jarvis` and `jarvis-apps` (D1). |
| CI | GitHub Actions (`.github/workflows/`). |
| Issue and label queue | GitHub Issues and labels API on `jarvis-apps`. |
| PR open and merge | GitHub Pull Requests; coding-agent user token for agent PRs; code-factory GitHub App for draft PRs and checker check runs (R42). |
| Approval-label check | Branch protection plus required check `admin-gate` (`p2-24`). |
| Image registry | GHCR (§4). |

## Expects

- `code_delivery: github` in `values/cluster-vars.yaml`, validated by the env schema (`schema/CONTRACT.md`).
- Both repos exist on the provider before env values reference them (D1).
- Flux reconciles cluster state from Git only; no hand-edited cluster copies of what Git should hold (R34).
- Credentials live outside CI where R25/R42 require (lane job: Cursor key and read-only GitHub token; publish job: short-lived App token; no push creds on the builder).

## Health

Not a single daemon. Health means:

- Flux can fetch default branches and reports `GitRepository` / `Kustomization` Ready.
- Required checks on `main` are configured and pass on the latest commit.
- After P2: `admin-gate` is required on `jarvis` and `jarvis-apps` and matches R36 behavior.

## Config

Env selection uses **lowercase snake_case** in `envs/<env>/values/cluster-vars.yaml`:

| Field | Required | Notes |
| --- | --- | --- |
| `code_delivery` | yes | `github` only. `forgejo` is the planned second provider (§9); any other value fails env-schema validation. |

No core manifest reads `code_delivery` until a second provider is built. Provider-specific URLs, org, and tokens are bootstrap or workflow secrets (§8), not duplicated in core.

## Proven by

| Capability | Checkpoint |
| --- | --- |
| Repo hosting | P0a exit: every Flux object Ready; rollback drill restores prior state (`p0a-16`). |
| CI | P0a exit: required `ci` runs every `ci/checks/` rule; seeded bad PRs fail each rule as it lands (`p0a-05`–`p0a-10`). |
| Issue and label queue | P4b: `code-factory:ready` actor check, ignored App label, `code-factory:failed` on timeout/cap (R40). |
| PR open and merge | P0a: agents merge own green PRs before P2; P4b: toy `code-factory:ready` issue becomes a draft PR with green `ci` and a `checker` check run. |
| Approval-label check | P2 exit: seeded protected-path PRs fail without `admin-approved` from the human admin account and pass after it; bot/App fixture actors fail; label from coding-agent account fails (`p2-24`, D38). |
| Image registry | P4a: `build.yml` publishes the demo app; package public; cluster pulls by digest (`p4a-04`, `p4a-07`). |

Also: `ci/checks/env-schema` rejects unknown `code_delivery` values (P0a seeded PR).
