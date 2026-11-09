"""Opening play fails before any change."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    example_flux_entry,
    load_cluster_vars,
    playbook_recap_field,
    roles_inventory_path,
)
from conftest import require_cli

from support import REPO_ROOT, bootstrap_example_copy

_EXAMPLE_SYNC = REPO_ROOT / "envs" / "env-00" / "flux" / "sync.yaml"

_REQUIRED = (
    "k3s_version",
    "k3s_sha256",
    "pod_cidr",
    "service_cidr",
    "backup_path",
    "local_path_data",
    "jarvis_env",
)


def _inventory(
    path: Path,
    *,
    data_disk_hosts: set[str] | None = None,
    control_plane: tuple[str, ...] = ("cp-1",),
) -> None:
    marked = data_disk_hosts or set()
    hosts: dict[str, dict[str, object]] = {
        "cp-1": {"ansible_connection": "local"},
        "wk-1": {"ansible_connection": "local"},
    }
    for name in marked:
        hosts[name]["data_disk"] = True
    doc = {
        "all": {
            "hosts": hosts,
            "children": {"control_plane": {"hosts": {name: None for name in control_plane}}},
        }
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _vars_file(
    path: Path,
    *,
    drop: str | None = None,
    jarvis_env: str | None = None,
    override: dict[str, object] | None = None,
) -> None:
    from jarvis.env import k3s

    cluster = load_cluster_vars()
    # The env's k3s pair is optional (core's release otherwise); the opening play reads it either way.
    cluster["k3s_version"], cluster["k3s_sha256"] = k3s.release(cluster)
    body = {key: cluster[key] for key in _REQUIRED}
    body.update(override or {})
    if jarvis_env is not None:
        body["jarvis_env"] = jarvis_env
    if drop is not None:
        del body[drop]
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def _assert_unchanged(output: str) -> None:
    recap = output.split("PLAY RECAP", 1)[-1]
    lines = [line for line in recap.splitlines() if "changed=" in line]
    assert len(lines) >= 2, output
    assert all(re.search(r"changed=0\b", line) for line in lines), output


def _run_site(
    tmp_path: Path,
    *,
    drop: str | None = None,
    extra: list[str] | None = None,
    data_disk_hosts: set[str] | None = None,
    check: bool = True,
    tags: str | None = None,
    path_prefix: Path | None = None,
    jarvis_env: str | None = None,
    flux_instance: bool = True,
    control_plane: tuple[str, ...] = ("cp-1",),
    override: dict[str, object] | None = None,
    sync_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """flux_instance passes the example env's FluxInstance; False keeps the jarvis_env path.
    sync_file replaces the example env's sync.yaml."""
    require_cli("ansible-playbook")
    inventory = tmp_path / "hosts.yml"
    _inventory(inventory, data_disk_hosts=data_disk_hosts, control_plane=control_plane)
    vars_path = tmp_path / "cluster-vars.yaml"
    _vars_file(vars_path, drop=drop, jarvis_env=jarvis_env, override=override)
    cmd = [
        "ansible-playbook",
        "site.yml",
        "-i",
        str(inventory),
        "-e",
        "ansible_connection=local",
        "-e",
        "ansible_become=false",
        "-e",
        f"jarvis_cluster_vars={vars_path}",
        *(extra or []),
    ]
    if flux_instance:
        cmd.extend(["-e", f"flux_instance_file={example_flux_entry(tmp_path).instance_file}"])
        cmd.extend(["-e", f"flux_sync_file={sync_file or _EXAMPLE_SYNC}"])
    if check:
        cmd.append("--check")
    if tags is not None:
        cmd.extend(["--tags", tags])
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env.get('PATH', '')}"
    return subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _k3s_paths(tmp_path: Path) -> list[str]:
    return [
        "-e",
        json.dumps(
            {
                "k3s_binary_path": str(tmp_path / "missing-k3s"),
                "k3s_data_dir": str(tmp_path / "missing-data"),
                "jarvis_marker_path": str(tmp_path / "missing-marker"),
            }
        ),
    ]


def _install_systemctl(bin_dir: Path, *, active_rc: int) -> None:
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "is-active" ]; then
              if [ "{active_rc}" -eq 0 ]; then
                echo active
              elif [ "{active_rc}" -eq 1 ]; then
                echo "systemctl: dbus connection failed" >&2
              else
                echo inactive
              fi
              exit {active_rc}
            fi
            echo "unexpected systemctl $*" >&2
            exit 1
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "systemctl").chmod(0o755)


def _stopped_before_base_os(result: subprocess.CompletedProcess[str], needle: str) -> None:
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert needle in output
    assert "PLAY [Base OS]" not in output
    _assert_unchanged(output)


def test_non_x86_host_fails_before_any_change(tmp_path: Path) -> None:
    result = _run_site(
        tmp_path,
        extra=["-e", '{"ansible_facts":{"architecture":"aarch64"}}'],
    )
    _stopped_before_base_os(result, "k3s role supports only amd64 hosts.")


