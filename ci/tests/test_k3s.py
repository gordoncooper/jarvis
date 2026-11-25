"""k3s Ansible role: the kubelet configs, node labels and taint, the sealed tokens and their refusals, the guard, the
mount drop-in, the data dirs, and the restart rule."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
import sample
import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    K3S_ROLE_DIR,
    K3S_ROLE_TEMPLATES,
    LOCALHOST,
    ansible_render_template,
    changed,
    failed_task,
    fake_systemctl,
    load_role_defaults,
    local_play,
    playbook,
    ran,
    role_tasks,
    roles_inventory_path,
    site_vars_file,
)
from conftest import require_cli

_CONFIGS = ("config.yaml.j2", "agent-config.yaml.j2")


def _render_configs(tmp_path: Path, hosts: dict[str, dict], **env: object) -> subprocess.CompletedProcess[str]:
    """Each host's server and agent configs and ssh user, from its hosts.yml entry in hosts through core's roles
    inventory and what jarvis site passes for the sample env, env's keys set over it: tmp_path/<host>-<file>."""
    require_cli("ansible-playbook")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(json.dumps({"all": {"hosts": hosts}}), encoding="utf-8")
    # A play outside infra/ansible reads neither its group_vars (ansible_user) nor, with no role, the role's defaults.
    group_vars = yaml.safe_load((ANSIBLE_DIR / "group_vars" / "all.yml").read_text(encoding="utf-8"))
    defaults = load_role_defaults(K3S_ROLE_DIR)
    play_vars = {"ansible_connection": "local", "ansible_user": group_vars["ansible_user"], "k3s_server_url": "https://cp-1:6443",
                 "k3s_server_token": "fixture-server", "k3s_agent_token": "fixture-agent",
                 **{key: defaults[key] for key in ("k3s_etcd_snapshot_schedule_cron", "k3s_etcd_snapshot_retention")}}
    tasks = "".join(f"    - ansible.builtin.template:\n        src: {K3S_ROLE_TEMPLATES / name}\n"
                    f"        dest: {tmp_path}/{{{{ inventory_hostname }}}}-{name}\n" for name in _CONFIGS)
    tasks += f"    - ansible.builtin.copy:\n        dest: {tmp_path}/{{{{ inventory_hostname }}}}.user\n        content: '{{{{ ansible_user }}}}'\n"
    play = local_play(tmp_path, tasks, hosts="all", vars=play_vars)
    return playbook(play, "-i", inventory, "-i", roles_inventory_path(), "-e", f"@{site_vars_file(tmp_path / 'vars.json', **env)}", json=True)


def _kubelet_args(tmp_path: Path, host: str) -> list[list[str]]:
    """host's kubelet-arg in its server config, then in its agent config, as _render_configs wrote them."""
    return [yaml.safe_load((tmp_path / f"{host}-{name}").read_text(encoding="utf-8"))["kubelet-arg"] for name in _CONFIGS]


def test_host_overrides_win_on_that_host_only(tmp_path: Path) -> None:
    """Rule: a host's own ssh_user and kubelet reserves apply on that host only; the env's memory reserve applies to
    both, and a host's larger memory.available stays."""
    # over: 250Mi is more than 10% of 2 GiB, so it stays; plain: 10% of 16 GiB beats the env's 100Mi.
    over = {"node_roles": ["control_plane", "backup", "edge"], "ssh_user": "alice", "k3s_node_memtotal_mb": 2048,
            "k3s_kubelet_kube_reserved": "cpu=50m", "k3s_kubelet_system_reserved": "cpu=25m",
            "k3s_kubelet_eviction_hard": "memory.available<250Mi"}
    plain = {"node_roles": ["workers"], "k3s_node_memtotal_mb": 16384}
    run = _render_configs(tmp_path, {"over": over, "plain": plain}, ssh_user="env-user", k3s_kubelet_kube_reserved="cpu=100m",
                          k3s_kubelet_system_reserved="cpu=200m", k3s_kubelet_eviction_hard="memory.available<100Mi",
                          node_memory_reserve_percent=10)
    assert run.returncode == 0, run.output
    assert {host: ((tmp_path / f"{host}.user").read_text(encoding="utf-8"), _kubelet_args(tmp_path, host)) for host in ("over", "plain")} == {
        "over": ("alice", [["kube-reserved=cpu=50m", "system-reserved=cpu=25m", "eviction-hard=memory.available<250Mi"]] * 2),
        "plain": ("env-user", [["kube-reserved=cpu=100m", "system-reserved=cpu=200m", "eviction-hard=memory.available<10%"]] * 2),
    }


