"""An instance's apps checkout: when env checks need it, and which repository and branch the helper reads."""

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
    """A local apps repository: main has no notes app, trunk does. Returns trunk's SHA."""
    shutil.copytree(REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps", path)

    def run(*args: str) -> str:
        return subprocess.run([*_GIT, *args], cwd=path, check=True, capture_output=True, text=True).stdout

    run("init", "-q", "-b", "trunk")
    run("add", "-A")
    run("commit", "-q", "-m", "tip")
    sha = run("rev-parse", "HEAD").strip()
    run("switch", "-q", "-c", "main")
    run("rm", "-rq", "notes")
    run("commit", "-q", "-m", "other branch")
    return sha


def _write_env(instance: Path, name: str, url: str, apps: list[str]) -> None:
    values = instance / "envs" / name / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text(yaml.safe_dump({"apps_git_repo_url": url}), encoding="utf-8")
    (values / "switches.yaml").write_text(yaml.safe_dump({"apps": apps}), encoding="utf-8")


def _helper(tmp_path: Path, instance: Path, *, branches: tuple[str, ...] = ("trunk",)) -> subprocess.CompletedProcess[str]:
    """Run a copy of the helper whose wiring deploys the given branches."""
    product = tmp_path / "product"
    (product / "ci" / "checks").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "ci" / "checkout-apps-for-instance.sh", product / "ci")
    shutil.copytree(REPO_ROOT / "ci" / "checks" / "lib", product / "ci" / "checks" / "lib")
    shutil.copytree(REPO_ROOT / "tools" / "lib", product / "tools" / "lib")
    wiring = product / "platform" / "app-hosting" / "app" / "flux.yaml"
    wiring.parent.mkdir(parents=True)
    wiring.write_text(
        "---\n".join(f"kind: GitRepository\nspec:\n  ref:\n    branch: {branch}\n" for branch in branches),
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(product / "ci" / "checkout-apps-for-instance.sh")],
        env={**os.environ, "INSTANCE": str(instance)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_helper_checks_out_the_listing_envs_url_at_the_deployed_branch(tmp_path: Path) -> None:
    """Rule: the URL comes from the env that lists apps and the branch from Flux's wiring; the SHA is logged."""
    sha = _apps_remote(tmp_path / "remote")
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///nonexistent/core-only", [])
    _write_env(instance, "env-b", f"file://{tmp_path / 'remote'}", ["notes"])
    proc = _helper(tmp_path, instance)
    assert proc.returncode == 0, proc.stderr
    checkout = Path(proc.stdout.strip())
    assert (checkout / "notes" / "app.yaml").is_file()
    assert sha in proc.stderr


def test_helper_updates_an_existing_checkout_to_the_tip(tmp_path: Path) -> None:
    """Rule: a second run moves a stale checkout to the live tip."""
    _apps_remote(tmp_path / "remote")
    instance = tmp_path / "instance"
    _write_env(instance, "env-b", f"file://{tmp_path / 'remote'}", ["notes"])
    (tmp_path / "first").mkdir()
    assert _helper(tmp_path / "first", instance).returncode == 0
    remote = tmp_path / "remote"
    subprocess.run([*_GIT, "switch", "-q", "trunk"], cwd=remote, check=True)
    (remote / "new-app").mkdir()
    (remote / "new-app" / "app.yaml").write_text("spec: {}\n", encoding="utf-8")
    subprocess.run([*_GIT, "add", "-A"], cwd=remote, check=True)
    subprocess.run([*_GIT, "commit", "-q", "-m", "newer"], cwd=remote, check=True)
    (tmp_path / "second").mkdir()
    proc = _helper(tmp_path / "second", instance)
    assert proc.returncode == 0, proc.stderr
    assert (Path(proc.stdout.strip()) / "new-app" / "app.yaml").is_file()


def test_helper_is_silent_for_a_core_only_instance(tmp_path: Path) -> None:
    """Rule: with no env listing an app the helper checks out nothing and succeeds."""
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///nonexistent/core-only", [])
    proc = _helper(tmp_path, instance)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize(
    ("url_b", "branches", "message"),
    [
        ("file:///nonexistent/apps", ("trunk",), "cannot read branch trunk"),
        ("REMOTE", ("trunk", "main"), "expected one apps branch"),
    ],
    ids=["repository-missing", "wiring-ambiguous"],
)
def test_helper_fails_clearly(tmp_path: Path, url_b: str, branches: tuple[str, ...], message: str) -> None:
    """Rule: an unreadable repository or an ambiguous deployed branch stops the helper with the reason."""
    _apps_remote(tmp_path / "remote")
    instance = tmp_path / "instance"
    _write_env(instance, "env-b", f"file://{tmp_path / 'remote'}" if url_b == "REMOTE" else url_b, ["notes"])
    proc = _helper(tmp_path, instance, branches=branches)
    assert proc.returncode != 0
    assert message in proc.stderr
    assert proc.stdout == ""


def test_helper_refuses_envs_reading_different_repositories(tmp_path: Path) -> None:
    """Rule: one checkout cannot prove two repositories, so the helper names the conflict, not the first env's."""
    instance = tmp_path / "instance"
    _write_env(instance, "env-a", "file:///one", ["notes"])
    _write_env(instance, "env-b", "file:///two", ["notes"])
    proc = _helper(tmp_path, instance)
    assert proc.returncode != 0
    assert "different apps repositories" in proc.stderr


def test_stale_checkout_failure_names_the_live_tip(tmp_path: Path) -> None:
    """Rule: when the checkout cannot answer for a selected app, the failure says it needs the live tip."""
    import _backup_selection

    stale = instance_apps_repo_for_checks(tmp_path)
    shutil.rmtree(stale / "notes")
    with pytest.raises(_backup_selection.AppsRepoError, match="live tip"):
        _backup_selection.backup_eligible(stale, "notes")
