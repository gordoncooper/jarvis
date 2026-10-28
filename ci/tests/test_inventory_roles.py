"""Role groups from the constructed inventory (p0a-11a)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    REPO_ROOT,
    cluster_vars_playbook_path,
    env_inventory_args,
    inventory_path,
    non_example_env_names,
    playbook_recap_field,
    roles_inventory_path,
)
from conftest import require_cli


def _expected_role_groups() -> dict[str, set[str]]:
    """Role groups from the example inventory: node_roles, plus data_disk is defined."""
    doc = yaml.safe_load(inventory_path().read_text(encoding="utf-8"))
    hosts = doc["all"]["hosts"]
    groups: dict[str, set[str]] = {}
    for hostname, host_vars in hosts.items():
        host_vars = host_vars or {}
        for role in host_vars.get("node_roles") or []:
            groups.setdefault(role, set()).add(hostname)
        if "data_disk" in host_vars:
            groups.setdefault("data_disk", set()).add(hostname)
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


def test_env_hosts_file_alone_fails_before_any_change() -> None:
    """Missing the roles inventory fails the opening assert, before any change."""
    require_cli("ansible-playbook")
    envs = non_example_env_names()
    assert envs
    env = envs[0]
    proc = subprocess.run(
        [
            "ansible-playbook",
            "--check",
            "site.yml",
            "-i",
            str(REPO_ROOT / "envs" / env / "inventory" / "hosts.yml"),
            "-e",
            f"jarvis_cluster_vars=../../envs/{env}/values/cluster-vars.yaml",
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
    assert "-i ../inventory/roles.yml" in text
    assert "base-os :" not in text
    assert playbook_recap_field(text, "changed") == 0


def test_data_disk_group_exists_when_no_host_sets_one(tmp_path: Path) -> None:
    require_cli("ansible-inventory")
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        "all:\n  hosts:\n    n1:\n      node_roles: [control_plane]\n",
        encoding="utf-8",
    )
    graph = subprocess.run(
        ["ansible-inventory", "--graph", "-i", str(hosts), "-i", str(roles_inventory_path())],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert graph.returncode == 0, graph.stdout + graph.stderr
    assert _parse_graph(graph.stdout).get("data_disk") == set()

    require_cli("ansible-playbook")
    play = subprocess.run(
        [
            "ansible-playbook",
            "--syntax-check",
            "site.yml",
            "-i",
            str(hosts),
            "-i",
            str(roles_inventory_path()),
            "-e",
            f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    text = play.stdout + play.stderr
    assert play.returncode == 0, text
    assert "Could not match supplied host pattern, ignoring: data_disk" not in text
