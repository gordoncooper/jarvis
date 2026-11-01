# Ansible (node bootstrap)

`site.yml` asserts facts before any change (one control-plane host, amd64, required cluster-vars), refuses a foreign or unmarked k3s, and checks a declared data disk. It then applies `base-os` on `all`, installs the k3s server on `control_plane`, joins every other host as an agent, reconciles node labels, then installs kubeconfig and `flux-operator` on `control_plane`. Settings come from the env `cluster-vars.yaml` via `jarvis_cluster_vars`. A host with no `data_disk` skips the disk check and keeps its data on root. A single-node env has no agent hosts; that play matches none.

Run from this directory after `make deps`. Before a run, every inventory hostname resolves on the controller and on every node, `ssh_user` has key auth and passwordless sudo, and `known_hosts` is already seeded. Prepare a node (below) gets one node there. An agent fails before it starts if the control-plane name does not resolve on that agent.

## Prepare a node

`tools/prep-node` takes a freshly installed Ubuntu Server node to that state, then records the host name and `ssh_user` in the env. Run it from a checkout on the machine that drives the build. Omitted flags are prompted.

```bash
tools/prep-node --env <env> --host <hostname> --login-user <user>
```

The host name must already be an inventory key and must resolve with `getent hosts` before the node is changed. The script confirms the host key, installs your public key, and asks before changing the short hostname. A new short hostname is recorded as `<short>.<same domain as --host>` and must resolve first. It prints the node's primary address and whether that address came from DHCP. On DHCP it warns that the address must stay stable (for example, a router DHCP reservation), because k3s breaks when the address changes. It does not write or rename netplan files, and it does not write the address into the repo. It installs only `openssh-server`, `python3`, and `sudo` if they are missing, and grants the chosen user passwordless sudo. It checks that key login works, then can turn off SSH password login. If the clock is not synced, it warns and asks before recording the env. If the age key file is missing it can create one and, if you agree, point `.sops.yaml` at the new public key and delete that env's existing `*.sops.yaml` files. It shows `git diff` and does not commit.

When you are not using the default age key file, set the same `SOPS_AGE_KEY_FILE` for `prep-node`, `bootstrap-secrets`, and `site.yml`.

Next, create secrets and commit, then run `site.yml`:

```bash
make bootstrap-secrets ENV=<env>
```

## Before the first run

The age private key defaults to `~/.config/sops/age/keys.txt` (`age_key_file`). `SOPS_AGE_KEY_FILE` overrides that path. The file must be the private half of the recipient in `.sops.yaml` (`age-keygen -y` prints the public half). When `flux-system/sops-age` already exists and the key file is absent, the run leaves the secret alone.

Every env run passes the env hosts file, then the constructed role groups. The second inventory lives in core and is not copied into an env. `node_roles` become groups of the same name. Hosts with `data_disk: true` are the `data_disk` group. That group is always present, including when no host sets `data_disk`.

```bash
../../.venv/bin/ansible-playbook site.yml \
  -i ../../envs/<env>/inventory/hosts.yml \
  -i ../inventory/roles.yml \
  -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml
```

## Operator tags

Narrow runs use `--tags` from the playbook controller. `k3s`, `kubeconfig`, and `flux-operator` are play tags. The opening play is tagged `always`, so its asserts run first. The opening play checks every host, so for a narrow run while a node is down, add `--limit` with the control plane or the target hosts. `--tags kubeconfig` then runs only the kubeconfig role. Proof runs are localhost plays with fake binaries.

| Tag | Purpose | Example |
| --- | --- | --- |
| `base-os` | Full base OS role (packages, journald, chrony, apt policy, timezone; edge hosts also turn off the resolved stub) | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags base-os` |
| `node-disk` | Check a declared data disk. Does not partition, format, wipe, or mount. Prepare the disk with `docs/deploy.md`, then set `data_disk: true` | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags node-disk` |
| `k3s` | k3s server, agents, and node labels | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags k3s` |
| `flux-operator` | One-time Flux Operator, sops-age bootstrap, FluxInstance | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags flux-operator` |
| `kubeconfig` | Write `~/.kube/jarvis-<jarvis_env>.yaml` for the playbook runner | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags kubeconfig` |
| `time` | Chrony and node timezone (assumes base-os packages are installed; run `base-os` for packages) | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags time` |
| `apt-policy` | Apt list refresh timers and inert unattended-upgrades policy | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags apt-policy` |
| `journald` | journald drop-in and restart | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags journald` |
| `resolved` | Edge hosts: stub listener off, `/etc/resolv.conf` → resolved's upstream list | `../../.venv/bin/ansible-playbook site.yml -i ../../envs/<env>/inventory/hosts.yml -i ../inventory/roles.yml -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml --tags resolved` |

