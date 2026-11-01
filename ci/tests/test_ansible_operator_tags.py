"""Play tags: --tags kubeconfig runs only the kubeconfig role."""

from __future__ import annotations

import subprocess

from ansible_role_proof import ANSIBLE_DIR, REPO_ROOT, env_name

LIST_TASKS_INVENTORY = REPO_ROOT / "ci/tests/fixtures/ansible-list-tasks/hosts.yml"
CLUSTER_VARS_REL = f"../../envs/{env_name()}/values/cluster-vars.yaml"


def list_tasks_for_tag(tag: str) -> str:
    run = subprocess.run(
        [
            "ansible-playbook",
            "site.yml",
            "-i",
            str(LIST_TASKS_INVENTORY),
            "-e",
            f"jarvis_cluster_vars={CLUSTER_VARS_REL}",
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


def test_kubeconfig_tag_runs_only_the_kubeconfig_role() -> None:
    listed = list_tasks_for_tag("kubeconfig")
    task_lines = [line for line in listed.splitlines() if " : " in line]
    assert task_lines, listed
    roles = {line.split(" : ", 1)[0].strip().split()[-1] for line in task_lines}
    assert "kubeconfig" in roles
    assert roles <= {"kubeconfig", "k3s", "node-disk"}, listed
    assert all("Download pinned k3s" not in line for line in task_lines), listed
    assert "base-os :" not in listed
    assert "flux-operator :" not in listed
