"""Which apps repository an instance's env checks read, and whether they need one at all.

An env that lists no app reads no app file, so a core-only instance needs no apps checkout,
empty or absent. An env that lists one needs the real repository: env checks and make verify
read eligibility from it, at the tip of the branch Flux deploys.

  source   prints "<url> <branch>" for ci/checkout-apps-for-instance.sh; nothing when core-only
  require  fails unless a core-only instance, or APPS_REPO is a real apps checkout
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

from jarvis.lib import apps as _apps  # noqa: E402
from jarvis.lib import backup_selection as _backup_selection  # noqa: E402
from jarvis.lib.env import instance_root  # noqa: E402

# The wiring every env's app sources come from: its ref is the branch Flux deploys.


class AppsCheckoutError(Exception):
    pass


def _envs_needing_apps(instance: Path) -> list[Path]:
    """Envs that list an app. Values that do not parse count: only the checks can clear them."""
    envs = instance / "envs"
    if not envs.is_dir():
        return []
    needing = []
    for env_dir in sorted(path for path in envs.iterdir() if path.is_dir()):
        data, errors = _apps.load_values(env_dir)
        if errors or _apps.app_names(data):
            needing.append(env_dir)
    return needing


def _apps_url(env_dir: Path) -> str | None:
    try:
        values = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    url = values.get("apps_git_repo_url") if isinstance(values, dict) else None
    return url.strip() if isinstance(url, str) and url.strip() else None


def source(instance: Path) -> tuple[str, str] | None:
    """The (url, ref) to check out, or None when no env lists an app: the repository's default
    branch, whose tags hold each app's pinned release."""
    needing = _envs_needing_apps(instance)
    if not needing:
        return None
    urls = {env_dir.name: _apps_url(env_dir) for env_dir in needing}
    for env, url in urls.items():
        if url is None:
            raise AppsCheckoutError(f"envs/{env}: lists apps but has no readable apps_git_repo_url")
    if len(set(urls.values())) > 1:
        # One APPS_REPO serves one run: checking an env against another's repository proves nothing.
        raise AppsCheckoutError(f"envs read different apps repositories: {sorted(urls)}")
    return next(iter(urls.values())), "HEAD"


def require(instance: Path) -> None:
    """Refuse an instance that lists apps without a real checkout; a core-only one passes."""
    needing = _envs_needing_apps(instance)
    if not needing:
        return
    if not os.environ.get("APPS_REPO"):
        raise AppsCheckoutError(
            "INSTANCE is set but APPS_REPO is unset; check out the env's apps_git_repo_url tree "
            "(see ci/checkout-apps-for-instance.sh) and export APPS_REPO"
        )
    _backup_selection.apps_repo_root(instance, needing[0])


def main(argv: list[str]) -> int:
    instance = instance_root(REPO_ROOT)
    try:
        if argv == ["source"]:
            found = source(instance)
            if found:
                print(*found)
        elif argv == ["require"]:
            require(instance)
        else:
            raise AppsCheckoutError(f"usage: {__file__} source|require")
    except (AppsCheckoutError, _backup_selection.AppsRepoError) as exc:
        print(f"apps checkout: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
