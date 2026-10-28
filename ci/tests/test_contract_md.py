"""contract-md CI check (p0a-08)."""

from __future__ import annotations

from pathlib import Path

from _contract_md import missing_contract_roots


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
    sandbox = tmp_path / "services" / "hermes" / "deploy" / "code-factory" / "sandbox"
    sandbox.mkdir(parents=True)
    (sandbox / "CONTRACT.md").write_text("# CONTRACT: sandbox\n", encoding="utf-8")

    assert missing_contract_roots(tmp_path) == []


def test_missing_contract_reported(tmp_path: Path):
    (tmp_path / "platform" / "traefik").mkdir(parents=True)

    missing = missing_contract_roots(tmp_path)
    assert missing == ["platform/traefik"]


def test_undiscovered_platform_child_without_contract_fails(tmp_path: Path):
    novel = tmp_path / "platform" / "future-widget"
    novel.mkdir(parents=True)
    (novel / "placeholder.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")

    missing = missing_contract_roots(tmp_path)
    assert missing == ["platform/future-widget"]


def test_ops_deploy_root_without_contract_fails(tmp_path: Path):
    ops = tmp_path / "services" / "hermes" / "deploy" / "ops"
    ops.mkdir(parents=True)
    (ops / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")

    missing = missing_contract_roots(tmp_path)
    assert missing == ["services/hermes/deploy/ops"]
