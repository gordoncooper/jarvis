# Deploy

From a fresh copy of this repo to a running cluster, then `make verify` and `make drill`. Commands run from the repo root unless a step says otherwise. Detail lives in [infra/ansible/README.md](../infra/ansible/README.md) and the contracts indexed in [docs/README.md](README.md).

## Prerequisites

- amd64 Ubuntu Server nodes. Exactly one host has the `control_plane` role. A host with the `gpu` role must have an NVIDIA device. The opening play of `site.yml` checks this before it changes a node.
- Internet access during the build, on the nodes and on the machine that runs the playbook.
- Encrypting needs only the public age recipient in `.sops.yaml`. The private key is needed to decrypt. `make bootstrap-secrets` decrypts only when `envs/<env>/ca.crt` is missing and `envs/<env>/secrets/cert-manager-ca.sops.yaml` already exists. `site.yml` reads the private key to create `flux-system/sops-age` when that secret is absent. The default path is `~/.config/sops/age/keys.txt` (`age_key_file`). `SOPS_AGE_KEY_FILE` overrides it. Keep the private key offline except for those reads.
- Inventory hostnames must resolve from the controller and from every node.
- A Git repository Flux can fetch over https. `flux_git_repo_url` is that remote.
- The GitHub Actions repository variable `OWNER_NAME`, set to the owner's personal name. `make ci-verify` needs the same value in the environment. An unset value fails with a message that names `OWNER_NAME`.
- `k3s_sha256` for the chosen `k3s_version` (`v<major>.<minor>.<patch>+k3s<n>`). Encode `+` as `%2B` in the URL. The line named `k3s` is the digest:

```bash
curl -fsSL "https://github.com/k3s-io/k3s/releases/download/<k3s_version>/sha256sum-amd64.txt"
```

`site.yml` downloads that same `k3s` file and checks it against `k3s_sha256` ([infra/ansible/roles/k3s/tasks/main.yml](../infra/ansible/roles/k3s/tasks/main.yml)).

- Router or LAN DNS forwards the env's DNS suffix (`dns_suffix`) to an edge node. App DNS answers that zone on UDP and TCP port 53 ([platform/app-dns/CONTRACT.md](../platform/app-dns/CONTRACT.md)).

## Install tools

```bash
make deps
```

Installs the pinned `sops`, `age`, `age-keygen`, `kubectl`, `kustomize`, `kubeconform`, and `gitleaks` binaries into `~/.local/bin`, and creates `.venv`. Put `~/.local/bin` on `PATH`.

## New env

Create the env empty. Do not copy an existing env folder or its secrets, and do not retarget one.

1. Add `envs/<env>/values/` (`cluster-vars.yaml`, `hermes.yaml`, `apps.yaml`) and `envs/<env>/inventory/hosts.yml` with that env's own values and hosts.
2. Add an age creation rule in the repo root `.sops.yaml` for `envs/<env>/secrets/`.
3. `make bootstrap-secrets ENV=<env>`
4. Render the ConfigMap with `python3 tools/lib/render_cluster_vars_configmap.py --env <env> --output envs/<env>/flux/generated/cluster-vars-configmap.yaml`.
5. Add the rest of `envs/<env>/flux/`, with every git path under `envs/<env>/`:
   - `flux-instance.yaml`: sync `url`, `ref` and `path` are this env's `flux_git_repo_url`, `flux_git_ref` and `envs/<env>/flux`. It is the env's only Flux version pin. `site.yml` applies it once on a new cluster, and stops before any change when it is missing.
   - `kustomizations/`: the four Flux Kustomizations `platform-controllers` (`./platform`), `platform-configs` (`./envs/<env>/flux/platform-configs`), `services` (`./services`) and `apps` (`./apps`), chained in that order with `dependsOn`.
   - `platform-configs/kustomization.yaml`: `platform/configs` plus this env's `secrets/` files.
   - `kustomization.yaml`: the generated ConfigMap, the FluxInstance and the four Kustomizations.

