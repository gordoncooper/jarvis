"""jarvis init and jarvis env add: a new instance repo, and a new env in one, valid as written."""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from jarvis.cli import core as cli_core
from jarvis.cli import init
from jarvis.env import scaffold, validate
from jarvis.secrets import age
from support import REPO_ROOT, checks_without_charts, git

CORE = ("https://github.com/example-owner/example-core", "v0.5.0")


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A controller home with no age key yet; init's environment changes are undone after."""
    home = tmp_path / "home"
    home.mkdir()
    # The download cache stays the real one, so the chart renders reuse it.
    monkeypatch.setenv("XDG_CACHE_HOME", os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    for name in ("INSTANCE", "ENVS"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.chdir(tmp_path)
    return home


def _init(tmp_path: Path, *extra: str) -> tuple[int, Path]:
    dest = tmp_path / "lab"
    code = init.run_init(
        [str(dest), "--env", "home", "--domain", "home.lan", "--repo-url", "ssh://git@github.com/me/lab",
         "--node", "node1.home.lan", "--ssh-user", "ops", *extra],
        CORE,
    )  # fmt: skip
    return code, dest


def _validate(instance: Path, env: str, *, charts: bool = True) -> int:
    """jarvis validate's checks of one env, in this process (the controller fixture restores ENVS)."""
    os.environ["ENVS"] = env
    return validate.run(instance, None if charts else checks_without_charts())


def test_init_writes_an_instance_that_passes_every_check(tmp_path: Path, controller: Path, capsys) -> None:
    """Rule: the env passes jarvis validate as soon as init returns; nothing is left to hand-write.
    The devcontainer is always written, the agent kit only with --with-agents."""
    code, dest = _init(tmp_path)
    out = capsys.readouterr().out
    assert code == 0 and "jarvis node prep node1.home.lan" in out and "ssh-ed25519 " in out
    git(dest, "add", "-A")
    git(dest, "commit", "--no-verify", "-qm", "home")
    assert _validate(dest, "home") == 0, capsys.readouterr().out
    for path in (".sops.yaml", "README.md", "renovate.json", ".github/workflows/ci.yml", "envs/home/ca.crt",
                 "envs/home/secrets/k3s-token.sops.yaml", "envs/home/secrets/flux-deploy-key.sops.yaml",
                 "envs/home/flux/sync.yaml", ".git/hooks/pre-commit", ".devcontainer/devcontainer.json"):  # fmt: skip
        assert (dest / path).is_file(), path
    assert os.access(dest / ".devcontainer/setup.sh", os.X_OK)
    assert [name for name in ("AGENTS.md", "CLAUDE.md", ".claude", ".cursor") if (dest / name).exists()] == []


def test_secrets_open_with_the_working_key_and_the_recovery_key(tmp_path: Path, controller: Path, capsys) -> None:
    """Rule: every secret is encrypted to the working key and the recovery key. The recovery key's
    private half is printed once and written nowhere."""
    code, dest = _init(tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    secret = next(line.strip() for line in out.splitlines() if line.strip().startswith("AGE-SECRET-KEY-"))
    working = age.public_key(controller / ".config" / "sops" / "age" / "keys.txt")
    keys = age.recipients(dest / ".sops.yaml", "home")
    assert keys[0] == working and len(keys) == 2
    on_disk = [path for path in dest.rglob("*") if path.is_file() and secret in path.read_text(errors="ignore")]
    assert on_disk == []
    recovery = tmp_path / "recovery.txt"
    recovery.write_text(secret + "\n", encoding="utf-8")
    recovery.chmod(0o600)
    for key_file in (controller / ".config" / "sops" / "age" / "keys.txt", recovery):
        opened = subprocess.run(
            ["sops", "decrypt", str(dest / "envs" / "home" / "secrets" / "k3s-token.sops.yaml")],
            env={**os.environ, "SOPS_AGE_KEY_FILE": str(key_file)}, capture_output=True, text=True,
        )  # fmt: skip
        assert opened.returncode == 0, (key_file, opened.stderr)


def test_init_refuses_a_folder_that_is_not_empty(tmp_path: Path, controller: Path, capsys) -> None:
    """Rule: init makes a new instance repo only in an empty folder, and touches nothing else."""
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "notes.txt").write_text("mine\n", encoding="utf-8")
    code, dest = _init(tmp_path)
    assert (code, capsys.readouterr().out) == (3, f"REFUSE init: {dest} is not empty\n")
    assert sorted(path.name for path in dest.iterdir()) == ["notes.txt"]


def test_init_writes_nothing_when_a_tool_is_missing(tmp_path: Path, controller: Path, monkeypatch, capsys) -> None:
    """Rule: the tools are checked before the first write, so a missing one leaves nothing half made."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    code, dest = _init(tmp_path)
    assert (code, capsys.readouterr().out) == (4, "FAIL init: not on PATH: git, age-keygen, sops, ssh-keygen (run jarvis doctor)\n")
    assert not dest.exists()
    assert not (controller / ".config").exists()


def test_a_given_recovery_recipient_is_used_and_nothing_is_printed(tmp_path: Path, controller: Path, capsys) -> None:
    """Rule: --recovery-recipient seals to that key and prints no private half; --no-ci writes no CI files."""
    _secret, public = age.new_key_in_memory()
    code, dest = _init(tmp_path, "--recovery-recipient", public, "--no-ci")
    assert code == 0
    assert age.recipients(dest / ".sops.yaml", "home")[1] == public
    assert "AGE-SECRET-KEY-" not in capsys.readouterr().out
    assert not (dest / "renovate.json").exists() and not (dest / ".github").exists()


def test_env_add_reuses_the_recipients_and_passes_the_env_checks(tmp_path: Path, controller: Path, capsys) -> None:
    """Rule: env add seals to the repo's recipients, reads Flux's source from the first env, passes the
    env checks as written, and refuses a name the repo already holds."""
    code, dest = _init(tmp_path)
    assert code == 0
    with pytest.raises(scaffold.ScaffoldError) as raised:
        init.run_env_add(dest, ["home", "--domain", "home.lan", "--node", "n1.home.lan"], CORE)
    assert str(raised.value) == "envs/home already exists"
    assert init.run_env_add(dest, ["lab", "--domain", "lab.lan", "--node", "n1.lab.lan"], CORE) == 0
    assert age.recipients(dest / ".sops.yaml", "lab") == age.recipients(dest / ".sops.yaml", "home")
    values = yaml.safe_load((dest / "envs" / "lab" / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    assert values["flux_git_repo_url"] == "ssh://git@github.com/me/lab" and values["flux_git_ref"] == "refs/heads/main"
    git(dest, "add", "-A")
    git(dest, "commit", "--no-verify", "-qm", "two")
    # The second env renders the same charts as the first, which the test above checks.
    assert _validate(dest, "lab", charts=False) == 0, capsys.readouterr().out


_ROLES = "(roles: control_plane, workers, backup, edge, gpu)"


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        pytest.param(["cp:control_plane", "wk:workers,edge"], "exactly one node needs the backup role, found 0", id="no-backup"),
        pytest.param(["a:control_plane,backup,edge,workers", "b:control_plane"],
                     "exactly one node needs the control_plane role, found 2", id="two-control-planes"),
        pytest.param(["a:control_plane,backup,workers"], "no node holds the edge role", id="no-edge"),
        pytest.param(["a", "b:workers"], f"a: give its roles, <name>:<role>,<role> {_ROLES}", id="no-roles-beside-another"),
        pytest.param(["a:master"], f"a: unknown role master {_ROLES}", id="unknown-role"),
    ],
)  # fmt: skip
def test_the_nodes_must_make_a_whole_cluster(nodes: list[str], message: str) -> None:
    """Rule: init refuses nodes that do not make the schema's whole cluster, naming what is missing."""
    with pytest.raises(scaffold.ScaffoldError) as raised:
        spec = scaffold.EnvSpec(
            name="home", domain="home.lan", repo_url="https://github.com/me/lab", core_url=CORE[0], core_tag=CORE[1],
            nodes=[scaffold.Node.parse(text, only=len(nodes) == 1) for text in nodes],
        )  # fmt: skip
        spec.check()
    assert str(raised.value) == message


def test_a_lone_node_gets_every_role_but_gpu() -> None:
    """Rule: a lone node given no roles holds the whole cluster; gpu is never assumed."""
    assert scaffold.Node.parse("n1", only=True).roles == ("control_plane", "workers", "backup", "edge")


def test_a_key_path_sops_would_not_read_is_refused_before_anything_is_written(
    tmp_path: Path, controller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: the env records the working key SOPS_AGE_KEY_FILE names, or nothing is made: with the
    two naming different files, init would seal to one and record the other."""
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(tmp_path / "other.txt"))
    with pytest.raises(scaffold.ScaffoldError) as raised:
        _init(tmp_path, "--age-key-file", str(tmp_path / "recorded.txt"))
    assert str(raised.value) == (
        f"SOPS_AGE_KEY_FILE ({tmp_path / 'other.txt'}) is not the age_key_file the env would record "
        f"({tmp_path / 'recorded.txt'}); unset it, or pass the same path as --age-key-file"
    )
    assert not any(path.exists() for path in (tmp_path / "lab", tmp_path / "other.txt", tmp_path / "recorded.txt"))


def test_the_installed_cli_knows_its_own_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: init pins the release the CLI came from, by the install record uv tool install writes
    (PEP 610)."""

    class Dist:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"url": "https://github.com/o/jarvis.git", "vcs_info": {"vcs": "git", "requested_revision": "v9.1.0"}})

    monkeypatch.setattr(cli_core, "distribution", lambda name: Dist())
    assert cli_core.own_release() == ("https://github.com/o/jarvis", "v9.1.0")


def test_with_examples_turns_on_the_example_apps_and_passes_the_env_checks(tmp_path: Path, controller: Path, capsys) -> None:
    """Rule: --with-examples turns on app-hosting and the example apps, reads them from the
    repository the examples name, and the env still passes validate as written."""
    code, dest = _init(tmp_path, "--with-examples")
    assert code == 0
    switches = yaml.safe_load((dest / "envs/home/values/switches.yaml").read_text())
    assert "app-hosting" in switches["features"]
    apps = yaml.safe_load((dest / "envs/home/values/apps.yaml").read_text())
    assert apps == scaffold.example_apps()
    cluster_vars = yaml.safe_load((dest / "envs/home/values/cluster-vars.yaml").read_text())
    assert cluster_vars["apps_git_repo_url"] == scaffold.example_apps_repository()
    assert (dest / "envs/home/flux/generated/apps.yaml").is_file()
    # The sample instance turns on the same apps and renders their charts (ci/checks/sample-instance).
    assert _validate(dest, "home", charts=False) == 0, capsys.readouterr().out


def test_the_example_apps_need_app_hosting_and_an_apps_repository() -> None:
    """Rule: --with-examples is refused for an env without app hosting and an apps repository."""
    spec = scaffold.EnvSpec("home", "home.lan", "https://github.com/me/lab", *CORE, [scaffold.Node.parse("n1", only=True)], examples=True)
    with pytest.raises(scaffold.ScaffoldError) as raised:
        spec.check()
    assert str(raised.value) == "the example apps need the app-hosting feature and an apps repository"


def test_the_agent_kit_is_opt_in(tmp_path: Path, controller: Path) -> None:
    """Rule: --with-agents writes the agent kit with core's own deny lists (without it, none: see
    test_init_writes_an_instance_that_passes_every_check); an age key already on the controller is used."""
    working = age.create_key(controller / ".config" / "sops" / "age" / "keys.txt")
    code, dest = _init(tmp_path / "new", "--with-agents")
    assert code == 0 and age.recipients(dest / ".sops.yaml", "home")[0] == working
    assert (dest / "CLAUDE.md").read_text() == "@AGENTS.md\n" and (dest / "AGENTS.md").is_file()
    assert (dest / ".claude/settings.json").read_bytes() == (REPO_ROOT / ".claude/settings.json").read_bytes()
    assert (dest / ".cursor/cli.json").read_bytes() == (REPO_ROOT / ".cursor/cli.json").read_bytes()
    assert json.loads((dest / ".claude/settings.json").read_text())["permissions"]["deny"]


def test_the_package_version_is_the_release_tag(tmp_path: Path) -> None:
    """Rule: a wheel built at v1.2.3 is 1.2.3 (hatch-vcs; nothing is bumped by hand), a throwaway tag is
    no version, and the wheel holds everything init copies (init runs from the installed CLI)."""
    for rel in filter(None, git(REPO_ROOT, "ls-files", "-z").split("\0")):
        source = REPO_ROOT / rel
        if source.is_file() and not source.is_symlink():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_bytes(source.read_bytes())

    def build(out: str) -> Path:
        subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(tmp_path / out)], cwd=tmp_path, check=True, capture_output=True)
        return next((tmp_path / out).glob("*.whl"))

    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "one"), ("tag", "-a", "v1.2.3", "-m", "v1.2.3")):
        git(tmp_path, *args)
    wheel = build("dist")
    names = set(zipfile.ZipFile(wheel).namelist())
    assert wheel.name.startswith("jarvis-1.2.3-")
    metadata = next(name for name in names if name.endswith("METADATA"))
    assert "Version: 1.2.3" in zipfile.ZipFile(wheel).read(metadata).decode()
    kit = {path.relative_to(REPO_ROOT).as_posix() for path in scaffold.KIT.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    assert kit and kit <= names, sorted(kit - names)
    # The quickstart gate tags the commit under test, past the release: that build is the next release's dev.
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "two")
    git(tmp_path, "tag", "quickstart/1-1")
    assert build("next").name.startswith("jarvis-1.2.4.dev1+g")
