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


def test_core_runs_this_checkout_unpinned_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: --core <this checkout> runs it for an env pinned elsewhere, with a warning."""
    from jarvis.cli import core as cli_core
    from support import copy_sample

    instance = copy_sample(tmp_path / "instance")
    monkeypatch.chdir(instance / "envs")
    # The CLI sets these in place; register them so they are restored after the test.
    for name in ("PATH", "INSTANCE", cli_core.UNPINNED_VAR):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setenv("APPS_REPO", str(REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"))
    assert cli.main(["--core", str(REPO_ROOT), "render", "--check"]) == 0
    assert "WARN jarvis: unpinned run with --core: env-00 pins core v0.2.0" in capsys.readouterr().err
    assert os.environ["INSTANCE"] == str(instance)


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


def _flux_env(tmp_path: Path) -> Path:
    flux = tmp_path / "envs" / "home" / "flux"
    flux.mkdir(parents=True)
    (flux / "sync.yaml").write_text(
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nmetadata:\n  name: flux-system\n", encoding="utf-8"
    )
    (flux / "services.yaml").write_text(
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nmetadata:\n  name: services\n", encoding="utf-8"
    )
    (flux / "kustomization.yaml").write_text("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n", encoding="utf-8")
    return flux.parent


def _listing(*ready: tuple[str, bool]) -> subprocess.CompletedProcess[str]:
    import json

    items = [
        {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": str(ok), "message": "working"}]}}
        for name, ok in ready
    ]
    return subprocess.CompletedProcess([], 0, json.dumps({"items": items}), "")


def test_up_waits_for_the_layers_the_root_has_not_made_yet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: a Ready root is not a Ready cluster. up waits until core's layers and the env's
    declared Kustomizations exist, and every listed one is Ready on two polls in a row."""
    env_dir = _flux_env(tmp_path)
    assert up.expected_kustomizations(env_dir) == {"flux-system", "services", "platform-controllers", "platform-configs"}
    every = ("flux-system", "platform-controllers", "platform-configs", "services")
    polls = iter(
        [
            _listing(("flux-system", True)),
            _listing(*((name, name != "platform-configs") for name in every)),
            _listing(*((name, True) for name in every)),
            _listing(*((name, True) for name in every), ("addon-x", False)),
            _listing(*((name, True) for name in (*every, "addon-x"))),
            _listing(*((name, True) for name in (*every, "addon-x"))),
        ]
    )
    calls: list[int] = []
    monkeypatch.setattr(up, "kubectl", lambda *a, **k: calls.append(1) or next(polls))
    monkeypatch.setattr(up, "_sleep", lambda seconds: None)
    assert up.wait_for_flux("home", env_dir) == 0
    assert len(calls) == 6 and "Ready (5)" in capsys.readouterr().out


def test_up_names_what_is_not_ready_when_the_wait_runs_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    env_dir = _flux_env(tmp_path)
    now = [0.0]
    monkeypatch.setattr(up, "kubectl", lambda *a, **k: _listing(("flux-system", True)))
    monkeypatch.setattr(up, "_clock", lambda: now[0])
    monkeypatch.setattr(up, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert up.wait_for_flux("home", env_dir) == 1
    out = capsys.readouterr().out
    assert "platform-configs: not created yet" in out and "services: not created yet" in out


def test_the_entry_point_loads_nothing_that_reads_core_trees() -> None:
    """Rule: an installed jarvis holds no core trees until it hands off to the pinned checkout, so
    its entry point imports only what choosing that checkout needs (no schema, no manifests)."""
    probe = "import sys, jarvis.cli.main; print(sorted(m for m in sys.modules if m.startswith('jarvis.')))"
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert eval(loaded) == ["jarvis.cli", "jarvis.cli.core", "jarvis.cli.main", "jarvis.env", "jarvis.env.pin", "jarvis.paths"]


def test_doctor_installs_the_hook_a_fresh_clone_lacks_and_keeps_anyone_elses(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert doctor.check_hook(tmp_path)[0][1].startswith("installed the pre-commit hook")
    assert doctor.check_hook(tmp_path)[0][0] == "PASS"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nmy own\n", encoding="utf-8")
    assert doctor.check_hook(tmp_path)[0][0] == "WARN"
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nmy own\n"


def test_doctor_finds_a_worktrees_hooks_where_git_runs_them(tmp_path: Path) -> None:
    """Rule: in a git worktree .git is a file and the hooks are the main repository's; doctor asks
    git where they are rather than calling the worktree's hook someone else's."""
    main = tmp_path / "main"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(tmp_path / "wt")], check=True)
    assert (tmp_path / "wt" / ".git").is_file()
    level, message = doctor.check_hook(tmp_path / "wt")[0]
    assert level == "PASS" and str(main / ".git" / "hooks" / "pre-commit") in message
    assert doctor.check_hook(tmp_path / "wt")[0] == ("PASS", "the pre-commit hook runs jarvis render --check and validate")


def test_version_names_this_release_and_each_envs_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli._core, "own_release", lambda: ("https://github.com/example/jarvis", "v9.9.9"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == "jarvis v9.9.9 (https://github.com/example/jarvis)\n"
    for env, ref in (("home", "refs/tags/v9.9.8"), ("lab", "refs/heads/main")):
        (tmp_path / "envs" / env / "values").mkdir(parents=True)
        (tmp_path / "envs" / env / "values" / "cluster-vars.yaml").write_text(f"core_git_ref: {ref}\n", encoding="utf-8")
    assert cli.main(["--version"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "home pins v9.9.8" and out[2].startswith("lab: ") and "not a tag" in out[2]


def test_help_in_an_instance_is_the_pinned_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: a command's options are its env's release's; an installed CLI, which holds no core
    trees, hands --help off to that release like the command itself, with the env it chose."""
    for env in ("home", "lab"):
        values = tmp_path / "envs" / env / "values"
        values.mkdir(parents=True)
        (values / "cluster-vars.yaml").write_text(
            "core_git_repo_url: https://github.com/example/jarvis\ncore_git_ref: refs/tags/v9.9.9\n", encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(cli._core.DELEGATED_VAR, raising=False)
    monkeypatch.delenv(cli._core.UNPINNED_VAR, raising=False)
    monkeypatch.setattr(cli._core, "checkout", lambda env_dir: tmp_path / "cached")
    handed: list[list[str]] = []

    def hand_off(checkout: Path, argv: list[str]) -> None:
        handed.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(cli._core, "hand_off", hand_off)
    with pytest.raises(SystemExit):
        cli.main(["validate", "--help"])
    assert handed == [["--env", "home", "validate", "--help"]]


def test_help_outside_an_instance_says_where_to_ask_when_this_cli_cannot_load_it(tmp_path: Path, monkeypatch, capsys) -> None:
    """Rule: an installed CLI without core trees answers --help outside an instance repo with where
    to run it, not a traceback."""
    monkeypatch.chdir(tmp_path)

    def missing(name: str):
        raise FileNotFoundError(f"{name}: schema/cluster-vars.schema.json")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["validate", "--help"]) == 0
    assert "jarvis validate: run jarvis validate --help from inside an instance repo" in capsys.readouterr().out
