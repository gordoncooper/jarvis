# Secrets

How an env's secrets are made, kept and recovered. `jarvis init` and `jarvis env add` mint a new env's secrets; `jarvis secrets` mints any that are missing later.

## Bootstrap

```bash
jarvis secrets --env <env>
```

Encryption uses the public recipients in `.sops.yaml`. Decryption (only to restore a missing `ca.crt`) uses the private key at `SOPS_AGE_KEY_FILE`, else `age_key_file`. It creates missing files only; a second run changes nothing. Commit the `*.sops.yaml` files and `ca.crt`, never the private key.

| File | What | When it is missing |
| --- | --- | --- |
| `k3s-token` | `server_token`, `agent_token`; read by `site.yml`, not applied | Refused once any other file exists: the cluster already has a token, which the owner seals ([backups.md](backups.md#rebuild-a-dead-node)). Keep it as long as any etcd snapshot it encrypts. |
| `cert-manager-ca` | The internal CA, constrained to `dns_suffix`; public root `envs/<env>/ca.crt` | Refused while `ca.crt` exists; delete both only to rotate ([cert-manager](../core/platform/cert-manager/CONTRACT.md#rotate-the-ca)) |
| `backups-restic` | Core's restic password and rest-server login | Refused once git records it; `--rotate-restic` mints a new one, which opens no existing repository ([backups.md](backups.md#rotate-the-restic-password)) |
| `app-<name>/backups-restic-app-<name>` | A hosted app's own password and `ns-app-<name>` login, from `jarvis secrets --env <env> --app app-<name>` | Refused once git records it ([backups.md](backups.md#app-credentials)) |
| `flux-deploy-key` | The read-only deploy key of an `ssh://` root | `jarvis secrets --rotate deploy-key` |
| `backups-offsite` | The off-host target, sealed by the owner with `jarvis encrypt` | ([backups.md](backups.md#off-host-copy)) |

Every sealed file needs a reader; one nothing reads fails `jarvis validate`.

## Age-key custody

Every env's secrets are encrypted to two recipients (its rule in `.sops.yaml`): the **working key** on the controller at `age_key_file`, and the **recovery key**, offline only. `jarvis init` prints the recovery key's private half once and keeps no copy; store it on paper or in a password manager entry you do not use day to day. Either key opens every secret. `jarvis validate` fails a sealed file that does not open for exactly those recipients, and `jarvis doctor` warns while an env has only one; `jarvis secrets --add-recovery-key` adds it (prints it once, re-encrypts everything; commit and push).

Prove each copy as the owner, never as a coding-agent user:

```bash
jarvis age-key-check --env <env> --key-file <path-to-that-copy>
```

It test-decrypts one secret and prints `PASS` or `FAIL`, nothing else. A key that opens nothing, an env with no secrets and a file that is not mode `0600` and yours all print `FAIL`.

### Keep-safe list

Keep these where a lost controller cannot take them:

1. The recovery key's private half.
2. With the off-host copy: the target's login and its restic password (`backups-offsite`).

The working key and the deploy key are not on the list: each can be replaced (below). Add a line only when something new cannot be brought back from a key in git.

## Changing recipients and rotating keys

Each needs a key that opens the env's files, changes only the instance repository, and prints no secret value except the recovery key, once.

| Command | What it does |
| --- | --- |
| `jarvis secrets --update-recipients` | Re-encrypts every file whose recipients are not the rule's, after a hand edit of the rule |
| `jarvis secrets --add-recovery-key` | Adds the recovery key to a one-recipient env |
| `jarvis secrets --rotate age` | New working key first in the key file and in the rule (recovery key kept), every file re-encrypted |
| `jarvis secrets --rotate deploy-key` | New read-only deploy key in place of the old; prints its public half |

After `--rotate age`:

1. Commit and push.
2. `kubectl -n flux-system delete secret sops-age && jarvis site --tags flux-operator`. Flux decrypts nothing between steps 1 and 2.
3. Once Flux is Ready, delete every other key's lines from the key file.

After `--rotate deploy-key`: add the printed key to the forge (read-only), commit and push, then `kubectl -n flux-system delete secret flux-deploy-key && jarvis site --tags flux-operator`. Once Flux reads the repository, remove the old key from the forge.

The restic passwords, the CA and the k3s tokens live in running systems too, so no command rotates them: [backups.md](backups.md#rotate-the-restic-password) and [cert-manager](../core/platform/cert-manager/CONTRACT.md#rotate-the-ca). The k3s server token is the datastore passphrase and does not change.

## Recovery

1. **The working key is lost, the recovery key is not:** put the recovery key at `age_key_file` (mode `0600`), run `jarvis secrets --rotate age`, then the steps after it above; the last one takes the recovery key off the controller again.
2. **A copy survives**, including Secret `flux-system/sops-age` key `age.agekey`: put it back at `age_key_file`, mode `0600`, and prove it with `jarvis age-key-check`. Do not delete sealed files or run `jarvis secrets`.
3. **The cluster is up and every key is gone:** make a new age key, put its public key on the env's rule, and reseal each live Secret from a mode `0600` manifest the owner holds, from the instance repository:
   - `secrets/<name>.sops.yaml`: `jarvis encrypt --env <env> --name <name> --force <file>`.
   - `secrets/<namespace>/<name>.sops.yaml`: `jarvis encrypt --env <env> --name <name> --namespace <namespace> --force <file>` (only `--namespace` picks the folder, and must match `metadata.namespace`).
   - `secrets/app-<name>/backups-restic-app-<name>.sops.yaml`: `jarvis encrypt --env <env> --app app-<name> --force <file>`.

   Never print a value. Prove the new key with `jarvis age-key-check` before replacing `flux-system/sops-age`. Do not run `jarvis secrets`.
4. **The cluster is gone too:** `jarvis secrets` mints new values; rotate restic and the CA before treating them as live.

`jarvis node prep` only checks that the key at `age_key_file` is one of the env's recipients; one that is not changes nothing and points here.
