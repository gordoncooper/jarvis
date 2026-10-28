"""cli-deny check and permission matcher (p0a-10)."""

from __future__ import annotations

import json
from pathlib import Path

from _cli_permissions import (
    command_allowed,
    matches_shell,
    run_dry_run,
)

def test_matches_shell_command_args_split():
    assert matches_shell("Shell(kubectl:apply*)", "kubectl apply -f a.yaml")
    assert not matches_shell("Shell(kubectl:apply*)", "kubectl get pods")


def test_deny_beats_allow():
    perms = {
        "allow": ["Shell(kubectl)"],
        "deny": ["Shell(kubectl:apply*)"],
    }
    assert not command_allowed(perms["allow"], perms["deny"], "kubectl apply -f x")


def test_minimal_deny_list_in_tmp(tmp_path: Path):
    cli = tmp_path / ".cursor" / "cli.json"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": [
                        "Shell(kubectl:apply*)",
                        "Shell(kubectl:get secret*)",
                        "Shell(*kubectl*get*secret*)",
                        "Shell(helm)",
                        "Shell(*helm *)",
                        "Shell(ssh)",
                        "Shell(*ssh *)",
                        "Shell(scp)",
                        "Shell(*scp *)",
                        "Shell(sops)",
                        "Shell(sops:*)",
                        "Shell(*sops *)",
                        "Shell(*.config/sops/age*)",
                        "Shell(*.agekey*)",
                        "Shell(flux:bootstrap*)",
                        "Shell(flux:install*)",
                        "Shell(flux:suspend*)",
                        "Shell(kubectl:drain*)",
                        "Shell(*kubectl*drain*)",
                        "Shell(kubectl:label*)",
                        "Shell(*kubectl*label*)",
                        "Shell(kubectl:rollout restart*)",
                        "Shell(*kubectl*rollout*restart*)",
                        "Shell(age:-d*)",
                        "Shell(age:*-d*)",
                        "Shell(age:*--decrypt*)",
                        "Shell(*age*-d*)",
                        "Shell(*age*--decrypt*)",
                        "Read(**/.config/sops/age/**)",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    failures, _ = run_dry_run(cli)
    assert failures == []
