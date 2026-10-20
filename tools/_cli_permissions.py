"""Cursor CLI shell permission matching for .cursor/cli.json (R34).

Semantics mirror cursor-agent's matchesShell (Cursor docs: commandBase + optional
command:args, glob wildcards, deny beats allow).
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
SHELL_ENTRY_RE = re.compile(r"^\s*(Shell|Bash)\s*\((.*)\)\s*$")


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


def extract_base_command(command_line: str) -> str:
    text = command_line.strip()
    space = text.find(" ")
    return text if space == -1 else text[:space]


def match_glob(pattern: str, value: str) -> bool:
    pattern = pattern.strip()
    if not value and pattern not in ("**", "**/*"):
        return False
    if "**" in pattern or "/" in pattern:
        return Path(value).match(pattern) or fnmatch.fnmatchcase(value, pattern)
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
    return any(matches_shell(token, command_line) for token in deny if parse_shell_entry(token))


def command_allowed(
    allow: list[str], deny: list[str], command_line: str
) -> bool:
    if command_blocked_by_deny(deny, command_line):
        return False
    shell_allow = [t for t in allow if parse_shell_entry(t)]
    if not shell_allow:
        return True
    return any(matches_shell(token, command_line) for token in shell_allow)


# Commands the dry-run must block (done check for p0a-10).
DRY_RUN_MUST_BLOCK: tuple[str, ...] = (
    "kubectl apply -f deployment.yaml",
    "helm upgrade --install demo ./chart",
    "ssh user@bench.lan",
    "sops decrypt envs/<env>/secrets/grafana-admin.sops.yaml",
    "sops -d envs/<env>/secrets/grafana-admin.sops.yaml",
)

# Representative reads that make verify uses; must stay allowed.
DRY_RUN_MUST_ALLOW: tuple[str, ...] = (
    "kubectl get pods -A",
    "kubectl describe pod demo -n default",
    "kubectl logs deploy/demo -n default --tail=20",
    "flux get kustomizations -A",
    "flux get sources git -A",
)


def run_dry_run(permissions_path: Path) -> tuple[list[str], list[str]]:
    perms = load_cli_permissions(permissions_path)
    allow, deny = perms["allow"], perms["deny"]
    failures: list[str] = []
    for cmd in DRY_RUN_MUST_BLOCK:
        if command_allowed(allow, deny, cmd):
            failures.append(f"expected BLOCK, got ALLOW: {cmd}")
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
        "age",
    )
    joined = "\n".join(deny).lower()
    missing = [s for s in required_substrings if s not in joined]
    if missing:
        return [f"permissions.deny missing coverage for: {', '.join(missing)}"]
    read_denies = [t for t in deny if t.strip().startswith("Read(")]
    if not read_denies:
        return ["permissions.deny should include Read(...) rules for the age key file"]
    return []
