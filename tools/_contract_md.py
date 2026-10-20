"""R2 / D8: report component roots that exist but lack CONTRACT.md."""

from __future__ import annotations

from pathlib import Path

# §6 layout folders assigned CONTRACT.md (D7 paths included; nested only where D7 names them).
COMPONENT_ROOTS: tuple[str, ...] = (
    "docs/code-delivery",
    "docs/build-agent",
    "platform/app-hosting",
    "platform/backups",
    "platform/cert-manager",
    "platform/flux-operator",
    "platform/lan-dns",
    "platform/traefik",
    "services/hermes/core",
    "services/hermes/core/schema",
    "services/hermes/deploy/code-factory",
    "services/hermes/deploy/code-factory/sandbox",
    "services/observability",
)


def _component_root_present(rel: str, root: Path) -> bool:
    """True when this §6 root exists on disk and is more than a namespace for a nested root."""
    component_dir = root / rel
    if not component_dir.is_dir():
        return False
    has_nested_root = any(
        other != rel and other.startswith(f"{rel}/") for other in COMPONENT_ROOTS
    )
    if not has_nested_root:
        return True
    return any(child.is_file() for child in component_dir.iterdir())


def missing_contract_roots(repo_root: Path | str) -> list[str]:
    """Return component root paths (relative) that exist but have no CONTRACT.md."""
    root = Path(repo_root).resolve()
    missing: list[str] = []
    for rel in COMPONENT_ROOTS:
        component_dir = root / rel
        if not _component_root_present(rel, root):
            continue
        if not (component_dir / "CONTRACT.md").is_file():
            missing.append(rel)
    return missing


def format_missing(missing: list[str]) -> list[str]:
    return [f"{rel}: missing CONTRACT.md" for rel in missing]
