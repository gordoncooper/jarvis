# Changelog

One section per release, newest first. A minor release lists what an env must do to upgrade
under **Upgrading**. An env pins core with `core_git_ref: refs/tags/<tag>` in
`values/cluster-vars.yaml` and the core `GitRepository` in `flux/sync.yaml`; moving the pin is
the upgrade.

## Unreleased

### Added

- `jarvis init [<dir>] --env <name> --domain <suffix> --repo-url <url> --node <host>[:<roles>]`
  writes a whole instance repository: the env's values and inventory, `.sops.yaml`, a README,
  the Renovate config and CI caller (`--no-ci` skips them), its minted secrets and `ca.crt`, a
  sealed read-only deploy key for an `ssh://` root (its public half printed for the forge), the
  rendered `flux/`, and a pre-commit hook. The env passes `jarvis validate` as soon as init
  returns, and pins the core release init ran from. `jarvis env add <name>` adds another env to
  the same repository. Omitted values are asked for at a terminal.
- An offline **recovery key**. init encrypts every secret to two age recipients, the working key
  on the controller and a recovery key whose private half it prints once and keeps nowhere
  (docs/secrets.md). Losing the working key is no longer fatal.
- `jarvis doctor` installs the pre-commit hook in a clone that lacks it, and warns while an env's
  secrets open for one age key only.
- The age recipients over an env's life (docs/secrets.md, Changing recipients and rotating keys):
  `jarvis secrets --add-recovery-key` gives an env made before v0.5.0 its recovery key;
  `--update-recipients` re-encrypts every sealed file to the rule's recipients after a change;
  `--rotate age` makes a new working key in place of the old or lost one (from the recovery key,
  when the working key is gone); `--rotate deploy-key` seals a new Flux deploy key. Each changes
  only the instance repository and prints the cluster steps that follow.
- `jarvis validate` fails when a sealed file is not SOPS-encrypted with age, or does not open for
  exactly the recipients its env's rule names.

- The `jarvis` CLI, one command for an instance repository, replacing the Makefile's operator
  targets and `tools/`. Run it from inside the instance repository: `jarvis doctor`, `validate`,
  `render`, `secrets`, `encrypt`, `age-key-check`, `node prep <host>`, `site`, `up`, `verify`,
  `drill` and `restore`. `--env` is needed only when the repository holds more than one env, and
  `INSTANCE=` is gone. Each command runs at the core release its env pins: this checkout when it
  is that tag, otherwise a checkout of the tag cached in `~/.cache/jarvis/core/<tag>`, set up from
  that release's own lockfiles. So the pin check is no longer a refusal to work around:
  `ALLOW_UNPINNED=1` is gone, and `--core <path>` runs a local checkout on purpose. Exit codes:
  0 ok, 1 a check failed, 2 usage, 3 refused for safety, 4 the environment is wrong.
- `jarvis up`: doctor, validate, render --check, secrets, a refusal while the env's files are not
  what Flux reads, site, a wait for every Flux Kustomization to be Ready, then verify.
- `jarvis doctor`: the pinned CLIs, the age key's mode and owner, the instance remote, and that
  every inventory host resolves.
- `mise run deps` (mise.toml's `deps` task) is the one way a core checkout installs itself:
  `make deps` and the CLI's cached checkouts both run it.

- One pinned toolchain for operators, CI and the app-check Action. `mise.toml` pins every CLI
  (sops, age, kubectl, flux, kustomize, kubeconform, kyverno, gitleaks, restic, rest-server, uv,
  and CI's k3d, actionlint, kube-apiserver and etcd), and `mise.lock` holds each one's checksum for
  Linux and macOS on amd64 and arm64. `pyproject.toml` and `uv.lock` pin every Python package with
  hashes. Renovate updates each lock in the same PR as the pin, so no checksum is bumped by hand.
  CI fails when a lock does not match its pins. `make deps` needs only mise; it installs Python
  3.12 for Ansible too, so a macOS or arm64 controller works. The flux CLI is the cluster's
  Flux release (a test checks they match), so `flux` commands no longer warn about a version skew.

- `make site` takes the first etcd snapshot on a new cluster (`k3s etcd-snapshot save`, when
  the snapshot directory is empty), so the backup chain and a drill have something to use
  before k3s's first scheduled snapshot (00:00 or 12:00 by default).

### Fixed

- A core-only env without `app-dns` passes the flux-prune check: app-dns's CoreDNS forward is
  required to be guarded when present, not to be present. Found by the first env init wrote.
- A drill on a cluster k3s has not snapshotted yet, or whose snapshot the restic copy does not
  hold yet (no repository, or no snapshot in it), says so instead of failing with
  `pod platform-backups/drill-etcd-restore failed`. Any other failure of that pod prints its
  log before cleanup deletes it.
- A pinned app tag newer than the local apps checkout's last fetch no longer stops prep-node,
  render or the checks: they fetch that checkout's tags once and read the app at the tag.

### Removed

- `envs/env-00`. Core ships no env: its tests and env-level rule checks run on a sample
  instance written by init's own code (`ci/tests/sample.py`), and e2e makes its env the same
  way. The example env's special cases in the schema and the checks are gone with it (it had to
  set every key and role, and needed no creation rule and no secrets). Its secrets README is
  `docs/secrets.md`.
- `ci/checks/env-schema`, `app-hosting`, `backup-isolation`, `flux-identity`, `flux-prune` and
  `flux-substitute`: `ci/checks/sample-instance` runs all of `jarvis validate` on the sample.
- `tools/`, the Makefile's operator targets (`site`, `verify`, `drill`, `drill-cleanup`, `restore`,
  `restore-all`, `bootstrap-secrets`, `encrypt`, `age-key-check`, `render`), `ALLOW_UNPINNED`,
  `ci/run-env-checks` and `ci/checkout-apps-for-instance.sh`: the jarvis CLI replaces them. The
  Makefile keeps `deps`, `venv` and `ci-verify` for core development.
