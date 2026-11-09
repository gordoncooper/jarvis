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
