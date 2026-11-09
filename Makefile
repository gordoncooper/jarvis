# Operator tools live in tools/. Build and test live in ci/.
SHELL := /bin/bash
.DEFAULT_GOAL := help
VENV ?= $(CURDIR)/.venv
ANSIBLE_PLAYBOOK ?= $(VENV)/bin/ansible-playbook
PIN_CHECK = "$(VENV)/bin/python" tools/pin-check --env "$(ENV)" $(if $(filter 1,$(ALLOW_UNPINNED)),--allow-unpinned)
NEED_VENV = test -x "$(VENV)/bin/python" || { echo "no venv at $(VENV): run 'make deps', or set VENV=<path>" >&2; exit 1; }
NEED_MISE = command -v mise >/dev/null || { echo "make deps needs mise: https://mise.jdx.dev/getting-started.html" >&2; exit 1; }
# Every target finds the CLIs mise installed (mise.toml) first on PATH, and uv syncs into VENV.
MISE_BIN_PATHS := $(shell command -v mise >/dev/null && mise bin-paths 2>/dev/null | paste -sd: -)
export PATH := $(if $(MISE_BIN_PATHS),$(MISE_BIN_PATHS):)$(PATH)
export UV_PROJECT_ENVIRONMENT := $(VENV)
# INSTANCE=<path> points every env command at <path>/envs/<env> and <path>/.sops.yaml.
# Unset, the env lives in this checkout. The Python tools resolve it in jarvis/lib/env.py.
export INSTANCE
INSTANCE_ROOT = $(abspath $(or $(INSTANCE),$(CURDIR)))

.PHONY: help deps venv ci-verify verify drill drill-cleanup bootstrap-secrets encrypt restore restore-all age-key-check render site
help:
	@echo "JARVIS Makefile"
	@echo
	@echo "Operator commands"
	@echo "  make deps               Install the pinned CLIs (mise) and every Python package (uv) into .venv"
	@echo "  make venv               Only the jarvis package's Python dependencies (enough for make render)"
	@echo "  make site               Apply an env (ENV=<env> required; optional TAGS=… LIMIT=… CHECK=1)"
	@echo "  make verify             Read-only standing-state checks (ENV=<env> required)"
	@echo "  make drill              Scratch volume and etcd restore drill (ENV=<env> required)"
	@echo "  make drill-cleanup      Retry drill namespace/repository cleanup (ENV=<env> required)"
	@echo "  make restore            Restore PVCs (ENV and APP required; optional AS_OF=RFC3339, NODE)"
	@echo "  make restore-all        Restore all PVCs (ENV required; optional AS_OF=RFC3339, RETIRE, ACCEPT_EMPTY, NODE)"
	@echo "  make bootstrap-secrets  Create missing env bootstrap SOPS secrets (ENV=<env> required; ROTATE_RESTIC=1 mints a new restic password; APP=app-<name> mints only that hosted app's backup credential)"
	@echo "  make encrypt NAME=...   Encrypt a Secret manifest from stdin or FILE=... (ENV=<env> required; NAMESPACE=<ns> for an add-on's Secret)"
	@echo "  make age-key-check      Test-decrypt one secret; prints PASS or FAIL (ENV and KEY required; owner only)"
	@echo "  make render             Render the env's generated files from its values (ENV required; CHECK=1 lists stale ones)"
	@echo "  ALLOW_UNPINNED=1        Let site, verify, drill and restore run from a checkout that is not the env's core tag"
	@echo "  INSTANCE=<path>         Instance checkout that holds envs/<env> and its .sops.yaml (default: this checkout)"
	@echo
	@echo "Developer commands"
	@echo "  make ci-verify          Run checks and full pytest (pre-push)"
	@echo "  VENV=<path>             Python venv used by every target (default: .venv in the repo)"

# Every pinned CLI (mise.toml, checksums in mise.lock) and the venv with every Python package
# (uv.lock): the operator tools, Ansible, and the test and lint tooling.
deps:
	@$(NEED_MISE)
	mise trust --quiet "$(CURDIR)/mise.toml"
	mise install --locked
	mise exec -- uv sync --frozen

