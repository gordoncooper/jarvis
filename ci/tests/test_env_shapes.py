"""Copies of the sample env, one per hosts shape: schema, a built Flux entry, role groups, and agent hosts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sample
import yaml
from ansible_role_proof import expected_role_groups, playbook, role_groups, roles_inventory_path, site_vars_file
from conftest import require_cli

from jarvis.checks.flux_build import env_tree
from jarvis.schema.env import validate_env_tree
from support import ExampleCopy, bootstrap_example_copy

_SHAPES = Path(__file__).resolve().parent / "fixtures" / "env-shapes"


def test_example_copy_passes_env_schema(example_copy: ExampleCopy) -> None:
    """Rule: a copy of the sample env with every feature and the fixture add-on, as jarvis init and an owner write it,
    is a valid env."""
    validate_env_tree(example_copy.root)


def _agent_hosts(copy: ExampleCopy, tmp_path: Path) -> set[str]:
    """The hosts site.yml's Join k3s agents play lists for copy's env (--list-hosts has no JSON form)."""
    run = playbook(
        "site.yml", "--list-hosts", "-i", copy.env_dir / "inventory" / "hosts.yml", "-i", roles_inventory_path(),
        "-e", f"@{site_vars_file(tmp_path / 'vars.json', copy.root, env=copy.name)}",
    )  # fmt: skip
    assert run.returncode == 0, run.output
    [(count, listed)] = re.findall(r": Join k3s agents\t.*\n.*\n {4}hosts \((\d+)\):\n((?: {6}\S+\n)*)", run.stdout)
    assert len(listed.split()) == int(count), run.stdout
    return set(listed.split())


@pytest.mark.parametrize(
    "hosts",
    [
        pytest.param(sample.HOSTS, id="sample"),
        *(pytest.param((_SHAPES / shape / "hosts.yml").read_text(encoding="utf-8"), id=shape)
          for shape in ("single-node", "gpu-mix", "secondary-disk")),
    ],
)
def test_env_shape_copy_bootstraps_builds_and_lists_hosts(tmp_path: Path, hosts: str) -> None:
    """Rule: a copy of the sample env with each hosts shape is valid and builds, its inventory has one group per node
    role, and every host but the control plane joins as an agent."""
    require_cli("ansible-playbook")
    copy = bootstrap_example_copy(tmp_path, hosts_text=hosts)
    validate_env_tree(copy.root)
    tree = env_tree(copy.root, f"envs/{copy.name}/flux")
    assert tree.error is None and not [path.error for path in tree.paths if path.error], tree
    groups = expected_role_groups(copy.env_dir / "inventory" / "hosts.yml")
    assert role_groups(copy.env_dir / "inventory" / "hosts.yml", roles_inventory_path()) == groups
    assert _agent_hosts(copy, tmp_path) == set(yaml.safe_load(hosts)["all"]["hosts"]) - groups["control_plane"]
