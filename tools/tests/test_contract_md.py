"""contract-md CI check (p0a-08)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from _contract_md import missing_contract_roots

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_MD_CHECK = REPO_ROOT / "tools" / "checks" / "contract-md"


def test_repo_tree_passes_contract_md_check():
    assert missing_contract_roots(REPO_ROOT) == []


def test_contract_md_check_executable_passes_in_repo():
    subprocess.run([str(CONTRACT_MD_CHECK)], cwd=REPO_ROOT, check=True)


def test_component_root_with_contract_passes(tmp_path: Path):
    root = tmp_path / "platform" / "flux-operator"
    root.mkdir(parents=True)
    (root / "CONTRACT.md").write_text("# CONTRACT: flux-operator\n", encoding="utf-8")

    assert missing_contract_roots(tmp_path) == []


def test_component_root_without_contract_fails(tmp_path: Path):
    root = tmp_path / "platform" / "flux-operator"
    root.mkdir(parents=True)

    missing = missing_contract_roots(tmp_path)
    assert missing == ["platform/flux-operator"]


def test_non_component_directory_ignored(tmp_path: Path):
    skills = tmp_path / "services" / "hermes" / "core" / "skills" / "geo"
    skills.mkdir(parents=True)
    assert missing_contract_roots(tmp_path) == []


def test_parent_path_with_only_nested_root_ignored(tmp_path: Path):
    schema = tmp_path / "services" / "hermes" / "core" / "schema"
    schema.mkdir(parents=True)
    (schema / "CONTRACT.md").write_text("# CONTRACT: schema\n", encoding="utf-8")

    assert missing_contract_roots(tmp_path) == []


def test_missing_contract_reported(tmp_path: Path):
    (tmp_path / "platform" / "traefik").mkdir(parents=True)

    missing = missing_contract_roots(tmp_path)
    assert missing == ["platform/traefik"]
