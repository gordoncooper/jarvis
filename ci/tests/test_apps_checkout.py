"""An instance's apps checkout: when env checks need it, which repository fetch reads, and that
the checks read each app at its pinned tag."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT, instance_apps_repo_for_checks, link_core_trees, render_switches, set_switches

_GIT = ["git", "-c", "user.email=fixture@example.com", "-c", "user.name=fixture"]


def _instance(tmp_path: Path, *, core_only: bool) -> Path:
    """Instance checkout with env-00; core-only drops its apps but keeps a backups section."""
    instance = tmp_path / "instance"
    env_dir = instance / "envs" / "env-00"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", env_dir)
    link_core_trees(instance, REPO_ROOT)
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


def _fetch(tmp_path: Path, instance: Path) -> Path | None:
    from jarvis.apps.checkout import fetch

    return fetch(instance, tmp_path / "checkout")


def test_fetch_checks_out_the_default_branch_with_every_tag(tmp_path: Path) -> None:
    """Rule: the URL comes from the env that lists apps; the checkout holds the default branch
    (its SHA logged) and the app tags, and the checks read each app at its pinned tag."""
    from jarvis.backups import selection as _backup_selection

    sha = _apps_remote(tmp_path / "remote")
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///nonexistent/core-only", [])
    _write_env(instance, "env-b", f"file://{tmp_path / 'remote'}", ["notes"])
    checkout = _fetch(tmp_path, instance)
    assert checkout is not None
    head = subprocess.run([*_GIT, "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True)
    assert head.stdout.strip() == sha
    assert not (checkout / "notes").exists()
    pinned = _backup_selection._pinned_view(checkout, instance / "envs" / "env-b")
    assert (pinned / "notes" / "app.yaml").is_file()


def test_a_pin_the_checkout_lacks_fails(tmp_path: Path) -> None:
    """Rule: the checks never fall back to the tip for an app whose pinned tag is missing."""
    from jarvis.backups import selection as _backup_selection

    _apps_remote(tmp_path / "remote")
    instance = tmp_path / "instance"
    _write_env(instance, "env-b", f"file://{tmp_path / 'remote'}", ["notes"])
    checkout = _fetch(tmp_path, instance)
    assert checkout is not None
    (instance / "envs" / "env-b" / "values" / "apps.yaml").write_text("versions:\n  notes: v9.9.9\n", encoding="utf-8")
    with pytest.raises(_backup_selection.AppsRepoError, match="no tag notes/v9.9.9"):
        _backup_selection._pinned_view(checkout, instance / "envs" / "env-b")


def test_a_pin_newer_than_the_clone_fetches_its_tags_once(tmp_path: Path) -> None:
    """Rule: an operator's clone that predates a pinned tag fetches the remote's tags and reads
    the app at it, instead of asking the operator to fetch; it moves no branch."""
    from jarvis.backups import selection as _backup_selection

    remote = tmp_path / "remote"
    _apps_remote(remote)
    clone = tmp_path / "clone"
    subprocess.run([*_GIT, "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
    head = subprocess.run([*_GIT, "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True).stdout
    subprocess.run([*_GIT, "tag", "-a", "notes/v0.1.1", "notes/v0.1.0", "-m", "notes/v0.1.1"], cwd=remote, check=True)
    instance = tmp_path / "instance"
    _write_env(instance, "env-b", f"file://{remote}", ["notes"])
    (instance / "envs" / "env-b" / "values" / "apps.yaml").write_text("versions:\n  notes: v0.1.1\n", encoding="utf-8")
    view = _backup_selection._pinned_view(clone, instance / "envs" / "env-b")
    assert (view / "notes" / "app.yaml").is_file()
    after = subprocess.run([*_GIT, "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True).stdout
    assert after == head


def test_fetch_checks_out_nothing_for_a_core_only_instance(tmp_path: Path) -> None:
    """Rule: with no env listing an app nothing is checked out."""
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///nonexistent/core-only", [])
    assert _fetch(tmp_path, instance) is None
    assert not (tmp_path / "checkout").exists()


def test_fetch_fails_clearly_on_an_unreadable_repository(tmp_path: Path) -> None:
    """Rule: an unreadable repository stops the checkout with the reason."""
    from jarvis.apps.checkout import AppsCheckoutError

    instance = tmp_path / "instance"
    _write_env(instance, "env-b", "file:///nonexistent/apps", ["notes"])
    with pytest.raises(AppsCheckoutError, match="cannot read branch HEAD"):
        _fetch(tmp_path, instance)


def test_fetch_refuses_envs_reading_different_repositories(tmp_path: Path) -> None:
    """Rule: one checkout cannot prove two repositories, so the conflict is named, not the first env's."""
    from jarvis.apps.checkout import AppsCheckoutError

    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///one", ["notes"])
    _write_env(instance, "env-b", "file:///two", ["notes"])
    with pytest.raises(AppsCheckoutError, match="different apps repositories"):
        _fetch(tmp_path, instance)


def test_unreadable_app_failure_names_the_pinned_tag(tmp_path: Path) -> None:
    """Rule: when the checkout cannot answer for a selected app, the failure says it reads the pinned tag."""
    from jarvis.backups import selection as _backup_selection

    stale = instance_apps_repo_for_checks(tmp_path)
    shutil.rmtree(stale / "notes")
    with pytest.raises(_backup_selection.AppsRepoError, match="pinned tag"):
        _backup_selection.backup_eligible(stale, "notes")
