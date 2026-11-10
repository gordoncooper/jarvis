# Agents

This repository holds Jarvis envs. Under `envs/<env>/` an env keeps its values, inventory and sealed secrets; `jarvis render` writes its `flux/` from them, and Flux applies that. These rules came with `jarvis init --with-agents`.

## What an agent may do here

- Change values, inventory and hosted-app files under `envs/<env>/`, then run `jarvis render` and `jarvis validate`. `jarvis render --check` only reads; `jarvis doctor` checks the machine and installs the pre-commit hook in a fresh clone.
- Work on a branch and open a pull request. The repository's owner reviews and merges every one.

## Hard stops

- **No hosts and no cluster.** No `ssh`, `scp`, `kubectl`, `flux` or `helm`, and none of the jarvis commands that reach a host, a cluster or a secret: `site`, `up`, `node`, `verify`, `drill`, `restore`, `secrets`, `age-key-check`. The owner, or an operator they choose, runs those.
- **No secrets.** Never decrypt a sealed file, and never read the age key, a kubeconfig or an SSH key. A new secret is sealed by the owner with `jarvis encrypt` from a file they hold.
- **Render owns `flux/`.** Never edit a file under `envs/<env>/flux/`; change the values and render again.
- **No plaintext addresses.** LAN IP addresses and numeric user IDs stay out of the repository, or go encrypted under `envs/<env>/secrets/`. `lan_cidrs` and the CIDR in a `lan-target` line are the only exceptions.
- **No force-push, no push to the default branch, no merge.**

The deny lists in `.claude/settings.json` and `.cursor/cli.json` block many of these. They are a speed bump, not the control: the real control is that the agent's user holds no age key, kubeconfig or SSH key. Treat every rule above as a hard stop whether a list blocks it or not.

## When a rule looks wrong

Say so with your reasoning before working around it. Do not follow it silently, and do not depart from it silently.
