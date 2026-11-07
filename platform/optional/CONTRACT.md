# CONTRACT: optional core pieces

Core pieces that run only in an env that names them. Each folder here is named for its switch under `optional` in `envs/<env>/values/switches.yaml`. An add-on is something else: a limited-tenant swappable service ([services/CONTRACT.md](../../services/CONTRACT.md)).

## Offers

- `app-hosting/`: the hosted-app Component and per-app wiring, with its own [CONTRACT.md](app-hosting/CONTRACT.md).
- `offsite-copy/`: CronJob `restic-offsite`, the nightly copy of every local repository to the env's off-host backend.
- `apps-backup-server/`: Deployment and Service `restic-apps`, the rest-server that holds only hosted apps' repositories.

What the two backup pieces do, and how to run them, stays in [platform/backups/CONTRACT.md](../backups/CONTRACT.md) (Off-host copy, App credentials), beside the always-on backup plumbing they extend.

## Expects

- Nothing always-on lists a folder here: not `platform/kustomization.yaml`, `platform/configs/kustomization.yaml` or `platform/backups/kustomization.yaml`. Only an env's rendered Flux wiring does, once the env names it.
- The always-on `platform/backups` is up: the two backup pieces use its namespace and its restic data.

## Health

App hosting's health checks are in its own contract, and the two backup pieces' in [platform/backups/CONTRACT.md](../backups/CONTRACT.md). An env that does not list a piece has none of its objects, and that is healthy.

## Config

To enable a piece, list its name under `optional` in `values/switches.yaml`, run `tools/lib/render_switches.py --env <env>`, then commit and push. `ci/checks/env-schema` fails an unknown name and a committed wiring that differs from the render. A new piece adds one row to `OPTIONAL_COMPONENTS` in `tools/lib/_switches.py` and no bundle to any env.

### Observability

Observability (metrics, logs, dashboards, uptime checks and alerts) is an optional core component, not an add-on. It stays off in an env until that env lists `observability` under `optional`, by the same steps as above.

It is not built yet. `observability` has no row in `OPTIONAL_COMPONENTS`, so env-schema and the render refuse the name today and no env can turn it on. The build adds that row and puts its files in `platform/optional/observability/`. Core never depends on it: core's own health is `make verify`.

## Proven by

- `ci/tests/test_switches.py` (each backup piece renders from here only when named, and an unknown name, `observability` included, fails before the render writes anything).
- `ci/tests/test_restic_rest_server.py`, `ci/tests/test_backup_isolation.py` and `ci/tests/test_rest_server_isolation.py` (what the two pieces do).
