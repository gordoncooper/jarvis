"""The jarvis CLI: one entry point, run from an instance repo, for the env it names or the only one."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.cli import doctor, up
from jarvis.cli import main as cli
from support import REPO_ROOT

_GIT = ["git", "-c", "user.email=fixture@example.com", "-c", "user.name=fixture"]


def _instance(tmp_path: Path, *envs: str) -> Path:
    root = tmp_path / "instance"
    for env in envs:
        (root / "envs" / env / "values").mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["render", "--check"], (None, None, ["render", "--check"])),
        (["--env", "lab", "render"], ("lab", None, ["render"])),
        (["render", "--env=lab", "--check"], ("lab", None, ["render", "--check"])),
        (["--core", "/c", "verify", "--env", "lab"], ("lab", "/c", ["verify"])),
    ],
)
def test_env_and_core_may_come_before_or_after_the_command(argv, expected) -> None:
    assert cli.split_globals(argv) == expected


@pytest.mark.parametrize("argv", [["render", "--env"], ["--env", "a", "--env", "b", "render"]])
def test_a_missing_or_repeated_global_is_a_usage_error(argv) -> None:
    with pytest.raises(cli.UsageError):
        cli.split_globals(argv)


def test_the_instance_is_the_nearest_folder_holding_envs(tmp_path: Path) -> None:
    root = _instance(tmp_path, "home")
    deep = root / "envs" / "home" / "values"
    assert cli.find_instance(deep) == root
    with pytest.raises(cli.UsageError, match="inside an instance repo"):
        cli.find_instance(tmp_path)


def test_the_env_is_implicit_only_when_there_is_one(tmp_path: Path) -> None:
    """Rule: --env is needed only when the repo holds several envs, and must name one of them."""
    one = _instance(tmp_path / "one", "home")
    assert cli.choose_env(one, None) == "home"
    two = _instance(tmp_path / "two", "home", "lab")
    assert cli.choose_env(two, "lab") == "lab"
    with pytest.raises(cli.UsageError, match="pass --env"):
        cli.choose_env(two, None)
    with pytest.raises(cli.UsageError, match="no env 'nope'"):
        cli.choose_env(two, "nope")


def _jarvis(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(Path(sys.executable).parent / "jarvis"), *args], cwd=cwd, capture_output=True, text=True)


def test_usage_errors_exit_2_and_help_needs_no_instance(tmp_path: Path) -> None:
    """Rule: exit 2 for a usage error; jarvis --help and jarvis <command> --help work anywhere."""
    assert _jarvis(cwd=tmp_path).returncode == 2
    assert _jarvis("bogus", cwd=tmp_path).returncode == 2
    outside = _jarvis("verify", cwd=tmp_path)
    assert outside.returncode == 2 and "inside an instance repo" in outside.stderr
    top = _jarvis("--help", cwd=tmp_path)
    assert top.returncode == 0 and "Exit codes" in top.stdout
    for command, words in (("restore", "--as-of"), ("site", "--tags"), ("up", "doctor, validate")):
        done = _jarvis(command, "--help", cwd=tmp_path)
        assert done.returncode == 0 and words in done.stdout, (command, done.stdout + done.stderr)


def test_every_command_names_a_module_that_has_its_entry(tmp_path: Path) -> None:
    """Rule: each command's module exists and has the entry the CLI calls."""
    import importlib

    for name, command in cli.COMMANDS.items():
        module = importlib.import_module(command.module)
        assert hasattr(module, "main" if command.takes_options else "run"), name