# The venv with only the jarvis package's own dependencies: enough for tools/render.
venv:
	@$(NEED_MISE)
	mise trust --quiet "$(CURDIR)/mise.toml"
	mise install --locked uv
	mise exec -- uv sync --frozen --no-dev

ci-verify: deps
	@bash ci/ci-local-verify

verify:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@$(PIN_CHECK)
	@"$(VENV)/bin/python" tools/verify/run --env "$(ENV)"

drill:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@$(PIN_CHECK)
	@"$(VENV)/bin/python" tools/drill --env "$(ENV)"

drill-cleanup:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@$(PIN_CHECK)
	@"$(VENV)/bin/python" tools/drill --env "$(ENV)" --cleanup-only

restore:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@test -n "$(APP)" || (echo "APP is required" >&2; exit 1)
	@$(NEED_VENV)
	@$(PIN_CHECK)
	@"$(VENV)/bin/python" tools/restore --env "$(ENV)" --app "$(APP)" \
		$(if $(AS_OF),--as-of "$(AS_OF)") \
		$(if $(NODE),--node "$(NODE)")

restore-all:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@$(PIN_CHECK)
	@"$(VENV)/bin/python" tools/restore --env "$(ENV)" --all \
		$(if $(AS_OF),--as-of "$(AS_OF)") \
		$(if $(RETIRE),--retire "$(RETIRE)") \
		$(if $(ACCEPT_EMPTY),--accept-empty "$(ACCEPT_EMPTY)") \
		$(if $(NODE),--node "$(NODE)")

bootstrap-secrets:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/bootstrap-secrets --env "$(ENV)" $(if $(filter 1,$(ROTATE_RESTIC)),--rotate-restic,) $(if $(APP),--app "$(APP)",)

render:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/render --env "$(ENV)" $(if $(filter 1,$(CHECK)),--check)

age-key-check:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@test -n "$(KEY)" || (echo "KEY is required" >&2; exit 1)
	@$(NEED_VENV)
	@"$(VENV)/bin/python" tools/age-key-check --env "$(ENV)" --key-file "$(KEY)"

encrypt:
	@test -n "$(NAME)" || (echo "NAME is required (<component>-<purpose>)" >&2; exit 1)
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@$(NEED_VENV)
	@if [ -n "$(FILE)" ]; then \
		"$(VENV)/bin/python" tools/encrypt --env "$(ENV)" --name "$(NAME)" $(if $(NAMESPACE),--namespace "$(NAMESPACE)") "$(FILE)"; \
	else \
		"$(VENV)/bin/python" tools/encrypt --env "$(ENV)" --name "$(NAME)" $(if $(NAMESPACE),--namespace "$(NAMESPACE)"); \
	fi

site:
	@test -n "$(ENV)" || (echo "ENV is required" >&2; exit 1)
	@if [ -n "$(CHECK)" ] && [ "$(CHECK)" != "1" ]; then echo "CHECK must be empty or 1" >&2; exit 1; fi
	@$(NEED_VENV)
	@PYTHONPATH=$(CURDIR) "$(VENV)/bin/python" -c \
		"from jarvis.schema.env import main; raise SystemExit(main())" \
		--env "$(ENV)"
	@$(PIN_CHECK)
	@cd infra/ansible && $(ANSIBLE_PLAYBOOK) site.yml \
		-i $(INSTANCE_ROOT)/envs/$(ENV)/inventory/hosts.yml \
		-i ../inventory/roles \
		-e jarvis_instance_root=$(INSTANCE_ROOT) \
		-e jarvis_cluster_vars=$(INSTANCE_ROOT)/envs/$(ENV)/values/cluster-vars.yaml \
		$(if $(TAGS),--tags "$(TAGS)") \
		$(if $(LIMIT),--limit "$(LIMIT)") \
		$(if $(filter 1,$(CHECK)),--check --diff)
