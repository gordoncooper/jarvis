"""The node memory reserve as the kubelet's memory.available eviction threshold.

The kubelet subtracts its hard eviction thresholds from allocatable, so pods cannot be
scheduled into them, and once free memory falls below them it evicts pods whose usage
exceeds their requests first, by priority among those, before the kernel has to act. The reserve is node_memory_reserve_percent of the node's
memory, rendered as a percent (the kubelet applies it to the node's own capacity); the
env's or host's eviction-hard keeps its other signals, and its memory.available wins when
it is larger. A percent of 0 leaves eviction-hard as written.
"""

from __future__ import annotations

import re

from ansible.errors import AnsibleFilterError

_SIGNAL = "memory.available"
# Kubernetes quantity suffixes (no exponent form); preflight.yml holds the same pattern.
_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|)$")
_FACTOR = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
           "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}


def _percent(value: object) -> int:
    text = str(value).strip()
    if not text.isdigit() or not 0 <= int(text) <= 50:
        raise AnsibleFilterError(f"node_memory_reserve_percent is a whole number from 0 to 50, not {value!r}")
    return int(text)


def _bytes(threshold: str, memtotal_mb: int) -> float:
    if threshold.endswith("%"):
        try:
            return float(threshold[:-1]) / 100 * memtotal_mb * 2**20
        except ValueError:
            raise AnsibleFilterError(f"eviction-hard {_SIGNAL}<{threshold} is not a percent") from None
    match = _QUANTITY.fullmatch(threshold)
    if match is None:
        raise AnsibleFilterError(f"eviction-hard {_SIGNAL}<{threshold} is not a quantity or a percent")
    return float(match.group(1)) * _FACTOR[match.group(2)]


def with_memory_reserve(eviction_hard: object, memtotal_mb: object, percent: object) -> str:
    """eviction-hard with memory.available at least percent of the node's memory."""
    entries = [entry.strip() for entry in str(eviction_hard).split(",") if entry.strip()]
    reserve = _percent(percent)
    if reserve == 0:
        return ",".join(entries)
    try:
        total = int(memtotal_mb)
    except (TypeError, ValueError):
        raise AnsibleFilterError(f"the node's memory (k3s_node_memtotal_mb, from ansible_facts['memtotal_mb']) is {memtotal_mb!r}") from None
    wanted = f"{_SIGNAL}<{reserve}%"
    for index, entry in enumerate(entries):
        signal, sep, threshold = entry.partition("<")
        if signal.strip() != _SIGNAL:
            continue
        if not sep:
            raise AnsibleFilterError(f"eviction-hard entry {entry!r} is not <signal><<threshold>")
        if _bytes(threshold.strip(), total) < reserve / 100 * total * 2**20:
            entries[index] = wanted
        return ",".join(entries)
    return ",".join([*entries, wanted])


class FilterModule:
    def filters(self) -> dict:
        return {"with_memory_reserve": with_memory_reserve}
