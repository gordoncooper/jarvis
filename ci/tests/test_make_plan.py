"""tools/make-plan prints a recipe and does not run it."""

from __future__ import annotations

import re
import subprocess
import sys

from support import REPO_ROOT

SCRIPT = REPO_ROOT / "tools" / "make-plan"
_MAKEFILE = REPO_ROOT / "Makefile"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_make_plan_prints_the_restore_recipe():
    proc = _run("restore", "ENV=env-00", "APP=demo")
    assert proc.returncode == 0
    assert "tools/restore" in proc.stdout and "--app" in proc.stdout


def test_make_plan_ignores_inherited_variables(monkeypatch):
    """An inherited VENV does not shape the dry-run expansion."""
    monkeypatch.setenv("VENV", "/inherited-venv")
    proc = _run("verify", "ENV=env-00")
    assert proc.returncode == 0
    assert "/inherited-venv" not in proc.stdout


def test_make_plan_rejects_a_target_that_is_not_a_name():
    proc = _run("restore;id")
    assert proc.returncode == 2 and "usage:" in proc.stderr


def test_make_plan_forwards_only_documented_names():
    """Make control variables and values make would expand are refused."""
    assert _run("help", "TAGS=base-os").returncode == 0
    for arg in ("MAKE=id", "MAKEFLAGS=-n", "ENV=$(shell id)", "ENV=`id`"):
        proc = _run("restore", arg)
        assert proc.returncode == 2, arg
        assert "usage:" in proc.stderr


def test_makefile_dry_run_has_no_parse_time_execution():
    """A shell call, forced recipe, sub-make, or makefile remake would run under make -n."""
    lines = [
        line
        for line in _MAKEFILE.read_text(encoding="utf-8").splitlines()
        if line.lstrip()[:1] != "#"
    ]
    body = "\n".join(lines)
    assert "$(shell" not in body and "`" not in body
    assert not re.search(r"(?m)^[A-Za-z0-9_.-]+.*!=", body)
    assert not any(line.startswith("\t+") for line in lines)
    assert "$(MAKE)" not in body and "${MAKE}" not in body
    assert not re.search(r"(?m)^ *(include|-include|sinclude)\b", body)
    assert not re.search(r"(?m)^ *(makefile|Makefile|GNUmakefile)\s*:", body)
