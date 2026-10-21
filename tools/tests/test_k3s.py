"""k3s Ansible role: config render and guard proof (p0a-12)."""

from __future__ import annotations

import json
import subprocess
import textwrap

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    CONTAINER_NAME,
    K3S_ROLE_DIR,
    K3S_ROLE_TEMPLATES,
    REPO_ROOT,
    ansible_render_template,
    docker_exec,
    env_name,
    inventory_path,
    non_example_env_names,
    k3s_guard_playbook_cmd,
    load_cluster_vars,
    require_docker,
    roles_inventory_path,
    start_systemd_container,
    stop_systemd_container,
)
from conftest import require_cli

K3S_CONFIG_TEMPLATE = "config.yaml.j2"


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


def test_k3s_config_template_encodes_p0a12_rules() -> None:
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
    assert "cluster-init: true" in rendered
    assert "secrets-encryption: true" in rendered
    assert f"cluster-cidr: {cluster['pod_cidr']}" in rendered
    assert f"service-cidr: {cluster['service_cidr']}" in rendered
    assert "etcd-snapshot-dir:" in rendered
    assert "kube-reserved=" in rendered
    assert "system-reserved=" in rendered
    assert "eviction-hard=" in rendered
    assert f"node-name: {inventory_hostname()}" in rendered
    assert "node-label" not in rendered
    assert not any(line.strip().startswith("INSTALL_K3S_") for line in rendered.splitlines())


def test_agent_config_template_encodes_join_rules() -> None:
    rendered = ansible_render_template(
        "agent-config.yaml.j2",
        {
            "k3s_server_url": "https://cp-1:6443",
            "k3s_token": "fixture-token",
            "inventory_hostname": "wk-1",
        },
        templates_dir=K3S_ROLE_TEMPLATES,
    )
    assert "server: https://cp-1:6443" in rendered
    assert "token: fixture-token" in rendered
    assert "node-name: wk-1" in rendered
    assert "cluster-init" not in rendered
    assert "node-label" not in rendered


def test_agent_token_slurp_is_delegated_and_hidden() -> None:
    tasks = yaml.safe_load((K3S_ROLE_DIR / "tasks" / "token.yml").read_text(encoding="utf-8"))
    slurp = next(task for task in tasks if "ansible.builtin.slurp" in task)
    assert slurp["no_log"] is True
    assert slurp["run_once"] is True
    assert "control_plane" in slurp["delegate_to"]
    config = next(
        task
        for task in yaml.safe_load((K3S_ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
        if task.get("name") == "Install k3s agent config"
    )
    assert config["no_log"] is True


def _agent_play_section(env: str) -> str:
    require_cli("ansible-playbook")
    proc = subprocess.run(
        [
            "ansible-playbook",
            "site.yml",
            "--list-hosts",
            "-i",
            str(REPO_ROOT / "envs" / env / "inventory" / "hosts.yml"),
            "-i",
            str(roles_inventory_path()),
            "-e",
            f"jarvis_cluster_vars=../../envs/{env}/values/cluster-vars.yaml",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    parts = proc.stdout.split("play #")
    section = next((part for part in parts if "Join k3s agents" in part), "")
    assert section, proc.stdout
    return section


def test_agent_play_host_pattern() -> None:
    assert "hosts (6):" in _agent_play_section(env_name())
    real_envs = non_example_env_names()
    assert real_envs
    for env in real_envs:
        assert "hosts (0):" in _agent_play_section(env)


def test_label_patch_reconciles_roles_and_enablelb() -> None:
    nodes = [
        {
            "metadata": {
                "name": "cp-1",
                "labels": {
                    "role.env-00.example/control_plane": "true",
                    "role.env-00.example/workers": "true",
                    "svccontroller.k3s.cattle.io/enablelb": "true",
                    "kubernetes.io/os": "linux",
                },
            }
        },
        {
            "metadata": {
                "name": "wk-1",
                "labels": {"kubernetes.io/hostname": "wk-1"},
            }
        },
    ]
    hosts = {"cp-1": ["control_plane"], "wk-1": ["workers", "edge"]}

    def render(edge: list[str]) -> list[dict[str, object]]:
        rendered = ansible_render_template(
            "label-patches.json.j2",
            {
                "dns_suffix": "env-00.example",
                "k3s_edge_group": edge,
                "k3s_control_plane_hosts": ["cp-1"],
                "k3s_label_hosts": hosts,
                "k3s_node_resources": nodes,
            },
            templates_dir=K3S_ROLE_TEMPLATES,
        )
        loaded = json.loads(rendered)
        assert isinstance(loaded, list)
        return loaded

    edged = {item["name"]: item["labels"] for item in render(["wk-1"])}
    assert edged["cp-1"]["role.env-00.example/workers"] is None
    assert edged["cp-1"]["svccontroller.k3s.cattle.io/enablelb"] is None
    assert "role.env-00.example/control_plane" not in edged["cp-1"]
    assert "kubernetes.io/os" not in edged["cp-1"]
    assert edged["wk-1"]["role.env-00.example/workers"] == "true"
    assert edged["wk-1"]["role.env-00.example/edge"] == "true"
    assert edged["wk-1"]["svccontroller.k3s.cattle.io/enablelb"] == "true"
    assert "kubernetes.io/hostname" not in edged["wk-1"]

    bare = [
        {
            "metadata": {
                "name": "cp-1",
                "labels": {"role.env-00.example/control_plane": "true"},
            }
        },
        {
            "metadata": {
                "name": "wk-1",
                "labels": {"svccontroller.k3s.cattle.io/enablelb": "true"},
            }
        },
    ]
    rendered = ansible_render_template(
        "label-patches.json.j2",
        {
            "dns_suffix": "env-00.example",
            "k3s_edge_group": [],
            "k3s_control_plane_hosts": ["cp-1"],
            "k3s_label_hosts": hosts,
            "k3s_node_resources": bare,
        },
        templates_dir=K3S_ROLE_TEMPLATES,
    )
    fallback = {item["name"]: item["labels"] for item in json.loads(rendered)}
    assert fallback["cp-1"]["svccontroller.k3s.cattle.io/enablelb"] == "true"
    assert fallback["wk-1"]["svccontroller.k3s.cattle.io/enablelb"] is None


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
