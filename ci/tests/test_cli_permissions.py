"""cli-deny: both coding-agent deny lists against one category corpus."""

from __future__ import annotations

import functools
import os
import shutil
from pathlib import Path

import pytest

from _cli_permissions import (
    CLIENTS,
    CORPUS_PATH,
    WRAPPERS,
    Row,
    check_repo,
    claude_forms,
    load_corpus,
    load_permissions,
    load_regression,
    matches_bash,
    policy_failures,
    row_blocked,
    shell_blocked,
)
from support import REPO_ROOT

CORPUS = load_corpus(CORPUS_PATH)
REGRESSION = load_regression()
ROWS = CORPUS + REGRESSION
BLOCK_CATEGORIES = sorted({r.category for r in CORPUS if r.verdict == "block"})


def _deny(client: str) -> list[str]:
    rel = next(path for name, path, _ in CLIENTS if name == client)
    return load_permissions(REPO_ROOT / rel)[1]


def _failures(client: str, deny: list[str]) -> list[str]:
    return _cached_failures(client, tuple(deny))


@functools.cache
def _cached_failures(client: str, deny: tuple[str, ...]) -> list[str]:
    return policy_failures(client, [], list(deny), ROWS)


def _blocks_fail(failures: list[str], client: str, category: str) -> bool:
    return any(f.startswith(f"{client}: expected block: {category} ") for f in failures)


def test_claude_matcher_follows_its_permission_docs():
    """Rule: Claude Code strips its documented wrappers, and a trailing " *" matches the bare command."""
    assert claude_forms("timeout 30 npm test")[-1] == "npm test"
    assert claude_forms("nohup stdbuf -o0 npm test") == [
        "nohup stdbuf -o0 npm test", "stdbuf -o0 npm test", "npm test"
    ]
    assert claude_forms("xargs -n1 grep x") == ["xargs -n1 grep x"]
    assert claude_forms("command -v sops") == ["command -v sops"]
    assert matches_bash("ls *", "ls") and matches_bash("ls:*", "ls -la")
    assert not matches_bash("ls *", "lsof") and matches_bash("ls*", "lsof")
    assert not matches_bash("* --help *", "npm --help")


def test_cursor_matcher_strips_no_wrapper():
    """Rule: Cursor matches the wrapper as the command, so the lists must name wrappers."""
    line = "timeout 5 kubectl apply -f x.yaml"
    assert not shell_blocked("cursor", ["Shell(kubectl:*apply *)"], line)
    assert shell_blocked("claude", ["Bash(kubectl *apply *)"], line)


