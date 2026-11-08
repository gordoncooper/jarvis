#!/usr/bin/env python3
"""Render backup registration Components from values/apps.yaml (core/platform/backups/CONTRACT.md).

render_switches.py runs this with the rest of the env's wiring. With no backup-enabled app it
removes flux/generated/backup-selection/ and its line in flux/platform-configs/kustomization.yaml.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jarvis.lib import apps as _apps
from jarvis.lib import backup_selection as _backup_selection
from jarvis.lib import switches as _switches
from jarvis.schema.env import app_hosting_listed, instance_root, switches_problems

REPO_ROOT = Path(__file__).resolve().parents[2]


def render_env(env_dir: Path, repo_root: Path) -> str | None:
    data, errors = _apps.load_values(env_dir)
    if errors:
        raise SystemExit(f"{env_dir / _apps.APPS_VALUES}: {'; '.join(errors)}")
    section = _backup_selection.load_section(data)
    if section is None:
        if (env_dir / _backup_selection.GENERATED_KUSTOMIZATION).is_file():
            raise SystemExit(f"{env_dir}: {_backup_selection.SECTION_REMOVED}")
        return None
    if not app_hosting_listed(env_dir):
        raise SystemExit(f"{env_dir}: backups in apps.yaml but app hosting is off")
    apps_list = _apps.app_names(data)
    apps_root = _backup_selection.selection_apps_root(repo_root, env_dir, apps_list)
    problems = _backup_selection.validate_section(
        section,
        apps_list=apps_list,
        apps_root=apps_root,
        rel=str(env_dir / _apps.APPS_VALUES),
    )
    if problems:
        raise SystemExit(f"{env_dir / _apps.APPS_VALUES}: {'; '.join(problems)}")
    enabled = _backup_selection.enabled_namespaces(section, apps_list=apps_list, apps_root=apps_root)
    # The wiring is rewritten from the switches below, and a name it does not know would drop silently.
    problems = switches_problems(env_dir, repo_root)
    if problems:
        raise SystemExit("; ".join(problems))
    switches, _ = _switches.load(env_dir)
    if enabled and _switches.APPS_BACKUP_SERVER not in _switches.names(switches, _switches.OPTIONAL):
        raise SystemExit(
            f"{env_dir}: backups select {enabled}, so {_switches.SWITCHES_FILE} lists "
            f"{_switches.APPS_BACKUP_SERVER} under {_switches.OPTIONAL}"
        )
    _backup_selection.write_registration_files(env_dir, enabled)
    wrapper = (_backup_selection.WRAPPER_ENTRY,) if enabled else ()
    (env_dir / "flux" / _switches.PLATFORM_CONFIGS_REL).write_text(
        _switches.platform_configs(switches, extra_components=wrapper), encoding="utf-8"
    )
    out_dir = env_dir / _backup_selection.GENERATED_COMPONENT
    out_file = env_dir / _backup_selection.GENERATED_KUSTOMIZATION
    text = _backup_selection.render_wrapper_component(enabled, section.get("overrides"))
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
    try:
        text = render_env(env_dir, repo_root)
    except _backup_selection.AppsRepoError as exc:
        raise SystemExit(f"{env_dir}: {exc}") from exc
    if text is None:
        print(f"{env_dir}: no backup-enabled app; no backup selection to generate", file=sys.stderr)
        return
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
