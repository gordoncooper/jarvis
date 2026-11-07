"""k3s Ansible role: config render and guard proof."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    K3S_ROLE_DIR,
    K3S_ROLE_TEMPLATES,
    REPO_ROOT,
    ansible_render_template,
    env_name,
    inventory_path,
    non_example_env_names,
    load_cluster_vars,
    load_role_defaults,
    playbook_recap_field,
    roles_inventory_path,
)
from conftest import require_cli
from support import ExampleCopy, _kustomize_build

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
    defaults = load_role_defaults(K3S_ROLE_DIR)
    cron = defaults["k3s_etcd_snapshot_schedule_cron"]
    retention = defaults["k3s_etcd_snapshot_retention"]
    variables = {
        "pod_cidr": cluster["pod_cidr"],
        "service_cidr": cluster["service_cidr"],
        "local_path_data": cluster["local_path_data"],
        "backup_path": cluster["backup_path"],
        "inventory_hostname": inventory_hostname(),
        "k3s_etcd_snapshot_schedule_cron": cron,
        "k3s_etcd_snapshot_retention": retention,
        "k3s_kubelet_kube_reserved": cluster["k3s_kubelet_kube_reserved"],
        "k3s_kubelet_system_reserved": cluster["k3s_kubelet_system_reserved"],
        "k3s_kubelet_eviction_hard": cluster["k3s_kubelet_eviction_hard"],
        "k3s_server_token": "fixture-server",
        "k3s_agent_token": "fixture-agent",
        "ansible_memtotal_mb": 16384,
    }
    rendered = render_k3s_config(variables)
    assert 'token: "fixture-server"' in rendered.splitlines()
    assert 'agent-token: "fixture-agent"' in rendered.splitlines()
    assert "cluster-init: true" in rendered
    assert "secrets-encryption: true" in rendered
    assert f"cluster-cidr: {cluster['pod_cidr']}" in rendered
    assert f"service-cidr: {cluster['service_cidr']}" in rendered
    assert f'etcd-snapshot-schedule-cron: "{cron}"' in rendered
    assert f"etcd-snapshot-retention: {retention}" in rendered
    assert "etcd-snapshot-dir:" in rendered
    assert "kube-reserved=" in rendered
    assert "system-reserved=" in rendered
    assert "eviction-hard=" in rendered
    assert f"node-name: {inventory_hostname()}" in rendered
    assert "node-label" not in rendered
    assert not any(line.strip().startswith("INSTALL_K3S_") for line in rendered.splitlines())


def test_agent_config_template_encodes_join_rules() -> None:
    cluster = load_cluster_vars()
    rendered = ansible_render_template(
        "agent-config.yaml.j2",
        {
            "k3s_server_url": "https://cp-1:6443",
            "k3s_agent_token": "fixture-agent",
            "k3s_server_token": "fixture-server",
            "inventory_hostname": "wk-1",
            "k3s_kubelet_kube_reserved": cluster["k3s_kubelet_kube_reserved"],
            "k3s_kubelet_system_reserved": cluster["k3s_kubelet_system_reserved"],
            "k3s_kubelet_eviction_hard": cluster["k3s_kubelet_eviction_hard"],
            "node_memory_reserve_percent": cluster["node_memory_reserve_percent"],
            "ansible_memtotal_mb": 16384,
        },
        templates_dir=K3S_ROLE_TEMPLATES,
    )
    assert "server: https://cp-1:6443" in rendered
    assert 'token: "fixture-agent"' in rendered.splitlines()
    assert "fixture-server" not in rendered and "agent-token" not in rendered
    assert "node-name: wk-1" in rendered
    assert f"kube-reserved={cluster['k3s_kubelet_kube_reserved']}" in rendered
    assert f"system-reserved={cluster['k3s_kubelet_system_reserved']}" in rendered
    assert f"eviction-hard=memory.available<{cluster['node_memory_reserve_percent']}%" in rendered
    assert "cluster-init" not in rendered
    assert "node-label" not in rendered
    assert "node-ip" not in rendered


def test_host_overrides_win_on_that_host_only(tmp_path: Path) -> None:
    """A per-host ssh_user and kubelet reserves apply on that host only; the env's memory
    reserve applies to both, and a host's larger memory.available stays."""
    require_cli("ansible-playbook")
    cluster = load_cluster_vars()
    defaults = load_role_defaults(K3S_ROLE_DIR)
    expr = yaml.safe_load((ANSIBLE_DIR / "site.yml").read_text())[0]["vars"]["ansible_user"]
    hosts = tmp_path / "hosts.yml"
    # over: 250Mi is more than 10% of 2 GiB, so it stays; plain: 10% of 16 GiB beats 100Mi.
    hosts.write_text(
        "all:\n  hosts:\n    over:\n      node_roles: [control_plane, backup, edge]\n"
        "      ssh_user: alice\n      k3s_kubelet_kube_reserved: cpu=50m\n"
        "      k3s_kubelet_system_reserved: cpu=25m\n"
        "      k3s_kubelet_eviction_hard: memory.available<250Mi\n"
        "      ansible_memtotal_mb: 2048\n"
        "    plain:\n      node_roles: [workers]\n      ansible_memtotal_mb: 16384\n"
    )
    dest = tmp_path / "out"
    dest.mkdir()
    cluster_vars = REPO_ROOT / "envs" / env_name() / "values" / "cluster-vars.yaml"
    play = tmp_path / "play.yml"
    play.write_text(
        "---\n- hosts: all\n  gather_facts: false\n  become: false\n  vars:\n"
        f"    ansible_user: \"{expr}\"\n    ansible_connection: local\n"
        f"    k3s_etcd_snapshot_schedule_cron: {defaults['k3s_etcd_snapshot_schedule_cron']!r}\n"
        f"    k3s_etcd_snapshot_retention: {defaults['k3s_etcd_snapshot_retention']}\n"
        "    k3s_server_url: https://cp-1:6443\n    k3s_agent_token: fixture-agent\n"
        "    k3s_server_token: fixture-server\n"
        f"  vars_files: [{cluster_vars}]\n  tasks:\n"
        f"    - ansible.builtin.template:\n        src: {K3S_ROLE_TEMPLATES / K3S_CONFIG_TEMPLATE}\n"
        f"        dest: {dest}/{{{{ inventory_hostname }}}}.yaml\n"
        f"    - ansible.builtin.template:\n        src: {K3S_ROLE_TEMPLATES / 'agent-config.yaml.j2'}\n"
        f"        dest: {dest}/{{{{ inventory_hostname }}}}.agent.yaml\n"
        f"    - ansible.builtin.copy:\n        dest: {dest}/{{{{ inventory_hostname }}}}.user\n"
        "        content: '{{ ansible_user }}'\n"
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    proc = subprocess.run(
        ["ansible-playbook", str(play), "-i", str(hosts), "-i", str(roles_inventory_path())],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    over = [(dest / f"over{s}").read_text() for s in (".yaml", ".agent.yaml")]
    plain = [(dest / f"plain{s}").read_text() for s in (".yaml", ".agent.yaml")]
    assert cluster["k3s_kubelet_eviction_hard"] != "memory.available<250Mi"
    assert (dest / "over.user").read_text() == "alice"
    assert (dest / "plain.user").read_text() == cluster["ssh_user"]
    for rendered in over:
        assert "kube-reserved=cpu=50m" in rendered
        assert "system-reserved=cpu=25m" in rendered
        assert "eviction-hard=memory.available<250Mi" in rendered
    for rendered in plain:
        assert f"kube-reserved={cluster['k3s_kubelet_kube_reserved']}" in rendered
        assert f"system-reserved={cluster['k3s_kubelet_system_reserved']}" in rendered
        assert "eviction-hard=memory.available<10%" in rendered


def test_agent_fails_when_control_plane_name_does_not_resolve(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        textwrap.dedent(
            """\
            all:
              children:
                control_plane:
                  hosts:
                    cp.invalid:
                agents:
                  hosts:
                    wk-1:
                      ansible_connection: local
            """
        ),
        encoding="utf-8",
    )
    play = tmp_path / "play.yml"
    token_tasks = K3S_ROLE_DIR / "tasks" / "token.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: Agent join preconditions
              hosts: wk-1
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_tasks: {token_tasks}
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    proc = subprocess.run(
        ["ansible-playbook", str(play), "-i", str(inventory), "-e", "k3s_mode=agent"],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "cp.invalid does not resolve on wk-1" in output


def _agent_play_section(env: str, root: Path | None = None) -> str:
    require_cli("ansible-playbook")
    repo = REPO_ROOT if root is None else root
    proc = subprocess.run(
        [
            "ansible-playbook",
            "site.yml",
            "--list-hosts",
            "-i",
            str(repo / "envs" / env / "inventory" / "hosts.yml"),
            "-i",
            str(roles_inventory_path()),
            "-e",
            f"jarvis_cluster_vars={repo / 'envs' / env / 'values' / 'cluster-vars.yaml'}",
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


def _hosts_outside_control_plane(env: str, root: Path | None = None) -> set[str]:
    repo = REPO_ROOT if root is None else root
    doc = yaml.safe_load(
        (repo / "envs" / env / "inventory" / "hosts.yml").read_text(encoding="utf-8")
    )
    hosts = doc["all"]["hosts"]
    return {
        name
        for name, host_vars in hosts.items()
        if "control_plane" not in ((host_vars or {}).get("node_roles") or [])
    }


def _listed_play_hosts(section: str) -> set[str]:
    listed: list[str] = []
    capture = False
    for line in section.splitlines():
        header = re.search(r"hosts \(\d+\):\s*(.*)$", line)
        if header:
            capture = True
            inline = header.group(1).strip()
            if inline:
                listed.extend(part.strip() for part in inline.split(",") if part.strip())
            continue
        if not capture:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"[A-Za-z0-9._-]+", stripped):
            listed.append(stripped)
            continue
        break
    return set(listed)


def test_agent_play_restarts_one_host_at_a_time() -> None:
    """A k3s pin bump restarts one agent before the next."""
    plays = yaml.safe_load((ANSIBLE_DIR / "site.yml").read_text(encoding="utf-8"))
    agent = next(play for play in plays if play.get("name") == "Join k3s agents")
    assert agent["serial"] == 1


def test_agent_play_host_pattern(example_copy: ExampleCopy) -> None:
    """The agent play is every host except the control plane."""
    targets = [(REPO_ROOT, env) for env in (env_name(), *non_example_env_names())]
    targets.append((example_copy.root, example_copy.name))
    for root, env in targets:
        assert _listed_play_hosts(_agent_play_section(env, root)) == _hosts_outside_control_plane(
            env, root
        )


def test_k3s_download_checksum_is_k3s_sha256() -> None:
    doc = yaml.safe_load((K3S_ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
    task = next(
        item
        for item in doc
        if isinstance(item, dict) and "ansible.builtin.get_url" in item
    )
    assert task["ansible.builtin.get_url"]["checksum"] == "sha256:{{ k3s_sha256 }}"


def _label_inventory(edge_hosts: list[str]) -> str:
    if edge_hosts:
        hosts = "\n".join(f"        {name}:" for name in edge_hosts)
        edge = f"      hosts:\n{hosts}\n"
    else:
        edge = "      hosts: {}\n"
    return (
        "all:\n"
        "  children:\n"
        "    edge:\n"
        f"{edge}"
        "    local:\n"
        "      hosts:\n"
        "        localhost:\n"
        "          ansible_connection: local\n"
    )


def _placement_blocked():
    import importlib.util

    loaded = importlib.util.spec_from_file_location(
        "k3s_placement", K3S_ROLE_DIR / "filter_plugins" / "placement.py"
    )
    assert loaded is not None and loaded.loader is not None
    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    return module.placement_blocked


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

    def render(
        edge_hosts: list[str],
        node_resources: list[dict[str, object]],
        label_hosts: dict[str, list[str]],
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Path(tmp) / "hosts.yml"
            inventory.write_text(_label_inventory(edge_hosts), encoding="utf-8")
            rendered = ansible_render_template(
                "label-patches.json.j2",
                {
                    "dns_suffix": "env-00.example",
                    "k3s_label_hosts": label_hosts,
                    "k3s_node_resources": node_resources,
                },
                templates_dir=K3S_ROLE_TEMPLATES,
                inventory=inventory,
            )
        loaded = json.loads(rendered)
        assert isinstance(loaded, list)
        return loaded

    edged = {item["name"]: item["labels"] for item in render(["wk-1"], nodes, hosts)}
    assert edged["cp-1"]["node-role.jarvis.io/control_plane"] == "true"
    assert edged["cp-1"]["role.env-00.example/control_plane"] is None
    assert edged["cp-1"]["role.env-00.example/workers"] is None
    assert edged["cp-1"]["svccontroller.k3s.cattle.io/enablelb"] is None
    assert "kubernetes.io/os" not in edged["cp-1"]
    assert edged["wk-1"]["node-role.jarvis.io/workers"] == "true"
    assert edged["wk-1"]["node-role.jarvis.io/edge"] == "true"
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
    no_edge = {item["name"]: item["labels"] for item in render([], bare, hosts)}
    assert "svccontroller.k3s.cattle.io/enablelb" not in no_edge["cp-1"]
    assert no_edge["wk-1"]["svccontroller.k3s.cattle.io/enablelb"] is None

    dedicated = {"key": "node-role.jarvis.io/dedicated", "value": "true", "effect": "NoSchedule"}
    hold = {"key": "example.com/hold", "value": "true", "effect": "NoSchedule"}
    roles = {"edge-only": ["edge"], "workers-only": ["workers"], "gpu-only": ["gpu"]}
    nodes = [
        {
            "metadata": {"name": name, "labels": {}},
            "spec": {"taints": [hold, dedicated] if name == "workers-only" else [hold]},
        }
        for name in roles
    ]
    first = {item["name"]: item for item in render(["edge-only"], nodes, roles)}
    assert [first[name]["taint"] for name in roles] == [None, "absent", "present"]
    assert "example.com/hold" not in json.dumps(first)
    from ansible_collections.kubernetes.core.plugins.modules.k8s_taint import _get_difference

    task = next(
        item["kubernetes.core.k8s_taint"]
        for item in yaml.safe_load((K3S_ROLE_DIR / "tasks" / "labels.yml").read_text())
        if "kubernetes.core.k8s_taint" in item
    )
    assert task["taints"] == [dedicated] and "replace" not in task
    assert _get_difference([hold, dedicated], task["taints"]) == [hold]


def test_placement_gate_stops_on_an_old_live_spec() -> None:
    """A missing workload passes. A live one without the new term or toleration blocks."""
    blocked = _placement_blocked()

    def live(kind: str, spec: dict) -> dict:
        nest = {"spec": spec}
        body = (
            {"spec": {"template": nest}}
            if kind == "Deployment"
            else {"spec": {"jobTemplate": {"spec": {"template": nest}}}}
        )
        return {"item": {"kind": kind, "name": "restic", "role": "backup"}, "resources": [body]}

    term = {"key": "node-role.jarvis.io/backup", "operator": "In", "values": ["true"]}
    affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [term]}]}}}
    tol = {"key": "node-role.jarvis.io/dedicated", "value": "true", "effect": "NoSchedule"}
    assert blocked([{"item": live("Deployment", {})["item"], "resources": []}]) == []
    assert blocked([live("Deployment", {})]) == ["Deployment restic"]
    assert blocked([live("Deployment", {"affinity": affinity, "tolerations": [tol]})]) == []
    assert blocked([live("CronJob", {"affinity": affinity})]) == ["CronJob restic"]


def test_relabel_precheck_runs_before_the_writes() -> None:
    """Read and stop come first, and they name the built workloads, including each role."""
    tasks = yaml.safe_load((K3S_ROLE_DIR / "tasks" / "labels.yml").read_text(encoding="utf-8"))
    found: dict[str, int] = {}
    for index, task in enumerate(tasks):
        action = next(key for key in task if key.startswith(("ansible.builtin.", "kubernetes.core.")))
        if action == "kubernetes.core.k8s_info" and "loop" in task:
            found["read"] = index
        elif action == "ansible.builtin.fail":
            found["stop"] = index
        elif action == "kubernetes.core.k8s":
            found["label"] = index
        elif action == "kubernetes.core.k8s_taint":
            found["taint"] = index
    read, stop, label, taint = (found[name] for name in ("read", "stop", "label", "taint"))
    assert read < stop < label and stop < taint
    info = tasks[read]["kubernetes.core.k8s_info"]
    loop = {(item["kind"], item["name"]): item["role"] for item in tasks[read]["loop"]}
    built: dict[tuple[str, str], dict] = {}
    for doc in yaml.safe_load_all(_kustomize_build("platform/backups")):
        if isinstance(doc, dict) and doc.get("kind") in {"Deployment", "CronJob"}:
            meta = doc["metadata"]
            assert meta["namespace"] == info["namespace"]
            built[(doc["kind"], meta["name"])] = doc
    assert set(loop) == set(built)
    assert "placement_blocked" in tasks[stop]["vars"]["blocked"]
    blocked = _placement_blocked()
    for key, role in loop.items():
        assert f"node-role.jarvis.io/{role}" in json.dumps(built[key]["spec"])
        assert blocked([{"item": {"kind": key[0], "name": key[1], "role": role}, "resources": [built[key]]}]) == []


def test_converged_node_renders_nothing() -> None:
    """A second run, once labels and the dedicated taint match, renders nothing."""
    dedicated = {"key": "node-role.jarvis.io/dedicated", "value": "true", "effect": "NoSchedule"}
    roles = {"edge-only": ["edge"], "gpu-only": ["gpu"]}
    nodes = [
        {"metadata": {"name": "edge-only", "labels": {"node-role.jarvis.io/edge": "true", "svccontroller.k3s.cattle.io/enablelb": "true"}}, "spec": {"taints": []}},
        {"metadata": {"name": "gpu-only", "labels": {"node-role.jarvis.io/gpu": "true"}}, "spec": {"taints": [dedicated]}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        inventory = Path(tmp) / "hosts.yml"
        inventory.write_text(_label_inventory(["edge-only"]), encoding="utf-8")
        rendered = ansible_render_template(
            "label-patches.json.j2",
            {"dns_suffix": "env-00.example", "k3s_label_hosts": roles, "k3s_node_resources": nodes},
            templates_dir=K3S_ROLE_TEMPLATES,
            inventory=inventory,
        )
    assert all(item["labels"] == {} and item["taint"] is None for item in json.loads(rendered))


def _run_guard(tmp_path: Path, *, marker_text: str | None) -> subprocess.CompletedProcess[str]:
    """refuse.yml on a host where systemctl says k3s is active, with an optional env marker."""
    require_cli("ansible-playbook")
    marker = tmp_path / "marker"
    if marker_text is not None:
        marker.write_text(marker_text, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\necho active\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)
    play = tmp_path / "play.yml"
    guard = K3S_ROLE_DIR / "tasks" / "refuse.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: k3s guard
              hosts: localhost
              gather_facts: false
              become: false
              environment:
                PATH: "{fake_bin}:/usr/bin:/bin"
              tasks:
                - ansible.builtin.import_tasks: {guard}
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    return subprocess.run(
        [
            "ansible-playbook",
            str(play),
            "-i",
            "localhost,",
            "-e",
            "ansible_connection=local",
            "-e",
            "k3s_service_name=k3s",
            "-e",
            "jarvis_env=fixture",
            "-e",
            f"k3s_binary_path={tmp_path / 'missing-k3s'}",
            "-e",
            f"k3s_data_dir={tmp_path / 'missing-data'}",
            "-e",
            f"jarvis_marker_path={marker}",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_k3s_guard_refuses_active_k3s_without_marker(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, marker_text=None)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "k3s is already present" in output
    assert not (tmp_path / "marker").exists()


def test_k3s_guard_refuses_a_marker_for_another_env(tmp_path: Path) -> None:
    """The marker has to name this env, not just exist."""
    result = _run_guard(tmp_path, marker_text="another-env\n")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "k3s is already present" in output


def test_k3s_guard_accepts_active_k3s_with_this_envs_marker(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, marker_text="fixture\n")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "k3s is already present" not in output
    assert (tmp_path / "marker").read_text(encoding="utf-8") == "fixture\n"


@pytest.mark.parametrize("mode", ["server", "agent"])
def test_unit_starts_the_pinned_binary_in_the_node_mode(mode: str) -> None:
    rendered = ansible_render_template(
        "k3s.service.j2",
        {"k3s_binary_path": "/opt/fixture/k3s", "k3s_mode": mode},
        templates_dir=K3S_ROLE_TEMPLATES,
    )
    assert f"ExecStart=/opt/fixture/k3s {mode}" in rendered.splitlines()


def test_agent_joins_the_first_control_plane_over_6443(tmp_path: Path) -> None:
    """The join URL is built by token.yml. Render that expression, not a copy of it."""
    tasks = yaml.safe_load((K3S_ROLE_DIR / "tasks" / "token.yml").read_text(encoding="utf-8"))
    expression = next(
        task["ansible.builtin.set_fact"]["k3s_server_url"]
        for task in tasks
        if "k3s_server_url" in (task.get("ansible.builtin.set_fact") or {})
    )
    (tmp_path / "url.j2").write_text(expression, encoding="utf-8")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        "all:\n  children:\n    control_plane:\n      hosts:\n        cp-1:\n        cp-2:\n"
        "    local:\n      hosts:\n        localhost:\n          ansible_connection: local\n",
        encoding="utf-8",
    )
    rendered = ansible_render_template("url.j2", {}, templates_dir=tmp_path, inventory=inventory)
    assert rendered.strip() == "https://cp-1:6443"


def test_every_task_that_touches_a_token_is_no_log() -> None:
    """A task that reads, compares or writes a k3s token hides its result."""
    touches = re.compile(r"k3s_(server|agent)_token|\bk3s_node_token\b|config\.yaml\.j2")
    seen = 0
    for name in ("main.yml", "token.yml"):
        for task in yaml.safe_load((K3S_ROLE_DIR / "tasks" / name).read_text(encoding="utf-8")):
            if touches.search(yaml.safe_dump(task)):
                seen += 1
                assert task.get("no_log") is True, task.get("name")
    assert seen >= 5


def test_files_that_can_hold_the_join_token_are_root_only() -> None:
    """The server and agent config files are written 0600 as root."""
    tasks = yaml.safe_load((K3S_ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
    configs = {
        task["ansible.builtin.template"]["src"]: task
        for task in tasks
        if isinstance(task, dict)
        and "ansible.builtin.template" in task
        and task["ansible.builtin.template"]["src"] in {"config.yaml.j2", "agent-config.yaml.j2"}
    }
    assert set(configs) == {"config.yaml.j2", "agent-config.yaml.j2"}
    for src, task in configs.items():
        template = task["ansible.builtin.template"]
        assert (template["mode"], template["owner"], template["group"]) == ("0600", "root", "root"), src


_PRIVATE_DATA_DIRS = (
    "{{ local_path_data }}",
    "{{ backup_path }}",
    "{{ backup_path }}/etcd-snapshots",
)


def _file_targets(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        file_task = node.get("ansible.builtin.file")
        if isinstance(file_task, dict):
            path = file_task.get("path")
            if isinstance(path, str):
                found.add(path)
            loop = node.get("loop")
            if isinstance(loop, list):
                found.update(item for item in loop if isinstance(item, str))
        for value in node.values():
            _file_targets(value, found)
    elif isinstance(node, list):
        for item in node:
            _file_targets(item, found)


def _role_file_targets(role: Path) -> set[str]:
    found: set[str] = set()
    tasks = role / "tasks"
    if not tasks.is_dir():
        return found
    for path in tasks.glob("*.yml"):
        _file_targets(yaml.safe_load(path.read_text(encoding="utf-8")), found)
    return found


def test_k3s_main_imports_the_mount_dropin() -> None:
    """Every host that runs the k3s role gets the data-mount drop-in."""
    tasks = yaml.safe_load((K3S_ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    imported = [
        task.get("ansible.builtin.import_tasks")
        for task in tasks
        if isinstance(task, dict)
    ]
    assert "service-unit.yml" in imported


def test_k3s_is_the_only_owner_of_private_data_directories() -> None:
    """One owner per path: only the k3s role creates the private data directories."""
    roles = ANSIBLE_DIR / "roles"
    owners = {path: set() for path in _PRIVATE_DATA_DIRS}
    for role in roles.iterdir():
        if not role.is_dir():
            continue
        for target in _role_file_targets(role):
            for owned in _PRIVATE_DATA_DIRS:
                if target == owned:
                    owners[owned].add(role.name)
    for owned, role_names in owners.items():
        assert role_names == {"k3s"}, (owned, role_names)


def test_private_data_directories_are_mode_0700(tmp_path: Path) -> None:
    """k3s sets local_path_data, backup_path, and etcd-snapshots to mode 0700."""
    require_cli("ansible-playbook")
    local_path = tmp_path / "local-path"
    backup = tmp_path / "backup"
    snapshots = backup / "etcd-snapshots"
    for path in (local_path, backup, snapshots):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)
    play = tmp_path / "play.yml"
    tasks = K3S_ROLE_DIR / "tasks" / "data-dirs.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: Private data directories
              hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_tasks: {tasks}
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    result = subprocess.run(
        [
            "ansible-playbook",
            str(play),
            "-i",
            "localhost,",
            "-e",
            "ansible_connection=local",
            "-e",
            json.dumps(
                {
                    "local_path_data": str(local_path),
                    "backup_path": str(backup),
                }
            ),
            "--check",
            "--diff",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    for path in (local_path, backup, snapshots):
        assert str(path) in output, output
        assert path.stat().st_mode & 0o777 == 0o755
    assert output.count('"mode": "0700"') + output.count("mode: '0700'") >= 3, output


def _systemctl(bin_dir: Path, *, fail: str = "", state: str = "active enabled") -> None:
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "state").write_text(state + "\n", encoding="utf-8")
    (bin_dir / "fail").write_text(fail, encoding="utf-8")
    (bin_dir / "systemctl.log").write_text("", encoding="utf-8")
    (bin_dir / "systemctl").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            from pathlib import Path

            root = Path(__file__).parent
            log = root / "systemctl.log"
            state_path = root / "state"
            args = sys.argv[1:]
            with log.open("a", encoding="utf-8") as handle:
                handle.write(" ".join(args) + "\\n")
            if not args:
                sys.stderr.write("systemctl: missing command\\n")
                sys.exit(1)
            command = args[0]
            if command == "daemon-reload":
                sys.exit(0)
            fail = (root / "fail").read_text(encoding="utf-8").strip()
            if command == fail:
                sys.stderr.write(f"{command} failed\\n")
                sys.exit(1)

            def read_state():
                active, enabled = state_path.read_text(encoding="utf-8").split()
                return active, enabled

            def write_state(active, enabled):
                state_path.write_text(f"{active} {enabled}\\n", encoding="utf-8")

            if command == "show":
                active, _enabled = read_state()
                sys.stdout.write(f"LoadState=loaded\\nActiveState={active}\\n")
                sys.exit(0)
            if command == "is-active":
                active, _enabled = read_state()
                sys.stdout.write(active + "\\n")
                sys.exit(0 if active == "active" else 3)
            if command == "is-enabled":
                _active, enabled = read_state()
                sys.stdout.write(enabled + "\\n")
                sys.exit(0 if enabled == "enabled" else 1)
            if command == "disable":
                active, _enabled = read_state()
                write_state(active, "disabled")
                sys.exit(0)
            if command == "stop":
                _active, enabled = read_state()
                write_state("inactive", enabled)
                sys.exit(0)
            sys.stderr.write("unexpected systemctl " + " ".join(args) + "\\n")
            sys.exit(1)
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "systemctl").chmod(0o755)


def _dropin_play(tmp_path: Path, bin_dir: Path) -> Path:
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: k3s unit
              hosts: localhost
              gather_facts: false
              become: false
              environment:
                PATH: "{bin_dir}:/usr/bin:/bin"
              tasks:
                - ansible.builtin.import_role:
                    name: k3s
                    tasks_from: service-unit.yml
            """
        ),
        encoding="utf-8",
    )
    return play


def _run_dropin(
    play: Path,
    *,
    mode: str,
    unit_dir: Path,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    service = "k3s" if mode == "server" else "k3s-agent"
    cluster = load_cluster_vars()
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    cmd = [
        "ansible-playbook",
        str(play),
        "-i",
        "localhost,",
        "-e",
        "ansible_connection=local",
        "-e",
        json.dumps(
            {
                "k3s_mode": mode,
                "k3s_service_name": service,
                "k3s_unit_dir": str(unit_dir),
                "local_path_data": cluster["local_path_data"],
                "backup_path": cluster["backup_path"],
            }
        ),
    ]
    if check:
        cmd.append("--check")
    return subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize("mode", ["server", "agent"])
def test_k3s_writes_mount_dropin_and_disables_opposite_unit(tmp_path: Path, mode: str) -> None:
    """The drop-in matches the configured paths and the opposite unit is stopped."""
    require_cli("ansible-playbook")
    bin_dir = tmp_path / "bin"
    _systemctl(bin_dir)
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    service = "k3s" if mode == "server" else "k3s-agent"
    opposite = "k3s-agent" if mode == "server" else "k3s"
    (unit_dir / f"{opposite}.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    play = _dropin_play(tmp_path, bin_dir)
    cluster = load_cluster_vars()
    expected = (
        "# Managed by Ansible (k3s role).\n"
        "[Unit]\n"
        f"RequiresMountsFor={cluster['local_path_data']} {cluster['backup_path']}\n"
    )

    checked = _run_dropin(play, mode=mode, unit_dir=unit_dir, check=True)
    checked_out = checked.stdout + checked.stderr
    assert checked.returncode == 0, checked_out

    first = _run_dropin(play, mode=mode, unit_dir=unit_dir)
    output = first.stdout + first.stderr
    assert first.returncode == 0, output
    dropin = unit_dir / f"{service}.service.d" / "jarvis-mount.conf"
    assert dropin.read_text(encoding="utf-8") == expected
    assert dropin.stat().st_mode & 0o777 == 0o644
    log = (bin_dir / "systemctl.log").read_text(encoding="utf-8")
    assert f"disable {opposite}" in log
    assert f"stop {opposite}" in log
    assert "daemon-reload" in log
    assert "restart" not in log

    second = _run_dropin(play, mode=mode, unit_dir=unit_dir)
    second_out = second.stdout + second.stderr
    assert second.returncode == 0, second_out
    assert playbook_recap_field(second_out, "changed") == 0
    assert dropin.read_text(encoding="utf-8") == expected

    again = _run_dropin(play, mode=mode, unit_dir=unit_dir, check=True)
    again_out = again.stdout + again.stderr
    assert again.returncode == 0, again_out
    assert playbook_recap_field(again_out, "changed") == 0


def test_k3s_missing_opposite_unit_is_fine(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    bin_dir = tmp_path / "bin"
    _systemctl(bin_dir, fail="show")
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    play = _dropin_play(tmp_path, bin_dir)
    result = _run_dropin(play, mode="server", unit_dir=unit_dir)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    log = (bin_dir / "systemctl.log").read_text(encoding="utf-8")
    assert "daemon-reload" in log
    assert "disable" not in log
    assert "stop" not in log


def test_k3s_opposite_unit_error_fails(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    bin_dir = tmp_path / "bin"
    _systemctl(bin_dir, fail="disable")
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "k3s-agent.service").write_text("[Service]\n", encoding="utf-8")
    play = _dropin_play(tmp_path, bin_dir)
    result = _run_dropin(play, mode="server", unit_dir=unit_dir)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Unable to disable" in output


_SERVER_TOKEN = "fixture-server-passphrase"
_AGENT_TOKEN = "fixture-agent-passphrase"
_CONFIG_KEYS = ("pod_cidr", "service_cidr", "k3s_kubelet_kube_reserved",
                "k3s_kubelet_system_reserved", "k3s_kubelet_eviction_hard", "node_memory_reserve_percent")
# The memory fact the role's kubelet reserve reads; these plays gather no facts.
_MEMORY_FACT = {"ansible_memtotal_mb": 16384}


def _sealed_tokens(tmp_path: Path, server: str = _SERVER_TOKEN) -> tuple[Path, Path]:
    """A k3s-token file sealed to a throwaway age key, and that key. Made once per tmp_path."""
    sops = require_cli("sops")
    keyfile = tmp_path / "age.txt"
    path = tmp_path / "k3s-token.sops.yaml"
    if keyfile.exists() or path.exists():
        return path, keyfile
    subprocess.run([require_cli("age-keygen"), "-o", str(keyfile)], check=True, capture_output=True)
    public = next(
        line.split(":", 1)[1].strip()
        for line in keyfile.read_text(encoding="utf-8").splitlines()
        if line.startswith("# public key:")
    )
    doc = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "k3s-token"},
           "stringData": {"server_token": server, "agent_token": _AGENT_TOKEN}}
    (tmp_path / ".sops.yaml").write_text(
        f"creation_rules:\n  - encrypted_regex: ^(data|stringData)$\n    age: {public}\n",
        encoding="utf-8",
    )
    sealed = subprocess.run(
        [sops, "encrypt", f"--filename-override={path.name}", "/dev/stdin"],
        input=yaml.safe_dump(doc), capture_output=True, text=True, check=True, cwd=tmp_path,
    )
    path.write_text(sealed.stdout, encoding="utf-8")
    return path, keyfile


def _play_env(keyfile: Path) -> dict[str, str]:
    return {**os.environ, "ANSIBLE_BECOME": "false", "SOPS_AGE_KEY_FILE": str(keyfile),
            "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg")}


def _run_token_read(
    tmp_path: Path, *, mode: str, node_token: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    require_cli("ansible-playbook")
    token_file, keyfile = _sealed_tokens(tmp_path)
    data_dir = tmp_path / "k3s"
    if node_token is not None:
        (data_dir / "server").mkdir(parents=True, exist_ok=True)
        (data_dir / "server" / "token").write_text(node_token + "\n", encoding="utf-8")
    facts = tmp_path / "facts.json"
    cluster = load_cluster_vars()
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        "all:\n  children:\n    control_plane:\n      hosts:\n        localhost:\n"
        "          ansible_connection: local\n",
        encoding="utf-8",
    )
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_tasks: {K3S_ROLE_DIR / "tasks" / "token.yml"}
                - ansible.builtin.copy:
                    dest: {facts}
                    content: "{{{{ {{'server': k3s_server_token, 'agent': k3s_agent_token}} | to_json }}}}"
                  no_log: true
                - ansible.builtin.template:
                    src: {K3S_ROLE_TEMPLATES / K3S_CONFIG_TEMPLATE}
                    dest: {tmp_path / "config.yaml"}
                  no_log: true
                  when: k3s_mode == 'server'
            """
        ),
        encoding="utf-8",
    )
    extra = {**load_role_defaults(K3S_ROLE_DIR), **{key: cluster[key] for key in _CONFIG_KEYS}, **_MEMORY_FACT,
             "local_path_data": "/fixture/local", "backup_path": "/fixture/backup", "jarvis_env": "fixture",
             "k3s_mode": mode, "k3s_token_file": str(token_file), "k3s_data_dir": str(data_dir)}
    proc = subprocess.run(
        ["ansible-playbook", str(play), "-i", str(inventory), "-e", json.dumps(extra)],
        cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False, env=_play_env(keyfile),
    )
    return proc, facts


@pytest.mark.parametrize("mode", ["server", "agent"])
def test_tokens_come_from_the_sealed_file(tmp_path: Path, mode: str) -> None:
    """The controller decrypts the sealed file. An agent gets the agent token only. Nothing prints."""
    proc, facts = _run_token_read(tmp_path, mode=mode)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    want = {"server": _SERVER_TOKEN if mode == "server" else "", "agent": _AGENT_TOKEN}
    assert json.loads(facts.read_text(encoding="utf-8")) == want
    assert _SERVER_TOKEN not in output and _AGENT_TOKEN not in output


def test_token_and_config_tasks_converge(tmp_path: Path) -> None:
    """A second run with the same sealed file and node token changes nothing."""
    first, _facts = _run_token_read(tmp_path, mode="server", node_token=f"K10ab::server:{_SERVER_TOKEN}")
    again, _facts = _run_token_read(tmp_path, mode="server", node_token=f"K10ab::server:{_SERVER_TOKEN}")
    assert first.returncode == 0 and again.returncode == 0, first.stdout + again.stdout
    assert playbook_recap_field(first.stdout, "changed") == 2
    assert playbook_recap_field(again.stdout, "changed") == 0


_STOPS = {
    "no-key": "Set SOPS_AGE_KEY_FILE or age_key_file",
    "key-mode": "Refuse an age private key",
    "missing": "is missing or lacks stringData.agent_token",
    "key-names": "is missing or lacks stringData.agent_token",
}


@pytest.mark.parametrize("broken", sorted(_STOPS))
def test_token_read_stops_visibly_before_any_decrypt(tmp_path: Path, broken: str) -> None:
    """A missing or loose key file, or a sealed file that is missing or lacks the key names, stops before the lookup."""
    token_file, keyfile = _sealed_tokens(tmp_path)
    if broken == "no-key":
        keyfile.rename(tmp_path / "moved-key")
    elif broken == "key-mode":
        keyfile.chmod(0o644)
    elif broken == "missing":
        token_file.unlink()
    else:
        token_file.write_text("stringData:\n  token: ENC[fixture]\n", encoding="utf-8")
    proc, facts = _run_token_read(tmp_path, mode="server")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _STOPS[broken] in output, output
    assert "TASK [Read the sealed k3s tokens]" not in output and not facts.exists()


@pytest.mark.parametrize(
    ("sealed", "node_token", "refused"),
    [
        (_SERVER_TOKEN, f"K10abc123::server:{_SERVER_TOKEN}", False),
        (_SERVER_TOKEN, _SERVER_TOKEN, False),
        (_SERVER_TOKEN, "K10abc123::server:fixture-node-passphrase", True),
        (f"K10abc123::server:{_SERVER_TOKEN}", f"K10abc123::server:{_SERVER_TOKEN}", False),
        (f"K10abc123::server:{_SERVER_TOKEN}", f"K10def456::server:{_SERVER_TOKEN}", True),
    ],
)
def test_server_refuses_a_sealed_token_that_differs_from_the_node(
    tmp_path: Path, sealed: str, node_token: str, refused: bool
) -> None:
    """A mismatch stops before any later task and prints neither value. A sealed K10 token must match whole."""
    _sealed_tokens(tmp_path, server=sealed)
    proc, facts = _run_token_read(tmp_path, mode="server", node_token=node_token)
    output = proc.stdout + proc.stderr
    assert (proc.returncode != 0) is refused, output
    assert ("differs from the sealed token" in output) is refused, output
    assert facts.exists() is not refused
    for value in ("fixture-node-passphrase", _SERVER_TOKEN, _AGENT_TOKEN):
        assert value not in output


def test_server_with_no_token_file_refuses_a_sealed_secure_form_token(tmp_path: Path) -> None:
    """A fresh server mints a new CA, so a sealed K10 token stops the run visibly, without its value."""
    _sealed_tokens(tmp_path, server=f"K10abc123::server:{_SERVER_TOKEN}")
    proc, facts = _run_token_read(tmp_path, mode="server")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "Nothing was changed" in output, output
    assert not facts.exists()
    for value in ("K10abc123", _SERVER_TOKEN, _AGENT_TOKEN):
        assert value not in output


def _role_play(tmp_path: Path, bin_dir: Path) -> Path:
    play = tmp_path / "role.yml"
    play.write_text(
        "---\n- hosts: localhost\n  gather_facts: false\n  become: false\n"
        f"  environment:\n    PATH: \"{bin_dir}:/usr/bin:/bin\"\n"
        "  roles:\n    - role: k3s\n      vars:\n        k3s_mode: server\n",
        encoding="utf-8",
    )
    return play


def _run_role(
    tmp_path: Path, *, running: bool, node_token: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Check mode of the whole server role against tmp paths and a stub systemctl."""
    require_cli("ansible-playbook")
    token_file, keyfile = _sealed_tokens(tmp_path)
    if node_token is not None:
        (tmp_path / "data" / "server").mkdir(parents=True)
        (tmp_path / "data" / "server" / "token").write_text(node_token + "\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _systemctl(bin_dir, state="active enabled" if running else "inactive disabled")
    binary = tmp_path / "k3s-binary"
    binary.write_bytes(b"fixture k3s\n")
    units = tmp_path / "systemd"
    (units / "k3s.service.d").mkdir(parents=True)
    config = tmp_path / "config.yaml"
    if running:
        config.write_text("# an older config\n", encoding="utf-8")
    cluster = load_cluster_vars()
    extra = {
        "jarvis_env": "fixture", "jarvis_marker_path": str(tmp_path / "env"),
        "k3s_token_file": str(token_file), "k3s_data_dir": str(tmp_path / "data"),
        "k3s_config_path": str(config), "k3s_unit_dir": str(units),
        "k3s_binary_path": str(binary), "k3s_version": "v0.0.0+k3s1",
        "k3s_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "local_path_data": str(tmp_path / "local"), "backup_path": str(tmp_path / "backup"),
        **{key: cluster[key] for key in _CONFIG_KEYS}, **_MEMORY_FACT,
    }
    return subprocess.run(
        ["ansible-playbook", str(_role_play(tmp_path, bin_dir)), "-i", "localhost,",
         "-e", "ansible_connection=local", "-e", json.dumps(extra), "--check"],
        cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False, env=_play_env(keyfile),
    )


def test_a_token_mismatch_stops_the_role_before_its_config(tmp_path: Path) -> None:
    """On a running server whose token differs, the role fails and never reaches the config task."""
    proc = _run_role(tmp_path, running=True, node_token="K10ab::server:fixture-node-passphrase")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "differs from the sealed token" in output, output
    assert "Install k3s server config" not in output and "fixture-node-passphrase" not in output


@pytest.mark.parametrize("running", [False, True])
def test_first_install_does_not_restart_and_a_config_change_does(tmp_path: Path, running: bool) -> None:
    """Restart k3s only if it was running before this run."""
    proc = _run_role(tmp_path, running=running)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    handler = output.split("RUNNING HANDLER [k3s : Restart k3s]")[1].split("PLAY RECAP")[0]
    assert ("changed: [localhost]" in handler) is running, handler
    assert ("skipping: [localhost]" in handler) is not running, handler


def _render_reserve(tmp_path: Path, cluster_vars: str, memtotal_mb: int) -> subprocess.CompletedProcess[str]:
    """Both kubelet configs from a cluster-vars text, as a node with memtotal_mb renders them."""
    require_cli("ansible-playbook")
    (tmp_path / "cluster-vars.yaml").write_text(cluster_vars, encoding="utf-8")
    play = tmp_path / "play.yml"
    play.write_text(
        "---\n- hosts: localhost\n  gather_facts: false\n  become: false\n"
        f"  vars_files: [{tmp_path / 'cluster-vars.yaml'}]\n  tasks:\n"
        + "".join(
            f"    - ansible.builtin.template:\n        src: {K3S_ROLE_TEMPLATES / src}\n        dest: {tmp_path / src}.out\n"
            for src in (K3S_CONFIG_TEMPLATE, "agent-config.yaml.j2")
        ),
        encoding="utf-8",
    )
    extra = {"ansible_memtotal_mb": memtotal_mb, "inventory_hostname": "node", "k3s_server_url": "https://cp-1:6443",
             "k3s_server_token": "fixture-server", "k3s_agent_token": "fixture-agent", "pod_cidr": "10.80.0.0/16",
             "service_cidr": "10.81.0.0/16", "local_path_data": "/fixture/local", "backup_path": "/fixture/backup",
             **{key: value for key, value in load_role_defaults(K3S_ROLE_DIR).items() if key.startswith("k3s_etcd")}}
    return subprocess.run(
        ["ansible-playbook", str(play), "-i", "localhost,", "-e", "ansible_connection=local", "-e", json.dumps(extra)],
        cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False, env=_play_env(tmp_path / "no-key"),
    )


_RESERVES = "k3s_kubelet_kube_reserved: cpu=100m,memory=128Mi\nk3s_kubelet_system_reserved: 'cpu=100m,memory=128Mi'\n"


@pytest.mark.parametrize(
    ("lines", "memtotal_mb", "eviction"),
    [
        ("k3s_kubelet_eviction_hard: memory.available<100Mi\nnode_memory_reserve_percent: 10\n", 16384, "memory.available<10%"),
        # A quoted percent: env-schema refuses it in an env, and the role still reads it.
        ('k3s_kubelet_eviction_hard: "memory.available<100Mi,nodefs.available<10%"\nnode_memory_reserve_percent: "15"\n',
         16384, "memory.available<15%,nodefs.available<10%"),
        ("k3s_kubelet_eviction_hard: 'nodefs.available<10%'\n", 4096, "nodefs.available<10%,memory.available<10%"),
        ("k3s_kubelet_eviction_hard: memory.available<2Gi\nnode_memory_reserve_percent: 10\n", 16384, "memory.available<2Gi"),
        ("k3s_kubelet_eviction_hard: memory.available<100Mi\nnode_memory_reserve_percent: 0\n", 16384, "memory.available<100Mi"),
    ],
    ids=["reserve-wins", "quoted-keeps-other-signals", "default-appended", "larger-env-value-stays", "off"],
)
def test_node_memory_reserve_sets_the_kubelet_eviction_threshold(
    tmp_path: Path, lines: str, memtotal_mb: int, eviction: str
) -> None:
    """Rule: every node's kubelet keeps node_memory_reserve_percent (default 10) of its memory
    out of allocatable as memory.available<N%, unless the env's eviction-hard is larger; the
    other reserves stay as written, and a second run changes nothing."""
    first = _render_reserve(tmp_path, _RESERVES + lines, memtotal_mb)
    assert first.returncode == 0, first.stdout + first.stderr
    for src in (K3S_CONFIG_TEMPLATE, "agent-config.yaml.j2"):
        args = yaml.safe_load((tmp_path / f"{src}.out").read_text(encoding="utf-8"))["kubelet-arg"]
        assert args == ["kube-reserved=cpu=100m,memory=128Mi", "system-reserved=cpu=100m,memory=128Mi",
                        f"eviction-hard={eviction}"], src
    again = _render_reserve(tmp_path, _RESERVES + lines, memtotal_mb)
    assert again.returncode == 0 and playbook_recap_field(again.stdout, "changed") == 0, again.stdout


@pytest.mark.parametrize("bad", ["node_memory_reserve_percent: 60\n", "node_memory_reserve_percent: ten\n",
                                 "k3s_kubelet_eviction_hard: memory.available<lots\n"], ids=["too-large", "not-a-number", "bad-quantity"])
def test_node_memory_reserve_refuses_what_it_cannot_apply(tmp_path: Path, bad: str) -> None:
    """Rule: a reserve outside 0 to 50, or an eviction-hard memory.available it cannot compare,
    stops the render and writes no config."""
    base = "k3s_kubelet_eviction_hard: memory.available<100Mi\n" if "eviction" not in bad else ""
    proc = _render_reserve(tmp_path, _RESERVES + base + bad, 16384)
    assert proc.returncode != 0, proc.stdout
    assert "node_memory_reserve_percent is a whole number" in proc.stdout or "not a quantity" in proc.stdout, proc.stdout
    assert not (tmp_path / f"{K3S_CONFIG_TEMPLATE}.out").exists()
