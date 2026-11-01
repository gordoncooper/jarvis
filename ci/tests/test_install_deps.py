"""install-deps sha256 verification."""

from __future__ import annotations

from pathlib import Path

import pytest

from support import REPO_ROOT, load_tool

INSTALL_DEPS = REPO_ROOT / "tools" / "install-deps"


def _load_module():
    return load_tool("install_deps", INSTALL_DEPS)


def test_verify_sha256_mismatch(tmp_path: Path) -> None:
    mod = _load_module()
    blob = tmp_path / "blob"
    blob.write_bytes(b"not the real binary")
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        mod.verify_sha256(blob, "0" * 64, "sops")
