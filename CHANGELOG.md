# Changelog

One section per release, newest first. A minor release lists what an env must do to upgrade
under **Upgrading**. An env pins core with `core_git_ref: refs/tags/<tag>` in
`values/cluster-vars.yaml` and the core `GitRepository` in `flux/sync.yaml`; moving the pin is
the upgrade.

## Unreleased

### Changed

- `jarvis site` hands Ansible what validation resolved, as one extra-vars file: the env's
  cluster-vars with the schema's defaults, the instance, the working age key's path and the env's
  `.sops.yaml` recipients. The roles no longer work these out or restate the schema's defaults
  and checks, so an `ansible-playbook site.yml` run by hand with `-e @cluster-vars.yaml` stops
  at the first value it lacks. Run `jarvis site`.
- `flux_known_hosts` defaults to GitHub's published key in the schema, where the flux-operator
  role kept it. For an `ssh://` root on another host without it, `jarvis validate` now prints
  `flux_known_hosts holds no key for <host>, the host of flux_git_repo_url`.
- A `lan-port` grant may take 5001/TCP and 51820-51821/UDP: core turns on neither k3s's
  embedded registry nor flannel's WireGuard backend, so nothing on the node holds them.
- `jarvis restore` finds VolSync's mover Job by its controller owner, the ReplicationSource,
  rather than by working out VolSync's Job name.
- An instance's Renovate reads `values/cluster-vars.yaml` and `values/apps.yaml` as YAML (core's
  preset now uses JSONata managers), so it finds the core pin whatever the order of
  `core_git_repo_url` and `core_git_ref`, and takes no pin from a comment. It opens the same PRs.
- `jarvis init --with-examples` sets `apps_git_repo_url` to the repository
  `jarvis/kit/examples-apps.yaml` names, not a `jarvis-apps` beside core's URL.

## v0.8.3

### Changed

- Each command's help names it (`usage: jarvis render …`) and no longer lists `--env`, which is
  still taken as before; its errors start `jarvis <command>:`. `jarvis help <command>` shows that
  command's help, and `init --help` and `env add --help` name `--core-url` and `--core-ref`.
  `jarvis node prep <host>` is unchanged.
- `jarvis secrets --rotate-restic` is `jarvis secrets --new-restic-password`, which is what it
  does: it mints a new password for a lost `backups-restic` file, and that password opens no
  existing repository. The restic rotation is still `docs/backups.md`'s procedure.
- `jarvis restore` no longer reads `AS_OF` from the environment; `--as-of` is the one way.
- `jarvis init` and `env add` write no `$schema` editor hint at the top of each values file. It
  named the release `init` ran from, and nothing moved it. A hint in an existing file is a
  comment: delete it or leave it.
- `python -m jarvis.render.apps`, `jarvis.render.cluster_vars` and `jarvis.render.switches` no
  longer run: `jarvis render` writes the whole `flux/`, where `jarvis.render.switches` wrote part of it.
- `jarvis validate` holds every env to what `jarvis init` and `env add` make: `values/switches.yaml`,
  the sealed `k3s-token`, `cert-manager-ca` and `backups-restic`, one `.sops.yaml` rule, and each
  feature's, add-on's and app's sealed files. It no longer checks less for an env with no sealed
  file. `jarvis node prep` runs that whole check before it touches the host, not a lighter one.
- `APPS_REPO` must be a git checkout holding each pinned tag, as the one the CLI fetches is: every
  app is read at its tag, never as the folder holds it.
- `jarvis restore` and `jarvis drill` run the restic image that `etcd-snapshots.yaml` names in the
  env's pinned core. Restore no longer reads the live CronJob, and neither checks the image's shape.
- The `addons` check no longer guesses an object's scope from its kind; the tenant's namespaced
  Roles keep an add-on from anything cluster-scoped.
- `age_key_file` is required, with no default: `jarvis init` writes the path of the key it used,
  and `jarvis env add` the first env's. `SOPS_AGE_KEY_FILE` still overrides it, in `doctor` too,
  and `init` and `env add` refuse when it names another file than the one they would record.
- `jarvis validate` checks that `jarvis_env` is a DNS label, as `init` required, and fails any
  `.sops.yaml` creation rule that is not exactly the one jarvis writes for an env folder. jarvis
  writes `.sops.yaml` as plain YAML, so the next `env add` or recipient change re-lays it out.
