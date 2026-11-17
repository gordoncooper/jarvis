# Ansible (node bootstrap)

`jarvis site --env <env>` runs `site.yml` from the instance repository, after the env schema passes. Settings come from the env's `cluster-vars.yaml` (`jarvis_cluster_vars`); the inventory is the env's hosts file, then core's `infra/inventory/roles`, which turns each `node_roles` entry into a group.

1. The opening play (tag `always`) checks every host before any change: one control-plane host, amd64, the required cluster-vars, core's `core/flux/flux-instance.yaml` and the env's `flux/sync.yaml`, no foreign or unmarked k3s, and each declared data disk.
2. `base-os` on every host.
3. The k3s server on `control_plane`, then every other host as an agent, one at a time. Node role labels and the dedicated taint.
4. `kubeconfig` and `flux-operator` on `control_plane`.

Before a run, every inventory hostname resolves on the controller and every node, `ssh_user` has key login and passwordless sudo, and `known_hosts` is seeded; `jarvis node prep` gets a node there. Any run that touches k3s (`--tags k3s`, untagged, `--check`, adding a node) decrypts `secrets/k3s-token.sops.yaml` on the controller, so it needs the age key file (`age_key_file`, or `SOPS_AGE_KEY_FILE`; mode `0600` and yours). `--tags labels` needs none.

## Prepare a node

```bash
jarvis node prep <hostname> --env <env> --login-user <user>
```

The host name must be an inventory key and resolve with `getent hosts`. It settles the Ansible user first (a recorded one is kept; a different one is refused), confirms the host key, installs your public key, and asks before renaming the host. It installs only `openssh-server`, `python3` and `sudo` if missing, grants passwordless sudo, checks key login, and can turn off password login. It prints the node's address and warns when it came from DHCP: keep it stable, since k3s breaks when it changes. It warns on an unsynced clock, can create a missing age key, adds a missing `.sops.yaml` rule (never changes a mismatched one), shows `git diff` and commits nothing.

## Operator tags

| Tag | Purpose | Example |
| --- | --- | --- |
| `base-os` | Packages, journald, chrony, apt policy, timezone; edge hosts turn off the resolved stub | `jarvis site --env <env> --tags base-os` |
| `node-disk` | Check a declared data disk. Never partitions, formats, wipes or mounts | `jarvis site --env <env> --tags node-disk` |
| `k3s` | k3s server, agents, node role labels, the dedicated taint | `jarvis site --env <env> --tags k3s` |
| `labels` | Only the node role labels and the taint; reads no sealed token | `jarvis site --env <env> --tags labels` |
| `flux-operator` | Flux Operator, `sops-age`, deploy key, FluxInstance and `sync.yaml` (once) | `jarvis site --env <env> --tags flux-operator` |
| `kubeconfig` | Write `~/.kube/jarvis-<jarvis_env>.yaml` (mode `0600`) for the runner; `~/.kube/config` is never touched | `jarvis site --env <env> --tags kubeconfig` |
| `time` | Chrony and timezone (after `base-os`) | `jarvis site --env <env> --tags time` |
| `apt-policy` | Apt refresh, upgrade timers, security-only unattended-upgrades | `jarvis site --env <env> --tags apt-policy` |
| `journald` | journald drop-in and restart | `jarvis site --env <env> --tags journald` |
| `resolved` | Edge hosts: stub listener off, `/etc/resolv.conf` → resolved's upstream list | `jarvis site --env <env> --tags resolved` |

The opening play checks every host, so a narrow run while a node is down needs `--limit` with the control plane or the target hosts.

**node-disk** (only on `data_disk: true` hosts): `/var/lib/jarvis` is a mount of its own filesystem whose top-level entries are empty or only the first directories of `local_path_data` and `backup_path` (and `lost+found`). An empty mount is refused once k3s has state, since data would be missing. `local_path_data` is under the mount and `backup_path` on it.

**k3s** creates `local_path_data`, `backup_path` and `backup_path/etcd-snapshots` at mode `0700` on every host, with a `RequiresMountsFor=` drop-in on its unit. It restarts k3s only when it was already running and its config, binary or unit changed. The server gets `token` and `agent-token` from the sealed file, agents the agent token only. A server whose token differs from the sealed one stops before its config changes, printing neither value.

**Edge hosts** (`base-os`): `DNSStubListener=no` and `/etc/resolv.conf` → `/run/systemd/resolve/resolv.conf`. On an existing cluster, run `jarvis site --env <env> --tags resolved --limit edge` before App DNS is applied, and again after adding a host to `edge`.

A `#` comment in a host template reaches the node, so rewording it restarts the service; explain in a Jinja `{# #}` comment.

## Patching and reboots

Nodes install updates from their distro's `-security` origin only, auto-remove nothing, and never reboot on their own. Each `jarvis site` run prints `REBOOT PENDING on <host>` for a host with `/var/run/reboot-required`. One node at a time, workers before the control plane:

1. `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`
2. `sudo reboot` on that node.
3. Once it is `Ready`, `kubectl uncordon <node>`.
4. `jarvis verify --env <env>` before the next node.

The single control plane's reboot is an API outage. Old kernels stay in `/boot` until the owner removes them.

## Recovery

- **node-disk refuses an empty data path after k3s has run:** restore the data onto the mount, or wipe the node and prepare the disk again ([deploy.md](../../docs/deploy.md#prepare-a-data-disk)).
- **The node's address changed** (etcd member URL mismatch): `systemctl stop k3s`, `/usr/local/bin/k3s server --cluster-reset` once, `systemctl start k3s`.
- **Flux rollback:** revert the commit; the root Kustomization runs as kustomize-controller itself, so it applies even when a Kustomization's identity is broken. If the FluxInstance, sources or operator are broken: `kubectl apply -f` core's `core/flux/flux-instance.yaml` and the env's `flux/sync.yaml` (and `core/flux/identities.yaml` if the accounts are gone) with `KUBECONFIG=~/.kube/jarvis-<env>.yaml`. An unusable operator Deployment: delete it and `jarvis site --env <env> --tags flux-operator`.
- **Age keys:** [secrets.md](../../docs/secrets.md#recovery).
- **Flux stuck fetching git with a working network:** check `timedatectl`. Set `chrony_nts: false` for a LAN time source without NTS.
- **Rebuild hold:** `site.yml` creates `kube-system/backup-rebuild-hold` on a new cluster whose backup host holds repositories ([backups.md](../../docs/backups.md#rebuild-a-dead-node)).