def test_every_required_cluster_var_is_asserted() -> None:
    """Rule: the opening play's cluster-vars assert names every required key."""
    tasks = yaml.safe_load((REPO_ROOT / "infra" / "ansible" / "tasks" / "preflight.yml").read_text(encoding="utf-8"))
    that = next(task for task in tasks if task.get("name") == "Require k3s cluster-vars")["ansible.builtin.assert"]["that"]
    assert {line.split()[0] for line in that if line.endswith("is defined")} == set(_REQUIRED)


def test_missing_cluster_var_fails_before_any_change(tmp_path: Path) -> None:
    """A required cluster-vars key that is missing fails the opening play and changes nothing."""
    result = _run_site(tmp_path, drop="jarvis_env")
    _stopped_before_base_os(
        result,
        "site.yml requires k3s_version, k3s_sha256, CIDRs, paths, and jarvis_env",
    )


@pytest.mark.parametrize("control_plane", [(), ("cp-1", "wk-1")], ids=["none", "two"])
def test_not_exactly_one_control_plane_fails_before_any_change(
    tmp_path: Path, control_plane: tuple[str, ...]
) -> None:
    result = _run_site(tmp_path, control_plane=control_plane)
    _stopped_before_base_os(result, "expected exactly one")


@pytest.mark.parametrize(
    "override",
    [{"k3s_sha256": "a" * 63}, {"k3s_version": ""}],
    ids=["sha-not-64", "empty-version"],
)
def test_malformed_k3s_pin_fails_before_any_change(
    tmp_path: Path, override: dict[str, object]
) -> None:
    """A checksum that is not 64 characters, or an empty version, stops the opening play."""
    result = _run_site(tmp_path, override=override)
    _stopped_before_base_os(
        result,
        "site.yml requires k3s_version, k3s_sha256, CIDRs, paths, and jarvis_env",
    )


def test_node_memory_reserve_out_of_range_fails_before_any_change(tmp_path: Path) -> None:
    """Rule: a node memory reserve that is no whole number from 0 to 50 stops the opening play."""
    result = _run_site(tmp_path, override={"node_memory_reserve_percent": 60})
    _stopped_before_base_os(result, "node_memory_reserve_percent is a whole number from 0 to 50")


@pytest.mark.parametrize("value", ["memory.available<100MB", "nodefs.available<10%,memory.available<"])
def test_unreadable_memory_threshold_fails_before_any_change(tmp_path: Path, value: str) -> None:
    """Rule: an eviction-hard memory.available the reserve cannot compare stops the opening play."""
    result = _run_site(tmp_path, override={"k3s_kubelet_eviction_hard": value})
    _stopped_before_base_os(result, "is not a Kubernetes quantity")


@pytest.mark.parametrize("value", ["memory.available<1.5Gi,nodefs.available<10%", "memory.available<7%"])
def test_readable_memory_threshold_passes_preflight(tmp_path: Path, value: str) -> None:
    """Rule: a quantity or percent memory.available, beside other signals, passes the check."""
    result = _run_site(tmp_path, override={"k3s_kubelet_eviction_hard": value})
    output = result.stdout + result.stderr
    task = output.split("TASK [Require a readable eviction-hard memory.available on each host]")[1].split("TASK [")[0]
    assert "ok: [cp-1]" in task and "ok: [wk-1]" in task and "failed" not in task, task


def test_env_without_its_flux_fails_before_any_change(tmp_path: Path) -> None:
    """site.yml applies core's FluxInstance and the env's sync.yaml, so an env without its flux/
    stops the opening play."""
    result = _run_site(tmp_path, jarvis_env="env-absent", flux_instance=False)
    _stopped_before_base_os(result, "core/flux/flux-instance.yaml (core's) or envs/env-absent/flux/sync.yaml is missing")


def test_missing_sync_yaml_with_flux_instance_present_fails_before_any_change(tmp_path: Path) -> None:
    """site.yml applies sync.yaml after flux-instance.yaml, so a missing sync.yaml stops it too."""
    result = _run_site(tmp_path, sync_file=tmp_path / "absent-sync.yaml")
    _stopped_before_base_os(result, "flux-instance.yaml (core's) or envs/")


