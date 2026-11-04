# env-01

## Purpose

Baseline single-node production for JARVIS AlphaCut: one CPU-only node (`bench.lan`). The admin is the only user at launch. No HA.

## What deploys here

Platform only: Flux, Traefik, cert-manager, App DNS, Kyverno, and VolSync backups with the off-host copy. The services and apps layers are empty placeholders. Observability, Grafana, Hermes and hosted apps are not deployed. `secrets/` still holds sealed Grafana and Hermes files for when they return; nothing reads them now.

env-01 values live only under `envs/env-01/`; core stays env-agnostic (`ci/checks/core-names`).

## Promotion

- Routine jarvis changes: agent PRs merge on green `ci`. The planned `admin-gate` check applies to protected paths.
- Infra Ansible (`infra/`): the admin re-runs `site.yml` on the controller node after merge (`infra/ansible/README.md`).
- Images the repo adds are digest-pinned (`ci/checks/digest-pin`).

## New env

Creating an env is [docs/deploy.md](../../docs/deploy.md).
