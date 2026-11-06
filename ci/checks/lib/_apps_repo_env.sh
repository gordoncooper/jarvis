# Prepare APPS_REPO for env-level checks (source from ci/checks/*).
# Fixture fallback only when INSTANCE is unset (product self-tests). With INSTANCE,
# require a real apps checkout via APPS_REPO — never core test fixtures.
_apps_repo_prepare() {
  local repo_root="$1"
  local fixture="${repo_root}/ci/tests/fixtures/app-hosting/jarvis-apps"
  if [[ -z "${INSTANCE:-}" ]]; then
    export APPS_REPO="${APPS_REPO:-${fixture}}"
    return 0
  fi
  if [[ -z "${APPS_REPO:-}" ]]; then
    echo "env check: INSTANCE is set but APPS_REPO is unset; check out the env's apps_git_repo_url tree (see ci/checkout-apps-for-instance.sh) and export APPS_REPO" >&2
    return 1
  fi
  local resolved_apps resolved_fixture
  resolved_apps="$(readlink -f "${APPS_REPO}")"
  resolved_fixture="$(readlink -f "${fixture}")"
  if [[ "${resolved_apps}" == "${resolved_fixture}" ]]; then
    echo "env check: APPS_REPO must not point at core test fixtures when INSTANCE is set" >&2
    return 1
  fi
  return 0
}
