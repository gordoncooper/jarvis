# env-01

## Purpose

Baseline single-node production for JARVIS AlphaCut: one CPU-only node (`bench.lan`). The admin is the only user at launch. No HA.

## What deploys here

Phases add components in order: platform (Flux, Traefik, cert-manager, App DNS, VolSync backups), observability (metrics, logs, Gatus, Grafana), Hermes ops and code-factory deploys, then hosted apps from `jarvis-apps` when enabled.

env-01 values live only under `envs/env-01/`; core stays env-agnostic (R3).

## Promotion

- Routine jarvis changes: agent PRs merge on green `ci` until the P2 exit; then `admin-gate` applies to protected paths (R36).
- Infra Ansible (`infra/`): the admin re-runs `site.yml` on the controller node after merge (§8).
- Hermes image bumps: the admin merges; soak between code-factory and ops deploys (R33).

## One-time host cleanup

Delete `${backup_path}/volsync-test` on the node by hand. The VolSync test used a Retain hostPath, and removing that bundle from Git does not remove the directory.

## New env

Create the env empty. Do not copy this folder or its secrets, and do not retarget env-01.

1. Add `envs/<env>/values/` (`cluster-vars.yaml`, `hermes.yaml`, `apps.yaml`) and `envs/<env>/inventory/hosts.yml` with that env's own values and hosts.
2. Add an age creation rule in the repo root `.sops.yaml` for `envs/<env>/secrets/`.
3. `make bootstrap-secrets ENV=<env>`
4. Render the ConfigMap with `python3 tools/lib/render_cluster_vars_configmap.py --env <env> --output envs/<env>/flux/generated/cluster-vars-configmap.yaml`.
5. Add the rest of `envs/<env>/flux/`, with every git path under `envs/<env>/`:
   - `flux-instance.yaml`: sync `url`, `ref` and `path` are this env's `flux_git_repo_url`, `flux_git_ref` and `envs/<env>/flux`. It is the env's only Flux version pin. `site.yml` applies it once on a new cluster, and stops before any change when it is missing.
   - `kustomizations/`: the four Flux Kustomizations `platform-controllers` (`./platform`), `platform-configs` (`./envs/<env>/flux/platform-configs`), `services` (`./services`) and `apps` (`./apps`), chained in that order with `dependsOn`.
   - `platform-configs/kustomization.yaml`: `platform/configs` plus this env's `secrets/` files.
   - `kustomization.yaml`: the generated ConfigMap, the FluxInstance and the four Kustomizations.
