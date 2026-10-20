"""core-names CI check (p0a-07)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from services.hermes.core.schema.core_names import scan_core_names
from services.hermes.core.schema.env import validate_env_tree
from test_env_schema import _write_env

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_NAMES_CHECK = REPO_ROOT / "tools" / "checks" / "core-names"


def test_repo_tree_passes_core_names_check():
    violations = scan_core_names(REPO_ROOT)
    assert violations == []


def test_core_names_check_executable_passes_in_repo():
    subprocess.run([str(CORE_NAMES_CHECK)], cwd=REPO_ROOT, check=True)


def test_hardcoded_dns_suffix_under_platform_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)

    dns_suffix = yaml.safe_load(
        (tmp_path / "envs/env-test/values/cluster-vars.yaml").read_text(encoding="utf-8")
    )["dns_suffix"]

    platform = tmp_path / "platform" / "fixture"
    platform.mkdir(parents=True)
    bad = platform / "bad.txt"
    bad.write_text(f"host: app.{dns_suffix}\n", encoding="utf-8")

    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == "platform/fixture/bad.txt"
    assert violations[0].kind == "dns_suffix"
    assert violations[0].value == dns_suffix


def _init_git_repo(root: Path) -> None:
    subprocess.check_call(["git", "init", "-q"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "test"], cwd=root)


def _git_add_all(root: Path) -> None:
    subprocess.check_call(["git", "add", "-A"], cwd=root)
