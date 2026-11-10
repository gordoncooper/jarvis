# Agents

Two things live under this name.

- **The agent kit for instance repositories.** `jarvis init --with-agents` copies it into a new repository: `AGENTS.md` (the rules for a coding agent working on envs), `CLAUDE.md` (which points at it), and a short deny list in each client's own format, `.claude/settings.json` and `.cursor/cli.json`. Its files are in `jarvis/kit/agents/`, inside the package, because init runs from the installed CLI before any core checkout exists. To add the kit to a repository made without it, copy those four files to the same places.
- **The rules for working on core itself:** [coding-agents.md](coding-agents.md), and [review.md](review.md) for the session that reviews a pull request. Core's own `.claude/settings.json` and `.cursor/cli.json` are links to the kit's deny lists, so core and every instance block the same commands.

## What the kit relies on

The real control is credential absence: the user an agent runs as holds no age key, no kubeconfig and no SSH key, so it cannot reach a host, a cluster or a secret whatever it runs. The deny lists are a speed bump on top of that. No list can block every form of a command, and neither client can block some forms (a command spelled through another interpreter, for one). They block:

- commands that reach real hosts or a cluster: `ssh`, `scp`, `sftp`, ad-hoc `ansible`, `kubectl`, `helm`, `flux`, and the jarvis commands `site`, `up`, `node`, `verify`, `drill`, `restore`, `secrets` and `age-key-check`;
- secret decryption (`sops`, `age -d`) and reads of the age key, kubeconfigs, SSH keys and k3s's own files;
- force-pushes, pushes to `main`, and merges.

The two lists say the same thing in two syntaxes and are kept in step by hand. Change both in one PR.
