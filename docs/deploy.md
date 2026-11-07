# Deploy

From a fresh copy of this repo to a running cluster, then `make verify`, `make drill`, and `make restore` to put a volume back. Commands run from the repo root unless a step says otherwise. Detail lives in [infra/ansible/README.md](../infra/ansible/README.md) and the contracts indexed in [docs/README.md](README.md).

## Prerequisites

- amd64 Ubuntu Server nodes. Exactly one host has the `control_plane` role. A host with the `gpu` role must have an NVIDIA device. The opening play of `site.yml` checks this before it changes a node.
- Internet access during the build, on the nodes and on the machine that runs the playbook. The controller needs Python 3.12 or newer (the pinned `ansible-core` requires it).
- Encrypting needs the public age recipient in `.sops.yaml`. The private key decrypts. `make bootstrap-secrets` decrypts only when `envs/<env>/ca.crt` is missing and `envs/<env>/secrets/cert-manager-ca.sops.yaml` already exists. Every `TAGS=k3s` or untagged run of `site.yml`, including `CHECK=1` and adding a node, needs the private key on the controller to decrypt `envs/<env>/secrets/k3s-token.sops.yaml`. A label-only run (`TAGS=labels`) does not. `site.yml` reads the private key whenever that file exists, including when `flux-system/sops-age` is already present, and it refuses a file that is not mode 0600 or not owned by the user running the play. It keeps only the identities whose public key is the env's `.sops.yaml` recipient, and refuses a file with none. When the secret is absent, it creates the secret with those identities. The default path is `~/.config/sops/age/keys.txt` (`age_key_file`). `SOPS_AGE_KEY_FILE` overrides it. `tools/prep-node` can create a missing key or read one that is already there. Keep the private key offline except for those reads.
- Inventory hostnames must resolve from the controller and from every node.
- A private instance repository for the env (see [New env](#new-env)). Flux reads it over `ssh://` with one read-only deploy key, or over `https://` when it needs no credential. `flux_git_repo_url` is that remote. Flux reads core from this repository over `https://` with no credential (`core_git_repo_url`).
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

Installs the pinned `sops`, `age`, `age-keygen`, `kubectl`, `kustomize`, `kubeconform`, `gitleaks`, `restic`, `rest-server`, and `kyverno` binaries into `~/.local/bin`, and creates `.venv`. Put `~/.local/bin` on `PATH`.

The Python tools need PyYAML, which `make deps` installs only in `.venv`. `make` targets already use it. When a step runs a tool directly, use `.venv/bin/python` as written, or the tool can fail with `No module named 'yaml'`.

## New env

A real env lives in its own private instance repository, made from `envs/env-00`. This repository keeps only the example. Copy that example only, never a real env and never its secrets.

1. Create an empty private repository and clone it next to this checkout. Copy `envs/env-00` into it as `envs/<env>`.
2. Point every env command at that checkout with `INSTANCE=<path>`, for example `make site ENV=<env> INSTANCE=../<instance>`. `INSTANCE` covers `site`, `verify`, `drill`, `restore`, `restore-all`, `seal`, `bootstrap-secrets`, `age-key-check`, `tools/make-plan`, `tools/prep-node`, the env schema and the env-level checks. They read `<instance>/envs/<env>` and `<instance>/.sops.yaml`. Unset, they read this checkout. `tools/make-plan` takes it only as an argument, `INSTANCE=<path>` after the target, and refuses it from the environment.
3. Replace the values in `envs/<env>/values/` and the hosts in `envs/<env>/inventory/hosts.yml`. Set `jarvis_env` to `<env>`. Every `envs/env-00/` path under `flux/` must name `<env>`. Set `flux_git_repo_url` and `flux_git_ref` to the instance repository, and `core_git_repo_url` and `core_git_ref` to this repository, and copy all four into `flux/sync.yaml`. Empty `addons` and `apps` in `values/switches.yaml`, take `app-hosting` off `optional`, delete `values/apps.yaml`, and run `INSTANCE=<path> .venv/bin/python tools/lib/render_switches.py --env <env>`: the example lists the sample add-on so CI renders it, and a new env starts core-only. Turn add-ons on later ([add-an-addon.md](add-an-addon.md#turn-it-on-in-an-env)).
4. Run `tools/prep-node` for the first host ([Prepare a node](#prepare-a-node)). It validates the env before it changes a host. It can create the age private key, or read one that is already there, and it prints the public key. It adds a missing age creation rule to the instance's `.sops.yaml`. It does not change a recipient that already exists.
5. Check that the instance's `.sops.yaml` has one age creation rule for `envs/<env>/secrets/`.
6. `make bootstrap-secrets ENV=<env> INSTANCE=<path>`
7. Make the deploy key ([Deploy key](#deploy-key)) when `flux_git_repo_url` is `ssh://`.
8. Render the ConfigMap with `INSTANCE=<path> .venv/bin/python tools/lib/render_cluster_vars_configmap.py --env <env> --output <path>/envs/<env>/flux/generated/cluster-vars-configmap.yaml`. prep-node also rewrites that file when it already exists.
9. Commit `envs/<env>/` and `.sops.yaml` in the instance repository. Do not commit the age private key. Push to the branch `flux_git_ref` names, then run `site.yml` as below.

An instance checkout with no `.git` counts as "not recorded" for prep-node and bootstrap-secrets, so neither refuses on the ground that git already records a file. Run them from a real clone when that refusal matters.

Run every env-level check against the instance with `INSTANCE=<path> bash ci/run-env-checks`. That one entry point is the list the instance CI calls too, so a new env-level check reaches an instance without a change there.

The copy already has `flux/`: `flux-instance.yaml` (it installs the controllers and is the env's only Flux version pin), `sync.yaml`, the four Kustomizations `platform-controllers` (`./core/platform`), `platform-configs` (`./envs/<env>/flux/platform-configs`), `services` (`./core/services`, plus the add-ons its `spec.components` lists) and `apps` (`./core/apps`), chained with `dependsOn`, `identities.yaml` (the service accounts those Kustomizations and the platform HelmReleases run as), and `platform-configs/kustomization.yaml`. It also has app hosting turned on (`app-hosting` in `values/switches.yaml`) and `generated/apps.yaml`, one wiring Kustomization per sample app listed there; the copy's render in step 3 removes them until it hosts an app ([add-a-hosted-app.md](add-a-hosted-app.md)). `flux/kustomization.yaml`, `flux/kustomizations/services.yaml` and `flux/platform-configs/kustomization.yaml` are that render's output, so a change goes in `values/switches.yaml`. A core-only env is a full env: with no add-on listed, every env-level check passes and `make verify` runs core's checks alone. Core is k3s, Flux, certificates, ingress, App DNS, backups, secrets and CI. Optional core components (app hosting, the off-host copy, the apps backup server) and add-ons (limited-tenant swappable services) are off until `values/switches.yaml` names them. That list is `core/platform/configs` plus `secrets/backups-restic.sops.yaml` and `secrets/cert-manager-ca.sops.yaml`, and the opt-in off-host copy. Step 6 creates those files, so the list needs no hand edit.

The FluxInstance, both GitRepositories and the root Kustomization carry `kustomize.toolkit.fluxcd.io/prune: disabled`, so a commit that deletes one from Git cannot make Flux remove its own sources; `ci/checks/flux-prune` enforces it. Removing one on purpose means deleting it from the cluster by hand. The layer Kustomizations keep prune on.

`sync.yaml` holds two Flux sources and the root Kustomization. The root `GitRepository` `flux-system` is the instance repository. It includes the core `GitRepository` `core` (this repository, public) at `core/`, so each Flux path reads env files and core files from one artifact, decrypted by the one `sops-age` key. The root Kustomization `flux-system` applies `./envs/<env>/flux` as kustomize-controller. `site.yml` applies `flux-instance.yaml` and then `sync.yaml` once on a new cluster, and stops before any change when either is missing; Flux reconciles both from Git after that.

`k3s_version`, `k3s_sha256`, and the Flux version pin are already set. Key rules are in [schema/CONTRACT.md](../schema/CONTRACT.md). A second `make bootstrap-secrets` changes nothing once the files exist.

### Deploy key

An `ssh://` root reads the instance repository with one read-only deploy key. The schema then requires `envs/<env>/secrets/flux-deploy-key.sops.yaml`.

1. In a new mode-0700 directory, make an ed25519 key with no passphrase: `ssh-keygen -t ed25519 -N '' -C flux-<env> -f <dir>/flux-deploy-key`.
2. Write a Secret manifest in that directory: `kind: Secret`, `metadata.namespace: flux-system`, and `stringData.identity` holding the private key. Seal it with the public recipient only, no age private key: `make seal ENV=<env> NAME=flux-deploy-key FILE=<dir>/secret.yaml INSTANCE=<path>`.
3. On the forge, add `<dir>/flux-deploy-key.pub` to the instance repository as a deploy key with write access off (read-only). Adding a deploy key needs repository admin.
4. Shred the directory. GitHub keeps the public half; the sealed file keeps the private half. A lost key is replaced, not recovered: make a new one and repeat.

`site.yml` creates `flux-system/flux-deploy-key` once from the sealed file, with GitHub's SSH host key as `known_hosts`, and never reconciles it from Git. It decrypts, so that run needs the age private key.

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
.venv/bin/python tools/prep-node --env <env> --host <hostname> --login-user <user>
```

Run it from a checkout on the machine that drives the build. Omitted flags are prompted. The Ansible user is settled before the host is touched; a recorded `ssh_user` is kept even when login differs. Produces a node that accepts key login and passwordless sudo, with the host name and `ssh_user` recorded in the env. What the script changes, and what it refuses to change, is in [infra/ansible/README.md](../infra/ansible/README.md).

## Bootstrap secrets, commit, and push

If the New env steps have not already created the secrets, run this from the repo root:

```bash
make bootstrap-secrets ENV=<env>
```

Commit all of `envs/<env>/` in the instance repository: `flux/`, values, inventory, prep-node's edits, secrets, and `ca.crt`. Commit its `.sops.yaml` too. Do not commit the age private key. Push to the branch `flux_git_ref` names. Flux syncs from that remote, so push before `site.yml`. Produces those files on the remote. What each secret file is, and how a second `make bootstrap-secrets` behaves, is in [envs/env-00/secrets/README.md](../envs/env-00/secrets/README.md).

A core-only env stops here. To host an app, follow [add-a-hosted-app.md](add-a-hosted-app.md). An env that will host an app that keeps data gives that app its own backup credential before the app stores anything: `make bootstrap-secrets ENV=<env> APP=app-<name>`, then the two lines in [platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md#app-credentials). An app volume without one is never backed up, and `make verify` fails it, unless the env's `backups:` section leaves that app unselected.

## Bootstrap the cluster

From the repo root. The env schema runs first and stops before Ansible on any error:

```bash
make site ENV=<env>
```

Add `INSTANCE=<path>` for an env in an instance checkout. Produces a k3s cluster, node role labels, kubeconfig `~/.kube/jarvis-<env>.yaml` on the machine that ran the playbook, the Flux Operator, and the env's FluxInstance and `sync.yaml` applied once. Tags (`TAGS=…`), the data-disk check, and recovery are in [infra/ansible/README.md](../infra/ansible/README.md).

## Verify

```bash
make verify ENV=<env>
```

Uses `~/.kube/jarvis-<env>.yaml`. Core's checks always run; an add-on's checks run only when the env lists it. Each standing check prints `PASS`, `WARN`, or `FAIL`. `WARN` is a scheduled job with no success yet whose first run is still pending or running inside its first interval (exit 0). Once that run finishes without success, or the first interval passes, it is `FAIL`. Any `FAIL` exits 1.

## Drill

```bash
make drill ENV=<env>
```

Produces a scratch-volume restore and an etcd snapshot-copy check, then removes the scratch namespace and repository. Success is `PASS drill cleanup complete` and exit 0. If cleanup cannot finish, the tool prints `make drill-cleanup ENV=<env>`. What the drill changes is in [platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md).

## Restore

One namespace:

```bash
make restore ENV=<env> APP=<namespace>
```

Optional `AS_OF=<RFC3339>` and `NODE=<node>`. A volume that was not recreated needs `AS_OF`, or the run fails. Produces a restore of that namespace's PVCs. Success is a `PASS restore <namespace>/<pvc> snapshot <id>` line, then `SUMMARY restored=... accepted=... retired=...`, and exit 0. `make restore` does not lift the rebuild hold. `backup: paused` stays until `make restore-all` lifts it, and that lift happens only when every listed repository is restored, accepted empty, or retired.

Every enrolled PVC:

```bash
make restore-all ENV=<env>
```

Optional `RETIRE`, `ACCEPT_EMPTY`, and `NODE=<node>`. Produces a restore of every enrolled PVC. The hold is lifted only when every listed repository is restored, accepted empty, or retired. Otherwise the hold stays and the PVCs stay `backup: paused`. Success is `HOLD lifted` when a hold was set and then lifted, the same `SUMMARY` line, and exit 0.

The etcd cluster reset is owner-run and is not a make target. Backups live on the same host as the data unless `backup_path` is on another disk. They cover mistakes and rollback, not disk loss. Detail is in [platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md).

## Age key

Keep two copies of the one age private key. The second copy stays offline, off the controller. Prove each copy as the owner, under their own user, on a file with mode `0600`:

```bash
make age-key-check ENV=<env> KEY=<path>
```

Prints `PASS` or `FAIL` only. `FAIL` is the same line for a key that cannot open a secret and for a file that is not mode `0600` or not owned by you. Test a mode `0600` copy owned by you. Keep the key, and where the offline copy is stored, in the owner's password manager. If a copy is lost, follow [Recovery](../envs/env-00/secrets/README.md#recovery). Custody is in [envs/env-00/secrets/README.md](../envs/env-00/secrets/README.md#age-key-custody).

## Dependency updates

Renovate opens update PRs right away and never merges them; the owner chooses when to merge, and may wait a few days after a fresh release first.
