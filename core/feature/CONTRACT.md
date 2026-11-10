# CONTRACT: optional core pieces

Core pieces that run only in an env that names them. Each folder here is named for its switch under `features` in `envs/<env>/values/switches.yaml`. An add-on is something else: a limited-tenant swappable service ([addons/CONTRACT.md](../../addons/CONTRACT.md)).

## Offers

- `app-hosting/`: the hosted-app Component and per-app wiring, with its own [CONTRACT.md](app-hosting/CONTRACT.md).
- `offsite-backup-copy/`: CronJob `restic-offsite`, the nightly copy of every local repository to the env's off-host backend. What it covers depends on the env's target: a dead disk only if the target is off that disk, and losing the site only if the target is at another site.
- `apps-backup-server/`: Deployment and Service `restic-apps`, the rest-server that holds only hosted apps' repositories.

What the two backup pieces do, and how to run them, stays in [core/platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md) (Off-host copy, App credentials), beside the always-on backup plumbing they extend.

## Expects

- Nothing always-on lists a folder here: not `core/platform/kustomization.yaml`, `core/platform/configs/kustomization.yaml` or `core/platform/backups/kustomization.yaml`. Only an env's rendered Flux wiring does, once the env names it.
- The always-on `core/platform/backups` is up: the two backup pieces use its namespace and its restic data.

## Health

App hosting's health checks are in its own contract, and the two backup pieces' in [core/platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md). An env that does not list a piece has none of its objects, and that is healthy.

## Config

To enable a piece, list its name under `features` in `values/switches.yaml`, run `jarvis render --env <env>`, then commit and push. the `env-schema` check (`jarvis validate`) fails an unknown name and a committed wiring that differs from the render. A new piece adds one row to `OPTIONAL_COMPONENTS` in `jarvis/env/switches.py` and no bundle to any env.

### Observability

Observability (metrics, logs, dashboards, uptime checks and alerts) is a feature, not an add-on. It stays off in an env until that env lists `observability` under `features`, by the same steps as above.

It is not built yet. `observability` has no row in `OPTIONAL_COMPONENTS`, so env-schema and the render refuse the name today and no env can turn it on. The build adds that row and puts its files in `core/feature/observability/`. Core never depends on it: core's own health is `jarvis verify`.

## Proven by

- `ci/tests/test_switches.py` (each backup piece renders from here only when named, and an unknown name, `observability` included, fails before the render writes anything).
- `ci/tests/test_restic_rest_server.py`, `ci/tests/test_backup_isolation.py` and `ci/tests/test_rest_server_isolation.py` (what the two pieces do).
