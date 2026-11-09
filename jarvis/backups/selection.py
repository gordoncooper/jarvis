"""Env backup selection for hosted apps: modes, eligibility, and registration intent.

CONTRACT: core/platform/backups/CONTRACT.md (App credentials), schema/CONTRACT.md (values/apps.yaml
backups). bootstrap-secrets still mints sealed credentials; render_backup_selection.py writes
registration Components and the platform-configs wrapper from this intent.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from jarvis.apps import values as _apps
from jarvis.backups import app_credentials as _app_backups
from jarvis.env.runtime import instance_root

BACKUPS_KEY = "backups"
GENERATED_COMPONENT_REL = "generated/backup-selection"
GENERATED_COMPONENT = Path("flux") / GENERATED_COMPONENT_REL
GENERATED_KUSTOMIZATION = GENERATED_COMPONENT / "kustomization.yaml"
# The entry the env's platform-configs lists under components.
WRAPPER_ENTRY = "../" + GENERATED_COMPONENT_REL
MODES = frozenset({"selected", "all_except"})
MODE_SELECTED = "selected"
MODE_ALL_EXCEPT = "all_except"
OVERRIDE_KEYS = frozenset(
    {
        "backup_schedule",
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
    }
)
_CRON = re.compile(r"^(\S+\s+){4}\S+$")
_RETENTION = re.compile(r"^[0-9]+$")
# Deleting the section must not read as turning an app's backups off.
SECTION_REMOVED = "backups is gone from values/apps.yaml but apps still register; set mode: selected with selected: [] and re-render first"
_APPS_REPO_ENV = "APPS_REPO"
STALE_HINT = "; eligibility reads the app at its pinned tag, so check the apps checkout has its tags"


class AppsRepoError(Exception):
    """The apps repository checkout is missing or unreadable."""


def _repo_basename(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts:
        return None
    return parts[-1].removesuffix(".git")


def _looks_like_apps_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any((path / child / "app.yaml").is_file() for child in path.iterdir() if child.is_dir())


def _resolve_apps_checkout(repo_root: Path, env_dir: Path) -> Path | None:
    cv_path = env_dir / "values" / "cluster-vars.yaml"
    if not cv_path.is_file():
        return None
    try:
        cluster_vars = yaml.safe_load(cv_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    url = cluster_vars.get("apps_git_repo_url")
    if not isinstance(url, str) or not url.strip():
        return None
    name = _repo_basename(url)
    if not name:
        return None
    roots = {instance_root(repo_root), repo_root.resolve()}
    candidates: list[Path] = []
    for root in roots:
        candidates.extend((root.parent / name, root / name))
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _looks_like_apps_repo(resolved):
            return resolved
    return None


_PINNED_VIEWS: dict[tuple[Path, tuple[tuple[str, str], ...]], Path] = {}


def _archive(checkout: Path, tag: str, app: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(checkout), "archive", "--format=tar", tag, app],
                          capture_output=True, check=False)


def _pinned_view(checkout: Path, env_dir: Path | None) -> Path:
    """The app folders the env runs, each at its pinned tag, when the checkout is a git
    repository and the env pins its apps (values/apps.yaml versions); otherwise the checkout as
    it is (a fixture tree). A pinned tag the checkout lacks fails: the checks must read the
    release the cluster runs, not the tip."""
    if env_dir is None or not (checkout / ".git").exists():
        return checkout
    data, errors = _apps.load_values(env_dir)
    versions = data.get(_apps.VERSIONS) if not errors else None
    if not isinstance(versions, dict) or not versions:
        return checkout
    pins = tuple(sorted((str(app), str(version)) for app, version in versions.items()))
    key = (checkout, pins)
    if key in _PINNED_VIEWS:
        return _PINNED_VIEWS[key]
    view = Path(tempfile.mkdtemp(prefix="jarvis-apps-pinned-"))
    fetched = False
    for app, version in pins:
        tag = f"refs/tags/{app}/{version}"
        archive = _archive(checkout, tag, app)
        if archive.returncode and not fetched:
            # A pin newer than the checkout's last fetch: fetch its tags once (it moves no branch).
            fetched = True
            subprocess.run(["git", "-C", str(checkout), "fetch", "--quiet", "--tags"], capture_output=True, check=False)
            archive = _archive(checkout, tag, app)
        if archive.returncode:
            raise AppsRepoError(f"{checkout}: no tag {app}/{version} with an {app}/ folder, "
                                "even after fetching the apps repository's tags")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(view, filter="data")
    _PINNED_VIEWS[key] = view
    return view


def apps_repo_root(repo_root: Path, env_dir: Path | None = None) -> Path:
    """Directory of app folders (<name>/app.yaml), each app as the env pins it.

    APPS_REPO overrides for tests and operators. Otherwise resolve a local checkout of
    apps_git_repo_url beside the instance or product checkout. A git checkout is read at each
    app's pinned tag (_pinned_view).
    """
    override = os.environ.get(_APPS_REPO_ENV)
    if override:
        path = Path(override).expanduser().resolve()
        if not _looks_like_apps_repo(path):
            raise AppsRepoError(f"{_APPS_REPO_ENV}={override!r} is not an apps repository checkout")
        return _pinned_view(path, env_dir)
    if env_dir is None:
        raise AppsRepoError(
            f"set {_APPS_REPO_ENV} to a checkout of the env's apps_git_repo_url, "
            "or clone that repository beside this checkout"
        )
    resolved = _resolve_apps_checkout(repo_root, env_dir)
    if resolved is not None:
        return _pinned_view(resolved, env_dir)
    raise AppsRepoError(
        f"set {_APPS_REPO_ENV} to a checkout of apps_git_repo_url, "
        "or clone the apps repository beside this checkout"
    )


def selection_apps_root(repo_root: Path, env_dir: Path, apps_list: Sequence[str]) -> Path | None:
    """Where a selection reads app files, or None when the env lists no app: it then reads none,
    so a core-only env passes with an empty or absent checkout. An env that lists one fails closed."""
    return apps_repo_root(repo_root, env_dir) if apps_list else None


def selection_problems(repo_root: Path, env_dir: Path, rel: str) -> list[str]:
    """Why the env's backups section cannot say which apps are enabled; empty when it can or is absent.

    Read this before enabled_app_names: that assumes a valid section and a readable checkout.
    """
    data, errors = _apps.load_values(env_dir)
    if errors:
        return [f"{rel}: {problem}" for problem in errors]
    section = load_section(data)
    if section is None:
        return []
    try:
        apps_list = _apps.app_names(data)
        apps_root = selection_apps_root(repo_root, env_dir, apps_list)
        return validate_section(section, apps_list=apps_list, apps_root=apps_root, rel=rel)
    except AppsRepoError as exc:
        return [f"{rel}: {exc}"]


def backup_eligible(apps_root: Path | None, app_name: str) -> bool:
    """True when the app file declares at least one volume with backup: true.

    No checkout, or a missing or unreadable file, raises AppsRepoError: the checkout cannot
    answer, which must not read as stateless and silently drop the app's backup.
    """
    if apps_root is None:
        raise AppsRepoError(f"no apps checkout to read {app_name}'s app file (set APPS_REPO){STALE_HINT}")
    path = apps_root / app_name / "app.yaml"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AppsRepoError(f"{path}: app file is missing or unreadable{STALE_HINT}") from exc
    spec = doc.get("spec") if isinstance(doc, dict) else None
    volumes = spec.get("volumes") or [] if isinstance(spec, dict) else None
    if not isinstance(volumes, list):
        raise AppsRepoError(f"{path}: not an app file (spec.volumes)")
    return any(isinstance(volume, dict) and volume.get("backup") is True for volume in volumes)


def load_section(data: dict[str, Any]) -> dict[str, Any] | None:
    section = data.get(BACKUPS_KEY)
    if section is None:
        return None
    return section if isinstance(section, dict) else {}


def validate_section(
    section: dict[str, Any],
    *,
    apps_list: Sequence[str],
    apps_root: Path | None,
    rel: str,
) -> list[str]:
    errors: list[str] = []
    allowed = {"mode", "selected", "exclude", "overrides"}
    errors.extend(f"{rel}.{BACKUPS_KEY}: unknown key {key!r}" for key in sorted(set(section) - allowed, key=str))
    mode = section.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        errors.append(f"{rel}.{BACKUPS_KEY}: mode must be {MODE_SELECTED!r} or {MODE_ALL_EXCEPT!r}")
        return errors
    listed = set(apps_list)
    selected = section.get("selected", [] if mode == MODE_SELECTED else None)
    exclude = section.get("exclude", [] if mode == MODE_ALL_EXCEPT else None)
    overrides = section.get("overrides", {})
    if mode == MODE_SELECTED:
        if selected is None:
            selected = []
        if exclude is not None:
            errors.append(f"{rel}.{BACKUPS_KEY}: exclude is only valid when mode is {MODE_ALL_EXCEPT!r}")
    if mode == MODE_ALL_EXCEPT:
        if exclude is None:
            exclude = []
        if selected is not None:
            errors.append(f"{rel}.{BACKUPS_KEY}: selected is only valid when mode is {MODE_SELECTED!r}")
    for label, names in (("selected", selected), ("exclude", exclude)):
        if names is None:
            continue
        if not isinstance(names, list):
            errors.append(f"{rel}.{BACKUPS_KEY}.{label} must be a list of app names")
            continue
        seen: set[str] = set()
        for item in names:
            if not isinstance(item, str) or not _apps.APP_NAME.fullmatch(item):
                errors.append(f"{rel}.{BACKUPS_KEY}.{label}: {item!r} is not an app name")
            elif item not in listed:
                errors.append(f"{rel}.{BACKUPS_KEY}.{label}: {item} is not on this env's app list")
            elif item in seen:
                errors.append(f"{rel}.{BACKUPS_KEY}.{label}: {item} is listed twice")
            else:
                seen.add(item)
    if not isinstance(overrides, dict):
        errors.append(f"{rel}.{BACKUPS_KEY}.overrides must be a mapping of app name to override keys")
        return errors
    for app, values in overrides.items():
        where = f"{rel}.{BACKUPS_KEY}.overrides.{app}"
        if not isinstance(app, str) or not _apps.APP_NAME.fullmatch(app):
            errors.append(f"{where}: not an app name")
            continue
        if app not in listed:
            errors.append(f"{where}: {app} is not on this env's app list")
        if not isinstance(values, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        errors.extend(
            f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(OVERRIDE_KEYS))})"
            for key in sorted(set(values) - OVERRIDE_KEYS, key=str)
        )
        for key, value in values.items():
            if key == "backup_schedule" and (not isinstance(value, str) or not _CRON.fullmatch(value.strip())):
                errors.append(f"{where}: backup_schedule must be a five-field cron")
            if str(key).startswith("backup_retention") and (
                not isinstance(value, int) or value < 0 or not _RETENTION.fullmatch(str(value))
            ):
                errors.append(f"{where}: {key} must be a non-negative integer")
    if errors:
        return errors
    enabled = enabled_app_names(section, apps_list=apps_list, apps_root=apps_root)
    for app in overrides:
        if app not in enabled:
            errors.append(f"{rel}.{BACKUPS_KEY}.overrides.{app}: overrides apply only to backup-enabled apps")
    if mode == MODE_SELECTED:
        for app in selected or []:
            if not backup_eligible(apps_root, app):
                errors.append(
                    f"{rel}.{BACKUPS_KEY}.selected: {app} has no backup-eligible volume "
                    f"(stateless apps belong off the list{STALE_HINT})"
                )
    return errors


def enabled_app_names(
    section: dict[str, Any],
    *,
    apps_list: Sequence[str],
    apps_root: Path | None,
) -> list[str]:
    """App names that should have a registered backup credential, in apps list order."""
    mode = section.get("mode")
    if mode == MODE_SELECTED:
        chosen = section.get("selected") or []
        return [app for app in apps_list if app in chosen and backup_eligible(apps_root, app)]
    if mode == MODE_ALL_EXCEPT:
        excluded = section.get("exclude") or []
        return [app for app in apps_list if app not in excluded and backup_eligible(apps_root, app)]
    return []


def enabled_namespaces(section: dict[str, Any], *, apps_list: Sequence[str], apps_root: Path | None) -> list[str]:
    return [f"app-{name}" for name in enabled_app_names(section, apps_list=apps_list, apps_root=apps_root)]


def write_registration_files(env_dir: Path, enabled_namespaces: Sequence[str]) -> None:
    """Write each registration Component under flux/platform-configs/backups/."""
    root = env_dir / "flux" / "platform-configs" / _app_backups.REGISTRATION_DIR
    enabled = set(enabled_namespaces)
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name not in enabled:
                for path in child.iterdir():
                    path.unlink()
                child.rmdir()
    for namespace in enabled_namespaces:
        folder = root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "kustomization.yaml"
        path.write_text(_app_backups.registration_component(namespace), encoding="utf-8")


def _overrides_generator(overrides: dict[str, Any]) -> str:
    """The generated ConfigMap the pvc-backup policies read: one key per namespace with an override."""
    literals = [
        "app-" + app + "=" + json.dumps(
            {key: value.strip() if isinstance(value, str) else value for key, value in values.items()},
            sort_keys=True,
        )
        for app, values in sorted(overrides.items())
        if values
    ]
    if not literals:
        return ""
    return yaml.safe_dump(
        {
            "configMapGenerator": [
                {
                    "name": _app_backups.OVERRIDES_CONFIGMAP,
                    "namespace": "platform-backups",
                    "options": {"disableNameSuffixHash": True},
                    "literals": literals,
                }
            ]
        },
        sort_keys=False,
    )


def render_wrapper_component(enabled_namespaces: Sequence[str], overrides: dict[str, Any] | None = None) -> str | None:
    """The Component platform-configs lists, or None when nothing is backup-enabled."""
    if not enabled_namespaces:
        return None
    lines = [
        "# Generated by jarvis render from values/apps.yaml — do not edit.",
        "# Registers backup credentials for apps the env selects (core/platform/backups/CONTRACT.md).",
        "apiVersion: kustomize.config.k8s.io/v1alpha1",
        "kind: Component",
        "components:",
    ]
    lines.extend(
        f"  - ../../platform-configs/{_app_backups.REGISTRATION_DIR}/{namespace}" for namespace in enabled_namespaces
    )
    return "\n".join(lines) + "\n" + _overrides_generator(overrides or {})
