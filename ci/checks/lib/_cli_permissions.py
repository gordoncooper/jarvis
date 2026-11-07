"""Coding-agent deny lists, proved against one category corpus.

`.cursor/cli.json` (Cursor) and `.claude/settings.json` (Claude Code) must
block every `block` row and allow every `allow` row of
`fixtures/guard-corpus.tsv`. The check proves the checked-in lists against
this model of each client. It does not prove the clients. The lists are
best-effort defence in depth. Workers hold no age key. A worker may name a
kubeconfig for cluster reads, so what it can reach is that kubeconfig's identity:
these lists do not stop a form they cannot see.

Both clients split a line into commands at && || ; | & newlines, $( ),
backticks and subshells, drop leading FOO=1 assignments, and match each
command as raw word text (quotes kept, words joined by one space, so corpus
rows use single spaces). Neither unwraps bash -c, eval, sudo, env, absolute
paths or interpreters, so the lists name those wrappers one by one.

Neither client lets an allow rule carve an exception out of a deny rule, so
breadth has to come off the deny side.

Known limits, left because closing them unblocks a real run or the pattern
language cannot name them: interpreter, busybox, strace, and git-push entries
stay wide, so a guarded word later in those arguments stays blocked. A
leading-wildcard shell wrapper also matches the same text later in a line,
and dropping the wildcard lets an absolute shell path through. Wrapper
options and nesting the Claude docs do not describe; a cluster-tool verb
whose flags sit where no entry looks; admin-kubeconfig readers other than
the named ones. A force push of the default branch can match both the force
entry and the branch entry, and patterns cannot subtract.

Cursor, mirroring cursor-agent 2026.10.22-27dcf5f: Shell(cmd:args) or
Shell(pattern), only * is a wildcard, anchored, case-sensitive, no wrapper
is stripped. Read(glob) without a leading / is scoped to the workspace.

Claude Code, from https://code.claude.com/docs/en/permissions: Bash(pattern),
* matches any text, a trailing " *" (or ":*") also matches the bare command,
and timeout, time, nice, nohup, stdbuf, command, builtin, noglob and a bare
xargs are stripped before matching. Read(path) uses gitignore patterns:
//abs, ~/home, /project, and relative paths under the working directory.
The docs do not say whether Bash matching is case-sensitive, whether nested
wrappers are all stripped, or whether a deny rule also sees the unstripped
command, so a block row must be blocked case-sensitively both after one
wrapper is stripped and after all are, and an allow row must stay allowed
case-insensitively in every form.
"""

from __future__ import annotations

import functools
import itertools
import json
import re
from dataclasses import dataclass
from pathlib import Path

SHELL_ENTRY_RE = re.compile(r"^\s*(Shell|Bash)\s*\((.*)\)\s*$")
READ_ENTRY_RE = re.compile(r"^\s*Read\s*\((.*)\)\s*$")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_LEADING_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
_HEAD_ONLY_KEYWORDS = {"for", "select", "case", "fi", "done", "esac", "}"}

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS_PATH = FIXTURES / "guard-corpus.tsv"
# Client name, settings file, shell tool name in that file.
CLIENTS: tuple[tuple[str, str, str], ...] = (
    ("cursor", ".cursor/cli.json", "Shell"),
    ("claude", ".claude/settings.json", "Bash"),
)


@dataclass(frozen=True)
class ShellEntry:
    full_pattern: str
    command_pattern: str
    args_pattern: str | None


@functools.cache
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
    """Split a shell line into "name args" strings, like the clients' parsers."""
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


# Cursor's * stops at line and paragraph separators.
_CURSOR_STAR = "[^\n\r" + chr(0x2028) + chr(0x2029) + "]*"


@functools.cache
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts = (re.escape(p) for p in pattern.strip().split("*"))
    return re.compile("^" + _CURSOR_STAR.join(parts) + "$")


