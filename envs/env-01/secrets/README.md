# env-01 secrets

SOPS-encrypted Kubernetes Secrets for this env. Paths match `.sops.yaml` (`envs/*/secrets/*.sops.yaml`).

## Bastion prerequisites (Gordon's user)

Install on the bastion before running `make bootstrap-secrets`:

- **sops** — [getsops/sops](https://github.com/getsops/sops) on `PATH`
- **age** — [FiloSottile/age](https://github.com/FiloSottile/age) (SOPS age backend)
- **openssl** — system package
- **python3** and **PyYAML** — e.g. `python3-yaml` (Debian/Ubuntu) or `pip install PyYAML` in a venv

The age **public** key must already be in the repo root `.sops.yaml`. Encryption does not need the private key; Gordon keeps the private key offline (R25).

## Usage

```bash
make bootstrap-secrets
```

Creates missing files only; a second run changes nothing. Commit the generated `*.sops.yaml` files; do not commit the age private key.

Bootstrap files (p0a-04): internal CA (`cert-manager-ca`), VolSync restic password (`backups-restic`), Grafana admin password (`grafana-admin`), and per-deploy Hermes API keys and dashboard credentials (`hermes-ops-*`, `hermes-code-factory-*`). Later secrets (Telegram, Google OAuth, and so on) are added with `make seal` or other make targets.
