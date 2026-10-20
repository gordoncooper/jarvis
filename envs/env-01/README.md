# env-01

## Purpose

Baseline single-node production for JARVIS AlphaCut: one CPU-only node (`bench.lan`). Gordon is the only user at launch. No HA.

## What deploys here

Phases add components in order: platform (Flux, Traefik, cert-manager, LAN DNS, VolSync backups), observability (metrics, logs, Gatus, Grafana), Hermes ops and code-factory deploys, then hosted apps from `jarvis-apps` when enabled.

env-01 values live only under `envs/env-01/`; core stays env-agnostic (R3).

## Promotion

- Routine jarvis changes: agent PRs merge on green `ci` until the P2 exit; then `admin-gate` applies to protected paths (R36).
- Infra Ansible (`infra/`): Gordon re-runs `site.yml` on the bastion after merge (§8).
- Hermes image bumps: Gordon merges; soak between code-factory and ops deploys (R33).
- New envs: copy this folder when hardware exists; do not retarget env-01 casually.
