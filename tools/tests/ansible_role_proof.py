"""Helpers for Ansible container proof tests and template renders."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from conftest import ci_mode
from services.hermes.core.schema.env import EXAMPLE_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = REPO_ROOT / "infra" / "ansible"
K3S_ROLE_DIR = ANSIBLE_DIR / "roles" / "k3s"
FLUX_OPERATOR_ROLE_DIR = ANSIBLE_DIR / "roles" / "flux-operator"
FLUX_OPERATOR_ROLE_TEMPLATES = FLUX_OPERATOR_ROLE_DIR / "templates"
FLUX_OPERATOR_ROLE_TASKS = FLUX_OPERATOR_ROLE_DIR / "tasks"
K3S_ROLE_TEMPLATES = K3S_ROLE_DIR / "templates"
SYSTEMD_DOCKERFILE = REPO_ROOT / "tools/tests/fixtures/systemd-ubuntu"
SYSTEMD_IMAGE = "jarvis-base-os-systemd:test"
CONTAINER_NAME = "jarvis-base-os-idem-test"
DOCKER_PLAYBOOK_EXTRA_VARS: list[str] = ["-e", "ansible_user=root"]


def _fail_or_skip(message: str, detail: str = "") -> None:
    text = f"{message}:\n{detail}" if detail else message
    if ci_mode():
        pytest.fail(text)
    pytest.skip(text)


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
    _fail_or_skip("docker is required for container proof tests")


def env_name(repo_root: Path = REPO_ROOT) -> str:
    # Role proofs always use the example env. repo_root stays for callers that pass it.
    _ = repo_root
    return EXAMPLE_ENV


def non_example_env_names(repo_root: Path = REPO_ROOT) -> list[str]:
    envs = repo_root / "envs"
    return sorted(
        path.name
        for path in envs.iterdir()
        if path.is_dir() and path.name != EXAMPLE_ENV
    )


def cluster_vars_playbook_path(repo_root: Path = REPO_ROOT) -> str:
    return f"../../envs/{env_name(repo_root)}/values/cluster-vars.yaml"


def inventory_path(repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / "envs" / env_name(repo_root) / "inventory" / "hosts.yml"


def load_cluster_vars(
    repo_root: Path = REPO_ROOT, *, env: str | None = None
) -> dict[str, object]:
    name = env_name(repo_root) if env is None else env
    path = repo_root / "envs" / name / "values" / "cluster-vars.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def ansible_render_template(
    template_name: str,
    variables: dict[str, object],
    *,
    templates_dir: Path,
) -> str:
    """Render a role template via ansible.builtin.template (same engine as the role)."""
    src = templates_dir / template_name
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
        _fail_or_skip(
            "failed to build Ubuntu 26.04 systemd test image",
            f"{build.stdout}\n{build.stderr}",
        )
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
        _fail_or_skip("docker run failed", run.stderr)

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
    _fail_or_skip(
        "systemd did not reach running/degraded in time",
        f"{logs.stdout}\n{logs.stderr}",
    )


def _prepare_container_for_ansible(name: str) -> None:
    prep = docker_exec(
        name,
        "bash",
        "-lc",
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq",
    )
    if prep.returncode != 0:
        stop_systemd_container(name)
        _fail_or_skip("container apt update failed", prep.stderr)


def stop_systemd_container(name: str = CONTAINER_NAME) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _playbook_cmd(
    container: str,
    *,
    extra_args: list[str],
) -> list[str]:
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
        *extra_args,
    ]


def flux_operator_check_playbook_cmd(container: str = CONTAINER_NAME) -> list[str]:
    return _playbook_cmd(
        container,
        extra_args=[
            "--check",
            "--skip-tags",
            "chrony-service,k3s,k3s-service",
        ],
    )


def k3s_guard_playbook_cmd(container: str = CONTAINER_NAME) -> list[str]:
    return _playbook_cmd(
        container,
        extra_args=["--tags", "k3s-guard"],
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
