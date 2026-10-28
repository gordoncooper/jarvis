"""OS-disk selection for the autoinstall profiles. No hypervisor."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "node-install-storage"

ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
MSR = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
BASIC = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
RECOVERY = "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"
LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
SWAP = "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"
LVM = "E6D6D379-F507-44C2-A23C-238F2A3DF928"

_GIB = 1 << 30
_MIB = 1 << 20


def _load():
    loader = importlib.machinery.SourceFileLoader("node_install_storage", str(SCRIPT))
    spec = importlib.util.spec_from_loader("node_install_storage", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _part(number: int, type_guid: str, fstype: str | None) -> dict:
    return {"number": number, "type": type_guid, "fstype": fstype}


def _disk(disk_id: str, parts: list[dict], **extra: object) -> dict:
    disk = {
        "id": disk_id,
        "name": extra.pop("name", "disk"),
        "removable": False,
        "install_media": False,
        "partitions": parts,
    }
    disk.update(extra)
    return disk


def _windows(linux: list[dict] | None = None) -> list[dict]:
    parts = [
        _part(1, ESP, "vfat"),
        _part(2, MSR, None),
        _part(3, BASIC, "ntfs"),
        _part(4, RECOVERY, "ntfs"),
    ]
    parts.extend(linux or [_part(5, LINUX, "ext4")])
    return parts


def _survey(*disks: dict) -> dict:
    return {"disks": list(disks)}


def test_dualboot_keeps_windows_disk_and_deletes_linux_only() -> None:
    module = _load()
    data = _disk("data", [_part(1, LINUX, "ext4")], name="nvme0n1")
    os_disk = _disk("os", _windows(), name="nvme1n1")
    loop = {"id": "loop0", "type": "loop", "partitions": _windows()}
    plan = module.select(_survey(data, os_disk, loop), "dualboot")

    assert plan["disk_id"] == "os"
    assert plan["delete"] == [5]
    assert plan["untouched"] == ["data", "loop0"]
    try:
        module._refuse_if_linux_remains("dualboot", plan["delete"])
    except module.SelectionError as exc:
        assert exc.code == "linux-still-present"
    else:
        raise AssertionError("linux-still-present")
    module._refuse_if_linux_remains("dualboot", [])
    module._refuse_if_linux_remains("wipe", plan["delete"])


def test_kernel_name_swap_does_not_change_the_disk() -> None:
    module = _load()
    first = module.select(
        _survey(
            _disk("data", [_part(1, LINUX, "ext4")], name="nvme0n1"),
            _disk("os", _windows(), name="nvme1n1"),
        ),
        "dualboot",
    )
    swapped = module.select(
        _survey(
            _disk("os", _windows(), name="nvme0n1"),
            _disk("data", [_part(1, LINUX, "ext4")], name="nvme1n1"),
        ),
        "dualboot",
    )

    assert first["disk_id"] == swapped["disk_id"] == "os"
    assert first["delete"] == swapped["delete"] == [5]


def test_second_esp_without_ntfs_is_not_the_os_disk() -> None:
    module = _load()
    stale = _disk(
        "stale",
        [
            _part(1, MSR, None),
            _part(2, ESP, "vfat"),
            _part(3, LINUX, "ext4"),
        ],
        name="nvme1n1",
    )
    os_disk = _disk("os", _windows(), name="nvme0n1")
    plan = module.select(_survey(stale, os_disk), "dualboot")

    assert plan["disk_id"] == "os"
    assert plan["delete"] == [5]
    assert 2 not in plan["delete"]
    assert 3 not in plan["delete"]
    assert "stale" in plan["untouched"]


def test_extra_linux_partitions_are_deleted() -> None:
    module = _load()
    linux = [_part(5, LINUX, "ext4"), _part(6, LINUX, "ext4")]
    plan = module.select(
        _survey(
            _disk("data", [_part(1, LINUX, "ext4")]),
            _disk("os", _windows(linux)),
        ),
        "dualboot",
    )

    assert plan["disk_id"] == "os"
    assert plan["delete"] == [5, 6]


def test_swap_lvm_and_ext4_go_ntfs_stays() -> None:
    module = _load()
    linux = [
        _part(5, LINUX, "ext4"),
        _part(6, SWAP, "swap"),
        _part(7, LVM, "LVM2_member"),
        _part(8, LINUX, "ntfs"),
    ]
    plan = module.select(_survey(_disk("os", _windows(linux))), "dualboot")

    assert plan == {"disk_id": "os", "delete": [5, 6, 7], "untouched": []}


def test_wipe_takes_the_only_fixed_disk() -> None:
    module = _load()
    plan = module.select(
        _survey(
            _disk("usb", [_part(1, ESP, "vfat")], removable=True),
            _disk("media", [], install_media=True),
            _disk("os", [_part(1, LINUX, "ext4")]),
        ),
        "wipe",
    )

    assert plan["disk_id"] == "os"
    assert plan["delete"] == []
    assert plan["untouched"] == ["media", "usb"]


def test_refusals(tmp_path: Path) -> None:
    module = _load()
    cases = [
        (
            "wipe",
            _survey(
                _disk("os", [_part(1, LINUX, "ext4")]),
                _disk("data", [_part(1, LINUX, "ext4")]),
            ),
            "wipe-not-one",
        ),
        ("wipe", _survey(_disk("usb", [], removable=True)), "wipe-not-one"),
        ("dualboot", _survey(_disk("os", [_part(1, LINUX, "ext4")])), "dualboot-not-one"),
        ("dualboot", _survey(_disk("a", _windows()), _disk("b", _windows())), "dualboot-not-one"),
    ]
    for mode, survey, code in cases:
        try:
            module.select(survey, mode)
        except module.SelectionError as exc:
            assert exc.code == code
        else:
            raise AssertionError(code)

    path = tmp_path / "survey.json"
    path.write_text(json.dumps(cases[2][1]), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "dualboot", "--survey", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stderr.strip() == "dualboot-not-one"


def test_storage_config_names_only_the_chosen_disk() -> None:
    module = _load()
    os_disk = _disk("os", _windows())
    os_disk["path"] = "/dev/disk/by-path/pci-os"
    os_disk["size"] = 200 * _GIB
    os_disk["label"] = "gpt"
    cursor = _MIB
    for part, size in (
        (os_disk["partitions"][0], 100 * _MIB),
        (os_disk["partitions"][1], 16 * _MIB),
        (os_disk["partitions"][2], 20 * _GIB),
        (os_disk["partitions"][4], 100 * _GIB),
        (os_disk["partitions"][3], _GIB),
    ):
        part["start"] = cursor
        part["size"] = size
        cursor += size
    plan = module.select({"disks": [os_disk, _disk("other", [_part(1, LINUX, "ext4")])]}, "dualboot")
    storage = module.build_storage(os_disk, "dualboot", plan["delete"])
    encoded = json.dumps(storage)

    assert "other" not in encoded
    assert storage["swap"]["size"] == 0
    kept = [
        action["number"]
        for action in storage["config"]
        if str(action.get("id", "")).startswith("keep-")
    ]
    assert kept == [1, 2, 3]
    assert all(
        action["match"]["path"] == "/dev/disk/by-path/pci-os"
        for action in storage["config"]
        if action["type"] == "disk"
    )
    names = {action.get("name") for action in storage["config"]}
    assert {"vg-os", "lv-root", "lv-data"} <= names
    data_mount = next(action for action in storage["config"] if action.get("id") == "data-mount")
    assert data_mount["spec"] == "LABEL=jarvis-data"
    assert data_mount["options"] == "defaults,nofail"
    esp = next(action for action in storage["config"] if action.get("id") == "esp-fs")
    assert esp["preserve"] is True
    boot = next(action for action in storage["config"] if action.get("id") == "boot-part")
    pv = next(action for action in storage["config"] if action.get("id") == "pv-part")
    assert boot.get("preserve") is not True and pv.get("preserve") is not True
    assert boot["number"] == 5 and pv["number"] == 6
    assert 4 not in [
        action["number"] for action in storage["config"] if action["type"] == "partition"
    ]
    assert pv["flag"] == "lvm"
    data_fs = next(action for action in storage["config"] if action.get("id") == "data-fs")
    assert data_fs["label"] == "jarvis-data"
    root = next(action for action in storage["config"] if action.get("name") == "lv-root")
    assert root["size"] == 60 * _GIB


def test_wipe_lays_out_the_one_disk() -> None:
    module = _load()
    disk = {
        "id": "os",
        "path": "/dev/disk/by-path/pci-os",
        "size": 200 * _GIB,
        "partitions": [],
    }
    storage = module.build_storage(disk, "wipe", [])
    parts = [action for action in storage["config"] if action["type"] == "partition"]
    assert [part["size"] for part in parts[:2]] == [_GIB, 2 * _GIB]
    assert parts[0]["flag"] == "boot"
    assert all(not part.get("preserve") for part in parts)
    assert storage["config"][0]["wipe"] == "superblock-recursive"
    assert storage["config"][0]["preserve"] is False
    root = next(action for action in storage["config"] if action.get("name") == "lv-root")
    assert root["size"] == 60 * _GIB


def test_write_replaces_storage_only(tmp_path: Path) -> None:
    module = _load()
    source = (REPO_ROOT / "docs" / "node-install" / "autoinstall-dualboot.yaml").read_text(
        encoding="utf-8"
    )
    path = tmp_path / "autoinstall.yaml"
    path.write_text(source, encoding="utf-8")
    disk = {
        "id": "os",
        "path": "/dev/disk/by-path/pci-os",
        "size": 200 * _GIB,
        "partitions": [],
    }
    module._write(str(path), module.build_storage(disk, "wipe", []))
    text = path.read_text(encoding="utf-8")
    assert 'password: "!"' in text
    assert "REPLACE_WITH_SSH_PUBLIC_KEY" in text
    assert "unset-early-command-did-not-run" not in text
    assert "name: 'vg-os'" in text
    assert "spec: 'LABEL=jarvis-data'" in text
    assert "dualboot --apply" in text