- `tools/install-deps`, `tools/versions.env`, `requirements-dev.txt`, `ci/fetch-test-apiserver`
  and the `python-pins` check: mise, uv and their lockfiles replace them.

### Changed

- `jarvis node prep` no longer creates the age key or writes `.sops.yaml` (init does both). It
  checks, before it touches the host, that the key at `age_key_file` is one of the env's
  recipients, and refuses otherwise. The example env's placeholder `ssh_user` is gone with
  env-00: the user init records is the Ansible user.
- The manifest checks require `core/` and `addons/` and scan `envs/`
  when it is there.
- The jarvis package is grouped by domain: `jarvis.env`, `apps`, `backups`, `render`, `secrets`,
  `node`, `schema`, `verify`, `checks` and `cli`. `jarvis/lib/` is gone.
- The instance workflow runs `jarvis validate` for each env with the core it pins, and the render
  on a Renovate PR is `jarvis render`. The CLI checks out the apps repository itself. An instance
  workflow at this release needs every env at this release or later, which the grouped core PR
  does.
- Generated files say `Generated by jarvis render`; the next render rewrites them.
- The coding-agent deny lists name the jarvis commands that reach hosts, a cluster or secrets.
- The k3s role reads the node's memory from `ansible_facts['memtotal_mb']`, not the injected
  `ansible_memtotal_mb` variable that ansible-core 2.24 drops (the `INJECT_FACTS_AS_VARS`
  deprecation warning in `make site`).
- The etcd snapshot copy (CronJob `platform-backups/etcd-snapshots`) runs hourly on its own
  schedule instead of five minutes after k3s's default snapshot time, so it no longer depends
  on k3s's snapshot schedule. `restic backup --skip-if-unchanged` makes a restic snapshot only
  when k3s wrote a new one, and `forget --prune` prunes only when `forget` removed one.
  `make verify` now expects a successful copy within about two hours.
- The instance preset makes a core release one PR: every env's `core_git_ref`, the instance
  workflow and the preset's own ref (group `jarvis core`). An app release still opens one PR
  per env, now named after the env (`renovate/env-01-notes-0.x`), not its `values` folder.
- The instance workflow's header adds `workflow_dispatch:` to the caller, to re-check on
  demand.

### Upgrading

- Install mise on the controller (https://mise.jdx.dev/getting-started.html). Then delete the
  binaries the old installer left in `~/.local/bin` (`sops age age-keygen kubectl kustomize
  kubeconform kyverno gitleaks restic rest-server`; keep `mise`): a shell finds them before mise's,
  so after the next version bump a hand-typed command would run the old one. A service started
  from one of them (an off-host rest-server, say) needs a copy of its own first
  (core/platform/backups/CONTRACT.md, off-host copy).
- Install the jarvis CLI on the controller, once: `mise use -g uv`, then
  `uv tool install git+https://github.com/<owner>/jarvis@v0.5.0`. From then on run `jarvis`
  from inside the instance repository in place of `make … ENV=… INSTANCE=…` from a core
  checkout: `make site ENV=env-01 INSTANCE=<path>` is `jarvis site`, `make restore APP=<ns>` is
  `jarvis restore --app <ns>`, `make bootstrap-secrets` is `jarvis secrets`, and so on
  (`jarvis --help`). The core checkout on the controller is no longer needed for operating an env.
- Give each env its recovery key, from inside the instance repository with the working key at
  `age_key_file`: `jarvis secrets --add-recovery-key`. Store the key it prints once offline,
  then commit and push the re-encrypted files. Until then `jarvis doctor` warns. The keep-safe
  list in docs/secrets.md is now two items; the working key copy in the password manager is no
  longer one of them.
- In the instance CI caller, add `workflow_dispatch:` under `on:`. Renovate moves the preset
  ref and the workflow pin in the core release's PR.

## v0.4.0

### Added

- `.github/workflows/instance.yml`, CI for instance repositories, called with
  `uses: <owner>/jarvis/.github/workflows/instance.yml@<tag>`, so an instance carries no CI
  logic of its own. It checks each env with the core its `core_git_ref` pins, checked out at
  `.core/`. On a Renovate PR it renders each env at that core and pushes the result with a
  GitHub App's token (`RENDER_CLIENT_ID`, `RENDER_PRIVATE_KEY`), so a core, app or k3s bump
  arrives with its generated files and CI runs on the complete PR. GITHUB_TOKEN cannot do this
  push: a push made with it starts no workflow. Without the App, the check names the stale files.
- `make venv`: only the Python venv with the jarvis package, enough for `make render`.

### Changed

- Core's Python is one package, `jarvis/` (`jarvis.lib`, `jarvis.checks`, `jarvis.schema`,
  `jarvis.verify`), described by `pyproject.toml`. `make deps` installs it editable into the
  venv; no module edits `sys.path`.
