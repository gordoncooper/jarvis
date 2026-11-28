"""node-disk only checks a declared data disk: it refuses a data path it cannot use, accepts one holding only Jarvis data,
and changes nothing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from ansible_role_proof import LOCALHOST, changed, failed_task, fake_tool, local_play, playbook, role_tasks
from conftest import require_cli

_ROOT_ID = "8:1"
_DISK_ID = "9:1"
# findmnt -T <path> -o <field>, answered from MOUNTS: {path: {"TARGET": ..., "MAJ:MIN": ...}}.
_FINDMNT = r"""
import sys
args = sys.argv[1:]
path, field = args[args.index("-T") + 1], args[args.index("-o") + 1]
if field not in MOUNTS.get(path, {}):
    sys.exit(f"findmnt: no {field} for {path}")
print(MOUNTS[path][field])
"""
_ROOT = {"/": ("/", _ROOT_ID)}
_OWN = {**_ROOT, "data": ("data", _DISK_ID)}
_ON_ROOT = {**_ROOT, "data": ("data", _ROOT_ID)}
_JARVIS = {"data/local-path": None, "data/backup/etcd-snapshots": None, "data/lost+found": None}
_JARVIS_MOUNTS = {**_OWN, "data/local-path": ("data", _DISK_ID), "data/backup": ("data", _DISK_ID)}
_RERUN = {**_JARVIS, "k3s-data/server/db": None}
# What the fake tools print when the role misuses them: findmnt asked about a path it was not given, or any disk tool.
_COMPLAINTS = ("findmnt:", "disk command invoked")
_MISSING = ("Refuse a missing data path", "data path {root}/data does not exist or is not a directory.")
_ROOT_FS = ("Refuse a data path on the root filesystem", "{root}/data is not its own filesystem; it is part of /.")
_FOREIGN = "Refuse entries that are not Jarvis data"
_OUTSIDE = "Refuse a backup_path outside the data mount"
_EMPTY = ("Refuse an empty data path when k3s has already run", "{root}/data has no Jarvis data, but {root}/k3s-data already"
          " holds k3s server or agent state, so existing data is missing or hidden under the mount.")


def _host(tmp_path: Path, entries: dict[str, str | None]) -> Path:
    """tmp_path/host holding entries, each a file's text or None for a directory."""
    root = tmp_path / "host"
    root.mkdir()
    for name, text in entries.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            path.mkdir()
        else:
            path.write_text(text, encoding="utf-8")
    return root


def _tree(root: Path) -> dict[str, str | None]:
    """Every path under root, with each file's text."""
    return {str(path.relative_to(root)): path.read_text(encoding="utf-8") if path.is_file() else None for path in root.rglob("*")}


def _node_disk(tmp_path: Path, root: Path, mounts: dict[str, tuple[str, str]], *flags: str,
               **values: object) -> subprocess.CompletedProcess[str]:
    """node-disk on localhost for a data disk at root/data, local_path_data and backup_path in it and k3s_data_dir at
    root/k3s-data, values set over them (a str is a path under root; None unsets). findmnt answers from mounts
    ({path: (target, MAJ:MIN)}, under root), and every disk tool fails, saying so."""
    require_cli("ansible-playbook")
    table = {str(root / path): {"TARGET": str(root / target), "MAJ:MIN": number} for path, (target, number) in mounts.items()}
    fake_tool(tmp_path / "bin", "findmnt", f"#!{sys.executable}\nMOUNTS = {table!r}{_FINDMNT}")
    for name in ("wipefs", "mkfs.ext4", "parted", "mount"):
        fake_tool(tmp_path / "bin", name, "#!/bin/sh\necho 'disk command invoked' >&2\nexit 99\n")
    disk = {"data_disk": True, "node_disk_mount": "data", "local_path_data": "data/local-path", "backup_path": "data/backup",
            "k3s_data_dir": "k3s-data", **values}
    extra = {key: str(root / value) if isinstance(value, str) else value for key, value in disk.items() if value is not None}
    play = local_play(tmp_path, role_tasks("node-disk"), bin_dir=tmp_path / "bin")
    return playbook(play, *LOCALHOST, "-e", json.dumps(extra), *flags, json=True)


