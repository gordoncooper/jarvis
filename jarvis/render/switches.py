"""Render an env's Flux wiring from values/switches.yaml (schema/CONTRACT.md).

Writes flux/kustomization.yaml, flux/kustomizations/services.yaml,
flux/platform-configs/kustomization.yaml with a registration under platform-configs/backups/ for
each listed app whose backup credential is sealed, and flux/generated/apps.yaml. Names, slots and
app lines are checked before the first write; env-schema then checks what needs the whole env,
such as sealed files. A second run changes nothing.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from jarvis.apps import backups as _app_backups
from jarvis.apps import values as _apps
from jarvis.env import switches as _switches
from jarvis.paths import PRODUCT_ROOT
from jarvis.render import apps as render_apps
from jarvis.schema.env import instance_root, switches_problems

REPO_ROOT = PRODUCT_ROOT


def render_env(env_dir: Path, repo_root: Path) -> None:
    if not (env_dir / _switches.SWITCHES_FILE).is_file():
        raise SystemExit(f"{env_dir}: missing {_switches.SWITCHES_FILE}")
    problems = switches_problems(env_dir, repo_root)
    if problems:
        raise SystemExit("; ".join(problems))
    apps = render_apps.render_env(env_dir)
    data, _ = _switches.load(env_dir)
    flux = env_dir / "flux"
    registrations = _app_backups.write_registrations(
        env_dir, _app_backups.registered(env_dir, _switches.names(data, _switches.APPS))
    )
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")) or {}
    for rel, text in (
        (_switches.SYNC_REL, _switches.sync(cluster_vars, env_dir.name)),
        (_switches.ROOT_REL, _switches.root_kustomization(data, apps_generated=_apps.GENERATED_REL)),
        (_switches.SERVICES_REL, _switches.services(data)),
        (_switches.PLATFORM_CONFIGS_REL, _switches.platform_configs(data, extra_components=registrations)),
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
