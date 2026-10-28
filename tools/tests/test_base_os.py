"""base-os Ansible role: template rules and site syntax (p0a-11)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from conftest import require_cli
from ansible_role_proof import (
    ANSIBLE_DIR,
    REPO_ROOT,
    ansible_render_template,
    cluster_vars_playbook_path,
    env_inventory_args,
    load_cluster_vars,
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

    journald = ansible_render_template(
        "journald-jarvis.conf.j2",
        {"jarvis_journald_system_max_use": cluster.get("jarvis_journald_system_max_use", "2G")},
        templates_dir=ROLE_TEMPLATES,
    )
    assert "SystemMaxUse=2G" in journald

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


def test_community_general_requirements_pin_is_the_only_version() -> None:
    galaxy = REPO_ROOT / ".venv" / "bin" / "ansible-galaxy"
    if not galaxy.is_file():
        galaxy = Path(require_cli("ansible-galaxy"))
    text = (ANSIBLE_DIR / "requirements.yml").read_text(encoding="utf-8")
    pinned = re.findall(r"name:\s*community\.general\s+version:\s*[\"']?(\d+\.\d+\.\d+)", text)
    assert pinned == ["12.1.0"]
    proc = subprocess.run(
        [str(galaxy), "collection", "list", "community.general"],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    versions = set(re.findall(r"community\.general\s+(\d+\.\d+\.\d+)", proc.stdout))
    assert versions == {"12.1.0"}, proc.stdout + proc.stderr
