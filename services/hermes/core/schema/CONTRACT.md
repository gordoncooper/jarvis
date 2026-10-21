# CONTRACT: Env schema

Defines what each deployment target under `envs/<env>/` may set. Core stays env-agnostic (R3); env specifics live only under `envs/` (R4). Bot tiers and model names live only in env values (R23). Profile schema is a later section of this file (`p2-04`).

## Offers

- A single validation contract for four files per env:
  - `values/cluster-vars.yaml` — flat keys for Flux postBuild substitution and Ansible host settings (inventory keeps hostnames only; D42).
  - `values/hermes.yaml` — bots, enabled cron, bot tier map, tier-to-model map, and code-factory lane models (with family).
  - `values/apps.yaml` — app hosting on or off and enabled apps with sizes.
  - `inventory/hosts.yml` — hostnames only (no IPs in plain inventory; R25).
- Rejection of unknown `code_delivery` and `build_agent` values (only built providers are accepted; §9 alternatives are named as stubs, not valid values).
- Rejection of unknown top-level keys in `cluster-vars.yaml` (allow-list only).
- Default `ssh_user: ubuntu` when omitted on deployable envs (D34).

## Expects

- One folder per env: `envs/<env>/` with the four files above present when that env is active.
- `cluster-vars.yaml` keys referenced as `${…}` in manifests must exist in that file (R4).
- `flux_git_repo_url` and `apps_git_repo_url` use https and share one GitHub owner per env; that owner must match across every non-example env (D45).
- `llm_provider_host` is a hostname only (no URL scheme or path); Hermes and Gatus read it (D45).

## Health

Not a runtime component. Health is “validation passes”: `tools/checks/env-schema` exits 0 for the env tree.

## Config

All keys in env value files use **lowercase snake_case** (including `cluster-vars.yaml`, `hermes.yaml`, and `apps.yaml`). Validation applies to the fields below. Types are descriptive; the validator (`p0a-05`) is authoritative.

### Identity values and product rules (D44)

These values live only under `envs/<env>/` (not in core). The core-names check enforces them outside `envs/` (see `p0a-07`).

| Value | Where set |
| --- | --- |
| Env name | `envs/<env>/` directory and `jarvis_env` |
| Inventory hostnames | `inventory/hosts.yml` |
| `ssh_user` | `cluster-vars.yaml` (optional; default `ubuntu`) |
| `dns_suffix` | `cluster-vars.yaml` |
| GitHub owner in repo URLs | `flux_git_repo_url`, `apps_git_repo_url` |
| Operator personal name | GitHub Actions repo variable only (never in the repo) |

Core must not name the jump-host role or hardcode operator home directory paths (D44 product rules; enforced by core-names).

### `values/cluster-vars.yaml`

| Field | Required | Notes |
| --- | --- | --- |
| `dns_suffix` | yes | Internal zone suffix for the env (e.g. `<dns_suffix>`). Never hardcoded in core (R3, R28). |
| `time_zone` | yes | IANA zone for the node time zone, VolSync schedules, and Hermes cron (e.g. `<time_zone>`; D21). |
| `ssh_user` | no | SSH login user for node bootstrap; Ansible sets `ansible_user` from it and runs tasks as root (default `ubuntu`; D34). |
| `code_delivery` | yes | `github` only. `forgejo` is the planned second provider (§9); any other value fails validation. |
| `build_agent` | yes | `cursor` only. `grok-build` and `opencode-local` are §9 stubs; any other value fails validation. |
| `backup_path` | yes | Local restic repository directory for VolSync and etcd snapshots (§7; D3). |
| `backup_retention_daily` | yes | Restic keep count for daily snapshots (§7). |
| `backup_retention_weekly` | yes | Restic keep count for weekly snapshots (§7). |
| `backup_retention_monthly` | yes | Restic keep count for monthly snapshots (§7). |
| `jarvis_env` | yes | Env name for `/etc/jarvis/env` marker and guards; must match `envs/<env>/` (p0a-12). |
| `k3s_version` | yes | Host binary version; changes only by PR (R33, D5). |
| `k3s_sha256` | yes | SHA-256 of the pinned `k3s` amd64 release binary (verified on install). |
| `pod_cidr` | yes | Pod network CIDR; set once per env (D4). |
| `service_cidr` | yes | Service network CIDR; set once per env (D4). |
| `local_path_data` | yes | k3s local-path provisioner data directory (D3). |
| `flux_git_repo_url` | yes | Public Git remote for FluxInstance sync (https only; R25). |
| `apps_git_repo_url` | yes | Public apps repo for app hosting and code-factory (https only; D45). |
| `flux_git_ref` | yes | Git ref for FluxInstance sync (e.g. `refs/heads/main`). |
| `llm_provider_host` | yes | AI provider API hostname (e.g. `api.x.ai`); no scheme or path (D45). |
| `ntp_pools` | yes | Chrony pool hostnames (base-os role); each entry is one `pool … iburst maxsources 4 nts` line. |
| `k3s_kubelet_kube_reserved` | yes | kubelet `kube-reserved` value (without the `kube-reserved=` prefix). |
| `k3s_kubelet_system_reserved` | yes | kubelet `system-reserved` value (without the `system-reserved=` prefix). |
| `k3s_kubelet_eviction_hard` | yes | kubelet `eviction-hard` threshold expression. |

Ingress class is a product constant in manifests (bundled Traefik; D32, D46), not an env setting.

### cluster-vars → Flux postBuild (D45)

- PostBuild variables use lowercase cluster-vars keys (`${dns_suffix}`, etc.).
- Lists and maps in `cluster-vars.yaml` are JSON-encoded strings in the rendered ConfigMap.
- Flux kustomize-controller `StrictPostBuildSubstitutions` must fail on a missing variable, not substitute empty.
- Literal `${…}` in manifests that is not a cluster var is written `$${…}`.

### env-00 reference env (D43)

`envs/env-00/` is never deployed. It sets **every** known `cluster-vars.yaml` key (required and optional) with invented example values. CI fails if the schema gains a key env-00 does not set. Inventory lists seven example hosts; `node_roles` and `data_disk` are added in `p0a-11a`. env-00 is excluded from core-names forbidden sources.

When adding a new cluster-vars key, set it in env-00 in the same PR.

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

Hostnames only. No IPs in plain inventory (R25, D42). Per-host `node_roles` and optional `data_disk` are validated in `p0a-11a`.

## Proven by

- `tools/checks/env-schema` in the required `ci` workflow (added in `p0a-05`).
- P0a exit check: a seeded PR that sets `code_delivery` or `build_agent` to a value other than `github` / `cursor` fails `ci`.
- pytest in `p0a-05` / `p0a-05a`: valid env tree, bad `build_agent`, `ssh_user` defaulting to `ubuntu`, env-00 completeness, and unknown cluster-vars keys rejected.
