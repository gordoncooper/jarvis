# CONTRACT: Env schema

Defines what each deployment target under `envs/<env>/` may set. Core stays env-agnostic (R3); env specifics live only under `envs/` (R4). Bot tiers and model names live only in env values (R23). Profile schema is a later section of this file (`p2-04`).

## Offers

- A single validation contract for four files per env:
  - `values/cluster-vars.yaml` — flat keys for Flux postBuild substitution and Ansible host settings (inventory keeps hostnames only; sensitive vars stay in SOPS group_vars).
  - `values/hermes.yaml` — bots, enabled cron, bot tier map, tier-to-model map, and code-factory lane models (with family).
  - `values/apps.yaml` — app hosting on or off and enabled apps with sizes.
  - `inventory/hosts.yml` — hostnames only (no IPs in plain inventory; R25).
- Rejection of unknown `code_delivery` and `build_agent` values (only built providers are accepted; §9 alternatives are named as stubs, not valid values).
- Default `ssh_user: ubuntu` when omitted (D34).

## Expects

- One folder per env: `envs/<env>/` with the four files above present when that env is active.
- `cluster-vars.yaml` keys referenced as `${…}` in manifests must exist in that file (R4).
- Gordon creates `jarvis` and `jarvis-apps` under `gordoncooper` before env values land in this repo (D1).

## Health

Not a runtime component. Health is “validation passes”: `tools/checks/env-schema` exits 0 for the env tree.

## Config

All keys in env value files use **lowercase snake_case** (including `cluster-vars.yaml`, `hermes.yaml`, and `apps.yaml`). Validation applies to the fields below. Types are descriptive; the validator (`p0a-05`) is authoritative.

### `values/cluster-vars.yaml`

| Field | Required | Notes |
| --- | --- | --- |
| `dns_suffix` | yes | Internal zone suffix for the env (env-01: `alpha.lan`). Never hardcoded in core (R3, R28). |
| `time_zone` | yes | IANA zone for the node time zone, VolSync schedules, and Hermes cron (env-01: `America/Los_Angeles`; D21). |
| `ssh_user` | no | SSH login user for node bootstrap; Ansible sets `ansible_user` from it and runs tasks as root (default `ubuntu`; D34). |
| `code_delivery` | yes | `github` only. `forgejo` is the planned second provider (§9); any other value fails validation. |
| `build_agent` | yes | `cursor` only. `grok-build` and `opencode-local` are §9 stubs; any other value fails validation. |
| `backup_path` | yes | Local restic repository directory for VolSync and etcd snapshots (§7; env-01 path set in `p0a-03`). |
| `backup_retention_daily` | yes | Restic keep count for daily snapshots (§7). |
| `backup_retention_weekly` | yes | Restic keep count for weekly snapshots (§7). |
| `backup_retention_monthly` | yes | Restic keep count for monthly snapshots (§7). |
| `k3s_version` | yes | Host binary version; changes only by PR (R33, D5). |
| `pod_cidr` | yes | Pod network CIDR; set once per env (D4). |
| `service_cidr` | yes | Service network CIDR; set once per env (D4). |
| `ingress_class` | when ingress exists | Ingress class from bundled Traefik (D32). |
| Feature toggles and sizing | per env | Role labels map roles to nodes; sizes and enabled platform features are env-specific patches, not core logic. |

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

Hostnames only. IPs, if needed, belong in encrypted `group_vars/*.sops.yaml` (D2, R25).

### env-01 reference values (documentation)

env-01 is the one-node baseline (§6, §7): `dns_suffix: alpha.lan`, `time_zone: America/Los_Angeles`, `code_delivery: github`, `build_agent: cursor`, default `ssh_user` unless overridden.

## Proven by

- `tools/checks/env-schema` in the required `ci` workflow (added in `p0a-05`).
- P0a exit check: a seeded PR that sets `code_delivery` or `build_agent` to a value other than `github` / `cursor` fails `ci`.
- pytest in `p0a-05`: valid env-01 tree, bad `build_agent`, and `ssh_user` defaulting to `ubuntu`.
