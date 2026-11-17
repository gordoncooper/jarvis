# CONTRACT: features

Core pieces an env runs only once it lists them under `features` in `values/switches.yaml`. Each folder is named for its switch and is a row of `FEATURE_COMPONENTS` in `jarvis/env/switches.py`.

## Offers

| Feature | What it adds |
| --- | --- |
| `app-hosting/` | Hosted apps: [CONTRACT.md](app-hosting/CONTRACT.md) |
| `app-dns/` | DNS for `<dns_suffix>`: [CONTRACT.md](app-dns/CONTRACT.md) |
| `apps-backup-server/` | rest-server `restic-apps` for hosted apps' repositories ([backups](../platform/backups/CONTRACT.md)) |
| `offsite-backup-copy/` | CronJob `restic-offsite` to the env's off-host target ([backups](../platform/backups/CONTRACT.md)) |

## Expects

| What | Rule |
| --- | --- |
| Always-on core | Lists no folder here; only an env's rendered wiring does |
| The backup pieces | `core/platform/backups` is up |
| Observability | Not built yet; `observability` is an unknown name until its row exists |

## Health

Each feature's own contract. An env that does not list a feature has none of its objects.
