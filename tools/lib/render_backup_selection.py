#!/usr/bin/env python3
"""Render backup registration Components from values/apps.yaml (platform/backups/CONTRACT.md).

When the env lists no backup-enabled app, remove flux/generated/backup-selection/ and its
line in flux/platform-configs/kustomization.yaml instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from schema.env import app_hosting_listed, instance_root  # noqa: E402
import _app_backups  # noqa: E402
import _apps  # noqa: E402
import _backup_selection  # noqa: E402


def _platform_configs(env_dir: Path) -> Path:
    return env_dir / "flux" / "platform-configs" / "kustomization.yaml"


def _sync_components(env_dir: Path, enabled_namespaces: list[str]) -> None:
    """Drop hand-listed backups/* components; keep app-hosting and the generated wrapper."""
    path = _platform_configs(env_dir)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: expected a Kustomization")
    wrapper = "../" + _backup_selection.GENERATED_COMPONENT_REL
    prefix = f"{_app_backups.REGISTRATION_DIR}/"
    kept: list[object] = []
    for item in doc.get("components") or []:
        if not isinstance(item, str):
            kept.append(item)
            continue
        stripped = item.strip().removeprefix("./")
        if stripped.startswith(prefix) or "backup-selection" in stripped:
            continue
        kept.append(item)
    if enabled_namespaces and wrapper not in kept:
        kept.append(wrapper)
    doc["components"] = kept
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def render_env(env_dir: Path, repo_root: Path) -> str | None:
    data, errors = _apps.load_values(env_dir)
    if errors:
        raise SystemExit(f"{env_dir / _apps.APPS_VALUES}: {'; '.join(errors)}")
    section = _backup_selection.load_section(data)
    if section is None:
        return None
    if not app_hosting_listed(env_dir, repo_root):
        raise SystemExit(f"{env_dir}: backups in apps.yaml but app hosting is off")
    apps_list = _apps.app_names(data)
    apps_root = _backup_selection.apps_repo_root(repo_root, env_dir)
    problems = _backup_selection.validate_section(
        section,
        apps_list=apps_list,
        apps_root=apps_root,
        rel=str(env_dir / _apps.APPS_VALUES),
    )
    if problems:
        raise SystemExit(f"{env_dir / _apps.APPS_VALUES}: {'; '.join(problems)}")
    enabled = _backup_selection.enabled_namespaces(section, apps_list=apps_list, apps_root=apps_root)
    _backup_selection.write_registration_files(env_dir, enabled)
    _sync_components(env_dir, enabled)
    out_dir = env_dir / _backup_selection.GENERATED_COMPONENT
    out_file = env_dir / _backup_selection.GENERATED_KUSTOMIZATION
    text = _backup_selection.render_wrapper_component(enabled)
    if text is None:
        if out_file.is_file():
            out_file.unlink()
        if out_dir.is_dir() and not any(out_dir.iterdir()):
            out_dir.rmdir()
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, help="Env name under envs/")
    args = parser.parse_args(argv)
    repo_root = instance_root(REPO_ROOT)
    env_dir = repo_root / "envs" / args.env
    text = render_env(env_dir, repo_root)
    if text is None:
        raise SystemExit(f"{env_dir}: no backup-enabled app; removed generated backup selection")
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
