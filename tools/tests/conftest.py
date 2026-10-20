"""Test harness: repo root on sys.path for imports from services/."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = _ROOT / "tools"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))


def ci_mode() -> bool:
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def require_cli(name: str, *, hint: str = "run make deps") -> str:
    path = shutil.which(name)
    if path:
        return path
    message = f"{name} not found on PATH ({hint})"
    if ci_mode():
        pytest.fail(f"{message} — required in CI")
    pytest.skip(message)
    raise AssertionError("unreachable")
