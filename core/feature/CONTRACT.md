# CONTRACT: features

Core pieces an env runs only once it lists them under `features` in `values/switches.yaml`. Each folder is named for its switch and is a row of `FEATURE_COMPONENTS` in `jarvis/env/switches.py`.

## Offers

| Feature | What it adds |
| --- | --- |
| `alerts/` | Failed reconciles posted to the env's target: [CONTRACT.md](alerts/CONTRACT.md) |
| `app-hosting/` | Hosted apps and their backups: [CONTRACT.md](app-hosting/CONTRACT.md) |
| `app-dns/` | DNS for `<dns_suffix>`: [CONTRACT.md](app-dns/CONTRACT.md) |
| `offsite-backup-copy/` | Every local repository copied to the env's off-host target: [CONTRACT.md](offsite-backup-copy/CONTRACT.md) |

## Expects

| What | Rule |
| --- | --- |
| Always-on core | Lists no folder here; only an env's rendered wiring does |
| The backup pieces | `core/platform/backups` is up |

## Health

Each feature's own contract. An env that does not list a feature has none of its objects.