- A quoted `dns_suffix` or `age_key_file`, or one with a comment, works in `secrets` and
  `node prep`.
- `jarvis node prep` installs the key ssh offers for the Ansible user (`ssh -G`), proves login as
  Ansible makes it, resolves the host without `getent`, and leaves `flux/` alone.
- `jarvis up` and `jarvis doctor` read `flux_git_repo_url`, not the clone's `origin`.
- `jarvis` runs only inside a git repository with `envs/` at its top.
- `jarvis init` and `env add` run from a core checkout need `--core-url` and `--core-ref`.
- `jarvis init` asks for `ssh_user` at a terminal, else writes none (the schema's `ubuntu`).
- `jarvis doctor` warns when mise is missing.
- A controller installs only the CLIs and Python packages the jarvis CLI runs, about 440 MB less
  for each core release it caches: core's test tools (rest-server, k3d, kube-apiserver, etcd) and
  test packages move to `mise.dev.toml` and uv.lock's dev group, which `make deps` installs. An
  instance's CI installs the same set as a controller. actionlint, which nothing ran, is no longer
  pinned.
- The Quickstart installs the CLI with `mise exec uv --` and no longer adds uv to your global mise
  config or asks for `mise activate`; `~/.local/bin` on `PATH` is enough.
- The internal CA is made by the `cryptography` package, not the controller's `openssl`, with the
  same extensions.
- `jarvis validate` has no `flux-prune` check. Its rule is now ValidatingPolicy `flux-prune`
  among the Git-only rules, so a miss prints under `FAIL policies`: what a Flux Kustomization
  applies of a kind Flux keeps carries `kustomize.toolkit.fluxcd.io/prune: disabled`, as before.
  A HelmRelease is exempt when it runs as any account but `flux-platform` (a tenant's). The five
  objects the check also named (the root Kustomization, ClusterRole `kyverno:backup-generate`,
  ConfigMap `coredns-custom`, Deployment `platform-backups/restic`, Traefik's HelmChartConfig)
  keep their annotation where they are written, and are no longer checked by name.
- Core's admission rule `stdout-logs` is gone. It guessed from five flag spellings whether a
  container logs to a file, and could refuse a flag that means something else.
- `jarvis up` validates only its own env, as `jarvis validate` does, so in a repo with envs on
  different core tags each env is checked by the release it pins.
- `jarvis secrets --rotate age` and `--rotate deploy-key` print their `kubectl` step with
  `--kubeconfig ~/.kube/jarvis-<env>.yaml`, so a pasted step reaches the env's cluster, not the
  controller's default one.
- Traefik reads a request on 443 for up to an hour (`readTimeout: 1h` on `websecure`), not
  Traefik 3's 60 s, which cut off an upload that took longer.
- `restic-check` prints `core's password does not open it` for a repository restic can't open
  with core's password (restic's exit 12), such as an app's left on the backup disk once app
  hosting is off. Other failures still print `restic check exited <code>`.

### Upgrading

- An env without `age_key_file`: add your working key's path (the old default was
  `~/.config/sops/age/keys.txt`).
- The controller that runs `jarvis up` must read `flux_git_repo_url` with git, as Flux does (for
  `ssh://`, with a key the forge accepts). `jarvis doctor` checks it.
- In your own scripts and runbooks: `jarvis secrets --rotate-restic` is
  `jarvis secrets --new-restic-password`, `AS_OF=<time> jarvis restore` is
  `jarvis restore --as-of <time>`, and `python -m jarvis.render.apps`, `.cluster_vars` or
  `.switches` is `jarvis render`.

## v0.8.2

### Changed

- Core acts only on its own namespaces, those labelled `jarvis.io/layer` `platform` or
  `feature`: the core door admits routes only from them, and core's backups (`pvc-backup`)
  enroll only their volumes. A namespace with neither that label nor a hosted app's (one made by
  hand) no longer reaches the core door or gets core's backups, and `jarvis verify` fails each of
  its volumes (`no ReplicationSource`) unless it is marked `backup: none`. `jarvis drill` labels
  its own namespace, `platform-drill`.
