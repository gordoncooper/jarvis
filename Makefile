# Wraps tools/ as targets land in P0a (render, check, verify, bootstrap-secrets, seal, …).
.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "JARVIS Makefile — targets are added with tools/ in later P0a tasks."
