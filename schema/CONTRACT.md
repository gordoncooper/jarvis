# CONTRACT: Env schema

Defines what each deployment target under `envs/<env>/` may set. Core stays env-agnostic (`ci/checks/core-names`); env specifics live only under `envs/`. Config with no consumer is not in the schema; each piece arrives with the code that uses it.

A real env lives in a private instance checkout. `INSTANCE=<path>` points the schema, the env-level checks and every env tool at `<path>/envs/<env>` and `<path>/.sops.yaml`. Unset, they read this checkout, which holds only the example env. `instance_root` in `tools/lib/_env.py` is the one resolver.

## Offers

- A single validation contract for three files per env:
  - `values/switches.yaml` — what the env enables, by name only (below).
  - `values/cluster-vars.yaml` — flat keys for Flux postBuild substitution and Ansible host settings. Inventory keeps hostnames only. Secrets stay in SOPS files that bootstrap-secrets and Flux decrypt.
  - `inventory/hosts.yml` — one block per host: `node_roles`, optional `data_disk`, and optional per-host `ssh_user` and `k3s_kubelet_*`. Host keys are hostnames. `ci/checks/env-schema` rejects an IP used as a host name and any other host key.
- Rejection of unknown `cluster-vars.yaml` keys (`code_delivery` is one: nothing reads it). The known set is the required keys plus optional `ssh_user`, `age_key_file`, `apps_git_repo_url`, `lan_cidrs`, `llm_provider_host`, `chrony_nts`, `backup_check_schedule`, and `node_memory_reserve_percent`.
- Default `ssh_user: ubuntu` when omitted. The example env still sets `ssh_user`.
- Default `backup_check_schedule: 30 6 * * 3` when omitted. The example env still sets `backup_check_schedule`.
- Default `age_key_file` is `~/.config/sops/age/keys.txt` when omitted. `SOPS_AGE_KEY_FILE` overrides it on the controller. The example env still sets `age_key_file`.
- Rejection of a `time_zone` that is not an IANA zone, and of a `k3s_version` that is not a k3s version string (`v<major>.<minor>.<patch>+k3s<n>`).
- `apps_git_repo_url`, when set, is https and validated like `flux_git_repo_url`. The two URLs use one owner handle. When `apps_git_repo_url` is unset, that handle comes from `flux_git_repo_url`. It is required once the env lists a hosted app.
- `llm_provider_host`, when set, is a bare hostname with no scheme or path. Omitting it is valid.
- `lan_cidrs`, when set, is a non-empty list of IPv4 CIDRs, each inside RFC 1918 and clear of `pod_cidr` and `service_cidr`. It is required once `values/apps.yaml` grants `lan-all` or `lan-target`.

## Expects

