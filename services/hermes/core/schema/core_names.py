"""Detect env-specific names outside envs/ (R3, R28)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from services.hermes.core.schema.env import ValidationError, validate_env_tree

# Relative path prefix -> exemption reason (documented in PR / open questions).
_EXEMPT_PREFIXES: tuple[tuple[str, str], ...] = (
    (
        "docs/",
        "Docs describe contracts and may name the illustrative env; not cluster manifests.",
    ),
)

_CONTRACT_SUFFIX = "CONTRACT.md"


@dataclass(frozen=True)
class ForbiddenNames:
    env_names: frozenset[str]
    dns_suffixes: frozenset[str]
    model_names: frozenset[str]


@dataclass(frozen=True)
class CoreNameViolation:
    path: str
    line: int
    kind: str
    value: str


def sole_env_directory_name(repo_root: Path | str) -> str:
    """Return the only env directory name, or raise if ambiguous."""
    root = Path(repo_root).resolve()
    names = sorted(p.name for p in (root / "envs").iterdir() if p.is_dir())
    if len(names) != 1:
        raise SystemExit(
            "expected exactly one directory under envs/; pass --env explicitly"
        )
    return names[0]


def collect_forbidden_names(repo_root: Path | str) -> ForbiddenNames:
    """Derive forbidden strings from envs/*/ (after env-schema validation)."""
    root = Path(repo_root).resolve()
    resolved = validate_env_tree(root)

    env_names: set[str] = set(resolved)
    dns_suffixes: set[str] = set()
    model_names: set[str] = set()

    for env_name, cluster_vars in resolved.items():
        dns_suffixes.add(cluster_vars["dns_suffix"])
        hermes_path = root / "envs" / env_name / "values" / "hermes.yaml"
        with hermes_path.open(encoding="utf-8") as fh:
            hermes = yaml.safe_load(fh) or {}
        if not isinstance(hermes, dict):
            raise ValidationError([f"{hermes_path}: expected a YAML mapping"])
        model_names.update(_model_strings(hermes.get("tier_model_map")))
        model_names.update(_model_strings(hermes.get("lane_model_map")))

    return ForbiddenNames(
        env_names=frozenset(env_names),
        dns_suffixes=frozenset(dns_suffixes),
        model_names=frozenset(model_names),
    )


def exemption_reason(rel_path: str) -> str | None:
    if rel_path.endswith(_CONTRACT_SUFFIX):
        return "CONTRACT.md documents reference values for the illustrative env."
    for prefix, reason in _EXEMPT_PREFIXES:
        if rel_path.startswith(prefix):
            return reason
    return None


def scan_core_names(repo_root: Path | str) -> list[CoreNameViolation]:
    """Scan git-tracked files outside envs/ for forbidden env, DNS, or model strings."""
    root = Path(repo_root).resolve()
    forbidden = collect_forbidden_names(root)
    violations: list[CoreNameViolation] = []

    for rel_path in _git_tracked_files(root):
        if rel_path.startswith("envs/"):
            continue
        if exemption_reason(rel_path) is not None:
            continue
        abs_path = root / rel_path
        if not abs_path.is_file():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            violations.extend(
                _line_violations(rel_path, line_no, line, forbidden),
            )

    return sorted(violations, key=lambda v: (v.path, v.line, v.kind, v.value))


def _git_tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        text=True,
    )
    return [part for part in out.split("\0") if part]


def _model_strings(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            names.add(stripped)
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in ("model", "name") and isinstance(child, str) and child.strip():
                names.add(child.strip())
            names.update(_model_strings(child))
    elif isinstance(value, list):
        for item in value:
            names.update(_model_strings(item))
    return names


def _line_violations(
    rel_path: str,
    line_no: int,
    line: str,
    forbidden: ForbiddenNames,
) -> list[CoreNameViolation]:
    found: list[CoreNameViolation] = []
    for value in sorted(forbidden.dns_suffixes, key=len, reverse=True):
        if value and value in line:
            found.append(
                CoreNameViolation(rel_path, line_no, "dns_suffix", value),
            )
            break
    for value in sorted(forbidden.env_names, key=len, reverse=True):
        if value and value in line:
            found.append(CoreNameViolation(rel_path, line_no, "env_name", value))
            break
    for value in sorted(forbidden.model_names, key=len, reverse=True):
        if value and value in line:
            found.append(
                CoreNameViolation(rel_path, line_no, "model_name", value),
            )
            break
    return found


def format_violations(violations: Iterable[CoreNameViolation]) -> list[str]:
    return [
        f"{v.path}:{v.line}: forbidden {v.kind} '{v.value}'"
        for v in violations
    ]