The kubeconfig role reads `k3s_kubeconfig_path` on the control-plane node, rewrites the server to that node's inventory hostname, and writes `~/.kube/jarvis-<jarvis_env>.yaml` (mode `0600`) on the playbook runner. `~/.kube` is mode `0700`. The role does not write `~/.kube/config` and does not keep a backup. kubectl on that machine uses `KUBECONFIG=~/.kube/jarvis-<env>.yaml`. A run whose server line does not change fails.

`node-disk` runs in the opening play, before any change, and only on hosts with `data_disk: true`. It does not partition, format, wipe, label, or mount. It checks `/var/lib/jarvis`: the path exists, it is a mount of its own filesystem (not part of `/`), and its top-level entries are empty or only Jarvis data. Jarvis data is the first directory of `local_path_data` and of `backup_path`, plus `lost+found`. Anything else is refused. An empty path is also refused when `k3s_data_dir` already holds k3s server or agent state, because existing data is missing or hidden under the mount. The env marker alone does not mean the node holds data. `local_path_data` must be a subdirectory of that mount, and `backup_path` must be on it. Prepare the disk with the commands in `docs/deploy.md`, then set `data_disk: true`. k3s is the only role that creates `local_path_data`, `backup_path`, and `backup_path/etcd-snapshots`, at mode `0700` on every host. The same role installs a `RequiresMountsFor=` drop-in for `local_path_data` and `backup_path` on the unit that host runs (`k3s.service.d` on the control plane, `k3s-agent.service.d` on every other host), including hosts with no data disk. A drop-in change reloads systemd and does not restart k3s. On a server/agent switch the role disables and stops the opposite unit before it starts its own. A missing opposite unit is fine. Any other error fails.

`journald`, `resolved`, and `chrony` restart through handlers; handlers do not appear in `ansible-playbook --list-tasks`. A change to `chrony.conf` restarts chrony. Enabling the service does not.

On hosts in the `edge` group, `base-os` turns off systemd-resolved's stub listener (`DNSStubListener=no`) and points `/etc/resolv.conf` at `/run/systemd/resolve/resolv.conf`. Host queries then use the upstream resolvers resolved already has, including after a DHCP or netplan DNS change. `getent hosts` still resolves inventory names. A host that later joins `edge` picks this up on the next `site.yml`. Leaving `edge` does not turn the stub back on. On an existing cluster, run `site.yml --tags resolved` on the edge hosts before App DNS is applied, and again after a `--tags k3s` run that adds a host to edge.

Pass the env inventory and `jarvis_cluster_vars` as you would for a normal run.

## Rebuild hold

See `platform/backups/CONTRACT.md`. `site.yml` may create ConfigMap `kube-system/backup-rebuild-hold` before the first FluxInstance.

## Recovery

If node-disk refuses a node because the data path is empty and k3s has already run, restore the data onto that mount, or wipe the node and prepare the disk again (`docs/deploy.md`).

The node address changed and k3s fails with an etcd member URL mismatch. Stop k3s (`systemctl stop k3s`), run `/usr/local/bin/k3s server --cluster-reset` once, then start it (`systemctl start k3s`). A later `site.yml` run does not clear this. A stable address prevents it. prep-node prints the address and, when it came from DHCP, warns that it must stay stable (for example, a router DHCP reservation). It does not pin the address.

Flux rollback: while the controllers are up, revert the commit. When the FluxInstance or the operator is the broken part, from the controller's reverted checkout run `kubectl apply -f envs/<env>/flux/flux-instance.yaml` with `KUBECONFIG=~/.kube/jarvis-<env>.yaml`. The file is plain YAML with no substitution. If the operator Deployment (`flux-system/flux-operator`) is unusable, delete it and re-run `--tags flux-operator`.

Age key: back up the key file (`age_key_file`, default `~/.config/sops/age/keys.txt`). If the file is lost while `flux-system/sops-age` still exists, copy `age.agekey` from that secret back to the key path at mode `0600` before deleting anything. If both are lost, the old secret values are gone. Accept the prep-node swap prompt (it deletes `envs/<env>/secrets/*.sops.yaml`), run `make bootstrap-secrets`, commit and push, then re-run `site.yml`. If a differing `flux-system/sops-age` still exists, see the case above and don't delete it.

Clock: if Flux is stuck fetching git while the network is fine, check `timedatectl`. chrony uses NTS only, against cluster-vars' `ntp_pools`.
