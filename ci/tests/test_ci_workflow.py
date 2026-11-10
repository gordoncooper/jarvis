"""The CI job that runs ci/checks keeps the history secret-scan reads."""

from __future__ import annotations

import copy

import yaml
from ansible_role_proof import REPO_ROOT


def _shallow_checkouts(workflow: dict) -> list[str]:
    """Jobs that run ci/run-checks but check out without fetch-depth 0."""
    shallow = []
    for name, job in workflow["jobs"].items():
        steps = job.get("steps") or []
        if not any("ci/run-checks" in str(step.get("run", "")) for step in steps):
            continue
        checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]
        if not checkouts or any((c.get("with") or {}).get("fetch-depth") != 0 for c in checkouts):
            shallow.append(name)
    return shallow


def test_checks_job_fetches_full_history() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert _shallow_checkouts(workflow) == []
    shallow = copy.deepcopy(workflow)
    del shallow["jobs"]["suite"]["steps"][0]["with"]["fetch-depth"]
    assert _shallow_checkouts(shallow) == ["suite"]


def test_the_required_ci_check_waits_on_every_suite_group() -> None:
    """Rule: branch protection requires the check named ci. It is the gate job: it needs the suite
    matrix, runs even when a group fails, and passes only when the suite succeeded."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    gate = workflow["jobs"]["ci"]
    assert gate["name"] == "ci" and gate["needs"] == "suite" and gate["if"] == "always()"
    assert "needs.suite.result" in str(gate["steps"])
    assert workflow["jobs"]["suite"]["strategy"]["matrix"]["group"] == ["checks", "roles"]
    assert not any(job.get("name") == "ci" for key, job in workflow["jobs"].items() if key != "ci")
