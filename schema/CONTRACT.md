# CONTRACT: Env schema

What each env under `envs/<env>/` may set. Run from an instance repository, the jarvis CLI points every check at `<instance>/envs/<env>` and `<instance>/.sops.yaml` (`instance_root` in `jarvis/env/runtime.py`). Every key, its type, default and meaning: [docs/reference.md](../docs/reference.md), rendered from the schemas by `python -m jarvis.schema.reference`.

## Offers

| What | Where |
| --- | --- |
| The shape of each values file: keys, types, patterns, defaults. A file names its schema on its first line (`# yaml-language-server: $schema=…`), so an editor checks it as you type. | `schema/cluster-vars.schema.json`, `hosts.schema.json`, `switches.schema.json`, `apps.schema.json` |
| The rules that span fields or files | `jarvis/schema/env.py`, the `env-schema` check of `jarvis validate` |
| That the committed `flux/` is the render of the values | `jarvis render --check`, the `render` check of `jarvis validate` |
| Resolved cluster-vars: the file with each schema `default` filled in | `resolve_cluster_vars` |
| Node roles: each is an Ansible group (with `infra/inventory/roles` as the second `-i`) and the label `node-role.jarvis.io/<role>=true`, set by the k3s role. A node with neither `workers` nor `edge` gets the taint `node-role.jarvis.io/dedicated=true:NoSchedule`; only `edge` nodes get `svccontroller.k3s.cattle.io/enablelb=true`. A role arrives with the first service that schedules by it. | `hosts.schema.json`, the k3s role |
| A pre-host mode for `jarvis node prep`, before the env's keys exist: skips the `.sops.yaml` rule per env and the recipient check | `validate_env_tree(…, pre_host=True)` |

## Expects

| Rule | Checked by |
| --- | --- |
| `values/cluster-vars.yaml` and `inventory/hosts.yml` exist | env-schema |
| `jarvis_env` is the env's folder name; `time_zone` is an IANA zone | env-schema |
| `pod_cidr` and `service_cidr` are CIDRs; `app_dns_cluster_ip` is a host address in `service_cidr`, not its first or tenth | env-schema |
| `lan_cidrs` are IPv4 CIDRs inside RFC 1918, clear of the cluster's ranges | env-schema |
| An `ssh://` root's host is among `flux_known_hosts`; unset, only `github.com` | env-schema |
| Across hosts: exactly one `control_plane` and one `backup` host, at least one `edge` host | env-schema |
| Once the env has `flux/` or sealed files, `values/switches.yaml` exists | env-schema |
| Each add-on in `switches.yaml` is a folder under core's `addons/`; at most one per slot (label `addons.jarvis.io/slot`) | env-schema, and the render refuses before it writes |
| Each app in `switches.yaml` has `<app>/app.yaml` in the apps repository (`apps_git_repo_url` beside the instance, or `APPS_REPO`) | env-schema, render |
| An env that lists apps lists `app-hosting` and sets `apps_git_repo_url`; each listed app has one `versions` pin, and only listed apps | env-schema, render |
| Each exception line in `apps.yaml` is one known grant for a listed app; `lan-all` and `lan-target` need `lan_cidrs` | env-schema, render |
| A `backups:` selection names only listed, backup-eligible apps; an app file the checkout cannot read fails | env-schema |
| An env that registers an app credential lists `apps-backup-server` | env-schema |
| Once sealed files exist: `k3s-token`, `cert-manager-ca`, `backups-restic`; `backups-offsite` while the off-host copy is listed; each registered app's credential in `secrets/app-<name>/`; a sealed file in each folder an enabled add-on decrypts | env-schema |
| An `ssh://` root has `secrets/flux-deploy-key.sops.yaml` | env-schema |
| Every sealed file has a reader, and opens for exactly the env's recipients | env-schema |
| `.sops.yaml` has one age creation rule per env, and none for an env that does not exist | env-schema |
| Deleting `backups:` while apps register refuses; set `selected: []` and re-render first | render |
| Every `${…}` a manifest reads exists; Flux's strict substitution fails the reconcile otherwise | `jarvis validate` builds with `flux build --strict-substitute` |
| Core holds no env name, hostname, `dns_suffix`, `ssh_user` or owner handle | review |

## Health

| Check | Pass |
| --- | --- |
| `jarvis validate` | `PASS env-schema` and `PASS render` for every env |
| `ci/checks/sample-instance` | the same checks on a sample instance written by `jarvis init`'s code |
