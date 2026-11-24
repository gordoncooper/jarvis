"""Play tags: --tags kubeconfig runs only the kubeconfig role."""

from __future__ import annotations

from pathlib import Path

from ansible_role_proof import REPO_ROOT, playbook, site_vars_file

LIST_TASKS_INVENTORY = REPO_ROOT / "ci/tests/fixtures/ansible-list-tasks/hosts.yml"


def list_tasks_for_tag(tmp_path: Path, tag: str) -> str:
    run = playbook("site.yml", "-i", LIST_TASKS_INVENTORY, "-e", f"@{site_vars_file(tmp_path / 'vars.json')}", "--list-tasks", "--tags", tag)
    assert run.returncode == 0, run.output
    return run.stdout


def test_kubeconfig_tag_runs_only_the_kubeconfig_role(tmp_path: Path) -> None:
    listed = list_tasks_for_tag(tmp_path, "kubeconfig")
    task_lines = [line for line in listed.splitlines() if " : " in line]
    assert task_lines, listed
    roles = {line.split(" : ", 1)[0].strip().split()[-1] for line in task_lines}
    assert "kubeconfig" in roles
    assert roles <= {"kubeconfig", "k3s", "node-disk"}, listed
    assert all("Download pinned k3s" not in line for line in task_lines), listed
    assert "base-os :" not in listed
    assert "flux-operator :" not in listed


def test_labels_tag_reads_no_sealed_token(tmp_path: Path) -> None:
    """A label-only run never reaches the task that decrypts the k3s tokens, so it needs no age key."""
    listed = list_tasks_for_tag(tmp_path, "labels")
    assert "Reconcile role and enablelb labels" in listed, listed
    assert "sealed k3s tokens" not in listed and "age private key" not in listed, listed