def test_agent_fails_when_control_plane_name_does_not_resolve(tmp_path: Path) -> None:
    """Rule: an agent stops before it joins when the control plane's inventory name does not resolve on it."""
    require_cli("ansible-playbook")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(json.dumps({"all": {"children": {"control_plane": {"hosts": {"cp.invalid": None}},
                                                          "agents": {"hosts": {"wk-1": {"ansible_connection": "local"}}}}}}), encoding="utf-8")
    run = playbook(local_play(tmp_path, role_tasks("k3s", "token.yml"), hosts="wk-1"), "-i", inventory, "-e", "k3s_mode=agent", json=True)
    assert run.returncode != 0 and failed_task(run) == (
        "Fail when the control-plane name does not resolve on this agent",
        "cp.invalid does not resolve on wk-1. Every inventory hostname must resolve on this node before the agent starts.",
    ), run.output


_LB = "svccontroller.k3s.cattle.io/enablelb"
_ROLE = "node-role.jarvis.io/"
_DEDICATED = {"key": "node-role.jarvis.io/dedicated", "value": "true", "effect": "NoSchedule"}
_HOLD = {"key": "example.com/hold", "value": "true", "effect": "NoSchedule"}


def _node(name: str, labels: dict[str, str], taints: list[dict] | None = None) -> dict:
    return {"metadata": {"name": name, "labels": labels}, **({"spec": {"taints": taints}} if taints is not None else {})}


@pytest.mark.parametrize(
    ("edge", "roles", "nodes", "patches"),
    [
        pytest.param(["wk-1"], {"cp-1": ["control_plane"], "wk-1": ["workers", "edge"]},
                     [_node("cp-1", {_LB: "true", f"{_ROLE}workers": "true", "kubernetes.io/os": "linux"}),
                      _node("wk-1", {"kubernetes.io/hostname": "wk-1"})],
                     [{"name": "cp-1", "labels": {f"{_ROLE}control_plane": "true", f"{_ROLE}workers": None, _LB: None}, "taint": "present"},
                      {"name": "wk-1", "labels": {f"{_ROLE}workers": "true", f"{_ROLE}edge": "true", _LB: "true"}, "taint": None}],
                     id="roles-and-enablelb"),
        pytest.param([], {"cp-1": ["control_plane"], "wk-1": ["workers", "edge"]},
                     [_node("cp-1", {}), _node("wk-1", {_LB: "true"})],
                     [{"name": "cp-1", "labels": {f"{_ROLE}control_plane": "true"}, "taint": "present"},
                      {"name": "wk-1", "labels": {f"{_ROLE}workers": "true", f"{_ROLE}edge": "true", _LB: None}, "taint": None}],
                     id="enablelb-only-in-the-edge-group"),
        pytest.param(["edge-only"], {"edge-only": ["edge"], "workers-only": ["workers"], "gpu-only": ["gpu"]},
                     [_node("edge-only", {}, [_HOLD]), _node("workers-only", {}, [_HOLD, _DEDICATED]), _node("gpu-only", {}, [_HOLD])],
                     [{"name": "edge-only", "labels": {f"{_ROLE}edge": "true", _LB: "true"}, "taint": None},
                      {"name": "workers-only", "labels": {f"{_ROLE}workers": "true"}, "taint": "absent"},
                      {"name": "gpu-only", "labels": {f"{_ROLE}gpu": "true"}, "taint": "present"}],
                     id="dedicated-taint"),
        pytest.param(["edge-only"], {"edge-only": ["edge"], "gpu-only": ["gpu"]},
                     [_node("edge-only", {f"{_ROLE}edge": "true", _LB: "true"}, []), _node("gpu-only", {f"{_ROLE}gpu": "true"}, [_DEDICATED])],
                     [{"name": "edge-only", "labels": {}, "taint": None}, {"name": "gpu-only", "labels": {}, "taint": None}],
                     id="converged"),
    ],
)
def test_label_patch_reconciles_roles_and_enablelb(
    tmp_path: Path, edge: list[str], roles: dict[str, list[str]], nodes: list[dict], patches: list[dict]
) -> None:
    """Rule: a node gets its role labels, and enablelb only in the edge group, losing jarvis's stale ones and no other; the
    dedicated taint goes on a node with neither workers nor edge and off the rest, other taints kept; converged, no patch."""
    inventory = tmp_path / "hosts.yml"
    local = {"localhost": {"ansible_connection": "local"}}
    inventory.write_text(json.dumps({"all": {"children": {"edge": {"hosts": dict.fromkeys(edge)}, "local": {"hosts": local}}}}), encoding="utf-8")
    rendered = ansible_render_template("label-patches.json.j2", {"k3s_label_hosts": roles, "k3s_node_resources": nodes},
                                       templates_dir=K3S_ROLE_TEMPLATES, inventory=inventory)
    assert json.loads(rendered) == patches


