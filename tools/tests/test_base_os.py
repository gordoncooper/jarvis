"""base-os Ansible role: template renders and optional container idempotence (p0a-11)."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from _core_names import sole_env_directory_name

REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = REPO_ROOT / "infra" / "ansible"
ROLE_TEMPLATES = ANSIBLE_DIR / "roles" / "base-os" / "templates"


def _env_name() -> str:
    return sole_env_directory_name(REPO_ROOT)


def _cluster_vars_playbook_path() -> str:
    return f"../../envs/{_env_name()}/values/cluster-vars.yaml"


def _inventory_path() -> Path:
    return REPO_ROOT / "envs" / _env_name() / "inventory" / "hosts.yml"


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(ROLE_TEMPLATES)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(template_name: str, variables: dict[str, str]) -> str:
    return _jinja_env().get_template(template_name).render(**variables)


def _load_cluster_vars_text(text: str) -> dict[str, object]:
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def test_journald_template_exact_content() -> None:
    rendered = _render(
        "journald-jarvis.conf.j2",
        {"jarvis_journald_system_max_use": "2G"},
    )
    assert rendered == textwrap.dedent(
        """\
        # Managed by Ansible (base-os): journald disk cap (framework §7).
        [Journal]
        SystemMaxUse=2G
        """
    )


def test_chrony_template_exact_content() -> None:
    rendered = _render("chrony.conf.j2", {})
    assert rendered == textwrap.dedent(
        """\
        # Managed by Ansible (base-os): chrony with NTS (framework §4).
        # Outbound 123/udp and 4460/tcp must remain available for NTS; no node firewall changes here.

        keyfile /etc/chrony/chrony.keys
        driftfile /var/lib/chrony/chrony.drift
        ntsdumpdir /var/lib/chrony

        # Ubuntu default pool with NTS enabled.
        pool ntp.ubuntu.com iburst maxsources 4 nts

        leapsectz right/UTC
        makestep 1.0 3 3
        rtcsync

        logdir /var/log/chrony
        """
    )


def test_auto_upgrades_template_exact_content() -> None:
    rendered = _render("20auto-upgrades.j2", {})
    assert rendered == textwrap.dedent(
        """\
        # Managed by Ansible (base-os): enable unattended security upgrades (framework §4).
        APT::Periodic::Update-Package-Lists "1";
        APT::Periodic::Download-Upgradeable-Packages "0";
        APT::Periodic::AutocleanInterval "0";
        APT::Periodic::Unattended-Upgrade "1";
        """
    )


def test_unattended_upgrades_template_exact_content() -> None:
    rendered = _render("50unattended-upgrades.j2", {})
    assert "Unattended-Upgrade::Automatic-Reboot \"false\";" in rendered
    assert rendered.endswith("\n")
    assert rendered.startswith(
        "// Managed by Ansible (base-os): security upgrades only; Gordon reboots after kernel updates (§8).\n"
    )


def test_timezone_with_slash_from_quoted_yaml() -> None:
    """cluster-vars may quote values; time_zone must still render in /etc/timezone content."""
    raw = textwrap.dedent(
        """\
        time_zone: "America/Chicago"
        ssh_user: ubuntu
        """
    )
    vars_map = _load_cluster_vars_text(raw)
    assert vars_map["time_zone"] == "America/Chicago"
    tz_file = f"{vars_map['time_zone']}\n"
    assert "/" in tz_file
    assert tz_file == "America/Chicago\n"


def test_ssh_user_quoted_vs_unquoted_yaml() -> None:
    quoted = _load_cluster_vars_text('ssh_user: "ubuntu"\n')
    unquoted = _load_cluster_vars_text("ssh_user: ubuntu\n")
    assert quoted["ssh_user"] == unquoted["ssh_user"] == "ubuntu"


def test_site_yml_syntax_check() -> None:
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook not installed")
    cmd = [
        "ansible-playbook",
        "--syntax-check",
        "site.yml",
        "-i",
        str(_inventory_path().resolve()),
        "-e",
        f"jarvis_cluster_vars={_cluster_vars_playbook_path()}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT / "infra" / "ansible",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ubuntu_image_available() -> str | None:
    if not _docker_available():
        return None
    for tag in ("ubuntu:26.04", "ubuntu:24.04"):
        proc = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            return tag
        pull = subprocess.run(
            ["docker", "pull", tag],
            capture_output=True,
            text=True,
            check=False,
        )
        if pull.returncode == 0:
            return tag
    return None


def _playbook_changed_count(output: str) -> int:
    match = re.search(r"changed=(\d+)", output)
    assert match is not None, output
    return int(match.group(1))


def test_base_os_role_idempotent_in_container() -> None:
    image = _ubuntu_image_available()
    if image is None:
        pytest.skip("no docker or no ubuntu image available")

    container = "jarvis-base-os-idem-test"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)

    playbook = [
        "ansible-playbook",
        "site.yml",
        "-i",
        f"{container},",
        "-e",
        f"jarvis_cluster_vars={_cluster_vars_playbook_path()}",
        "-e",
        "ansible_connection=docker",
    ]

    start = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--privileged",
            image,
            "sleep",
            "3600",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert start.stdout.strip()

    prep = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "bash",
            "-lc",
            "export DEBIAN_FRONTEND=noninteractive && "
            "apt-get update -qq && "
            "apt-get install -y -qq python3 sudo systemd > /dev/null",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if prep.returncode != 0:
        subprocess.run(["docker", "rm", "-f", container], check=False)
        pytest.skip(f"container prep failed: {prep.stderr}")

    cwd = REPO_ROOT / "infra" / "ansible"
    first = subprocess.run(
        playbook,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    second = subprocess.run(
        playbook,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(["docker", "rm", "-f", container], check=False)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert _playbook_changed_count(second.stdout + second.stderr) == 0
