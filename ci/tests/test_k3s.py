"""k3s Ansible role: config render and guard proof (p0a-12)."""

from __future__ import annotations

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
    }
    rendered = render_k3s_config(variables)
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
            "k3s_token": "fixture-token",
            "inventory_hostname": "wk-1",
            "k3s_kubelet_kube_reserved": cluster["k3s_kubelet_kube_reserved"],
            "k3s_kubelet_system_reserved": cluster["k3s_kubelet_system_reserved"],
            "k3s_kubelet_eviction_hard": cluster["k3s_kubelet_eviction_hard"],
        },
        templates_dir=K3S_ROLE_TEMPLATES,
    )
    assert "server: https://cp-1:6443" in rendered
    assert "token: fixture-token" in rendered
    assert "node-name: wk-1" in rendered
    assert f"kube-reserved={cluster['k3s_kubelet_kube_reserved']}" in rendered
    assert f"system-reserved={cluster['k3s_kubelet_system_reserved']}" in rendered
    assert f"eviction-hard={cluster['k3s_kubelet_eviction_hard']}" in rendered
    assert "cluster-init" not in rendered
    assert "node-label" not in rendered


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
        ["ansible-playbook", str(play), "-i", str(inventory)],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "cp.invalid does not resolve on wk-1" in output


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


def _hosts_outside_control_plane(env: str) -> set[str]:
    doc = yaml.safe_load(
        (REPO_ROOT / "envs" / env / "inventory" / "hosts.yml").read_text(encoding="utf-8")
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


def test_agent_play_host_pattern() -> None:
    envs = [env_name(), *non_example_env_names()]
    assert non_example_env_names()
    for env in envs:
        assert _listed_play_hosts(_agent_play_section(env)) == _hosts_outside_control_plane(env)


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
        edge_hosts: list[str], node_resources: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Path(tmp) / "hosts.yml"
            inventory.write_text(_label_inventory(edge_hosts), encoding="utf-8")
            rendered = ansible_render_template(
                "label-patches.json.j2",
                {
                    "dns_suffix": "env-00.example",
                    "k3s_label_hosts": hosts,
                    "k3s_node_resources": node_resources,
                },
                templates_dir=K3S_ROLE_TEMPLATES,
                inventory=inventory,
            )
        loaded = json.loads(rendered)
        assert isinstance(loaded, list)
        return loaded

    edged = {item["name"]: item["labels"] for item in render(["wk-1"], nodes)}
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
    no_edge = {item["name"]: item["labels"] for item in render([], bare)}
    assert "svccontroller.k3s.cattle.io/enablelb" not in no_edge["cp-1"]
    assert no_edge["wk-1"]["svccontroller.k3s.cattle.io/enablelb"] is None


def test_k3s_guard_refuses_active_k3s_without_marker(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\necho active\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)
    play = tmp_path / "play.yml"
    guard = K3S_ROLE_DIR / "tasks" / "guard.yml"
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
    result = subprocess.run(
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
            f"jarvis_marker_path={tmp_path / 'missing-marker'}",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "k3s is already present" in output


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


def _systemctl(bin_dir: Path, *, fail: str = "") -> None:
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "state").write_text("active enabled\n", encoding="utf-8")
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
