# env-01 secrets

SOPS-encrypted Kubernetes Secrets for this env. Paths match `.sops.yaml` (`envs/env-01/secrets/*.sops.yaml`).

Bootstrap, age-key custody, recovery, and the key check are in [envs/env-00/secrets/README.md](../../env-00/secrets/README.md).

## Prerequisites

`make deps` installs what the tools need. Ensure `~/.local/bin` is on `PATH`.

The age key for this env is `~/.config/sops/age/env-01-bench.txt` (`age_key_file`). Keep the private key offline except on the controller. Do not commit it.

## Usage

```bash
make deps && make bootstrap-secrets ENV=env-01
```

`SOPS_AGE_KEY_FILE` overrides `age_key_file` when it is set. Commit the generated `*.sops.yaml` files and `ca.crt`.

The public CA certificate `envs/env-01/ca.crt` is committed. The private key is never written there. Rotating the CA and reissuing the leaf is in `platform/cert-manager/CONTRACT.md`. Rotating the restic password is in `platform/backups/CONTRACT.md`.
