"""cli-deny check and permission matcher (p0a-10)."""

from __future__ import annotations

from _cli_permissions import (
    command_allowed,
    matches_shell,
    split_commands,
)


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