def test_the_dedicated_taint_task_leaves_other_taints_alone() -> None:
    """Rule: the taint task names only the dedicated taint and never replaces, so a node keeps its other taints."""
    tasks = yaml.safe_load((K3S_ROLE_DIR / "tasks" / "labels.yml").read_text(encoding="utf-8"))
    [taint] = [task["kubernetes.core.k8s_taint"] for task in tasks if "kubernetes.core.k8s_taint" in task]
    assert taint["taints"] == [_DEDICATED] and "replace" not in taint, taint


def test_every_task_that_touches_a_token_is_no_log() -> None:
    """Rule: a task that reads, compares or writes a k3s token hides its result."""
    touches = re.compile(r"k3s_(server|agent)_token|\bk3s_node_token\b|config\.yaml\.j2")
    seen = 0
    for name in ("main.yml", "token.yml"):
        for task in yaml.safe_load((K3S_ROLE_DIR / "tasks" / name).read_text(encoding="utf-8")):
            if touches.search(yaml.safe_dump(task)):
                seen += 1
                assert task.get("no_log") is True, task.get("name")
    assert seen >= 5


def test_files_that_can_hold_the_join_token_are_root_only() -> None:
    """Rule: the server and agent config files, which hold the join token, are written 0600 as root."""
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


@pytest.mark.parametrize("marker", [None, "another-env\n", "fixture\n"], ids=["no-marker", "another-env", "this-env"])
def test_k3s_guard_accepts_an_active_k3s_only_with_this_envs_marker(tmp_path: Path, marker: str | None) -> None:
    """Rule: on a host where k3s is active, the opening play's guard passes only with a marker naming this env, not just
    any marker, and writes none itself."""
    require_cli("ansible-playbook")
    path = tmp_path / "marker"
    if marker is not None:
        path.write_text(marker, encoding="utf-8")
    fake_systemctl(tmp_path / "bin")
    extra = {"jarvis_env": "fixture", "jarvis_marker_path": str(path), "k3s_binary_path": str(tmp_path / "missing-k3s"),
             "k3s_data_dir": str(tmp_path / "missing-data")}
    run = playbook(local_play(tmp_path, role_tasks("k3s", "refuse.yml"), bin_dir=tmp_path / "bin"), *LOCALHOST, "-e", json.dumps(extra), json=True)
    refusal = None if marker == "fixture\n" else (
        "Refuse host with foreign or unmarked k3s",
        f"k3s is already present on this host but {path} is missing or does not contain 'fixture'. Wipe the node or fix"
        " the marker before re-running this env.",
    )
    assert (run.returncode != 0) is (refusal is not None) and failed_task(run) == refusal, run.output
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == marker


