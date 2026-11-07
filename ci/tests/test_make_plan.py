"""tools/make-plan prints a recipe and does not run it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

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


def test_instance_points_make_plan_and_a_tool_at_an_instance_checkout(tmp_path: Path) -> None:
    """INSTANCE resolves envs/<env> in an instance checkout; unset resolves this checkout."""
    instance = tmp_path / "instance"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", instance / "envs" / "env-00")
    values = instance / "envs" / "env-00" / "values" / "cluster-vars.yaml"
    values.write_text(
        values.read_text(encoding="utf-8").replace("env-00.example", "instance.example"),
        encoding="utf-8",
    )
    plan = _run("site", "ENV=env-00", f"INSTANCE={instance}")
    assert plan.returncode == 0, plan.stderr
    assert f"-i {instance}/envs/env-00/inventory/hosts.yml" in plan.stdout
    assert _run("site", "ENV=env-00").stdout.count(f"{REPO_ROOT}/envs/env-00/inventory/hosts.yml") == 1

    def render(**extra: str) -> str:
        env = {k: v for k, v in os.environ.items() if k != "INSTANCE"} | extra
        cmd = [sys.executable, "tools/lib/render_cluster_vars_configmap.py", "--env", "env-00"]
        return subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True).stdout

    assert "dns_suffix: instance.example" in render(INSTANCE=str(instance))
    assert "dns_suffix: env-00.example" in render()


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


def test_make_plan_forwards_the_documented_namespace_and_venv():
    """NAMESPACE and VENV are in the Makefile help, so a plan can show them."""
    proc = _run("seal", "ENV=env-00", "NAME=demo", "NAMESPACE=hello", "VENV=/demo-venv")
    assert proc.returncode == 0, proc.stderr
    assert '--namespace "hello"' in proc.stdout and "/demo-venv/bin/python" in proc.stdout


def test_make_plan_refuses_instance_from_the_environment(monkeypatch):
    """A plan never silently ignores INSTANCE and shows this checkout instead."""
    monkeypatch.setenv("INSTANCE", "/demo-instance")
    proc = _run("site", "ENV=env-00")
    assert proc.returncode == 2 and "as an argument" in proc.stderr
    assert "/other-instance" in _run("site", "ENV=env-00", "INSTANCE=/other-instance").stdout


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
