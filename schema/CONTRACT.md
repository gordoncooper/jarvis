# CONTRACT: Env schema

Defines what each deployment target under `envs/<env>/` may set. Core stays env-agnostic (R3); env specifics live only under `envs/` (R4). Bot tiers and model names live only in env values (R23). The profile schema is its own contract, `services/hermes/core/schema/CONTRACT.md`, added in `p2-04`.

## Offers

- A single validation contract for four files per env:
  - `values/cluster-vars.yaml` — flat keys for Flux postBuild substitution and Ansible host settings. Inventory keeps hostnames only. Secrets stay in SOPS files that bootstrap-secrets and Flux decrypt.
  - `values/hermes.yaml` — bots, enabled cron, bot tier map, tier-to-model map, and code-factory lane models (with family).
  - `values/apps.yaml` — app hosting on or off and enabled apps with sizes.
  - `inventory/hosts.yml` — one block per host: `node_roles` and an optional `data_disk` (no IPs; R25, D42).
- Rejection of unknown `code_delivery` and `build_agent` values (only built providers are accepted; §9 alternatives are named as stubs, not valid values).
- Rejection of unknown `cluster-vars.yaml` keys. The known set is the required keys plus optional `ssh_user`.
- Default `ssh_user: ubuntu` when omitted (D34). The example env still sets `ssh_user`.
- `apps_git_repo_url` required and https, validated like `flux_git_repo_url`. Both URLs use one owner handle, and that handle is the same across every non-example env (D45).
- `llm_provider_host` required, a bare hostname with no scheme or path (D45).

## Expects

- One folder per env: `envs/<env>/` with the four files above present when that env is active.
- `cluster-vars.yaml` keys referenced as `${…}` in manifests must exist in that file (R4).

## Health

Not a runtime component. Health is “validation passes”: `tools/checks/env-schema` exits 0 for the env tree.

## Config

All keys in env value files use **lowercase snake_case** (including `cluster-vars.yaml`, `hermes.yaml`, and `apps.yaml`). Validation applies to the fields below. Types are descriptive; the validator (`p0a-05`) is authoritative.

### `values/cluster-vars.yaml`

| Field | Required | Notes |
| --- | --- | --- |
| `dns_suffix` | yes | Internal zone suffix for the env (e.g. `<dns_suffix>`). Never hardcoded in core (R3, R28). |
| `time_zone` | yes | IANA zone for the node time zone, VolSync schedules, and Hermes cron (e.g. `<time_zone>`; D21). |
| `ssh_user` | no | SSH login user for node bootstrap; Ansible sets `ansible_user` from it and runs tasks as root (default `ubuntu`; D34). |
| `code_delivery` | yes | `github` only. `forgejo` is the planned second provider (§9); any other value fails validation. |
| `build_agent` | yes | `cursor` only. `grok-build` and `opencode-local` are §9 stubs; any other value fails validation. |
| `backup_path` | yes | Local restic repository directory for VolSync and etcd snapshots (§7; set per `<env>` in `p0a-03`). |
| `backup_retention_daily` | yes | Restic keep count for daily snapshots (§7). |
| `backup_retention_weekly` | yes | Restic keep count for weekly snapshots (§7). |
| `backup_retention_monthly` | yes | Restic keep count for monthly snapshots (§7). |
| `jarvis_env` | yes | Env name for `/etc/jarvis/env` marker and guards; must match `envs/<env>/` (p0a-12). |
| `k3s_version` | yes | Host binary version; changes only by PR (R33, D5). |
| `k3s_sha256` | yes | SHA-256 of the pinned `k3s` amd64 release binary (verified on install). |
| `pod_cidr` | yes | Pod network CIDR; set once per env (D4). |
| `service_cidr` | yes | Service network CIDR; set once per env (D4). |
| `local_path_data` | yes | k3s local-path provisioner data directory (D3). |
| `flux_git_repo_url` | yes | Public Git remote for FluxInstance sync (https only; owner/repo path; R25). |
| `apps_git_repo_url` | yes | https Git remote for the apps repo. Same owner handle as `flux_git_repo_url`. That handle is the same across every non-example env (D45). |
| `flux_git_ref` | yes | Git ref for FluxInstance sync (e.g. `refs/heads/main`). |
| `llm_provider_host` | yes | AI provider API hostname, with no scheme, path, or port (D45). |
| `ntp_pools` | yes | Chrony pool hostnames (base-os role); each entry is one `pool … iburst maxsources 4 nts` line. |
| `k3s_kubelet_kube_reserved` | yes | kubelet `kube-reserved` value (without the `kube-reserved=` prefix). |
| `k3s_kubelet_system_reserved` | yes | kubelet `system-reserved` value (without the `system-reserved=` prefix). |
| `k3s_kubelet_eviction_hard` | yes | kubelet `eviction-hard` threshold expression. |

### `values/hermes.yaml`

| Area | Notes |
| --- | --- |
| `bots` | Which Hermes profiles run in ops and code-factory deploys (R19). |
| `cron` | Which scheduled jobs are enabled; times use `time_zone` from cluster-vars. |
| `bot_tier_map` | Maps each bot to `frontier`, `mid`, or `cheap` (R23). |
| `tier_model_map` | Maps tiers to provider model names for that env only. |
| `lane_model_map` | Builder, checker, and `code-factory:hard` models and families for the code-factory workflow (R41). |

