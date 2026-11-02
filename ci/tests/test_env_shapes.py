"""Env-00 copies: schema, inventory graph, agent hosts, and a built Flux entry."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from ansible_role_proof import ANSIBLE_DIR, roles_inventory_path
from conftest import require_cli
from schema.env import validate_env_tree
from support import ExampleCopy, bootstrap_example_copy
from test_flux_entry import validate_env_flux
from test_inventory_roles import _parse_graph
from test_k3s import _agent_play_section, _hosts_outside_control_plane, _listed_play_hosts

_SHAPES = Path(__file__).resolve().parent / "fixtures" / "env-shapes"


def test_example_copy_passes_env_schema(example_copy: ExampleCopy) -> None:
    """A bootstrapped copy of env-00 is a valid env."""
    validate_env_tree(example_copy.root)


def test_env_shape_copy_bootstraps_builds_and_lists_hosts(tmp_path: Path, shape: str) -> None:
    """Each hosts shape copies env-00, bootstraps, builds Flux, and lists agent hosts."""
    hosts = (_SHAPES / shape / "hosts.yml").read_text(encoding="utf-8")
    copy = bootstrap_example_copy(tmp_path, hosts_text=hosts)
    validate_env_tree(copy.root)
    validate_env_flux(copy.root, copy.name)
    _assert_inventory_graph(copy)
    assert _listed_play_hosts(_agent_play_section(copy.name, copy.root)) == (
        _hosts_outside_control_plane(copy.name, copy.root)
    )


def pytest_generate_tests(metafunc) -> None:
    if "shape" in metafunc.fixturenames:
        metafunc.parametrize("shape", ["single-node", "gpu-mix", "secondary-disk"])


def _assert_inventory_graph(copy: ExampleCopy) -> None:
    require_cli("ansible-inventory")
    hosts = copy.env_dir / "inventory" / "hosts.yml"
    proc = subprocess.run(
        [
            "ansible-inventory",
            "--graph",
            "-i",
            str(hosts),
            "-i",
            str(roles_inventory_path()),
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    groups = _parse_graph(proc.stdout)
    doc = yaml.safe_load(hosts.read_text(encoding="utf-8"))
    expected: dict[str, set[str]] = {}
    for hostname, host_vars in doc["all"]["hosts"].items():
        host_vars = host_vars or {}
        for role in host_vars.get("node_roles") or []:
            expected.setdefault(role, set()).add(hostname)
        if "data_disk" in host_vars:
            expected.setdefault("data_disk", set()).add(hostname)
    for name, members in expected.items():
        assert groups.get(name) == members, name
    assert groups.get("data_disk", set()) == expected.get("data_disk", set())
