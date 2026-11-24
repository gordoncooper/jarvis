# Developer targets for a core checkout. Operators use the jarvis CLI from an instance repo.
SHELL := /bin/bash
.DEFAULT_GOAL := help
VENV ?= $(CURDIR)/.venv
NEED_MISE = command -v mise >/dev/null || { echo "make deps needs mise: https://mise.jdx.dev/getting-started.html" >&2; exit 1; }
# Every target finds the CLIs mise installed (mise.toml and mise.dev.toml) first on PATH, and uv
# syncs into VENV. -E dev, not an exported MISE_ENV: $(shell) sees none before GNU make 4.4.
MISE_BIN_PATHS := $(shell command -v mise >/dev/null && mise -E dev bin-paths 2>/dev/null | paste -sd: -)
export PATH := $(if $(MISE_BIN_PATHS),$(MISE_BIN_PATHS):)$(PATH)
export UV_PROJECT_ENVIRONMENT := $(VENV)

.PHONY: help deps venv ci-verify
help:
	@echo "JARVIS core: developer targets"
	@echo
	@echo "  make deps        Install the pinned CLIs (mise) and every Python package (uv) into .venv,"
	@echo "                   with the jarvis CLI at .venv/bin/jarvis"
	@echo "  make venv        Only the jarvis package and its Python dependencies"
	@echo "  make ci-verify   Run the rule checks and the full test suite (pre-push)"
	@echo "  VENV=<path>      The venv every target uses (default: .venv in the repo)"
	@echo
	@echo "Operators run jarvis from an instance repo: jarvis --help (docs/deploy.md)."

# Every pinned CLI (mise.toml and mise.dev.toml, checksums in their locks) and the venv with every
# Python package (uv.lock): the jarvis CLI, Ansible, and the test and lint tooling. mise.toml's deps
# task is the one definition; the jarvis CLI runs it without -E dev for the core release an env pins.
deps:
	@$(NEED_MISE)
	mise trust --quiet "$(CURDIR)/mise.toml"
	mise -E dev run deps

# The venv with only the jarvis package's own dependencies.
venv:
	@$(NEED_MISE)
	mise trust --quiet "$(CURDIR)/mise.toml"
	mise install --locked uv
	mise exec -- uv sync --frozen --no-dev

ci-verify: deps
	@bash ci/ci-local-verify