def test_unmarked_k3s_fails_before_any_change(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _install_systemctl(bin_dir, active_rc=0)
    result = _run_site(
        tmp_path,
        extra=_k3s_paths(tmp_path),
        path_prefix=bin_dir,
    )
    _stopped_before_base_os(result, "k3s is already present")
    assert not (tmp_path / "missing-marker").exists()


def test_failing_data_disk_stops_every_host(tmp_path: Path) -> None:
    """One host with a bad data disk stops the run before base-os, including the other host."""
    bin_dir = tmp_path / "bin"
    _install_systemctl(bin_dir, active_rc=4)
    result = _run_site(
        tmp_path,
        extra=[
            *_k3s_paths(tmp_path),
            "-e",
            json.dumps({"node_disk_mount": str(tmp_path / "missing-mount")}),
        ],
        data_disk_hosts={"cp-1"},
        path_prefix=bin_dir,
    )
    _stopped_before_base_os(result, "does not exist")


def test_systemctl_inactive_rc_is_allowed(tmp_path: Path) -> None:
    """is-active exit 3 (inactive or failed) is success and is not 'k3s is present'."""
    bin_dir = tmp_path / "bin"
    _install_systemctl(bin_dir, active_rc=3)
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: Refuse unmarked k3s
              hosts: localhost
              gather_facts: false
              become: false
              environment:
                PATH: "{bin_dir}:/usr/bin:/bin"
              tasks:
                - ansible.builtin.import_role:
                    name: k3s
                    tasks_from: refuse.yml
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
            "jarvis_env=fixture",
            *_k3s_paths(tmp_path),
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "k3s is already present" not in output
    assert "Probe whether k3s service is active" in output
    assert playbook_recap_field(output, "changed") == 0


def test_systemctl_probe_error_is_not_treated_as_inactive(tmp_path: Path) -> None:
    """A failed is-active probe fails the run. It is not read as 'k3s is absent'."""
    bin_dir = tmp_path / "bin"
    _install_systemctl(bin_dir, active_rc=1)
    result = _run_site(
        tmp_path,
        extra=_k3s_paths(tmp_path),
        path_prefix=bin_dir,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Probe whether k3s service is active" in output
    assert "k3s is already present" not in output
    assert "PLAY [Base OS]" not in output
    _assert_unchanged(output)


def _write_opening_play(path: Path, *, bin_dir: Path, vars_file: str) -> None:
    tasks = ANSIBLE_DIR / "tasks"
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: Assert facts before any change
              hosts: all
              gather_facts: false
              become: false
              any_errors_fatal: true
              vars_files:
                - {vars_file}
              environment:
                PATH: "{bin_dir}:/usr/bin:/bin"
              tasks:
                - ansible.builtin.import_tasks: {tasks / "preflight.yml"}
                - ansible.builtin.import_tasks: {tasks / "assert-gpu.yml"}
                - ansible.builtin.import_role:
                    name: k3s
                    tasks_from: refuse.yml
                - ansible.builtin.import_role:
                    name: node-disk
            """
        ),
        encoding="utf-8",
    )


def test_preflight_second_run_changes_nothing(tmp_path: Path) -> None:
    """A host that passes the opening asserts changes nothing, including on --check."""
    require_cli("ansible-playbook")
    bin_dir = tmp_path / "bin"
    _install_systemctl(bin_dir, active_rc=4)
    inventory = tmp_path / "hosts.yml"
    _inventory(inventory)
    vars_path = tmp_path / "cluster-vars.yaml"
    _vars_file(vars_path)
    play = tmp_path / "play.yml"
    _write_opening_play(play, bin_dir=bin_dir, vars_file=str(vars_path))
    flux_instance = example_flux_entry(tmp_path).instance_file
    extra = [
        "-e",
        "ansible_connection=local",
        "-e",
        f"flux_instance_file={flux_instance}",
        "-e",
        f"flux_sync_file={_EXAMPLE_SYNC}",
        *_k3s_paths(tmp_path),
    ]
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")

    def run(*, check: bool) -> str:
        cmd = ["ansible-playbook", str(play), "-i", str(inventory), *extra]
        if check:
            cmd.append("--check")
        result = subprocess.run(
            cmd,
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "PLAY [Base OS]" not in output
        _assert_unchanged(output)
        assert playbook_recap_field(output, "changed") == 0
        return output

    run(check=False)
    run(check=False)
    run(check=True)


def test_copied_env_flux_instance_resolves_without_override(tmp_path: Path) -> None:
    """group_vars resolves core's core/flux/flux-instance.yaml beside the playbook, and that assert passes."""
    require_cli("ansible-playbook")
    hosts = (
        REPO_ROOT / "ci/tests/fixtures/env-shapes/single-node/hosts.yml"
    ).read_text(encoding="utf-8")
    copy = bootstrap_example_copy(tmp_path, hosts_text=hosts)
    playbook_dir = copy.root / "infra" / "ansible"
    (playbook_dir / "group_vars").mkdir(parents=True)
    shutil.copy(ANSIBLE_DIR / "group_vars" / "all.yml", playbook_dir / "group_vars" / "all.yml")
    playbook = playbook_dir / "open.yml"
    bin_dir = tmp_path / "bin"
    _install_systemctl(bin_dir, active_rc=4)
    _write_opening_play(
        playbook, bin_dir=bin_dir, vars_file='"{{ jarvis_cluster_vars }}"'
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    result = subprocess.run(
        [
            "ansible-playbook",
            str(playbook),
            "-i",
            str(copy.env_dir / "inventory" / "hosts.yml"),
            "-i",
            str(roles_inventory_path()),
            "-e",
            "ansible_connection=local",
            "-e",
            "ansible_become=false",
            "-e",
            f"jarvis_cluster_vars={copy.env_dir / 'values' / 'cluster-vars.yaml'}",
            *_k3s_paths(tmp_path),
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Require the env FluxInstance and Flux sources in Git" in output
    assert "flux/sync.yaml is missing" not in output
    assert "PLAY [Base OS]" not in output
    assert playbook_recap_field(output, "changed") == 0
