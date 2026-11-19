"""GPU fact assert: PCI vendor files live behind symlinks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from ansible_role_proof import REPO_ROOT, playbook
from conftest import require_cli

FIXTURE_DIR = REPO_ROOT / "ci" / "tests" / "fixtures" / "gpu-assert"
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


def _assert_gpu(devices: Path, hosts: str = "hosts.yml") -> subprocess.CompletedProcess[str]:
    require_cli("ansible-playbook")
    return playbook(FIXTURE_DIR / "play.yml", "-i", FIXTURE_DIR / hosts, "-e", f"node_pci_devices_path={devices}")


@pytest.mark.parametrize(
    ("vendor", "hosts", "passes"),
    [
        (NVIDIA, "hosts.yml", True),  # the vendor file sits behind a symlink
        (OTHER, "hosts-no-gpu.yml", True),  # only hosts declared gpu need an NVIDIA device
        (None, "hosts.yml", False),  # no PCI devices at all
        (OTHER, "hosts.yml", False),
    ],
)
def test_a_gpu_host_needs_an_nvidia_device_behind_the_sysfs_links(tmp_path: Path, vendor, hosts: str, passes: bool) -> None:
    devices = _sysfs(tmp_path, vendor) if vendor else tmp_path / "devices"
    devices.mkdir(exist_ok=True)
    proc = _assert_gpu(devices, hosts)
    assert (proc.returncode == 0) is passes, proc.output
    assert ("no NVIDIA device" in proc.output) is not passes
