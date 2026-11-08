"""contract-md CI check: every component root exists and has CONTRACT.md."""

from __future__ import annotations

from pathlib import Path

from jarvis.checks.contract_md import EXPLICIT_COMPONENT_ROOTS, format_missing, missing_contract_roots


def _listed_roots(tmp_path: Path) -> None:
    """Every explicitly listed root, each with its CONTRACT.md."""
    for rel in EXPLICIT_COMPONENT_ROOTS:
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "CONTRACT.md").write_text("# CONTRACT\n", encoding="utf-8")


def test_component_root_with_contract_passes(tmp_path: Path):
    _listed_roots(tmp_path)
    root = tmp_path / "core" / "platform" / "flux-operator"
    root.mkdir(parents=True)
    (root / "CONTRACT.md").write_text("# CONTRACT: flux-operator\n", encoding="utf-8")

    assert missing_contract_roots(tmp_path) == []


def test_component_root_without_contract_fails(tmp_path: Path):
    _listed_roots(tmp_path)
    (tmp_path / "core" / "platform" / "flux-operator").mkdir(parents=True)

    assert missing_contract_roots(tmp_path) == ["core/platform/flux-operator"]


def test_non_component_directory_ignored(tmp_path: Path):
    """Folders inside an add-on are its own; only the add-on root needs the contract."""
    _listed_roots(tmp_path)
    (tmp_path / "addons" / "widget" / "app" / "skills" / "geo").mkdir(parents=True)
    for rel in ("addons", "addons/widget"):
        (tmp_path / rel / "CONTRACT.md").write_text("# CONTRACT\n", encoding="utf-8")

    assert missing_contract_roots(tmp_path) == []


def test_addon_and_slot_without_contract_fail(tmp_path: Path):
    """Every add-on root, every slot, and the add-on contract itself have CONTRACT.md."""
    _listed_roots(tmp_path)
    (tmp_path / "addons" / "widget").mkdir(parents=True)
    (tmp_path / "addons" / "slots" / "chat-bot").mkdir(parents=True)

    assert missing_contract_roots(tmp_path) == ["addons", "addons/slots/chat-bot", "addons/widget"]


def test_undiscovered_platform_child_without_contract_fails(tmp_path: Path):
    _listed_roots(tmp_path)
    novel = tmp_path / "core" / "platform" / "future-widget"
    novel.mkdir(parents=True)
    (novel / "placeholder.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")

    assert missing_contract_roots(tmp_path) == ["core/platform/future-widget"]


def test_listed_root_that_does_not_exist_fails(tmp_path: Path):
    """A listed root with no folder is a failure, not a skip."""
    _listed_roots(tmp_path)
    gone = EXPLICIT_COMPONENT_ROOTS[0]
    (tmp_path / gone / "CONTRACT.md").unlink()
    (tmp_path / gone).rmdir()

    missing = missing_contract_roots(tmp_path)
    assert missing == [gone]
    assert format_missing(missing) == [
        f"{gone}: missing CONTRACT.md (the component root is absent or has none)"
    ]
