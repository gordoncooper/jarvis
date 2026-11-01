"""Helpers for Ansible role proof tests and template renders."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

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


def standard_flux_kustomization_paths(env: str) -> dict[str, str]:
    """The four Flux Kustomizations every env entry applies, in dependsOn order."""
    return {
        "platform-controllers": "./platform",
        "platform-configs": f"./envs/{env}/flux/platform-configs",
        "services": "./services",
        "apps": "./apps",
    }


class ExampleFluxEntry(NamedTuple):
    env: str
    instance_file: Path
    instance: dict
    kustomizations: list[dict]


def example_flux_entry(tmp_path: Path) -> ExampleFluxEntry:
    """The example env's FluxInstance (written to tmp_path) and four Kustomizations.

    The example env has no flux/ tree, so tests that need one build it here
    from its cluster-vars and the flux-operator lookup name.
    """
    env = env_name()
    cluster = load_cluster_vars()
    role = load_role_defaults(FLUX_OPERATOR_ROLE_DIR)
    instance = {
        "apiVersion": "fluxcd.controlplane.io/v1",
        "kind": "FluxInstance",
        "metadata": {
            "name": role["flux_instance_name"],
            "namespace": role["flux_system_namespace"],
        },
        "spec": {
            "sync": {
                "kind": "GitRepository",
                "url": cluster["flux_git_repo_url"],
                "ref": cluster["flux_git_ref"],
                "path": f"envs/{env}/flux",
            },
            "kustomize": {
                "patches": [
                    {
                        "patch": (
                            "- op: add\n"
                            "  path: /spec/template/spec/containers/0/args/-\n"
                            "  value: --feature-gates=StrictPostBuildSubstitutions=true\n"
                        ),
                        "target": {"kind": "Deployment", "name": "kustomize-controller"},
                    }
                ]
            },
        },
    }
    instance_file = tmp_path / "flux-instance.yaml"
    instance_file.write_text(yaml.safe_dump(instance, sort_keys=False), encoding="utf-8")
    names = list(standard_flux_kustomization_paths(env).items())
    kustomizations = [
        {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": name, "namespace": role["flux_system_namespace"]},
            "spec": {
                "path": path,
                "dependsOn": [{"name": names[index - 1][0]}] if index else [],
            },
        }
        for index, (name, path) in enumerate(names)
    ]
    return ExampleFluxEntry(env, instance_file, instance, kustomizations)


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
