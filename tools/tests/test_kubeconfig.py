"""Kubeconfig fetch keeps the group_vars path and rewrites the server URL."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

from ansible_role_proof import ANSIBLE_DIR
from conftest import require_cli

_TASKS = ANSIBLE_DIR / "roles" / "kubeconfig" / "tasks" / "main.yml"


def _task_entries() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    doc = yaml.safe_load(_TASKS.read_text(encoding="utf-8"))
    tasks = [
        task
        for entry in doc
        for section in ("block", "always")
        for task in (entry.get(section) or ())
    ]
    return doc, tasks


def test_kubeconfig_role_does_not_override_group_vars_path() -> None:
    doc, tasks = _task_entries()
    fetch = next(task for task in tasks if "ansible.builtin.fetch" in task)
    assert fetch["ansible.builtin.fetch"]["src"] == "{{ k3s_kubeconfig_path }}"
    var_blocks = [entry.get("vars") or {} for entry in doc]
    var_blocks += [task.get("vars") or {} for task in tasks]
    assert all("k3s_kubeconfig_path" not in block for block in var_blocks)


def test_kubeconfig_server_rewrites_to_inventory_hostname(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    _, tasks = _task_entries()
    replace = next(task for task in tasks if "ansible.builtin.replace" in task)
    spec = replace["ansible.builtin.replace"]
    hostname = "node.example"
    kubeconfig = tmp_path / "k3s.yaml"
    kubeconfig.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- cluster:\n"
        "    server: https://127.0.0.1:6443\n"
        "  name: default\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        f"all:\n  hosts:\n    {hostname}:\n      ansible_connection: local\n",
        encoding="utf-8",
    )
    play = tmp_path / "play.yml"
    play.write_text(
        "---\n"
        "- name: Rewrite kubeconfig server\n"
        f"  hosts: {hostname}\n"
        "  gather_facts: false\n"
        "  become: false\n"
        "  tasks:\n"
        "    - ansible.builtin.replace:\n"
        f"        path: {json.dumps(str(kubeconfig))}\n"
        f"        regexp: {json.dumps(spec['regexp'])}\n"
        f"        replace: {json.dumps(spec['replace'])}\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    proc = subprocess.run(
        ["ansible-playbook", str(play), "-i", str(inventory)],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = kubeconfig.read_text(encoding="utf-8")
    assert text.count(f"https://{hostname}:6443") == 1
    assert "127.0.0.1" not in text
