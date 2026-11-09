"""The apps repository an instance's envs read: whether they need one, and a checkout of it.

An env that lists no app reads no app file, so a core-only instance needs no apps checkout,
empty or absent. An env that lists one needs the real repository: jarvis validate, render and
verify read each app's file at its pinned tag <app>/v<version>.

fetch() keeps one checkout per apps repository in the user's cache: the default branch and every
tag. The jarvis CLI points APPS_REPO at it before a command that reads app files.

  python -m jarvis.apps.checkout require   fails unless core-only, or APPS_REPO is a real checkout
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import yaml

from jarvis.apps import values as _apps
from jarvis.backups import selection as _backup_selection
from jarvis.env.runtime import instance_root
from jarvis.paths import PRODUCT_ROOT, cache_dir

REPO_ROOT = PRODUCT_ROOT


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
    urls: dict[str, str] = {}
    for env_dir in needing:
        url = _apps_url(env_dir)
        if url is None:
            raise AppsCheckoutError(f"envs/{env_dir.name}: lists apps but has no readable apps_git_repo_url")
        urls[env_dir.name] = url
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
            "INSTANCE is set but APPS_REPO is unset; run the check through the jarvis CLI, "
            "which checks out the env's apps_git_repo_url, or export APPS_REPO"
        )
    _backup_selection.apps_repo_root(instance, needing[0])


def _git(dest: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(dest), *args], capture_output=True, text=True, check=False)
    if proc.returncode:
        raise AppsCheckoutError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()}")


def fetch(instance: Path, dest: Path | None = None) -> Path | None:
    """Check out the apps repository the instance's envs read: its default branch and every tag.

    Returns the checkout, or None when no env lists an app. dest defaults to one folder per
    repository URL in the cache; the folder belongs to this function, so local changes there
    are overwritten.
    """
    found = source(instance)
    if found is None:
        return None
    url, branch = found
    if dest is None:
        dest = cache_dir() / "apps" / hashlib.sha256(url.encode()).hexdigest()[:16]
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "-q")
    try:
        _git(dest, "fetch", "-q", "--depth", "1", url, branch)
    except AppsCheckoutError as exc:
        raise AppsCheckoutError(f"cannot read branch {branch} of {url}: {exc}") from exc
    _git(dest, "checkout", "-q", "-f", "--detach", "FETCH_HEAD")
    try:
        _git(dest, "fetch", "-q", "--depth", "1", "--force", url, "+refs/tags/*:refs/tags/*")
    except AppsCheckoutError as exc:
        raise AppsCheckoutError(f"cannot read the tags of {url}: {exc}") from exc
    return dest


def main(argv: list[str]) -> int:
    instance = instance_root(REPO_ROOT)
    try:
        if argv == ["require"]:
            require(instance)
        else:
            raise AppsCheckoutError("usage: python -m jarvis.apps.checkout require")
    except (AppsCheckoutError, _backup_selection.AppsRepoError) as exc:
        print(f"apps checkout: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