def test_agent_joins_the_first_control_plane_over_6443(tmp_path: Path) -> None:
    """Rule: an agent joins https://<the first control_plane host>:6443, rendered from token.yml's own expression."""
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


def test_private_data_directories_are_mode_0700(tmp_path: Path) -> None:
    """Rule: the role makes local_path_data, backup_path and its etcd-snapshots mode 0700; --check changes none of them."""
    require_cli("ansible-playbook")
    dirs = [tmp_path / "local-path", tmp_path / "backup", tmp_path / "backup" / "etcd-snapshots"]
    for path in dirs:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)
    extra = {"local_path_data": str(dirs[0]), "backup_path": str(dirs[1])}
    run = playbook(local_play(tmp_path, role_tasks("k3s", "data-dirs.yml")), *LOCALHOST, "-e", json.dumps(extra), "--check", "--diff", json=True)
    assert run.returncode == 0, run.output
    items = [item for play in run.json["plays"] for task in play["tasks"] for item in task["hosts"]["localhost"].get("results", [])]
    assert {item["item"]: item["diff"]["after"].get("mode") for item in items} == dict.fromkeys(map(str, dirs), "0700")
    assert [path.stat().st_mode & 0o777 for path in dirs] == [0o755] * 3


_SERVICE = {"server": "k3s", "agent": "k3s-agent"}


def _service_unit(tmp_path: Path, mode: str, *flags: str) -> subprocess.CompletedProcess[str]:
    """service-unit.yml as the role runs it in mode, on unit files in tmp_path/systemd and the fake systemctl in
    tmp_path/bin."""
    extra = {"k3s_mode": mode, "k3s_service_name": _SERVICE[mode], "k3s_unit_dir": str(tmp_path / "systemd"),
             "local_path_data": "/srv/local-path", "backup_path": "/srv/backup"}
    play = local_play(tmp_path, role_tasks("k3s", "service-unit.yml"), bin_dir=tmp_path / "bin")
    return playbook(play, *LOCALHOST, "-e", json.dumps(extra), *flags, json=True)


def _calls(log: Path) -> list[str]:
    """Every systemctl call in log but the systemd module's reads of a unit's state (show, is-enabled)."""
    return [call for call in log.read_text(encoding="utf-8").splitlines() if not call.startswith(("show ", "is-enabled "))]