def test_core_runs_its_own_envs_itself(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: in core's own checkout the command runs that checkout, whatever env-00 pins."""
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("PATH", os.environ["PATH"])  # the CLI puts the toolchain first; undo it after
    monkeypatch.setenv("APPS_REPO", str(REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"))
    assert cli.main(["--env", "env-00", "render", "--check"]) == 0
    assert "INSTANCE" not in os.environ


def test_validate_checks_only_the_named_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: jarvis validate limits the schema to its env, so each env is checked by the core it pins."""
    from jarvis.env import validate

    seen: list[str | None] = []
    monkeypatch.setattr(validate, "run", lambda instance: seen.append(os.environ.get("ENVS")) or 0)
    monkeypatch.setenv("ENVS", "")  # restored after the test, as the CLI sets it in place
    assert cli.COMMANDS["validate"].run(tmp_path, "lab", []) == 0
    assert seen == ["lab"]
    with pytest.raises(cli.UsageError):
        cli.COMMANDS["validate"].run(tmp_path, "lab", ["--extra"])


def test_doctor_fails_an_age_key_others_can_read(tmp_path: Path) -> None:
    """Rule: the age key must be yours and mode 0600; a missing one says where it goes."""
    env_dir = tmp_path / "envs" / "home"
    (env_dir / "values").mkdir(parents=True)
    key = tmp_path / "keys.txt"
    (env_dir / "values" / "cluster-vars.yaml").write_text(f"age_key_file: {key}\n", encoding="utf-8")
    assert doctor.check_age_key(env_dir)[0][0] == "FAIL"
    key.write_text("AGE-SECRET-KEY-1\n", encoding="utf-8")
    key.chmod(0o644)
    status, message = doctor.check_age_key(env_dir)[0]
    assert status == "FAIL" and "chmod 600" in message
    key.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert doctor.check_age_key(env_dir)[0][0] == "PASS"


def test_doctor_names_a_host_that_does_not_resolve(tmp_path: Path) -> None:
    env_dir = tmp_path / "envs" / "home"
    (env_dir / "inventory").mkdir(parents=True)
    (env_dir / "inventory" / "hosts.yml").write_text(
        "all:\n  hosts:\n    localhost: {}\n    nothing.invalid: {}\n", encoding="utf-8"
    )
    results = dict((message.split()[0], status) for status, message in doctor.check_hosts(env_dir))
    assert results == {"localhost": "PASS", "nothing.invalid": "FAIL"}


def _pushed_instance(tmp_path: Path) -> Path:
    """An instance clone whose env is committed and pushed to its origin's main."""
    origin = tmp_path / "origin.git"
    subprocess.run([*_GIT, "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run([*_GIT, "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    values = clone / "envs" / "home" / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text("flux_git_ref: refs/heads/main\n", encoding="utf-8")
    subprocess.run([*_GIT, "add", "-A"], cwd=clone, check=True)
    subprocess.run([*_GIT, "commit", "-q", "-m", "home"], cwd=clone, check=True)
    subprocess.run([*_GIT, "push", "-q", "origin", "HEAD:main"], cwd=clone, check=True, capture_output=True)
    return clone


def test_up_refuses_until_flux_reads_what_the_checkout_holds(tmp_path: Path) -> None:
    """Rule: jarvis up stops before site while the env has uncommitted changes or unpushed commits."""
    clone = _pushed_instance(tmp_path)
    assert up.unpushed(clone, "home") is None
    secret = clone / "envs" / "home" / "secrets" / "k3s-token.sops.yaml"
    secret.parent.mkdir()
    secret.write_text("sealed\n", encoding="utf-8")
    assert "uncommitted" in (up.unpushed(clone, "home") or "")
    subprocess.run([*_GIT, "add", "-A"], cwd=clone, check=True)
    subprocess.run([*_GIT, "commit", "-q", "-m", "secrets"], cwd=clone, check=True)
    assert "push them first" in (up.unpushed(clone, "home") or "")


def test_up_stops_at_the_first_failing_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: each step must pass before the next; the first failure's exit code is up's."""
    ran: list[str] = []

    def step(name: str, code: int = 0):
        return lambda *args, **kwargs: ran.append(name) or code

    monkeypatch.setattr(up.doctor, "run", step("doctor"))
    monkeypatch.setattr(up.validate, "run", step("validate"))
    monkeypatch.setattr(up.render, "main", step("render"))
    monkeypatch.setattr(up.mint, "main", step("secrets"))
    monkeypatch.setattr(up, "_refuse_unpushed", step("pushed", 3))
    monkeypatch.setattr(up.site, "main", step("site"))
    assert up.run(tmp_path, "home") == 3
    assert ran == ["doctor", "validate", "render", "secrets", "pushed"]
    assert "stopped at pushed" in capsys.readouterr().out


def test_the_entry_point_loads_nothing_that_reads_core_trees() -> None:
    """Rule: an installed jarvis holds no core trees until it hands off to the pinned checkout, so
    its entry point imports only what choosing that checkout needs (no schema, no manifests)."""
    probe = "import sys, jarvis.cli.main; print(sorted(m for m in sys.modules if m.startswith('jarvis.')))"
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert eval(loaded) == ["jarvis.cli", "jarvis.cli.core", "jarvis.cli.main", "jarvis.env", "jarvis.env.pin", "jarvis.paths"]