- One folder per env: `envs/<env>/` with the `cluster-vars.yaml` and `hosts.yml` above present when that env is active.
- Once an env has a `flux/` folder or sealed files, it holds `values/switches.yaml`, and commits `flux/kustomization.yaml`, `flux/kustomizations/services.yaml` and `flux/platform-configs/kustomization.yaml` exactly as `tools/lib/render_switches.py` writes them from it. An unknown name, a name listed twice, or a hand-edited render fails, and the render refuses an unknown name or two add-ons for one slot before it writes.
- Once an env has a `flux/` folder or sealed files, `flux/` holds every Flux entry file: `sync.yaml`, `flux-instance.yaml`, `kustomization.yaml`, `identities.yaml`, the four `kustomizations/*.yaml` and `platform-configs/kustomization.yaml`. A missing one fails the schema. An env with neither yet (prep-node on a new env) needs none.
- Once `envs/<env>/secrets/` holds any `*.sops.yaml`, it also holds `cert-manager-ca.sops.yaml` and `backups-restic.sops.yaml`, which the env's platform-configs path applies.
- Once `envs/<env>/secrets/` holds any `*.sops.yaml`, it also holds `k3s-token.sops.yaml`. Before that it is not required, because prep-node validates a new env before `make bootstrap-secrets` runs.
- When `flux_git_repo_url` is `ssh://`, `envs/<env>/secrets/flux-deploy-key.sops.yaml` (the read-only deploy key) is required.
- Once `envs/<env>/secrets/` holds any `*.sops.yaml` and the env's switches list `offsite-backup-copy`, `envs/<env>/secrets/backups-offsite.sops.yaml` is required. Before bootstrap it is not, for the same prep-node reason. An env that does not list it needs nothing new.
- App backup credentials. A registration is `backups/app-<name>`, carried by the generated wrapper under `components:` in the env's `flux/platform-configs/kustomization.yaml`; any other entry but the app-hosting switch fails. `flux/platform-configs/backups/app-<name>/kustomization.yaml` must be exactly the Component `make bootstrap-secrets APP=app-<name>` writes. Once bootstrapped, `envs/<env>/secrets/app-<name>/backups-restic-app-<name>.sops.yaml` is required. An env that registers any app lists `apps-backup-server` in its switches. A file named `backups-restic-app-<name>.sops.yaml` anywhere else under `secrets/` fails. Registration follows the `backups:` selection in `values/apps.yaml`: `mode: selected` (default off, list `selected` backup-eligible apps) or `mode: all_except` (every eligible listed app except `exclude`). Ineligible and excluded apps need no credential; each selected eligible app requires its sealed file once bootstrapped. `tools/lib/render_backup_selection.py` generates `flux/generated/backup-selection/` and each registration Component; `components:` then holds only the app-hosting switch and `../generated/backup-selection`, and hand-copied `backups/app-*` entries or any other entry fail. With no `backups:` section the env registers no app; deleting the section while apps still register fails, so set `selected: []` and re-render first. Optional per-app `overrides` under `backups` may set `backup_schedule` and `backup_retention_*`; the render carries them in the wrapper as ConfigMap `platform-backups/backup-overrides`. Eligibility reads each app's file from a checkout of `apps_git_repo_url`, or from `APPS_REPO` when set; a missing checkout, or an app file it is missing or cannot parse, fails. An env that lists no app reads no app file, so it needs no checkout, empty or absent.
- App hosting is off unless the env's switches list `app-hosting` under `optional`. The render writes the kustomize `components:` entry, kept apart from the app backup registrations: any other component must still be a `backups/app-<name>` registration.
- Hosted apps: with app hosting on, `apps` in `values/switches.yaml` (app names only, each a DNS label of at most 59 characters, once, each with a `<name>/app.yaml` in the apps repository: a missing checkout or a name it does not hold fails) and optional `values/apps.yaml` with `exceptions:` (lines `- <app>: <grant>`, one grant each, for a listed app; grants `privileged`, `root`, `host-network`, `host-path`, `hostname <name>` outside `dns_suffix`, `lan-port <port>/<TCP|UDP>`, `lan-all`, `lan-target <name> <cidr> <port>/<TCP|UDP>` (the name a DNS label other than `all`, the range inside `lan_cidrs`), `no-uptime-check`, each once; `lan-all` and `lan-target` need `lan_cidrs`). Optional `backups:` selects which listed apps register a backup credential: `mode: selected` with `selected` (default off) or `mode: all_except` with optional `exclude`; only apps whose app file declares a `backup: true` volume may appear on `selected`, and `exclude` reads no app file; optional `overrides.<app>` may set `backup_schedule` and `backup_retention_daily`, `backup_retention_weekly`, and `backup_retention_monthly`, for that app's ReplicationSources in place of the `cluster-vars.yaml` values; a field it leaves out keeps the `cluster-vars.yaml` value ([platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md)). Eligibility reads each app's `app.yaml` from `apps_git_repo_url` or `APPS_REPO`. `backups:` is validated even when the env lists no app, and then needs no apps checkout. Any other key, name or grant fails, and so do an `apps:` key in `values/apps.yaml` and a non-empty list with app hosting off. An env that lists an app sets `apps_git_repo_url`, lists `generated/apps.yaml` in `flux/kustomization.yaml`, and commits that file exactly as `tools/lib/render_switches.py` renders it (the render check is skipped pre-host, like the cluster-vars ConfigMap). With `backups:` present, `tools/lib/render_backup_selection.py` output must match under `flux/generated/backup-selection/` and `flux/platform-configs/backups/`. A missing or empty list runs no app and has neither the file nor the line. ([platform/optional/app-hosting/CONTRACT.md](../platform/optional/app-hosting/CONTRACT.md)).
- `cluster-vars.yaml` keys referenced as `${…}` in manifests must exist in that file. A missing postBuild variable fails the Flux reconcile.
- Add-ons: `addons` in `values/switches.yaml` lists the add-ons the env turns on, by folder name under core's `services/` ([services/CONTRACT.md](../services/CONTRACT.md)); the render writes them under `spec.components` of `flux/kustomizations/services.yaml`. An empty list is a core-only env. An unknown add-on, the same add-on twice, or two add-ons that fill one slot (label `addons.jarvis.io/slot`) fails the schema. Once `envs/<env>/secrets/` holds any `*.sops.yaml`, each folder an enabled add-on decrypts, `envs/<env>/secrets/<namespace>/`, holds at least one sealed file. Before that it is not required, for the same prep-node reason. The example env is exempt, as it holds no secret files. Core is `<root>/core` when the instance holds it there, and otherwise the product checkout that runs the schema.

