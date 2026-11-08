"""Report component roots that lack CONTRACT.md, including roots that do not exist."""

from __future__ import annotations

from pathlib import Path

from jarvis.schema.env import ADDONS_DIR, SLOTS_DIR

# Contract paths that must exist. platform/*, the add-on contract at services/, each
# add-on services/<name>/ and each slot services/slots/<slot>/ are discovered at scan time.
# A root joins this list in the PR that creates it, and is removed when it is deleted.
EXPLICIT_COMPONENT_ROOTS: tuple[str, ...] = (
    "docs/code-delivery",
    "platform/optional",
    "platform/optional/app-hosting",
    "schema",
)


def component_roots(repo_root: Path | str) -> tuple[str, ...]:
    """All component root paths for this repo tree."""
    root = Path(repo_root).resolve()
    roots: set[str] = set(EXPLICIT_COMPONENT_ROOTS)
    platform_dir = root / "platform"
    if platform_dir.is_dir():
        for child in sorted(platform_dir.iterdir()):
            if child.is_dir():
                roots.add(f"platform/{child.name}")
    services_dir = root / ADDONS_DIR
    if services_dir.is_dir():
        roots.add(ADDONS_DIR)
        for child in sorted(services_dir.iterdir()):
            if child.is_dir() and child.name != SLOTS_DIR:
                roots.add(f"{ADDONS_DIR}/{child.name}")
        slots_dir = services_dir / SLOTS_DIR
        if slots_dir.is_dir():
            for child in sorted(slots_dir.iterdir()):
                if child.is_dir():
                    roots.add(f"{ADDONS_DIR}/{SLOTS_DIR}/{child.name}")
    return tuple(sorted(roots))


def missing_contract_roots(repo_root: Path | str) -> list[str]:
    """Return component root paths (relative) with no CONTRACT.md. A listed root that is absent counts."""
    root = Path(repo_root).resolve()
    return [
        rel
        for rel in component_roots(root)
        if not (root / rel / "CONTRACT.md").is_file()
    ]


def format_missing(missing: list[str]) -> list[str]:
    return [f"{rel}: missing CONTRACT.md (the component root is absent or has none)" for rel in missing]