### `values/apps.yaml`

| Field | Notes |
| --- | --- |
| `app_hosting_enabled` | App hosting on or off for the env (R30). |
| `apps` | Enabled hosted apps and size quota when on. |

### `inventory/hosts.yml`

Host keys are hostnames, under `all.hosts`. No other top-level key, and no keys under `all` besides `hosts`. Inventory is not encrypted and does not need the age key (D2, D42, R25).

Each host is a mapping with `node_roles` and, when that host has a data disk, `data_disk`. No other host keys.

| Field | Required | Notes |
| --- | --- | --- |
| `node_roles` | yes | Non-empty list of unique roles from `control_plane`, `workers`, `storage`, `backup`, `gpu`, `edge`. Exactly one host includes `control_plane`. Exactly one host includes `backup`. At least one host includes `edge`. The same host may include more than one role. No role implies another. |
| `data_disk` | no | Mapping with `id` and `wipe_confirmed` only. `id` is a whole-disk `/dev/disk/by-id/<id>` path, not a kernel name (`/dev/nvmeXnY`, `/dev/sdX`) and not a `-partN` partition. `wipe_confirmed` is a boolean. `true` does not authorize a wipe. |

### Node roles and labels

`infra/inventory/roles.yml` is the one constructed inventory. Every env run passes it as the second `-i`, after `envs/<env>/inventory/hosts.yml`. No env copies it. `node_roles` become Ansible groups of the same name, so a play can target `control_plane` or `workers`. Hosts with `data_disk` set are the `data_disk` group. That group is not a role. The `edge` group is the only set of hosts that receive `svccontroller.k3s.cattle.io/enablelb=true`.

Kubernetes labels are `role.<dns_suffix>/<role>=true` for each role on the node. The `role.<dns_suffix>/` prefix is reserved for them. The task that sets and removes them is `p0a-12`. This schema does not apply labels.

The example env declares every known role. A new known role is declared on that env in the same PR (D43, D45).

### Example env

`envs/env-00` is the never-deployed example env. `EXAMPLE_ENV` in `schema/env.py` is that folder name. It sets every known `cluster-vars.yaml` key, including keys that are optional for other envs, and it sets the Hermes model maps. A new known key is added to this env in the same PR (D43, D45). It has no `secrets/`, SOPS file, or `flux/`.

### Explicit env

`bootstrap-secrets`, `seal`, and the cluster-vars renderer require `--env`. The Makefile targets `bootstrap-secrets` and `seal` require `ENV=`. Each fails when the env is omitted (D45).

### Identity values and product rules

`tools/checks/core-names` reads identity values from every env except the example env: the env folder name, inventory hostnames, `ssh_user`, `dns_suffix`, and the owner handle in `flux_git_repo_url` and `apps_git_repo_url`. The owner's personal name is not in the repo. It comes from the `OWNER_NAME` environment variable (a GitHub Actions repository variable of the same name in CI). The check fails when that variable is unset or empty, and the failure text does not include the value.

Scanned paths are `infra/`, `platform/`, `schema/`, `services/`, `tools/`, `docs/`, `rulebooks/`, `.github/` except `CODEOWNERS`, and every `README` / `README.md`. Env names may appear under `envs/**` and `docs/**`. Hostnames, `dns_suffix`, the owner handle, and `ssh_user` forms may appear only under the `envs/<env>/` they were read from. The personal name may appear nowhere.

Matching is case-insensitive, on tokens made of letters, digits, underscores, and hyphens. A dot is a boundary, so a DNS suffix inside a longer hostname fails, and a value inside a longer token passes. `ssh_user` matches only as `/home/<user>`, `<user>@<host>`, or `~<user>`.

Repo URLs outside `envs/<env>/` fail because they contain the owner handle. Bare repo names are not identity values.

Product rules in the same check: the word `bastion`, and any hardcoded `/home/` path. This file and `tools/_core_names.py` name those tokens. That pair is the one exemption, and it covers only the product rules.

`.sops.yaml` has one age creation rule per non-example env, keyed on `envs/<env>/secrets/` (D45). The example env has no root rule.

### Reference shape (documentation)

Each env under `envs/<env>/` supplies its own values (§6, §7), for example: `dns_suffix`, `time_zone`, `code_delivery`, `build_agent`, and optional `ssh_user` (default `ubuntu` when omitted).

## Proven by

- `tools/checks/env-schema` in the required `ci` workflow (added in `p0a-05`).
- P0a exit check: a seeded PR that sets `code_delivery` or `build_agent` to a value other than `github` / `cursor` fails `ci`.
- pytest in `p0a-05`: valid env tree, bad `build_agent`, and `ssh_user` defaulting to `ubuntu`.
- A drift test fails when the known-key set gains a key the example env does not set.
- Behavior tests reject an unknown role, an env without exactly one `control_plane` or exactly one `backup` host, an env with no `edge` host, an unknown host key, and a data disk id that is not a whole-disk `/dev/disk/by-id` path.
- A drift test fails when the known role set gains a role the example env does not declare.
- `ansible-inventory --graph` on the example env shows each role as a group. `site.yml` syntax-checks against that env's hosts file and `infra/inventory/roles.yml`.
- `tools/checks/core-names` rejects identity values on the scanned paths and fails when `OWNER_NAME` is unset.
