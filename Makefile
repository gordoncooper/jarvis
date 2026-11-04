# Operator tools live in tools/. Build and test live in ci/.
SHELL := /bin/bash
.DEFAULT_GOAL := help
ANSIBLE_PLAYBOOK ?= $(CURDIR)/.venv/bin/ansible-playbook

.PHONY: help deps ci-verify verify drill drill-cleanup bootstrap-secrets seal restore restore-all age-key-check site
help:
	@echo "JARVIS Makefile"
	@echo
	@echo "Operator commands"
	@echo "  make deps               Install pinned CLIs and Python dev deps (Ansible, pytest, …)"
	@echo "  make site               Apply an env (ENV=<env> required; optional TAGS=…)"
	@echo "  make verify             Read-only standing-state checks (ENV=<env> required)"
	@echo "  make drill              Scratch volume and etcd restore drill (ENV=<env> required)"
	@echo "  make drill-cleanup      Retry drill namespace/repository cleanup (ENV=<env> required)"
	@echo "  make restore            Restore PVCs (ENV and APP required; optional AS_OF=RFC3339, NODE)"
	@echo "  make restore-all        Restore all PVCs (ENV required; optional RETIRE, ACCEPT_EMPTY, NODE)"
	@echo "  make bootstrap-secrets  Create missing env bootstrap SOPS secrets (ENV=<env> required; ROTATE_RESTIC=1 mints a new restic password)"
	@echo "  make seal NAME=...      Encrypt a Secret manifest from stdin or FILE=... (ENV=<env> required)"
	@echo "  make age-key-check      Test-decrypt one secret; prints PASS or FAIL (ENV and KEY required; owner only)"
	@echo
	@echo "Developer commands"
	@echo "  make ci-verify          Run checks and full pytest (pre-push)"
	@echo "                          Set OWNER_NAME to the owner's personal name, the same value as the GitHub Actions repository variable"

deps:
	python3 tools/install-deps
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install --no-deps -r requirements-dev.txt
	.venv/bin/pip check

ci-verify: deps
	@bash ci/ci-local-verify

verify:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@.venv/bin/python tools/verify/run --env "$(ENV)"

drill:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@.venv/bin/python tools/drill --env "$(ENV)"

drill-cleanup:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@.venv/bin/python tools/drill --env "$(ENV)" --cleanup-only

restore:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@test -n "$(APP)" || (echo "APP is required" >&2; exit 1)
	@.venv/bin/python tools/restore --env "$(ENV)" --app "$(APP)" \
		$(if $(AS_OF),--as-of "$(AS_OF)") \
		$(if $(NODE),--node "$(NODE)")

restore-all:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@.venv/bin/python tools/restore --env "$(ENV)" --all \
		$(if $(RETIRE),--retire "$(RETIRE)") \
		$(if $(ACCEPT_EMPTY),--accept-empty "$(ACCEPT_EMPTY)") \
		$(if $(NODE),--node "$(NODE)")

bootstrap-secrets:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@.venv/bin/python tools/bootstrap-secrets --env "$(ENV)" $(if $(filter 1,$(ROTATE_RESTIC)),--rotate-restic,)

age-key-check:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@test -n "$(KEY)" || (echo "KEY is required" >&2; exit 1)
	@.venv/bin/python tools/age-key-check --env "$(ENV)" --key-file "$(KEY)"

seal:
	@test -n "$(NAME)" || (echo "NAME is required (<component>-<purpose>)" >&2; exit 1)
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@if [ -n "$(FILE)" ]; then \
		.venv/bin/python tools/seal --env "$(ENV)" --name "$(NAME)" "$(FILE)"; \
	else \
		.venv/bin/python tools/seal --env "$(ENV)" --name "$(NAME)"; \
	fi

site:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@PYTHONPATH=$(CURDIR) $(CURDIR)/.venv/bin/python -c \
		"from schema.env import main; raise SystemExit(main(['--env', '$(ENV)']))"
	@cd infra/ansible && $(ANSIBLE_PLAYBOOK) site.yml \
		-i ../../envs/$(ENV)/inventory/hosts.yml \
		-i ../inventory/roles \
		-e jarvis_cluster_vars=../../envs/$(ENV)/values/cluster-vars.yaml \
		$(if $(TAGS),--tags "$(TAGS)")
