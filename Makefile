# Operator tools live in tools/. Build and test live in ci/.
SHELL := /bin/bash
.DEFAULT_GOAL := help
VENV ?= $(CURDIR)/.venv
ANSIBLE_PLAYBOOK ?= $(VENV)/bin/ansible-playbook
NEED_VENV = test -x "$(VENV)/bin/python" || { echo "no venv at $(VENV): run 'make deps', or set VENV=<path>" >&2; exit 1; }
# INSTANCE=<path> points every env command at <path>/envs/<env> and <path>/.sops.yaml.
# Unset, the env lives in this checkout. The Python tools resolve it in tools/lib/_env.py.
export INSTANCE
INSTANCE_ROOT = $(abspath $(or $(INSTANCE),$(CURDIR)))

.PHONY: help deps ci-verify verify drill drill-cleanup bootstrap-secrets seal restore restore-all age-key-check site
help:
	@echo "JARVIS Makefile"
	@echo
	@echo "Operator commands"
	@echo "  make deps               Install pinned CLIs and Python dev deps (Ansible, pytest, …)"
	@echo "  make site               Apply an env (ENV=<env> required; optional TAGS=… LIMIT=… CHECK=1)"
	@echo "  make verify             Read-only standing-state checks (ENV=<env> required)"
	@echo "  make drill              Scratch volume and etcd restore drill (ENV=<env> required)"
	@echo "  make drill-cleanup      Retry drill namespace/repository cleanup (ENV=<env> required)"
	@echo "  make restore            Restore PVCs (ENV and APP required; optional AS_OF=RFC3339, NODE)"
	@echo "  make restore-all        Restore all PVCs (ENV required; optional AS_OF=RFC3339, RETIRE, ACCEPT_EMPTY, NODE)"
	@echo "  make bootstrap-secrets  Create missing env bootstrap SOPS secrets (ENV=<env> required; ROTATE_RESTIC=1 mints a new restic password)"
	@echo "  make seal NAME=...      Encrypt a Secret manifest from stdin or FILE=... (ENV=<env> required; NAMESPACE=<ns> for an add-on's Secret)"
	@echo "  make age-key-check      Test-decrypt one secret; prints PASS or FAIL (ENV and KEY required; owner only)"
	@echo "  INSTANCE=<path>         Instance checkout that holds envs/<env> and its .sops.yaml (default: this checkout)"
	@echo
	@echo "Developer commands"
	@echo "  make ci-verify          Run checks and full pytest (pre-push)"
	@echo "                          Set OWNER_NAME to the owner's personal name, the same value as the GitHub Actions repository variable"
	@echo "  VENV=<path>             Python venv used by every target (default: .venv in the repo)"

deps:
	python3 tools/install-deps
	@test -d "$(VENV)" || python3 -m venv "$(VENV)"
	"$(VENV)/bin/pip" install --no-deps -r requirements-dev.txt
	"$(VENV)/bin/pip" check

ci-verify: deps
	@bash ci/ci-local-verify

verify:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/verify/run --env "$(ENV)"

drill:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/drill --env "$(ENV)"

drill-cleanup:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/drill --env "$(ENV)" --cleanup-only

restore:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@test -n "$(APP)" || (echo "APP is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/restore --env "$(ENV)" --app "$(APP)" \
		$(if $(AS_OF),--as-of "$(AS_OF)") \
		$(if $(NODE),--node "$(NODE)")

restore-all:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/restore --env "$(ENV)" --all \
		$(if $(AS_OF),--as-of "$(AS_OF)") \
		$(if $(RETIRE),--retire "$(RETIRE)") \
		$(if $(ACCEPT_EMPTY),--accept-empty "$(ACCEPT_EMPTY)") \
		$(if $(NODE),--node "$(NODE)")

bootstrap-secrets:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/bootstrap-secrets --env "$(ENV)" $(if $(filter 1,$(ROTATE_RESTIC)),--rotate-restic,)

age-key-check:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@test -n "$(KEY)" || (echo "KEY is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/age-key-check --env "$(ENV)" --key-file "$(KEY)"

seal:
	@test -n "$(NAME)" || (echo "NAME is required (<component>-<purpose>)" >&2; exit 1)
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@if [ -n "$(FILE)" ]; then \
		"$(VENV)/bin/python" tools/seal --env "$(ENV)" --name "$(NAME)" $(if $(NAMESPACE),--namespace "$(NAMESPACE)") "$(FILE)"; \
	else \
		"$(VENV)/bin/python" tools/seal --env "$(ENV)" --name "$(NAME)" $(if $(NAMESPACE),--namespace "$(NAMESPACE)"); \
	fi

site:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@if [ -n "$(CHECK)" ] && [ "$(CHECK)" != "1" ]; then echo "CHECK must be empty or 1" >&2; exit 1; fi
	@$(NEED_VENV)
	@PYTHONPATH=$(CURDIR) "$(VENV)/bin/python" -c \
		"from schema.env import main; raise SystemExit(main())" \
		--env "$(ENV)"
	@cd infra/ansible && $(ANSIBLE_PLAYBOOK) site.yml \
		-i $(INSTANCE_ROOT)/envs/$(ENV)/inventory/hosts.yml \
		-i ../inventory/roles \
		-e jarvis_instance_root=$(INSTANCE_ROOT) \
		-e jarvis_cluster_vars=$(INSTANCE_ROOT)/envs/$(ENV)/values/cluster-vars.yaml \
		$(if $(TAGS),--tags "$(TAGS)") \
		$(if $(LIMIT),--limit "$(LIMIT)") \
		$(if $(filter 1,$(CHECK)),--check --diff)
