# env-01 secrets

SOPS-encrypted Kubernetes Secrets for this env. Paths match `.sops.yaml` (`envs/*/secrets/*.sops.yaml`).

## Prerequisites (Gordon's user)

On the machine you run this on, install pinned **sops** and **age** plus system packages:

```bash
make deps
```

`make deps` installs [getsops/sops](https://github.com/getsops/sops) and [FiloSottile/age](https://github.com/FiloSottile/age) into `~/.local/bin` (pinned in `tools/versions.env`). It also needs **openssl** and **python3** with **PyYAML**; on Debian/Ubuntu it installs missing `openssl` / `python3-yaml` packages when `sudo` and `apt-get` are available, otherwise it prints the exact `apt-get` command and exits.

Ensure `~/.local/bin` is on `PATH` on that machine. The age **public** key must already be in the repo root `.sops.yaml`. Encryption does not need the private key; Gordon keeps the private key offline (R25).

## Usage

```bash
make deps && make bootstrap-secrets
```

Creates missing files only; a second run changes nothing. Commit the generated `*.sops.yaml` files; do not commit the age private key.

Bootstrap files (p0a-04): internal CA (`cert-manager-ca`), VolSync restic password (`backups-restic`, key `RESTIC_PASSWORD` only — not `RESTIC_REPOSITORY`; the repository path is per PVC from `cluster-vars` `backup_path` when ReplicationSources or the CronJob fallback are wired in p0a-14/p0b), Grafana admin password (`grafana-admin`), and per-deploy Hermes API keys and dashboard credentials (`hermes-ops-*`, `hermes-code-factory-*`). Later secrets (Telegram, Google OAuth, and so on) are added with `make seal` or other make targets.
