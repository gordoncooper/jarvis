# Design note: P0a catch-up PR 1 (p0a-03, p0a-05, p0a-05a)

**Base:** `main` @ eb71301. **Status:** implemented per review.

## Scope

- **env-01:** Keep `dns_suffix: alpha.lan` (plan p0a-03). Add `apps_git_repo_url`, `llm_provider_host: api.x.ai`; keep `flux_git_repo_url`. Fix `hosts.yml` comment (D42).
- **Schema + CONTRACT.md:** D45 (`apps_git_repo_url`, `llm_provider_host`, owner rule, env-00 section, postBuild notes); remove `ingress_class` as env config (D46).
- **env-00:** Every known `cluster-vars` key with invented values; full Hermes model maps; seven hostnames in `inventory/hosts.yml` only (roles/disks in PR 3).
- **Tools:** `bootstrap-secrets`, `seal`, `render_cluster_vars_configmap.py` require `--env`; removed `sole_env_directory_name`. Tests use `env-01` explicitly.
- **core-names:** Exclude `env-00` from forbidden-name sources when collecting from envs.

## Patterns

`services/hermes/core/schema/env.py`, `tools/checks/env-schema`, `tools/tests/test_env_schema.py`, four files per env (no `nodes.yaml` on main).

## Verification

`make ci-verify` on bastion before push.
