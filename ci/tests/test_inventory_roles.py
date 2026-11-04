"""Role groups from the constructed inventory (p0a-11a)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    env_inventory_args,
    inventory_path,
    playbook_recap_field,
    roles_inventory_path,
)
from support import ExampleCopy, real_and_copy_envs
from conftest import require_cli


def _expected_role_groups() -> dict[str, set[str]]:
    """Role groups from the example inventory: one per node_roles entry."""
    doc = yaml.safe_load(inventory_path().read_text(encoding="utf-8"))
    hosts = doc["all"]["hosts"]
    groups: dict[str, set[str]] = {}
    for hostname, host_vars in hosts.items():
        host_vars = host_vars or {}
        for role in host_vars.get("node_roles") or []:
            groups.setdefault(role, set()).add(hostname)
    return groups


def _parse_graph(text: str) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        group = re.search(r"@([^:\s]+):\s*$", raw)
        if group:
            current = group.group(1)
            groups.setdefault(current, set())
            continue
        host = re.search(r"--([A-Za-z0-9._-]+)\s*$", raw)
        if host and current is not None:
            groups[current].add(host.group(1))
    return groups


def test_env_00_inventory_graph_shows_role_groups() -> None:
    require_cli("ansible-inventory")
    proc = subprocess.run(
        ["ansible-inventory", "--graph", *env_inventory_args()],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    groups = _parse_graph(proc.stdout)
    expected = _expected_role_groups()
    assert set(expected) <= set(groups)
    for name, hosts in expected.items():
        assert groups[name] == hosts, name
    assert not any(name.startswith("_") for name in groups)


def test_env_hosts_file_alone_fails_before_any_change(example_copy: ExampleCopy) -> None:
    """Missing the roles inventory fails the opening assert, before any change."""
    require_cli("ansible-playbook")
    for root, env in real_and_copy_envs(example_copy):
        proc = subprocess.run(
            [
                "ansible-playbook",
                "--check",
                "site.yml",
                "-i",
                str(root / "envs" / env / "inventory" / "hosts.yml"),
                "-e",
                f"jarvis_cluster_vars={root / 'envs' / env / 'values' / 'cluster-vars.yaml'}",
                "-e",
                "ansible_connection=local",
                "-e",
                "ansible_become=false",
            ],
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        text = proc.stdout + proc.stderr
        assert proc.returncode != 0, text
        assert "Assert exactly one control_plane host" in text
        assert "make site ENV=" in text
        assert "infra/inventory/roles" in text
        assert "roles.yml" not in text
        assert "base-os :" not in text
        assert playbook_recap_field(text, "changed") == 0


def test_data_disk_is_a_host_var_with_no_group(tmp_path: Path) -> None:
    """A host with data_disk: true keeps the var for node-disk, and no data_disk group exists."""
    require_cli("ansible-inventory")
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        "all:\n  hosts:\n    n1:\n      node_roles: [control_plane]\n      data_disk: true\n",
        encoding="utf-8",
    )
    inventory = ["-i", str(hosts), "-i", str(roles_inventory_path())]
    graph = subprocess.run(
        ["ansible-inventory", "--graph", *inventory],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert graph.returncode == 0, graph.stdout + graph.stderr
    assert "data_disk" not in _parse_graph(graph.stdout)

    host = subprocess.run(
        ["ansible-inventory", "--host", "n1", *inventory],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert host.returncode == 0, host.stdout + host.stderr
    assert json.loads(host.stdout)["data_disk"] is True
