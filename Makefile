# Wraps tools/ as targets land in P0a (render, check, verify, bootstrap-secrets, seal, …).
.DEFAULT_GOAL := help

ENV ?= env-01

.PHONY: help deps bootstrap-secrets seal
help:
	@echo "JARVIS Makefile — targets are added with tools/ in later P0a tasks."
	@echo "  make deps               Install pinned sops, age, age-keygen, and gitleaks into ~/.local/bin"
	@echo "  make bootstrap-secrets  Create missing env bootstrap SOPS secrets (ENV=$(ENV))"
	@echo "  make seal NAME=...      Encrypt a Secret manifest from stdin or FILE=..."

deps:
	python3 tools/install-deps

bootstrap-secrets:
	python3 tools/bootstrap-secrets --env $(ENV)

seal:
	@test -n "$(NAME)" || (echo "NAME is required (<component>-<purpose>)" >&2; exit 1)
	@if [ -n "$(FILE)" ]; then \
		python3 tools/seal --env $(ENV) --name $(NAME) $(FILE); \
	else \
		python3 tools/seal --env $(ENV) --name $(NAME); \
	fi
