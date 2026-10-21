"""Kubeconfig fetch keeps the group_vars node path."""

from __future__ import annotations

import yaml

from ansible_role_proof import ANSIBLE_DIR

_TASKS = ANSIBLE_DIR / "roles" / "kubeconfig" / "tasks" / "main.yml"


def test_kubeconfig_fetch_src_is_the_node_path() -> None:
    doc = yaml.safe_load(_TASKS.read_text(encoding="utf-8"))
    tasks = [
        task
        for entry in doc
        for section in ("block", "always")
        for task in (entry.get(section) or ())
    ]
    fetch = next(task for task in tasks if "ansible.builtin.fetch" in task)
    assert fetch["ansible.builtin.fetch"]["src"] == "{{ k3s_kubeconfig_path }}"
    var_blocks = [entry.get("vars") or {} for entry in doc]
    var_blocks += [task.get("vars") or {} for task in tasks]
    assert all("k3s_kubeconfig_path" not in block for block in var_blocks)
