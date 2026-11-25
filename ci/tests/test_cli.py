"""The jarvis CLI: one entry point, run from an instance repo, for the env it names or the only one,
at the core release that env pins."""

from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest
import sample
import yaml

from jarvis.apps import checkout
from jarvis.cli import core as cli_core
from jarvis.cli import doctor, up
from jarvis.cli import main as cli
from jarvis.env import validate
from jarvis.env.pin import pin_problem
from jarvis.node import site
from support import REPO_ROOT, copy_sample, git, sample_root

_BRANCH_PIN = "follows refs/heads/main, not a tag; pin core_git_ref to refs/tags/<tag>"


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    return root


def _env(instance: Path, env: str, ref: str | None = None, url: str | None = None) -> Path:
    """envs/<env> in instance, its cluster-vars.yaml pinning core at ref (from url) when given."""
    values = instance / "envs" / env / "values"
    values.mkdir(parents=True, exist_ok=True)
    if ref:
        text = f"core_git_ref: {ref}\n" + (f"core_git_repo_url: {url}\n" if url else "")
        (values / "cluster-vars.yaml").write_text(text, encoding="utf-8")
    return values.parent


def _instance(tmp_path: Path, *envs: str) -> Path:
    root = tmp_path / "instance"
    for env in envs:
        _env(root, env)
    return _repo(root)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variables a run of the CLI sets in place, registered so they are restored after the test."""
    for name in ("PATH", "INSTANCE", cli_core.UNPINNED_VAR):
        monkeypatch.setenv(name, os.environ.get(name, ""))


@pytest.fixture
def handed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, list[str]]]:
    """An installed CLI's hand-offs to a core checkout, each recorded (checkout, argv) and ending the run."""
    handed: list[tuple[Path, list[str]]] = []
    monkeypatch.delenv(cli_core.DELEGATED_VAR, raising=False)
    monkeypatch.delenv(cli_core.UNPINNED_VAR, raising=False)

    def hand_off(checkout: Path, argv: list[str]) -> None:
        handed.append((checkout, argv))
        raise SystemExit(0)

    monkeypatch.setattr(cli_core, "hand_off", hand_off)
    return handed


@pytest.fixture
def core(tmp_path: Path) -> Path:
    """A core checkout whose HEAD is annotated tag v1.0.0."""
    repo = tmp_path / "core"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "file").write_text("one\n", encoding="utf-8")
    for args in (("add", "file"), ("commit", "-q", "-m", "one"), ("tag", "-a", "v1.0.0", "-m", "v1.0.0")):
        git(repo, *args)
    return repo


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(["render", "--check"], (None, None, ["render", "--check"]), id="none"),
        pytest.param(["--env", "lab", "render"], ("lab", None, ["render"]), id="env-before"),
        pytest.param(["render", "--env=lab", "--check"], ("lab", None, ["render", "--check"]), id="env-equals-between"),
        pytest.param(["--core", "/c", "verify", "--env", "lab"], ("lab", "/c", ["verify"]), id="core-before-env-after"),
    ],
)
def test_env_and_core_may_come_before_or_after_the_command(argv, expected) -> None:
    """Rule: --env and --core may come before or after the command, with = or a separate value."""
    assert cli.split_globals(argv) == expected


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(["render", "--env"], "--env needs a value", id="missing"),
        pytest.param(["--env", "a", "--env", "b", "render"], "--env given twice", id="repeated"),
    ],
)
def test_a_missing_or_repeated_global_is_a_usage_error(argv: list[str], message: str) -> None:
    """Rule: --env without a value, or given twice, is a usage error."""
    with pytest.raises(cli.UsageError) as raised:
        cli.split_globals(argv)
    assert str(raised.value) == message


def test_the_instance_is_the_git_repository_holding_envs(tmp_path: Path) -> None:
    """Rule: the instance is the git repository the command runs in, not any folder above it that
    holds envs/."""
    root = _instance(tmp_path, "home")
    assert cli.find_instance(root / "envs" / "home" / "values") == root
    other = _repo(tmp_path / "project")
    (other / "tools" / "envs").mkdir(parents=True)
    for start in (tmp_path, other / "tools"):
        with pytest.raises(cli.UsageError) as raised:
            cli.find_instance(start)
        assert str(raised.value) == "run jarvis from inside an instance repo (a git repository that holds envs/)"


