"""base-os Ansible role: template rules and site syntax (p0a-11)."""

from __future__ import annotations

import subprocess

from conftest import require_cli
from ansible_role_proof import (
    ANSIBLE_DIR,
    ansible_render_template,
    cluster_vars_playbook_path,
    env_inventory_args,
    load_cluster_vars,
    load_role_defaults,
)

ROLE_TEMPLATES = ANSIBLE_DIR / "roles" / "base-os" / "templates"


def test_base_os_templates_encode_manual_upgrade_policy() -> None:
    cluster = load_cluster_vars()
    pools = cluster["ntp_pools"]
    assert isinstance(pools, list)
    chrony = ansible_render_template(
        "chrony.conf.j2", {"ntp_pools": pools}, templates_dir=ROLE_TEMPLATES
    )
    assert " nts" in chrony
    for pool in pools:
        assert f"pool {pool}" in chrony

    journald_cap = load_role_defaults(ANSIBLE_DIR / "roles" / "base-os")[
        "jarvis_journald_system_max_use"
    ]
    journald = ansible_render_template(
        "journald-jarvis.conf.j2",
        {"jarvis_journald_system_max_use": journald_cap},
        templates_dir=ROLE_TEMPLATES,
    )
    assert f"SystemMaxUse={journald_cap}" in journald

    auto_upgrades_path = ROLE_TEMPLATES / "20auto-upgrades.j2"
    assert "departure from framework" not in auto_upgrades_path.read_text(encoding="utf-8")
    auto_upgrades = ansible_render_template(
        "20auto-upgrades.j2", {}, templates_dir=ROLE_TEMPLATES
    )
    assert 'Unattended-Upgrade "0"' in auto_upgrades

    unattended = ansible_render_template(
        "50unattended-upgrades.j2", {}, templates_dir=ROLE_TEMPLATES
    )
    assert 'Automatic-Reboot "false"' in unattended


def test_site_yml_syntax_check() -> None:
    require_cli("ansible-playbook")
    cmd = [
        "ansible-playbook",
        "--syntax-check",
        "site.yml",
        *env_inventory_args(),
        "-e",
        f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
