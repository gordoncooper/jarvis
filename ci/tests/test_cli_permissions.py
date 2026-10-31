"""cli-deny check and permission matcher (p0a-10, R34)."""

from __future__ import annotations

from pathlib import Path

import pytest

from _cli_permissions import (
    DENY_CLASS_NAMES,
    KUBECTL_VERBS,
    ToolVerbs,
    classification_failures,
    command_allowed,
    corpus_shell_cases,
    deny_by_default_failures,
    deny_without_class,
    load_cli_permissions,
    matches_shell,
    parse_kubectl_help,
    policy_failures,
    split_commands,
)

_REPO = Path(__file__).resolve().parents[2]
_CLI = _REPO / ".cursor" / "cli.json"
_WRAPPERS = {
    "bare",
    "&&",
    ";",
    "|",
    "$( )",
    "backtick",
    "bash -c",
    "sh -c",
    "env",
    "timeout",
    "xargs",
    "nohup",
    "sudo",
    "absolute",
}
_GUARDED = {
    "remote shell and copy",
    "sops decrypt or edit",
    "age decrypt",
    "age key",
    "kubectl Secret reads",
    "kubectl mutations",
    "helm mutations",
    "flux mutations",
}


def test_matches_shell_command_args_split():
    assert matches_shell("Shell(kubectl:apply*)", "kubectl apply -f a.yaml")
    assert not matches_shell("Shell(kubectl:apply*)", "kubectl get pods")
    # Only * is a wildcard: ? is literal, and matching is case-sensitive.
    assert not matches_shell("Shell(kubectl:get ?ecret)", "kubectl get secret")
    assert not matches_shell("Shell(kubectl:get secret)", "kubectl get Secret")


def test_split_commands_like_the_cli():
    assert split_commands("FOO=1 cd x && timeout 5 ssh h | tee o; echo $(sops -d f)") == [
        "cd x",
        "timeout 5 ssh h",
        "tee o",
        "sops -d f",
        "echo $(sops -d f)",
    ]
    # bash -c is not unwrapped: the quoted script stays one argument.
    assert split_commands("bash -c 'ssh h'") == ["bash -c 'ssh h'"]


def test_deny_beats_allow():
    perms = {
        "allow": ["Shell(kubectl)"],
        "deny": ["Shell(kubectl:apply*)"],
    }
    assert not command_allowed(perms["allow"], perms["deny"], "kubectl apply -f x")
    assert command_allowed(perms["allow"], perms["deny"], "kubectl get pods")
    # Every command in a chain must be allowed, and any denied one blocks the line.
    assert not command_allowed(perms["allow"], perms["deny"], "kubectl get pods && ls")
    assert not command_allowed(
        perms["allow"], perms["deny"], "kubectl get pods && kubectl apply -f x"
    )


def test_corpus_crosses_each_guarded_category_with_each_wrapper():
    """R34: must-block cases are the category × wrapper product."""
    product = [case for case in corpus_shell_cases() if case.wrapper != "retained"]
    categories = {case.category for case in product}
    assert _GUARDED <= categories
    assert {case.wrapper for case in product} == _WRAPPERS
    for category in _GUARDED:
        got = {case.wrapper for case in product if case.category == category}
        assert got == _WRAPPERS


def test_empty_allow_passes_deny_by_default():
    """R34: an empty allow list is the default."""
    assert deny_by_default_failures([]) == []


def test_broad_shell_allow_fails_deny_by_default():
    """R34: a broad Shell allow entry fails the deny-by-default assertion."""
    failures = deny_by_default_failures(["Shell(*)"])
    assert "deny-by-default: allow list must stay empty" in failures
    assert any("must-block category" in line for line in failures)


def test_unclassified_subcommand_fails():
    """R34: a subcommand nobody has classed fails."""
    tool = ToolVerbs("kubectl", ("get", "frobnicate"), frozenset(), frozenset({"get"}))
    failures = classification_failures([], [], tools=(tool,))
    assert failures == ["unclassified kubectl subcommand: frobnicate"]


def test_classed_mutation_must_be_denied():
    """R34: a mutation class is a lie unless the guard denies that subcommand."""
    tool = ToolVerbs("kubectl", ("delete",), frozenset({"delete"}), frozenset())
    failures = classification_failures([], [], tools=(tool,))
    assert failures == ["kubectl mutation delete is not denied"]


def test_named_read_must_stay_allowed():
    """R34: a named read is a lie if a deny entry blocks it."""
    tool = ToolVerbs("kubectl", ("get",), frozenset(), frozenset({"get"}))
    failures = classification_failures([], ["Shell(*get*)"], tools=(tool,))
    assert failures == ["kubectl read get is denied"]


@pytest.mark.parametrize("category", DENY_CLASS_NAMES)
def test_seeded_deny_class_removal_names_the_category(category: str):
    """R34: removing one deny class makes the policy check fail and name it."""
    perms = load_cli_permissions(_CLI)
    deny = deny_without_class(perms["deny"], category)
    assert len(deny) < len(perms["deny"])
    failures = policy_failures(perms["allow"], deny, kubectl=KUBECTL_VERBS)
    assert any(line.startswith(f"expected block: {category} ") for line in failures)


def test_parse_kubectl_help_skips_plugin_commands():
    text = """
Basic Commands (Beginner):
  get             Display one or many resources
  delete          Delete resources

Subcommands provided by plugins:
  my-plugin       From a workstation plugin

Other Commands:
  version         Print the client and server version information
"""
    assert parse_kubectl_help(text) == ("get", "delete", "version")
