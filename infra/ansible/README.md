# Ansible (node bootstrap)

`site.yml` asserts facts, applies `base-os` on `all`, prepares a declared `data_disk`, installs the k3s server on `control_plane`, joins every other host as an agent, reconciles node labels, then installs kubeconfig and `flux-operator` on `control_plane`. Settings come from the env `cluster-vars.yaml` via `jarvis_cluster_vars`. A host with no `data_disk` keeps its data on root; that play matches no hosts. A single-node env has no agent hosts; that play matches none.

Every env run passes the env hosts file, then the constructed role groups. The second file lives in core and is not copied into an env. `node_roles` become groups of the same name. Hosts with `data_disk` set are the `data_disk` group.

```bash
ansible-playbook site.yml \
  -i ../../envs/<env>/inventory/hosts.yml \
  -i ../inventory/roles.yml \
  -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml
```

## Operator tags

Narrow runs use `--tags` from the playbook controller. `k3s`, `kubeconfig`, and `flux-operator` are play tags, so `--tags kubeconfig` runs only that role. Container proof tests skip `chrony-service`, `k3s`, and `kubeconfig` where noted in pytest.

| Tag | Purpose | Example |
| --- | --- | --- |
| `base-os` | Full base OS role (packages, journald, chrony, apt policy, timezone) | `ansible-playbook site.yml --tags base-os` |
| `node-disk` | Partition, label, and mount a declared data disk. A wipe also needs `-e jarvis_wipe_confirm=<node>:<disk id>` | `ansible-playbook site.yml --tags node-disk -e jarvis_wipe_confirm=<node>:<disk id>` |
| `k3s` | k3s server, agents, and node labels | `ansible-playbook site.yml --tags k3s` |
| `flux-operator` | One-time Flux Operator, sops-age bootstrap, FluxInstance | `ansible-playbook site.yml --tags flux-operator` |
| `kubeconfig` | Fetch and install kubeconfig for the playbook runner | `ansible-playbook site.yml --tags kubeconfig` |
| `time` | Chrony and node timezone (assumes base-os packages are installed; run `base-os` for packages) | `ansible-playbook site.yml --tags time` |
| `apt-policy` | Apt list refresh timers and inert unattended-upgrades policy | `ansible-playbook site.yml --tags apt-policy` |
| `journald` | journald drop-in and restart | `ansible-playbook site.yml --tags journald` |

`node-disk` refuses to format the disk that holds `/`, and it refuses to format unless `wipe_confirmed` is boolean true and `jarvis_wipe_confirm` is `<inventory_hostname>:<data_disk.id>`. A `jarvis-data` filesystem on that disk is mounted and not formatted. The root check compares `findmnt` of `/` to the disk and its partitions. It does not follow LVM or device-mapper, so those layouts rely on the two confirmations. Not checked: mounted elsewhere, a second label, a foreign or non-empty mountpoint, partition contents, and data paths outside the mount. The boot drop-in is `k3s.service.d` on `control_plane` and `k3s-agent.service.d` on every other host.

`journald` restarts journald through a handler; handlers do not appear in `ansible-playbook --list-tasks`.

Pass the env inventory and `jarvis_cluster_vars` as you would for a normal run.
