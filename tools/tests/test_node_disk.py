"""node-disk role: D42 safety rules (p0a-11a)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_DISK_TASKS = REPO_ROOT / "infra/ansible/roles/node-disk/tasks/main.yml"


def test_node_disk_tasks_encode_d42_wipe_gate() -> None:
    text = NODE_DISK_TASKS.read_text(encoding="utf-8")
    assert "Refusing to wipe" in text
    assert "jarvis_wipe_confirm" in text
    assert "wipe_confirmed" in text
    assert "Refuse when data disk is the root disk" in text
    assert "Refuse non-empty jarvis data directory without mark" in text
    assert "check_mode: false" in text
    assert "jarvis_disk_is_marked" in text