def test_the_env_is_implicit_only_when_there_is_one(tmp_path: Path) -> None:
    """Rule: --env is needed only when the repo holds several envs, and must name one of them."""
    assert cli.choose_env(_instance(tmp_path / "one", "home"), None) == "home"
    two = _instance(tmp_path / "two", "home", "lab")
    assert cli.choose_env(two, "lab") == "lab"
    for name, message in ((None, "this repo holds several envs; pass --env (home, lab)"),
                          ("nope", f"no env 'nope' in {two / 'envs'} (envs: home, lab)")):  # fmt: skip
        with pytest.raises(cli.UsageError) as raised:
            cli.choose_env(two, name)
        assert str(raised.value) == message


def test_usage_errors_exit_2_and_help_needs_no_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: exit 2 for a usage error; jarvis --help, and a command's help as jarvis <command> --help
    or jarvis help <command>, work anywhere."""
    monkeypatch.chdir(tmp_path)
    usage = cli.__doc__ + "\n"
    assert "Exit codes" in usage
    # Each word is in the usage too, so each form must print the command's own help, and both the same.
    for command, words in (("restore", "--as-of"), ("site", "--tags"), ("up", "doctor, validate")):
        assert cli.main([command, "--help"]) == 0
        own = capsys.readouterr()
        assert cli.main(["help", command]) == 0
        assert capsys.readouterr() == own and words in own.out and own.out != usage and own.err == "", own
    # As a shell runs the installed script (own is up's help): main() gets the words typed, no more, and exits with its code.
    outside = "jarvis: run jarvis from inside an instance repo (a git repository that holds envs/)\n"
    for argv, code, out, err in (([], 2, usage, ""), (["--help"], 0, usage, ""), (["up", "--help"], 0, own.out, ""),
                                 (["help", "up"], 0, own.out, ""), (["--env", "lab", "verify"], 2, "", outside),
                                 (["bogus", "--env", "lab"], 2, "", "jarvis: unknown command 'bogus'; see jarvis --help\n")):  # fmt: skip
        shell = subprocess.run([str(Path(sys.executable).parent / "jarvis"), *argv], cwd=tmp_path, capture_output=True, text=True)
        assert (shell.returncode, shell.stdout, shell.stderr) == (code, out, err), argv


def test_every_command_names_a_module_that_has_its_entry(tmp_path: Path) -> None:
    """Rule: each command's module exists and has the entry the CLI calls."""
    for name, command in cli.COMMANDS.items():
        module = importlib.import_module(command.module)
        assert hasattr(module, "main" if command.takes_options else "run"), name