def match_pattern(pattern: str, value: str) -> bool:
    """Cursor shell pattern match: only * is a wildcard, anchored, case-sensitive."""
    return bool(_pattern_regex(pattern).match(value))


def extract_base_command(command: str) -> str:
    text = command.strip()
    space = text.find(" ")
    return text if space == -1 else text[:space]


def matches_shell(entry_token: str, command: str) -> bool:
    """Match one Cursor Shell entry against ONE command (already split)."""
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


def matches_bash(pattern: str, command: str, *, fold: bool = False) -> bool:
    """Match one Claude Code Bash pattern against ONE command (already stripped)."""
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    if fold:
        pattern, command = pattern.lower(), command.lower()
    if pattern.endswith(" *") and pattern.count("*") == 1 and command == pattern[:-2]:
        return True
    return _bash_regex(pattern).fullmatch(command) is not None


@functools.cache
def _bash_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")), re.DOTALL)


_BARE_WRAPPERS = {"time", "nice", "nohup", "command", "builtin", "noglob", "xargs"}
_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")


def claude_forms(command: str) -> list[str]:
    """The command, then each form after stripping one documented wrapper."""
    forms = [command]
    while True:
        words = forms[-1].split(" ")
        head, rest = words[0], words[1:]
        if head == "timeout" and rest and _DURATION_RE.match(rest[0]):
            rest = rest[1:]
        elif head == "stdbuf" and rest and rest[0].startswith("-"):
            rest = list(itertools.dropwhile(lambda w: w.startswith("-"), rest))
        elif head not in _BARE_WRAPPERS or not rest or rest[0].startswith("-"):
            return forms
        if not rest:
            return forms
        forms.append(" ".join(rest))


# Stand-in roots: real home and workspace paths never enter the corpus.
_HOME, _WORKSPACE = "/h", "/w"


def _read_anchor(client: str, pattern: str) -> str | None:
    """Absolute glob for one Read pattern."""
    if client == "claude":
        if pattern.startswith("//"):
            return pattern[1:]
        if pattern.startswith("~/"):
            return _HOME + pattern[1:]
        if pattern.startswith("/"):
            return _WORKSPACE + pattern
        if "/" not in pattern.rstrip("/"):
            pattern = "**/" + pattern
    elif pattern.startswith("/"):
        return pattern
    elif pattern.startswith("~"):
        return None  # Cursor documents workspace and absolute paths only.
    return f"{_WORKSPACE}/{pattern.removeprefix('./')}"


def _glob_regex(glob: str) -> str:
    out = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"/\*\*", "/.+")
    return out.replace(r"\*", "[^/]*")


def read_blocked(client: str, deny: list[str], path: str) -> bool:
    if path.startswith("~/"):
        path = _HOME + path[1:]
    elif not path.startswith("/"):
        path = f"{_WORKSPACE}/{path}"
    for token in deny:
        pattern = parse_read_entry(token)
        anchored = None if pattern is None else _read_anchor(client, pattern)
        if anchored and re.fullmatch(_glob_regex(anchored), path):
            return True
    return False