## Health

Not a runtime component. Health is “validation passes”: `ci/checks/env-schema` exits 0 for the env tree.

## Config

All keys in env value files use **lowercase snake_case** (including `cluster-vars.yaml`). Validation applies to the fields below. Types are descriptive; the validator in `schema/env.py` is authoritative.

### `values/switches.yaml`

One short file, names only, one per line. Core is on in every env, so it has no section: `platform/kustomization.yaml` and `platform/configs/kustomization.yaml` list it. `tools/lib/render_switches.py --env <env>` turns it into the Flux wiring, and it and `tools/lib/render_backup_selection.py` refuse an unknown name before they write; `INSTANCE=<path>` points it at an instance. Settings stay where they were: an app's in its app file, an exception line or backup selection in `values/apps.yaml`, a sealed secret under `secrets/`.

| Section | Names | Notes |
| --- | --- | --- |
| `optional` | `app-hosting`, `offsite-backup-copy` (needs `backups-offsite`), `apps-backup-server`. | Off until listed. An optional core component adds one row to `OPTIONAL_COMPONENTS` in `tools/lib/_switches.py` and no bundle to any env. Observability is an optional core component too, not an add-on: off until an env lists `observability` here, a name refused until it is built ([platform/optional/CONTRACT.md](../platform/optional/CONTRACT.md)). |
| `addons` | A folder under core's `services/`. | Limited-tenant swappable services ([services/CONTRACT.md](../services/CONTRACT.md)). At most one per slot. |
| `apps` | A folder in the apps repository. | Hosted apps: untrusted, held to their app file by the cluster ([platform/optional/app-hosting/CONTRACT.md](../platform/optional/app-hosting/CONTRACT.md)). Listing a name switches an app on and grants it nothing; a new app in that repository never deploys until an env lists it. |

### `values/cluster-vars.yaml`

A required key that is present but null counts as unset and fails.

