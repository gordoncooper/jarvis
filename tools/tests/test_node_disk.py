"""node-disk refusals: root disk, both confirmations, and label adoption."""

from __future__ import annotations

import subprocess

from ansible_role_proof import ANSIBLE_DIR, REPO_ROOT
from conftest import require_cli

FIXTURE = REPO_ROOT / "tools" / "tests" / "fixtures" / "node-disk"


def test_disk_refusal_cases() -> None:
    require_cli("ansible-playbook")
    proc = subprocess.run(
        ["ansible-playbook", str(FIXTURE / "refusals.yml"), "-i", "wk-2,", "-e", "ansible_connection=local"],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
