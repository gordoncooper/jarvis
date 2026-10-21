"""Kubeconfig fetch reads the node path, not the runner path."""

from __future__ import annotations

import json
import os
import subprocess

import yaml

from ansible_role_proof import ANSIBLE_DIR
from conftest import require_cli

_ROLE_TASKS = ANSIBLE_DIR / "roles" / "kubeconfig" / "tasks" / "main.yml"
_GROUP_VARS = ANSIBLE_DIR / "group_vars" / "all.yml"


def _fetch_src(tasks: object) -> tuple[dict[str, object], str]:
    assert isinstance(tasks, list)
    for task in tasks:
        if not isinstance(task, dict) or "block" not in task:
            continue
        block = task["block"]
        assert isinstance(block, list)
        for inner in block:
            if not isinstance(inner, dict):
                continue
            fetch = inner.get("ansible.builtin.fetch")
            if isinstance(fetch, dict) and isinstance(fetch.get("src"), str):
                raw_vars = task.get("vars") or {}
                assert isinstance(raw_vars, dict)
                return raw_vars, fetch["src"]
    raise AssertionError("kubeconfig fetch task not found")


def test_kubeconfig_fetch_src_is_the_node_path() -> None:
    require_cli("ansible")
    block_vars, src = _fetch_src(yaml.safe_load(_ROLE_TASKS.read_text(encoding="utf-8")))
    group_vars = yaml.safe_load(_GROUP_VARS.read_text(encoding="utf-8"))
    assert isinstance(group_vars, dict)
    node_path = group_vars["k3s_kubeconfig_path"]
    assert isinstance(node_path, str) and node_path.startswith("/")
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    env["ANSIBLE_BECOME"] = "false"
    run = subprocess.run(
        [
            "ansible",
            "localhost",
            "-c",
            "local",
            "-e",
            json.dumps(group_vars),
            "-e",
            json.dumps(block_vars),
            "-m",
            "ansible.builtin.debug",
            "-a",
            f"msg={src}",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    text = run.stdout + run.stderr
    assert run.returncode == 0, text
    assert node_path in text, text
    assert f"{os.environ['HOME']}/.kube/config" not in text, text
