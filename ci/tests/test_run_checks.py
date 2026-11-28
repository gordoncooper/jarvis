"""run-checks runs every listed check, reports each failure, and fails if any failed."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from support import REPO_ROOT


@pytest.mark.parametrize(
    ("checks", "failed"),
    [
        pytest.param({"first": 1, "second": 0, "third": 1, "fourth": 0}, ["first", "third"], id="two-fail"),
        pytest.param({"first": 0, "last": 3}, ["last"], id="last-fails"),
        pytest.param({"first": 0, "second": 0}, [], id="all-pass"),
    ],
)
def test_every_check_runs_and_any_failure_fails_the_run(tmp_path: Path, checks: dict[str, int], failed: list[str]) -> None:
    """Rule: run-checks runs every listed check, also after one fails, names each that failed, and then fails (#156)."""
    root = tmp_path / "repo"
    (root / "ci" / "checks").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "ci" / "run-checks", root / "ci" / "run-checks")
    (root / "ci" / "rule-checks.txt").write_text("\n".join(checks) + "\n", encoding="utf-8")
    for name, code in checks.items():
        check = root / "ci" / "checks" / name
        check.write_text(f"#!/usr/bin/env bash\necho ran-{name}\nexit {code}\n", encoding="utf-8")
        check.chmod(0o755)
    # Outside GitHub Actions run-checks prints no ::group:: lines around each check.
    proc = subprocess.run(["bash", str(root / "ci" / "run-checks")], capture_output=True, text=True, check=False,
                          env={**os.environ, "GITHUB_ACTIONS": ""})  # fmt: skip
    summary = [f"failing checks ({len(failed)}): {' '.join(failed)}"] if failed else []
    assert (proc.returncode, proc.stdout, proc.stderr.splitlines()) == (
        1 if failed else 0, "".join(f"ran-{name}\n" for name in checks), [*(f"check failed: {name}" for name in failed), *summary])


def test_validate_runs_every_env_check_and_names_each_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    """Rule: jarvis validate runs every env-level check on the instance, in order, names each failure, and fails."""
    from jarvis.env import validate

    ran: list[tuple[str, Path]] = []

    def check(name: str):
        def run(instance: Path) -> list[str]:
            ran.append((name, instance))
            return ["it broke"] if name == "flux-identity" else []

        return run

    monkeypatch.setattr(validate, "CHECKS", {name: check(name) for name in validate.CHECKS})
    assert validate.run(tmp_path) == 1
    assert ran == [(name, tmp_path) for name in validate.CHECKS]
    assert capsys.readouterr().out == "".join(
        "FAIL flux-identity\n  it broke\n" if name == "flux-identity" else f"PASS {name}\n" for name in validate.CHECKS)
