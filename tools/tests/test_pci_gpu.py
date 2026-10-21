"""GPU fact assert: PCI vendor files live behind symlinks (p0a-11b)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ansible_role_proof import ANSIBLE_DIR, REPO_ROOT
from conftest import require_cli

FIXTURE_DIR = REPO_ROOT / "tools" / "tests" / "fixtures" / "gpu-assert"
NVIDIA = "0x10de\n"
OTHER = "0x8086\n"


def _sysfs(root: Path, vendor: str) -> Path:
    devices = root / "devices"
    real = root / "real" / "0000:01:00.0"
    real.mkdir(parents=True)
    (real / "vendor").write_text(vendor, encoding="utf-8")
    devices.mkdir()
    (devices / "0000:01:00.0").symlink_to("../real/0000:01:00.0", target_is_directory=True)
    return devices


def _assert_gpu(devices: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ansible-playbook",
            str(FIXTURE_DIR / "play.yml"),
            "-i",
            str(FIXTURE_DIR / "hosts.yml"),
            "-e",
            f"node_pci_devices_path={devices}",
            "-e",
            "ansible_become=false",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def test_nvidia_vendor_behind_a_symlink_passes(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    proc = _assert_gpu(_sysfs(tmp_path, NVIDIA))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_missing_nvidia_vendor_behind_a_symlink_fails(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    proc = _assert_gpu(_sysfs(tmp_path, OTHER))
    text = proc.stdout + proc.stderr
    assert proc.returncode != 0, text
    assert "no NVIDIA device" in text