@pytest.mark.usefixtures("cli_env")
def test_core_runs_this_checkout_unpinned_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: --core <this checkout> runs it for an env pinned elsewhere, with a warning."""
    instance = _repo(copy_sample(tmp_path / "instance"))
    monkeypatch.chdir(instance / "envs")
    monkeypatch.setenv("APPS_REPO", os.environ["APPS_REPO"])
    assert cli.main(["--core", str(REPO_ROOT), "render", "--check"]) == 0
    assert "WARN jarvis: unpinned run with --core: env-00 pins core v0.2.0" in capsys.readouterr().err
    assert os.environ["INSTANCE"] == str(instance)


@pytest.mark.usefixtures("cli_env")
def test_site_validates_an_env_with_hosted_apps_from_its_apps_repository(tmp_path: Path, monkeypatch, capsys) -> None:
    """Rule: every command whose run reads app files gets the apps checkout, with no APPS_REPO set:
    jarvis site validates an env that lists apps, at their pinned tags, then runs the playbook."""
    values = copy_sample(instance := tmp_path / "instance", "app-hosting") / "envs" / "env-00" / "values"
    remote = sample.apps_repo(tmp_path / "remote")
    # The env's https URL, served from the local repository.
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    monkeypatch.setenv(f"GIT_CONFIG_KEY_{count}", f"url.file://{remote}.insteadOf")
    monkeypatch.setenv(f"GIT_CONFIG_VALUE_{count}", yaml.safe_load((values / "cluster-vars.yaml").read_text())["apps_git_repo_url"])
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(count + 1))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("APPS_REPO", raising=False)
    monkeypatch.setattr(checkout, "_fetch_for", None)
    monkeypatch.setattr(site, "shutil", types.SimpleNamespace(which=lambda name: name))
    monkeypatch.setattr(site, "playbook_command", lambda *a, **k: ["true"])
    monkeypatch.chdir(_repo(instance))
    assert cli.main(["--core", str(REPO_ROOT), "site"]) == 0, capsys.readouterr()


def test_validate_checks_only_the_named_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: jarvis validate limits the schema to its env, so each env is checked by the core it pins."""
    seen: list[str | None] = []
    monkeypatch.setattr(validate, "run", lambda instance: seen.append(os.environ.get("ENVS")) or 0)
    monkeypatch.setenv("ENVS", "")  # restored after the test, as the CLI sets it in place
    assert cli.COMMANDS["validate"].run(tmp_path, "lab", []) == 0
    assert seen == ["lab"]
    with pytest.raises(cli.UsageError) as raised:
        cli.COMMANDS["validate"].run(tmp_path, "lab", ["--extra"])
    assert str(raised.value) == "this command takes no arguments: --extra"


def test_doctor_fails_an_age_key_others_can_read(tmp_path: Path) -> None:
    """Rule: the age key must be yours and mode 0600; a missing one says where it goes."""
    env_dir = tmp_path / "envs" / "home"
    (env_dir / "values").mkdir(parents=True)
    key = tmp_path / "keys.txt"
    (env_dir / "values" / "cluster-vars.yaml").write_text(f"age_key_file: {key}\n", encoding="utf-8")
    assert doctor.check_age_key(env_dir) == [("FAIL", f"age key {key} is missing; copy it from where you keep it (mode 0600)")]
    key.write_text("AGE-SECRET-KEY-1\n", encoding="utf-8")
    key.chmod(0o644)
    assert doctor.check_age_key(env_dir) == [("FAIL", f"age key {key} is mode 0644, not 0600 (chmod 600 {key})")]
    key.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert doctor.check_age_key(env_dir) == [("PASS", f"age key {key} is yours and mode 0600")]


def test_doctor_names_a_host_that_does_not_resolve(tmp_path: Path) -> None:
    """Rule: doctor checks that every inventory host resolves, and names each one that does not."""
    env_dir = tmp_path / "envs" / "home"
    (env_dir / "inventory").mkdir(parents=True)
    (env_dir / "inventory" / "hosts.yml").write_text(
        "all:\n  hosts:\n    localhost: {}\n    nothing.invalid: {}\n", encoding="utf-8"
    )
    assert doctor.check_hosts(env_dir) == [
        ("PASS", "localhost resolves"),
        ("FAIL", "nothing.invalid does not resolve; add it on the router or in your DNS"),
    ]


@pytest.mark.parametrize("worktree", [pytest.param(False, id="clone"), pytest.param(True, id="worktree")])
def test_doctor_installs_the_hook_where_git_runs_it_and_keeps_anyone_elses(tmp_path: Path, worktree: bool) -> None:
    """Rule: doctor installs the pre-commit hook a fresh clone or worktree lacks where git runs it (a
    worktree's .git is a file and its hooks are the main repository's), and keeps anyone else's."""
    instance = main = _repo(tmp_path / "main")
    if worktree:
        git(main, "commit", "-q", "--allow-empty", "-m", "one")
        git(main, "worktree", "add", "-q", "--detach", str(instance := tmp_path / "wt"))
    hook = main / ".git" / "hooks" / "pre-commit"
    assert doctor.check_hook(instance) == [("PASS", f"installed the pre-commit hook at {hook} (a fresh clone or worktree has none)")]
    assert doctor.check_hook(instance) == [("PASS", "the pre-commit hook runs jarvis validate")]
    hook.write_text("#!/bin/sh\nmy own\n", encoding="utf-8")
    assert doctor.check_hook(instance) == [("WARN", f"{hook} is not jarvis's; commits are not checked before they are made")]
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nmy own\n"


