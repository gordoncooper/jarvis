"""Operator Ansible tags: guard and required-var asserts appear on every tag."""

from __future__ import annotations

import subprocess

import pytest

from ansible_role_proof import ANSIBLE_DIR, REPO_ROOT, env_name

LIST_TASKS_INVENTORY = (
    REPO_ROOT / "tools/tests/fixtures/ansible-list-tasks/hosts.yml"
)
CLUSTER_VARS_REL = f"../../envs/{env_name()}/values/cluster-vars.yaml"
NODES_VARS_REL = f"../../envs/{env_name()}/values/nodes.yaml"

OPERATOR_TAGS = (
    "base-os",
    "node-disk",
    "kubeconfig",
    "k3s-upgrade",
    "k3s-config",
    "time",
    "apt-policy",
    "journald",
)

GUARD_SNIPPETS = (
    "Refuse host with foreign or unmarked k3s",
    "Stat jarvis env marker",
)

REQUIRED_VAR_SNIPPETS = (
    "Require time_zone from cluster-vars",
    "Require k3s cluster-vars",
    "Require flux-operator cluster-vars",
)


def list_tasks_for_tag(tag: str) -> str:
    run = subprocess.run(
        [
            "ansible-playbook",
            "site.yml",
            "-i",
            str(LIST_TASKS_INVENTORY),
            "-e",
            f"jarvis_cluster_vars={CLUSTER_VARS_REL}",
            "-e",
            f"jarvis_nodes_vars={NODES_VARS_REL}",
            "--list-tasks",
            "--tags",
            tag,
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    return run.stdout


@pytest.mark.parametrize("tag", OPERATOR_TAGS)
def test_operator_tag_includes_guard_and_required_asserts(tag: str) -> None:
    listed = list_tasks_for_tag(tag)
    for snippet in GUARD_SNIPPETS:
        assert snippet in listed, f"tag={tag} missing guard snippet: {snippet}"
    for snippet in REQUIRED_VAR_SNIPPETS:
        assert snippet in listed, f"tag={tag} missing required-var snippet: {snippet}"


def test_k3s_upgrade_excludes_untagged_base_os_and_flux_operator_work() -> None:
    listed = list_tasks_for_tag("k3s-upgrade")
    for line in listed.splitlines():
        if " base-os : " in line and "Require time_zone" not in line:
            pytest.fail(f"unexpected base-os task on k3s-upgrade: {line.strip()}")
        if " flux-operator : " in line and "Require flux-operator cluster-vars" not in line:
            pytest.fail(f"unexpected flux-operator task on k3s-upgrade: {line.strip()}")