@functools.cache
def _commands(line: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((c, tuple(claude_forms(c))) for c in split_commands(line))


def shell_blocked(client: str, deny: list[str], line: str, *, loose: bool = False) -> bool:
    """Whether a deny entry blocks the line. loose takes the reading that blocks more."""
    shell = [t for t in deny if parse_shell_entry(t)]
    for command, forms in _commands(line):
        if client == "cursor":
            if any(matches_shell(t, command) for t in shell):
                return True
            continue
        # Strict: blocked whether Claude Code strips one wrapper or all of them.
        strict = {forms[min(1, len(forms) - 1)], forms[-1]}
        for token in shell:
            pattern = parse_shell_entry(token).full_pattern
            if loose and any(matches_bash(pattern, f, fold=True) for f in forms):
                return True
            if not loose and all(matches_bash(pattern, f) for f in strict):
                return True
    return False


@dataclass(frozen=True)
class Row:
    verdict: str
    category: str
    kind: str
    text: str
    where: str


# A "wrapped" row is the command plus each of these forms of it.
WRAPPERS: tuple[str, ...] = (
    "{c}", "true && {c}", "true; {c}", "true | {c}", "echo $({c})", "echo `{c}`",
    "bash -c '{c}'", "sh -c '{c}'", "env {c}", "timeout 5 {c}", "xargs {c}",
    "nohup {c}", "nice {c}", "stdbuf -o0 {c}", "sudo {c}", "/usr/bin/{c}",
)
_EXPAND_RE = re.compile(r"\{(any|anycase):([^{}]*)\}")


def _expand(text: str) -> list[str]:
    """{any:a,b} gives one row per option; {anycase:word} one per spelling of word."""
    match = _EXPAND_RE.search(text)
    if match is None:
        return [text]
    if match[1] == "any":
        options = match[2].split(",")
    else:
        options = ["".join(c) for c in itertools.product(*((x.lower(), x.upper()) for x in match[2]))]
    return [t for o in options for t in _expand(text[: match.start()] + o + text[match.end() :])]


def load_corpus(path: Path) -> list[Row]:
    """verdict, category, kind (shell, wrapped or read), input."""
    rows: list[Row] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if (
            len(parts) != 4
            or parts[0] not in ("block", "allow")
            or parts[2] not in ("shell", "wrapped", "read")
            or not parts[1]
            or not parts[3]
        ):
            raise ValueError(f"{path.name} line {lineno}: want verdict, category, kind, input")
        verdict, category, kind, text = parts
        texts = _expand(text)
        if kind == "wrapped":
            kind, texts = "shell", [w.format(c=t) for t in texts for w in WRAPPERS]
        rows.extend(Row(verdict, category, kind, t, f"{path.name}:{lineno}") for t in texts)
    return rows


def row_blocked(client: str, deny: list[str], row: Row) -> bool:
    if row.kind == "read":
        return read_blocked(client, deny, row.text)
    return shell_blocked(client, deny, row.text, loose=row.verdict == "allow")


def load_permissions(path: Path) -> tuple[list[str], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    permissions = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(permissions, dict):
        raise ValueError("must include a permissions object")
    allow, deny = permissions.get("allow", []), permissions.get("deny", [])
    if not isinstance(allow, list) or not isinstance(deny, list):
        raise ValueError("permissions.allow and permissions.deny must be arrays")
    return [str(x) for x in allow], [str(x) for x in deny]


def policy_failures(client: str, allow: list[str], deny: list[str], rows: list[Row]) -> list[str]:
    """Names the category and row, never the input: a block row that passes is a bypass."""
    tool = next(shell for name, _, shell in CLIENTS if name == client)
    failures: list[str] = []
    if any(token.strip() for token in allow):
        failures.append(f"{client}: allow list must stay empty")
    for token in deny:
        entry = SHELL_ENTRY_RE.match(token)
        if not READ_ENTRY_RE.match(token) and (entry is None or entry[1] != tool):
            failures.append(f"{client}: deny entry is not a {tool}(...) or Read(...) rule")
    for row in rows:
        if row_blocked(client, deny, row) != (row.verdict == "block"):
            failures.append(f"{client}: expected {row.verdict}: {row.category} ({row.where})")
    return list(dict.fromkeys(failures))


def check_repo(root: Path) -> list[str]:
    rows = load_corpus(CORPUS_PATH)
    failures: list[str] = []
    for client, rel, _ in CLIENTS:
        path = root / rel
        if not path.is_file():
            failures.append(f"missing {rel}")
            continue
        try:
            allow, deny = load_permissions(path)
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            failures.append(f"{rel}: {exc}")
            continue
        failures += policy_failures(client, allow, deny, rows)
    return failures
