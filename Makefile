# Operator tools live in tools/. Build and test live in ci/.
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help deps ci-verify verify drill bootstrap-secrets seal restore restore-all
help:
	@echo "JARVIS Makefile"
	@echo
	@echo "Operator commands"
	@echo "  make deps               Install pinned CLIs and Python dev deps (Ansible, pytest, …)"
	@echo "  make verify             Read-only standing-state checks (ENV=<env> required)"
	@echo "  make drill              Scratch volume and etcd restore drill (ENV=<env> required)"
	@echo "  make restore            Restore PVCs (ENV and APP required; optional AS_OF=RFC3339)"
	@echo "  make restore-all        Restore all PVCs (ENV required; optional RETIRE, ACCEPT_EMPTY)"
	@echo "  make bootstrap-secrets  Create missing env bootstrap SOPS secrets (ENV=<env> required)"
	@echo "  make seal NAME=...      Encrypt a Secret manifest from stdin or FILE=... (ENV=<env> required)"
	@echo
	@echo "Developer commands"
	@echo "  make ci-verify          Run checks and full pytest (pre-push)"
	@echo "                          Set OWNER_NAME to the owner's personal name, the same value as the GitHub Actions repository variable"

deps:
	python3 tools/install-deps
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

ci-verify: deps
	@bash ci/ci-local-verify

verify:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@python3 tools/verify/run --env "$(ENV)"

drill:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@python3 tools/drill --env "$(ENV)"

restore:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@test -n "$(APP)" || (echo "APP is required" >&2; exit 1)
	@python3 tools/restore --env "$(ENV)" --app "$(APP)"

restore-all:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@python3 tools/restore --env "$(ENV)" --all \
		$(if $(RETIRE),--retire "$(RETIRE)") \
		$(if $(ACCEPT_EMPTY),--accept-empty "$(ACCEPT_EMPTY)")

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
