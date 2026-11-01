"""node-disk only checks a declared data disk (p0a-11b)."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from ansible_role_proof import ANSIBLE_DIR, playbook_recap_field
from conftest import require_cli

_ROOT_ID = "8:1"
_DISK_ID = "9:1"


def _install_bin(bin_dir: Path, mounts: dict[str, dict[str, str]]) -> None:
    bin_dir.mkdir()
    (bin_dir / "mounts.json").write_text(json.dumps(mounts), encoding="utf-8")
    (bin_dir / "findmnt").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys
            from pathlib import Path

            table = json.loads(Path(__file__).with_name("mounts.json").read_text())
            args = sys.argv[1:]
            target = None
            field = None
            index = 0
            while index < len(args):
                if args[index] == "-T" and index + 1 < len(args):
                    target = args[index + 1]
                    index += 2
                    continue
                if args[index] == "-o" and index + 1 < len(args):
                    field = args[index + 1]
                    index += 2
                    continue
                index += 1
            row = table.get(target)
            if row is None or field not in row:
                sys.stderr.write(f"findmnt: no {field} for {target}\\n")
                sys.exit(1)
            sys.stdout.write(row[field] + "\\n")
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "findmnt").chmod(0o755)
    for name in ("wipefs", "mkfs.ext4", "parted", "mount"):
        tool = bin_dir / name
        tool.write_text(
            "#!/bin/sh\necho 'disk command invoked' >&2\nexit 99\n",
            encoding="utf-8",
        )
        tool.chmod(0o755)


def _mount_row(target: str, maj_min: str) -> dict[str, str]:
    return {"TARGET": target, "MAJ:MIN": maj_min}


def _run(
    tmp_path: Path,
    *,
    mount: Path,
    mounts: dict[str, dict[str, str]],
    extra: dict[str, object],
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    require_cli("ansible-playbook")
    bin_dir = tmp_path / "bin"
    if not (bin_dir / "findmnt").exists():
        _install_bin(bin_dir, mounts)
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: Check data disk
              hosts: localhost
              gather_facts: false
              become: false
              environment:
                PATH: "{bin_dir}:/usr/bin:/bin"
              tasks:
                - ansible.builtin.include_role:
                    name: node-disk
            """
        ),
        encoding="utf-8",
    )
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
        json.dumps({"node_disk_mount": str(mount), **extra}),
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


def _paths(mount: Path, k3s_data: Path) -> dict[str, object]:
    return {
        "data_disk": True,
        "local_path_data": str(mount / "local-path"),
        "backup_path": str(mount / "backup"),
        "k3s_data_dir": str(k3s_data),
    }


def _own_mount(mount: Path) -> dict[str, dict[str, str]]:
    return {
        str(mount): _mount_row(str(mount), _DISK_ID),
        "/": _mount_row("/", _ROOT_ID),
    }


