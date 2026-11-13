#!/usr/bin/env bash
# The devcontainer's controller toolchain: mise, uv from it, then the jarvis CLI at the core release
# the first env pins. Each jarvis command installs its env's own pinned tools on first use.
set -euo pipefail

# renovate: datasource=github-releases depName=jdx/mise
MISE_VERSION=v2026.11.8
curl -fsSL https://mise.run | MISE_VERSION="${MISE_VERSION}" sh
export PATH="${HOME}/.local/bin:${PATH}"
grep -q 'mise activate' "${HOME}/.bashrc" || echo 'eval "$(~/.local/bin/mise activate bash)"' >> "${HOME}/.bashrc"
mise use -g uv

vars="$(find envs -path '*/values/cluster-vars.yaml' | sort | head -n 1)"
url="$(sed -n 's/^core_git_repo_url:[[:space:]]*//p' "${vars}" | tr -d "\"'")"
tag="$(sed -n 's#^core_git_ref:[[:space:]]*refs/tags/##p' "${vars}" | tr -d "\"'")"
mise exec -- uv tool install --force "git+${url}@${tag}"
echo "jarvis from ${url}@${tag} is installed; run jarvis doctor"
