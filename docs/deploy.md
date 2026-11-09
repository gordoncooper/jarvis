# Deploy

From a fresh copy of this repo to a running cluster, then `jarvis verify`, `jarvis drill`, and `jarvis restore` to put a volume back. Commands run from the instance repository with the jarvis CLI unless a step says otherwise. Detail lives in [infra/ansible/README.md](../infra/ansible/README.md) and the contracts indexed in [docs/README.md](README.md).

## Prerequisites

- amd64 Ubuntu Server nodes. Exactly one host has the `control_plane` role. A host with the `gpu` role must have an NVIDIA device. The opening play of `site.yml` checks this before it changes a node.
- Internet access during the build, on the nodes and on the machine that runs the playbook. The controller (Linux or macOS, amd64 or arm64) needs [mise](https://mise.jdx.dev/getting-started.html); the jarvis CLI installs everything else, Python 3.12 for Ansible included ([Install tools](#install-tools)).
- Encrypting needs the public age recipient in `.sops.yaml`. The private key decrypts. `jarvis secrets` decrypts only when `envs/<env>/ca.crt` is missing and `envs/<env>/secrets/cert-manager-ca.sops.yaml` already exists. Every `--tags k3s` or untagged `jarvis site`, including `--check` and adding a node, needs the private key on the controller to decrypt `envs/<env>/secrets/k3s-token.sops.yaml`. A label-only run (`--tags labels`) does not. `site.yml` reads the private key whenever that file exists, including when `flux-system/sops-age` is already present, and it refuses a file that is not mode 0600 or not owned by the user running the play. It keeps only the identities whose public key is the env's `.sops.yaml` recipient, and refuses a file with none. When the secret is absent, it creates the secret with those identities. The default path is `~/.config/sops/age/keys.txt` (`age_key_file`). `SOPS_AGE_KEY_FILE` overrides it. `jarvis node prep` can create a missing key or read one that is already there. Keep the private key offline except for those reads.
- Inventory hostnames must resolve from the controller and from every node.
- A private instance repository for the env, empty to start (see [New env](#new-env)). Flux reads it over `ssh://` with one read-only deploy key, or over `https://` when it needs no credential. `flux_git_repo_url` is that remote. Flux reads core from this repository over `https://` with no credential (`core_git_repo_url`).
- Nothing for k3s: an env runs core's release (`k3s_version` and `k3s_sha256` in `infra/ansible/group_vars/all.yml`). To run another, set both in `values/cluster-vars.yaml`; `k3s_sha256` is the line named `k3s` for that `k3s_version` (`v<major>.<minor>.<patch>+k3s<n>`; encode `+` as `%2B` in the URL):

```bash
curl -fsSL "https://github.com/k3s-io/k3s/releases/download/<k3s_version>/sha256sum-amd64.txt"
```

`site.yml` downloads that same `k3s` file and checks it against `k3s_sha256` ([infra/ansible/roles/k3s/tasks/main.yml](../infra/ansible/roles/k3s/tasks/main.yml)).

- Router or LAN DNS forwards the env's DNS suffix (`dns_suffix`) to an edge node. App DNS answers that zone on UDP and TCP port 53 ([core/feature/app-dns/CONTRACT.md](../core/feature/app-dns/CONTRACT.md)).

## Install tools

Install mise once, then the jarvis CLI from a core release:

```bash
mise use -g uv
uv tool install git+https://github.com/<owner>/jarvis@<core tag>
jarvis --help
```

Run `jarvis` from inside the instance repository (the folder that holds `envs/`). Each command runs at the core release its env pins (`core_git_ref`): the first time a release is needed, jarvis clones it into `~/.cache/jarvis/core/<tag>` and installs that release's pinned CLIs and Python packages from its own `mise.lock` and `uv.lock` (its `deps` task: `sops`, `age`, `kubectl`, `flux` at the cluster's Flux version, `kustomize`, `kubeconform`, `kyverno`, `gitleaks`, `restic`, `rest-server`, Ansible, and Python 3.12 for it). So the CLI, the checks and the cluster always run the same core, and the installed `jarvis` only needs updating when you want its own fixes. `jarvis doctor` checks the controller; `--env <env>` picks the env when the repository holds more than one.

To work on core itself, run `make deps` in a core checkout; it installs the same toolchain and `.venv/bin/jarvis`. `jarvis --core <path> <command>` runs that checkout for an env instead of its pin.

## New env

A real env lives in its own private instance repository, which `jarvis init` writes. Create an empty private repository on your forge first; then, from the folder that should hold its clone:

```bash
jarvis init my-clusters --env home --domain home.lan \
  --repo-url ssh://git@github.com/<you>/my-clusters --node node1.home.lan
cd my-clusters
```

`--node` names each host, as `<host>` for a single node (every role but `gpu`) or `<host>:<role>,<role>` for more; repeat it per node. `--feature app-dns` (and `app-hosting`, `offsite-backup-copy`, `apps-backup-server`) turns a feature on, `--apps-repo-url` names the hosted apps repository, `--ssh-user` the user Ansible logs in as (default: you), `--time-zone` the nodes' zone. Anything left out is asked for at a terminal. `jarvis init --help` lists every flag.

`jarvis init`:

1. makes the folder a git repository on `--branch` (default `main`);
2. creates the working age key at `age_key_file` if there is none (mode `0600`) and prints its public key;
3. makes the **recovery key** and prints its private half once: store it offline before you go on ([secrets.md](secrets.md#age-key-custody));
4. writes `envs/<env>/values/` and `inventory/hosts.yml`, the env's creation rule in `.sops.yaml` (both keys), a README, and unless `--no-ci` the Renovate config and the CI caller (below);
5. mints the env's secrets (`jarvis secrets`) and `ca.crt`, and for an `ssh://` `--repo-url` seals a read-only deploy key and prints its public half;
6. renders `flux/` and installs a pre-commit hook that runs `jarvis render --check` and `jarvis validate` for each env a commit touches.

The env pins the core release `jarvis init` ran from. It passes `jarvis validate` as soon as init returns. Then:

1. Add the printed deploy key to the repository on the forge, read-only (it needs repository admin).
2. For the GitHub App that pushes Renovate's renders, see [Renovate and CI](#renovate-and-ci).
3. `jarvis node prep <host>` for each host ([Prepare a node](#prepare-a-node)).
4. `git add -A && git commit -m "home: first cluster" && git remote add origin <url> && git push -u origin main`.
5. `jarvis up`: doctor, validate, render check, site, a wait for Flux, then verify.

`jarvis env add <name> --domain <suffix> --node <host>` adds another cluster to the same repository, with its own deploy key, CA and secrets, encrypted to the same two age recipients.

### Renovate and CI

`renovate.json` extends core's instance preset at the env's core tag. Renovate then opens one PR per pin an env holds: the core release tag, k3s, and each hosted app's tag under `versions:`. Nothing merges on its own. `.github/workflows/ci.yml` calls core's instance workflow, which runs `jarvis validate` for each env with the core its `core_git_ref` pins. A pin bump changes rendered files too (`flux/sync.yaml`, the app wiring), so on a Renovate PR the workflow renders each env and pushes the result. For that push, create a GitHub App once: no webhook, repository permission Contents read and write, installed on the instance repository only. Set the repository variable `RENDER_CLIENT_ID` to its client ID and the secret `RENDER_PRIVATE_KEY` to a private key it generates. Without the App, a Renovate PR's check names the stale files and the render command to run.

An instance checkout with no `.git` counts as "not recorded" for `jarvis node prep` and `jarvis secrets`, so neither refuses on the ground that git already records a file. Run them from a real clone when that refusal matters.

Run every env-level check with `jarvis validate`. The instance CI runs the same command, so a new env-level check reaches an instance without a change there.

The env's `flux/` is render output: `sync.yaml` (whose root Kustomization sets `postBuild.substitute: {jarvis_env: <env>}`), `kustomization.yaml`, the `services` Kustomization (`./addons`, plus the add-ons its `spec.components` lists) and `platform-configs/kustomization.yaml`. The rest of the Flux wiring is core's, at the core tag the env pins: `flux/` in this repository (`core/flux` in the root artifact) holds the FluxInstance (Flux's only version pin), the service accounts the Kustomizations and platform HelmReleases run as, and the layers `platform-controllers` (`./core/platform`) and `platform-configs` (`./envs/${jarvis_env}/flux/platform-configs`), chained with `dependsOn` to `services`. The env's `flux/kustomization.yaml` lists it as `../../../core/flux`.  An env that hosts apps also has `generated/apps.yaml`, one wiring Kustomization per app ([add-a-hosted-app.md](add-a-hosted-app.md)). Everything under `flux/` is that render's output, and a render replaces it whole, so a change goes in `values/`. A core-only env is a full env: with no add-on listed, every env-level check passes and `jarvis verify` runs core's checks alone. Core is k3s, Flux, certificates, ingress, App DNS, backups, secrets and CI. Features (app hosting, the off-host copy, the apps backup server) and add-ons (limited-tenant swappable services) are off until `values/switches.yaml` names them. That list is `core/platform/configs` plus `secrets/backups-restic.sops.yaml` and `secrets/cert-manager-ca.sops.yaml`, and the opt-in off-host copy. `jarvis init` creates those files, so the list needs no hand edit.

The FluxInstance, both GitRepositories and the root Kustomization carry `kustomize.toolkit.fluxcd.io/prune: disabled`, so a commit that deletes one from Git cannot make Flux remove its own sources; `jarvis validate` (flux-prune) enforces it. Removing one on purpose means deleting it from the cluster by hand. The layer Kustomizations keep prune on.

`sync.yaml` holds two Flux sources and the root Kustomization. The root `GitRepository` `flux-system` is the instance repository. It includes the core `GitRepository` `core` (this repository, public) at `core/`, so each Flux path reads env files and core files from one artifact, decrypted by the one `sops-age` key. The root Kustomization `flux-system` applies `./envs/<env>/flux` as kustomize-controller. `site.yml` applies `flux-instance.yaml` and then `sync.yaml` once on a new cluster, and stops before any change when either is missing; Flux reconciles both from Git after that.

The k3s release and the Flux version come with the core tag. Key rules are in [schema/CONTRACT.md](../schema/CONTRACT.md). A second `jarvis secrets` changes nothing once the files exist.

### Deploy key

An `ssh://` root reads the instance repository with one read-only deploy key. The schema then requires `envs/<env>/secrets/flux-deploy-key.sops.yaml`. `jarvis init` and `jarvis env add` make and seal it, and print the public half to add on the forge. To replace a lost or leaked one by hand:

1. In a new mode-0700 directory, make an ed25519 key with no passphrase: `ssh-keygen -t ed25519 -N '' -C flux-<env> -f <dir>/flux-deploy-key`.
2. Write a Secret manifest in that directory: `kind: Secret`, `metadata.namespace: flux-system`, and `stringData.identity` holding the private key. Seal it with the public recipient only, no age private key: `jarvis encrypt --env <env> --name flux-deploy-key <dir>/secret.yaml`.
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

Node setup after that is `jarvis node prep` and `jarvis site` (`infra/ansible/README.md`).

## Prepare a node

```bash
jarvis node prep <hostname> --env <env> --login-user <user>
```

Run it from the instance repository on the machine that drives the build. Omitted flags are prompted. The Ansible user is settled before the host is touched; a recorded `ssh_user` is kept even when login differs. Produces a node that accepts key login and passwordless sudo, with the host name and `ssh_user` recorded in the env. What the script changes, and what it refuses to change, is in [infra/ansible/README.md](../infra/ansible/README.md).

## Bootstrap secrets, commit, and push

If the New env steps have not already created the secrets, run:

```bash
jarvis secrets --env <env>
```

Commit all of `envs/<env>/` in the instance repository: `flux/`, values, inventory, `jarvis node prep`'s edits, secrets, and `ca.crt`. Commit its `.sops.yaml` too. Do not commit the age private key. Push to the branch `flux_git_ref` names. Flux syncs from that remote, so push before `jarvis site`. Produces those files on the remote. What each secret file is, and how a second `jarvis secrets` behaves, is in [envs/env-00/secrets/README.md](secrets.md).

A core-only env stops here. To host an app, follow [add-a-hosted-app.md](add-a-hosted-app.md). An env that will host an app that keeps data gives that app its own backup credential before the app stores anything: `jarvis secrets --env <env> --app app-<name>`, then the two lines in [core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md#app-credentials). An app volume without one is never backed up, and `jarvis verify` fails it, unless the env's `backups:` section leaves that app unselected.

## Bootstrap the cluster

The env schema runs first and refuses before Ansible on any error:

```bash
jarvis site --env <env>
```

Produces a k3s cluster, node role labels, kubeconfig `~/.kube/jarvis-<env>.yaml` on the machine that ran the playbook, the Flux Operator, and the env's FluxInstance and `sync.yaml` applied once. Tags (`--tags …`), the data-disk check, and recovery are in [infra/ansible/README.md](../infra/ansible/README.md).

## Verify

```bash
jarvis verify --env <env>
```

Uses `~/.kube/jarvis-<env>.yaml`. Core's checks always run; an add-on's checks run only when the env lists it. Each standing check prints `PASS`, `WARN`, or `FAIL`. `WARN` is a scheduled job with no success yet whose first run is still pending or running inside its first interval (exit 0). Once that run finishes without success, or the first interval passes, it is `FAIL`. Any `FAIL` exits 1.

## Drill

```bash
jarvis drill --env <env>
```

Produces a scratch-volume restore and an etcd snapshot-copy check, then removes the scratch namespace and repository. Success is `PASS drill cleanup complete` and exit 0. If cleanup cannot finish, the tool prints `jarvis drill --cleanup-only --env <env>`. What the drill changes is in [core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md).

## Restore

One namespace:

```bash
jarvis restore --env <env> --app <namespace>
```

Optional `--as-of <RFC3339>` and `--node <node>`. A volume that was not recreated needs `--as-of`, or the run fails. Produces a restore of that namespace's PVCs. Success is a `PASS restore <namespace>/<pvc> snapshot <id>` line, then `SUMMARY restored=... accepted=... retired=...`, and exit 0. `jarvis restore` does not lift the rebuild hold. `backup: paused` stays until `jarvis restore --all` lifts it, and that lift happens only when every listed repository is restored, accepted empty, or retired.

Every enrolled PVC:

```bash
jarvis restore --all --env <env>
```

Optional `--retire`, `--accept-empty`, and `--node <node>` (`jarvis restore --help`). Produces a restore of every enrolled PVC. The hold is lifted only when every listed repository is restored, accepted empty, or retired. Otherwise the hold stays and the PVCs stay `backup: paused`. Success is `HOLD lifted` when a hold was set and then lifted, the same `SUMMARY` line, and exit 0.

The etcd cluster reset is owner-run and is not a jarvis command. Backups live on the same host as the data unless `backup_path` is on another disk. They cover mistakes and rollback, not disk loss. Detail is in [core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md).

## Age key

The env's secrets open with either of two age keys: the working key on the controller, and the offline recovery key `jarvis init` showed once. Prove each copy as the owner, under their own user, on a file with mode `0600`:

```bash
jarvis age-key-check --env <env> --key-file <path>
```

Prints `PASS` or `FAIL` only. `FAIL` is the same line for a key that cannot open a secret and for a file that is not mode `0600` or not owned by you. Test a mode `0600` copy owned by you. Keep a copy of the working key in the owner's password manager, and the recovery key offline. If a copy is lost, follow [Recovery](secrets.md#recovery). Custody is in [envs/env-00/secrets/README.md](secrets.md#age-key-custody).

## Dependency updates

Renovate opens update PRs right away and never merges them; the owner chooses when to merge, and may wait a few days after a fresh release first.
