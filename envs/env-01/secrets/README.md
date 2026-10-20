# env-01 secrets

SOPS-encrypted Kubernetes Secrets for this env. Paths match `.sops.yaml` (`envs/*/secrets/*.sops.yaml`).

Run on the bastion (Gordon's user, after the age public key is in `.sops.yaml`):

```bash
make bootstrap-secrets
```

Creates missing files only; a second run changes nothing. Commit the generated `*.sops.yaml` files; do not commit the age private key.

Bootstrap files (p0a-04): internal CA (`cert-manager-ca`), VolSync restic password (`backups-restic`), Grafana admin password (`grafana-admin`), and per-deploy Hermes API keys and dashboard credentials (`hermes-ops-*`, `hermes-code-factory-*`). Later secrets (Telegram, Google OAuth, and so on) are added with `make seal` or other make targets.
