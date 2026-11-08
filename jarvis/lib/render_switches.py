#!/usr/bin/env python3
"""Render an env's Flux wiring from values/switches.yaml (schema/CONTRACT.md).

Writes flux/kustomization.yaml, flux/kustomizations/services.yaml,
flux/platform-configs/kustomization.yaml, flux/generated/apps.yaml and the backup selection.
Names, slots, app lines and the backup selection are checked before the first write; env-schema
then checks what needs the whole env, such as sealed files. A second run changes nothing.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jarvis.lib import apps as _apps
from jarvis.lib import backup_selection as _backup_selection
from jarvis.lib import (
    render_apps,
    render_backup_selection,
)
from jarvis.lib import switches as _switches
from jarvis.schema.env import instance_root, switches_problems

REPO_ROOT = Path(__file__).resolve().parents[2]


def render_env(env_dir: Path, repo_root: Path) -> None:
    if not (env_dir / _switches.SWITCHES_FILE).is_file():
        raise SystemExit(f"{env_dir}: missing {_switches.SWITCHES_FILE}")
    problems = switches_problems(env_dir, repo_root)
    if problems:
        raise SystemExit("; ".join(problems))
    apps = render_apps.render_env(env_dir)
    try:
        selection = render_backup_selection.render_env(env_dir, repo_root)
    except _backup_selection.AppsRepoError as exc:
        raise SystemExit(f"{env_dir}: {exc}") from exc
    data, _ = _switches.load(env_dir)
    flux = env_dir / "flux"
    wrapper = (_backup_selection.WRAPPER_ENTRY,) if selection is not None else ()
    for rel, text in (
        (_switches.ROOT_REL, _switches.root_kustomization(data, apps_generated=_apps.GENERATED_REL)),
        (_switches.SERVICES_REL, _switches.services(data)),
        (_switches.PLATFORM_CONFIGS_REL, _switches.platform_configs(data, extra_components=wrapper)),
    ):
        (flux / rel).parent.mkdir(parents=True, exist_ok=True)
        (flux / rel).write_text(text, encoding="utf-8")
    generated = flux / _apps.GENERATED_REL
    if apps is None:
        generated.unlink(missing_ok=True)
    else:
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(apps, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, help="Env name under envs/")
    args = parser.parse_args(argv)
    repo_root = instance_root(REPO_ROOT)
    render_env(repo_root / "envs" / args.env, repo_root)


if __name__ == "__main__":
    main()