| Field | Required | Notes |
| --- | --- | --- |
| `dns_suffix` | yes | Internal zone suffix for the env (e.g. `<dns_suffix>`). A lowercase DNS name. Never hardcoded in core (`ci/checks/core-names`). |
| `time_zone` | yes | IANA time zone name for the node, and VolSync schedules (e.g. `<time_zone>`). Unknown names fail validation. |
| `ssh_user` | no | SSH login user for node bootstrap; Ansible sets `ansible_user` from it and runs tasks as root (default `ubuntu`). |
| `age_key_file` | no | Age private key path on the controller for the flux-operator sops-age bootstrap and the k3s token read. Default `~/.config/sops/age/keys.txt`. `SOPS_AGE_KEY_FILE` overrides it. |
| `backup_path` | yes | Absolute path. Parent of `backup_path/etcd-snapshots` and of one restic repository per PVC (`backup_path/restic/ns-<namespace>/pvc-<pvc>`). The `ns-` and `pvc-` prefixes keep a path segment off rest-server's reserved names. |
| `backup_retention_daily` | yes | Restic keep count for daily snapshots. |
| `backup_retention_weekly` | yes | Restic keep count for weekly snapshots. |
| `backup_retention_monthly` | yes | Restic keep count for monthly snapshots. |
| `backup_schedule` | yes | Five-field cron for each PVC's VolSync ReplicationSource (`trigger.schedule`). The controller evaluates it in `time_zone`. |
| `backup_check_schedule` | no | Five-field cron for CronJob `platform-backups/restic-check`, evaluated in `time_zone`. Default `30 6 * * 3`. Keep every start at least 3 hours from each `backup_schedule` start and from the etcd copy (`5 */12 * * *`). |
| `jarvis_env` | yes | Env name for `/etc/jarvis/env` marker and guards; must match `envs/<env>/`. |
| `k3s_version` | yes | k3s version string `v<major>.<minor>.<patch>+k3s<n>`. The host binary changes only by a pull request. |
| `k3s_sha256` | yes | SHA-256 of the pinned `k3s` amd64 release binary (verified on install). |
| `pod_cidr` | yes | Pod network CIDR; an IP network, set once per env. |
| `service_cidr` | yes | Service network CIDR; an IP network, set once per env. |
| `app_dns_cluster_ip` | yes | ClusterIP of the App DNS Service. CoreDNS `forward` takes an address. A host address inside `service_cidr`, not the network or broadcast address, and not the first address (the Kubernetes API) or the tenth address (k3s cluster DNS). |
| `local_path_data` | yes | k3s local-path provisioner data directory. A subdirectory of `/var/lib/jarvis`, not that mount itself. |
| `flux_git_repo_url` | yes | The env repository, the root `GitRepository` in `flux/sync.yaml`. `https://` (no userinfo, query, or fragment) or `ssh://<user>@<host>/<owner>/<repo>` read with the sealed deploy key; owner/repo path. |
| `apps_git_repo_url` | no (yes when `values/switches.yaml` lists an app) | An https Git remote for the apps repository, read with no credential, with the same owner handle as `flux_git_repo_url`. Each listed app's sources read it ([platform/optional/app-hosting/CONTRACT.md](../platform/optional/app-hosting/CONTRACT.md)). When unset, the env owner is the `flux_git_repo_url` handle. |
| `lan_cidrs` | no (yes when `values/apps.yaml` grants `lan-all` or `lan-target`) | The LAN's ranges: a list of IPv4 CIDRs inside RFC 1918, clear of `pod_cidr` and `service_cidr`. Hosted apps' internet egress excludes them; `lan-all` opens them and a `lan-target` range sits inside them ([platform/optional/app-hosting/CONTRACT.md](../platform/optional/app-hosting/CONTRACT.md)). Not published in `cluster-vars`; the app wiring render carries it. |
| `flux_git_ref` | yes | Git ref of the root source (e.g. `refs/heads/main`). |
| `core_git_repo_url` | yes | The public core repository, the `core` `GitRepository` in `flux/sync.yaml`. https only, read with no credential. |
| `core_git_ref` | yes | Git ref of the core source (e.g. `refs/heads/main`). |
| `llm_provider_host` | no | When set, an AI provider API hostname, with no scheme, path, or port. |
| `ntp_pools` | yes | Chrony pool hostnames (base-os role); each entry is one `pool … iburst maxsources 4` line, plus `nts` while `chrony_nts` is on. |
| `chrony_nts` | no | `true` or `false`; default `true`. `false` renders the pools without `nts`, for a LAN or router time source that does not speak NTS. Host-only (Ansible). Any other value fails validation. |
| `k3s_kubelet_kube_reserved` | yes | kubelet `kube-reserved` value (without the `kube-reserved=` prefix). |
| `k3s_kubelet_system_reserved` | yes | kubelet `system-reserved` value (without the `system-reserved=` prefix). |
| `k3s_kubelet_eviction_hard` | yes | kubelet `eviction-hard` threshold expression. Its `memory.available` is raised to the node memory reserve below when that is larger. |
| `node_memory_reserve_percent` | no | Whole number from 0 to 50; default `10`. The share of every node's memory no pod is scheduled into: headroom for pods that grow past their requests. The k3s role renders it as the kubelet's `memory.available<N%` hard eviction threshold. The kubelet subtracts it from allocatable; below it, the kubelet evicts pods whose usage exceeds their requests first, by priority among them, and only then pods within their requests, lowest priority first, before the kernel's OOM killer has to act. The env's or the host's `k3s_kubelet_eviction_hard` keeps its own `memory.available` when that is larger, compared on the node's memory. `kube-reserved` and `system-reserved` are unchanged and come on top. `0` leaves `eviction-hard` as written. Host-only (Ansible). |

