"""install-deps: sha256 verification and the apt package set."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from support import REPO_ROOT, load_tool

INSTALL_DEPS = REPO_ROOT / "tools" / "install-deps"
WRONG_BYTES = b"not the real binary"


def _load_module():
    return load_tool("install_deps", INSTALL_DEPS)


def test_verify_sha256_mismatch(tmp_path: Path) -> None:
    mod = _load_module()
    blob = tmp_path / "blob"
    blob.write_bytes(WRONG_BYTES)
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        mod.verify_sha256(blob, "0" * 64, "sops")


@pytest.mark.parametrize("name", ["sops", "kustomize"])
def test_wrong_download_bytes_install_nothing(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary or archive whose bytes miss the pinned checksum is never installed."""
    mod = _load_module()
    monkeypatch.setattr(mod, "download", lambda url, dest: dest.write_bytes(WRONG_BYTES))
    versions = {
        "sops_version": "1.0.0",
        "sops_linux_amd64_binary_sha256": "0" * 64,
        "kustomize_version": "1.0.0",
        "kustomize_linux_amd64_tarball_sha256": "0" * 64,
    }
    dest_dir = tmp_path / "bin"
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        if name == "sops":
            mod.install_sops(versions, "amd64", dest_dir)
        else:
            mod.install_tarball_cli(
                name, mod.TARBALL_INSTALLS[name], versions, "amd64", dest_dir
            )
    assert not (dest_dir / name).exists()


@pytest.mark.parametrize("machine", ["aarch64", "arm64", "riscv64"])
def test_only_amd64_is_supported(machine: str, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod.platform, "machine", lambda: machine)
    with pytest.raises(SystemExit, match="amd64 only"):
        mod.linux_arch()
    monkeypatch.setattr(mod.platform, "machine", lambda: "x86_64")
    assert mod.linux_arch() == "amd64"


def test_apt_refreshes_once_and_asks_for_yaml_but_not_bcrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apt-get update runs once before the install. python3-yaml is still requested, python3-bcrypt is not."""
    mod = _load_module()
    calls: list[list[str]] = []
    installed = False

    def fake_run(cmd, **kwargs):
        nonlocal installed
        calls.append(list(cmd))
        if cmd[:2] == ["python3", "-c"]:
            # Nothing is importable until apt runs. bcrypt never becomes importable.
            if installed and "bcrypt" not in cmd[2]:
                return subprocess.CompletedProcess(cmd, 0)
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[:3] == ["sudo", "apt-get", "install"]:
            installed = True
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    mod.require_system_tools()

    updates = [c for c in calls if c[:3] == ["sudo", "apt-get", "update"]]
    installs = [c for c in calls if c[:3] == ["sudo", "apt-get", "install"]]
    assert len(updates) == 1
    assert len(installs) == 1
    assert calls.index(updates[0]) < calls.index(installs[0])
    assert "python3-yaml" in installs[0]
    assert "python3-venv" in installs[0]
    assert "python3-bcrypt" not in installs[0]


def test_named_tools_install_alone_and_an_unknown_name_installs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: install-deps <tool>... installs only those, and an unknown name fails before any download."""
    mod = _load_module()
    calls: list[str] = []
    monkeypatch.setattr(mod, "require_system_tools", lambda: None)
    monkeypatch.setattr(mod, "install_sops", lambda *args: calls.append("sops"))
    monkeypatch.setattr(mod, "install_tarball_cli", lambda name, *args: calls.append(name))
    mod.main(["kyverno", "kustomize"])
    assert calls == ["kyverno", "kustomize"]
    with pytest.raises(SystemExit, match="unknown tool helm"):
        mod.main(["helm"])
    assert calls == ["kyverno", "kustomize"]
