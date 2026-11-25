"""The constructed inventory: data_disk stays a host var with no group, and site.yml refuses the env hosts file alone."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    changed,
    failed_task,
    inventory_path,
    playbook,
    role_groups,
    roles_inventory_path,
    site_vars_file,
)
from conftest import require_cli


def test_env_hosts_file_alone_fails_before_any_change(tmp_path: Path) -> None:
    """Rule: site.yml given only the env hosts file stops at the control-plane assert, before any other task, and
    says to pass both inventories."""
    require_cli("ansible-playbook")
    hosts = inventory_path()
    run = playbook(
        "--check", "site.yml", "-i", hosts, "-e", f"@{site_vars_file(tmp_path / 'vars.json')}",
        "-e", "ansible_connection=local", "-e", "ansible_become=false", json=True,
    )  # fmt: skip
    assert run.returncode != 0, run.output
    assert [task["task"]["name"] for play in run.json["plays"] for task in play["tasks"]] == ["Assert exactly one control_plane host"]
    assert failed_task(run) == (
        "Assert exactly one control_plane host",
        "control_plane has 0 hosts; expected exactly one. Pass both inventories with jarvis site --env <env>"
        " (hosts file, then infra/inventory/roles).",
    )
    assert changed(run) == dict.fromkeys(yaml.safe_load(hosts.read_text(encoding="utf-8"))["all"]["hosts"], 0)
    assert "roles.yml" not in run.output


def test_data_disk_is_a_host_var_with_no_group(tmp_path: Path) -> None:
    """Rule: a host's data_disk: true reaches node-disk as a host var, and no group is made from it."""
    require_cli("ansible-inventory")
    hosts = tmp_path / "hosts.yml"
    hosts.write_text("all:\n  hosts:\n    n1:\n      node_roles: [control_plane]\n      data_disk: true\n", encoding="utf-8")
    assert role_groups(hosts, roles_inventory_path()) == {"control_plane": {"n1"}}
    host = subprocess.run(["ansible-inventory", "--host", "n1", "-i", str(hosts), "-i", str(roles_inventory_path())],
                          cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False)
    assert host.returncode == 0, host.stderr
    assert json.loads(host.stdout)["data_disk"] is True
