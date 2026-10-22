"""cli-deny check and permission matcher (p0a-10)."""

from __future__ import annotations

import json
from pathlib import Path

from _cli_permissions import (
    command_allowed,
    command_blocked_by_deny,
    load_cli_permissions,
    matches_read,
    matches_shell,
    read_blocked_by_deny,
    run_dry_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_JSON = REPO_ROOT / ".cursor" / "cli.json"


def test_kubectl_apply_blocked_kubectl_get_allowed():
    perms = load_cli_permissions(CLI_JSON)
    deny = perms["deny"]
    assert command_blocked_by_deny(deny, "kubectl apply -f x.yaml")
    assert not command_blocked_by_deny(deny, "kubectl get pods -A")


def test_helm_and_ssh_blocked():
    perms = load_cli_permissions(CLI_JSON)
    deny = perms["deny"]
    assert command_blocked_by_deny(deny, "helm list")
    assert command_blocked_by_deny(deny, "ssh user@host")


def test_flux_writes_blocked_reads_allowed():
    perms = load_cli_permissions(CLI_JSON)
    deny = perms["deny"]
    assert command_blocked_by_deny(deny, "flux bootstrap github")
    assert command_blocked_by_deny(deny, "flux delete source git demo")
    assert not command_blocked_by_deny(deny, "flux get kustomizations -A")
    assert not command_blocked_by_deny(deny, "flux reconcile kustomization demo --with-source")


def test_age_key_path_read_blocked():
    perms = load_cli_permissions(CLI_JSON)
    deny = perms["deny"]
    assert read_blocked_by_deny(deny, ".config/sops/age/alphacut.txt")
    assert matches_read("Read(**/.config/sops/age/**)", ".config/sops/age/alphacut.txt")


def test_sops_decrypt_blocked_sops_encrypt_not_denied_by_decrypt_rules():
    perms = load_cli_permissions(CLI_JSON)
    deny = perms["deny"]
    assert command_blocked_by_deny(deny, "sops -d file.yaml")
    assert command_blocked_by_deny(deny, "sops decrypt file.yaml")
    assert not command_blocked_by_deny(deny, "sops -e file.yaml")


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
                        "Shell(sops:decrypt*)",
                        "Shell(sops:-d*)",
                        "Shell(sops:* -d*)",
                        "Shell(sops:edit*)",
                        "Shell(sops:exec-env*)",
                        "Shell(sops:exec-file*)",
                        "Shell(sops:[!e-]*)",
                        "Shell(sops:en[!c]*)",
                        "Shell(*sops* -d*)",
                        "Shell(*sops en[!c]*)",
                        "Shell(*.config/sops/age*)",
                        "Shell(*.agekey*)",
                        "Shell(flux:bootstrap*)",
                        "Shell(flux:install*)",
                        "Shell(flux:suspend*)",
                        "Read(**/.config/sops/age/**)",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    failures, _ = run_dry_run(cli)
    assert failures == []
