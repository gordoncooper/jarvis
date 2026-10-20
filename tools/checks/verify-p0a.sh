#!/usr/bin/env bash
# Phase verify for P0a (not a CI rule check; run via make verify PHASE=p0a).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

export PYTHONPATH="${repo_root}/tools:${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"

exec python3 -m _verify_p0a
