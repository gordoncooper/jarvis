"""Render envs/<env>/flux/generated/apps.yaml from values/apps.yaml (core/feature/app-hosting/CONTRACT.md).

jarvis/render/switches.py runs this with the rest of the env's wiring. It refuses what env-schema's
apps_problems refuses, each app read at its pinned tag, where it also reads each chart app's
chart for its release. With no app listed there is nothing to render, and it removes the file
and its line in flux/kustomization.yaml.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from jarvis.apps import checkout as _checkout
from jarvis.apps import values as _apps
from jarvis.paths import PRODUCT_ROOT
from jarvis.schema.env import apps_problems, resolve_cluster_vars

REPO_ROOT = PRODUCT_ROOT


def render_env(env_dir: Path) -> str | None:
    """The rendered file, or None when the env lists no app."""
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")) or {}
    problems = apps_problems(env_dir, env_dir.parent.parent.resolve(), cluster_vars)
    if problems:
        raise SystemExit("; ".join(problems))
    data, _ = _apps.load_values(env_dir)
    if not _apps.app_names(data):
        return None
    return _apps.render(data, resolve_cluster_vars(cluster_vars), _checkout.apps_repo_root(REPO_ROOT, env_dir))
