# Ansible (node bootstrap)

`site.yml` applies `base-os`, then `k3s`, then `flux-operator`, reading settings from the env `cluster-vars.yaml` via `jarvis_cluster_vars`.

Every env run passes the env hosts file, then the constructed role groups. The second file lives in core and is not copied into an env. `node_roles` become groups of the same name. Hosts with `data_disk` set are the `data_disk` group.

```bash
ansible-playbook site.yml \
  -i ../../envs/<env>/inventory/hosts.yml \
  -i ../inventory/roles.yml \
  -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml
```

## Operator tags

Narrow runs use `--tags` from the playbook controller. Guard tasks and cluster-vars asserts are tagged `always` so they still run. Container proof tests skip heavy service tags (`chrony-service`, `k3s-service`, `flux-cluster`) where noted in pytest.

| Tag | Purpose | Example |
| --- | --- | --- |
| `base-os` | Full base OS role (packages, journald, chrony, apt policy, timezone) | `ansible-playbook site.yml --tags base-os` |
| `k3s` | Full k3s role (install, service, kubeconfig) | `ansible-playbook site.yml --tags k3s` |
| `flux-operator` | One-time Flux Operator, sops-age bootstrap, FluxInstance | `ansible-playbook site.yml --tags flux-operator` |
| `kubeconfig` | Fetch and install kubeconfig for the playbook runner | `ansible-playbook site.yml --tags kubeconfig` |
| `k3s-upgrade` | Pinned k3s binary and unit after a version pin change | `ansible-playbook site.yml --tags k3s-upgrade` |
| `k3s-config` | `/etc/rancher/k3s/config.yaml` and k3s restart | `ansible-playbook site.yml --tags k3s-config` |
| `time` | Chrony and node timezone (assumes base-os packages are installed; run `base-os` for packages) | `ansible-playbook site.yml --tags time` |
| `apt-policy` | Apt list refresh timers and inert unattended-upgrades policy | `ansible-playbook site.yml --tags apt-policy` |
| `journald` | journald drop-in and restart | `ansible-playbook site.yml --tags journald` |

`k3s-config`, `k3s-upgrade`, and `journald` restart their services through handlers; handlers do not appear in `ansible-playbook --list-tasks`.

Pass the env inventory and `jarvis_cluster_vars` as you would for a normal run.
