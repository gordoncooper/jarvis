# Operator tools live in tools/. Build and test live in ci/.
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help deps ci-verify verify bootstrap-secrets seal restore restore-all
help:
	@echo "JARVIS Makefile"
	@echo
	@echo "Operator commands"
	@echo "  make deps               Install pinned CLIs and Python dev deps (Ansible, pytest, …)"
	@echo "  make verify             Phase exit check (PHASE=p0a|p0b ENV=<env>; p0b needs DRILL_LOG)"
	@echo "  make restore            Restore PVCs in namespace APP (optional AS_OF=RFC3339)"
	@echo "  make restore-all        Restore every enrolled PVC (optional RETIRE=ns-a/pvc-a,ns-b/pvc-b)"
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
	@test -n "$(PHASE)" || (echo "PHASE is required" >&2; exit 1)
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@case "$(PHASE)" in \
	  p0a) python3 tools/verify/p0a --env "$(ENV)" ;; \
	  p0b) test -n "$(DRILL_LOG)" || { echo "DRILL_LOG is required" >&2; exit 1; }; \
	       python3 tools/verify/p0b --env "$(ENV)" --drill-log "$(DRILL_LOG)" ;; \
	  *) echo "unknown PHASE=$(PHASE)" >&2; exit 1 ;; \
	esac

restore:
	@test -n "$(APP)" || (echo "APP is required" >&2; exit 1)
	@python3 tools/restore --app "$(APP)"

restore-all:
	@python3 tools/restore --all $(if $(RETIRE),--retire "$(RETIRE)")

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
