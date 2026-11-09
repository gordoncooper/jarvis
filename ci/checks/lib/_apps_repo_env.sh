# Prepare APPS_REPO for env-level checks (source from ci/checks/*).
# Fixture fallback only when INSTANCE is unset (product self-tests). With INSTANCE, an env that
# lists an app needs a real apps checkout via APPS_REPO — never core test fixtures. A core-only
# instance needs none.
_apps_repo_prepare() {
  local repo_root="$1"
  local fixture="${repo_root}/ci/tests/fixtures/app-hosting/jarvis-apps"
  if [[ -z "${INSTANCE:-}" ]]; then
    export APPS_REPO="${APPS_REPO:-${fixture}}"
    return 0
  fi
  if [[ -n "${APPS_REPO:-}" && "$(readlink -f "${APPS_REPO}")" == "$(readlink -f "${fixture}")" ]]; then
    echo "env check: APPS_REPO must not point at core test fixtures when INSTANCE is set" >&2
    return 1
  fi
  PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m jarvis.apps.checkout require
}