@pytest.mark.parametrize(("mode", "other"), [("server", "agent"), ("agent", "server")], ids=["server", "agent"])
def test_k3s_writes_mount_dropin_and_disables_opposite_unit(tmp_path: Path, mode: str, other: str) -> None:
    """Rule: --check on a fresh host passes and writes nothing; a run writes the drop-in (0644) that holds k3s for the
    data mounts, reloads systemd, and disables and stops the opposite unit, restarting nothing; a rerun changes nothing."""
    require_cli("ansible-playbook")
    log = fake_systemctl(tmp_path / "bin")
    (tmp_path / "systemd").mkdir()
    (tmp_path / "systemd" / f"{_SERVICE[other]}.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    dropin = tmp_path / "systemd" / f"{_SERVICE[mode]}.service.d" / "jarvis-mount.conf"
    expected = "# Managed by Ansible (k3s role).\n[Unit]\nRequiresMountsFor=/srv/local-path /srv/backup\n"

    checked = _service_unit(tmp_path, mode, "--check")
    assert checked.returncode == 0 and not dropin.parent.exists(), checked.output
    first = _service_unit(tmp_path, mode)
    assert first.returncode == 0, first.output
    assert (dropin.read_text(encoding="utf-8"), dropin.stat().st_mode & 0o777) == (expected, 0o644)
    calls = _calls(log)
    assert calls == [f"disable {_SERVICE[other]}", f"stop {_SERVICE[other]}", "daemon-reload"], calls

    again = _service_unit(tmp_path, mode)
    assert again.returncode == 0 and changed(again) == {"localhost": 0}, again.output
    assert (dropin.read_text(encoding="utf-8"), _calls(log)) == (expected, calls)


@pytest.mark.parametrize(
    ("unit", "fail", "calls", "refusal"),
    [
        pytest.param(False, "show", ["daemon-reload"], None, id="no-opposite-unit"),
        pytest.param(True, "disable", ["disable k3s-agent"],
                     ("Disable and stop the opposite k3s unit", "Unable to disable service k3s-agent: disable failed\n"), id="disable-fails"),
    ],
)
def test_k3s_opposite_unit_missing_is_fine_and_its_error_fails(
    tmp_path: Path, unit: bool, fail: str, calls: list[str], refusal: tuple[str, str] | None
) -> None:
    """Rule: with no opposite unit file the role asks systemctl nothing about it (a show fails here); a systemctl error on
    the opposite unit fails the run at that task."""
    require_cli("ansible-playbook")
    log = fake_systemctl(tmp_path / "bin", fail=fail)
    (tmp_path / "systemd").mkdir()
    if unit:
        (tmp_path / "systemd" / "k3s-agent.service").write_text("[Service]\n", encoding="utf-8")
    run = _service_unit(tmp_path, "server")
    assert (run.returncode != 0) is (refusal is not None) and failed_task(run) == refusal, run.output
    assert _calls(log) == calls


_SERVER_TOKEN = "fixture-server-passphrase"
_AGENT_TOKEN = "fixture-agent-passphrase"
_NODE_PASSPHRASE = "fixture-node-passphrase"


def _tokens(tmp_path: Path, sealed: str = _SERVER_TOKEN, node: str | None = None) -> dict[str, str]:
    """The role's token vars: a k3s-token file holding sealed and the agent token, sealed to a new age key in tmp_path;
    that key; and node, if given, as this node's own server token."""
    public = sample.throwaway_key(tmp_path)
    (tmp_path / ".sops.yaml").write_text(f"creation_rules:\n  - encrypted_regex: ^(data|stringData)$\n    age: {public}\n", encoding="utf-8")
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "k3s-token"},
              "stringData": {"server_token": sealed, "agent_token": _AGENT_TOKEN}}
    sops = [require_cli("sops"), "encrypt", "--filename-override=k3s-token.sops.yaml", "/dev/stdin"]
    sealed_file = tmp_path / "k3s-token.sops.yaml"
    sealed_file.write_text(subprocess.run(sops, input=yaml.safe_dump(secret), capture_output=True, text=True, check=True, cwd=tmp_path).stdout,
                           encoding="utf-8")
    if node is not None:
        (tmp_path / "k3s" / "server").mkdir(parents=True)
        (tmp_path / "k3s" / "server" / "token").write_text(node + "\n", encoding="utf-8")
    return {"k3s_token_file": str(sealed_file), "sops_age_key_file": str(tmp_path / ".age" / "keys.txt"), "k3s_data_dir": str(tmp_path / "k3s")}


def _read_tokens(tmp_path: Path, tokens: dict[str, str], mode: str = "server") -> subprocess.CompletedProcess[str]:
    """token.yml as the role runs it on a control-plane host, then the server config it feeds, with what jarvis site
    passes for the sample env; the tokens it read go to tmp_path/facts.json."""
    require_cli("ansible-playbook")
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(json.dumps({"all": {"children": {"control_plane": {"hosts": {"localhost": {"ansible_connection": "local"}}}}}}),
                         encoding="utf-8")
    facts = "{{ {'server': k3s_server_token, 'agent': k3s_agent_token} | to_json }}"
    tasks = role_tasks("k3s", "token.yml") + (
        f"    - ansible.builtin.copy:\n        dest: {tmp_path / 'facts.json'}\n        content: \"{facts}\"\n      no_log: true\n"
        f"    - ansible.builtin.template:\n        src: {K3S_ROLE_TEMPLATES / 'config.yaml.j2'}\n        dest: {tmp_path / 'config.yaml'}\n"
        "      no_log: true\n      when: k3s_mode == 'server'\n"
    )
    # The memory fact the config's kubelet reserve reads; this play gathers no facts.
    extra = {**tokens, "jarvis_env": "fixture", "k3s_mode": mode, "k3s_node_memtotal_mb": 16384}
    site = site_vars_file(tmp_path / "vars.json")
    return playbook(local_play(tmp_path, tasks), "-i", inventory, "-e", f"@{site}", "-e", json.dumps(extra), json=True)


