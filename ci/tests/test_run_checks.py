"""run-checks runs every listed check, reports each failure, and fails if any failed."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from support import REPO_ROOT


def _repo(tmp_path: Path, checks: dict[str, int]) -> Path:
    """A throwaway repo whose checks only record that they ran, then exit with a fixed code."""
    root = tmp_path / "repo"
    (root / "ci" / "checks").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "ci" / "run-checks", root / "ci" / "run-checks")
    (root / "ci" / "rule-checks.txt").write_text("\n".join(checks) + "\n", encoding="utf-8")
    for name, code in checks.items():
        check = root / "ci" / "checks" / name
        check.write_text(f"#!/usr/bin/env bash\necho ran-{name}\nexit {code}\n", encoding="utf-8")
        check.chmod(0o755)
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "ci" / "run-checks")],
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_failing_check_is_reported_and_later_checks_still_run(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"first": 1, "second": 0, "third": 1, "fourth": 0})
    proc = _run(root)
    assert proc.returncode != 0
    assert all(f"ran-{name}" in proc.stdout for name in ("first", "second", "third", "fourth"))
    assert "check failed: first" in proc.stderr
    assert "check failed: third" in proc.stderr
    assert "check failed: second" not in proc.stderr


def test_a_single_failing_check_fails_the_run(tmp_path: Path) -> None:
    proc = _run(_repo(tmp_path, {"first": 0, "last": 3}))
    assert proc.returncode != 0
    assert "check failed: last" in proc.stderr


def test_run_env_checks_runs_every_env_check_against_instance(tmp_path: Path) -> None:
    """One entry point runs all env-level checks on INSTANCE, names each failure, and fails."""
    names = ("env-schema", "addons", "flux-identity", "flux-prune", "flux-substitute", "manifest-lint", "secret-scan")
    root = _repo(tmp_path, {name: 1 if name == "flux-prune" else 0 for name in names})
    shutil.copy(REPO_ROOT / "ci" / "run-env-checks", root / "ci" / "run-env-checks")
    for name in names:
        check = root / "ci" / "checks" / name
        check.write_text(
            f'#!/usr/bin/env bash\necho "ran-{name} on $INSTANCE"\n'
            f"exit {1 if name == 'flux-prune' else 0}\n",
            encoding="utf-8",
        )
    proc = subprocess.run(
        ["bash", str(root / "ci" / "run-env-checks")],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "INSTANCE": "/instance-fixture"},
    )
    assert proc.returncode != 0
    assert all(f"ran-{name} on /instance-fixture" in proc.stdout for name in names)
    assert "FAIL flux-prune" in proc.stderr
    assert "FAIL env-schema" not in proc.stderr


def test_all_passing_checks_pass_the_run(tmp_path: Path) -> None:
    proc = _run(_repo(tmp_path, {"first": 0, "second": 0}))
    assert proc.returncode == 0, proc.stderr
