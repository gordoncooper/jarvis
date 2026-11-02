"""Shared test helpers.

One repo root, one script loader, and the fixtures several modules used to
copy from each other. Ansible role renders stay in ansible_role_proof.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_RENOVATE_NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_]\w*)>")


def load_tool(module_name: str, path: Path) -> ModuleType:
    """Load a repo script that is not an importable package."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def renovate_python_pattern(pattern: str) -> str:
    """Renovate's RE2 named group `(?<name>...)` as Python `re` `(?P<name>...)`.

    This is not Renovate's engine. A pattern RE2 accepts and Python reads
    differently still matches here.
    """
    return _RENOVATE_NAMED_GROUP.sub(r"(?P<\1>", pattern)


def _kustomize_build(rel_path: str) -> str:
    """Chart defaults the values don't set pass unchecked."""
    kustomize = shutil.which("kustomize")
    if not kustomize:
        pytest.fail("kustomize not found on PATH (run make deps)")
    proc = subprocess.run(
        [
            kustomize,
            "build",
            "--load-restrictor",
            "LoadRestrictionsNone",
            str(REPO_ROOT / rel_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _default_hosts() -> str:
    return (
        textwrap.dedent(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup, edge]
            """
        ).strip()
        + "\n"
    )


def _ensure_sops_creation_rule(root: Path, env_name: str) -> None:
    import yaml

    path = root / ".sops.yaml"
    rules: list[dict[str, object]] = []
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict) and isinstance(loaded.get("creation_rules"), list):
            rules = list(loaded["creation_rules"])
    needle = f"envs/{env_name}/secrets/"
    if any(
        isinstance(rule, dict) and needle in str(rule.get("path_regex", ""))
        for rule in rules
    ):
        return
    rules.append(
        {
            "path_regex": f"envs/{env_name}/secrets/.*\\.sops\\.yaml$",
            "age": "age1fixture",
        }
    )
    path.write_text(
        yaml.safe_dump({"creation_rules": rules}, sort_keys=False),
        encoding="utf-8",
    )


def _valid_cluster_vars() -> dict:
    import yaml
    from schema.env import EXAMPLE_ENV

    cluster_vars_path = REPO_ROOT / "envs" / EXAMPLE_ENV / "values" / "cluster-vars.yaml"
    reference = yaml.safe_load(cluster_vars_path.read_text(encoding="utf-8"))
    assert isinstance(reference, dict)
    return dict(reference)


def _write_env(
    root: Path,
    env_name: str = "env-test",
    *,
    cluster_vars: dict | None = None,
    omit_ssh_user: bool = False,
    hosts_yml: str | None = None,
) -> Path:
    import yaml
    from schema.env import EXAMPLE_ENV, is_example_env

    env = root / "envs" / env_name
    (env / "values").mkdir(parents=True)
    (env / "inventory").mkdir(parents=True)

    cv = cluster_vars if cluster_vars is not None else _valid_cluster_vars()
    if omit_ssh_user:
        cv = {k: v for k, v in cv.items() if k != "ssh_user"}
    if cv.get("jarvis_env") in (None, EXAMPLE_ENV):
        cv["jarvis_env"] = env_name

    (env / "values" / "cluster-vars.yaml").write_text(
        yaml.safe_dump(cv, sort_keys=False), encoding="utf-8"
    )
    (env / "values" / "hermes.yaml").write_text(
        textwrap.dedent(
            """
            bots: []
            cron: {}
            bot_tier_map:
              concierge: mid
            tier_model_map: {}
            lane_model_map: {}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (env / "values" / "apps.yaml").write_text(
        "app_hosting_enabled: false\napps: []\n",
        encoding="utf-8",
    )
    (env / "inventory" / "hosts.yml").write_text(
        hosts_yml if hosts_yml is not None else _default_hosts(),
        encoding="utf-8",
    )
    if not is_example_env(env_name):
        _ensure_sops_creation_rule(root, env_name)
    return env