@pytest.mark.parametrize(
    ("mode", "read"),
    [
        pytest.param("server", {"server": _SERVER_TOKEN, "agent": _AGENT_TOKEN}, id="server"),
        pytest.param("agent", {"server": "", "agent": _AGENT_TOKEN}, id="agent"),
    ],
)
def test_tokens_come_from_the_sealed_file(tmp_path: Path, mode: str, read: dict[str, str]) -> None:
    """Rule: the controller decrypts the sealed file; an agent gets the agent token only; no token prints."""
    run = _read_tokens(tmp_path, _tokens(tmp_path), mode)
    assert run.returncode == 0 and ran(run, "Read the sealed k3s tokens") == {"localhost": "ok"}, run.output
    assert json.loads((tmp_path / "facts.json").read_text(encoding="utf-8")) == read
    assert _SERVER_TOKEN not in run.output and _AGENT_TOKEN not in run.output, run.output


def test_token_and_config_tasks_converge(tmp_path: Path) -> None:
    """Rule: a second run with the same sealed file and node token changes nothing; the first writes the tokens and the
    config."""
    tokens = _tokens(tmp_path, node=f"K10ab::server:{_SERVER_TOKEN}")
    first, again = (_read_tokens(tmp_path, tokens) for _ in range(2))
    assert first.returncode == 0 and changed(first) == {"localhost": 2}, first.output
    assert again.returncode == 0 and changed(again) == {"localhost": 0}, again.output


_KEY_NAMES = ("Require the sealed token file and its key names",
              "k3s-token.sops.yaml for fixture is missing or lacks stringData.agent_token (and stringData.server_token on the"
              " server). See docs/backups.md, Rebuild a dead node.")


@pytest.mark.parametrize(
    ("broken", "refusal"),
    [
        pytest.param(lambda token, key: key.unlink(), ("Require the age private key on the controller",
                     "Every k3s run, check mode included, decrypts the sealed k3s tokens. Set SOPS_AGE_KEY_FILE or age_key_file"
                     " to the age private key on the controller."), id="no-key"),
        # The refusal's no_log hides its message, from the operator too.
        pytest.param(lambda token, key: key.chmod(0o644), ("Refuse an age private key that is not mode 0600 and owned by the runner", ""),
                     id="key-mode"),
        pytest.param(lambda token, key: token.unlink(), _KEY_NAMES, id="missing"),
        pytest.param(lambda token, key: token.write_text("stringData:\n  token: ENC[fixture]\n", encoding="utf-8"), _KEY_NAMES, id="key-names"),
    ],
)
def test_token_read_stops_visibly_before_any_decrypt(tmp_path: Path, broken, refusal: tuple[str, str]) -> None:
    """Rule: a missing or loose key file, or a sealed file that is missing or lacks the key names, stops the run at its
    own check, before the decrypt."""
    tokens = _tokens(tmp_path)
    broken(Path(tokens["k3s_token_file"]), Path(tokens["sops_age_key_file"]))
    run = _read_tokens(tmp_path, tokens)
    assert run.returncode != 0 and failed_task(run) == refusal, run.output
    assert ran(run, "Read the sealed k3s tokens") == {} and not (tmp_path / "facts.json").exists()


_DIFFERS = ("Refuse a sealed server token that differs from this node's",
            "The k3s server token on localhost differs from the sealed token in k3s-token.sops.yaml. Nothing was changed.")
_FRESH = ("Refuse a sealed secure-form server token on a server with no token file",
          "The sealed server_token in k3s-token.sops.yaml is the K10...::server: form, and localhost has no k3s token file yet,"
          " so it would mint a new CA and reject that token. Nothing was changed. Seal only the passphrase after ::server:"
          " (see docs/backups.md, Rebuild a dead node).")