Produces `envs/<env>/` with values, inventory, an age rule, bootstrap secrets, the rendered ConfigMap, and the Flux entry. Key rules are in [schema/CONTRACT.md](../schema/CONTRACT.md). Step 3 runs from the repo root. Known ordering issue: these steps run before `tools/prep-node`. If prep-node creates the age key, step 2 could not yet name that key in `.sops.yaml`, and step 3 could not encrypt with it. After node prep, repeat steps 2 and 3. A second `make bootstrap-secrets` changes nothing once the files exist.

## Prepare a data disk

Jarvis does not partition, format, or mount disks. On a host that should keep data off the root disk, prepare the disk, mount it at `/var/lib/jarvis`, then set `data_disk: true` on that host in `envs/<env>/inventory/hosts.yml`. A host without `data_disk` keeps its data on the root disk.

```bash
ls -l /dev/disk/by-id/
lsblk -o NAME,SIZE,MODEL,SERIAL,MOUNTPOINT /dev/disk/by-id/<disk-id>
sudo mkfs.ext4 -L jarvis-data /dev/disk/by-id/<disk-id>
sudo mkdir -p /var/lib/jarvis
printf '%s\n' 'LABEL=jarvis-data /var/lib/jarvis ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount /var/lib/jarvis
```

`<disk-id>` is the whole-disk name from `/dev/disk/by-id/`, not a kernel name such as `/dev/sdX` or `/dev/nvmeXnY`. Confirm the `lsblk` line is the data disk before `mkfs.ext4`. That command erases the disk.

Node setup after that is `tools/prep-node` and `site.yml` (`infra/ansible/README.md`).

## Prepare a node

```bash
tools/prep-node --env <env> --host <hostname> --login-user <user>
```

Run it from a checkout on the machine that drives the build. Omitted flags are prompted. Produces a node that accepts key login and passwordless sudo, with the host name and `ssh_user` recorded in the env. What the script changes, and what it refuses to change, is in [infra/ansible/README.md](../infra/ansible/README.md).

## Bootstrap secrets, commit, and push

If the New env steps have not already created the secrets, run this from the repo root:

```bash
make bootstrap-secrets ENV=<env>
```

Commit all of `envs/<env>/`: `flux/`, values, inventory, prep-node's edits, secrets, and `ca.crt`. Commit `.sops.yaml` too. Do not commit the age private key. Push to the branch `flux_git_ref` names. Flux syncs from that remote, so push before `site.yml`. Produces those files on the remote. What each secret file is, and how a second `make bootstrap-secrets` behaves, is in that env's `secrets/README.md`.

## Bootstrap the cluster

From `infra/ansible`:

```bash
../../.venv/bin/ansible-playbook site.yml \
  -i ../../envs/<env>/inventory/hosts.yml \
  -i ../inventory/roles.yml \
  -e jarvis_cluster_vars=../../envs/<env>/values/cluster-vars.yaml
```

Produces a k3s cluster, node role labels, kubeconfig `~/.kube/jarvis-<env>.yaml` on the machine that ran the playbook, the Flux Operator, and the env's FluxInstance applied once. Tags, the data-disk check, and recovery are in [infra/ansible/README.md](../infra/ansible/README.md).

## Verify

```bash
make verify ENV=<env>
```

Uses `~/.kube/jarvis-<env>.yaml`. Produces one `PASS` line per standing check, and exits 0. A failing check prints `FAIL` and the command exits 1.

## Drill

```bash
make drill ENV=<env>
```

Produces a scratch-volume restore and an etcd snapshot-copy check, then removes the scratch namespace and repository. Success is `PASS drill cleanup complete` and exit 0. If cleanup cannot finish, the tool prints `make drill-cleanup ENV=<env>`. What the drill changes is in [platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md).

## Backups

Backups live on the same host as the data unless `backup_path` is on another disk. They cover mistakes and rollback, not disk loss. Restore and the etcd reset are in [platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md).
