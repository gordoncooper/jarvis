"""Pytest defaults for this repository's CI suite."""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_APPS = _REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"

if "APPS_REPO" not in os.environ and _FIXTURE_APPS.is_dir():
    os.environ["APPS_REPO"] = str(_FIXTURE_APPS)