def _refused(run: subprocess.CompletedProcess[str], task: str, message: str) -> bool:
    """Whether run stopped at task with a message that starts with message. _DIFFERS leaves out the runbook's path:
    select-tests would read it in this file as naming core's backups folder."""
    found = failed_task(run)
    return found is not None and found[0] == task and found[1].startswith(message)


@pytest.mark.parametrize(
    ("sealed", "node", "refusal"),
    [
        pytest.param(_SERVER_TOKEN, f"K10abc123::server:{_SERVER_TOKEN}", None, id="passphrase-matches"),
        pytest.param(_SERVER_TOKEN, f"K10abc123::server:{_NODE_PASSPHRASE}", _DIFFERS, id="passphrase-differs"),
        pytest.param(f"K10abc123::server:{_SERVER_TOKEN}", f"K10abc123::server:{_SERVER_TOKEN}", None, id="secure-form-matches"),
        pytest.param(f"K10abc123::server:{_SERVER_TOKEN}", f"K10def456::server:{_SERVER_TOKEN}", _DIFFERS, id="secure-form-differs"),
        pytest.param(f"K10abc123::server:{_SERVER_TOKEN}", None, _FRESH, id="secure-form-on-a-fresh-server"),
    ],
)
def test_server_refuses_a_sealed_token_that_differs_from_the_node(
    tmp_path: Path, sealed: str, node: str | None, refusal: tuple[str, str] | None
) -> None:
    """Rule: a server whose token differs from the sealed one stops before the config, as does a fresh server given a
    sealed K10 token (it would mint a new CA); a sealed K10 token must match whole, and no token prints."""
    run = _read_tokens(tmp_path, _tokens(tmp_path, sealed, node))
    assert (run.returncode != 0) is (refusal is not None), run.output
    assert _refused(run, *refusal) if refusal else failed_task(run) is None, run.output
    assert (tmp_path / "facts.json").exists() is (refusal is None)
    assert not [value for value in (_NODE_PASSPHRASE, _SERVER_TOKEN, _AGENT_TOKEN, "K10abc123") if value in run.output], run.output


def _server_role(tmp_path: Path, *, running: bool, node: str | None = None) -> tuple[subprocess.CompletedProcess[str], Path]:
    """The whole role as a server in check mode, on tmp paths and the fake systemctl, with k3s active and an older
    config when running. Returns the run and systemctl's log."""
    require_cli("ansible-playbook")
    log = fake_systemctl(tmp_path / "bin", running=running)
    binary = tmp_path / "k3s-binary"
    binary.write_bytes(b"fixture k3s\n")
    (tmp_path / "systemd" / "k3s.service.d").mkdir(parents=True)
    if running:
        (tmp_path / "config.yaml").write_text("# an older config\n", encoding="utf-8")
    extra = {**_tokens(tmp_path, node=node), "jarvis_env": "fixture", "jarvis_marker_path": str(tmp_path / "env"),
             "k3s_config_path": str(tmp_path / "config.yaml"), "k3s_unit_dir": str(tmp_path / "systemd"),
             "k3s_binary_path": str(binary), "k3s_version": "v0.0.0+k3s1", "k3s_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
             "local_path_data": str(tmp_path / "local"), "backup_path": str(tmp_path / "backup"), "k3s_node_memtotal_mb": 16384}
    play = local_play(tmp_path, role_tasks("k3s"), bin_dir=tmp_path / "bin", vars={"k3s_mode": "server"})
    site = site_vars_file(tmp_path / "vars.json")
    return playbook(play, *LOCALHOST, "-e", f"@{site}", "-e", json.dumps(extra), "--check", json=True), log


def test_a_token_mismatch_stops_the_role_before_its_config(tmp_path: Path) -> None:
    """Rule: on a running server whose token differs from the sealed one, the role stops before its config task and
    prints no token."""
    run, _log = _server_role(tmp_path, running=True, node=f"K10ab::server:{_NODE_PASSPHRASE}")
    assert run.returncode != 0 and _refused(run, *_DIFFERS), run.output
    assert ran(run, "Install k3s server config") == {}
    assert not [value for value in (_NODE_PASSPHRASE, _SERVER_TOKEN, _AGENT_TOKEN) if value in run.output], run.output


