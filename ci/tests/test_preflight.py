"""Opening play fails before any change (J12, D42, D46)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import ANSIBLE_DIR, load_cluster_vars, playbook_recap_field
from conftest import require_cli

_REQUIRED = (
    "k3s_version",
    "k3s_sha256",
    "pod_cidr",
    "service_cidr",
    "backup_path",
    "local_path_data",
    "jarvis_env",
)


def _inventory(path: Path, *, data_disk_hosts: set[str] | None = None) -> None:
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
            "children": {"control_plane": {"hosts": {"cp-1": None}}},
        }
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _vars_file(path: Path, *, drop: str | None = None) -> None:
    cluster = load_cluster_vars()
    body = {key: cluster[key] for key in _REQUIRED}
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
) -> subprocess.CompletedProcess[str]:
    require_cli("ansible-playbook")
    inventory = tmp_path / "hosts.yml"
    _inventory(inventory, data_disk_hosts=data_disk_hosts)
    vars_path = tmp_path / "cluster-vars.yaml"
    _vars_file(vars_path, drop=drop)
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


@pytest.mark.parametrize("key", _REQUIRED)
def test_missing_cluster_var_fails_before_any_change(tmp_path: Path, key: str) -> None:
    """Each required cluster-vars key fails the opening play and changes nothing."""
    result = _run_site(tmp_path, drop=key)
    _stopped_before_base_os(
        result,
        "site.yml requires k3s_version, k3s_sha256, CIDRs, paths, and jarvis_env",
    )


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
    tasks = ANSIBLE_DIR / "tasks"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: Assert facts before any change
              hosts: all
              gather_facts: false
              become: false
              any_errors_fatal: true
              vars_files:
                - {vars_path}
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
    extra = [
        "-e",
        "ansible_connection=local",
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
