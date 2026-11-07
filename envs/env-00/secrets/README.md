# Secrets

Shared rules for every env. A copy of this env keeps this file. Secret files are not in the example env. `make bootstrap-secrets` creates them on the copy.

## Bootstrap

`make deps` installs what the tools need. Ensure `~/.local/bin` is on `PATH`.

```bash
make bootstrap-secrets ENV=<env>
```

`SOPS_AGE_KEY_FILE` overrides `age_key_file` when it is set. When it is unset, bootstrap uses `age_key_file` from `envs/<env>/values/cluster-vars.yaml` (default `~/.config/sops/age/keys.txt`). Encryption uses the public recipient in the instance checkout's `.sops.yaml` (`INSTANCE=<path>`) and does not need the private key. Decryption, including restoring a missing `ca.crt` from the sealed CA, uses that private key.

The command creates missing files only. A second run changes nothing. Commit the generated `*.sops.yaml` files and `ca.crt`. Do not commit the age private key.

Files: k3s server and agent tokens (`k3s-token`, keys `server_token` and `agent_token`; read by `site.yml` on the controller, not applied to the cluster), internal CA (`cert-manager-ca`), VolSync restic credentials (`backups-restic`). Nothing else is generated. Later secrets are added with `make seal`. `backups-restic` holds `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (the `rest:` server prefix), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`. A volume repository URL appends `ns-<namespace>/pvc-<pvc>`.

A new CA permits only that env's `dns_suffix` and its subdomains. The public certificate is `envs/<env>/ca.crt`. The private key is never written there. If `ca.crt` is present and the sealed CA is not, bootstrap refuses, so a new CA cannot be committed next to a different root. Delete both together, and only as part of the CA rotation in `platform/cert-manager/CONTRACT.md`.

If `k3s-token` is missing and any other file above is present, bootstrap refuses: that env's cluster already has a token, and the owner seals it (`platform/backups/CONTRACT.md`, Rebuild a dead node). Keep the sealed token as long as any etcd snapshot it encrypts.

If `backups-restic` is missing and git already records it, bootstrap refuses to recreate it. `make bootstrap-secrets ENV=<env> ROTATE_RESTIC=1` then mints a new password anyway; a file that exists stays as it is. That flag does not add the password to existing repositories. Rotating a live password is in `platform/backups/CONTRACT.md`.

A hosted app that keeps data gets its own credential: `make bootstrap-secrets ENV=<env> APP=app-<name>` seals `app-<name>/backups-restic-app-<name>.sops.yaml` (a restic password, an apps-server login `ns-app-<name>`, and its bcrypt `htpasswd` line) and writes the Component that registers it. It mints nothing else, and refuses when that app's file is missing and git already records it. Registering it, and retiring it, is in `platform/backups/CONTRACT.md`, App credentials. This example env registers no app.

`backups-offsite` is not generated: the owner sets up the off-host target and seals it with `make seal` (`platform/backups/CONTRACT.md`, Off-host copy). This example env lists `offsite-backup-copy` under `optional` in `values/switches.yaml` to show the copy turned on; a copy that does not want it takes it off that list and re-renders.

## Age-key custody

Keep two copies of the one age private key. The second copy stays offline, off the controller. A second recipient in `.sops.yaml` is optional and is not required.

Prove each copy. The owner runs this under their own user, never a coding-agent user. The key file must be unreadable by other users (mode `0600`).

```bash
make age-key-check ENV=<env> KEY=<path-to-that-copy>
```

The command test-decrypts one secret of that env and prints `PASS` or `FAIL`. Decrypted secret text is not shown and is not written to a file. `PASS` means that copy opens a secret. `FAIL` is the same line when the key cannot open a secret, when the env has no secrets yet, and when the file is not mode `0600` or not owned by you. Test a mode `0600` copy owned by you.

### Keep-safe list

In the owner's password manager, keep what must survive a lost controller:

- The age private key, and where the offline copy is stored.
- When the env has the off-host copy: the off-host restic password in `backups-offsite`, and the target's login. Recovering from the off-host copy needs them, and the sealed file needs the age key.

Not a custody item: the read-only deploy key in `flux-deploy-key`, when the env's root is `ssh://`. The forge holds the public half. If the private half is lost, regenerate it ([docs/deploy.md](../../../docs/deploy.md#deploy-key)) and replace the deploy key on the forge.

Add a line here when a later change creates something else that must survive that loss.

## Recovery

1. The cluster is up and a copy of the key still exists, including Secret `flux-system/sops-age` key `age.agekey`. Put that key back at `age_key_file`, mode `0600`. Do not delete sealed files and do not run `make bootstrap-secrets`. Prove the restored copy with `make age-key-check`.
2. The cluster is up and every copy of the key is gone. Reseal the live Secrets to a new key: write a new age key, put its public key on the env's creation rule, and replace each sealed file from a mode `0600` manifest the owner holds. For a real env, set `INSTANCE=<path>` on each command as shown, so encryption uses that checkout's `.sops.yaml`.
   - Top-level secret, `secrets/<name>.sops.yaml` (such as `k3s-token`, `backups-restic`, `cert-manager-ca` or `backups-offsite`): `INSTANCE=<path> .venv/bin/python tools/seal --env <env> --name <name> --force <file>`.
   - Add-on secret, `secrets/<namespace>/<name>.sops.yaml`: `INSTANCE=<path> .venv/bin/python tools/seal --env <env> --name <name> --namespace <namespace> --force <file>`. Only `--namespace` chooses the folder, not `metadata.namespace`. Without it, the tool writes a new top-level file and leaves the nested one encrypted to the lost key. The manifest's `metadata.namespace` must match `--namespace`.
   - App credential, `secrets/app-<name>/backups-restic-app-<name>.sops.yaml`: `INSTANCE=<path> .venv/bin/python tools/seal --env <env> --app app-<name> --force <file>`. The tool names the file and keeps the Secret in `platform-backups`, where the backup policy reads it. Any other form for this file is refused.

   Build each manifest rather than copying a live Secret. It holds `apiVersion`, `kind`, `type`, `metadata.name`, `metadata.namespace`, any annotations the sealed file already carries, and the decoded values under `stringData`. Leave out the live Secret's `uid`, `resourceVersion`, `creationTimestamp`, `managedFields` and Flux labels. Delete the manifest once it is sealed.

   Do not print Secret values. Prove the new key with `make age-key-check` before replacing `flux-system/sops-age`. Do not run `make bootstrap-secrets`.
3. The cluster is gone as well. Regenerate with `make bootstrap-secrets`. The new files are new values. Follow the restic rotation and the CA rotation before treating them as the live credentials.

prep-node writes a missing `.sops.yaml`, or a missing rule for this env, once. A second run changes nothing. A key that does not match the existing recipient changes nothing and points back at this section.
