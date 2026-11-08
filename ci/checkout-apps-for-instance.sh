#!/usr/bin/env bash
# Check out the apps repository an instance's envs read (the URL from their cluster-vars): its
# default branch and every tag, since each env runs each app at its pinned tag <app>/v<version>
# (values/apps.yaml versions) and the checks read the app there. Prints the checkout directory
# on stdout, or nothing when no env lists an app; logs the checked SHA on stderr.
#
# Instance CI runs this before ci/run-env-checks. make verify needs the same live tip once an
# env selects backups, because eligibility reads the app files, so an operator runs it too:
#   export APPS_REPO="$(INSTANCE=<path> bash ci/checkout-apps-for-instance.sh)"
# The directory (CHECKOUT_APPS_DIR, default <instance>/.ci-apps-checkout) belongs to this script:
# local changes there are overwritten.
set -euo pipefail

if [[ -z "${INSTANCE:-}" ]]; then
  echo "checkout-apps-for-instance: INSTANCE is unset" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
instance="$(readlink -f "${INSTANCE}")"
export PATH="${HOME}/.local/bin:${repo_root}/.venv/bin:${PATH}"

source_line="$(PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m jarvis.checks.apps_checkout source)"
if [[ -z "${source_line}" ]]; then
  echo "checkout-apps-for-instance: no env lists an app; nothing to check out" >&2
  exit 0
fi
read -r apps_url branch <<<"${source_line}"

dest="${CHECKOUT_APPS_DIR:-${instance}/.ci-apps-checkout}"
git init -q "${dest}"
if ! git -C "${dest}" fetch -q --depth 1 "${apps_url}" "${branch}"; then
  echo "checkout-apps-for-instance: cannot read branch ${branch} of ${apps_url}" >&2
  exit 1
fi
git -C "${dest}" checkout -q -f --detach FETCH_HEAD
if ! git -C "${dest}" fetch -q --depth 1 --force "${apps_url}" "+refs/tags/*:refs/tags/*"; then
  echo "checkout-apps-for-instance: cannot read the tags of ${apps_url}" >&2
  exit 1
fi

sha="$(git -C "${dest}" rev-parse HEAD)"
echo "checkout-apps-for-instance: ${apps_url} ${branch} @ ${sha}" >&2
printf '%s\n' "${dest}"
