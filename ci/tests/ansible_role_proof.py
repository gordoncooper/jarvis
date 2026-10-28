"""Helpers for Ansible role proof tests and template renders."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

from schema.env import EXAMPLE_ENV, is_example_env

REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = REPO_ROOT / "infra" / "ansible"
K3S_ROLE_DIR = ANSIBLE_DIR / "roles" / "k3s"
FLUX_OPERATOR_ROLE_DIR = ANSIBLE_DIR / "roles" / "flux-operator"
FLUX_OPERATOR_ROLE_TEMPLATES = FLUX_OPERATOR_ROLE_DIR / "templates"
FLUX_OPERATOR_ROLE_TASKS = FLUX_OPERATOR_ROLE_DIR / "tasks"
K3S_ROLE_TEMPLATES = K3S_ROLE_DIR / "templates"


def env_name() -> str:
    return EXAMPLE_ENV


def non_example_env_names(repo_root: Path = REPO_ROOT) -> list[str]:
    envs = repo_root / "envs"
    return sorted(
        path.name
        for path in envs.iterdir()
        if path.is_dir() and not is_example_env(path.name)
    )


def cluster_vars_playbook_path() -> str:
    return f"../../envs/{env_name()}/values/cluster-vars.yaml"


def inventory_path(repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / "envs" / env_name() / "inventory" / "hosts.yml"


def roles_inventory_path(repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / "infra" / "inventory" / "roles.yml"


def env_inventory_args(repo_root: Path = REPO_ROOT) -> list[str]:
    """Env hosts first, then the constructed role groups. Order is required."""
    return [
        "-i",
        str(inventory_path(repo_root).resolve()),
        "-i",
        str(roles_inventory_path(repo_root).resolve()),
    ]


def load_role_defaults(role_dir: Path) -> dict[str, object]:
    data = yaml.safe_load((role_dir / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def load_cluster_vars(
    repo_root: Path = REPO_ROOT, *, env: str | None = None
) -> dict[str, object]:
    name = env_name() if env is None else env
    path = repo_root / "envs" / name / "values" / "cluster-vars.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def ansible_render_template(
    template_name: str,
    variables: dict[str, object],
    *,
    templates_dir: Path,
    inventory: Path | None = None,
) -> str:
    """Render a role template via ansible.builtin.template (same engine as the role)."""
    src = templates_dir / template_name
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        dest = Path(handle.name)
    extra: list[str] = []
    if variables:
        extra.extend(["-e", json.dumps(variables)])
    cmd = ["ansible", "localhost"]
    if inventory is not None:
        cmd.extend(["-i", str(inventory)])
    cmd.extend(
        [
            "-c",
            "local",
            "-m",
            "ansible.builtin.template",
            "-a",
            f"src={src} dest={dest}",
            *extra,
        ]
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    run = subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    try:
        if run.returncode != 0:
            raise AssertionError(run.stdout + run.stderr)
        return dest.read_text(encoding="utf-8")
    finally:
        dest.unlink(missing_ok=True)


def playbook_recap_field(output: str, field: str) -> int:
    recap_lines = [
        line
        for line in output.splitlines()
        if f"{field}=" in line and "PLAY RECAP" not in line
    ]
    assert recap_lines, output
    match = re.search(rf"{field}=(\d+)", recap_lines[-1])
    assert match is not None, output
    return int(match.group(1))
