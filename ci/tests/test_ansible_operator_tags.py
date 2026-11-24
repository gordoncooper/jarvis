"""Narrow play tags: each lists the always-tagged checks and its own tasks, and never reaches the k3s install."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import ANSIBLE_DIR, REPO_ROOT, playbook, site_vars_file

LIST_TASKS_INVENTORY = REPO_ROOT / "ci/tests/fixtures/ansible-list-tasks/hosts.yml"


def _names(tasks: list[dict], role: str = "") -> list[str]:
    """The tasks' names, blocks opened, as --list-tasks lists them: a role's as "<role> : <name>"."""
    return [(f"{role} : " if role else "") + task["name"] for entry in tasks for task in entry.get("block", [entry])]


def _load(path: str) -> list[dict]:
    """A file under infra/ansible, by its path there: a whole repo path would narrow select-tests to this file."""
    return yaml.safe_load((ANSIBLE_DIR / path).read_text(encoding="utf-8"))


# Tagged always, so every run lists them: the opening play's checks, and the flux-operator play's check-mode notice.
[FLUX_OPERATOR_PLAY] = [play for play in _load("site.yml") if "flux-operator" in play.get("tags", [])]
ALWAYS = [
    *_names(_load("tasks/preflight.yml")),
    *_names(_load("tasks/assert-gpu.yml")),
    *_names(_load("roles/k3s/tasks/refuse.yml"), "k3s"),
    *_names(_load("roles/node-disk/tasks/main.yml"), "node-disk"),
    *_names(FLUX_OPERATOR_PLAY["pre_tasks"]),
]
# The k3s install downloads k3s and decrypts the sealed tokens with the age private key. A task anywhere that does one
# of these says so in its name, as the install's own tasks do.
NEVER = ("Download pinned k3s", "sealed k3s tokens", "age private key")
K3S_INSTALL = [
    task["name"]
    for path in ("roles/k3s/tasks/main.yml", "roles/k3s/tasks/token.yml")
    for task in _load(path)
    if "name" in task
]


@pytest.mark.parametrize(
    ("tag", "own", "purpose"),
    [
        pytest.param("kubeconfig", _names(_load("roles/kubeconfig/tasks/main.yml"), "kubeconfig"),
                     "kubeconfig : Write the per-env kubeconfig", id="kubeconfig"),
        pytest.param("labels", _names(_load("roles/k3s/tasks/labels.yml"), "k3s"),
                     "k3s : Reconcile role and enablelb labels", id="labels"),
    ],
)
def test_narrow_tag_lists_only_its_own_tasks(tmp_path: Path, tag: str, own: list[str], purpose: str) -> None:
    """Rule: a narrow tag lists the always-tagged tasks and, beside them, only its own, the one it is for among them;
    wherever a task lands, none it lists downloads k3s, reads the sealed k3s tokens or needs the age private key."""
    vars_file = site_vars_file(tmp_path / "vars.json")
    run = playbook("site.yml", "-i", LIST_TASKS_INVENTORY, "-e", f"@{vars_file}", "--list-tasks", "--tags", tag)
    assert run.returncode == 0, run.output
    # --list-tasks has no JSON form: a task is a line indented six spaces, then its tags.
    listed = re.findall(r"^ {6}(.+)\tTAGS: \[(.*)\]$", run.stdout, re.M)
    added = [name for name, tags in listed if "always" not in tags.split(", ")]
    assert [name for name, tags in listed if "always" in tags.split(", ")] == ALWAYS, run.stdout
    assert added == own and purpose in added, run.stdout
    assert all(any(words in name for name in K3S_INSTALL) for words in NEVER), K3S_INSTALL
    assert not [words for words in NEVER if words in run.stdout], run.stdout
