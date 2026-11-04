"""tools/make-plan prints a recipe and does not run it."""

from __future__ import annotations

import subprocess
import sys

from support import REPO_ROOT

SCRIPT = REPO_ROOT / "tools" / "make-plan"


def test_make_plan_prints_the_restore_recipe():
    proc = subprocess.run(
        [sys.executable, SCRIPT, "restore", "ENV=env-00", "APP=demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "tools/restore" in proc.stdout and "--app" in proc.stdout


def test_make_plan_rejects_a_target_that_is_not_a_name():
    proc = subprocess.run(
        [sys.executable, SCRIPT, "restore;id"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2 and "usage:" in proc.stderr
