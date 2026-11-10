"""jarvis init and jarvis env add: a new instance repo, and a new env in one, valid as written."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from jarvis.cli import core as cli_core
from jarvis.cli import init
from jarvis.env import scaffold
from jarvis.secrets import age
from support import REPO_ROOT

FIXTURE_APPS = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"
CORE = ("https://github.com/example-owner/example-core", "v0.5.0")


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A controller home with no age key yet; init's environment changes are undone after."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    monkeypatch.setenv("INSTANCE", os.environ.get("INSTANCE", ""))
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


def _validate(instance: Path, env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "jarvis.env.validate"],
        cwd=REPO_ROOT,
        env={**os.environ, "INSTANCE": str(instance), "ENVS": env, "APPS_REPO": str(FIXTURE_APPS)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_init_writes_an_instance_that_passes_every_check(tmp_path: Path, controller: Path, capsys) -> None:
    """Rule: the env passes jarvis validate as soon as init returns; nothing is left to hand-write."""
    code, dest = _init(tmp_path)
    assert code == 0
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(dest), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--no-verify", "-qm", "home"], check=True)
    done = _validate(dest, "home")
    assert done.returncode == 0, done.stdout + done.stderr
    for path in (".sops.yaml", "README.md", "renovate.json", ".github/workflows/ci.yml", "envs/home/ca.crt",
                 "envs/home/secrets/k3s-token.sops.yaml", "envs/home/secrets/flux-deploy-key.sops.yaml",
                 "envs/home/flux/sync.yaml", ".git/hooks/pre-commit"):  # fmt: skip
        assert (dest / path).is_file(), path
    out = capsys.readouterr().out
    assert "jarvis node prep node1.home.lan" in out and "ssh-ed25519 " in out


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


def test_init_refuses_a_folder_that_is_not_empty(tmp_path: Path, controller: Path) -> None:
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "notes.txt").write_text("mine\n", encoding="utf-8")
    code, dest = _init(tmp_path)
    assert code == 3
    assert sorted(path.name for path in dest.iterdir()) == ["notes.txt"]


def test_init_writes_nothing_when_a_tool_is_missing(tmp_path: Path, controller: Path, monkeypatch) -> None:
    """Rule: the tools are checked before the first write, so a missing one leaves nothing half made."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    code, dest = _init(tmp_path)
    assert code == 4
    assert not dest.exists()
    assert not (controller / ".config").exists()


def test_a_given_recovery_recipient_is_used_and_nothing_is_printed(tmp_path: Path, controller: Path, capsys) -> None:
    _secret, public = age.new_key_in_memory()
    code, dest = _init(tmp_path, "--recovery-recipient", public, "--no-ci")
    assert code == 0
    assert age.recipients(dest / ".sops.yaml", "home")[1] == public
    assert "AGE-SECRET-KEY-" not in capsys.readouterr().out
    assert not (dest / "renovate.json").exists() and not (dest / ".github").exists()


def test_env_add_reuses_the_recipients_and_passes_every_check(tmp_path: Path, controller: Path) -> None:
    code, dest = _init(tmp_path)
    assert code == 0
    assert init.run_env_add(dest, ["lab", "--domain", "lab.lan", "--node", "n1.lab.lan"], CORE) == 0
    assert age.recipients(dest / ".sops.yaml", "lab") == age.recipients(dest / ".sops.yaml", "home")
    values = yaml.safe_load((dest / "envs" / "lab" / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    assert values["flux_git_repo_url"] == "ssh://git@github.com/me/lab" and values["flux_git_ref"] == "refs/heads/main"
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(dest), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--no-verify", "-qm", "two"], check=True)
    done = _validate(dest, "lab")
    assert done.returncode == 0, done.stdout + done.stderr


def test_env_add_refuses_an_existing_name(tmp_path: Path, controller: Path) -> None:
    code, dest = _init(tmp_path)
    assert code == 0
    with pytest.raises(scaffold.ScaffoldError, match="already exists"):
        init.run_env_add(dest, ["home", "--domain", "home.lan", "--node", "n1.home.lan"], CORE)


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        (["cp:control_plane", "wk:workers,edge"], "backup role"),
        (["a:control_plane,backup,edge,workers", "b:control_plane"], "control_plane role, found 2"),
        (["a:control_plane,backup,workers"], "no node holds the edge role"),
        (["a", "b:workers"], "give its roles"),
        (["a:master"], "unknown role master"),
    ],
)
def test_the_nodes_must_make_a_whole_cluster(nodes: list[str], message: str) -> None:
    with pytest.raises(scaffold.ScaffoldError, match=message):
        spec = scaffold.EnvSpec(
            name="home", domain="home.lan", repo_url="https://github.com/me/lab", core_url=CORE[0], core_tag=CORE[1],
            nodes=[scaffold.Node.parse(text, only=len(nodes) == 1) for text in nodes],
        )  # fmt: skip
        spec.check()


def test_a_lone_node_gets_every_role_but_gpu() -> None:
    assert scaffold.Node.parse("n1", only=True).roles == ("control_plane", "workers", "backup", "edge")


