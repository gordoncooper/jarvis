"""The env's core pin: the jarvis CLI runs each env's command with the core release it pins."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.cli import core as cli_core
from jarvis.cli import main as cli_main
from jarvis.env.pin import PinError, pin_problem
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
    (env / "values").mkdir(parents=True, exist_ok=True)
    (env / "values" / "cluster-vars.yaml").write_text(f"core_git_ref: {ref}\n", encoding="utf-8")
    return env


def test_checkout_at_the_pinned_tag_is_the_pin(tmp_path: Path, core: Path) -> None:
    """Rule: a clean checkout at the env's tag is its pin, an annotated tag included."""
    assert pin_problem(core, _env(tmp_path, "refs/tags/v1.0.0")) is None


def test_another_commit_is_not_the_pin(tmp_path: Path, core: Path) -> None:
    (core / "file").write_text("two\n", encoding="utf-8")
    _git(core, "commit", "-q", "-am", "two")
    assert "but this checkout is at" in (pin_problem(core, _env(tmp_path, "refs/tags/v1.0.0")) or "")


def test_uncommitted_changes_are_not_the_pin(tmp_path: Path, core: Path) -> None:
    """Rule: a tracked file changed in the checkout is not the tag."""
    (core / "file").write_text("edited\n", encoding="utf-8")
    assert "uncommitted changes" in (pin_problem(core, _env(tmp_path, "refs/tags/v1.0.0")) or "")


def test_a_branch_ref_is_not_a_pin(tmp_path: Path, core: Path) -> None:
    """Rule: an env that follows a branch has no pin, even at that branch's tip."""
    assert "not a tag" in (pin_problem(core, _env(tmp_path, "refs/heads/main")) or "")
    with pytest.raises(PinError, match="not a tag"):
        cli_core.pinned_tag(_env(tmp_path, "refs/heads/main"))


def test_a_tag_the_checkout_lacks_is_not_the_pin(tmp_path: Path, core: Path) -> None:
    assert "does not have" in (pin_problem(core, _env(tmp_path, "refs/tags/v9.9.9")) or "")


def test_an_env_pinned_elsewhere_runs_from_a_cached_checkout_of_its_tag(
    tmp_path: Path, core: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: when this checkout is not the env's pin, the command runs from a checkout of the
    pinned tag in the cache, cloned once and set up from its own lockfiles, with the same argv."""
    env = _env(tmp_path, "refs/tags/v1.0.0")
    with (env / "values" / "cluster-vars.yaml").open("a", encoding="utf-8") as handle:
        handle.write(f"core_git_repo_url: file://{core}\n")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv(cli_core.DELEGATED_VAR, raising=False)
    monkeypatch.delenv(cli_core.UNPINNED_VAR, raising=False)
    monkeypatch.chdir(env.parents[1])
    set_up: list[Path] = []
    handed: list[tuple[Path, list[str]]] = []
    monkeypatch.setattr(cli_core, "setup", lambda checkout: set_up.append(checkout) or (checkout / ".jarvis-ready").touch())

    def hand_off(checkout: Path, argv: list[str]) -> None:
        handed.append((checkout, argv))
        raise SystemExit(0)

    monkeypatch.setattr(cli_core, "hand_off", hand_off)
    for _ in range(2):
        with pytest.raises(SystemExit):
            cli_main.main(["render", "--check"])
    cached = tmp_path / "cache" / "jarvis" / "core" / "v1.0.0"
    assert (cached / "file").read_text(encoding="utf-8") == "one\n"
    assert set_up == [cached]
    assert handed == [(cached, ["render", "--check"])] * 2


def test_a_branch_pin_is_refused_before_anything_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: an env that follows a branch is refused (exit 3): there is no release to run."""
    env = _env(tmp_path, "refs/heads/main")
    monkeypatch.delenv(cli_core.UNPINNED_VAR, raising=False)
    monkeypatch.chdir(env.parents[1])
    assert cli_main.main(["render"]) == 3
    assert "not a tag" in capsys.readouterr().err


def test_release_notes_are_the_tags_changelog_section(tmp_path: Path) -> None:
    """Rule: a release's notes are its CHANGELOG.md section; a tag with none fails the release."""
    notes = subprocess.run([sys.executable, str(REPO_ROOT / "ci" / "release-notes"), "v0.2.0"],
                           capture_output=True, text=True, check=False)
    assert notes.returncode == 0 and "### Upgrading" in notes.stdout and "## " not in notes.stdout.replace("### ", "")
    missing = subprocess.run([sys.executable, str(REPO_ROOT / "ci" / "release-notes"), "v0.0.0-none"],
                             capture_output=True, text=True, check=False)
    assert missing.returncode == 1 and "no '## v0.0.0-none' section" in missing.stderr
