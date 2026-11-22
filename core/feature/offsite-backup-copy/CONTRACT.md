# CONTRACT: offsite-backup-copy

A copy of every local backup repository on a target off the backup host. A feature: `offsite-backup-copy` under `features`. Runbook: [docs/backups.md](../../../docs/backups.md#off-host-copy).

## Offers

| What | Detail |
| --- | --- |
| CronJob `platform-backups/restic-offsite` | Daily 04:00: `restic copy --no-lock` of every local repository into `<backups-offsite RESTIC_REPOSITORY><path>`, `init --copy-chunker-params` when missing. Never forgets or prunes. An `ns-app-*` repository is copied with that app's password on both sides. |

## Expects

| What | From |
| --- | --- |
| `secrets/backups-offsite.sops.yaml` with `RESTIC_REPOSITORY` (ending `/`; `rest:` or `s3:`, not `sftp:`), `RESTIC_PASSWORD` (not the local one), and the backend's login variables | the owner |
| The local repositories, and a node labelled `node-role.jarvis.io/backup=true` | [backups](../../platform/backups/CONTRACT.md) |

## Health

| Check | Pass |
| --- | --- |
| `restic-offsite` | last success within one interval plus 12 hours; before the first success, a warning while the first run is due |

## Limits

- The off-host side only grows; its owner prunes it.