- `jarvis restore --app <namespace>` is `jarvis restore --namespace <namespace>`. Its pod that
  binds an unbound volume runs at the PriorityClass of the workloads that mount it, so a restore
  into a hosted app's namespace works with `--node` too.
- `jarvis validate` checks core's admission rules as the env's build makes them, so it holds
  what the cluster runs, a feature's exception included. App DNS brings its own exception to the
  `load-balancers` rule, whose message for any other LoadBalancer is now `not a LoadBalancer
  Service: core publishes through Traefik's`.
- `restic-check` prints `no backup credential registered for it` (was `for its app`) for an
  app repository with no registered credential.
- `jarvis verify --smoke` labels its namespace, `platform-smoke`, `jarvis.io/layer: platform`,
  and its App DNS probe waits for its route to read `Accepted` on the core door before it asks
  the name, failing `was not Accepted on platform-gateway/jarvis section core` otherwise.

### Upgrading

- Before moving the pin, list any namespace of yours that is neither core's nor a hosted app's:
  `kubectl get ns -L jarvis.io/layer,hosting.jarvis.io/app`, the ones with both columns empty,
  leaving out k3s's (`kube-*`, `default`) and Flux's (`flux-system`). Move each one's workload
  in as a hosted app, or mark each of its volumes `backup: none`; nothing else is needed.
- Use `jarvis restore --namespace` where a script or runbook says `--app`.

## v0.8.1

### Fixed

- `jarvis site` and `jarvis node prep` work on an env that lists hosted apps. The CLI fetched the
  apps repository only for a fixed list of commands, and their validation reads the app files;
  now the first read of an app file fetches it, whichever command makes it.
- A granted LAN port has a name: App DNS answers `<app>.apps.<dns_suffix>` with the address of
  the app's LoadBalancer Service. Core names each app LoadBalancer (MutatingPolicy
  `app-lan-name`), and admission refuses an app Service that is unnamed or names any other
  hostname. An app LoadBalancer made before this release gets its name the next time it is
  written; for a chart app, once `jarvis verify` passes on this release,
  `flux reconcile helmrelease app-<name> -n flux-system --force`. Run before that, the reconcile
  writes the Service before `app-lan-name` exists, and it stays unnamed.
- `jarvis verify` warns, instead of failing, while a restored volume's backup waits for its
  first scheduled sync.
- `jarvis verify` right after a boot: while a node's kubelet has not renewed its Lease, it prints
  `FAIL too freshly started to judge` and runs no check, instead of passing them on status left
  from before the boot.

## v0.8.0

### Changed

- Core makes each hosted app's web routes. An app file's `network.web` is a list of entries,
  each a Service and its port: HTTPRoute `app-web` serves `<app>.apps.<dns_suffix>`, and a
  named entry's `app-web-<entry>` serves `<app>-<entry>.apps.<dns_suffix>`. An entry's custom
  `hostnames` (in place of `risky.hostnames`) get route `app-hostnames[-<entry>]` once the env
  grants them. An app ships no route; one whose routing a Service and port cannot express marks
  its entry `ownRoute: true` and ships that entry's route, and admission refuses any other route
  an app ships. The example apps are hello-web v0.3.0, notes v0.3.0 and lan-demo v0.2.0.
- A hosted app can run an upstream Helm chart as it is, in place of `deploy/`: its app file's
  `chart` names an `oci://` chart, a version and values, and the env entry's `values` lay over
  them. `jarvis render` reads the app file at its pinned tag and commits the chart source,
  ConfigMap `app-<name>-values` and HelmRelease `app-<name>`, which helm-controller installs as
  the app's tenant into `app-<name>`. The app check and `jarvis validate` render the chart with
  `helm template` and hold its output to the app rules, not core's Kyverno rules; the app-check
  Action installs helm. Dropping the app uninstalls the chart. An image with no chart runs from a
  generic one, such as bjw-s app-template, as lan-demo v0.2.0 now does.
- `values/apps.yaml` holds one entry per app the env runs, keyed by its name: `version`, its
  `grants`, and `backups` (default on). It replaces `apps:` in `values/switches.yaml` and the
  `versions:` and `exceptions:` lists.
- `jarvis validate` and `jarvis render` read each app at its pinned tag and hold it to the app
  rules of the core the env pins, so a core pin move past an app fails the instance's CI. They
  also refuse a web name two apps would both make, a `lan-port` grant on a port core or the node
  holds (22, 53, 80, 443, 2379-2380, 5001, 6443, 8472/UDP, 10250, 51820-51821/UDP), and one
  port granted to two apps.
