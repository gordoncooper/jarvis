# Concepts

The ideas behind Jarvis, one section each. For the commands, start with the [Quickstart](quickstart.md).

## Three repositories, three levels of trust

| Repository | Holds | Trust |
| --- | --- | --- |
| **Core** (this one) | k3s and Flux setup, the platform, the features, the add-ons, the `jarvis` CLI and its checks | Trusted: the maintainer's |
| **Instance** (yours, made by `jarvis init`) | One folder per cluster under `envs/`: its values, inventory and sealed secrets, plus the Flux wiring `jarvis render` writes from them | The owner's: every change is a decision they merge |
| **Apps** (`jarvis-apps`, or your own) | Hosted apps: anyone's software, one folder per app, released by tag | Untrusted: held to its app file by admission policies |

An instance repository can hold many clusters. Each cluster pins its own core release and its own app versions, and moves when its owner merges the pull request that bumps the pin.

## Four layers

| Layer | Where | On when | Examples |
| --- | --- | --- | --- |
| Platform | `core/platform/` | Always | Flux, Traefik, cert-manager and the cluster CA, Kyverno, backups, the `testapp` canary |
| Feature | `core/feature/` | The env lists it under `features` | App DNS, app hosting, the off-host backup copy, the apps backup server |
| Add-on | `addons/` | The env lists it under `addons` | A service people use, namespaced, as its own tenant |
| Hosted app | the apps repository | The env lists it under `apps` | Anything, held by admission |

Dependencies point down only: a feature relies on the platform, an add-on on both, an app on all three. Turning a feature or add-on off leaves everything below it working.

## An env is decisions only

`envs/<env>/values/` holds what the owner decides: the cluster's DNS suffix, its features, add-ons and apps, and app grants. `inventory/hosts.yml` holds the nodes and their roles, and `secrets/` the sealed secrets. Everything under `flux/` is `jarvis render` output: nobody edits it, and a render replaces it whole. `jarvis validate` checks the values against JSON Schema (`schema/`) and the rules that span files.

## One release, everywhere

Every `jarvis` command runs the core release the env pins. When the installed CLI is a different release, it runs a cached checkout of the pinned tag, with that release's own tools (`mise.lock`) and Python packages (`uv.lock`). CI, the cluster and the operator therefore always run the same core. Upgrading is merging a pin bump. Renovate opens the pull request, and core's instance workflow renders and checks it.

## Checks run in three places

1. **On commit:** the pre-commit hook `jarvis init` installs runs `jarvis render --check` and `jarvis validate`.
2. **Before any change to a cluster:** `jarvis up` and `jarvis site` refuse an env that fails validation or whose render is stale, whatever CI said.
3. **In the cluster:** a bad commit fails Flux's build, and Flux keeps the last good state applied.

CI runs the same `jarvis validate`, so it is a convenience, never the only safety.

## Secrets

Secrets are sealed in git with SOPS and age, each to two keys: a **working key** on the controller, and a **recovery key** kept offline. Either opens every secret. The cluster gets the working key once, as `flux-system/sops-age`, and decrypts at apply time. See [secrets.md](secrets.md).

## Backups

Every PVC is backed up by default. VolSync snapshots each one with restic to a rest-server on the env's `backup` node, and an off-host copy can follow. `jarvis drill` restores a scratch volume to prove the chain, and `jarvis restore` puts real volumes back. See [core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md).

## Proving it works

- `jarvis verify` reads the cluster's standing state: Flux, nodes and roles, pods, backups, and HTTPS through Traefik to the canary.
- `jarvis verify --smoke` also uses each capability once (a certificate, App DNS, app admission) and removes what it made.
- `jarvis drill` proves backup and restore.
- Core's CI brings up a real cluster from every change (e2e), and runs the [Quickstart](quickstart.md) as written.