### `inventory/hosts.yml`

Host keys are hostnames, under `all.hosts`. No other top-level key, and no keys under `all` besides `hosts`. Inventory is not encrypted and does not need the age key. `ci/checks/env-schema` rejects an IP used as a host name and any host key other than the rows below.

Each host is a mapping with `node_roles` and the optional keys in the table. No other host keys.

| Field | Required | Notes |
| --- | --- | --- |
| `node_roles` | yes | Non-empty list of unique roles from `control_plane`, `workers`, `backup`, `gpu`, `edge`. Exactly one host includes `control_plane`. Exactly one host includes `backup`. At least one host includes `edge`. The same host may include more than one role. No role implies another. |
| `data_disk` | no | Plain flag `true`, or omit the key. `true` means the admin has mounted a filesystem at `/var/lib/jarvis`. Any other value is rejected. |
| `ssh_user` | no | SSH user for this host; overrides env `ssh_user`. Non-empty string. |
| `k3s_kubelet_kube_reserved` | no | Host kubelet `kube-reserved`; overrides the env value. Non-empty string. |
| `k3s_kubelet_system_reserved` | no | Host kubelet `system-reserved`; overrides the env value. Non-empty string. |
| `k3s_kubelet_eviction_hard` | no | Host kubelet `eviction-hard`; overrides the env value. Non-empty string. The env's node memory reserve still applies to it. |

### Node roles and labels

`infra/inventory/roles` is the second inventory (a directory). Every env run passes it as the second `-i`, after `envs/<env>/inventory/hosts.yml`. No env copies it. `node_roles` become Ansible groups of the same name, so a play can target `control_plane` or `workers`. `data_disk` is a host var read by the `node-disk` role; it makes no group. The `edge` group is the only set of hosts that receive `svccontroller.k3s.cattle.io/enablelb=true`. `backup` is a label only, and etcd snapshots stay on the control plane.

Kubernetes labels are `node-role.jarvis.io/<role>=true` for each role on the node. That prefix is reserved for them. The k3s role sets and removes them, and it removes leftover labels under `role.<dns_suffix>/`. This schema does not apply labels. A node whose roles include neither `workers` nor `edge` gets one taint, `node-role.jarvis.io/dedicated=true:NoSchedule`. A node that includes `workers` or `edge` gets none, so a single node that includes `workers` stays schedulable for pods that tolerate nothing, and k3s service load-balancer pods can still land on an edge node. Role labels choose the node. The taint only keeps other pods off a dedicated node.

The example env declares every known role. A new known role is declared on that env in the same PR. A role is added only in the same PR as the first service that schedules by it. `workers` is the general role for app nodes, and a role does not depend on another.

### Example env

`envs/env-00` is the never-deployed example env and the template for a new env. `EXAMPLE_ENV` in `schema/env.py` is that folder name. It sets every known `cluster-vars.yaml` key, including keys that are optional for other envs, and its `values/switches.yaml` names what CI renders. A new known key is added to this env in the same PR. Its `secrets/` directory holds only `README.md` and no secret files. It has no SOPS creation rule and no `ca.crt`. Its `flux/` tree is the Flux entry a copy starts from: `flux-instance.yaml`, `sync.yaml` (the two sources and the root Kustomization, with placeholder URLs), `identities.yaml` (the service accounts the Flux Kustomizations and HelmReleases run as), the rendered `generated/cluster-vars-configmap.yaml`, `kustomization.yaml`, the four files under `kustomizations/`, and `platform-configs/kustomization.yaml`. That platform-configs list, rendered from the switches, is `core/platform/configs` plus `backups-restic` and `cert-manager-ca`, the two files `make bootstrap-secrets` creates that this path applies. Those secret files are not in this env; a copy has them after bootstrap-secrets. In a non-example env, every `envs/<name>/` path under its `flux/` uses that env's name.

