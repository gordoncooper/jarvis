"""install-deps sha256 verification."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_DEPS = REPO_ROOT / "tools" / "install-deps"


def _load_module():
    import importlib.machinery

    loader = importlib.machinery.SourceFileLoader("install_deps", str(INSTALL_DEPS))
    spec = importlib.util.spec_from_loader("install_deps", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    loader.exec_module(mod)
    return mod


def test_verify_sha256_mismatch(tmp_path: Path) -> None:
    mod = _load_module()
    blob = tmp_path / "blob"
    blob.write_bytes(b"not the real binary")
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        mod.verify_sha256(blob, "0" * 64, "sops")
