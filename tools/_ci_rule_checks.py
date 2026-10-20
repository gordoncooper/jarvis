"""CI rule check names (shared by tools/run-checks and verify-p0a)."""

from __future__ import annotations

from pathlib import Path

CHECKS_FILE = "tools/ci-rule-checks.txt"
PHASE_VERIFY_SCRIPTS_FILE = "tools/phase-verify-scripts.txt"


def ci_rule_checks_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / CHECKS_FILE


def expected_ci_rule_checks(repo_root: Path | str) -> tuple[str, ...]:
    path = ci_rule_checks_path(repo_root)
    lines = path.read_text(encoding="utf-8").splitlines()
    names: list[str] = []
    for line in lines:
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        names.append(name)
    if not names:
        raise ValueError(f"{CHECKS_FILE} is empty")
    return tuple(names)


def phase_verify_scripts_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / PHASE_VERIFY_SCRIPTS_FILE


def phase_verify_scripts(repo_root: Path | str) -> tuple[str, ...]:
    path = phase_verify_scripts_path(repo_root)
    lines = path.read_text(encoding="utf-8").splitlines()
    names: list[str] = []
    for line in lines:
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        names.append(name)
    if not names:
        raise ValueError(f"{PHASE_VERIFY_SCRIPTS_FILE} is empty")
    return tuple(names)
