"""Helpers for base-os Ansible role proof tests (container + template render)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from _core_names import sole_env_directory_name

REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = REPO_ROOT / "infra" / "ansible"
ROLE_DIR = ANSIBLE_DIR / "roles" / "base-os"
K3S_ROLE_DIR = ANSIBLE_DIR / "roles" / "k3s"
FLUX_OPERATOR_ROLE_DIR = ANSIBLE_DIR / "roles" / "flux-operator"
FLUX_OPERATOR_ROLE_TEMPLATES = FLUX_OPERATOR_ROLE_DIR / "templates"
FLUX_OPERATOR_ROLE_TASKS = FLUX_OPERATOR_ROLE_DIR / "tasks"
ROLE_TEMPLATES = ROLE_DIR / "templates"
K3S_ROLE_TEMPLATES = K3S_ROLE_DIR / "templates"
SYSTEMD_DOCKERFILE = REPO_ROOT / "tools/tests/fixtures/systemd-ubuntu"
SYSTEMD_IMAGE = "jarvis-base-os-systemd:test"
CONTAINER_NAME = "jarvis-base-os-idem-test"
# site.yml sets ansible_user from ssh_user; the systemd fixture connects as root.
DOCKER_PLAYBOOK_EXTRA_VARS: list[str] = ["-e", "ansible_user=root"]
CHRONY_INVALID_FIXTURE = REPO_ROOT / "tools/tests/fixtures/chrony-invalid.conf"
ROLE_TASKS = ROLE_DIR / "tasks" / "main.yml"

EXPECTED_20_AUTO_UPGRADES = textwrap.dedent(
    """\
    # Managed by Ansible (base-os): refresh package lists only; upgrades are run manually by the admin.
    # Deliberate departure from framework §4 unattended upgrades until project docs are updated.
    APT::Periodic::Update-Package-Lists "1";
    APT::Periodic::Download-Upgradeable-Packages "0";
    APT::Periodic::AutocleanInterval "0";
    APT::Periodic::Unattended-Upgrade "0";
    """
)


def ci_mode() -> bool:
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def require_docker() -> None:
    if docker_available():
        return
    message = "docker is required for base-os container proof tests"
    if ci_mode():
        pytest.fail(message)
    pytest.skip(message)


def env_name(repo_root: Path = REPO_ROOT) -> str:
    return sole_env_directory_name(repo_root)


def cluster_vars_playbook_path(repo_root: Path = REPO_ROOT) -> str:
    return f"../../envs/{env_name(repo_root)}/values/cluster-vars.yaml"


def inventory_path(repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / "envs" / env_name(repo_root) / "inventory" / "hosts.yml"


def load_cluster_vars(repo_root: Path = REPO_ROOT) -> dict[str, object]:
    path = (
        repo_root
        / "envs"
        / env_name(repo_root)
        / "values"
        / "cluster-vars.yaml"
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def chrony_template_validate_command() -> str:
    """Return the role's chrony.conf template validate= command (single source of truth)."""
    tasks = yaml.safe_load(ROLE_TASKS.read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("name") != "Install chrony configuration with NTS":
            continue
        module = task.get("ansible.builtin.template")
        assert isinstance(module, dict), task
        validate = module.get("validate")
        assert isinstance(validate, str) and validate
        return validate
    raise AssertionError("chrony template task with validate not found in role tasks")


def ansible_render_template(
    template_name: str,
    variables: dict[str, object],
    *,
    templates_dir: Path | None = None,
) -> str:
    """Render a role template via ansible.builtin.template (same engine as the role)."""
    src = (templates_dir or ROLE_TEMPLATES) / template_name
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        dest = Path(handle.name)
    extra: list[str] = []
    if variables:
        extra.extend(["-e", json.dumps(variables)])
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    run = subprocess.run(
        [
            "ansible",
            "localhost",
            "-c",
            "local",
            "-m",
            "ansible.builtin.template",
            "-a",
            f"src={src} dest={dest}",
            *extra,
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    try:
        if run.returncode != 0:
            raise AssertionError(run.stdout + run.stderr)
        return dest.read_text(encoding="utf-8")
    finally:
        dest.unlink(missing_ok=True)


def build_systemd_image() -> str:
    require_docker()
    build = subprocess.run(
        [
            "docker",
            "build",
            "--build-arg",
            "UBUNTU_VERSION=26.04",
            "-t",
            SYSTEMD_IMAGE,
            str(SYSTEMD_DOCKERFILE),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        detail = f"{build.stdout}\n{build.stderr}"
        if ci_mode():
            pytest.fail(f"failed to build Ubuntu 26.04 systemd test image:\n{detail}")
        pytest.skip(f"failed to build Ubuntu 26.04 systemd test image:\n{detail}")
    return SYSTEMD_IMAGE


def start_systemd_container(name: str = CONTAINER_NAME) -> str:
    image = build_systemd_image()
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--privileged",
            "--cgroupns=host",
            "-v",
            "/sys/fs/cgroup:/sys/fs/cgroup:rw",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/run/lock",
            image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        if ci_mode():
            pytest.fail(f"docker run failed: {run.stderr}")
        pytest.skip(f"docker run failed: {run.stderr}")

    deadline = time.time() + 120
    while time.time() < deadline:
        wait = subprocess.run(
            [
                "docker",
                "exec",
                name,
                "systemctl",
                "is-system-running",
                "--wait",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        state = wait.stdout.strip()
        if state in {"running", "degraded"}:
            _prepare_container_for_ansible(name)
            return name
        time.sleep(2)

    logs = subprocess.run(
        ["docker", "logs", name],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(["docker", "rm", "-f", name], check=False)
    if ci_mode():
        pytest.fail(
            "systemd did not reach running/degraded in time:\n"
            f"{logs.stdout}\n{logs.stderr}"
        )
    pytest.skip("systemd did not become ready in the test container")


def _prepare_container_for_ansible(name: str) -> None:
    prep = docker_exec(
        name,
        "bash",
        "-lc",
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq",
    )
    if prep.returncode != 0:
        stop_systemd_container(name)
        if ci_mode():
            pytest.fail(f"container apt update failed: {prep.stderr}")
        pytest.skip(f"container apt update failed: {prep.stderr}")


def stop_systemd_container(name: str = CONTAINER_NAME) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def playbook_cmd() -> list[str]:
    return [
        "ansible-playbook",
        "site.yml",
        "-i",
        f"{CONTAINER_NAME},",
        "-e",
        f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
        "-e",
        "ansible_connection=docker",
        *DOCKER_PLAYBOOK_EXTRA_VARS,
        "--skip-tags",
        "chrony-service,k3s,k3s-service,flux-cluster",
    ]


def flux_operator_check_playbook_cmd(container: str = CONTAINER_NAME) -> list[str]:
    return [
        "ansible-playbook",
        "site.yml",
        "-i",
        f"{container},",
        "-e",
        f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
        "-e",
        "ansible_connection=docker",
        *DOCKER_PLAYBOOK_EXTRA_VARS,
        "--check",
        "--skip-tags",
        "chrony-service,k3s,k3s-service",
    ]


def k3s_guard_playbook_cmd(container: str = CONTAINER_NAME) -> list[str]:
    return [
        "ansible-playbook",
        "site.yml",
        "-i",
        f"{container},",
        "-e",
        f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
        "-e",
        "ansible_connection=docker",
        *DOCKER_PLAYBOOK_EXTRA_VARS,
        "--tags",
        "k3s-guard",
    ]


def run_playbook() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        playbook_cmd(),
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def playbook_recap_field(output: str, field: str) -> int:
    recap_lines = [
        line
        for line in output.splitlines()
        if f"{field}=" in line and "PLAY RECAP" not in line
    ]
    assert recap_lines, output
    match = re.search(rf"{field}=(\d+)", recap_lines[-1])
    assert match is not None, output
    return int(match.group(1))


def docker_exec(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", name, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def assert_apt_timer_states(container: str) -> None:
    daily_enabled = docker_exec(container, "systemctl", "is-enabled", "apt-daily.timer")
    assert daily_enabled.stdout.strip() == "enabled", (
        daily_enabled.stdout + daily_enabled.stderr
    )

    upgrade_enabled = docker_exec(
        container,
        "systemctl",
        "is-enabled",
        "apt-daily-upgrade.timer",
    )
    assert upgrade_enabled.stdout.strip() == "disabled", (
        upgrade_enabled.stdout + upgrade_enabled.stderr
    )
    upgrade_active = docker_exec(
        container,
        "systemctl",
        "is-active",
        "apt-daily-upgrade.timer",
    )
    assert upgrade_active.stdout.strip() in {"inactive", "failed"}, (
        upgrade_active.stdout + upgrade_active.stderr
    )


def assert_20_auto_upgrades_file(container: str) -> None:
    content = docker_exec(
        container,
        "cat",
        "/etc/apt/apt.conf.d/20auto-upgrades",
    )
    assert content.returncode == 0, content.stderr
    assert content.stdout == EXPECTED_20_AUTO_UPGRADES


def assert_localtime_matches_zone(container: str, time_zone: str) -> None:
    link = docker_exec(container, "readlink", "-f", "/etc/localtime")
    assert link.returncode == 0, link.stderr
    expected = f"/usr/share/zoneinfo/{time_zone}"
    assert link.stdout.strip() == expected, link.stdout