def test_node_disk_refuses_a_missing_path(tmp_path: Path) -> None:
    mount = tmp_path / "missing"
    result = _run(
        tmp_path,
        mount=mount,
        mounts=_own_mount(mount),
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "does not exist" in output
    assert "disk command invoked" not in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_refuses_a_path_that_is_not_a_mount(tmp_path: Path) -> None:
    """A directory inside a non-root mount is not itself a mount."""
    parent = tmp_path / "disk"
    mount = parent / "jarvis"
    mount.mkdir(parents=True)
    parent_id = "9:2"
    mounts = {
        str(mount): _mount_row(str(parent), parent_id),
        str(parent): _mount_row(str(parent), parent_id),
        "/": _mount_row("/", _ROOT_ID),
    }
    result = _run(
        tmp_path,
        mount=mount,
        mounts=mounts,
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "is not a mount" in output
    assert "its own filesystem" not in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_refuses_a_mount_on_the_root_filesystem(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    mount.mkdir()
    mounts = {
        str(mount): _mount_row(str(mount), _ROOT_ID),
        "/": _mount_row("/", _ROOT_ID),
    }
    result = _run(
        tmp_path,
        mount=mount,
        mounts=mounts,
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "part of /" in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_refuses_unrelated_entries(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    mount.mkdir()
    (mount / "notes.txt").write_text("foreign\n", encoding="utf-8")
    result = _run(
        tmp_path,
        mount=mount,
        mounts=_own_mount(mount),
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "notes.txt" in output
    assert "not Jarvis data" in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_marker_without_k3s_state_allows_an_empty_mount(tmp_path: Path) -> None:
    """The env marker is written before k3s runs, so it is not evidence of data."""
    mount = tmp_path / "data"
    mount.mkdir()
    (mount / "lost+found").mkdir()
    marker = tmp_path / "env"
    marker.write_text("env-test\n", encoding="utf-8")
    k3s_data = tmp_path / "k3s-data"
    (k3s_data / "server").mkdir(parents=True)
    extra = _paths(mount, k3s_data)
    extra["jarvis_marker_path"] = str(marker)
    result = _run(tmp_path, mount=mount, mounts=_own_mount(mount), extra=extra)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0
    assert marker.read_text(encoding="utf-8") == "env-test\n"


@pytest.mark.parametrize("k3s_role", ["server", "agent"])
def test_node_disk_refuses_an_empty_mount_when_k3s_has_run(tmp_path: Path, k3s_role: str) -> None:
    mount = tmp_path / "data"
    mount.mkdir()
    (mount / "lost+found").mkdir()
    k3s_data = tmp_path / "k3s-data"
    (k3s_data / k3s_role / "db").mkdir(parents=True)
    result = _run(tmp_path, mount=mount, mounts=_own_mount(mount), extra=_paths(mount, k3s_data))
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "no Jarvis data" in output
    assert "k3s server or agent state" in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_refuses_a_configured_path_outside_the_mount(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    mount.mkdir()
    extra = _paths(mount, tmp_path / "k3s-data")
    extra["local_path_data"] = str(tmp_path / "elsewhere")
    result = _run(tmp_path, mount=mount, mounts=_own_mount(mount), extra=extra)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "outside" in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_refuses_a_nested_mount(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    local = mount / "local-path"
    local.mkdir(parents=True)
    mounts = _own_mount(mount)
    mounts[str(local)] = _mount_row(str(local), "9:2")
    result = _run(
        tmp_path,
        mount=mount,
        mounts=mounts,
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "outside" in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_refuses_backup_path_on_another_mount(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    local = mount / "local-path"
    backup = mount / "backup"
    local.mkdir(parents=True)
    backup.mkdir()
    mounts = _own_mount(mount)
    mounts[str(local)] = _mount_row(str(mount), _DISK_ID)
    mounts[str(backup)] = _mount_row(str(backup), "9:3")
    result = _run(
        tmp_path,
        mount=mount,
        mounts=mounts,
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "backup_path" in output
    assert "outside" in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_refuses_local_path_data_equal_to_the_mount(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    mount.mkdir()
    extra = _paths(mount, tmp_path / "k3s-data")
    extra["local_path_data"] = str(mount)
    result = _run(tmp_path, mount=mount, mounts=_own_mount(mount), extra=extra)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "subdirectory" in output
    assert playbook_recap_field(output, "changed") == 0


def test_node_disk_accepts_an_empty_mount_and_changes_nothing(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    mount.mkdir()
    before = sorted(path.name for path in mount.iterdir())
    result = _run(
        tmp_path,
        mount=mount,
        mounts=_own_mount(mount),
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0
    assert sorted(path.name for path in mount.iterdir()) == before
    assert "disk command invoked" not in output

    again = _run(
        tmp_path,
        mount=mount,
        mounts=_own_mount(mount),
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    again_out = again.stdout + again.stderr
    assert again.returncode == 0, again_out
    assert playbook_recap_field(again_out, "changed") == 0

    checked = _run(
        tmp_path,
        mount=mount,
        mounts=_own_mount(mount),
        extra=_paths(mount, tmp_path / "k3s-data"),
        check=True,
    )
    checked_out = checked.stdout + checked.stderr
    assert checked.returncode == 0, checked_out
    assert playbook_recap_field(checked_out, "changed") == 0


def test_node_disk_accepts_only_jarvis_directories(tmp_path: Path) -> None:
    mount = tmp_path / "data"
    for name in ("local-path", "backup", "lost+found"):
        (mount / name).mkdir(parents=True)
    (mount / "backup" / "etcd-snapshots").mkdir()
    mounts = _own_mount(mount)
    for name in ("local-path", "backup"):
        mounts[str(mount / name)] = _mount_row(str(mount), _DISK_ID)
    result = _run(
        tmp_path,
        mount=mount,
        mounts=mounts,
        extra=_paths(mount, tmp_path / "k3s-data"),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0
    assert {path.name for path in mount.iterdir()} == {"local-path", "backup", "lost+found"}


def test_node_disk_changes_nothing_without_data_disk(tmp_path: Path) -> None:
    mount = tmp_path / "absent"
    extra = _paths(mount, tmp_path / "k3s-data")
    extra.pop("data_disk")
    result = _run(tmp_path, mount=mount, mounts={}, extra=extra)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0
    assert not mount.exists()
    assert "findmnt:" not in output
