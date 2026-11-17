# Deploy

The [Quickstart](quickstart.md) is the same path in seven steps; this page is the detail. Commands run from the instance repository with the jarvis CLI. Contracts are indexed in [docs/README.md](README.md).

## Prerequisites

- amd64 Ubuntu Server nodes, exactly one with the `control_plane` role; a `gpu` host has an NVIDIA device. `site.yml`'s opening play checks this before it changes a node.
- Internet on the nodes and the controller (Linux or macOS, amd64 or arm64) with [mise](https://mise.jdx.dev/getting-started.html).
- Inventory hostnames resolve from the controller and every node.
- An empty private instance repository. Flux reads it over `ssh://` with a read-only deploy key, or `https://` with no credential (`flux_git_repo_url`), and reads core over `https://` (`core_git_repo_url`).
- The age private key on the controller (`age_key_file`, default `~/.config/sops/age/keys.txt`; `SOPS_AGE_KEY_FILE` overrides) for any `jarvis site` that touches k3s, mode `0600` and yours. A label-only run (`--tags labels`) needs none. Keep it offline otherwise.
- k3s: core's release (`infra/ansible/group_vars/all.yml`) unless the env sets both `k3s_version` and `k3s_sha256` (the `k3s` line of `https://github.com/k3s-io/k3s/releases/download/<k3s_version>/sha256sum-amd64.txt`, `+` as `%2B`).
- With App DNS, the router or LAN DNS forwards `dns_suffix` to an edge node ([app-dns](../core/feature/app-dns/CONTRACT.md)).

## Install tools

```bash
mise use -g uv
uv tool install git+https://github.com/<owner>/jarvis@<core tag>
jarvis --help
```

Each command runs at the core release its env pins (`core_git_ref`): jarvis clones that release into `~/.cache/jarvis/core/<tag>` once and installs its pinned CLIs and Python packages. `jarvis doctor` checks the controller; `--env <env>` picks an env when the repository holds several. In a core checkout, `make deps` installs the same toolchain, and `jarvis --core <path> <command>` runs that checkout instead of the pin.

## New env

Create an empty private repository on your forge, then:

```bash
jarvis init my-clusters --env home --domain home.lan \
  --repo-url ssh://git@github.com/<you>/my-clusters --node node1.home.lan
cd my-clusters
```

`--node <host>` gives one node every role but `gpu`; `--node <host>:<role>,<role>` per node for more. `--feature <name>` turns a feature on, `--apps-repo-url` names the apps repository, `--with-examples` turns on the example apps, `--with-agents` adds the agent kit ([agents/README.md](../agents/README.md)), `--ssh-user` and `--time-zone` set those values. Anything left out is asked for. `jarvis init --help` lists every flag.

`jarvis init` makes the git repository, creates the working age key if there is none, prints the **recovery key**'s private half once (store it offline now: [secrets.md](secrets.md#age-key-custody)), writes the env's values, inventory, `.sops.yaml` rule, README, devcontainer, Renovate config and CI caller (unless `--no-ci`), mints the secrets and `ca.crt`, seals a deploy key for an `ssh://` URL and prints its public half, renders `flux/`, and installs a pre-commit hook running `jarvis render --check` and `jarvis validate`. The env passes `jarvis validate` at once. Then:

1. Add the printed deploy key to the repository, read-only.
2. Set up the render App ([Renovate and CI](#renovate-and-ci)).
3. `jarvis node prep <host>` for each host.
4. `git add -A && git commit -m "home: first cluster" && git remote add origin <url> && git push -u origin main`.
5. `jarvis up`: doctor, validate, render check, secrets, site, a wait for Flux, then verify.

`jarvis env add <name> --domain <suffix> --node <host>` adds another cluster to the same repository with its own deploy key, CA and secrets.

Everything under `flux/` is render output, replaced whole by `jarvis render`; a change goes in `values/`. `jarvis validate` runs every env-level check, and the instance CI runs the same command.

### Renovate and CI

`renovate.json` extends core's instance preset at the env's core tag: one PR per pin (the core tag, k3s, each app's tag under `versions:`), never merged on its own. `.github/workflows/ci.yml` calls core's instance workflow, which validates each env with its pinned core and, on a Renovate PR, renders and pushes the result. For that push, create a GitHub App once: no webhook, Contents read and write, installed on the instance repository only. Set repository variable `RENDER_CLIENT_ID` to its client ID and secret `RENDER_PRIVATE_KEY` to a private key it generates. Without it, the check names the stale files and the render command.

### Deploy key

An `ssh://` root reads with one read-only deploy key, sealed in `secrets/flux-deploy-key.sops.yaml`. `site.yml` creates `flux-system/flux-deploy-key` from it once. A lost or leaked key: `jarvis secrets --rotate deploy-key` ([secrets.md](secrets.md#changing-recipients-and-rotating-keys)).

## Prepare a data disk

Jarvis never partitions, formats or mounts disks. To keep a host's data off its root disk, mount a filesystem at `/var/lib/jarvis` and set `data_disk: true` on that host:

```bash
ls -l /dev/disk/by-id/
lsblk -o NAME,SIZE,MODEL,SERIAL,MOUNTPOINT /dev/disk/by-id/<disk-id>
sudo mkfs.ext4 -L jarvis-data /dev/disk/by-id/<disk-id>
sudo mkdir -p /var/lib/jarvis
printf '%s\n' 'LABEL=jarvis-data /var/lib/jarvis ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount /var/lib/jarvis
```

`<disk-id>` is the whole-disk name under `/dev/disk/by-id/`. `mkfs.ext4` erases it: check the `lsblk` line first.

## Prepare a node

```bash
jarvis node prep <hostname> --env <env> --login-user <user>
```

It leaves the host with key login and passwordless sudo, and records the host name and `ssh_user` in the env; a recorded `ssh_user` is kept. What it changes and refuses: [infra/ansible/README.md](../infra/ansible/README.md).

## Secrets, commit, push

`jarvis secrets --env <env>` mints what is missing and changes nothing on a second run. Commit all of `envs/<env>/` and `.sops.yaml`, never the age private key, and push to the branch `flux_git_ref` names before `jarvis site`. What each file is: [secrets.md](secrets.md). A hosted app that keeps data needs its own backup credential first ([backups.md](backups.md#app-credentials)).

## Bootstrap the cluster

```bash
jarvis site --env <env>
```

It validates the env first. It installs k3s and node labels, writes `~/.kube/jarvis-<env>.yaml`, installs the Flux Operator, and applies the FluxInstance and `sync.yaml` once. Tags and recovery: [infra/ansible/README.md](../infra/ansible/README.md).

## Verify

```bash
jarvis verify --env <env> [--smoke]
```

Each standing check prints `PASS`, `WARN` or `FAIL`; any `FAIL` exits 1. `WARN` is a scheduled job whose first run is still due. `--smoke` adds one short-lived probe per enabled capability in namespace `platform-smoke`: a Certificate from the internal CA, a new App DNS name (asked on port 53 from the controller), a hosted app's pod admitted and the same pod with an ungranted risk refused, and each add-on's own probe.

## Drill and restore

`jarvis drill --env <env>`, `jarvis restore --env <env> --app <namespace>` and `jarvis restore --all --env <env>`: [backups.md](backups.md).

## Age key

```bash
jarvis age-key-check --env <env> --key-file <path>
```

Prints `PASS` or `FAIL` only, for the working key and the offline recovery key alike. A file that is not mode `0600` and yours fails too. Lost a copy: [secrets.md](secrets.md#recovery).

## Dependency updates

Renovate opens update PRs at once and never merges them; the owner chooses when.
