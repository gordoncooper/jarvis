# Agents

Infra, platform, and services for Jarvis (GitOps on k3s).

Read `agents/coding-agents.md` for how to work on core. A session reviewing a pull request reads `agents/review.md`.

Repo-level tool permissions live in `.cursor/cli.json` and `.claude/settings.json`, which express the same deny intent in their own formats. Both link to the agent kit in `jarvis/kit/agents/`, which `jarvis init --with-agents` copies into an instance repository ([agents/README.md](agents/README.md)).
