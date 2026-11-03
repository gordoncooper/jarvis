"""The secret-scan job checks out every commit."""

from __future__ import annotations

import copy

import yaml

from support import REPO_ROOT


def secret_scan_checkout_fetch_depth(workflow: dict) -> object:
    """fetch-depth on the checkout that feeds secret-scan. Missing means shallow."""
    job = workflow["jobs"]["fast"]
    checkout = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    with_block = checkout.get("with")
    if not isinstance(with_block, dict):
        return None
    return with_block.get("fetch-depth")


def test_secret_scan_checkout_is_full_history() -> None:
    """gitleaks reads every commit, so that job cannot be a shallow clone."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    assert secret_scan_checkout_fetch_depth(workflow) == 0


def test_shallow_secret_scan_checkout_fails_the_full_history_guard() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    assert secret_scan_checkout_fetch_depth(workflow) == 0
    shallow = copy.deepcopy(workflow)
    checkout = next(
        step
        for step in shallow["jobs"]["fast"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    checkout.pop("with", None)
    assert secret_scan_checkout_fetch_depth(shallow) != 0
