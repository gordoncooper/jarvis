"""site.yml's opening play: a host it refuses stops every host before any change; a host it passes changes nothing."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from ansible_role_proof import changed, fake_systemctl, playbook, plays, ran, roles_inventory_path, site_vars_file
from conftest import require_cli

_BOTH = ("cp-1", "wk-1")
# The guard's probe on each host: cp-1 runs the k3s server, wk-1 the agent.
_PROBES = ["is-active k3s", "is-active k3s-agent"]
_MEMORY = "Require a readable eviction-hard memory.available on each host"
_UNREADABLE = "eviction-hard's memory.available on {host} is not a Kubernetes quantity (such as 100Mi) or a percent (such as 10%)."
_FLUX = "Require the env FluxInstance and Flux sources in Git"
_CONTROL_PLANES = "Assert exactly one control_plane host"
_COUNTED = ("control_plane has {} hosts; expected exactly one. Pass both inventories with jarvis site --env <env> (hosts file,"
            " then infra/inventory/roles).")
_UNMARKED = ("Refuse host with foreign or unmarked k3s", "k3s is already present on this host but {tmp}/missing-marker is missing"
             " or does not contain 'env-00'. Wipe the node or fix the marker before re-running this env.")


def _site(tmp_path: Path, *flags: str, control_plane: tuple[str, ...] = ("cp-1",), gpu: tuple[str, ...] = (),
          host_vars: dict[str, dict] | None = None, running: bool = False, is_active_rc: int | None = 4, fail: str = "",
          **values: object) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """site.yml --tags always (the opening play's tasks, no later role) as jarvis site runs it, for cp-1 and wk-1 on this
    machine through core's roles inventory: cp-1 the control plane unless control_plane says otherwise, gpu naming the gpu
    hosts, host_vars a host's own vars. The sample env's vars with values over them (None drops one); k3s's paths and the
    data path point at nothing in tmp_path; the fake systemctl's is-active exits 4 (no such unit) unless told otherwise.
    Returns the run and the systemctl calls, sorted."""
    require_cli("ansible-playbook")
    hosts = {name: {"node_roles": ["control_plane" if name in control_plane else "workers", *(["gpu"] if name in gpu else [])],
                    **(host_vars or {}).get(name, {})} for name in _BOTH}
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(json.dumps({"all": {"hosts": hosts}}), encoding="utf-8")
    log = fake_systemctl(tmp_path / "bin", running=running, fail=fail, is_active_rc=is_active_rc)
    paths = {"k3s_binary_path": "missing-k3s", "k3s_data_dir": "missing-data", "jarvis_marker_path": "missing-marker",
             "node_disk_mount": "missing-mount"}
    site = site_vars_file(tmp_path / "vars.json", **{"ansible_connection": "local", "ansible_become": False,
                                                     **{key: str(tmp_path / name) for key, name in paths.items()}, **values})
    run = playbook("site.yml", "-i", inventory, "-i", roles_inventory_path(), "-e", f"@{site}", "--tags", "always", *flags,
                   json=True, env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    return run, sorted(log.read_text(encoding="utf-8").splitlines())


def _failures(run: subprocess.CompletedProcess[str]) -> dict[str, tuple[str, str]]:
    """Each host's failed task in a json=True run, named as in its task file, and its msg."""
    return {host: (task["task"]["name"].split(" : ", 1)[-1], result.get("msg", "")) for play in run.json["plays"]
            for task in play["tasks"] for host, result in task["hosts"].items() if result.get("failed")}


@pytest.mark.parametrize(
    ("knobs", "refusal", "hosts", "probes"),
    [
        pytest.param({"ansible_facts": {"architecture": "aarch64"}}, ("Require an amd64 host", "k3s role supports only amd64 hosts."),
                     _BOTH, [], id="not-amd64"),
        pytest.param({"control_plane": ()}, (_CONTROL_PLANES, _COUNTED.format(0)), _BOTH, [], id="no-control-plane"),
        pytest.param({"control_plane": _BOTH}, (_CONTROL_PLANES, _COUNTED.format(2)), _BOTH, [], id="two-control-planes"),
        pytest.param({"k3s_kubelet_eviction_hard": "memory.available<100MB"}, (_MEMORY, _UNREADABLE), _BOTH, [],
                     id="memory-in-megabytes"),
        pytest.param({"k3s_kubelet_eviction_hard": "nodefs.available<10%,memory.available<"}, (_MEMORY, _UNREADABLE), _BOTH, [],
                     id="memory-without-a-value"),
        # The k3s role strips each signal, so a space after the comma still brings memory.available to the check.
        pytest.param({"k3s_kubelet_eviction_hard": "nodefs.available<10%, memory.available<100MB"}, (_MEMORY, _UNREADABLE), _BOTH,
                     [], id="memory-after-a-space"),
        # A host's own value, which the roles inventory composes, since the env's extra vars would hide it.
        pytest.param({"host_vars": {"wk-1": {"k3s_kubelet_eviction_hard": "memory.available<100MB"}}}, (_MEMORY, _UNREADABLE),
                     ("wk-1",), [], id="host-memory-in-megabytes"),
        pytest.param({"jarvis_instance_root": None}, ("Require the instance repository", "jarvis_instance_root is not set: run"
                     " jarvis site --env <env> from the instance repository, which passes it."), _BOTH, [], id="no-instance-repository"),
        pytest.param({"jarvis_env": "env-absent"}, (_FLUX, "core/flux/flux-instance.yaml (core's) or envs/env-absent/flux/sync.yaml"
                     " is missing. flux-operator applies them on a new cluster; commit sync.yaml before running site.yml."),
                     _BOTH, [], id="env-without-its-flux"),
        pytest.param({"flux_instance_file": "/nonexistent/flux-instance.yaml"}, (_FLUX, "core/flux/flux-instance.yaml (core's) or"
                     " envs/env-00/flux/sync.yaml is missing. flux-operator applies them on a new cluster; commit sync.yaml before"
                     " running site.yml."), _BOTH, [], id="no-core-flux-instance"),
        pytest.param({"gpu": ("cp-1",), "node_pci_devices_path": "/nonexistent/pci-devices"},
                     ("A declared gpu host has an NVIDIA device", "gpu host cp-1 has no NVIDIA device."), ("cp-1",), [],
                     id="gpu-host-without-nvidia"),
        # k3s is on the host when it is active, or its binary or data directory is there (any file, any directory).
        pytest.param({"running": True, "is_active_rc": None}, _UNMARKED, _BOTH, _PROBES, id="unmarked-k3s"),
        pytest.param({"k3s_binary_path": "/bin/sh"}, _UNMARKED, _BOTH, _PROBES, id="unmarked-k3s-binary"),
        pytest.param({"k3s_data_dir": "/"}, _UNMARKED, _BOTH, _PROBES, id="unmarked-k3s-data"),
        pytest.param({"fail": "is-active"}, ("Probe whether k3s service is active", "The command exited with a non-zero return code."),
                     _BOTH, _PROBES, id="probe-error"),
        pytest.param({"host_vars": {"cp-1": {"data_disk": True}}}, ("Refuse a missing data path", "data path {tmp}/missing-mount does"
                     " not exist or is not a directory."), ("cp-1",), _PROBES, id="one-bad-data-disk"),
    ],
)
def test_a_refused_host_stops_every_host_before_any_change(
    tmp_path: Path, knobs: dict[str, object], refusal: tuple[str, str], hosts: tuple[str, ...], probes: list[str]
) -> None:
    """Rule: each refusal of the opening play (arch, control plane count, memory.available, instance, Flux files, GPU, an
    unmarked k3s, a failed probe, a data disk) stops every host before Base OS, with nothing changed and no marker written."""
    task, message = refusal
    expected = {host: (task, message.format(host=host, tmp=tmp_path)) for host in hosts}
    run, calls = _site(tmp_path, "--check", **knobs)
    assert run.returncode != 0 and _failures(run) == expected, run.output
    assert (plays(run), changed(run), calls) == (["Assert facts before any change"], dict.fromkeys(_BOTH, 0), probes)
    assert not (tmp_path / "missing-marker").exists()


# The checks a passing host must have run, not skipped: the memory threshold, the Flux files, the probe and the guard.
_PASSED = (_MEMORY, _FLUX, "Probe whether k3s service is active", "Refuse host with foreign or unmarked k3s")


@pytest.mark.parametrize(
    ("knobs", "flags"),
    [
        pytest.param({"k3s_kubelet_eviction_hard": "memory.available<1.5Gi,nodefs.available<10%", "is_active_rc": None}, (),
                     id="quantity-and-k3s-inactive"),
        pytest.param({"k3s_kubelet_eviction_hard": "memory.available<7%"}, ("--check",), id="percent-check-mode"),
    ],
)
def test_a_host_the_opening_play_passes_changes_nothing(tmp_path: Path, knobs: dict[str, object], flags: tuple[str, ...]) -> None:
    """Rule: a readable memory.available and k3s inactive (is-active 3) or absent (4) pass the opening play, changing nothing,
    in check mode too; it reads core's FluxInstance beside the playbook and sync.yaml in the instance, which holds no core."""
    run, calls = _site(tmp_path, *flags, **knobs)
    assert run.returncode == 0 and changed(run) == dict.fromkeys(_BOTH, 0), run.output
    assert {task: ran(run, task) for task in _PASSED} == dict.fromkeys(_PASSED, dict.fromkeys(_BOTH, "ok"))
    assert calls == _PROBES and not (tmp_path / "missing-marker").exists()
