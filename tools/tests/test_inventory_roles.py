"""Role groups from the constructed inventory (p0a-11a)."""

from __future__ import annotations

import re
import subprocess

from ansible_role_proof import ANSIBLE_DIR, env_inventory_args
from conftest import require_cli

_EXPECTED_GROUPS = {
    "control_plane": {"cp-1"},
    "workers": {"wk-1", "wk-2", "wk-3", "wk-4"},
    "gpu": {"wk-2", "wk-3"},
    "edge": {"wk-1"},
    "storage": {"st-1"},
    "backup": {"bk-1"},
    "data_disk": {"cp-1", "wk-1", "wk-2", "wk-3", "st-1", "bk-1"},
}


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
    assert set(_EXPECTED_GROUPS) <= set(groups)
    for name, hosts in _EXPECTED_GROUPS.items():
        assert groups[name] == hosts, name
    assert not any(name.startswith("_") for name in groups)
