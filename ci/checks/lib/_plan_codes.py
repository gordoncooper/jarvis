"""Product docs and CONTRACTs contain no plan codes.

Scanned paths: every CONTRACT.md, everything under docs/ (including
docs/CONTRACT.template.md), and every README or README.md, including
those under envs/.

Not scanned: rulebooks/, AGENTS.md, .github/, and code. No path exemptions.
The pattern is case-sensitive. A bare lowercase p2, or a lowercase r or d
plus digits, is not a plan code. Version strings and ordinary words are not.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Phase names (P2, P0a, P4b), step ids (p0a-03, p0b-10, p2-24, p4a-04),
# p0a/p0b with no step number, rule and decision ids, open questions, and §<n>.
_PLAN_CODE = re.compile(
    r"\bP\d+[a-z]?\b"
    r"|\bp\d+[a-z]?-\d+\b"
    r"|\bp0[ab]\b"
    r"|\bR\d+\b"
    r"|\bD\d+\b"
    r"|\bOQ\d+\b"
    r"|§\d+"
)

_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules"})
_EXCLUDED_PREFIXES = ("rulebooks/", ".github/")
_DOC_NAMES = frozenset({"CONTRACT.md", "README", "README.md"})


def _scanned(rel: str) -> bool:
    if rel == "AGENTS.md" or rel.startswith(_EXCLUDED_PREFIXES):
        return False
    name = rel.rsplit("/", 1)[-1]
    if name in _DOC_NAMES:
        return True
    return rel.startswith("docs/")


def scan_plan_codes(repo_root: Path | str) -> list[str]:
    root = Path(repo_root).resolve()
    violations: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRS and not name.startswith(".")
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if not _scanned(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for match in _PLAN_CODE.finditer(line):
                    violations.append(f"{rel}:{number}: {match.group(0)}")
    return violations
