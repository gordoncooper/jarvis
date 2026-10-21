"""Facts for the wipe checks, from one lsblk --json tree."""

from __future__ import annotations

import re
from typing import Any

_WHOLE_TYPES = frozenset({"disk", "loop"})


def _walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for node in nodes:
        found.append(node)
        children = node.get("children") or []
        if isinstance(children, list):
            found.extend(_walk(children))
    return found


def _mountpoints(node: dict[str, Any]) -> list[str] | None:
    """None when this node did not report mounts. Empty when it reported none."""
    if "mountpoints" not in node:
        return None
    raw = node.get("mountpoints")
    if isinstance(raw, str):
        return [raw] if raw else []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return bool(value)


def _kernel(stat: dict[str, Any], disk_id: str) -> str:
    if not _flag(stat.get("exists")):
        return ""
    if _flag(stat.get("islnk")):
        return _basename(str(stat.get("lnk_source") or ""))
    return _basename(str(disk_id))


def _matches(node: dict[str, Any], kernel: str) -> bool:
    if not kernel:
        return False
    names = {
        str(node.get("kname") or ""),
        str(node.get("name") or ""),
        _basename(str(node.get("path") or "")),
    }
    return kernel in names


def _part_number(node: dict[str, Any]) -> int | None:
    raw = node.get("partn")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.isdecimal() and int(raw) > 0:
        return int(raw)
    # Loop partitions often leave partn empty. The kernel name ends with the index.
    name = str(node.get("kname") or node.get("name") or "")
    match = re.search(r"(\d+)$", name)
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def _partition_path(kernel: str) -> str:
    if not kernel:
        return ""
    suffix = "p1" if kernel[-1].isdigit() else "1"
    return f"/dev/{kernel}{suffix}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _devices(tree: Any) -> list[dict[str, Any]]:
    if not isinstance(tree, dict):
        return []
    devices = tree.get("blockdevices")
    if not isinstance(devices, list):
        return []
    return [node for node in devices if isinstance(node, dict)]


def node_disk_facts(
    tree: Any,
    stat: Any,
    disk_id: str,
    mount: str,
    label: str,
    mount_stat: Any,
    mount_entries: Any,
) -> dict[str, Any]:
    """Describe the by-id disk using the lsblk tree and the mount-point stat."""
    nodes = _walk(_devices(tree))
    # A complete mountpoints column tells us whether / is on a block device.
    # Overlay root has no block mount, so it is seen and is not this disk.
    saw_root = bool(nodes) and all(_mountpoints(node) is not None for node in nodes)
    mounted = [(node, _mountpoints(node) or []) for node in nodes]
    disk_stat = stat if isinstance(stat, dict) else {}
    kernel = _kernel(disk_stat, str(disk_id))
    matched = next((node for node in nodes if _matches(node, kernel)), None)
    ours_nodes = _walk([matched]) if matched is not None else []
    ours_ids = {id(node) for node in ours_nodes}

    our_mounts: list[str] = []
    for node, points in mounted:
        if id(node) not in ours_ids:
            continue
        for point in points:
            if point not in our_mounts:
                our_mounts.append(point)

    root_device = ""
    for node, points in mounted:
        if "/" not in points:
            continue
        root_device = _text(node.get("path")) or (
            "/dev/" + _text(node.get("kname") or node.get("name"))
        )
        break

    parts = [node for node in ours_nodes if node.get("type") == "part"]
    on_self = any(node.get("label") == label for node in ours_nodes)
    elsewhere = any(
        node.get("label") == label and id(node) not in ours_ids for node in nodes
    )
    mount_hits = [node for node, points in mounted if mount in points]
    mounted_here = mount in our_mounts
    point = mount_stat if isinstance(mount_stat, dict) else {}
    try:
        entries = int(mount_entries or 0)
    except (TypeError, ValueError):
        entries = 0
    is_mount = len(mount_hits) > 0
    exists = _flag(point.get("exists"))
    isdir = _flag(point.get("isdir"))
    device_type = matched.get("type") if matched else ""
    return {
        "exists": _flag(disk_stat.get("exists")),
        "kernel": kernel,
        "is_whole": device_type in _WHOLE_TYPES,
        "is_partition": device_type == "part",
        "model": _text(matched.get("model")) if matched else "",
        "size": _text(matched.get("size")) if matched else "",
        "partitions": [_text(node.get("name")) for node in parts],
        "partition_numbers": [
            number for node in parts if (number := _part_number(node)) is not None
        ],
        "partition_path": _partition_path(kernel),
        "saw_root": saw_root,
        "is_root": "/" in our_mounts,
        "root_mount_device": root_device,
        "mounted_elsewhere": [item for item in our_mounts if item != mount],
        "mounted_here": mounted_here,
        "label_elsewhere": elsewhere,
        "mountpoint_foreign": is_mount and not mounted_here,
        "mountpoint_nonempty": exists and ((not isdir) or ((not is_mount) and entries > 0)),
        "ours": on_self and not elsewhere,
    }


def node_disk_mount_matches(tree: Any, mount: str, label: str) -> bool:
    """True when exactly one device is mounted there and carries the label."""
    hits = [
        node
        for node in _walk(_devices(tree))
        if mount in (_mountpoints(node) or [])
    ]
    return len(hits) == 1 and hits[0].get("label") == label


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "node_disk_facts": node_disk_facts,
            "node_disk_mount_matches": node_disk_mount_matches,
        }