- An app with `backups` on and a `backup: true` volume needs its own credential: `jarvis secrets
  --env <env>` mints it, `jarvis validate` requires it once the env is bootstrapped, and `jarvis
  verify` fails a volume of it with no ReplicationSource. With `backups: false` nothing is sealed
  or reported.
- The env checks read the apps repository only from `APPS_REPO`, which the jarvis CLI sets; they
  no longer look for a checkout beside the instance.
- `jarvis restore`'s binder pod meets core's rules and an app namespace's baseline (sized, Pod
  Security restricted, PriorityClass `hosted-app`), so it can bind a hosted app's claim.
- `jarvis up` and the pre-commit hook `jarvis init` installs no longer run `jarvis render --check`
  beside `jarvis validate`, which already runs it. A hook installed before keeps working as it is.
- The instance workflow restores the chart and schema cache, as core's CI does, so an instance's
  `jarvis validate` reaches a chart host only for a chart version no earlier run pulled.

### Fixed

- `jarvis node prep` waits up to 10 minutes for apt's lock, so it no longer fails on a new node
  still running unattended-upgrades after boot.
- The flux-operator role's first API call reads no secret, so a wrong kubeconfig shows its error.

### Removed

- An app file's `network.web.healthPath` and `envs.<env>`, and the `no-uptime-check` grant: no
  uptime check read them. The namespace annotations `hosting.jarvis.io/host` and `env` go with
  them; `hosting.jarvis.io/apps-domain` names the apps domain.
- `jarvis secrets --app`: `jarvis secrets` already mints each listed app's missing credential.

### Upgrading

In each env that hosts apps, in one commit:

- Move the core pin and each app's pin together: this core refuses an app file whose `web` is
  not a list of entries. For the example apps that is hello-web v0.3.0, notes v0.3.0 and
  lan-demo v0.1.1 (or v0.2.0, below). Their own routes `hello-web` and `notes` are pruned by each app's
  Kustomization, and core's `app-web` serves the same names.
- Rewrite `values/apps.yaml` as one entry per app: its `versions:` pin as `version`, and its
  `exceptions:` lines as `grants` without the `<app>:` prefix. Delete `apps:` from
  `values/switches.yaml`, and any `no-uptime-check` grant.
- For each app with a `backup: true` volume and no sealed credential (its backups were off),
  run `jarvis secrets --env <env>` to seal one, or set its `backups: false`. A credential sealed
  under an earlier release stays as it is.
- Move the instance's Renovate preset (`renovate/instance.json#<tag>` in `renovate.json`) with
  the core pin, as Renovate's "jarvis core" PR does. The earlier preset reads app pins from
  `versions:`, and in the new `values/apps.yaml` it reads each `version:` line as an app named
  `version`.
- An app that moves from `deploy/` to a chart, as lan-demo v0.2.0 does, moves in its own pair of
  commits: drop it from `values/apps.yaml`, and list it at the new tag once its objects are gone.
  In one commit, its old Kustomization's prune and the chart's install act on the same objects.

Then `jarvis render` and `jarvis validate`. An env that moved the core pin alone fails its app
Kustomizations on the new App schema; move the app pins, or rebuild it.

## v0.7.0

### Changed

- Routes are Gateway API. Core's Gateway `platform-gateway/jarvis` has a core door (listener
  `core`, `*.<dns_suffix>`, for every namespace that is not a hosted app's), and app-hosting
  adds the apps door (`apps`, `*.apps.<dns_suffix>`, and `apps-granted` for granted custom
  hostnames) for app namespaces only. Both share Traefik's address on 443 (shared-address mode).
  Traefik's Ingress and CRD providers are off, so an Ingress routes nothing; testapp, the smoke
  probe and App DNS (`resources HTTPRoute`) use HTTPRoutes. The wildcard Certificates move to
  `platform-gateway`, and TLSStore `default` is gone.
- A hosted app ships an HTTPRoute instead of an Ingress: `hostnames` its own `${app_host}` (or a
  granted custom hostname), `parentRefs` Gateway `platform-gateway/jarvis` section `apps`
  ([add-a-hosted-app.md](docs/add-a-hosted-app.md)). Admission refuses a route with no
  hostnames. The example apps are hello-web v0.2.0 and notes v0.2.0.

