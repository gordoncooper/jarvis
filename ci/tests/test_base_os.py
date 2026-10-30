"""base-os Ansible role: template rules and site syntax (p0a-11)."""

from __future__ import annotations

import subprocess

import yaml

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


def test_edge_hosts_do_not_use_the_resolved_stub() -> None:
    """Edge host DNS uses resolved's upstream list, not the 127.0.0.53 stub."""
    drop_in = ansible_render_template(
        "resolved-stub.conf.j2", {}, templates_dir=ROLE_TEMPLATES
    )
    assert "DNSStubListener=no" in drop_in

    tasks = yaml.safe_load(
        (ANSIBLE_DIR / "roles" / "base-os" / "tasks" / "main.yml").read_text(encoding="utf-8")
    )
    blocks = [task for task in tasks if isinstance(task, dict) and "block" in task]
    matched = [
        task
        for task in blocks
        if "edge" in str(task.get("when"))
        and _block_text(task, "resolved-stub.conf.j2")
        and _block_text(task, "/run/systemd/resolve/resolv.conf")
    ]
    assert len(matched) == 1
    notified = [
        item
        for item in matched[0]["block"]
        if isinstance(item, dict) and item.get("notify") == "Restart systemd-resolved"
    ]
    assert len(notified) == 1
    assert notified[0]["ansible.builtin.template"]["src"] == "resolved-stub.conf.j2"
    link_tasks = [
        item
        for item in matched[0]["block"]
        if isinstance(item, dict) and "ansible.builtin.file" in item
    ]
    assert all(item.get("notify") is None for item in link_tasks)

    handlers = yaml.safe_load(
        (ANSIBLE_DIR / "roles" / "base-os" / "handlers" / "main.yml").read_text(encoding="utf-8")
    )
    assert any(
        isinstance(handler, dict)
        and handler.get("name") == "Restart systemd-resolved"
        and handler["ansible.builtin.service"]["name"] == "systemd-resolved"
        for handler in handlers
    )


def _block_text(task: dict[str, object], needle: str) -> bool:
    return needle in yaml.safe_dump(task.get("block"))


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
