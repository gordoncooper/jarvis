# env-00

Never-deployed example env. Copy this folder to `envs/<env>` in a private instance repository. Copy this example only, never a real env.

Replace the invented values in `values/` and the hosts in `inventory/hosts.yml`, and the placeholder URLs and refs in `values/cluster-vars.yaml` and `flux/sync.yaml`. Set `jarvis_env` to the new folder name. Every `envs/env-00/` path under `flux/` must use that name. `k3s_version`, `k3s_sha256`, and the Flux version pin are already set.

Then follow [New env](../../docs/deploy.md#new-env).
