#!/usr/bin/env bash
# Check out the apps repository for instance env-level checks (branch main, latest).
# Prints the checkout directory on stdout; logs the resolved SHA on stderr.
# Requires INSTANCE. Used by instance CI before ci/run-env-checks.
set -euo pipefail

if [[ -z "${INSTANCE:-}" ]]; then
  echo "checkout-apps-for-instance: INSTANCE is unset" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
instance="$(readlink -f "${INSTANCE}")"

apps_url="$(
  python3 - <<'PY'
import os
import sys
from pathlib import Path

import yaml

instance = Path(os.environ["INSTANCE"]).resolve()
envs = instance / "envs"
if not envs.is_dir():
    sys.exit(0)
for env_dir in sorted(p for p in envs.iterdir() if p.is_dir()):
    apps_path = env_dir / "values" / "apps.yaml"
    cv_path = env_dir / "values" / "cluster-vars.yaml"
    if not cv_path.is_file():
        continue
    try:
        apps_data = yaml.safe_load(apps_path.read_text(encoding="utf-8")) if apps_path.is_file() else {}
        cv = yaml.safe_load(cv_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        continue
    apps = apps_data.get("apps") if isinstance(apps_data, dict) else None
    backups = apps_data.get("backups") if isinstance(apps_data, dict) else None
    needs = (isinstance(apps, list) and len(apps) > 0) or backups is not None
    if not needs:
        continue
    url = cv.get("apps_git_repo_url")
    if isinstance(url, str) and url.strip():
        print(url.strip())
        break
PY
)"

if [[ -z "${apps_url}" ]]; then
  echo "checkout-apps-for-instance: no env lists hosted apps or backups; nothing to check out" >&2
  exit 0
fi

dest="${CHECKOUT_APPS_DIR:-${instance}/.ci-apps-checkout}"
if [[ -d "${dest}/.git" ]]; then
  git -C "${dest}" fetch --depth 1 origin main
  git -C "${dest}" checkout -f main
  git -C "${dest}" reset --hard origin/main
else
  rm -rf "${dest}"
  git clone --depth 1 --branch main "${apps_url}" "${dest}"
fi

sha="$(git -C "${dest}" rev-parse HEAD)"
echo "checkout-apps-for-instance: ${apps_url} @ ${sha}" >&2
printf '%s\n' "${dest}"
