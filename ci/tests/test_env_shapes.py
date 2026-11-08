"""Env-00 copies: schema, inventory graph, agent hosts, and a built Flux entry."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import ANSIBLE_DIR, roles_inventory_path
from conftest import require_cli
from jarvis.schema.env import validate_env_tree
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
    _assert_shape(shape, yaml.safe_load(hosts))
    validate_env_tree(copy.root)
    validate_env_flux(copy.root, copy.name)
    _assert_inventory_graph(copy)
    assert _listed_play_hosts(_agent_play_section(copy.name, copy.root)) == (
        _hosts_outside_control_plane(copy.name, copy.root)
    )


def test_gpu_mix_without_the_gpu_role_fails() -> None:
    """Dropping gpu from the mix no longer has GPU workers."""
    doc = yaml.safe_load((_SHAPES / "gpu-mix" / "hosts.yml").read_text(encoding="utf-8"))
    for host in doc["all"]["hosts"].values():
        host["node_roles"] = [role for role in host["node_roles"] if role != "gpu"]
    with pytest.raises(AssertionError):
        _assert_shape("gpu-mix", doc)


def test_secondary_disk_without_data_disk_fails() -> None:
    """Dropping data_disk leaves no host with the flag."""
    doc = yaml.safe_load((_SHAPES / "secondary-disk" / "hosts.yml").read_text(encoding="utf-8"))
    for host in doc["all"]["hosts"].values():
        host.pop("data_disk", None)
    with pytest.raises(AssertionError):
        _assert_shape("secondary-disk", doc)


def _assert_shape(shape: str, doc: dict) -> None:
    hosts: dict = doc["all"]["hosts"]
    if shape == "single-node":
        assert len(hosts) == 1
        return
    if shape == "gpu-mix":
        control = [name for name, host in hosts.items() if "control_plane" in host["node_roles"]]
        workers = [name for name, host in hosts.items() if "workers" in host["node_roles"]]
        assert len(control) == 1
        assert "gpu" not in hosts[control[0]]["node_roles"]
        assert workers
        assert all("gpu" in hosts[name]["node_roles"] for name in workers)
        return
    if shape == "secondary-disk":
        marked = [name for name, host in hosts.items() if host.get("data_disk") is True]
        assert len(marked) == 1
        return
    raise AssertionError(shape)


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
    for name, members in expected.items():
        assert groups.get(name) == members, name
