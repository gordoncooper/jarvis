"""Cursor CLI shell permission matching for .cursor/cli.json (R34).

Mirrors the matcher in cursor-agent 2026.10.22-27dcf5f. The dry-run proves
the checked-in policy against this corpus. It does not prove the client.

Matcher:
- the line is split into commands at && || ; | & newlines, $( ), backticks
  and subshells, and leading FOO=1 assignments are dropped;
- each command is matched as "name args" (raw word text, quotes kept);
- only * is a wildcard (? is literal), matching is case-sensitive and
  anchored;
- bash -c, eval, sudo, absolute paths and the like are NOT unwrapped, so
  cli.json lists them itself.
Read(pathOrGlob) keeps glob semantics. Deny beats allow. An empty allow
list is the default: a command no deny entry matches is allowed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SHELL_ENTRY_RE = re.compile(r"^\s*(Shell|Bash)\s*\((.*)\)\s*$")
READ_ENTRY_RE = re.compile(r"^\s*Read\s*\((.*)\)\s*$")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_LEADING_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
_HEAD_ONLY_KEYWORDS = {"for", "select", "case", "fi", "done", "esac", "}"}


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


def _close_paren(text: str, i: int) -> int:
    """Index just past the ) matching the ( before position i."""
    depth = 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in "'\"`":
            i = _skip_quoted(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _skip_quoted(text: str, i: int) -> int:
    quote = text[i]
    i += 1
    while i < len(text) and text[i] != quote:
        i += 2 if text[i] == "\\" and quote != "'" else 1
    return min(i + 1, len(text))


def _nested_commands(text: str, out: list[str]) -> None:
    """Collect commands inside $( ) and backticks within a double-quoted word."""
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text.startswith("$((", i):
            i = _close_paren(text, i + 3)
        elif text.startswith("$(", i):
            j = _close_paren(text, i + 2)
            out.extend(split_commands(text[i + 2 : j - 1]))
            i = j
        elif text[i] == "`":
            j = _skip_quoted(text, i)
            out.extend(split_commands(text[i + 1 : j - 1]))
            i = j
        else:
            i += 1


def split_commands(line: str) -> list[str]:
    """Split a shell line into "name args" strings, like the CLI's parser."""
    out: list[str] = []
    words: list[str] = []
    word = ""
    heredocs: list[str] = []
    skip_next_word = False
    i, n = 0, len(line)

    def end_word() -> None:
        nonlocal word, skip_next_word
        if word:
            if skip_next_word:
                skip_next_word = False
            else:
                words.append(word)
            word = ""

    def end_command() -> None:
        end_word()
        cmd = list(words)
        words.clear()
        while cmd and cmd[0] in _LEADING_KEYWORDS:
            cmd.pop(0)
        while cmd and _ASSIGNMENT_RE.match(cmd[0]):
            cmd.pop(0)
        if cmd and cmd[0] not in _HEAD_ONLY_KEYWORDS:
            out.append(" ".join(cmd))

    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n:
            if line[i + 1] == "\n":
                i += 2
                continue
            word += line[i : i + 2]
            i += 2
        elif ch in " \t":
            end_word()
            i += 1
        elif ch == "\n":
            end_command()
            i += 1
            for delim in heredocs:
                while i < n:
                    eol = line.find("\n", i)
                    eol = n if eol == -1 else eol
                    body_line = line[i:eol]
                    i = eol + 1
                    if body_line.strip("\t") == delim:
                        break
            heredocs.clear()
        elif ch == "'":
            j = _skip_quoted(line, i)
            word += line[i:j]
            i = j
        elif ch == '"':
            j = _skip_quoted(line, i)
            _nested_commands(line[i + 1 : j - 1], out)
            word += line[i:j]
            i = j
        elif ch == "`":
            j = _skip_quoted(line, i)
            out.extend(split_commands(line[i + 1 : j - 1]))
            word += line[i:j]
            i = j
        elif line.startswith("$((", i):
            j = _close_paren(line, i + 3)
            word += line[i:j]
            i = j
        elif line.startswith("$(", i) or (
            ch in "<>" and line.startswith("(", i + 1) and not word
        ):
            start = i + 2
            j = _close_paren(line, start)
            out.extend(split_commands(line[start : j - 1]))
            word += line[i:j]
            i = j
        elif line.startswith("${", i):
            j = line.find("}", i)
            j = n if j == -1 else j + 1
            word += line[i:j]
            i = j
        elif ch == "(" and not word:
            j = _close_paren(line, i + 1)
            end_command()
            out.extend(split_commands(line[i + 1 : j - 1]))
            i = j
        elif ch in ";|&()":
            if ch == "&" and line.startswith("&>", i):
                end_word()
                i += 3 if line.startswith("&>>", i) else 2
                skip_next_word = True
                continue
            end_command()
            i += 1
        elif ch in "<>":
            if word.isdigit():
                word = ""
            end_word()
            op_end = i
            while op_end < n and line[op_end] in "<>&|-":
                op_end += 1
            op = line[i:op_end]
            i = op_end
            if op.startswith("<<") and not op.startswith("<<<"):
                while i < n and line[i] in " \t":
                    i += 1
                k = i
                while k < n and line[k] not in " \t\n;|&<>":
                    k += 1
                heredocs.append(line[i:k].strip("'\""))
                i = k
            elif op.endswith("&"):
                k = i
                while k < n and (line[k].isdigit() or line[k] == "-"):
                    k += 1
                i = k
            else:
                skip_next_word = True
        else:
            word += ch
            i += 1
    end_command()
    return out


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts = (re.escape(p) for p in pattern.strip().split("*"))
    return re.compile("^" + "[^\n\r\u2028\u2029]*".join(parts) + "$")


def match_pattern(pattern: str, value: str) -> bool:
    """CLI shell pattern match: only * is a wildcard, anchored, case-sensitive."""
    return bool(_pattern_regex(pattern).match(value))


def extract_base_command(command: str) -> str:
    text = command.strip()
    space = text.find(" ")
    return text if space == -1 else text[:space]


def matches_shell(entry_token: str, command: str) -> bool:
    """Match one deny/allow entry against ONE command (already split)."""
    entry = parse_shell_entry(entry_token)
    if entry is None:
        return False
    line = command.strip()
    base = extract_base_command(line)
    if entry.args_pattern is not None:
        args = line[line.index(" ") + 1 :].strip() if " " in line else ""
        cmd_hit = match_pattern(entry.command_pattern, base) or match_pattern(
            entry.command_pattern, line
        )
        if cmd_hit and match_pattern(entry.args_pattern, args):
            return True
    return (
        match_pattern(entry.full_pattern, line)
        or line.startswith(f"{entry.full_pattern} ")
        or match_pattern(entry.full_pattern, base)
        or base == entry.full_pattern
    )


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
    shell_deny = [t for t in deny if parse_shell_entry(t)]
    return any(
        matches_shell(token, cmd)
        for cmd in split_commands(command_line)
        for token in shell_deny
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
    return all(
        any(matches_shell(token, cmd) for token in shell_allow)
        for cmd in split_commands(command_line)
    )


def read_allowed(allow: list[str], deny: list[str], file_path: str) -> bool:
    if read_blocked_by_deny(deny, file_path):
        return False
    read_allow = [t for t in allow if parse_read_entry(t)]
    if not read_allow:
        return True
    return any(matches_read(token, file_path) for token in read_allow)



# Quoted "<env>" / "<name>" stay one word. A bare < is a redirect to this splitter.
_WRAPPERS: tuple[tuple[str, str], ...] = (
    ("bare", "{c}"),
    ("&&", "true && {c}"),
    (";", "true; {c}"),
    ("|", "true | {c}"),
    ("$( )", "echo $({c})"),
    ("backtick", "echo `{c}`"),
    ("bash -c", "bash -c '{c}'"),
    ("sh -c", "sh -c '{c}'"),
    ("env", "env {c}"),
    ("timeout", "timeout 5 {c}"),
    ("xargs", "xargs {c}"),
    ("nohup", "nohup {c}"),
    ("sudo", "sudo {c}"),
    ("absolute", ""),
)

# One command per guarded form. Wrappers are applied to every entry.
_GUARDED: tuple[tuple[str, str], ...] = (
    ("remote shell and copy", 'ssh "<name>"'),
    ("remote shell and copy", 'scp "<name>" "<name>"'),
    ("remote shell and copy", 'sftp "<name>"'),
    ("remote shell and copy", 'ssh-copy-id "<name>"'),
    ("remote shell and copy", 'rsync "<name>" "dest:<name>"'),
    ("sops decrypt or edit", 'sops decrypt "envs/<env>/secrets/<name>.sops.yaml"'),
    ("sops decrypt or edit", 'sops -d "envs/<env>/secrets/<name>.sops.yaml"'),
    ("sops decrypt or edit", 'sops edit "envs/<env>/secrets/<name>.sops.yaml"'),
    ("sops decrypt or edit", 'sops --encrypt "envs/<env>/secrets/<name>.sops.yaml"'),
    ("age decrypt", 'age -d "<name>"'),
    ("age decrypt", 'age --decrypt "<name>"'),
    ("age decrypt", 'age -i "<name>" -d "<name>"'),
    ("age key", 'cat ".config/sops/age/keys.txt"'),
    ("age key", 'ls ".config/sops/age"'),
    ("age key", 'cat "<name>.agekey"'),
    ("kubectl Secret reads", 'kubectl get secret "<name>"'),
    ("kubectl Secret reads", 'kubectl get SECRET "<name>"'),
    ("kubectl Secret reads", 'kubectl describe secret "<name>"'),
    ("kubectl mutations", 'kubectl delete "ns/<name>"'),
    ("helm mutations", 'helm upgrade "<name>" "<name>"'),
    ("flux mutations", 'flux delete kustomization "<name>"'),
)

# Deny families the cross product does not cover, plus broad-pattern collisions
# that already block on main. Not crossed with wrappers.
_RETAINED: tuple[tuple[str, str], ...] = (
    ("gh merge", 'gh pr merge "<name>"'),
    ("gh merge", 'gh api -X PUT "repos/<name>/<name>/pulls/1/merge"'),
    ("git force-push", 'git push --force origin "<name>"'),
    ("git force-push", 'git push -f origin "<name>"'),
    ("git force-push", 'git push origin +"<name>"'),
    ("kubectl Secret reads", "kubectl proxy --port 8001"),
    ("kubectl Secret reads", "kubectl get secretstores"),
    ("kubectl mutations", 'kubectl auth reconcile -f "<name>"'),
    ("kubectl mutations", 'kubectl rollout restart "deploy/<name>"'),
    ("kubectl mutations", 'kubectl --context "<name>" delete "ns/<name>"'),
    ("helm mutations", 'helm --kube-context "<name>" upgrade "<name>" "<name>"'),
    ("remote shell and copy", "rg ssh ."),
    ("remote shell and copy", 'echo "a | ssh b"'),
    ("sops decrypt or edit", "command -v sops"),
    ("sops decrypt or edit", 'git commit -m "tighten sops rule"'),
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
    "kubectl get nodes --show-labels",
    "kubectl get statefulsets -A",
    "kubectl get replicasets -A",
    "kubectl get pods --field-selector=status.phase=Running",
    "kubectl get nodes -o jsonpath='{.items[*].status.capacity.cpu}'",
    "docker images -d",
    "python3 manage.py --dry-run",
    "rg -n 'kubectl apply' docs",
    "helm template demo ./chart",
    "ls | xargs docker image ls",
    "rg -n timeout docs",
    "rg -n nohup docs",
    "git commit -m 'add env check'",
    "cd x && env A=1 docker image ls",
    "cd x && timeout 5 du -sh storage",
    "helm --debug template demo ./chart",
    "helm lint ./chart",
    "git push origin my-branch",
    "git push -u origin my-feature",
    "gh pr view 98",
    "rsync -av ./a/ ./b/",
    "kubectl get pods | grep secret",
    "pytest -q",
    "make ci-verify",
    "command -v kubectl",
    "helm template cm jetstack/cert-manager --set installCRDs=true",
    "timeout 60 docker image ls",
    "nohup python3 -m http.server --directory coverage 8000",
)


@dataclass(frozen=True)
class ShellCase:
    category: str
    wrapper: str
    command: str


@dataclass(frozen=True)
class ReadCase:
    category: str
    path: str


def _absolute_form(command: str) -> str:
    tool, _, rest = command.partition(" ")
    return f"/usr/bin/{tool} {rest}" if rest else f"/usr/bin/{tool}"


def corpus_shell_cases() -> tuple[ShellCase, ...]:
    cases: list[ShellCase] = []
    for category, base in _GUARDED:
        for name, template in _WRAPPERS:
            command = _absolute_form(base) if name == "absolute" else template.format(c=base)
            cases.append(ShellCase(category, name, command))
    cases.extend(ShellCase(category, "retained", command) for category, command in _RETAINED)
    return tuple(cases)


def corpus_read_cases() -> tuple[ReadCase, ...]:
    return (
        ReadCase("age key", ".config/sops/age/keys.txt"),
        ReadCase("age key", ".config/sops/age"),
        ReadCase("age key", "<name>.agekey"),
    )


def _uniq(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def corpus_failures(allow: list[str], deny: list[str]) -> list[str]:
    failures: list[str] = []
    for case in corpus_shell_cases():
        if command_allowed(allow, deny, case.command):
            failures.append(f"expected block: {case.category} ({case.wrapper})")
    for case in corpus_read_cases():
        if read_allowed(allow, deny, case.path):
            failures.append(f"expected block: {case.category} (read)")
    for command in DRY_RUN_MUST_ALLOW:
        if not command_allowed(allow, deny, command):
            failures.append(f"expected allow: {command}")
    return failures


def deny_by_default_failures(allow: list[str]) -> list[str]:
    """Empty allow is the default. No allow entry may match a must-block case."""
    failures: list[str] = []
    if any(token.strip() for token in allow):
        failures.append("deny-by-default: allow list must stay empty")
    for token in allow:
        if parse_shell_entry(token):
            hit: set[str] = set()
            for case in corpus_shell_cases():
                if case.category in hit:
                    continue
                if any(matches_shell(token, cmd) for cmd in split_commands(case.command)):
                    hit.add(case.category)
                    failures.append(
                        "deny-by-default: allow entry matches must-block category "
                        f"{case.category}"
                    )
        if parse_read_entry(token):
            for case in corpus_read_cases():
                if matches_read(token, case.path):
                    failures.append(
                        "deny-by-default: allow entry matches must-block category "
                        f"{case.category}"
                    )
                    break
    return _uniq(failures)


def _shell_pattern(token: str) -> str:
    entry = parse_shell_entry(token)
    return "" if entry is None else entry.full_pattern


def _is_remote(token: str) -> bool:
    pattern = _shell_pattern(token)
    return any(part in pattern for part in ("ssh", "scp", "sftp", "rsync"))


def _is_sops(token: str) -> bool:
    pattern = _shell_pattern(token)
    return "sops" in pattern and "sops/age" not in pattern


def _is_age_decrypt(token: str) -> bool:
    pattern = _shell_pattern(token)
    return "age" in pattern and ("-d" in pattern or "decrypt" in pattern)


def _is_age_key(token: str) -> bool:
    if parse_read_entry(token) is not None:
        return "sops/age" in token or "agekey" in token
    pattern = _shell_pattern(token)
    return "sops/age" in pattern or "agekey" in pattern


def _is_kubectl_secret(token: str) -> bool:
    pattern = _shell_pattern(token).lower()
    return "kubectl" in pattern and ("ecret" in pattern or "proxy" in pattern)


def _is_kubectl_mutation(token: str) -> bool:
    pattern = _shell_pattern(token)
    return "kubectl" in pattern and not _is_kubectl_secret(token)


def _is_helm(token: str) -> bool:
    return "helm" in _shell_pattern(token)


def _is_flux(token: str) -> bool:
    return "flux" in _shell_pattern(token)


def _is_gh(token: str) -> bool:
    return _shell_pattern(token).startswith("gh:")


def _is_git(token: str) -> bool:
    return _shell_pattern(token).startswith("git:")


_DENY_CLASS: dict[str, Callable[[str], bool]] = {
    "remote shell and copy": _is_remote,
    "sops decrypt or edit": _is_sops,
    "age decrypt": _is_age_decrypt,
    "age key": _is_age_key,
    "kubectl Secret reads": _is_kubectl_secret,
    "kubectl mutations": _is_kubectl_mutation,
    "helm mutations": _is_helm,
    "flux mutations": _is_flux,
    "gh merge": _is_gh,
    "git force-push": _is_git,
}

DENY_CLASS_NAMES: tuple[str, ...] = tuple(_DENY_CLASS)


def deny_without_class(deny: list[str], category: str) -> list[str]:
    """Drop one deny class. The corpus must then fail and name that category."""
    drop = _DENY_CLASS[category]
    return [token for token in deny if not drop(token)]


# Help-visible kubectl 1.36.5 commands (tools/versions.env). Plugins omitted.
# Used when the pinned binary is not on PATH. Discovery replaces this when it is.
KUBECTL_VERBS: tuple[str, ...] = (
    "create", "expose", "run", "set", "explain", "get", "edit", "delete",
    "rollout", "scale", "autoscale", "certificate", "cluster-info", "top",
    "cordon", "uncordon", "drain", "taint", "describe", "logs", "attach",
    "exec", "port-forward", "proxy", "cp", "auth", "debug", "events", "diff",
    "apply", "patch", "replace", "wait", "kustomize", "label", "annotate",
    "completion", "api-resources", "api-versions", "config", "kuberc",
    "plugin", "version",
)

# Helm 3.16.4 help names, top-level aliases, and the hidden docs command.
_HELM_VERBS: tuple[str, ...] = (
    "completion", "create", "dependency", "dep", "dependencies", "env", "get",
    "help", "history", "hist", "install", "lint", "list", "ls", "package",
    "plugin", "pull", "fetch", "push", "registry", "repo", "rollback",
    "search", "show", "inspect", "status", "template", "test", "uninstall",
    "del", "delete", "un", "upgrade", "verify", "version", "docs",
)

# flux 2.8.8 root commands (flux_distribution_version), plus cobra help.
_FLUX_VERBS: tuple[str, ...] = (
    "bootstrap", "build", "check", "completion", "create", "debug", "delete",
    "diff", "docgen", "envsubst", "events", "export", "get", "help", "install",
    "list", "logs", "migrate", "pull", "push", "reconcile", "resume", "stats",
    "suspend", "tag", "trace", "tree", "uninstall", "version",
)

_KUBECTL_MUTATIONS = frozenset({
    "apply", "annotate", "attach", "autoscale", "certificate", "cordon", "cp",
    "create", "debug", "delete", "drain", "edit", "exec", "expose", "label",
    "patch", "port-forward", "proxy", "replace", "rollout", "run", "scale",
    "set", "taint", "uncordon",
})
_KUBECTL_READS = frozenset({
    "api-resources", "api-versions", "auth", "cluster-info", "completion",
    "config", "describe", "diff", "events", "explain", "get", "kuberc",
    "kustomize", "logs", "plugin", "top", "version", "wait",
})
_HELM_MUTATIONS = frozenset({
    "del", "delete", "get", "hist", "history", "install", "list", "ls",
    "rollback", "status", "test", "un", "uninstall", "upgrade",
})
_HELM_READS = frozenset({
    "completion", "create", "dependencies", "dependency", "dep", "docs", "env",
    "fetch", "help", "inspect", "lint", "package", "plugin", "pull", "push",
    "registry", "repo", "search", "show", "template", "verify", "version",
})
_FLUX_MUTATIONS = frozenset({
    "bootstrap", "create", "debug", "delete", "install", "migrate", "resume",
    "suspend", "uninstall",
})
_FLUX_READS = frozenset({
    "build", "check", "completion", "diff", "docgen", "envsubst", "events",
    "export", "get", "help", "list", "logs", "pull", "push", "reconcile",
    "stats", "tag", "trace", "tree", "version",
})

_MUTATION_PROBE = {
    ("kubectl", "rollout"): 'kubectl rollout restart "deploy/<name>"',
    ("kubectl", "proxy"): "kubectl proxy --port 8001",
}
_READ_PROBE = {
    ("kubectl", "get"): "kubectl get pods",
    ("kubectl", "describe"): "kubectl describe pods",
    ("kubectl", "logs"): 'kubectl logs "deploy/<name>"',
    ("kubectl", "auth"): "kubectl auth can-i get pods",
    ("flux", "reconcile"): 'flux reconcile kustomization "<name>"',
    ("helm", "template"): 'helm template "<name>" "<name>"',
}


@dataclass(frozen=True)
class ToolVerbs:
    name: str
    verbs: tuple[str, ...]
    mutations: frozenset[str]
    reads: frozenset[str]


def _default_tools(kubectl: tuple[str, ...]) -> tuple[ToolVerbs, ...]:
    return (
        ToolVerbs("kubectl", kubectl, _KUBECTL_MUTATIONS, _KUBECTL_READS),
        ToolVerbs("helm", _HELM_VERBS, _HELM_MUTATIONS, _HELM_READS),
        ToolVerbs("flux", _FLUX_VERBS, _FLUX_MUTATIONS, _FLUX_READS),
    )


def _mutation_probe(tool: str, verb: str) -> str:
    return _MUTATION_PROBE.get((tool, verb), f'{tool} {verb} "ns/<name>"')


def _read_probe(tool: str, verb: str) -> str:
    return _READ_PROBE.get((tool, verb), f"{tool} {verb}")


def classification_failures(
    allow: list[str],
    deny: list[str],
    *,
    tools: tuple[ToolVerbs, ...] | None = None,
    kubectl: tuple[str, ...] | None = None,
) -> list[str]:
    """Every known subcommand is a denied mutation or a named read."""
    chosen = tools if tools is not None else _default_tools(
        kubectl if kubectl is not None else KUBECTL_VERBS
    )
    failures: list[str] = []
    for tool in chosen:
        for verb in tool.verbs:
            in_mutation = verb in tool.mutations
            in_read = verb in tool.reads
            if in_mutation and in_read:
                failures.append(f"{tool.name} subcommand {verb} is classed as both")
                continue
            if not in_mutation and not in_read:
                failures.append(f"unclassified {tool.name} subcommand: {verb}")
                continue
            if in_mutation and command_allowed(allow, deny, _mutation_probe(tool.name, verb)):
                failures.append(f"{tool.name} mutation {verb} is not denied")
            if in_read and not command_allowed(allow, deny, _read_probe(tool.name, verb)):
                failures.append(f"{tool.name} read {verb} is denied")
    return failures


_KUBECTL_VERB_RE = re.compile(r"^  ([a-z][a-z0-9-]*)(?:\s{2,}|\t)")
_REPO_ROOT = Path(__file__).resolve().parents[3]


def parse_kubectl_help(text: str) -> tuple[str, ...]:
    """Built-in commands from `kubectl --help`. Plugin sections are skipped."""
    verbs: list[str] = []
    skip = False
    for line in text.splitlines():
        if "provided by plugins" in line.lower():
            skip = True
            continue
        if line.startswith("Other Commands:"):
            skip = False
        if skip or not line.startswith("  "):
            continue
        match = _KUBECTL_VERB_RE.match(line)
        if match and match.group(1) not in verbs:
            verbs.append(match.group(1))
    return tuple(verbs)


def _pinned_kubectl_version() -> str | None:
    path = _REPO_ROOT / "tools" / "versions.env"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("kubectl_version="):
            return line.split("=", 1)[1].strip()
    return None


def _discover_pinned_kubectl_verbs() -> tuple[str, ...] | None:
    """Offline client help only, and only for the pinned kubectl version."""
    binary = shutil.which("kubectl")
    pinned = _pinned_kubectl_version()
    if binary is None or pinned is None:
        return None
    env = os.environ.copy()
    env["KUBECONFIG"] = os.devnull
    env.pop("KUBERNETES_SERVICE_HOST", None)
    env.pop("KUBERNETES_SERVICE_PORT", None)
    try:
        version = subprocess.run(
            [binary, "version", "--client", "-o", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        help_proc = subprocess.run(
            [binary, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if version.returncode != 0 or help_proc.returncode != 0:
        return None
    try:
        reported = str(json.loads(version.stdout)["clientVersion"]["gitVersion"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if reported.lstrip("v") != pinned:
        return None
    verbs = parse_kubectl_help(help_proc.stdout)
    return verbs or None


def kubectl_verbs() -> tuple[str, ...]:
    discovered = _discover_pinned_kubectl_verbs()
    return discovered if discovered is not None else KUBECTL_VERBS


def policy_failures(
    allow: list[str],
    deny: list[str],
    *,
    kubectl: tuple[str, ...] | None = None,
) -> list[str]:
    verbs = kubectl if kubectl is not None else kubectl_verbs()
    return _uniq(
        corpus_failures(allow, deny)
        + deny_by_default_failures(allow)
        + classification_failures(allow, deny, kubectl=verbs)
    )


def check_cli_policy(path: Path) -> list[str]:
    perms = load_cli_permissions(path)
    return policy_failures(perms["allow"], perms["deny"])
