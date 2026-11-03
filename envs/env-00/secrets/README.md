# Secrets

Shared rules for every env. A copy of this env keeps this file. Secret files are not in the example env. `make bootstrap-secrets` creates them on the copy.

## Bootstrap

`make deps` installs what the tools need. Ensure `~/.local/bin` is on `PATH`.

```bash
make bootstrap-secrets ENV=<env>
```

`SOPS_AGE_KEY_FILE` overrides `age_key_file` when it is set. When it is unset, bootstrap uses `age_key_file` from `envs/<env>/values/cluster-vars.yaml` (default `~/.config/sops/age/keys.txt`). Encryption uses the public recipient in `.sops.yaml` and does not need the private key. Decryption, including restoring a missing `ca.crt` from the sealed CA, uses that private key.

The command creates missing files only. A second run changes nothing. Commit the generated `*.sops.yaml` files and `ca.crt`. Do not commit the age private key.

Files: internal CA (`cert-manager-ca`), VolSync restic credentials (`backups-restic`), Grafana admin password (`grafana-admin`), and per-deploy Hermes API keys and dashboard credentials (`hermes-ops-*`, `hermes-code-factory-*`). Later secrets are added with `make seal`. `backups-restic` holds `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (the `rest:` server prefix), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`. A volume repository URL appends `ns-<namespace>/pvc-<pvc>`.

A new CA permits only that env's `dns_suffix` and its subdomains. The public certificate is `envs/<env>/ca.crt`. The private key is never written there. If `ca.crt` is present and the sealed CA is not, bootstrap refuses, so a new CA cannot be committed next to a different root. Delete both together, and only as part of the CA rotation in `platform/cert-manager/CONTRACT.md`.

If git already records `backups-restic`, bootstrap refuses to recreate it. `make bootstrap-secrets ENV=<env> ROTATE_RESTIC=1` mints a new password anyway. That flag does not add the password to existing repositories. Rotating a live password is in `platform/backups/CONTRACT.md`.

## Age-key custody

Keep two copies of the one age private key. The second copy stays offline, off the controller. A second recipient in `.sops.yaml` is optional and is not required.

Prove each copy. The owner runs this under their own user, never a coding-agent user. The key file must be unreadable by other users (mode `0600`).

```bash
make age-key-check ENV=<env> KEY=<path-to-that-copy>
```

The command test-decrypts one secret of that env and prints `PASS` or `FAIL`. Decrypted secret text is not shown and is not written to a file. `PASS` means that copy opens a secret. `FAIL` means it does not, including when the env has no secrets yet.

### Keep-safe list

In the owner's password manager, keep what must survive a lost controller:

- The age private key, and where the offline copy is stored.

Add a line here when a later change creates something else that must survive that loss.

## Recovery

1. The cluster is up and a copy of the key still exists, including Secret `flux-system/sops-age` key `age.agekey`. Put that key back at `age_key_file`, mode `0600`. Do not delete sealed files and do not run `make bootstrap-secrets`. Prove the restored copy with `make age-key-check`.
2. The cluster is up and every copy of the key is gone. Reseal the live Secrets to a new key: write a new age key, put its public key on the env's creation rule, and replace each sealed file with `make seal --force` from a mode `0600` manifest the owner holds. Do not print Secret values. Prove the new key with `make age-key-check` before replacing `flux-system/sops-age`. Do not run `make bootstrap-secrets`.
3. The cluster is gone as well. Regenerate with `make bootstrap-secrets`. The new files are new values. Follow the restic rotation and the CA rotation before treating them as the live credentials.

prep-node writes a missing `.sops.yaml`, or a missing rule for this env, once. A second run changes nothing. A key that does not match the existing recipient changes nothing and points back at this section.
