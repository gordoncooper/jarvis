"""k3s Ansible role: config render and guard proof (p0a-12)."""

from __future__ import annotations

import subprocess
import textwrap

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    CONTAINER_NAME,
    K3S_ROLE_TEMPLATES,
    REPO_ROOT,
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


def inventory_hostname() -> str:
    hosts = yaml.safe_load(inventory_path().read_text(encoding="utf-8"))
    all_hosts = hosts["all"]["hosts"]
    assert isinstance(all_hosts, dict) and all_hosts
    return next(iter(all_hosts))


def load_nodes() -> dict[str, object]:
    from ansible_role_proof import env_name

    path = REPO_ROOT / "envs" / env_name() / "values" / "nodes.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    nodes = data.get("nodes")
    assert isinstance(nodes, dict)
    return nodes


def render_k3s_config(variables: dict[str, object]) -> str:
    return ansible_render_template(
        K3S_CONFIG_TEMPLATE,
        variables,
        templates_dir=K3S_ROLE_TEMPLATES,
    )


def test_k3s_config_template_encodes_p0a12_rules() -> None:
    cluster = load_cluster_vars()
    hostname = inventory_hostname()
    nodes = load_nodes()
    variables = {
        "pod_cidr": cluster["pod_cidr"],
        "service_cidr": cluster["service_cidr"],
        "local_path_data": cluster["local_path_data"],
        "backup_path": cluster["backup_path"],
        "inventory_hostname": hostname,
        "nodes": nodes,
        "k3s_etcd_snapshot_schedule_cron": "0 */12 * * *",
        "k3s_etcd_snapshot_retention": 5,
        "k3s_kubelet_kube_reserved": cluster["k3s_kubelet_kube_reserved"],
        "k3s_kubelet_system_reserved": cluster["k3s_kubelet_system_reserved"],
        "k3s_kubelet_eviction_hard": cluster["k3s_kubelet_eviction_hard"],
    }
    rendered = render_k3s_config(variables)
    assert "cluster-init: true" in rendered
    assert "secrets-encryption: true" in rendered
    assert f"cluster-cidr: {cluster['pod_cidr']}" in rendered
    assert f"service-cidr: {cluster['service_cidr']}" in rendered
    assert "etcd-snapshot-dir:" in rendered
    assert "kube-reserved=" in rendered
    assert "system-reserved=" in rendered
    assert "eviction-hard=" in rendered
    assert not any(line.strip().startswith("INSTALL_K3S_") for line in rendered.splitlines())
    node_spec = nodes[hostname]
    assert isinstance(node_spec, dict)
    roles = node_spec.get("roles")
    assert isinstance(roles, list)
    for role in roles:
        assert f'role.jarvis.lan/{role}=true' in rendered


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


@pytest.mark.container
def test_k3s_guard_refuses_active_k3s_without_marker() -> None:
    require_docker()
    start_systemd_container()
    try:
        _install_fake_k3s_service(CONTAINER_NAME)
        result = subprocess.run(
            k3s_guard_playbook_cmd(),
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "k3s is already present" in result.stdout + result.stderr
    finally:
        stop_systemd_container()
