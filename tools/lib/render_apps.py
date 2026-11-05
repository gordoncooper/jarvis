#!/usr/bin/env python3
"""Render envs/<env>/flux/generated/apps.yaml from values/apps.yaml (platform/app-hosting/CONTRACT.md).

With no app listed there is nothing to render: remove the file and its line in
flux/kustomization.yaml instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from schema.env import app_hosting_listed, instance_root, resolve_cluster_vars  # noqa: E402  (puts tools/lib on the path)
import _apps  # noqa: E402


def render_env(env_dir: Path) -> str | None:
    """The rendered file, or None when the env lists no app."""
    data, errors = _apps.load_values(env_dir)
    if errors:
        raise SystemExit(f"{env_dir / _apps.APPS_VALUES}: {'; '.join(errors)}")
    if not _apps.app_names(data):
        return None
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")) or {}
    problems = _apps.validate(data, cluster_vars.get("dns_suffix"))
    if problems:
        raise SystemExit(f"{env_dir / _apps.APPS_VALUES}: {'; '.join(problems)}")
    if not app_hosting_listed(env_dir, env_dir.parents[1]):
        raise SystemExit(f"{env_dir}: lists apps, but app hosting is off (platform/app-hosting/CONTRACT.md, Config)")
    if not cluster_vars.get("apps_git_repo_url"):
        raise SystemExit(f"{env_dir}: lists apps, so values/cluster-vars.yaml sets apps_git_repo_url")
    return _apps.render(data, resolve_cluster_vars(cluster_vars))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, help="Env name under envs/")
    parser.add_argument("--output", type=Path, help="Write file instead of stdout")
    args = parser.parse_args(argv)
    env_dir = instance_root(REPO_ROOT) / "envs" / args.env
    text = render_env(env_dir)
    if text is None:
        raise SystemExit(f"{env_dir}: lists no app; there is nothing to render")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
