"""Renovate config validator: repo mode and version parity."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import require_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
RENOVATE_JSON = REPO_ROOT / "renovate.json"
VERSIONS_ENV = REPO_ROOT / "tools" / "versions.env"
RENOVATE_PACKAGE_JSON = REPO_ROOT / "tools" / "renovate" / "package.json"


def _load_versions_env() -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in VERSIONS_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        versions[key.strip()] = value.strip()
    return versions


def test_renovate_version_matches_package_json() -> None:
    versions = _load_versions_env()
    package = json.loads(RENOVATE_PACKAGE_JSON.read_text(encoding="utf-8"))
    pkg_version = package["dependencies"]["renovate"]
    assert versions["renovate_version"] == pkg_version


def test_renovate_json_passes_repo_config_validator() -> None:
    validator = require_cli("renovate-config-validator")
    proc = subprocess.run(
        [validator],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "as global config" not in output


def test_repo_config_validator_rejects_global_only_allowed_commands(
    tmp_path: Path,
) -> None:
    validator = require_cli("renovate-config-validator")

    config = json.loads(RENOVATE_JSON.read_text(encoding="utf-8"))
    config["allowedCommands"] = ["x"]
    bad = tmp_path / "renovate.json"
    bad.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    global_mode = subprocess.run(
        [validator, str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert global_mode.returncode == 0, global_mode.stdout + global_mode.stderr

    repo_mode = subprocess.run(
        [validator],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    output = repo_mode.stdout + repo_mode.stderr
    assert repo_mode.returncode != 0, output
    assert re.search(r"allowedCommands", output, re.IGNORECASE)