- CI lints with ruff and type-checks the package with pyright (`ci/lint`, pinned in
  `requirements-dev.txt`, configured in `pyproject.toml`).
- Core ships the Flux wiring every env runs: `core/flux` holds the FluxInstance (Flux's version
  pin), the Flux identities and the `platform-controllers` and `platform-configs` layers, paths
  by `${jarvis_env}`. An env's `flux/kustomization.yaml` lists it as `../../../core/flux`.
  Renovate bumps Flux in core; the instance preset no longer reads a FluxInstance.
- `tools/render --env <env>` (or `make render ENV=<env>`) renders an env's whole `flux/` from
  its values and replaces it, so a file core moved or retired leaves on the next render. It
  works on a copy: values it refuses change nothing. `--check` (`CHECK=1`) lists each file under
  `flux/` that is missing, extra or different, and writes nothing. It replaces
  `tools/lib/render_*.py`, and `flux/sync.yaml` is now rendered from `cluster-vars.yaml` too.
- e2e builds its env from only `values/`, `inventory/hosts.yml` and `.sops.yaml`: bootstrap-secrets
  and `tools/render` generate the rest, so a new env is its hand-written files plus encrypted secrets.
- Names: the `optional` section of `values/switches.yaml` is `features`, and `tools/seal` /
  `make seal` are `tools/encrypt` / `make encrypt`. The old names are gone, not aliased.
- Layout: core's universal part is `core/` (`core/flux`, `core/platform`, and `core/feature` for
  the features, which were `platform/optional/`), and add-ons are top-level `addons/` (was
  `services/`). The root source includes the core repository twice, `fromPath: core` and
  `fromPath: addons`, so Flux paths are `./core/...` and `./addons/...`.
- The add-on contract states the feature-or-add-on rule: an add-on ships nothing cluster-scoped
  but its own Namespaces (`ci/checks/addons`); one that needs more is a feature.
- The `hello` sample add-on is a test fixture (`ci/tests/fixtures/addons/hello/`), not part of the
  product: `addons/` ships no add-on, and env-00 lists none.
- JSON Schemas for the four values files under `schema/` (cluster-vars, switches, apps, hosts),
  each named by a `# yaml-language-server: $schema=` line for editors. env-schema validates
  `cluster-vars.yaml`'s keys, types and patterns with its schema (`jsonschema`, pinned), with the
  same messages; cross-field rules stay in Python.
- App DNS is a feature, `app-dns` under `features` in `values/switches.yaml`
  (`core/feature/app-dns/`). Off, the LAN's own DNS names apps: edge hosts keep the resolved
  stub, `app_dns_cluster_ip` is not required, and `make verify` reaches the canary at Traefik's
  address. e2e runs core both ways.
- Core sets the k3s release every env runs (`k3s_version` and `k3s_sha256` in
  `infra/ansible/group_vars/all.yml`); an env overrides it by setting both in `cluster-vars.yaml`.
  Renovate bumps core's.
- The instance preset reads `core_git_ref` only, not `flux/sync.yaml` (rendered from it), and
  lists the render commit's author in `gitIgnoredAuthors`, so Renovate keeps rebasing a PR the
  workflow rendered.
- Checks read core from a tree laid out like the root artifact (core/ beside envs/), else from
  the core running them. A core checked out at an instance's `core/` is no longer looked for.

### Fixed

- Backup selection with no apps checkout fails with the missing-checkout error instead of a
  `TypeError`.

### Upgrading

- Run `make deps` again in the core checkout, so the venv has the package.
- In `values/switches.yaml`, rename `optional:` to `features:`; the old name is refused. To keep
  App DNS, add `app-dns` under `features`. Use `make encrypt` where you used `make seal`.
- Move the env's `core_git_ref` to `refs/tags/v0.4.0` and run `tools/render --env <env>` from a
  checkout of the tag, in one commit: the render replaces `flux/` whole, so
  `flux/flux-instance.yaml`, `flux/identities.yaml` and the two platform layer files leave, and
  `sync.yaml` gains the two includes.
- Replace the instance's CI workflow with the call to core's (the header of
  `.github/workflows/instance.yml`), move `renovate.json`'s preset to `#v0.4.0`, and, for
  rendered Renovate PRs, create the GitHub App described in `docs/deploy.md`.
- To run core's k3s release, delete `k3s_version` and `k3s_sha256` from `cluster-vars.yaml`;
  to keep your own, leave both.
- Optional: add `# yaml-language-server: $schema=https://raw.githubusercontent.com/<owner>/jarvis/<tag>/schema/<name>.schema.json`
  as the first line of each values file, for editor checks.

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
