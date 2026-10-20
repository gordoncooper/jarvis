# Wraps tools/ as targets land in P0a (render, check, verify, bootstrap-secrets, seal, …).
.DEFAULT_GOAL := help

.PHONY: help deps bootstrap-secrets seal
help:
	@echo "JARVIS Makefile — targets are added with tools/ in later P0a tasks."
	@echo "  make deps               Install pinned CLIs and Python dev deps (Ansible, pytest, …)"
	@echo "  make bootstrap-secrets  Create missing env bootstrap SOPS secrets (optional ENV=<env>)"
	@echo "  make seal NAME=...      Encrypt a Secret manifest from stdin or FILE=... (optional ENV=<env>)"

deps:
	python3 tools/install-deps
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt
	.venv/bin/ansible-galaxy collection install -r infra/ansible/collections/requirements.yml

bootstrap-secrets:
	@if [ -n "$(ENV)" ]; then \
		python3 tools/bootstrap-secrets --env "$(ENV)"; \
	else \
		python3 tools/bootstrap-secrets; \
	fi

seal:
	@test -n "$(NAME)" || (echo "NAME is required (<component>-<purpose>)" >&2; exit 1)
	@if [ -n "$(ENV)" ]; then \
		env_args="--env $(ENV)"; \
	else \
		env_args=; \
	fi; \
	if [ -n "$(FILE)" ]; then \
		python3 tools/seal $$env_args --name "$(NAME)" "$(FILE)"; \
	else \
		python3 tools/seal $$env_args --name "$(NAME)"; \
	fi