def test_corpus_expansions_and_bad_rows_fail(tmp_path: Path):
    """Rule: {anycase:} is every spelling, {any:} every option, wrapped every wrapper;
    a malformed corpus row is an error."""
    path = tmp_path / "corpus.tsv"
    path.write_text("block\tdemo\tshell\tget {anycase:ab}{any:,s}\n", encoding="utf-8")
    texts = sorted(r.text for r in load_corpus(path))
    assert texts == sorted(f"get {a}{s}" for a in ("ab", "aB", "Ab", "AB") for s in ("", "s"))
    path.write_text("block\tdemo\twrapped\tget x\n", encoding="utf-8")
    rows = load_corpus(path)
    assert {r.kind for r in rows} == {"shell"} and len(rows) == len(WRAPPERS)
    assert {"get x", "sudo get x", "bash -c 'get x'", "/usr/bin/get x"} <= {r.text for r in rows}
    path.write_text("deny\tdemo\tshell\tget x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="want verdict, category, kind, input"):
        load_corpus(path)


@pytest.mark.parametrize(
    ("client", "entry", "category"),
    [
        ("cursor", "Shell(kubectl:*get*CrEt*)", "secret-read"),
        ("claude", "Bash(kubectl *get*CrEt*)", "secret-read"),
        ("cursor", "Shell(cat:*.kube/*)", "admin-kubeconfig"),
        ("claude", "Bash(cat *.kube/*)", "admin-kubeconfig"),
        ("cursor", "Read(/etc/rancher/**)", "admin-kubeconfig"),
        ("claude", "Read(//etc/rancher/**)", "admin-kubeconfig"),
        ("cursor", "Shell(make:*drill*)", "cluster-entrypoint"),
        ("claude", "Bash(make *drill*)", "cluster-entrypoint"),
        ("cursor", "Shell(git:*push * main)", "forge-write"),
        ("claude", "Bash(git *push * main)", "forge-write"),
        ("cursor", "Shell(flux:*reconcile*)", "cluster-write"),
        ("claude", "Bash(flux *reconcile*)", "cluster-write"),
    ],
)
def test_deleted_deny_entry_fails_its_category(client: str, entry: str, category: str):
    """Rule: deleting one guard entry makes the check fail and name the category."""
    deny = _deny(client)
    assert entry in deny
    assert not _blocks_fail(_failures(client, deny), client, category)
    assert _blocks_fail(_failures(client, [t for t in deny if t != entry]), client, category)


@pytest.mark.parametrize("category", BLOCK_CATEGORIES)
def test_claude_file_missing_a_cursor_category_fails(category: str):
    """Rule: parity. A category the Cursor list blocks and the Claude list lacks fails."""
    rows = [r for r in ROWS if r.category == category and r.verdict == "block"]
    deny = [t for t in _deny("claude") if not any(row_blocked("claude", [t], r) for r in rows)]
    assert not _blocks_fail(_failures("cursor", _deny("cursor")), "cursor", category)
    assert _blocks_fail(_failures("claude", deny), "claude", category)


def test_intended_client_differences_are_pinned():
    """Rule: the lists differ only where the clients' documented semantics differ. Cursor
    needs one entry per process wrapper; Claude strips those wrappers, but needs an
    end-anchored verb because stripping a bare xargs can leave the verb last."""
    cursor, claude = _deny("cursor"), _deny("claude")
    wrapped = "timeout 5 kubectl delete ns demo"
    no_timeout = ([t for t in cursor if "timeout" not in t], [t for t in claude if "timeout" not in t])
    assert shell_blocked("cursor", cursor, wrapped) and shell_blocked("claude", claude, wrapped)
    assert not shell_blocked("cursor", no_timeout[0], wrapped)
    assert shell_blocked("claude", no_timeout[1], wrapped)
    piped = "kubectl get pods -o name | xargs kubectl delete"
    bare_verb = ([t for t in cursor if not t.endswith("delete)")], [t for t in claude if not t.endswith("delete)")])
    assert shell_blocked("cursor", cursor, piped) and shell_blocked("claude", claude, piped)
    assert shell_blocked("cursor", bare_verb[0], piped)
    assert not shell_blocked("claude", bare_verb[1], piped)


@pytest.mark.parametrize(("client", "entry"), [("cursor", "Shell(git:*)"), ("claude", "Bash(git *)")])
def test_over_blocking_everyday_work_fails(client: str, entry: str):
    """Rule: a deny entry that blocks a must-allow row fails the check."""
    failures = _failures(client, _deny(client) + [entry])
    assert any(f.startswith(f"{client}: expected allow: git ") for f in failures)


@pytest.mark.parametrize(("client", "allow", "deny"), [
    ("cursor", ["Shell(*)"], []),
    ("claude", [], ["Shell(*ssh *)"]),
])
def test_allow_entries_and_foreign_rules_fail(client: str, allow: list[str], deny: list[str]):
    """Rule: the allow list stays empty, and a rule the client would ignore is an error."""
    failures = policy_failures(client, allow, deny, [])
    assert failures == [
        f"{client}: allow list must stay empty"
        if allow
        else f"{client}: deny entry is not a Bash(...) or Read(...) rule"
    ]


@pytest.mark.parametrize("client", [name for name, _, _ in CLIENTS])
def test_category_corpus_covers_the_regression_rows(client: str):
    """Rule: deleting every entry that blocks a regression must-block row also fails the
    category corpus, so the corpus alone guards each regression row; every regression
    must-allow row is in the category corpus."""
    deny = _deny(client)

    def blockers(row: Row) -> frozenset[str]:
        return frozenset(t for t in deny if row_blocked(client, [t], row))

    corpus = {blockers(r) for r in CORPUS if r.verdict == "block"}
    regression = [(r.where, blockers(r)) for r in REGRESSION if r.verdict == "block"]
    uncovered = [where for where, b in regression if not any(c <= b for c in corpus)]
    assert uncovered == []
    allowed = {r.text for r in CORPUS if r.verdict == "allow"}
    assert [r.where for r in REGRESSION if r.verdict == "allow" and r.text not in allowed] == []


def test_claude_readings_where_its_docs_are_silent():
    """Rule: a Claude block row must match after stripping and in its own case; an allow
    row fails if any form matches in any case."""
    def run(verdict: str, deny: str, text: str) -> list[str]:
        return policy_failures("claude", [], [deny], [Row(verdict, "demo", "shell", text, "t:1")])

    for deny, text in (("Bash(timeout *)", "timeout 5 npm test"), ("Bash(NPM *)", "npm test")):
        assert run("block", deny, text) == ["claude: expected block: demo (t:1)"]
        assert run("allow", deny, text) == ["claude: expected allow: demo (t:1)"]


def test_result_does_not_depend_on_kubectl_on_path(tmp_path: Path, monkeypatch):
    """Rule: the check never runs kubectl, so PATH does not change its result."""
    for _, rel, _ in CLIENTS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, tmp_path / rel)
    marker = tmp_path / "ran"
    fake = tmp_path / "bin" / "kubectl"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\ntouch {marker}\necho frobnicate\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", os.devnull)
    without = check_repo(tmp_path)
    monkeypatch.setenv("PATH", str(fake.parent))
    assert check_repo(tmp_path) == without
    assert not marker.exists()
