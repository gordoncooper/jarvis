# Wraps tools/ (render, check, bootstrap-secrets, seal, …).
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help deps ci-verify bootstrap-secrets seal
help:
	@echo "JARVIS Makefile"
	@echo "  make deps               Install pinned CLIs and Python dev deps (Ansible, pytest, …)"
	@echo "  make ci-verify          Run checks and full pytest (pre-push)"
	@echo "  make bootstrap-secrets  Create missing env bootstrap SOPS secrets (ENV=<env> required)"
	@echo "  make seal NAME=...      Encrypt a Secret manifest from stdin or FILE=... (ENV=<env> required)"

deps:
	python3 tools/install-deps
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

ci-verify: deps
	@bash tools/ci-local-verify

bootstrap-secrets:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@python3 tools/bootstrap-secrets --env "$(ENV)"

seal:
	@test -n "$(NAME)" || (echo "NAME is required (<component>-<purpose>)" >&2; exit 1)
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@if [ -n "$(FILE)" ]; then \
		python3 tools/seal --env "$(ENV)" --name "$(NAME)" "$(FILE)"; \
	else \
		python3 tools/seal --env "$(ENV)" --name "$(NAME)"; \
	fi
