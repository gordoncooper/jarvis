# Ansible (node bootstrap)

`site.yml` asserts facts, applies `base-os` on `all`, prepares a declared `data_disk`, installs the k3s server on `control_plane`, joins every other host as an agent, reconciles node labels, then installs kubeconfig and `flux-operator` on `control_plane`. Settings come from the env `cluster-vars.yaml` via `jarvis_cluster_vars`. A host with no `data_disk` keeps its data on root; that play matches no hosts. A single-node env has no agent hosts; that play matches none.

Run from this directory after `make deps`. Before a run, every inventory hostname resolves on the controller and on every node, `ssh_user` has key auth and passwordless sudo, and `known_hosts` is already seeded. Prepare a node (below) gets one node there. An agent fails before it starts if the control-plane name does not resolve on that agent.

## Prepare a node

`tools/prep-node` takes a freshly installed Ubuntu Server node to that state, then records the host name and `ssh_user` in the env. Run it from a checkout on the machine that drives the build. Omitted flags are prompted.

```bash
tools/prep-node --env <env> --host <hostname> --login-user <user>
```

The host name must already be an inventory key and must resolve with `getent hosts` before the node is changed. The script confirms the host key, installs your public key, and asks before changing the short hostname. A new short hostname is recorded as `<short>.<same domain as --host>` and must resolve first. If the primary address came from DHCP, it can pin that address, gateway, and DNS with one netplan file and `netplan try`. It installs only `openssh-server`, `python3`, and `sudo` if they are missing, and grants the chosen user passwordless sudo. It checks key-only login. If the age key file is missing it can create one and, if you agree, point `.sops.yaml` at the new public key and delete that env's existing `*.sops.yaml` files. It shows `git diff` and does not commit.

Next, create secrets and commit, then run `site.yml`:

```bash
make bootstrap-secrets ENV=<env>
```

## Before the first run

The age private key defaults to `~/.config/sops/age/keys.txt` (`age_key_file`). `SOPS_AGE_KEY_FILE` overrides that path. The file must be the private half of the recipient in `.sops.yaml` (`age-keygen -y` prints the public half). When `flux-system/sops-age` already exists and the key file is absent, the run leaves the secret alone.

Every env run passes the env hosts file, then the constructed role groups. The second file lives in core and is not copied into an env. `node_roles` become groups of the same name. Hosts with `data_disk` set are the `data_disk` group.

```bash
../../.venv/bin/ansible-playbook site.yml \
  -i ../../envs/<env>/inventory/hosts.yml \
  -i ../inventory/roles.yml \
  -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml
```

## Operator tags

Narrow runs use `--tags` from the playbook controller. `k3s`, `kubeconfig`, and `flux-operator` are play tags, so `--tags kubeconfig` runs only that role. Container proof tests skip `chrony-service`, `k3s`, and `kubeconfig` where noted in pytest.

| Tag | Purpose | Example |
| --- | --- | --- |
| `base-os` | Full base OS role (packages, journald, chrony, apt policy, timezone) | `../../.venv/bin/ansible-playbook site.yml --tags base-os` |
| `node-disk` | Partition, label, and mount a declared data disk. A wipe also needs `-e jarvis_wipe_confirm` set to comma-separated `<node>:<disk id>` pairs | `../../.venv/bin/ansible-playbook site.yml --tags node-disk -e "jarvis_wipe_confirm=<node>:<disk id>,<node>:<disk id>"` |
| `k3s` | k3s server, agents, and node labels | `../../.venv/bin/ansible-playbook site.yml --tags k3s` |
| `flux-operator` | One-time Flux Operator, sops-age bootstrap, FluxInstance | `../../.venv/bin/ansible-playbook site.yml --tags flux-operator` |
| `kubeconfig` | Fetch and install kubeconfig for the playbook runner | `../../.venv/bin/ansible-playbook site.yml --tags kubeconfig` |
| `time` | Chrony and node timezone (assumes base-os packages are installed; run `base-os` for packages) | `../../.venv/bin/ansible-playbook site.yml --tags time` |
| `apt-policy` | Apt list refresh timers and inert unattended-upgrades policy | `../../.venv/bin/ansible-playbook site.yml --tags apt-policy` |
| `journald` | journald drop-in and restart | `../../.venv/bin/ansible-playbook site.yml --tags journald` |

`node-disk` refuses to format the disk that holds `/`, and it refuses to format unless `wipe_confirmed` is boolean true and `jarvis_wipe_confirm` includes that host's `<inventory_hostname>:<data_disk.id>`. The value is a comma-separated list of those pairs. A `jarvis-data` filesystem on that disk is mounted and not formatted. The root check resolves `findmnt` of `/` through a btrfs `[subvol]` suffix, `readlink` of `/dev/root`, and `lsblk -nso NAME,TYPE` parents, including device-mapper and LVM. It refuses when the data disk is one of those disks, before either confirmation. A `jarvis-data` label on a device other than that disk or its partition is refused, including when both confirmations are set. A second label is refused. Not checked: mounted elsewhere, a foreign or non-empty mountpoint, partition contents, data paths outside the mount, and other disks in a multi-device btrfs filesystem. The boot drop-in is `k3s.service.d` on `control_plane` and `k3s-agent.service.d` on every other host.

`journald` restarts journald through a handler; handlers do not appear in `ansible-playbook --list-tasks`.

Pass the env inventory and `jarvis_cluster_vars` as you would for a normal run.