@pytest.mark.parametrize(
    ("entries", "mounts", "values", "flags", "refusal"),
    [
        pytest.param({}, _OWN, {}, (), _MISSING, id="missing"),
        pytest.param({"data": "not a directory\n"}, _OWN, {}, (), _MISSING, id="a-file"),
        pytest.param({"data": None}, {**_ROOT, "data": (".", "9:2")}, {}, (),
                     ("Refuse a data path that is not a mount", "{root}/data is not a mount; it is part of {root}."), id="not-a-mount"),
        pytest.param({"data": None}, _ON_ROOT, {}, (), _ROOT_FS, id="on-the-root-filesystem"),
        # As jarvis site --check runs it: if check mode skipped reading a filesystem id, the two ids would differ and it pass.
        pytest.param({"data": None}, _ON_ROOT, {}, ("--check",), _ROOT_FS, id="on-the-root-filesystem-check-mode"),
        pytest.param({"data/notes.txt": "foreign\n"}, _OWN, {}, (), (_FOREIGN, "{root}/data holds entries that are not Jarvis"
                     " data: notes.txt."), id="foreign-entry"),
        pytest.param({"data/.notes": "foreign\n"}, _OWN, {}, (), (_FOREIGN, "{root}/data holds entries that are not Jarvis data:"
                     " .notes."), id="hidden-foreign-entry"),
        pytest.param({"data/lost+found": None, "k3s-data/agent/db": None}, _OWN, {}, (), _EMPTY, id="empty-after-the-agent-ran"),
        pytest.param({"data/lost+found": None, "k3s-data/server/db": None}, _OWN, {}, (), _EMPTY, id="empty-after-the-server-ran"),
        pytest.param({"data": None}, _OWN, {"backup_path": "elsewhere"}, (), (_OUTSIDE, "backup_path ({root}/elsewhere) is outside"
                     " {root}/data."), id="backup-path-outside"),
        pytest.param({"data": None}, _OWN, {"backup_path": "data-backup"}, (), (_OUTSIDE, "backup_path ({root}/data-backup) is"
                     " outside {root}/data."), id="backup-path-beside-the-mount"),
        pytest.param({"data/local-path": None}, {**_OWN, "data/local-path": ("data/local-path", "9:2")}, {}, (),
                     ("Refuse local_path_data on another mount", "local_path_data ({root}/data/local-path) is outside {root}/data"
                      " (mounted at {root}/data/local-path)."), id="local-path-data-on-its-own-mount"),
        pytest.param({"data/local-path": None, "data/backup": None},
                     {**_OWN, "data/local-path": ("data", _DISK_ID), "data/backup": ("data/backup", "9:3")}, {}, (),
                     ("Refuse backup_path on another mount", "backup_path ({root}/data/backup) is outside {root}/data"
                      " (mounted at {root}/data/backup)."), id="backup-path-on-its-own-mount"),
    ],
)
def test_node_disk_refuses_a_data_path_it_cannot_use(
    tmp_path: Path, entries: dict[str, str | None], mounts: dict[str, tuple[str, str]], values: dict[str, object],
    flags: tuple[str, ...], refusal: tuple[str, str],
) -> None:
    """Rule: a data path that is missing or a file, not a mount, on the root filesystem, holding what is not Jarvis data, empty
    once k3s stored state, or with backup_path or local_path_data outside it is refused: nothing changes, no disk tool runs."""
    root = _host(tmp_path, entries)
    before = _tree(root)
    task, message = refusal
    run = _node_disk(tmp_path, root, mounts, *flags, **values)
    assert run.returncode != 0 and failed_task(run) == (task, message.format(root=root)), run.output
    assert (changed(run), _tree(root)) == ({"localhost": 0}, before), run.output
    assert not [word for word in _COMPLAINTS if word in run.output], run.output


@pytest.mark.parametrize(
    ("entries", "mounts", "values", "flags"),
    [
        pytest.param({}, {}, {"data_disk": None}, (), id="no-data-disk"),
        pytest.param({"data": None}, _OWN, {}, (), id="empty-mount"),
        pytest.param({"data": None}, _OWN, {}, ("--check",), id="empty-mount-check-mode"),
        pytest.param(_JARVIS, _JARVIS_MOUNTS, {}, (), id="jarvis-data"),
        pytest.param(_RERUN, _JARVIS_MOUNTS, {}, (), id="jarvis-data-after-k3s-ran"),
        # As jarvis site --check reruns a node: every read node-disk has runs in check mode.
        pytest.param(_RERUN, _JARVIS_MOUNTS, {}, ("--check",), id="rerun-in-check-mode"),
        pytest.param({"data/lost+found": None, "env": "env-test\n", "k3s-data/server": None}, _OWN, {"jarvis_marker_path": "env"},
                     (), id="marker-without-k3s-state"),
    ],
)
def test_node_disk_accepts_only_jarvis_data_and_changes_nothing(
    tmp_path: Path, entries: dict[str, str | None], mounts: dict[str, tuple[str, str]], values: dict[str, object],
    flags: tuple[str, ...],
) -> None:
    """Rule: without data_disk nothing is checked; a mount holding nothing or only Jarvis data passes, also once k3s
    stored state (the env marker is none), and nothing changes, in check mode too, with no disk tool run."""
    root = _host(tmp_path, entries)
    before = _tree(root)
    run = _node_disk(tmp_path, root, mounts, *flags, **values)
    assert run.returncode == 0, run.output
    assert (changed(run), _tree(root)) == ({"localhost": 0}, before), run.output
    assert not [word for word in _COMPLAINTS if word in run.output], run.output
