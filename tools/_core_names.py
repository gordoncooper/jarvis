"""Detect env-specific names outside envs/ (R3, R28). CI rule check helper."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import yaml

from services.hermes.core.schema.env import ValidationError, validate_env_tree

# Relative path prefix -> exemption reason (documented in PR / open questions).
_EXEMPT_PREFIXES: tuple[tuple[str, str], ...] = (
    (
        "docs/",
        "Planning docs may link to the external project-plans repo (not cluster manifests).",
    ),
    (
        "tools/tests/fixtures/",
        "Committed test fixtures use neutral or third-party placeholders only.",
    ),
    (
        "tools/_core_names.py",
        "Implements the checker; literals name rule kinds and patterns, not env data.",
    ),
    (
        "tools/tests/test_core_names.py",
        "Pytest seeds forbidden strings in temp repos; source lines name rule kinds under test.",
    ),
)

_BASTION_RE = re.compile(r"bastion", re.IGNORECASE)


@dataclass(frozen=True)
class ForbiddenNames:
    env_names: frozenset[str]
    dns_suffixes: frozenset[str]
    model_names: frozenset[str]
    flux_git_repo_urls: frozenset[str]
    github_owner_segments: frozenset[str]
    inventory_hostnames: frozenset[str]
    ssh_users: frozenset[str]


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
    flux_git_repo_urls: set[str] = set()
    github_owner_segments: set[str] = set()
    inventory_hostnames: set[str] = set()
    ssh_users: set[str] = set()

    for env_name, cluster_vars in resolved.items():
        dns_suffixes.add(cluster_vars["dns_suffix"])
        ssh_users.add(cluster_vars["ssh_user"])

        repo_url = cluster_vars["flux_git_repo_url"]
        if isinstance(repo_url, str) and repo_url.strip():
            flux_git_repo_urls.add(repo_url.strip())
            owner = _github_owner_from_https_url(repo_url)
            if owner:
                github_owner_segments.add(f"github.com/{owner}/")

        inventory_hostnames.update(
            _inventory_hostnames(root / "envs" / env_name / "inventory" / "hosts.yml"),
        )

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
        flux_git_repo_urls=frozenset(flux_git_repo_urls),
        github_owner_segments=frozenset(github_owner_segments),
        inventory_hostnames=frozenset(inventory_hostnames),
        ssh_users=frozenset(ssh_users),
    )


def exemption_reason(rel_path: str) -> str | None:
    if rel_path == "README.md":
        return "Root README may link to the external project-plans repo."
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


def _github_owner_from_https_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        return None
    host = parsed.netloc.lower()
    if host not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    return parts[0] if parts else None


def _inventory_hostnames(hosts_path: Path) -> set[str]:
    if not hosts_path.is_file():
        return set()
    with hosts_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return set()
    all_group = data.get("all")
    if not isinstance(all_group, dict):
        return set()
    hosts = all_group.get("hosts")
    if not isinstance(hosts, dict):
        return set()
    return {name for name in hosts if isinstance(name, str) and name.strip()}


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
    for value in sorted(forbidden.flux_git_repo_urls, key=len, reverse=True):
        if value and value in line:
            found.append(
                CoreNameViolation(rel_path, line_no, "flux_git_repo_url", value),
            )
            break
    for value in sorted(forbidden.github_owner_segments, key=len, reverse=True):
        if value and value in line:
            found.append(
                CoreNameViolation(rel_path, line_no, "github_owner", value),
            )
            break
    for value in sorted(forbidden.inventory_hostnames, key=len, reverse=True):
        if value and value in line:
            found.append(
                CoreNameViolation(rel_path, line_no, "inventory_hostname", value),
            )
            break
    for user in sorted(forbidden.ssh_users, key=len, reverse=True):
        if not user:
            continue
        home = f"/home/{user}"
        if home in line:
            found.append(CoreNameViolation(rel_path, line_no, "ssh_user_home", home))
            break
        at = f"{user}@"
        if at in line:
            found.append(CoreNameViolation(rel_path, line_no, "ssh_user_at", at))
            break
    if "/home/" in line:
        found.append(CoreNameViolation(rel_path, line_no, "home_path", "/home/"))
    if _BASTION_RE.search(line):
        found.append(CoreNameViolation(rel_path, line_no, "bastion", "bastion"))
    return found


def format_violations(violations: Iterable[CoreNameViolation]) -> list[str]:
    return [
        f"{v.path}:{v.line}: forbidden {v.kind} '{v.value}'"
        for v in violations
    ]
