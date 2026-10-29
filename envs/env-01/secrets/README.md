# env-01 secrets

SOPS-encrypted Kubernetes Secrets for this env. Paths match `.sops.yaml` (`envs/env-01/secrets/*.sops.yaml`).

## Prerequisites (the owner)

On the machine you run this on, install pinned **sops** and **age** plus system packages:

```bash
make deps
```

`make deps` installs [getsops/sops](https://github.com/getsops/sops) and [FiloSottile/age](https://github.com/FiloSottile/age) into `~/.local/bin` (pinned in `tools/versions.env`). It also needs **openssl** and **python3** with **PyYAML** and **bcrypt**; on Debian/Ubuntu it installs missing `openssl` / `python3-yaml` / `python3-bcrypt` packages when `sudo` and `apt-get` are available, otherwise it prints the exact `apt-get` command and exits.

Ensure `~/.local/bin` is on `PATH` on that machine. The age **public** key must already be in the repo root `.sops.yaml`. Encryption does not need the private key. Keep the private key offline except on the controller during bootstrap (R25). The env-01 age key is `~/.config/sops/age/env-01-bench.txt`. `tools/bootstrap-secrets` does not read `age_key_file`. `SOPS_AGE_KEY_FILE` must be set to that path on the same command line as `make bootstrap-secrets ENV=env-01`. `tools/prep-node` can create a missing key; then run that command, commit, and run `site.yml`. If that file is lost, see Recovery in `infra/ansible/README.md` before deleting anything.

## Usage

```bash
make deps && SOPS_AGE_KEY_FILE=~/.config/sops/age/env-01-bench.txt make bootstrap-secrets ENV=env-01
```

Creates missing files only; a second run changes nothing. Commit the generated `*.sops.yaml` files and `ca.crt`; do not commit the age private key.

Bootstrap files (p0a-04): internal CA (`cert-manager-ca`), VolSync restic credentials (`backups-restic`: `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` as the `rest:` server prefix, `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`; a volume's repository URL appends `ns-<namespace>/pvc-<pvc>`), Grafana admin password (`grafana-admin`), and per-deploy Hermes API keys and dashboard credentials (`hermes-ops-*`, `hermes-code-factory-*`). Later secrets (Telegram, Google OAuth, and so on) are added with `make seal` or other make targets. `bootstrap-secrets` does not rewrite an existing file. To rotate `backups-restic`, delete that file and run the command again. Encryption uses the public key in `.sops.yaml`.

The public CA certificate `envs/env-01/ca.crt` is committed. The private key is never written there. An existing `ca.crt` is left as-is, so rotating the CA means deleting both `envs/env-01/secrets/cert-manager-ca.sops.yaml` and `envs/env-01/ca.crt`, then rerunning the bootstrap.
