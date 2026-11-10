# Quickstart

From nothing to a verified cluster on one node. Every command below runs on your controller, the machine you work from. CI runs these commands as written on every release (`.github/workflows/quickstart.yml`), so if one fails for you, it is a bug: please report it.

## You need

- **A node:** a fresh Ubuntu Server machine (amd64) that you can SSH into as a user who can `sudo`. Give it a name that your DNS resolves, from the controller and from the node itself.
- **A controller:** macOS or Linux, with `curl` and `git`.
- **An empty private repository on GitHub** for your clusters. Flux reads it with a read-only deploy key that `jarvis init` makes.

## 1. Your choices

Set these once in your shell. Every command after uses them.

```bash
# Your choices: every command below reads them.
JARVIS_VERSION=v0.5.2                            # the core release to start from
REPO_URL=ssh://git@github.com/<you>/my-clusters  # your new, empty, private repository
BRANCH=main                                      # the branch Flux follows
NODE=node1.home.lan                              # your node's name; your DNS resolves it
LOGIN=ubuntu                                     # the user you log in to the node as
```

## 2. Install the tools

mise installs uv, uv installs the jarvis CLI, and jarvis installs everything else it needs at the versions your cluster's core release pins, on first use.

```bash
curl -fsSL https://mise.run | sh
export PATH="$HOME/.local/bin:$PATH"
mise use -g uv
mise exec -- uv tool install "git+https://github.com/gordoncooper/jarvis@$JARVIS_VERSION"
```

To keep the tools on your `PATH` in new shells, add `eval "$(~/.local/bin/mise activate bash)"` (or `zsh`) to your shell's startup file.

## 3. Write your repository

```bash
jarvis init my-clusters --env home --domain home.lan --repo-url "$REPO_URL" --branch "$BRANCH" --node "$NODE"
cd my-clusters
```

This writes a whole repository for one cluster, `home`, whose services are named under `home.lan`. It holds the cluster's values, its inventory, its sealed secrets and its Flux wiring, plus CI and Renovate setup. It also makes two age keys, which open the secrets: a working key on this machine, and a **recovery key**.

- The recovery key's private half is printed **once**. Store it offline now, on paper or in a password manager entry you don't use day to day. It opens every secret if this machine is lost.
- A **deploy key** is printed for an `ssh://` repository. Add it on GitHub: the repository's **Settings → Deploy keys → Add deploy key**, with **Allow write access** off.

`jarvis init --help` lists the rest: more nodes (`--node` per host, with roles), features (`--feature app-dns`), the example apps (`--with-examples`), and the coding-agent kit (`--with-agents`).

## 4. Prepare the node

```bash
jarvis node prep "$NODE" --login-user "$LOGIN"
```

It logs in as `$LOGIN` and creates the user Ansible will use (you, by default), with your SSH key and passwordless `sudo`. It also installs the few packages Ansible needs and records the host in your inventory. It asks four things:

1. Whether to use the recorded Ansible user: press Enter.
2. Whether to add the node's SSH host key: check the fingerprint against the node's console, then `y`.
3. The node's short hostname: press Enter to keep it.
4. Whether to turn SSH password login off: your choice (`y` is safer).

It also asks for the `sudo` password if `$LOGIN` needs one.

## 5. Push

```bash
git add -A
git commit -m "home: first cluster"
git remote add origin "$REPO_URL"
git push -u origin "$BRANCH"
```

The commit runs the pre-commit hook `jarvis init` installed: `jarvis render --check` and `jarvis validate`.

## 6. Bring it up

```bash
jarvis up
jarvis verify --smoke
```

`jarvis up` checks this machine, the values and the render, refuses until what you pushed is what Flux will read, and installs k3s and Flux on the node. It then waits for every Flux layer to be Ready, and ends with `jarvis verify`: `PASS` on every line, `WARN` for scheduled jobs that have not run yet. It takes about ten to twenty minutes. `jarvis verify --smoke` then uses each capability once (a certificate from the cluster's CA, plus App DNS and app admission when they are on) and cleans up after itself.

## 7. Reach it

- Trust `envs/home/ca.crt` on your devices: it is the root of every certificate the cluster issues.
- Point `*.home.lan` at the node in your DNS (a wildcard record). Or turn App DNS on: add `app-dns` to `features` in `envs/home/values/switches.yaml`, set `app_dns_cluster_ip`, run `jarvis render`, then commit and push. Your router then forwards `home.lan` to the node.
- `https://testapp.home.lan` then answers. It is the cluster's canary.

## Next

- **Another cluster in the same repository:** `jarvis env add lab --domain lab.lan --node <host>`, then steps 4 to 6 with `--env lab`.
- **Day 2:** Renovate opens a pull request for each new core, k3s or app release. CI renders it and checks it, so you review and merge. When a release's notes say hosts change, run `jarvis site`.
- **A hosted app, an add-on, or recovery:** [deploy.md](deploy.md), [add-a-hosted-app.md](add-a-hosted-app.md), [add-an-addon.md](add-an-addon.md), [secrets.md](secrets.md).
- **Another machine, or a teammate:** open the repository in its devcontainer (`.devcontainer/`). It installs the same tools in a container and mounts your age key, kubeconfigs and SSH keys. It needs the repository, so it cannot run `jarvis init` itself.
