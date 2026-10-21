"""R2 / D8: report component roots that exist but lack CONTRACT.md."""

from __future__ import annotations

from pathlib import Path

# §6 contract paths. platform/* is discovered at scan time.
# services/hermes/core and its profile schema return with p2-01 and p2-04.
EXPLICIT_COMPONENT_ROOTS: tuple[str, ...] = (
    "docs/code-delivery",
    "docs/build-agent",
    "schema",
    "services/hermes/deploy/code-factory",
    "services/hermes/deploy/code-factory/sandbox",
    "services/hermes/deploy/ops",
    "services/observability",
)


def component_roots(repo_root: Path | str) -> tuple[str, ...]:
    """All §6 component root paths for this repo tree."""
    root = Path(repo_root).resolve()
    roots: set[str] = set(EXPLICIT_COMPONENT_ROOTS)
    platform_dir = root / "platform"
    if platform_dir.is_dir():
        for child in sorted(platform_dir.iterdir()):
            if child.is_dir():
                roots.add(f"platform/{child.name}")
    return tuple(sorted(roots))


def _component_root_present(rel: str, root: Path, all_roots: tuple[str, ...]) -> bool:
    """True when this §6 root exists on disk and is more than a namespace for a nested root."""
    component_dir = root / rel
    if not component_dir.is_dir():
        return False
    has_nested_root = any(
        other != rel and other.startswith(f"{rel}/") for other in all_roots
    )
    if not has_nested_root:
        return True
    return any(child.is_file() for child in component_dir.iterdir())


def missing_contract_roots(repo_root: Path | str) -> list[str]:
    """Return component root paths (relative) that exist but have no CONTRACT.md."""
    root = Path(repo_root).resolve()
    all_roots = component_roots(root)
    missing: list[str] = []
    for rel in all_roots:
        component_dir = root / rel
        if not _component_root_present(rel, root, all_roots):
            continue
        if not (component_dir / "CONTRACT.md").is_file():
            missing.append(rel)
    return missing


def format_missing(missing: list[str]) -> list[str]:
    return [f"{rel}: missing CONTRACT.md" for rel in missing]
