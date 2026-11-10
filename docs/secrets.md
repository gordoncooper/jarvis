# Secrets

How an env's secrets are made, kept and recovered. `jarvis init` (and `jarvis env add`) mints a new env's secrets; `jarvis secrets` mints any that are missing later.

## Bootstrap

The jarvis CLI installs what these commands need for the core release the env pins (docs/deploy.md, Install tools).

```bash
jarvis secrets --env <env>
```

`SOPS_AGE_KEY_FILE` overrides `age_key_file` when it is set. When it is unset, bootstrap uses `age_key_file` from `envs/<env>/values/cluster-vars.yaml` (default `~/.config/sops/age/keys.txt`). Encryption uses the public recipient in the instance repository's `.sops.yaml` and does not need the private key. Decryption, including restoring a missing `ca.crt` from the sealed CA, uses that private key.

The command creates missing files only. A second run changes nothing. Commit the generated `*.sops.yaml` files and `ca.crt`. Do not commit the age private key.

Files: k3s server and agent tokens (`k3s-token`, keys `server_token` and `agent_token`; read by `site.yml` on the controller, not applied to the cluster), internal CA (`cert-manager-ca`), VolSync restic credentials (`backups-restic`). Nothing else is generated. Later secrets are added with `jarvis encrypt`. `backups-restic` holds `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` (the `rest:` server prefix), `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`. A volume repository URL appends `ns-<namespace>/pvc-<pvc>`.

A new CA permits only that env's `dns_suffix` and its subdomains. The public certificate is `envs/<env>/ca.crt`. The private key is never written there. If `ca.crt` is present and the sealed CA is not, bootstrap refuses, so a new CA cannot be committed next to a different root. Delete both together, and only as part of the CA rotation in `core/platform/cert-manager/CONTRACT.md`.

If `k3s-token` is missing and any other file above is present, bootstrap refuses: that env's cluster already has a token, and the owner seals it (`core/platform/backups/CONTRACT.md`, Rebuild a dead node). Keep the sealed token as long as any etcd snapshot it encrypts.

If `backups-restic` is missing and git already records it, bootstrap refuses to recreate it. `jarvis secrets --env <env> --rotate-restic` then mints a new password anyway; a file that exists stays as it is. That flag does not add the password to existing repositories. Rotating a live password is in `core/platform/backups/CONTRACT.md`.

A hosted app that keeps data gets its own credential: `jarvis secrets --env <env> --app app-<name>` seals `app-<name>/backups-restic-app-<name>.sops.yaml` (a restic password, an apps-server login `ns-app-<name>`, and its bcrypt `htpasswd` line) and writes the Component that registers it. It mints nothing else, and refuses when that app's file is missing and git already records it. Registering it, and retiring it, is in `core/platform/backups/CONTRACT.md`, App credentials.

`backups-offsite` is not generated: the owner sets up the off-host target and seals it with `jarvis encrypt` (`core/platform/backups/CONTRACT.md`, Off-host copy). The env turns the copy on by listing `offsite-backup-copy` under `features` in `values/switches.yaml`.

Every sealed file needs a reader. A file nothing reads (a left-over secret, or a folder no enabled add-on decrypts) fails `jarvis validate`. Delete it, or enable what uses it.

## Age-key custody

Every env's secrets are encrypted to two age recipients (its creation rule in `.sops.yaml`): the **working key** first, on the controller at `age_key_file`, and the **recovery key** second, offline only. `jarvis init` makes the recovery key, prints its private half once and keeps no copy; store it on paper or in a password manager entry you do not use day to day. Either key opens every secret, so losing the working key is not fatal. `jarvis validate` fails when a sealed file does not open for exactly the rule's recipients, and `jarvis doctor` warns while an env has only one.

An env made before v0.5.0 has the working key only. Give it the recovery key once:

```bash
jarvis secrets --add-recovery-key
```

It prints the recovery key's private half once, adds its public key to the rule, and re-encrypts every sealed file to both. Commit and push the result. Run again, it changes nothing.

Prove each copy. The owner runs this under their own user, never a coding-agent user. The key file must be unreadable by other users (mode `0600`).

```bash
jarvis age-key-check --env <env> --key-file <path-to-that-copy>
```

The command test-decrypts one secret of that env and prints `PASS` or `FAIL`. Decrypted secret text is not shown and is not written to a file. `PASS` means that copy opens a secret. `FAIL` is the same line when the key cannot open a secret, when the env has no secrets yet, and when the file is not mode `0600` or not owned by you. Test a mode `0600` copy owned by you.

### Keep-safe list

Everything else in git opens with one of these. Keep them where a lost controller cannot take them:

1. The recovery key's private half.
2. When the env has the off-host copy: the off-host target's login and its restic password (`backups-offsite`). Recovering from the off-host copy, when this repository is gone as well, needs them.