def _pushed_instance(tmp_path: Path) -> Path:
    """An instance clone whose env is committed and pushed to its origin's main."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    values = clone / "envs" / "home" / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text(f"flux_git_repo_url: {origin}\nflux_git_ref: refs/heads/main\n", encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "-m", "home"), ("push", "-q", "origin", "HEAD:main")):
        git(clone, *args)
    return clone


def test_up_refuses_until_flux_reads_what_the_checkout_holds(tmp_path: Path) -> None:
    """Rule: jarvis up stops before site while the env has uncommitted changes or unpushed commits."""
    clone = _pushed_instance(tmp_path)
    assert up.unpushed(clone, "home") is None
    secret = clone / "envs" / "home" / "secrets" / "k3s-token.sops.yaml"
    secret.parent.mkdir()
    secret.write_text("sealed\n", encoding="utf-8")
    assert up.unpushed(clone, "home") == "envs/home or .sops.yaml has uncommitted changes; commit and push them first"
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "secrets")
    origin = tmp_path / "origin.git"
    assert up.unpushed(clone, "home") == f"this checkout has commits that refs/heads/main on {origin} lacks; push them first"


def test_up_stops_at_the_first_failing_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: each step must pass before the next; the first failure's exit code is up's. Validate
    checks up's env only, as jarvis validate does, so each env is checked by the core it pins."""
    ran: list[str] = []

    def step(name: str, code: int = 0):
        return lambda *args, **kwargs: ran.append(name) or code

    monkeypatch.setenv("ENVS", "")  # restored after the test, as up sets it in place
    monkeypatch.setattr(up.doctor, "run", step("doctor"))
    monkeypatch.setattr(up.validate, "run", lambda instance: ran.append(f"validate {os.environ['ENVS']}") or 0)
    monkeypatch.setattr(up.mint, "main", step("secrets"))
    monkeypatch.setattr(up, "_refuse_unpushed", step("pushed", 3))
    monkeypatch.setattr(up.site, "main", step("site"))
    assert up.run(tmp_path, "home") == 3
    assert ran == ["doctor", "validate home", "secrets", "pushed"]
    banners = "".join(f"== up: {name}\n" for name in ("doctor", "validate", "secrets", "pushed"))
    assert capsys.readouterr().out == banners + "FAIL up: stopped at pushed (exit 3)\n"


_SHA = "0123456789abcdef0123456789abcdef01234567"


def _listing(*items: tuple[str, bool, int, str], archived: str = "2026-11-19T08:00:00Z"):
    """up's two kubectl lists for one poll: Kustomizations (name, Ready, observed generation,
    applied revision), each at generation 2 and last reconciled at 08:30 from the root source; and
    that source, whose artifact is at the newest applied revision and was archived at archived."""

    def revision(sha: str) -> str:
        return f"refs/heads/main@sha1:{sha}"

    body = [
        {"kind": "Kustomization", "metadata": {"name": name, "namespace": "flux-system", "generation": 2},
         "spec": {"interval": "10m", "sourceRef": {"kind": "GitRepository", "name": "flux-system"}},
         "status": {"observedGeneration": observed, "lastAppliedRevision": revision(sha),
                    "history": [{"lastReconciled": "2026-11-19T08:30:00Z"}],
                    "conditions": [{"type": "Ready", "status": str(ok), "message": "working"}]}}
        for name, ok, observed, sha in items
    ]  # fmt: skip
    newest = revision(_SHA if any(sha == _SHA for *_, sha in items) else items[-1][3])
    source = {"kind": "GitRepository", "metadata": {"name": "flux-system", "namespace": "flux-system"},
              "status": {"artifact": {"revision": newest, "lastUpdateTime": archived}}}  # fmt: skip
    return [subprocess.CompletedProcess([], 0, json.dumps({"items": listed}), "") for listed in (body, [source])]


