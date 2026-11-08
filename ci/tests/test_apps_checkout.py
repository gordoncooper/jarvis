"""An instance's apps checkout: when env checks need it, which repository the helper reads, and that
the checks read each app at its pinned tag."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT, instance_apps_repo_for_checks, render_switches, set_switches

_GIT = ["git", "-c", "user.email=fixture@example.com", "-c", "user.name=fixture"]


def _instance(tmp_path: Path, *, core_only: bool) -> Path:
    """Instance checkout with env-00; core-only drops its apps but keeps a backups section."""
    instance = tmp_path / "instance"
    env_dir = instance / "envs" / "env-00"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", env_dir)
    (instance / "core").symlink_to(REPO_ROOT)
    if core_only:
        (env_dir / "values" / "apps.yaml").write_text("backups:\n  mode: selected\n", encoding="utf-8")
        set_switches(env_dir, apps=[])
        render_switches(env_dir, instance)
    return instance


def _env_schema(instance: Path, apps_repo: Path | None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "APPS_REPO"}
    env["INSTANCE"] = str(instance)
    if apps_repo is not None:
        env["APPS_REPO"] = str(apps_repo)
    return subprocess.run(
        ["bash", str(REPO_ROOT / "ci" / "checks" / "env-schema")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("empty", [False, True], ids=["absent", "empty"])
def test_core_only_instance_needs_no_apps_checkout(tmp_path: Path, empty: bool) -> None:
    """Rule: an instance that lists no app passes env checks with an empty or absent checkout."""
    instance = _instance(tmp_path, core_only=True)
    checkout = tmp_path / "apps"
    checkout.mkdir()
    proc = _env_schema(instance, checkout if empty else None)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_instance_listing_apps_refuses_an_empty_checkout(tmp_path: Path) -> None:
    """Rule: an instance that lists an app fails closed on a checkout holding no apps."""
    instance = _instance(tmp_path, core_only=False)
    checkout = tmp_path / "apps"
    checkout.mkdir()
    proc = _env_schema(instance, checkout)
    assert proc.returncode != 0
    assert "not an apps repository" in proc.stderr


def _apps_remote(path: Path) -> str:
    """A local apps repository whose default branch no longer holds notes, while notes/v0.1.0
    tags the commit that does. Returns the default branch's SHA."""
    shutil.copytree(REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps", path)

    def run(*args: str) -> str:
        return subprocess.run([*_GIT, *args], cwd=path, check=True, capture_output=True, text=True).stdout

    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("commit", "-q", "-m", "notes 0.1.0")
    run("tag", "-a", "notes/v0.1.0", "-m", "notes/v0.1.0")
    run("rm", "-rq", "notes")
    run("commit", "-q", "-m", "notes moved on")
    return run("rev-parse", "HEAD").strip()


def _write_env(instance: Path, name: str, url: str, apps: list[str]) -> None:
    values = instance / "envs" / name / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text(yaml.safe_dump({"apps_git_repo_url": url}), encoding="utf-8")
    (values / "switches.yaml").write_text(yaml.safe_dump({"apps": apps}), encoding="utf-8")
    (values / "apps.yaml").write_text(yaml.safe_dump({"versions": {app: "v0.1.0" for app in apps}}), encoding="utf-8")


def _helper(tmp_path: Path, instance: Path) -> subprocess.CompletedProcess[str]:
    """Run a copy of the helper against the instance."""
    product = tmp_path / "product"
    (product / "ci" / "checks").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "ci" / "checkout-apps-for-instance.sh", product / "ci")
    shutil.copytree(REPO_ROOT / "ci" / "checks" / "lib", product / "ci" / "checks" / "lib")
    # The helper runs jarvis.checks.apps_checkout from the product root.
    shutil.copytree(REPO_ROOT / "jarvis", product / "jarvis", ignore=shutil.ignore_patterns("__pycache__"))
    return subprocess.run(
        ["bash", str(product / "ci" / "checkout-apps-for-instance.sh")],
        env={**os.environ, "INSTANCE": str(instance)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_helper_checks_out_the_default_branch_with_every_tag(tmp_path: Path) -> None:
    """Rule: the URL comes from the env that lists apps; the checkout holds the default branch
    (its SHA logged) and the app tags, and the checks read each app at its pinned tag."""
    from jarvis.lib import backup_selection as _backup_selection

    sha = _apps_remote(tmp_path / "remote")
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///nonexistent/core-only", [])
    _write_env(instance, "env-b", f"file://{tmp_path / 'remote'}", ["notes"])
    proc = _helper(tmp_path, instance)
    assert proc.returncode == 0, proc.stderr
    checkout = Path(proc.stdout.strip())
    assert sha in proc.stderr
    assert not (checkout / "notes").exists()
    pinned = _backup_selection._pinned_view(checkout, instance / "envs" / "env-b")
    assert (pinned / "notes" / "app.yaml").is_file()


def test_a_pin_the_checkout_lacks_fails(tmp_path: Path) -> None:
    """Rule: the checks never fall back to the tip for an app whose pinned tag is missing."""
    from jarvis.lib import backup_selection as _backup_selection

    _apps_remote(tmp_path / "remote")
    instance = tmp_path / "instance"
    _write_env(instance, "env-b", f"file://{tmp_path / 'remote'}", ["notes"])
    checkout = Path(_helper(tmp_path, instance).stdout.strip())
    (instance / "envs" / "env-b" / "values" / "apps.yaml").write_text("versions:\n  notes: v9.9.9\n", encoding="utf-8")
    with pytest.raises(_backup_selection.AppsRepoError, match="no tag notes/v9.9.9"):
        _backup_selection._pinned_view(checkout, instance / "envs" / "env-b")


def test_helper_is_silent_for_a_core_only_instance(tmp_path: Path) -> None:
    """Rule: with no env listing an app the helper checks out nothing and succeeds."""
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///nonexistent/core-only", [])
    proc = _helper(tmp_path, instance)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_helper_fails_clearly_on_an_unreadable_repository(tmp_path: Path) -> None:
    """Rule: an unreadable repository stops the helper with the reason."""
    instance = tmp_path / "instance"
    _write_env(instance, "env-b", "file:///nonexistent/apps", ["notes"])
    proc = _helper(tmp_path, instance)
    assert proc.returncode != 0
    assert "cannot read branch HEAD" in proc.stderr
    assert proc.stdout == ""


def test_helper_refuses_envs_reading_different_repositories(tmp_path: Path) -> None:
    """Rule: one checkout cannot prove two repositories, so the helper names the conflict, not the first env's."""
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///one", ["notes"])
    _write_env(instance, "env-b", "file:///two", ["notes"])
    proc = _helper(tmp_path, instance)
    assert proc.returncode != 0
    assert "different apps repositories" in proc.stderr


def test_unreadable_app_failure_names_the_pinned_tag(tmp_path: Path) -> None:
    """Rule: when the checkout cannot answer for a selected app, the failure says it reads the pinned tag."""
    from jarvis.lib import backup_selection as _backup_selection

    stale = instance_apps_repo_for_checks(tmp_path)
    shutil.rmtree(stale / "notes")
    with pytest.raises(_backup_selection.AppsRepoError, match="pinned tag"):
        _backup_selection.backup_eligible(stale, "notes")