@pytest.mark.parametrize(
    ("running", "restart"), [pytest.param(False, "skipped", id="first-install"), pytest.param(True, "changed", id="config-change")]
)
def test_first_install_does_not_restart_and_a_config_change_does(tmp_path: Path, running: bool, restart: str) -> None:
    """Rule: a changed config restarts k3s only if it was running before this run; check mode changes no unit."""
    run, log = _server_role(tmp_path, running=running)
    assert run.returncode == 0, run.output
    assert (ran(run, "Install k3s server config"), ran(run, "Restart k3s")) == ({"localhost": "changed"}, {"localhost": restart})
    assert _calls(log) == ["is-active k3s"]


_RESERVES = "k3s_kubelet_kube_reserved: cpu=100m,memory=128Mi\nk3s_kubelet_system_reserved: 'cpu=100m,memory=128Mi'\n"


@pytest.mark.parametrize(
    ("lines", "memtotal_mb", "eviction"),
    [
        ("k3s_kubelet_eviction_hard: memory.available<100Mi\nnode_memory_reserve_percent: 10\n", 16384, "memory.available<10%"),
        # A quoted percent: env-schema refuses it in an env, and the role still reads it.
        ('k3s_kubelet_eviction_hard: "memory.available<100Mi,nodefs.available<10%"\nnode_memory_reserve_percent: "15"\n',
         16384, "memory.available<15%,nodefs.available<10%"),
        ("k3s_kubelet_eviction_hard: 'nodefs.available<10%'\nnode_memory_reserve_percent: 10\n", 4096, "nodefs.available<10%,memory.available<10%"),
        ("k3s_kubelet_eviction_hard: memory.available<2Gi\nnode_memory_reserve_percent: 10\n", 16384, "memory.available<2Gi"),
        ("k3s_kubelet_eviction_hard: memory.available<5%\nnode_memory_reserve_percent: 10\n", 16384, "memory.available<10%"),
        ("k3s_kubelet_eviction_hard: memory.available<100Mi\nnode_memory_reserve_percent: 0\n", 16384, "memory.available<100Mi"),
        ("k3s_kubelet_eviction_hard: 'nodefs.available<10%'\nnode_memory_reserve_percent: 0\n", 4096, "nodefs.available<10%"),
    ],
    ids=["reserve-wins", "quoted-keeps-other-signals", "appended", "larger-env-value-stays", "smaller-percent-raised", "off",
         "off-adds-no-signal"],
)
def test_node_memory_reserve_sets_the_kubelet_eviction_threshold(
    tmp_path: Path, lines: str, memtotal_mb: int, eviction: str
) -> None:
    """Rule: every node's kubelet keeps node_memory_reserve_percent of its memory out of allocatable as
    memory.available<N%, unless the env's eviction-hard is larger, and 0 leaves it as written; other reserves stay."""
    run = _render_configs(tmp_path, {"node": {"k3s_node_memtotal_mb": memtotal_mb}}, **yaml.safe_load(_RESERVES + lines))
    assert run.returncode == 0, run.output
    reserves = ["kube-reserved=cpu=100m,memory=128Mi", "system-reserved=cpu=100m,memory=128Mi"]
    assert _kubelet_args(tmp_path, "node") == [[*reserves, f"eviction-hard={eviction}"]] * 2


def test_node_memory_reserve_refuses_what_it_cannot_apply(tmp_path: Path) -> None:
    """Rule: an eviction-hard memory.available it cannot compare stops the render and writes no config."""
    bad = "k3s_kubelet_eviction_hard: memory.available<lots\nnode_memory_reserve_percent: 10\n"
    run = _render_configs(tmp_path, {"node": {"k3s_node_memtotal_mb": 16384}}, **yaml.safe_load(_RESERVES + bad))
    assert run.returncode != 0 and failed_task(run) == (
        "ansible.builtin.template",
        "Task failed: The filter plugin 'with_memory_reserve' failed: eviction-hard memory.available<lots is not a quantity or a percent",
    ), run.output
    assert not list(tmp_path.glob("node-*"))