The working key is not on the list: lost, it is replaced from the recovery key (Recovery, below). Nor is the read-only deploy key in `flux-deploy-key`, when the env's root is `ssh://`: the forge holds the public half, and `jarvis secrets --rotate deploy-key` replaces it.

Add a line here only when a later change creates something else that no key in git can bring back.

## Changing recipients and rotating keys

Each command needs a key that opens the env's files (`SOPS_AGE_KEY_FILE`, else `age_key_file`), changes only the instance repository, and prints what to do next. None prints a secret value except the recovery key, once.

| Command | What it does |
| --- | --- |
| `jarvis secrets --update-recipients` | Re-encrypts every sealed file whose recipients are not the rule's. Run it after you change the rule by hand. |
| `jarvis secrets --add-recovery-key` | Adds the recovery key to an env that has one recipient (above). |
| `jarvis secrets --rotate age` | Makes a new working key, puts it first in the key file beside what it held, puts it in place of the rule's first recipient (the recovery key stays), and re-encrypts every file. |
| `jarvis secrets --rotate deploy-key` | Seals a new read-only deploy key in place of the old one and prints its public half. |

After `--rotate age`:

1. Commit and push.
2. Replace the cluster's copy: `kubectl -n flux-system delete secret sops-age && jarvis site --tags flux-operator`. The role recreates `sops-age` from the key file's identities that are in the rule, so the old key is left out. Between steps 1 and 2 Flux cannot decrypt and applies nothing; it carries on once both are done.
3. Once Flux is Ready, delete every other key's lines from the key file, so it holds the new working key only.

After `--rotate deploy-key`: add the printed public key to the instance repository on the forge (read-only), commit and push, then `kubectl -n flux-system delete secret flux-deploy-key && jarvis site --tags flux-operator`. Once Flux reads the repository again, remove the old deploy key from the forge.

The restic passwords, the cluster CA and the k3s tokens are not rotated by a command. Each lives in a running system as well as in git, so a new value must be put into that system first: a restic repository keeps its own key list, cert-manager issues from the CA, and every k3s node joined with the token. Their procedures are in `core/platform/backups/CONTRACT.md` (restic) and `core/platform/cert-manager/CONTRACT.md` (CA); the k3s server token is the datastore passphrase and does not change, and the agent token changes at a rebuild (`core/platform/backups/CONTRACT.md`, k3s token). `--rotate-restic` mints a new password for a missing file only (Bootstrap, above).

## Recovery

1. The working key is lost and the recovery key is not: put the recovery key at `age_key_file` (mode `0600`) and run `jarvis secrets --rotate age`. The new key takes the lost one's place in the rule and every file is re-encrypted; then follow the steps after `--rotate age` above. The last one removes the recovery key from the controller again.
2. The cluster is up and a copy of the key still exists, including Secret `flux-system/sops-age` key `age.agekey`. Put that key back at `age_key_file`, mode `0600`. Do not delete sealed files and do not run `jarvis secrets`. Prove the restored copy with `jarvis age-key-check`.
3. The cluster is up and every copy of both keys is gone (the cluster's `sops-age` included). Reseal the live Secrets to a new key: write a new age key, put its public key on the env's creation rule, and replace each sealed file from a mode `0600` manifest the owner holds. Run each command from the instance repository, so encryption uses its `.sops.yaml`.
   - Top-level secret, `secrets/<name>.sops.yaml` (such as `k3s-token`, `backups-restic`, `cert-manager-ca` or `backups-offsite`): `jarvis encrypt --env <env> --name <name> --force <file>`.
   - Add-on secret, `secrets/<namespace>/<name>.sops.yaml`: `jarvis encrypt --env <env> --name <name> --namespace <namespace> --force <file>`. Only `--namespace` chooses the folder, not `metadata.namespace`. Without it, the tool writes a new top-level file and leaves the nested one encrypted to the lost key. The manifest's `metadata.namespace` must match `--namespace`.
   - App credential, `secrets/app-<name>/backups-restic-app-<name>.sops.yaml`: `jarvis encrypt --env <env> --app app-<name> --force <file>`. The tool names the file and keeps the Secret in `platform-backups`, where the backup policy reads it. Any other form for this file is refused.

   Do not print Secret values. Prove the new key with `jarvis age-key-check` before replacing `flux-system/sops-age`. Do not run `jarvis secrets`.
4. The cluster is gone as well. Regenerate with `jarvis secrets`. The new files are new values. Follow the restic rotation and the CA rotation before treating them as the live credentials.

`jarvis init` and `jarvis env add` write the env's creation rule. `jarvis node prep` only checks that the key at `age_key_file` is one of the env's recipients; a key that is not changes nothing and points back at this section.
