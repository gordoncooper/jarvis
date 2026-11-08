# env-00

Never-deployed example env. Copy this folder to `envs/<env>` in a private instance repository. Copy this example only, never a real env.

Replace the invented values in `values/` and the hosts in `inventory/hosts.yml`, and the placeholder URLs and refs in `values/cluster-vars.yaml` and `flux/sync.yaml`. Set `jarvis_env` to the new folder name. Every `envs/env-00/` path under `flux/`, and `jarvis_env` in the root Kustomization's `postBuild.substitute` in `flux/sync.yaml`, must use that name. `k3s_version` and `k3s_sha256` are already set; the Flux version comes with the core tag (core's `flux/flux-instance.yaml`).

`values/switches.yaml` names what the env enables. It lists the sample add-on `hello` and the sample apps so CI renders them. A copy empties `addons` and `apps`, takes `app-hosting` off `optional`, deletes `values/apps.yaml` (exceptions, backups, app pins), runs `tools/lib/render_switches.py`, and starts core-only.

Then follow [New env](../../docs/deploy.md#new-env).