def _polls(*listings, calls: list[str] | None = None):
    """A kubectl for up that answers each poll from the next listing, the last one from then on."""
    queue = list(listings)

    def kubectl(args, **_kwargs):
        if calls is not None:
            calls.append(args[1])
        kustomizations, sources = queue[0]
        if args[1].startswith("kustomizations"):
            return kustomizations
        if len(queue) > 1:
            queue.pop(0)
        return sources

    return kubectl


def test_up_waits_for_everything_the_env_tree_applies() -> None:
    """Rule: up waits for what Flux's build of the env says the cluster gets: the root, core's
    layers, the add-ons and every app's wiring and own Kustomizations, not a list of its own."""
    expected, error = up.expected_kustomizations(sample_root("app-hosting"), "env-00")
    assert error is None, error
    assert {"flux-system", "platform-controllers", "platform-configs", "services", "app-notes-wiring"} <= set(expected)
    # A hosted app's own Kustomizations read the apps repository, so they are not held to the env's commit.
    assert expected["app-notes"] is False and expected["services"] is True


def test_up_is_done_only_when_each_is_ready_for_its_spec_at_the_pushed_commit(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: a Ready root is not a Ready cluster: each expected Kustomization must exist, be Ready for its
    generation and, if it reads the root source, have applied the pushed commit (an older Ready is not)."""
    expected = {"flux-system": True, "services": True, "app-x": False}
    monkeypatch.setattr(up, "expected_kustomizations", lambda instance, env: (expected, None))
    monkeypatch.setattr(up, "pushed_revision", lambda instance, env: _SHA)
    old = "f" * 40
    current = (("flux-system", True, 2, _SHA), ("services", True, 2, _SHA), ("app-x", True, 2, _SHA))
    calls: list[str] = []
    polls = _polls(
        _listing(("flux-system", True, 2, old)),
        _listing(("flux-system", True, 2, _SHA), ("services", True, 1, _SHA), ("app-x", True, 2, _SHA)),
        _listing(("flux-system", True, 2, _SHA), ("services", False, 2, _SHA), ("app-x", True, 2, _SHA)),
        # A new core tag: the root source's new artifact keeps its revision, so each still reads Ready.
        _listing(*current, archived="2026-11-19T08:45:00Z"),
        _listing(*current),
        calls=calls,
    )
    monkeypatch.setattr(up, "kubectl", polls)
    monkeypatch.setattr(up, "_sleep", lambda seconds: None)
    assert up.wait_for_flux(Path("."), "home") == 0
    assert len(calls) == 10
    assert capsys.readouterr().out == "PASS up: every Flux Kustomization is Ready at 0123456789ab (3)\n"


def test_up_names_what_is_not_ready_when_the_wait_runs_out(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: when the wait runs out, up fails and names each Kustomization still waited for, with why."""
    monkeypatch.setattr(up, "expected_kustomizations", lambda instance, env: ({"flux-system": True, "services": True}, None))
    monkeypatch.setattr(up, "pushed_revision", lambda instance, env: _SHA)
    now = [0.0]
    monkeypatch.setattr(up, "kubectl", _polls(_listing(("flux-system", True, 2, "f" * 40))))
    monkeypatch.setattr(up, "_clock", lambda: now[0])
    monkeypatch.setattr(up, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert up.wait_for_flux(Path("."), "home") == 1
    assert capsys.readouterr().out == (
        "FAIL up: Flux is not Ready after 20 minutes: flux-system: not yet at 0123456789ab; services: not created yet\n"
    )


def test_up_stops_when_the_tree_does_not_build(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: an env tree Flux cannot build fails up before it waits, with the build's error."""
    monkeypatch.setattr(up, "expected_kustomizations", lambda instance, env: ({}, "flux build services failed: boom"))
    monkeypatch.setattr(up, "pushed_revision", lambda instance, env: _SHA)
    assert up.wait_for_flux(Path("."), "home") == 1
    assert capsys.readouterr().out == "FAIL up: flux build services failed: boom\n"


def test_the_entry_point_loads_nothing_that_reads_core_trees() -> None:
    """Rule: an installed jarvis holds no core trees until it hands off to the pinned checkout, so
    its entry point imports only what choosing that checkout needs (no schema, no manifests)."""
    probe = "import json, sys, jarvis.cli.main; print(json.dumps(sorted(m for m in sys.modules if m.startswith('jarvis.'))))"
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert json.loads(loaded) == ["jarvis.cli", "jarvis.cli.core", "jarvis.cli.main", "jarvis.env", "jarvis.env.pin", "jarvis.paths"]


def test_version_names_this_release_and_each_envs_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: --version names this CLI's release and, inside an instance repo, the release each env pins."""
    monkeypatch.setattr(cli._core, "own_release", lambda: ("https://github.com/example/jarvis", "v9.9.9"))
    monkeypatch.chdir(_repo(tmp_path))
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == "jarvis v9.9.9 (https://github.com/example/jarvis)\n"
    _env(tmp_path, "home", "refs/tags/v9.9.8")
    _env(tmp_path, "lab", "refs/heads/main")
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"jarvis v9.9.9 (https://github.com/example/jarvis)\nhome pins v9.9.8\nlab: lab {_BRANCH_PIN}\n"


def test_help_in_an_instance_is_the_pinned_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handed) -> None:
    """Rule: a command's options are its env's release's; an installed CLI, which holds no core
    trees, hands --help off to that release like the command itself, with the env it chose."""
    for env in ("home", "lab"):
        _env(tmp_path, env, "refs/tags/v9.9.9", "https://github.com/example/jarvis")
    monkeypatch.chdir(_repo(tmp_path))
    monkeypatch.setattr(cli_core, "checkout", lambda env_dir: tmp_path / "cached")
    with pytest.raises(SystemExit):
        cli.main(["validate", "--help"])
    assert handed == [(tmp_path / "cached", ["--env", "home", "validate", "--help"])]


def test_help_outside_an_instance_says_where_to_ask_when_this_cli_cannot_load_it(tmp_path: Path, monkeypatch, capsys) -> None:
    """Rule: an installed CLI without core trees answers --help outside an instance repo with where
    to run it, not a traceback."""
    monkeypatch.chdir(tmp_path)

    def missing(name: str):
        raise FileNotFoundError(f"{name}: schema/cluster-vars.schema.json")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["validate", "--help"]) == 0
    assert capsys.readouterr().out == (
        "jarvis validate: run jarvis validate --help from inside an instance repo; a command's options are"
        " those of the core release its env pins\n"
    )


@pytest.mark.parametrize(
    ("ref", "edit", "message"),
    [
        pytest.param("refs/tags/v1.0.0", None, None, id="the-tag"),
        pytest.param("refs/tags/v1.0.0", "untracked", None, id="untracked-file"),
        pytest.param("refs/tags/v1.0.0", "commit", "env-x pins core v1.0.0 ({tagged}), but this checkout is at {head}",
                     id="another-commit"),
        pytest.param("refs/tags/v1.0.0", "edit", "this checkout has uncommitted changes to tracked files, so it is not v1.0.0",
                     id="uncommitted"),
        pytest.param("refs/heads/main", None, f"env-x {_BRANCH_PIN}", id="branch"),
        pytest.param("refs/tags/v9.9.9", None, "env-x pins core v9.9.9, which this checkout does not have", id="missing-tag"),
    ],
)  # fmt: skip
def test_only_a_clean_checkout_at_the_envs_tag_is_its_pin(tmp_path: Path, core: Path, ref: str, edit: str | None,
                                                          message: str | None) -> None:  # fmt: skip
    """Rule: a clean checkout at the env's tag is its pin, annotated or with untracked files; another
    commit, a changed tracked file, a branch (even at its tip) or a tag the checkout lacks is not."""
    if edit:
        (core / ("new" if edit == "untracked" else "file")).write_text("two\n", encoding="utf-8")
    if edit == "commit":
        git(core, "commit", "-q", "-am", "two")
    expected = message and message.format(tagged=git(core, "rev-parse", "v1.0.0^{commit}")[:12], head=git(core, "rev-parse", "HEAD")[:12])
    assert pin_problem(core, _env(tmp_path / "instance", "env-x", ref)) == expected


def test_an_env_pinned_elsewhere_runs_from_a_cached_checkout_of_its_tag(
    tmp_path: Path, core: Path, monkeypatch: pytest.MonkeyPatch, handed
) -> None:
    """Rule: when this checkout is not the env's pin, the command runs from a checkout of the
    pinned tag in the cache, cloned once and set up from its own lockfiles, with the same argv."""
    _env(instance := _repo(tmp_path / "instance"), "env-x", "refs/tags/v1.0.0", f"file://{core}")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(instance)
    set_up: list[Path] = []
    monkeypatch.setattr(cli_core, "setup", lambda checkout: set_up.append(checkout) or (checkout / ".jarvis-ready").touch())
    for _ in range(2):
        with pytest.raises(SystemExit):
            cli.main(["render", "--check"])
    cached = tmp_path / "cache" / "jarvis" / "core" / "v1.0.0"
    assert (cached / "file").read_text(encoding="utf-8") == "one\n"
    assert set_up == [cached]
    assert handed == [(cached, ["render", "--check"])] * 2


def test_a_tag_with_slashes_has_its_own_cached_checkout(tmp_path: Path, core: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: a tag may hold slashes (release/1.0): its cached checkout nests, a second tag gets its own,
    and each is the tag even beside a branch of its name (an instance repository pushed under it)."""
    for args in (("tag", "release/1.0"), ("tag", "release/1.1"), ("checkout", "-q", "-b", "release/1.0")):
        git(core, *args)
    (core / "file").write_text("branch\n", encoding="utf-8")
    git(core, "commit", "-qam", "branch")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(cli_core, "setup", lambda checkout: (checkout / ".jarvis-ready").touch())
    first = cli_core.checkout_tag(f"file://{core}", "release/1.0")
    second = cli_core.checkout_tag(f"file://{core}", "release/1.1")
    assert first == tmp_path / "cache" / "jarvis" / "core" / "release" / "1.0" and second.name == "1.1"
    assert (first / "file").read_text(encoding="utf-8") == (second / "file").read_text(encoding="utf-8") == "one\n"
    assert sorted(path.name for path in first.parent.iterdir()) == ["1.0", "1.1"]
    # at_tag, which decides whether init runs here: the checkout is the tag, the branch's tip is not.
    assert cli_core.at_tag(first, "release/1.0") and not cli_core.at_tag(core, "release/1.0")
    (first / "file").write_text("edited\n", encoding="utf-8")
    assert not cli_core.at_tag(first, "release/1.0")


def test_a_branch_pin_is_refused_before_anything_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, handed) -> None:
    """Rule: an env that follows a branch is refused (exit 3): there is no release to run."""
    _env(instance := _repo(tmp_path / "instance"), "env-x", "refs/heads/main")
    monkeypatch.chdir(instance)
    assert cli.main(["render"]) == 3
    assert capsys.readouterr() == ("", f"REFUSE jarvis: env-x {_BRANCH_PIN}\n")
    assert handed == []


def test_release_notes_are_the_tags_changelog_section() -> None:
    """Rule: a release's notes are its CHANGELOG.md section; a tag with none fails the release."""
    notes = subprocess.run([sys.executable, str(REPO_ROOT / "ci" / "release-notes"), "v0.2.0"],
                           capture_output=True, text=True, check=False)
    assert notes.returncode == 0 and "### Upgrading" in notes.stdout and "## " not in notes.stdout.replace("### ", "")
    missing = subprocess.run([sys.executable, str(REPO_ROOT / "ci" / "release-notes"), "v0.0.0-none"],
                             capture_output=True, text=True, check=False)
    assert (missing.returncode, missing.stderr) == (1, "CHANGELOG.md has no '## v0.0.0-none' section; add it before tagging\n")
