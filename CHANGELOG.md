# Changelog

One section per release, newest first. A minor release lists what an env must do to upgrade
under **Upgrading**. An env pins core with `core_git_ref: refs/tags/<tag>` in
`values/cluster-vars.yaml` and the core `GitRepository` in `flux/sync.yaml`; moving the pin is
the upgrade.

## Unreleased

### Changed

- Core's Python is one package, `jarvis/` (`jarvis.lib`, `jarvis.checks`, `jarvis.schema`,
  `jarvis.verify`), described by `pyproject.toml`. `make deps` installs it editable into the
  venv; no module edits `sys.path`. The commands under `tools/` and `tools/lib/render_*.py` stay
  where they were.
- CI lints with ruff and type-checks the package with pyright (`ci/lint`, pinned in
  `requirements-dev.txt`, configured in `pyproject.toml`).

- Core ships the Flux wiring every env runs: `flux/` holds the FluxInstance (Flux's version pin),
  the Flux identities and the `platform-controllers` and `platform-configs` layers, paths by
  `${jarvis_env}`. An env's `flux/` keeps `sync.yaml`, `kustomization.yaml` (which lists
  `../../../core/flux`), `kustomizations/services.yaml`, `platform-configs/` and its generated
  files. Renovate bumps Flux in core; the instance preset no longer reads a FluxInstance.
- App DNS is an optional component, `app-dns` under `optional` in `values/switches.yaml`
  (`platform/optional/app-dns/`, applied with `platform-configs`). Off, the LAN's own DNS names
  apps: edge hosts keep the resolved stub, `app_dns_cluster_ip` is not required, and `make
  verify` reaches the canary at Traefik's address. e2e runs core both ways.
- Core sets the k3s release every env runs (`k3s_version` and `k3s_sha256` in
  `infra/ansible/group_vars/all.yml`); an env overrides it by setting both in `cluster-vars.yaml`.
  Renovate bumps core's.
- `flux/sync.yaml` is generated from `cluster-vars.yaml` (`render_switches.py`), and env-schema
  requires the render, so the Flux sources' URLs and refs have one source.

### Fixed

- Backup selection with no apps checkout fails with the missing-checkout error instead of a
  `TypeError`.

### Upgrading

- Run `make deps` again in the core checkout, so the venv has the package.
- In each env, delete `flux/flux-instance.yaml`, `flux/identities.yaml`,
  `flux/kustomizations/platform-controllers.yaml` and `flux/kustomizations/platform-configs.yaml`;
  and render again (`tools/lib/render_switches.py`), which now writes `flux/sync.yaml` too. Move the env's core pin in
  the same commit: the env's `flux/` and core's now depend on each other.
- To keep App DNS, add `app-dns` under `optional` in `values/switches.yaml` and render again.
- To run core's k3s release, delete `k3s_version` and `k3s_sha256` from `cluster-vars.yaml`;
  to keep your own, leave both.

## v0.3.0

### Added

- `core_git_ref` pins a tag. `make site`, `verify`, `drill` and `restore` refuse a checkout that is
  not the env's tag (another commit, uncommitted changes, or a branch ref); `ALLOW_UNPINNED=1`
  warns instead.
- A pushed `v*` tag publishes a GitHub release whose notes are that tag's section here.
- Hosted apps run at a pinned tag: `versions:` in `values/apps.yaml` pins each listed app
  (`<app>: v<x.y.z>` for the apps repository's tag `<app>/v<x.y.z>`). The wiring deploys that tag,
  and eligibility and app checks read the app at it. A merge to the apps repository changes no env
  until its pin moves.
- `renovate/instance.json`, a Renovate preset for instance repositories: one PR per core tag, k3s,
  Flux and app pin.
- The app-check Action (`.github/actions/app-check`): an apps repository checks every app folder
  against a core tag's rules with `uses: <owner>/jarvis/.github/actions/app-check@<tag>`.
  `tools/app-check --repo <dir>` and `tools/install-deps <tool>...` back it.
- `flux_known_hosts` in `cluster-vars.yaml`: the SSH host keys Flux trusts for an `ssh://` root
  (unset: GitHub's published key).
- `ENVS=<env>...` limits the env-level checks to those envs.
- An e2e job brings a k3d cluster up with Flux from the PR's commit (on a release tag, core at the
  tag), runs env-00's hosted apps at their pinned tags, `make verify` and `make drill`, and is a
  required check.

### Upgrading

- Set `core_git_ref` (cluster-vars) and the core `GitRepository` `spec.ref.name` (`flux/sync.yaml`)
  to `refs/tags/<tag>`, and run tools from a checkout of that tag.
- An env that lists apps adds `versions:` to `values/apps.yaml` with a pin for each, then renders
  again (`tools/lib/render_switches.py`). The instance's apps checkout needs the apps repository's
  tags (`ci/checkout-apps-for-instance.sh` fetches them).
- An instance using the Renovate preset adds a rule naming its apps repository for the app pins
  ([docs/deploy.md](docs/deploy.md), step 9).

## v0.2.0

### Changed

- Kyverno policies are CEL types (`policies.kyverno.io/v1`): GeneratingPolicies `pvc-backup`,
  `pvc-backup-app`, `app-derive`, `app-derive-backups` and MutatingPolicy `app-priority`. No
  ClusterPolicy remains, so Kyverno 1.20 needs no port.
- App namespaces set `volsync.backube/privileged-movers` themselves; `kyverno:backup-namespaces` is gone.
- The agent rulebook is `agents/coding-agents.md`; the deny lists are one short list per client.
- pytest runs on every CPU (`pytest-xdist`).

### Removed

- The `apps` Flux layer (`core/apps`, `kustomizations/apps.yaml`, `flux-apps`) and the placeholder
  ConfigMaps.
- `llm_provider_host`, the chat-bot slot contract, and the planned code-delivery rows.
- `ci/checks/core-names`, `plan-codes` and `cli-deny`, and `tools/make-plan`. CI no longer needs `OWNER_NAME`.

### Upgrading

- Delete `flux/kustomizations/apps.yaml`, the `flux-apps` objects in `flux/identities.yaml` and
  `llm_provider_host`, then re-render (`tools/lib/render_switches.py --env <env>`).
- Delete any sealed file nothing reads; env-schema now fails one.
