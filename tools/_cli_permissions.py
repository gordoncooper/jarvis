"""Cursor CLI shell permission matching for .cursor/cli.json (R34).

Semantics mirror cursor-agent's matchesShell / Read(pathOrGlob) (Cursor docs:
commandBase + optional command:args, glob wildcards, deny beats allow).
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

SHELL_ENTRY_RE = re.compile(r"^\s*(Shell|Bash)\s*\((.*)\)\s*$")
READ_ENTRY_RE = re.compile(r"^\s*Read\s*\((.*)\)\s*$")


@dataclass(frozen=True)
class ShellEntry:
    full_pattern: str
    command_pattern: str
    args_pattern: str | None


def parse_shell_entry(token: str) -> ShellEntry | None:
    match = SHELL_ENTRY_RE.match(token.strip())
    if not match:
        return None
    inner = (match.group(2) or "").strip()
    colon = inner.find(":")
    if colon != -1:
        return ShellEntry(
            full_pattern=inner,
            command_pattern=inner[:colon].strip(),
            args_pattern=inner[colon + 1 :].strip(),
        )
    return ShellEntry(
        full_pattern=inner,
        command_pattern=inner,
        args_pattern=None,
    )


def parse_read_entry(token: str) -> str | None:
    match = READ_ENTRY_RE.match(token.strip())
    if not match:
        return None
    return (match.group(1) or "").strip()


def extract_base_command(command_line: str) -> str:
    text = command_line.strip()
    space = text.find(" ")
    return text if space == -1 else text[:space]


def _glob_pattern_variants(pattern: str) -> list[str]:
    pat = pattern.strip()
    variants = [pat]
    if pat.startswith("**/"):
        variants.append(pat[3:])
    return variants


def match_glob(pattern: str, value: str) -> bool:
    pattern = pattern.strip()
    if not value and pattern not in ("**", "**/*"):
        return False
    normalized = value.replace("\\", "/").lstrip("./")
    if "**" in pattern or "/" in pattern:
        for pat in _glob_pattern_variants(pattern):
            if Path(normalized).match(pat) or Path(value).match(pat):
                return True
            if fnmatch.fnmatchcase(normalized, pat) or fnmatch.fnmatchcase(value, pat):
                return True
        return False
    return fnmatch.fnmatchcase(value, pattern)


def matches_shell(entry_token: str, command_line: str) -> bool:
    entry = parse_shell_entry(entry_token)
    if entry is None:
        return False
    line = command_line.strip()
    base = extract_base_command(line)
    if entry.args_pattern is not None:
        args = line[line.index(" ") + 1 :].strip() if " " in line else ""
        cmd_hit = match_glob(entry.command_pattern, base) or match_glob(
            entry.command_pattern, line
        )
        if cmd_hit and match_glob(entry.args_pattern, args):
            return True
    return (
        match_glob(entry.full_pattern, line)
        or line.startswith(f"{entry.full_pattern} ")
        or match_glob(entry.full_pattern, base)
        or base == entry.full_pattern
    )


def matches_read(entry_token: str, file_path: str) -> bool:
    pattern = parse_read_entry(entry_token)
    if pattern is None:
        return False
    normalized = file_path.replace("\\", "/").lstrip("./")
    return match_glob(pattern, normalized) or match_glob(pattern, file_path)


def load_cli_permissions(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("cli.json root must be an object")
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        raise ValueError("cli.json must include a permissions object")
    allow = permissions.get("allow", [])
    deny = permissions.get("deny", [])
    if not isinstance(allow, list) or not isinstance(deny, list):
        raise ValueError("permissions.allow and permissions.deny must be arrays")
    return {"allow": [str(x) for x in allow], "deny": [str(x) for x in deny]}


def command_blocked_by_deny(deny: list[str], command_line: str) -> bool:
    return any(
        matches_shell(token, command_line) for token in deny if parse_shell_entry(token)
    )


def read_blocked_by_deny(deny: list[str], file_path: str) -> bool:
    return any(
        matches_read(token, file_path) for token in deny if parse_read_entry(token)
    )


def command_allowed(
    allow: list[str], deny: list[str], command_line: str
) -> bool:
    if command_blocked_by_deny(deny, command_line):
        return False
    shell_allow = [t for t in allow if parse_shell_entry(t)]
    if not shell_allow:
        return True
    return any(matches_shell(token, command_line) for token in shell_allow)


def read_allowed(allow: list[str], deny: list[str], file_path: str) -> bool:
    if read_blocked_by_deny(deny, file_path):
        return False
    read_allow = [t for t in allow if parse_read_entry(t)]
    if not read_allow:
        return True
    return any(matches_read(token, file_path) for token in read_allow)


DRY_RUN_MUST_BLOCK: tuple[str, ...] = (
    "kubectl apply -f deployment.yaml",
    "helm upgrade --install demo ./chart",
    "ssh user@node.example",
    "sops decrypt envs/<env>/secrets/grafana-admin.sops.yaml",
    "sops -d envs/<env>/secrets/grafana-admin.sops.yaml",
    "flux bootstrap github",
    "flux install",
    "flux suspend kustomization demo",
)

DRY_RUN_MUST_BLOCK_READS: tuple[str, ...] = (
    ".config/sops/age/alphacut.txt",
)

DRY_RUN_MUST_ALLOW: tuple[str, ...] = (
    "kubectl get pods -A",
    "kubectl describe pod demo -n default",
    "kubectl logs deploy/demo -n default --tail=20",
    "flux get kustomizations -A",
    "flux get sources git -A",
    "flux logs kustomization demo",
    "flux tree kustomization demo",
    "flux events --for Kustomization/demo",
    "flux reconcile kustomization demo --with-source",
)


def run_dry_run(permissions_path: Path) -> tuple[list[str], list[str]]:
    perms = load_cli_permissions(permissions_path)
    allow, deny = perms["allow"], perms["deny"]
    failures: list[str] = []
    for cmd in DRY_RUN_MUST_BLOCK:
        if command_allowed(allow, deny, cmd):
            failures.append(f"expected BLOCK, got ALLOW: {cmd}")
    for path in DRY_RUN_MUST_BLOCK_READS:
        if read_allowed(allow, deny, path):
            failures.append(f"expected Read BLOCK, got ALLOW: {path}")
    for cmd in DRY_RUN_MUST_ALLOW:
        if not command_allowed(allow, deny, cmd):
            failures.append(f"expected ALLOW, got BLOCK: {cmd}")
    return failures, list(deny)


def validate_deny_coverage(deny: list[str]) -> list[str]:
    """Ensure required families appear in the deny list."""
    required_substrings = (
        "kubectl",
        "helm",
        "ssh",
        "scp",
        "sops",
        "flux",
    )
    joined = "\n".join(deny).lower()
    missing = [s for s in required_substrings if s not in joined]
    if missing:
        return [f"permissions.deny missing coverage for: {', '.join(missing)}"]
    read_denies = [t for t in deny if t.strip().startswith("Read(")]
    if not read_denies:
        return ["permissions.deny should include Read(...) rules for the age key file"]
    if not any("sops/age" in t for t in read_denies):
        return ["permissions.deny should include Read(**/.config/sops/age/**)"]
    return []
