"""Product docs and CONTRACTs contain no plan codes.

Scanned paths: every Markdown file, every README* (any case, any extension),
and everything under docs/ (including docs/CONTRACT.template.md). That picks
up CONTRACT.md, RUNBOOK.md, readme.md, and README.txt, including under envs/.

Not scanned: rulebooks/, AGENTS.md, .github/, and code. No path exemptions.
The pattern is case-sensitive. A phase id is P0-P4 with an optional a/b
suffix of either case (P0a, P0A). A bare lowercase p2, or a lowercase r or d
plus digits, is not a plan code. Version strings and ordinary words are not.

Unmatched on purpose: F<n> and B<n>. B2 is a storage product name.
Known collisions that still match: P4, R2, and D3.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Phase names (P0-P4, optional a/b of either case: P2, P0a, P0A, P4b, P4B),
# step ids (p0a-03, p0b-10, p2-24, p2-24b, p4a-04), p0a/p0b with no step
# number, rule ids (R31, R4a), decision ids, open questions (OQ7, OQ-7),
# audit findings (J16), and §<n> with an optional space (§6, § 5).
# A step id may end with one letter (p2-24b). A longer word after the digits
# does not match. P100, P40, and P256 are not phases.
_PLAN_CODE = re.compile(
    r"\bP[0-4][abAB]?\b"
    r"|\bp[0-4][a-z]?-\d+[a-z]?\b"
    r"|\bp0[abAB]\b"
    r"|\bR\d+[a-zA-Z]?\b"
    r"|\bD\d+\b"
    r"|\bOQ-?\d+\b"
    r"|\bJ\d+\b"
    r"|§\s*\d+"
)

_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules"})
_EXCLUDED_PREFIXES = ("rulebooks/", ".github/")


def _scanned(rel: str) -> bool:
    if rel == "AGENTS.md" or rel.startswith(_EXCLUDED_PREFIXES):
        return False
    name = rel.rsplit("/", 1)[-1]
    lowered = name.lower()
    if lowered.startswith("readme") or lowered.endswith(".md"):
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
