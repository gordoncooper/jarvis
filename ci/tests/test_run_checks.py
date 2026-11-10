"""run-checks runs every listed check, reports each failure, and fails if any failed."""

from __future__ import annotations

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


def test_validate_runs_every_env_check_and_names_each_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    """jarvis validate runs every env-level check on the instance, names each failure, and fails."""
    from jarvis.env import validate

    ran: list[tuple[str, Path]] = []

    def check(name: str):
        def run(instance: Path) -> list[str]:
            ran.append((name, instance))
            return ["it broke"] if name == "flux-prune" else []

        return run

    monkeypatch.setattr(validate, "CHECKS", {name: check(name) for name in validate.CHECKS})
    assert validate.run(tmp_path) == 1
    assert ran == [(name, tmp_path) for name in validate.CHECKS]
    out = capsys.readouterr().out
    assert "FAIL flux-prune\n  it broke" in out
    assert "PASS env-schema" in out


def test_core_runs_every_env_check_on_a_sample_instance() -> None:
    """Rule: core's CI runs jarvis validate's checks (all of them) on a sample instance, and the
    ones that also read core's own trees on core itself, under the same names."""
    from jarvis.env import validate

    listed = (REPO_ROOT / "ci" / "rule-checks.txt").read_text(encoding="utf-8").split()
    assert "sample-instance" in listed
    assert "python3 ci/tests/sample.py --validate " in (REPO_ROOT / "ci" / "checks" / "sample-instance").read_text(encoding="utf-8")
    for name in ("addons", "manifest-lint", "secret-scan"):
        assert name in validate.CHECKS and name in listed
        script = (REPO_ROOT / "ci" / "checks" / name).read_text(encoding="utf-8")
        assert f"python3 -m jarvis.env.validate --only {name}" in script, name


def test_all_passing_checks_pass_the_run(tmp_path: Path) -> None:
    proc = _run(_repo(tmp_path, {"first": 0, "second": 0}))
    assert proc.returncode == 0, proc.stderr
