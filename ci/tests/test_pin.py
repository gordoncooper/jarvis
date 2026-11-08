"""The env's core pin: tools act on a cluster only from the checkout at the env's core tag."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.lib.pin import PinError, require_pin
from support import REPO_ROOT


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def core(tmp_path: Path) -> Path:
    """A core checkout whose HEAD is annotated tag v1.0.0."""
    repo = tmp_path / "core"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "file").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "file")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    return repo


def _env(tmp_path: Path, ref: str) -> Path:
    env = tmp_path / "instance" / "envs" / "env-x"
    (env / "values").mkdir(parents=True)
    (env / "values" / "cluster-vars.yaml").write_text(f"core_git_ref: {ref}\n", encoding="utf-8")
    return env


def test_checkout_at_the_pinned_tag_runs(tmp_path: Path, core: Path) -> None:
    """Rule: a clean checkout at the env's tag passes, an annotated tag included."""
    assert require_pin(core, _env(tmp_path, "refs/tags/v1.0.0"), allow_unpinned=False) is None


def _refused(core: Path, env: Path, expected: str) -> None:
    with pytest.raises(PinError) as caught:
        require_pin(core, env, allow_unpinned=False)
    assert expected in str(caught.value) and "--allow-unpinned" in str(caught.value)
    assert expected in (require_pin(core, env, allow_unpinned=True) or "")


def test_another_commit_is_refused(tmp_path: Path, core: Path) -> None:
    """Rule: a checkout past the tag is refused, and with --allow-unpinned only warned."""
    (core / "file").write_text("two\n", encoding="utf-8")
    _git(core, "commit", "-q", "-am", "two")
    _refused(core, _env(tmp_path, "refs/tags/v1.0.0"), "git checkout v1.0.0")


def test_uncommitted_changes_are_refused(tmp_path: Path, core: Path) -> None:
    """Rule: a tracked file changed in the checkout is not the tag."""
    (core / "file").write_text("edited\n", encoding="utf-8")
    _refused(core, _env(tmp_path, "refs/tags/v1.0.0"), "uncommitted changes")


def test_a_branch_ref_is_not_a_pin(tmp_path: Path, core: Path) -> None:
    """Rule: an env that follows a branch is refused even at that branch's tip."""
    _refused(core, _env(tmp_path, "refs/heads/main"), "not a tag")


def test_a_tag_the_checkout_lacks_is_refused(tmp_path: Path, core: Path) -> None:
    _refused(core, _env(tmp_path, "refs/tags/v9.9.9"), "git fetch --tags")


def test_pin_check_script_refuses_and_allows(tmp_path: Path) -> None:
    """Rule: tools/pin-check (make site, verify, drill, restore) exits non-zero for an env whose
    tag this checkout lacks, and zero with --allow-unpinned; the example env is never deployed."""
    env = _env(tmp_path, "refs/tags/v0.0.0-missing")
    run = lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, str(REPO_ROOT / "tools" / "pin-check"), "--env", env.name, *args],
        capture_output=True, text=True, check=False, env={**os.environ, "INSTANCE": str(env.parents[1])},
    )
    refused = run()
    assert refused.returncode == 1 and "git fetch --tags" in refused.stderr, refused.stderr
    allowed = run("--allow-unpinned")
    assert allowed.returncode == 0 and "unpinned run" in allowed.stderr, allowed.stderr
    example = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "pin-check"), "--env", "env-00"],
                             capture_output=True, text=True, check=False, env={k: v for k, v in os.environ.items() if k != "INSTANCE"})
    assert example.returncode == 0, example.stderr


def test_release_notes_are_the_tags_changelog_section(tmp_path: Path) -> None:
    """Rule: a release's notes are its CHANGELOG.md section; a tag with none fails the release."""
    notes = subprocess.run([sys.executable, str(REPO_ROOT / "ci" / "release-notes"), "v0.2.0"],
                           capture_output=True, text=True, check=False)
    assert notes.returncode == 0 and "### Upgrading" in notes.stdout and "## " not in notes.stdout.replace("### ", "")
    missing = subprocess.run([sys.executable, str(REPO_ROOT / "ci" / "release-notes"), "v0.0.0-none"],
                             capture_output=True, text=True, check=False)
    assert missing.returncode == 1 and "no '## v0.0.0-none' section" in missing.stderr