### Upgrading

- Move each hosted app to a release that ships an HTTPRoute in the same commit as the core pin,
  never after it: once the core pin moves, the app's account can no longer touch Ingresses, so
  the app's Kustomization fails. For the example apps that is `hello-web: v0.2.0` and
  `notes: v0.2.0` in `values/apps.yaml`; `lan-demo` has no web address and stays.
- An env whose apps shipped an Ingress then sticks: each app Kustomization fails `PruneFailed`,
  because Flux reads each stale Ingress in its inventory as the app's account, which can no
  longer read Ingresses. Rebuild the env, or for each app set `deletionPolicy: Orphan` on
  Kustomization `flux-system/app-<name>` and delete it; its wiring `app-<name>-wiring` makes it
  again with a fresh inventory, which adopts the app's objects.
- An add-on's Ingress becomes an HTTPRoute on the core door, as `core/platform/testapp` shows.

## v0.6.1

### Fixed

- `jarvis verify`'s Flux Ready, and the wait in `jarvis up`, no longer pass on a Ready left from
  before a core upgrade. A new core tag reaches an env's root `GitRepository` as a new included
  artifact under the same revision, and Flux reconciles at once only on a new revision. So each
  Kustomization applies the new core on its next interval (10 minutes for core's layers) and reads
  Ready until then. Both now need every Kustomization to have applied its source's current
  artifact (`status.history`, Flux 2.7 and later), and every HelmRelease and source to be Ready
  for its current spec. Right after a core pin moves, `verify` fails on Flux Ready, naming each
  Kustomization that is behind, until Flux catches up; run it again then.

## v0.6.0

### Added

- The `alerts` feature: one Flux Alert posts every failed Kustomization or HelmRelease reconcile
  to a target the env owner seals in `secrets/alerts-notify.sops.yaml` (key `address`), with
  the Provider type from `alerts_provider` in `values/cluster-vars.yaml` (default `generic`)
  ([deploy.md](docs/deploy.md#alerts)). Every env's cluster-vars ConfigMap now carries
  `alerts_provider`.

### Changed

- Hosted apps' backups belong to app-hosting. The apps rest-server (`restic-apps`) and the
  `pvc-backup-app` policy move to `core/feature/app-hosting/backups/` and come with
  `app-hosting`; core's `pvc-backup` enrolls only the trusted zone. An app's backups are on once
  its credential is sealed (`jarvis secrets --app app-<name>`): the render registers the
  credential of each listed app that has one, and `jarvis secrets` no longer writes it. `jarvis verify` reports an
  app volume without a credential as a warning, from the `apps` component.
- The CLI needs Python 3.13 or later, which uv installs when the controller has an older one.
  Ansible is 15 (ansible-core 2.22).
- Core's rules hold at admission too. `core/platform/kyverno/rules/rules.yaml` is installed on
  `platform-configs` for namespaces labelled `jarvis.io/layer` `platform` or `feature`, and
  `jarvis validate` applies the same file to everything an env deploys. The Git-only backup rule
  stays in `jarvis/checks/git-rules.yaml`. Each core namespace sets its layer and Pod Security
  `enforce` level.
- `jarvis validate` checks what an env deploys with the upstream tools. Two new checks read
  Flux's build of every env path plus each HelmRelease's chart, rendered with `helm template`
  (Helm 4, pinned in `mise.toml`): `kubeconform` checks every object against its schema, and
  `policies` applies core's rules (images pinned by digest, requests and a memory limit, probes,
  stdout logs, LoadBalancer Services, backups) with the Kyverno CLI.
  Both need the network to pull charts and schemas. kubeconform reads custom kinds from the CRDs
  the objects carry and those the pinned flux CLI installs.
- cert-manager, Kyverno and k8s-gateway charts come from OCIRepositories (`chartRef`), like
  flux-operator. VolSync publishes no OCI chart and keeps its HelmRepository.
- The flux-operator image, Kyverno's hook images and cert-manager's startupapicheck resources are
  now set, which the rendered charts showed were missing.
- The node bootstrap installs Helm 4.3.2 (was 3.16.4).
- `jarvis validate` pulls each chart version once into `~/.cache/jarvis/charts` and renders it
  from there, so a second run reaches no chart host. kubeconform reads the CRDs the objects carry
  before Kubernetes' own schemas, so only k3s's Traefik kinds still ask a schema host.
- Core's Renovate reads chart and image pins with its own `flux` and `kubernetes` managers. Regex
  managers stay only where no manager exists: k3s, Flux, the Helm host CLI, mise in the
  devcontainers, the quickstart's release and the example apps.
- Each values file is checked against its own JSON Schema (`schema/*.schema.json`), and the
  messages are the validator's own: `<file>: <key>: <message>`. `inventory/hosts.yml`,
  `values/switches.yaml` and `values/apps.yaml` were checked by hand-written Python that copied
  their schemas; `cluster-vars.schema.json` now also holds the DNS name, URL, path, NTP pool and
  known-hosts patterns. A required key set to `null` is reported as the wrong type.
- `jarvis validate` runs `jarvis render --check` as its `render` check, and env-schema no longer
  compares rendered files itself. A hand-edited file under `flux/` is reported as differing from
  the render.
- The env checks read Flux's own build: each Kustomization an env applies is built with
  `flux build kustomization --dry-run --strict-substitute --in-memory-build`, from the root in
  `flux/sync.yaml` down, its `substituteFrom` ConfigMaps and Secrets read from what is already
  built. Python no longer resolves paths or substitutes variables itself, so the checks see what
  the reconcile applies. A variable with no value and no default now fails `jarvis validate` as it
  fails the reconcile.
- One tree layout: an env is always read against the core running the checks (the env's pin, as
  the CLI and instance CI run it). A `core/` or `addons/` folder in an instance repo is no longer
  looked for, and nothing guesses where core is.
- Whether an env runs the off-host copy comes from `values/switches.yaml`, the decision, not
  from its rendered platform-configs list. The env-schema message names the switch.
- `jarvis up` waits for every Kustomization Flux's build of the env says the cluster gets (add-ons
  and hosted apps included), each Ready for its current spec and, if it reads the env repository,
  at the pushed commit. It no longer reads core's layer files itself or waits for two Ready polls
  in a row, and a Ready left from before the push no longer counts.
- `jarvis site` (Ansible) has no default instance repository: it stops, saying to run
  `jarvis site --env <env>` from the instance, instead of reading envs from the core checkout.
- Core's Renovate moves the release `docs/quickstart.md` starts from (on each core tag) and the
  example apps `jarvis init --with-examples` pins (on each jarvis-apps release).
- `site.yml` reads the env's cluster-vars once, as extra vars: `jarvis site` passes
  `-e @envs/<env>/values/cluster-vars.yaml`, and a hand-run `ansible-playbook site.yml` needs
  the same in place of `-e jarvis_cluster_vars=…`. The flux-operator role talks to the cluster
  with `kubernetes.core` instead of `k3s kubectl`; base-os installs `python3-kubernetes` on every
  node. The rebuild hold is its own `backups` role. `values/switches.yaml`'s old `optional`
  key no longer turns App DNS on.

### Removed

- The `apps-backup-server` feature: app-hosting brings the apps server.
- `backups:` in `values/apps.yaml` (`mode`, `selected`, `exclude`, `overrides`). The sealed
  credential is the one record of which apps back up, so env-schema no longer compares a
  selection against it or reads app files for eligibility. Per-app schedule and retention had no
  user and are gone with it; every volume uses the env's.
- The `flux-substitute` check: Flux's strict substitution in the build above does its job.
- The `digest-pin`, `loadbalancer`, `manifest-lint` and `pvc-backup` checks, which read YAML as
  written and missed what kustomize and Helm render. Their rules are Kyverno policies that
  `jarvis validate` applies to what an env deploys (`policies`). Not carried over: the
  observability exception to `backup: none` (no such add-on exists), the check that the
  `pvc-backup` policy is listed (e2e's backup and restore proves it), the scan of `addons/`
  for restic URLs, and the requests and memory limit in Traefik's HelmChartConfig (k3s ships
  Traefik's chart, so the rendered checks do not see it).
- The `backup-isolation` check (`jarvis validate`) and `jarvis verify`'s `app backup isolation`
  check. The first read Kyverno's CEL expressions with a regex; the second re-read on the
  cluster what core's own manifests fix. Core's tests prove both by behaviour: the pinned
  rest-server and restic, run as the manifests configure them, keep each app login to its own
  repositories, and the Kyverno CLI shows each volume gets only its own zone's credential.

### Fixed

- `jarvis secrets --add-recovery-key`, `--update-recipients` and `--rotate age` undo themselves
  when re-encrypting fails partway: `.sops.yaml`, the sealed files already re-encrypted and the key
  file (a rotation's new identity) are put back as they were. There is nothing left to delete or
  `git checkout` by hand.
- The package version is the release tag (hatch-vcs): `uv tool list` and pip show `0.5.2` for an
  install from `v0.5.2`, where they showed `0`. Nothing is bumped by hand at a release.
- The etcd copy's restic commands wait for the repository lock instead of failing when another
  restic run holds it. Two `restic init`s at once on a new repository can still delete its
  config, so a first copy started by hand in a new cluster's first hour can race the schedule
  (a Limit in `core/platform/backups/CONTRACT.md`).
- `jarvis site`'s apt tasks wait up to 600 s for the dpkg lock (`apt_lock_timeout` in
  `infra/ansible/group_vars/all.yml`) instead of failing on a node still running
  unattended-upgrades.

### Upgrading

- In each env that hosts apps: delete `backups:` from `values/apps.yaml` and `apps-backup-server`
  from `features` in `values/switches.yaml`, then `jarvis render`. Make these edits on the core
  release's Renovate PR, which stays red until they are there. An app whose credential is sealed
  keeps its backups; the old selection is not read.
- Every env's render gains `alerts_provider` in its cluster-vars ConfigMap; the instance workflow
  commits it to the Renovate PR, so there is nothing to edit. To turn alerts on, see
  [deploy.md](docs/deploy.md#alerts).
- Hosts: nothing to run. (Corrected in v0.6.1; this said to run `jarvis site` for Helm 4.3.2
  and `python3-kubernetes`.) On an existing cluster that run changes nothing: the flux-operator
  role installs Helm only to bootstrap Flux Operator on a new cluster, and the k3s role already
  installed `python3-kubernetes` on control-plane nodes.
- Right after the pin moves, `jarvis verify` can pass on state from before the upgrade for up to
  10 minutes; v0.6.1 fixes it. Wait 10 minutes after the Renovate PR merges before you verify.

## v0.5.2

### Added

- `jarvis --version`: the release this CLI came from, and inside an instance repo the release
  each env pins.

### Fixed

- `jarvis <command> --help` from an installed CLI crashed for commands that read core's trees
  (`validate`, `render`, `site`, `verify`: `FileNotFoundError …/schema/cluster-vars.schema.json`).
  Inside an instance repo it is now handed to the release the env pins, as the command itself is,
  so it shows that release's options; outside one it says where to run it.
- `jarvis doctor` called a git worktree's pre-commit hook someone else's (in a worktree `.git` is a
  file). doctor and init now ask git where the hook is, and doctor installs it there.

### Upgrading

- Nothing to do: merge the core release's Renovate PR. Reinstall the CLI to get `--version` and
  the help fix: `mise exec -- uv tool install --force "git+https://github.com/<owner>/jarvis@v0.5.2"`.

## v0.5.1

### Fixed

- `jarvis secrets --add-recovery-key`, `--update-recipients` and `--rotate age` check that the
  working key exists and opens every sealed file of the env before they change anything. A wrong
  or missing key used to get as far as printing a recovery key and adding it to `.sops.yaml`, then
  stop at the first file with sops's last line ("but none were."). Now it is refused first, naming
  the key file and saying nothing was changed. If re-encrypting still fails partway, the rule is
  put back and the files already re-encrypted are named, with the `git checkout` that undoes them.

### Upgrading

- Nothing to do: merge the core release's Renovate PR.

## v0.5.0

### Added

- [docs/reference.md](docs/reference.md): every key of an env's values files, with its type,
  default, when it is required and what it means, generated from the JSON Schemas
  (`python -m jarvis.schema.reference`, kept current by a test). Each key's description now lives
  in its schema, so editors show it as you type; `schema/CONTRACT.md` keeps only the rules that span
  keys.
- [docs/quickstart.md](docs/quickstart.md): one node to a verified cluster in seven steps, and
  [docs/concepts.md](docs/concepts.md). The README starts there. CI runs the Quickstart's
  commands as written (`.github/workflows/quickstart.yml`) on every release tag, nightly and on
  changes that reach them: on Ubuntu with the runner as both controller and node, through
  `jarvis up` and `jarvis verify --smoke`, and the controller's half on macOS.
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
- `jarvis verify --smoke`: after the standing checks, one short-lived probe per enabled capability,
  each making something, checking it and removing it (docs/deploy.md, Verify). A Certificate from
  the internal CA becomes Ready; with App DNS, a new Ingress name answers at App DNS's address; with
  hosted apps, a copy of a running app pod is admitted and the same copy with an ungranted risk is
  refused, both as server-side dry runs. An add-on brings its own with `register_smoke`. e2e runs it.
- `jarvis init --with-examples` turns on app hosting with the example apps, from `jarvis-apps`
  beside core (`jarvis/kit/examples-apps.yaml`, which core's tests and e2e also run), for a
  first-run tour. `jarvis env add` takes it too.
- `jarvis init --with-agents` adds the coding-agent kit to the new repository: `AGENTS.md`,
  `CLAUDE.md`, and the deny lists in `.claude/settings.json` and `.cursor/cli.json`
  (agents/README.md). Core's own deny lists are links to the same files.
- A devcontainer in every repository `jarvis init` writes (`.devcontainer/`): opened on a machine
  with only a container runtime, it installs mise, uv and the jarvis CLI at the env's core
  release, and mounts the age key, kubeconfigs and SSH keys from the machine. Core has its own for
  working on core, which runs `mise run deps`.

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

- An env's `apps_git_repo_url` may name another owner's repository than its `flux_git_repo_url`
  (the example apps beside core, another team's apps): the rule that they share an owner handle is
  gone. Hosted apps are untrusted and held by admission wherever they come from.

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

In this order, on the controller, for each instance repository:

1. Install mise (https://mise.jdx.dev/getting-started.html), then delete the binaries the old
   installer left in `~/.local/bin` (`sops age age-keygen kubectl kustomize kubeconform kyverno
   gitleaks restic rest-server`; keep `mise`): a shell finds them before mise's, so after the next
   version bump a hand-typed command would run the old one. A service started from one of them
   (an off-host rest-server, say) needs a copy of its own first (core/platform/backups/CONTRACT.md,
   off-host copy).
2. Install the jarvis CLI, once, as docs/quickstart.md does:
   `mise use -g uv`, then
   `mise exec -- uv tool install "git+https://github.com/<owner>/jarvis@v0.5.0"`.
   From then on run `jarvis` from inside the instance repository, in place of
   `make … ENV=… INSTANCE=…` from a core checkout: `make site ENV=env-01 INSTANCE=<path>` is
   `jarvis site`, `make restore APP=<ns>` is `jarvis restore --app <ns>`, `make bootstrap-secrets`
   is `jarvis secrets`, and so on (`jarvis --help`). The core checkout on the controller is no
   longer needed for operating an env.
3. Merge the core release's Renovate PR (the grouped `jarvis core` one): it moves every env's
   `core_git_ref`, the instance workflow and the preset, and core's instance workflow renders each
   env's `flux/` at the new tag. Flux then applies v0.5.0's manifests; nothing on the hosts changes,
   so no `jarvis site` is needed.
4. Pull, then run `jarvis doctor` once: it installs the pre-commit hook (`jarvis render --check`
   and `jarvis validate` on every commit) in a clone that lacks it.
5. Give each env its recovery key, with the working key at `age_key_file`:
   `jarvis secrets --add-recovery-key`. Store the key it prints, once, offline; then commit and push
   the re-encrypted files. Until then `jarvis doctor` warns. The keep-safe list in docs/secrets.md
   is now two items, and the working key copy in a password manager is no longer one of them.
6. In the instance CI caller, add `workflow_dispatch:` under `on:`.
7. Optional, what `jarvis init` now writes for a new repository: copy `jarvis/kit/devcontainer/`
   to `.devcontainer/` for the devcontainer, and the files in `jarvis/kit/agents/` for the agent
   kit (agents/README.md).

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
