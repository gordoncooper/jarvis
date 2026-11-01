"""R33: requirements-dev.txt pins each Python package with ==."""

from __future__ import annotations

import re
from pathlib import Path

_EXACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][0-9A-Za-z.+!]*\Z")
_FILE = "requirements-dev.txt"


def scan_python_pins(repo_root: Path | str) -> list[str]:
    path = Path(repo_root) / _FILE
    if not path.is_file():
        return [f"{_FILE}: missing"]
    violations: list[str] = []
    pins = 0
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _EXACT.fullmatch(line) is None:
            violations.append(f"{_FILE}:{number}: {line} is not an exact pin")
            continue
        pins += 1
    if pins == 0 and not violations:
        violations.append(f"{_FILE}: no exact pins")
    return violations