The example's switches also turn on the off-host copy, `offsite-backup-copy`: `core/platform/optional/offsite-backup-copy` and `backups-offsite`, which the owner seals with `make seal` rather than `make bootstrap-secrets`. The test copy seals a placeholder for it.

The example's switches list the sample add-on, so CI renders an add-on. A copy empties `addons` and `apps`, takes `app-hosting` off `optional`, deletes `values/apps.yaml`, re-renders, and starts core-only. The test copy seals a placeholder in each folder the sample decrypts.

### Explicit env

`bootstrap-secrets`, `seal`, and the cluster-vars renderer require `--env`. The Makefile targets `bootstrap-secrets` and `seal` require `ENV=`. Each fails when the env is omitted.

### postBuild substitution

The FluxInstance sets kustomize-controller `StrictPostBuildSubstitutions=true`. A missing postBuild variable fails the reconcile. `$${...}` escapes a literal `${...}`.

The cluster-vars ConfigMap publishes an allow-list. Ansible reads `values/cluster-vars.yaml` for host settings, including every key that is not published. `jarvis_env` is published because verify, drill and restore read it from the live ConfigMap.

The published set is `CLUSTER_VARS_PUBLISHED` in `tools/lib/render_cluster_vars_configmap.py`. Every other known key in `schema/env.py` is host-only (Ansible only). When `flux/generated/cluster-vars-configmap.yaml` is present, it must match that render.

In `flux/sync.yaml`, the root `GitRepository` `flux-system` url and `ref.name` match `flux_git_repo_url` and `flux_git_ref`, its `core` `GitRepository` matches `core_git_repo_url` and `core_git_ref` and has no `secretRef`, the root includes `core` at `toPath: core`, an `ssh://` root names a `secretRef`, and the root Kustomization `flux-system` applies `./envs/<env>/flux` from `flux-system`. `site.yml` preflight requires the file.

`validate_env_tree(..., pre_host=True)` skips the missing `.sops.yaml` recipient requirement and the generated ConfigMap match. Other `.sops.yaml` checks still run. `tools/prep-node` uses that mode before it contacts a host, then runs the full check at the end.

### Identity values and product rules

`ci/checks/core-names` reads identity values from every env except the example env: the env folder name, inventory hostnames, `ssh_user` (env and per-host), `dns_suffix`, and the owner handle in `flux_git_repo_url` and `apps_git_repo_url`. The owner's personal name is not in the repo. It comes from the `OWNER_NAME` environment variable (a GitHub Actions repository variable of the same name in CI). The check fails when that variable is unset or empty, and the failure text does not include the value.

Every tracked file is scanned except `.github/CODEOWNERS` and `.mailmap`; that includes root-level files such as `Makefile`, `renovate.json`, `AGENTS.md`, `.sops.yaml`, and the `.cursor/` and `.claude/` folders. Env names may appear under `envs/**` and `docs/**`, and in the `path_regex` field of `.sops.yaml` (the field alone on its line, no comment after it) and nowhere else in that file. Hostnames, `dns_suffix`, the owner handle, and `ssh_user` forms may appear only under the `envs/<env>/` they were read from. The personal name may appear nowhere.

Matching is case-insensitive, on tokens made of letters, digits, underscores, and hyphens. A dot is a boundary, so a DNS suffix inside a longer hostname fails, and a value inside a longer token passes. `ssh_user` matches only as `/home/<user>`, `<user>@<host>`, or `~<user>`.

Repo URLs outside `envs/<env>/` fail because they contain the owner handle. Bare repo names are not identity values.

Product rules in the same check: the word `bastion`, and any hardcoded `/home/` path. This file and `ci/checks/lib/_core_names.py` name those tokens. That pair is the one exemption, and it covers only the product rules.

`.sops.yaml` has one age creation rule per non-example env, keyed on `envs/<env>/secrets/`. The example env has no root rule. A creation rule that names an env folder that does not exist fails.

