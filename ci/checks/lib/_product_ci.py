"""Product trees must not import, execute, source, or read ci/.

The only exception: tools/verify may read ci/rule-checks.txt.
That read is not an import, an execute, or a source.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# tools/verify may read this file. Nothing else under ci/ is allowed.
ALLOWED_READ_PREFIX = "tools/verify/"
ALLOWED_READ_PATH = "ci/rule-checks.txt"

_PRODUCT_ROOTS = (
    "tools/",
    "infra/",
    "platform/",
    "services/",
    "apps/",
    "envs/",
    "schema/",
)

_INLINE_CODE = re.compile(r"`[^`\n]*`")
_IMPORT = re.compile(r"(?:^|\s)(?:import\s+ci\b|from\s+ci\b)")
_EXEC = re.compile(
    r"(?:^|[\s;|&])(?:python3?|bash|sh|exec|source|\.)\s+['\"]?ci/\S+"
)
# ci/ as its own path. A preceding word character (pci/) does not match.
_CI_PATH = re.compile(r"(?<![A-Za-z0-9_])ci/[A-Za-z0-9_./-]*")


@dataclass(frozen=True)
class ProductCiViolation:
    path: str
    line: int
    reference: str


def scan_product_ci(repo_root: Path | str) -> list[ProductCiViolation]:
    """Scan product trees. Fails on an import, execute, source, or read of ci/."""
    root = Path(repo_root).resolve()
    violations: list[ProductCiViolation] = []
    for rel_path in _git_tracked_files(root):
        if not rel_path.startswith(_PRODUCT_ROOTS):
            continue
        abs_path = root / rel_path
        if not abs_path.is_file():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            violations.extend(_line_violations(rel_path, line_no, line))
    return sorted(violations, key=lambda item: (item.path, item.line, item.reference))


def format_violations(violations: list[ProductCiViolation]) -> list[str]:
    return [
        f"{item.path}:{item.line}: product must not reference {item.reference}"
        for item in violations
    ]


def _line_violations(rel_path: str, line_no: int, line: str) -> list[ProductCiViolation]:
    text = _INLINE_CODE.sub(" ", line) if rel_path.endswith(".md") else line
    found: list[str] = []
    imported = _IMPORT.search(text) is not None
    executed = _EXEC.search(text) is not None
    if imported:
        found.append("import ci")
    for match in _CI_PATH.finditer(text):
        ref = match.group(0)
        if (
            not imported
            and not executed
            and rel_path.startswith(ALLOWED_READ_PREFIX)
            and ref == ALLOWED_READ_PATH
        ):
            continue
        found.append(ref)
    unique: list[str] = []
    for ref in found:
        if ref not in unique:
            unique.append(ref)
    return [ProductCiViolation(rel_path, line_no, ref) for ref in unique]


def _git_tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        text=True,
    )
    return [part for part in out.split("\0") if part]
