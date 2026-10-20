"""base-os Ansible role: template renders and container proof (p0a-11)."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    CHRONY_INVALID_FIXTURE,
    CONTAINER_NAME,
    EXPECTED_20_AUTO_UPGRADES,
    ansible_render_template,
    assert_20_auto_upgrades_file,
    assert_apt_timer_states,
    assert_localtime_matches_zone,
    chrony_template_validate_command,
    cluster_vars_playbook_path,
    docker_exec,
    inventory_path,
    load_cluster_vars,
    playbook_recap_field,
    require_docker,
    run_playbook,
    start_systemd_container,
    stop_systemd_container,
)

JOURNALD_EXPECTED_BODY = textwrap.dedent(
    """\
    # Managed by Ansible (base-os): journald disk cap (framework §7).
    [Journal]
    SystemMaxUse={system_max_use}
    """
)

CHRONY_MARKER = "JARVIS_CHRONY_VALIDATE_PRESERVE_MARKER\n"
CHRONY_INVALID_LINE = "invalid-syntax-test-line"


@pytest.mark.parametrize(
    ("raw_value", "expected_max"),
    [
        ("2G", "2G"),
        ('"2G"', "2G"),
    ],
)
def test_journald_template_via_ansible_jinja(raw_value: str, expected_max: str) -> None:
    variables = {"jarvis_journald_system_max_use": yaml_scalar(raw_value)}
    rendered = ansible_render_template("journald-jarvis.conf.j2", variables)
    assert rendered == JOURNALD_EXPECTED_BODY.format(system_max_use=expected_max)


def yaml_scalar(raw: str) -> str:
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, str)
    return loaded


def test_chrony_template_exact_content() -> None:
    cluster = load_cluster_vars()
    pools = cluster["ntp_pools"]
    assert isinstance(pools, list)
    rendered = ansible_render_template("chrony.conf.j2", {"ntp_pools": pools})
    pool_lines = "\n".join(
        f"pool {pool} iburst maxsources 4 nts" for pool in pools
    )
    assert rendered == textwrap.dedent(
        f"""\
        # Managed by Ansible (base-os): chrony with NTS (framework §4).
        # Outbound 123/udp and 4460/tcp must remain available for NTS; no node firewall changes here.

        keyfile /etc/chrony/chrony.keys
        driftfile /var/lib/chrony/chrony.drift
        ntsdumpdir /var/lib/chrony

        {pool_lines}

        leapsectz right/UTC
        makestep 1.0 3
        rtcsync

        logdir /var/log/chrony
        """
    )


def test_auto_upgrades_template_exact_content() -> None:
    rendered = ansible_render_template("20auto-upgrades.j2", {})
    assert rendered == EXPECTED_20_AUTO_UPGRADES


def test_unattended_upgrades_template_exact_content() -> None:
    rendered = ansible_render_template("50unattended-upgrades.j2", {})
    assert rendered == textwrap.dedent(
        """\
        // Managed by Ansible (base-os): unattended-upgrades package present but inactive; no auto-removals.
        // apt-daily-upgrade.timer is disabled in role tasks; upgrades are run manually by the admin.
        Unattended-Upgrade::Allowed-Origins {
        };

        Unattended-Upgrade::Package-Blacklist {
        };

        Unattended-Upgrade::DevRelease "false";
        Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";
        Unattended-Upgrade::Remove-New-Unused-Dependencies "false";
        Unattended-Upgrade::Remove-Unused-Dependencies "false";
        Unattended-Upgrade::Automatic-Reboot "false";
        """
    )


def test_site_yml_syntax_check() -> None:
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook not installed")
    cmd = [
        "ansible-playbook",
        "--syntax-check",
        "site.yml",
        "-i",
        str(inventory_path().resolve()),
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


def test_chrony_template_validate_leaves_existing_conf_unchanged() -> None:
    require_docker()
    validate = chrony_template_validate_command()
    start_systemd_container()
    try:
        prep = run_playbook()
        assert prep.returncode == 0, prep.stdout + prep.stderr
        seed = docker_exec(
            CONTAINER_NAME,
            "bash",
            "-lc",
            f"printf %s '{CHRONY_MARKER}' > /etc/chrony/chrony.conf",
        )
        assert seed.returncode == 0, seed.stderr
        apply = subprocess.run(
            [
                "ansible",
                CONTAINER_NAME,
                "-i",
                f"{CONTAINER_NAME},",
                "-e",
                "ansible_connection=docker",
                "-m",
                "ansible.builtin.template",
                "-a",
                (
                    f"src={CHRONY_INVALID_FIXTURE} dest=/etc/chrony/chrony.conf "
                    f'validate="{validate}"'
                ),
            ],
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        output = apply.stdout + apply.stderr
        assert apply.returncode != 0, output
        assert CHRONY_INVALID_LINE in output or "Invalid" in output, output
        after = docker_exec(CONTAINER_NAME, "cat", "/etc/chrony/chrony.conf")
        assert after.returncode == 0, after.stderr
        assert after.stdout == CHRONY_MARKER
    finally:
        stop_systemd_container()


def test_base_os_role_idempotent_in_container() -> None:
    require_docker()
    start_systemd_container()
    try:
        first = run_playbook()
        second = run_playbook()
        assert first.returncode == 0, first.stdout + first.stderr
        assert second.returncode == 0, second.stdout + second.stderr
        assert playbook_recap_field(second.stdout + second.stderr, "changed") == 0
        assert_apt_timer_states(CONTAINER_NAME)
        assert_20_auto_upgrades_file(CONTAINER_NAME)
        cluster = load_cluster_vars()
        time_zone = cluster["time_zone"]
        assert isinstance(time_zone, str)
        assert_localtime_matches_zone(CONTAINER_NAME, time_zone)
    finally:
        stop_systemd_container()