### Reference shape (documentation)

Each env under `envs/<env>/` supplies its own values, for example: `dns_suffix`, `time_zone`, optional `ssh_user` (default `ubuntu` when omitted), and optional `age_key_file` (default `~/.config/sops/age/keys.txt` when omitted).

## Proven by

- `ci/checks/env-schema` in the required `ci` workflow.
- `ci/tests/test_env_schema.py`: valid env tree, an env without `hermes.yaml` or `apps.yaml`, `build_agent` and `code_delivery` as unknown keys, `storage` as an unknown role, `ssh_user` defaulting to `ubuntu`, and `age_key_file` defaulting to `~/.config/sops/age/keys.txt`.
- A behavior test accepts an env that omits any one of `apps_git_repo_url`, `lan_cidrs` and `llm_provider_host`, and an env that omits all. A value that is set still has to be allowed.
- A behavior test rejects a `lan_cidrs` that is not a list, holds a non-network address, a public range, or a range overlapping the cluster's.
- A behavior test accepts two non-example envs whose `flux_git_repo_url` owners differ.
- A behavior test rejects a `.sops.yaml` creation rule that names an env folder that does not exist.
- A behavior test rejects a `dns_suffix` that is not a lowercase DNS name.
- A behavior test accepts a new env that lists the off-host copy before bootstrap, rejects it once bootstrapped without `backups-offsite.sops.yaml`, and accepts it once the file is there.
- `ci/tests/test_switches.py`: one name enables an app with no other env file and a second render changes nothing; an unknown name in each section fails and refuses the render, and so do two add-ons for one slot; a `core` section, a missing file, a hand-edited render and `apps:` in `values/apps.yaml` fail; an unlisted optional component renders nothing; selected backups need the apps server switch.
- `ci/tests/test_backup_isolation.py` accepts a bootstrapped copy with one registered app credential, and rejects a registration outside `app-`, an edited registration Component, a missing sealed file, a registration without the apps server, and an app credential sealed outside its own folder.
- `ci/tests/test_backup_selection.py` rejects an ineligible app on `selected`, requires a sealed credential for a selected eligible app once bootstrapped, accepts an `all_except` exclusion without that sealed file, renders registration Components from selection, fails verify when a required app has no credential, rejects each malformed section and an unknown component, and fails closed on an unreadable app file or missing checkout.
- `ci/tests/test_addons.py` accepts known add-ons, rejects an unknown add-on, a duplicate, and two add-ons for one slot, and rejects a bootstrapped env whose add-on Secret folder is empty.
- A behavior test rejects a repo URL that includes userinfo, a query, or a fragment.
- A behavior test accepts the example env's source pair and rejects a core source whose url does not match cluster-vars.
- A behavior test requires `flux-deploy-key.sops.yaml` for an `ssh://` root and accepts the env once it is there.
- A behavior test rejects a generated ConfigMap that does not match the cluster-vars render, and accepts that tree in pre-host mode.
- A behavior test rejects a relative `backup_path`.
- The cluster-vars ConfigMap render publishes only the allow-list in the postBuild section. Host-only keys stay out. A stale render fails the flux entry test.
- A behavior test rejects an unknown `time_zone` and a `k3s_version` that is not `v<major>.<minor>.<patch>+k3s<n>`.
- A behavior test rejects a `backup_schedule` or `backup_check_schedule` that is not a five-field cron, and one resolves an omitted `backup_check_schedule` to `30 6 * * 3`.
- A behavior test rejects an `app_dns_cluster_ip` that is not an IP address, is outside `service_cidr`, is not a host address, or is the first or tenth address of `service_cidr`.
- A drift test fails when the known-key set gains a key the example env does not set.
- Behavior tests reject an unknown role, an env without exactly one `control_plane` or exactly one `backup` host, an env with no `edge` host, and an unknown host key.
- A drift test fails when the known role set gains a role the example env does not declare.
- `ansible-inventory --graph` on the example env shows each role as a group. `site.yml` syntax-checks against that env's hosts file and `infra/inventory/roles`.
- `ci/checks/core-names` rejects identity values on the scanned paths and fails when `OWNER_NAME` is unset.
