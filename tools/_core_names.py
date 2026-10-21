"""Identity scan for core (D44).

Sources are non-example envs plus OWNER_NAME. Repo slugs are not sources:
a repo URL outside envs/<env>/ already contains the owner handle, and the
handle match is a whole token, so the URL fails with the handle. Bare repo
names stay allowed.

Product rules (the word bastion, and any /home/ path) are not applied to
this file or to schema/CONTRACT.md. Those two paths
are the one exemption: both have to spell the tokens. Identity values are
still checked there.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from schema.env import is_example_env, repo_owner_handle, validate_env_tree

# One exemption, two paths: files that state the product-rule tokens.
_PRODUCT_RULE_EXEMPT = frozenset(
    {
        "tools/_core_names.py",
        "schema/CONTRACT.md",
    }
)

_SCAN_ROOTS = (
    "infra/",
    "platform/",
    "schema/",
    "services/",
    "tools/",
    "docs/",
    "rulebooks/",
    ".github/",
)

_README_NAMES = frozenset({"README", "README.md"})

# Token characters. Dot and slash are boundaries, so a DNS suffix still
# matches inside a longer hostname, and a handle still matches inside a URL.
_TOKEN = r"A-Za-z0-9_\-"

_PRODUCT_WORD = "bastion"
_HOME_PREFIX = "/home/"
_PRODUCT_WORD_RE = re.compile(
    rf"(?<![{_TOKEN}]){_PRODUCT_WORD}(?![{_TOKEN}])",
    re.IGNORECASE,
)


class OwnerNameUnset(Exception):
    """OWNER_NAME is missing or blank. The message never carries a value."""

    def __init__(self) -> None:
        super().__init__("OWNER_NAME is unset")


@dataclass(frozen=True)
class CoreNameViolation:
    path: str
    line: int
    kind: str
    value: str


@dataclass(frozen=True)
class ForbiddenNames:
    """value -> env names that own it. Env names use the same map keyed by themselves."""

    env_names: dict[str, frozenset[str]]
    dns_suffixes: dict[str, frozenset[str]]
    owner_handles: dict[str, frozenset[str]]
    inventory_hostnames: dict[str, frozenset[str]]
    ssh_users: dict[str, frozenset[str]]


def _require_owner_name() -> str:
    raw = os.environ.get("OWNER_NAME")
    if raw is None or not raw.strip():
        raise OwnerNameUnset()
    return raw.strip()


def _add(bucket: dict[str, set[str]], env_name: str, value: str) -> None:
    text = value.strip()
    if not text:
        return
    bucket.setdefault(text, set()).add(env_name)


def _freeze(bucket: dict[str, set[str]]) -> dict[str, frozenset[str]]:
    return {value: frozenset(envs) for value, envs in bucket.items()}


def collect_forbidden_names(repo_root: Path | str) -> ForbiddenNames:
    """Identity values from every env except the example env."""
    root = Path(repo_root).resolve()
    resolved = validate_env_tree(root)

    env_names: dict[str, set[str]] = {}
    dns_suffixes: dict[str, set[str]] = {}
    owner_handles: dict[str, set[str]] = {}
    inventory_hostnames: dict[str, set[str]] = {}
    ssh_users: dict[str, set[str]] = {}

    for env_name, cluster_vars in resolved.items():
        if is_example_env(env_name):
            continue
        _add(env_names, env_name, env_name)
        dns = cluster_vars.get("dns_suffix")
        if isinstance(dns, str):
            _add(dns_suffixes, env_name, dns)
        user = cluster_vars.get("ssh_user")
        if isinstance(user, str):
            _add(ssh_users, env_name, user)
        for key in ("flux_git_repo_url", "apps_git_repo_url"):
            url = cluster_vars.get(key)
            if isinstance(url, str):
                owner = repo_owner_handle(url)
                if owner:
                    _add(owner_handles, env_name, owner)
        for hostname in _inventory_hostnames(
            root / "envs" / env_name / "inventory" / "hosts.yml"
        ):
            _add(inventory_hostnames, env_name, hostname)

    return ForbiddenNames(
        env_names=_freeze(env_names),
        dns_suffixes=_freeze(dns_suffixes),
        owner_handles=_freeze(owner_handles),
        inventory_hostnames=_freeze(inventory_hostnames),
        ssh_users=_freeze(ssh_users),
    )


def scan_core_names(repo_root: Path | str) -> list[CoreNameViolation]:
    """Scan the D44 path set. Fails before scanning when OWNER_NAME is unset."""
    owner_name = _require_owner_name()
    root = Path(repo_root).resolve()
    forbidden = collect_forbidden_names(root)
    violations: list[CoreNameViolation] = []

    for rel_path in _git_tracked_files(root):
        if not _is_scanned(rel_path):
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
                _line_violations(rel_path, line_no, line, forbidden, owner_name)
            )

    return sorted(violations, key=lambda v: (v.path, v.line, v.kind, v.value))


def format_violations(violations: Iterable[CoreNameViolation]) -> list[str]:
    lines: list[str] = []
    for violation in violations:
        if violation.kind == "owner_name":
            lines.append(f"{violation.path}:{violation.line}: forbidden owner_name")
        else:
            lines.append(
                f"{violation.path}:{violation.line}: forbidden {violation.kind} '{violation.value}'"
            )
    return lines


def _is_scanned(rel_path: str) -> bool:
    if rel_path == ".github/CODEOWNERS":
        return False
    name = rel_path.rsplit("/", 1)[-1]
    if name in _README_NAMES:
        return True
    return rel_path.startswith(_SCAN_ROOTS)


def _git_tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        text=True,
    )
    return [part for part in out.split("\0") if part]


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


def _token_re(value: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{_TOKEN}]){re.escape(value)}(?![{_TOKEN}])",
        re.IGNORECASE,
    )


def _env_name_allowed(rel_path: str) -> bool:
    return rel_path.startswith("envs/") or rel_path.startswith("docs/")


def _owned_value_allowed(rel_path: str, envs: frozenset[str]) -> bool:
    for env_name in envs:
        prefix = f"envs/{env_name}/"
        if rel_path.startswith(prefix):
            return True
    return False


def _longest(values: dict[str, frozenset[str]]) -> list[tuple[str, frozenset[str]]]:
    return sorted(values.items(), key=lambda item: len(item[0]), reverse=True)


def _match_token(
    rel_path: str,
    line_no: int,
    line: str,
    values: dict[str, frozenset[str]],
    kind: str,
    found: list[CoreNameViolation],
    *,
    allowed,
) -> None:
    for value, envs in _longest(values):
        if allowed(rel_path, envs):
            continue
        if _token_re(value).search(line):
            found.append(CoreNameViolation(rel_path, line_no, kind, value))
            return


def _ssh_hits(
    rel_path: str,
    line_no: int,
    line: str,
    users: dict[str, frozenset[str]],
    found: list[CoreNameViolation],
) -> None:
    for user, envs in _longest(users):
        if _owned_value_allowed(rel_path, envs):
            continue
        user_re = re.escape(user)
        flags = re.IGNORECASE
        hit = False
        if re.search(rf"/home/{user_re}(?![{_TOKEN}])", line, flags):
            found.append(
                CoreNameViolation(rel_path, line_no, "ssh_user_home", f"/home/{user}")
            )
            hit = True
        if re.search(
            rf"(?<![{_TOKEN}]){user_re}@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?",
            line,
            flags,
        ):
            found.append(CoreNameViolation(rel_path, line_no, "ssh_user_at", f"{user}@"))
            hit = True
        if re.search(rf"~{user_re}(?![{_TOKEN}])", line, flags):
            found.append(
                CoreNameViolation(rel_path, line_no, "ssh_user_tilde", f"~{user}")
            )
            hit = True
        if hit:
            return


def _line_violations(
    rel_path: str,
    line_no: int,
    line: str,
    forbidden: ForbiddenNames,
    owner_name: str,
) -> list[CoreNameViolation]:
    found: list[CoreNameViolation] = []
    if not _env_name_allowed(rel_path):
        _match_token(
            rel_path,
            line_no,
            line,
            forbidden.env_names,
            "env_name",
            found,
            allowed=lambda _path, _envs: False,
        )
    _match_token(
        rel_path,
        line_no,
        line,
        forbidden.dns_suffixes,
        "dns_suffix",
        found,
        allowed=_owned_value_allowed,
    )
    _match_token(
        rel_path,
        line_no,
        line,
        forbidden.owner_handles,
        "owner_handle",
        found,
        allowed=_owned_value_allowed,
    )
    _match_token(
        rel_path,
        line_no,
        line,
        forbidden.inventory_hostnames,
        "inventory_hostname",
        found,
        allowed=_owned_value_allowed,
    )
    _ssh_hits(rel_path, line_no, line, forbidden.ssh_users, found)
    if rel_path not in _PRODUCT_RULE_EXEMPT:
        if _HOME_PREFIX in line:
            found.append(CoreNameViolation(rel_path, line_no, "home_path", _HOME_PREFIX))
        if _PRODUCT_WORD_RE.search(line):
            found.append(
                CoreNameViolation(rel_path, line_no, "bastion", _PRODUCT_WORD)
            )
    if _token_re(owner_name).search(line):
        found.append(CoreNameViolation(rel_path, line_no, "owner_name", owner_name))
    return found