def test_the_installed_cli_knows_its_own_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rule: init pins the release the CLI came from: a checkout's tag and origin, or the install
    record uv tool install writes (PEP 610)."""
    monkeypatch.setattr(cli_core, "PRODUCT_ROOT", tmp_path)  # not a git checkout

    class Dist:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"url": "https://github.com/o/jarvis.git", "vcs_info": {"vcs": "git", "requested_revision": "v9.1.0"}})

    monkeypatch.setattr(cli_core, "distribution", lambda name: Dist())
    assert cli_core.own_release() == ("https://github.com/o/jarvis", "v9.1.0")
    assert cli_core.https_url("git@github.com:o/jarvis.git") == "https://github.com/o/jarvis"


def test_with_examples_turns_on_the_example_apps_and_passes_every_check(tmp_path: Path, controller: Path, monkeypatch) -> None:
    """Rule: --with-examples turns on app-hosting and the example apps, reads them from jarvis-apps
    beside core, and the env still passes validate as written."""
    monkeypatch.setenv("APPS_REPO", str(FIXTURE_APPS))
    code, dest = _init(tmp_path, "--with-examples")
    assert code == 0
    switches = yaml.safe_load((dest / "envs/home/values/switches.yaml").read_text())
    assert "app-hosting" in switches["features"]
    assert switches["apps"] == list(scaffold.example_apps()["versions"])
    apps = yaml.safe_load((dest / "envs/home/values/apps.yaml").read_text())
    assert apps == scaffold.example_apps()
    cluster_vars = yaml.safe_load((dest / "envs/home/values/cluster-vars.yaml").read_text())
    assert cluster_vars["apps_git_repo_url"] == "https://github.com/example-owner/jarvis-apps"
    assert (dest / "envs/home/flux/generated/apps.yaml").is_file()
    done = _validate(dest, "home")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_example_apps_need_app_hosting_and_an_apps_repository() -> None:
    spec = scaffold.EnvSpec("home", "home.lan", "https://github.com/me/lab", *CORE, [scaffold.Node.parse("n1", only=True)], examples=True)
    with pytest.raises(scaffold.ScaffoldError, match="app-hosting feature and an apps repository"):
        spec.check()


def test_the_agent_kit_is_opt_in(tmp_path: Path, controller: Path) -> None:
    """Rule: init writes the devcontainer always and the agent kit only with --with-agents; the
    kit's deny lists are core's own (core's .claude and .cursor files link to the kit)."""
    code, dest = _init(tmp_path)
    assert code == 0
    assert (dest / ".devcontainer/devcontainer.json").is_file() and os.access(dest / ".devcontainer/setup.sh", os.X_OK)
    assert not (dest / "AGENTS.md").exists() and not (dest / ".claude").exists()
    code, dest = _init(tmp_path / "with", "--with-agents")
    assert code == 0
    assert (dest / "CLAUDE.md").read_text() == "@AGENTS.md\n" and (dest / "AGENTS.md").is_file()
    assert (dest / ".claude/settings.json").read_bytes() == (REPO_ROOT / ".claude/settings.json").read_bytes()
    assert (dest / ".cursor/cli.json").read_bytes() == (REPO_ROOT / ".cursor/cli.json").read_bytes()
    assert json.loads((dest / ".claude/settings.json").read_text())["permissions"]["deny"]


def test_the_installed_package_carries_the_kit(tmp_path: Path) -> None:
    """Rule: init runs from the installed CLI, so everything it copies is in the wheel."""
    import zipfile

    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(tmp_path)], cwd=REPO_ROOT, check=True, capture_output=True)
    names = set(zipfile.ZipFile(next(tmp_path.glob("*.whl"))).namelist())
    kit = {path.relative_to(REPO_ROOT).as_posix() for path in scaffold.KIT.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    assert kit and kit <= names, sorted(kit - names)


def test_the_package_version_is_the_release_tag(tmp_path: Path) -> None:
    """Rule: a core release is its tag, and the package says so: a wheel built at v1.2.3 is 1.2.3
    (hatch-vcs), so uv tool list and pip agree with jarvis --version. Nothing is bumped by hand."""
    import zipfile

    files = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
    for rel in filter(None, files.split("\0")):
        source = REPO_ROOT / rel
        if source.is_file() and not source.is_symlink():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_bytes(source.read_bytes())
    who = ["-c", "user.email=t@t", "-c", "user.name=t"]
    # A throwaway tag on the same commit, as the quickstart gate pushes, is not a version.
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], [*who, "commit", "-q", "-m", "one"],
                 [*who, "tag", "-a", "v1.2.3", "-m", "v1.2.3"], ["tag", "quickstart/1-1"]):  # fmt: skip
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(tmp_path / "dist")], cwd=tmp_path, check=True, capture_output=True)
    wheel = next((tmp_path / "dist").glob("*.whl"))
    assert wheel.name.startswith("jarvis-1.2.3-")
    metadata = next(name for name in zipfile.ZipFile(wheel).namelist() if name.endswith("METADATA"))
    assert "Version: 1.2.3" in zipfile.ZipFile(wheel).read(metadata).decode()
