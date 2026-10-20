"""k3s Ansible role: config render and guard proof (p0a-12)."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    CONTAINER_NAME,
    K3S_ROLE_TEMPLATES,
    ansible_render_template,
    cluster_vars_playbook_path,
    docker_exec,
    inventory_path,
    k3s_guard_playbook_cmd,
    load_cluster_vars,
    require_docker,
    start_systemd_container,
    stop_systemd_container,
)

K3S_CONFIG_TEMPLATE = "config.yaml.j2"
K3S_KUBECONFIG_TASKS = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "ansible"
    / "roles"
    / "k3s"
    / "tasks"
    / "kubeconfig.yml"
)


def inventory_hostname() -> str:
    hosts = yaml.safe_load(inventory_path().read_text(encoding="utf-8"))
    all_hosts = hosts["all"]["hosts"]
    assert isinstance(all_hosts, dict) and all_hosts
    return next(iter(all_hosts))


def render_k3s_config(variables: dict[str, object]) -> str:
    return ansible_render_template(
        K3S_CONFIG_TEMPLATE,
        variables,
        templates_dir=K3S_ROLE_TEMPLATES,
    )


def expected_config_from_cluster_vars(cluster: dict[str, object]) -> str:
    schedule = cluster.get("k3s_etcd_snapshot_schedule_cron", "0 */12 * * *")
    retention = cluster.get("k3s_etcd_snapshot_retention", 5)
    host = inventory_hostname()
    backup = cluster["backup_path"]
    kube_reserved = cluster["k3s_kubelet_kube_reserved"]
    system_reserved = cluster["k3s_kubelet_system_reserved"]
    eviction_hard = cluster["k3s_kubelet_eviction_hard"]
    return textwrap.dedent(
        f"""\
        # Managed by Ansible (k3s role): server config (framework §8 step 4; not the k3s install script).
        cluster-init: true
        secrets-encryption: true
        cluster-cidr: {cluster["pod_cidr"]}
        service-cidr: {cluster["service_cidr"]}
        tls-san:
          - {host}
        default-local-storage-path: {cluster["local_path_data"]}
        etcd-snapshot-schedule-cron: "{schedule}"
        etcd-snapshot-retention: {retention}
        etcd-snapshot-dir: {backup}/etcd-snapshots
        kubelet-arg:
          - "kube-reserved={kube_reserved}"
          - "system-reserved={system_reserved}"
          - "eviction-hard={eviction_hard}"
        """
    )


def test_k3s_config_template_exact_content_env_01() -> None:
    cluster = load_cluster_vars()
    variables = {
        "pod_cidr": cluster["pod_cidr"],
        "service_cidr": cluster["service_cidr"],
        "local_path_data": cluster["local_path_data"],
        "backup_path": cluster["backup_path"],
        "inventory_hostname": inventory_hostname(),
        "k3s_etcd_snapshot_schedule_cron": "0 */12 * * *",
        "k3s_etcd_snapshot_retention": 5,
        "k3s_kubelet_kube_reserved": cluster["k3s_kubelet_kube_reserved"],
        "k3s_kubelet_system_reserved": cluster["k3s_kubelet_system_reserved"],
        "k3s_kubelet_eviction_hard": cluster["k3s_kubelet_eviction_hard"],
    }
    rendered = render_k3s_config(variables)
    assert rendered == expected_config_from_cluster_vars(cluster)
    assert not any(line.strip().startswith("INSTALL_K3S_") for line in rendered.splitlines())
    assert "cluster-init: true" in rendered
    assert "secrets-encryption: true" in rendered


@pytest.mark.parametrize(
    ("raw_cidr", "expected"),
    [
        ("10.42.0.0/16", "10.42.0.0/16"),
        ('"10.42.0.0/16"', "10.42.0.0/16"),
    ],
)
def test_k3s_config_template_quoted_cidr(raw_cidr: str, expected: str) -> None:
    loaded = yaml.safe_load(raw_cidr)
    assert isinstance(loaded, str)
    cluster = load_cluster_vars()
    variables = {
        "pod_cidr": loaded,
        "service_cidr": cluster["service_cidr"],
        "local_path_data": cluster["local_path_data"],
        "backup_path": cluster["backup_path"],
        "inventory_hostname": inventory_hostname(),
        "k3s_etcd_snapshot_schedule_cron": "0 */12 * * *",
        "k3s_etcd_snapshot_retention": 5,
        "k3s_kubelet_kube_reserved": cluster["k3s_kubelet_kube_reserved"],
        "k3s_kubelet_system_reserved": cluster["k3s_kubelet_system_reserved"],
        "k3s_kubelet_eviction_hard": cluster["k3s_kubelet_eviction_hard"],
    }
    rendered = render_k3s_config(variables)
    assert f"cluster-cidr: {expected}" in rendered


def test_site_yml_syntax_check_includes_k3s() -> None:
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


def _install_fake_k3s_service(container: str) -> None:
    unit = textwrap.dedent(
        """\
        [Unit]
        Description=Fake k3s for guard tests

        [Service]
        Type=simple
        ExecStart=/bin/sleep 3600
        Restart=no

        [Install]
        WantedBy=multi-user.target
        """
    )
    write = docker_exec(
        container,
        "bash",
        "-lc",
        "cat > /etc/systemd/system/k3s.service <<'EOF'\n"
        + unit
        + "EOF\n"
        "systemctl daemon-reload && systemctl start k3s",
    )
    assert write.returncode == 0, write.stderr
    active = docker_exec(container, "systemctl", "is-active", "k3s")
    assert active.stdout.strip() == "active", active.stdout + active.stderr


def _install_stopped_k3s_binary(container: str) -> None:
    prep = docker_exec(
        container,
        "bash",
        "-lc",
        "install -m 0755 /bin/true /usr/local/bin/k3s && mkdir -p /var/lib/rancher/k3s",
    )
    assert prep.returncode == 0, prep.stderr
    inactive = docker_exec(container, "systemctl", "is-active", "k3s")
    assert inactive.stdout.strip() != "active", inactive.stdout + inactive.stderr


def _run_k3s_guard() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        k3s_guard_playbook_cmd(),
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def test_k3s_guard_fresh_node_writes_marker() -> None:
    require_docker()
    start_systemd_container()
    try:
        result = _run_k3s_guard()
        assert result.returncode == 0, result.stdout + result.stderr
        marker = docker_exec(CONTAINER_NAME, "cat", "/etc/jarvis/env")
        assert marker.returncode == 0, marker.stderr
        assert marker.stdout.strip() == load_cluster_vars()["jarvis_env"]
    finally:
        stop_systemd_container()


def test_k3s_guard_refuses_active_k3s_without_marker() -> None:
    require_docker()
    start_systemd_container()
    try:
        _install_fake_k3s_service(CONTAINER_NAME)
        result = _run_k3s_guard()
        assert result.returncode != 0, result.stdout + result.stderr
        assert "k3s is already present" in result.stdout + result.stderr
    finally:
        stop_systemd_container()


def test_k3s_guard_refuses_stopped_k3s_without_marker() -> None:
    require_docker()
    start_systemd_container()
    try:
        _install_stopped_k3s_binary(CONTAINER_NAME)
        result = _run_k3s_guard()
        assert result.returncode != 0, result.stdout + result.stderr
        assert "k3s is already present" in result.stdout + result.stderr
    finally:
        stop_systemd_container()


def _kubeconfig_task_by_name(name: str) -> dict[str, object]:
    loaded = yaml.safe_load(K3S_KUBECONFIG_TASKS.read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    for entry in loaded:
        if not isinstance(entry, dict):
            continue
        block = entry.get("block")
        if isinstance(block, list):
            for task in block:
                if isinstance(task, dict) and task.get("name") == name:
                    return task
        always = entry.get("always")
        if isinstance(always, list):
            for task in always:
                if isinstance(task, dict) and task.get("name") == name:
                    return task
    raise AssertionError(f"task not found: {name}")


def test_kubeconfig_staging_tasks_never_report_changed() -> None:
    """Only Install kubeconfig may report changed on a re-run (staging is scratch work)."""
    staging_names = (
        "Create temp directory for kubeconfig staging",
        "Fetch kubeconfig from node",
        "Point kubeconfig server at inventory hostname",
        "Remove kubeconfig staging directory",
    )
    for name in staging_names:
        task = _kubeconfig_task_by_name(name)
        assert task.get("changed_when") is False, name
    install = _kubeconfig_task_by_name("Install kubeconfig for site.yml runner")
    assert "changed_when" not in install


def test_k3s_guard_allows_matching_marker() -> None:
    require_docker()
    env = str(load_cluster_vars()["jarvis_env"])
    start_systemd_container()
    try:
        seed = docker_exec(
            CONTAINER_NAME,
            "bash",
            "-lc",
            f"mkdir -p /etc/jarvis && printf '%s\\n' '{env}' > /etc/jarvis/env",
        )
        assert seed.returncode == 0, seed.stderr
        _install_fake_k3s_service(CONTAINER_NAME)
        result = _run_k3s_guard()
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        stop_systemd_container()
