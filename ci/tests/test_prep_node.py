"""prep-node env edits, DNS fail-fast, address report, and age keys."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import require_cli

from support import REPO_ROOT, RUN_MODULE, sample_root

_HOSTS = """\
# inventory header
all:
  hosts:
    cp-1: # primary
      node_roles: [control_plane]
      data_disk: true
    wk-1:
      # leave this comment
      node_roles: [workers, edge]
"""

_HOSTS_RENAMED = """\
# inventory header
all:
  hosts:
    node-a: # primary
      node_roles: [control_plane]
      data_disk: true
    wk-1:
      # leave this comment
      node_roles: [workers, edge]
"""

_CLUSTER = """\
# cluster header
time_zone: Etc/UTC
ssh_user: example # the login
backup_path: /var/lib/jarvis/backup
"""


def _load():
    return importlib.import_module("jarvis.node.prep")


def test_env_update_renames_host_and_keeps_comments(tmp_path: Path) -> None:
    """The rename keeps every comment and changes nothing else; a second run writes nothing."""
    module = _load()
    env = tmp_path / "envs" / "env-test"
    inventory = env / "inventory" / "hosts.yml"
    cluster = env / "values" / "cluster-vars.yaml"
    for path, text in ((inventory, _HOSTS), (cluster, _CLUSTER)):
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")

    module.apply_env_edits(tmp_path, "env-test", "cp-1", "node-a", "example")

    after = {inventory: _HOSTS_RENAMED, cluster: _CLUSTER}
    assert {path: path.read_text(encoding="utf-8") for path in after} == after
    mtimes = {path: path.stat().st_mtime_ns for path in after}

    module.apply_env_edits(tmp_path, "env-test", "node-a", "node-a", "example")

    assert {path: path.read_text(encoding="utf-8") for path in after} == after
    assert {path: path.stat().st_mtime_ns for path in after} == mtimes


def _copy_example_env(root: Path, name: str = "env-new") -> Path:
    """A sample instance at root holding one env, as jarvis init writes it (ssh_user: example)."""
    import sample

    sample.build(root, name)
    return root / "envs" / name


def _ssh_user_env(tmp_path: Path, recorded: str | None, per_host: str | None = None) -> Path:
    """The sample env with ssh_user recorded as given (None: the key is gone), and wk-1's own."""
    dest = _copy_example_env(tmp_path)
    cluster = dest / "values" / "cluster-vars.yaml"
    line = "" if recorded is None else f"ssh_user: {recorded}\n"
    cluster.write_text(cluster.read_text(encoding="utf-8").replace("ssh_user: example\n", line), encoding="utf-8")
    if per_host:
        hosts = dest / "inventory" / "hosts.yml"
        text = hosts.read_text(encoding="utf-8").replace("    wk-1:\n", f"    wk-1:\n      ssh_user: {per_host}\n")
        hosts.write_text(text, encoding="utf-8")
    return cluster


def test_the_recorded_ssh_user_is_kept_on_every_node(tmp_path: Path) -> None:
    """jarvis init records ssh_user; prep keeps it, on the first node and each later one."""
    module = _load()
    cluster = _copy_example_env(tmp_path) / "values" / "cluster-vars.yaml"
    before = cluster.read_text(encoding="utf-8")
    module.apply_env_edits(tmp_path, "env-new", "cp-1", "cp-1", "example")
    module.apply_env_edits(tmp_path, "env-new", "wk-1", "wk-1", "example")
    assert cluster.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("per_host", "refusal"), [(None, "set ssh_user on that host"), ("alice", "change ssh_user on that host")],
    ids=["env-record", "host-record"],
)
def test_apply_env_edits_refuses_a_different_ssh_user(tmp_path: Path, per_host, refusal: str) -> None:
    """Write-time backstop: a recorded ssh_user, the env's or the host's, is not overwritten."""
    module = _load()
    cluster = _ssh_user_env(tmp_path, "example", per_host)
    hosts = cluster.parents[1] / "inventory" / "hosts.yml"
    before = cluster.read_text(encoding="utf-8"), hosts.read_text(encoding="utf-8")
    with pytest.raises(module.PrepError, match=refusal):
        module.apply_env_edits(tmp_path, "env-new", "wk-1", "wk-1", "other")
    assert (cluster.read_text(encoding="utf-8"), hosts.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize(("written", "recorded"), [('"ops"', "ops"), ("", None)], ids=["quoted", "empty"])
def test_recorded_ssh_user_is_the_scalar_or_none(tmp_path: Path, written: str, recorded) -> None:
    """A quoted ssh_user is the user, not the quotes; an empty one is the schema default."""
    module = _load()
    _ssh_user_env(tmp_path, written)
    assert module.recorded_ssh_user(tmp_path, "env-new") == recorded
    assert module.effective_ssh_user(tmp_path, "env-new") == (recorded or module.DEFAULT_SSH_USER)


def _ssh_snapshot() -> tuple[tuple[str, ...], bytes | None, int | None]:
    directory = Path.home() / ".ssh"
    if not directory.exists():
        return tuple(), None, None
    names = tuple(sorted(path.name for path in directory.iterdir()))
    known = directory / "known_hosts"
    if not known.is_file():
        return names, None, None
    return names, known.read_bytes(), known.stat().st_mtime_ns


def _host_state() -> tuple:
    etc_hosts = Path("/etc/hosts")
    envs = {path: path.read_bytes() for path in (sample_root() / "envs").rglob("*") if path.is_file()}
    git = subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO_ROOT, text=True)
    return etc_hosts.read_bytes(), etc_hosts.stat().st_mtime_ns, _ssh_snapshot(), git, envs


@pytest.mark.parametrize(
    ("host", "message"),
    [
        ("prep-node-does-not-resolve.invalid", "Add it on the router.\nNothing was changed."),
        ("localhost", "not a host in the inventory"),
    ],
    ids=["does-not-resolve", "not-in-inventory"],
)
def test_a_host_that_does_not_resolve_or_is_not_listed_exits_before_any_change(host: str, message: str) -> None:
    before = _host_state()
    result = subprocess.run(
        [*RUN_MODULE, "jarvis.node.prep", "--env", "env-00", "--host", host, "--login-user", "tester"],
        cwd=REPO_ROOT,
        env={**os.environ, "INSTANCE": str(sample_root()), "SOPS_AGE_KEY_FILE": str(sample_root() / ".age" / "keys.txt")},
        input="\n", capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert result.returncode != 0
    assert all(line in result.stderr for line in message.splitlines())
    assert _host_state() == before


def test_dhcp_warns_and_static_does_not_and_nothing_is_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DHCP warns that the address must stay stable. Static does not. No netplan or sudo write."""
    module = _load()
    calls: list[tuple[str, str]] = []

    class _Session:
        def __init__(self, text: str) -> None:
            self._text = text

        def bash(self, script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(("bash", script))
            assert args == []
            assert "netplan" not in script
            assert "sudo" not in script
            return subprocess.CompletedProcess([], 0, self._text, "")

        def sudo_bash(self, script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"sudo write issued: {script}")

    address = "192.0.2.10/24"
    module.report_address(
        _Session(f"iface=eth0\naddress={address}\ndhcp=yes\n")
    )
    dhcp_out = capsys.readouterr().out
    assert address in dhcp_out
    assert "DHCP" in dhcp_out
    assert "stable" in dhcp_out
    assert "k3s" in dhcp_out

    module.report_address(
        _Session(f"iface=eth0\naddress={address}\ndhcp=no\n")
    )
    static_out = capsys.readouterr().out
    assert address in static_out
    assert "static" in static_out
    assert "Warning" not in static_out
    assert "DHCP" not in static_out
    assert calls
    assert all(kind == "bash" for kind, _script in calls)


def test_missing_primary_address_fails() -> None:
    module = _load()

    class _Session:
        def bash(self, script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            assert script == module._DETECT_NET
            return subprocess.CompletedProcess([], 0, "iface=eth0\naddress=\ndhcp=yes\n", "")

    with pytest.raises(module.PrepError, match="primary address"):
        module.report_address(_Session())


def test_sshd_dropin_turns_off_password_login_only() -> None:
    assert _load().sshd_password_off() == "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"


def test_clock_synced_is_yes_only() -> None:
    module = _load()
    assert module.clock_synced("yes\n")
    assert not module.clock_synced("no\n")
    assert not module.clock_synced("")


def test_unsynced_clock_prompt_defaults_to_no(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    prompts: list[tuple[str, bool]] = []

    def fake_batch(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "no\n", "")

    def fake_confirm(question: str, default_yes: bool = False) -> bool:
        prompts.append((question, default_yes))
        return False

    monkeypatch.setattr(module, "batch_ssh", fake_batch)
    monkeypatch.setattr(module, "confirm", fake_confirm)
    with pytest.raises(module.PrepError, match="clock is not synced"):
        module.check_clock("user", "host.example")
    assert prompts == [("Continue with the clock unsynced?", False)]


def test_inventory_name_keeps_the_connected_domain() -> None:
    module = _load()
    assert module.inventory_name("node.example", "other") == "other.example"


def test_age_key_create_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating an age key refuses to replace an existing file. The env names the path, with no
    default, and SOPS_AGE_KEY_FILE overrides it."""
    require_cli("age-keygen")
    from jarvis.secrets import age

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    with pytest.raises(age.AgeError):
        age.key_path(None)
    path = age.key_path("~/.config/sops/age/keys.txt")
    assert path == tmp_path / ".config" / "sops" / "age" / "keys.txt"
    public = age.create_key(path)
    assert public.startswith("age1")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    original = path.read_bytes()
    with pytest.raises(age.AgeError):
        age.create_key(path)
    assert path.read_bytes() == original
    override = tmp_path / "other-key.txt"
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(override))
    assert age.key_path("~/.config/sops/age/keys.txt") == override


def test_show_git_diff_does_not_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load()
    seen: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.show_git_diff(tmp_path, "env-test")
    assert seen["argv"][:3] == ["git", "--no-pager", "diff"]


def _keyed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, recipients: str) -> tuple[Path, Path, str]:
    """An env whose cluster-vars name a key path, and a key at SOPS_AGE_KEY_FILE, which overrides it."""
    require_cli("age-keygen")
    from jarvis.secrets import age

    key = tmp_path / "age" / "keys.txt"
    public = age.create_key(key)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    repo = tmp_path / "repo"
    env = repo / "envs" / "env-test"
    (env / "values").mkdir(parents=True)
    (env / "values" / "cluster-vars.yaml").write_text("age_key_file: ~/.config/sops/age/keys.txt\n", encoding="utf-8")
    age.write_creation_rules(repo / ".sops.yaml", [age.creation_rule("env-test", recipients.format(public=public).split(","))])
    return repo, key, public


def test_a_key_among_the_recipients_passes_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule: the working key is one of the env's recipients (with the recovery key beside it)."""
    module = _load()
    repo, _key, public = _keyed_env(tmp_path, monkeypatch, recipients="{public},age1" + "q" * 58)
    before = {path: path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    module.check_age_key(repo, "env-test")
    assert f"Age public key: {public}" in capsys.readouterr().out
    assert {path: path.read_bytes() for path in repo.rglob("*") if path.is_file()} == before


def test_a_key_that_is_not_a_recipient_is_refused_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: a mismatched age key deletes nothing and edits nothing, and points at the recovery."""
    module = _load()
    repo, _key, _public = _keyed_env(tmp_path, monkeypatch, recipients="age1" + "q" * 58)
    before = {path: path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    with pytest.raises(module.PrepError, match="docs/secrets.md"):
        module.check_age_key(repo, "env-test")
    assert {path: path.read_bytes() for path in repo.rglob("*") if path.is_file()} == before


def test_a_missing_key_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    repo, key, _public = _keyed_env(tmp_path, monkeypatch, recipients="{public}")
    key.unlink()
    with pytest.raises(module.PrepError, match="is missing; copy it here"):
        module.check_age_key(repo, "env-test")


def test_env_schema_accepts_an_env_as_init_writes_it() -> None:
    """prep's schema step accepts the sample instance, written by jarvis init's code."""
    module = _load()
    module.run_env_schema(sample_root())


def test_env_schema_rejects_a_seeded_bad_copy(tmp_path: Path) -> None:
    """A seeded invalid copy of env-00 fails as PrepError, not a traceback."""
    module = _load()
    dest = tmp_path / "envs" / "env-00"
    shutil.copytree(sample_root() / "envs" / "env-00", dest)
    cluster = dest / "values" / "cluster-vars.yaml"
    original = cluster.read_text(encoding="utf-8")
    seeded = original.replace("time_zone: Etc/UTC", "time_zone: Not/A_Zone", 1)
    assert seeded != original
    cluster.write_text(seeded, encoding="utf-8")

    with pytest.raises(module.PrepError) as excinfo:
        module.run_env_schema(tmp_path)
    message = str(excinfo.value)
    assert "time_zone" in message
    assert "Traceback" not in message
    assert "FileNotFoundError" not in message


def test_env_schema_oserror_is_prep_error(tmp_path: Path) -> None:
    """An unreadable env tree is a PrepError message, not a traceback."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory mode 000")
    module = _load()
    envs = tmp_path / "envs"
    envs.mkdir()
    envs.chmod(0)
    try:
        with pytest.raises(module.PrepError) as excinfo:
            module.run_env_schema(tmp_path)
    finally:
        envs.chmod(0o755)
    message = str(excinfo.value)
    assert "env schema check failed" in message
    assert "Permission denied" in message
    assert "Traceback" not in message
    assert isinstance(excinfo.value.__cause__, OSError)


def _block_host_calls(module: object, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    host_calls: list[str] = []

    def record(name: str):
        def _blocked(*_args: object, **_kwargs: object) -> object:
            host_calls.append(name)
            raise AssertionError(f"{name} called")

        return _blocked

    real_run = module.subprocess.run

    def guarded_run(argv: object, *args: object, **kwargs: object) -> object:
        command = argv[0] if argv else ""
        if command in {"ssh", "ssh-keyscan"}:
            host_calls.append(str(command))
            raise AssertionError(f"{command} called")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(module, "assert_host_resolves", record("assert_host_resolves"))
    monkeypatch.setattr(module, "ensure_public_key", record("ensure_public_key"))
    monkeypatch.setattr(module, "ensure_host_key", record("ensure_host_key"))
    monkeypatch.setattr(module, "scan_host_keys", record("scan_host_keys"))
    monkeypatch.setattr(module, "open_session", record("open_session"))
    monkeypatch.setattr(module, "ssh", record("ssh"))
    monkeypatch.setattr(module, "batch_ssh", record("batch_ssh"))
    monkeypatch.setattr(module.subprocess, "run", guarded_run)
    return host_calls


def _stub_host_steps(
    module: object, monkeypatch: pytest.MonkeyPatch, *, hostname: str = "cp-1"
) -> list[str]:
    """Replace every host-touching run() step. Returns the names that were called."""
    called: list[str] = []

    class _Session:
        def close(self) -> None:
            called.append("close")

    def track(name: str, ret: object = None):
        def _fn(*_args: object, **_kwargs: object) -> object:
            called.append(name)
            return ret

        return _fn

    real_schema = module.run_env_schema

    def schema(repo: Path, *, pre_host: bool = False) -> None:
        called.append("run_env_schema")
        if pre_host:
            real_schema(repo, pre_host=True)

    monkeypatch.setattr(module, "run_env_schema", schema)
    monkeypatch.setattr(module, "assert_host_resolves", track("assert_host_resolves"))
    monkeypatch.setattr(module, "ensure_public_key", track("ensure_public_key", Path("/tmp/id.pub")))
    monkeypatch.setattr(module, "public_key_text", track("public_key_text", "ssh-ed25519 AAAA"))
    monkeypatch.setattr(module, "ensure_host_key", track("ensure_host_key"))
    monkeypatch.setattr(module, "open_session", track("open_session", _Session()))
    monkeypatch.setattr(module, "install_login_key", track("install_login_key"))
    monkeypatch.setattr(module, "ensure_sudo", track("ensure_sudo"))
    monkeypatch.setattr(module, "configure_hostname", track("configure_hostname", hostname))
    monkeypatch.setattr(module, "ensure_ansible_user", track("ensure_ansible_user"))
    monkeypatch.setattr(module, "install_minimum", track("install_minimum"))
    monkeypatch.setattr(module, "report_address", track("report_address"))
    monkeypatch.setattr(module, "proof", track("proof"))
    monkeypatch.setattr(module, "offer_ssh_passwords_off", track("offer_ssh_passwords_off"))
    monkeypatch.setattr(module, "check_clock", track("check_clock"))
    monkeypatch.setattr(module, "check_age_key", track("check_age_key"))
    monkeypatch.setattr(module, "show_git_diff", track("show_git_diff"))
    return called


def _accept_default_ansible_user(module: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: True)


def test_schema_error_makes_zero_host_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema error exits before SSH, ssh-keyscan, or other host calls."""
    module = _load()
    dest = tmp_path / "envs" / "env-00"
    shutil.copytree(sample_root() / "envs" / "env-00", dest)
    cluster = dest / "values" / "cluster-vars.yaml"
    original = cluster.read_text(encoding="utf-8")
    seeded = original.replace("dns_suffix: env-00.example", "dns_suffix: Home_Lab", 1)
    assert seeded != original
    cluster.write_text(seeded, encoding="utf-8")
    host_calls = _block_host_calls(module, monkeypatch)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    with pytest.raises(module.PrepError) as excinfo:
        module.run(["--env", "env-00", "--host", "cp-1", "--login-user", "ubuntu"])
    assert "dns_suffix" in str(excinfo.value)
    assert host_calls == []


@pytest.mark.parametrize(
    ("recorded", "per_host", "host", "login", "kept"),
    [
        ("example", None, "cp-1", "ubuntu", "example"),  # first node: init's record, whatever the login
        ("ops", "alice", "wk-1", "ubuntu", "ops"),  # a per-host ssh_user is that host's user
        ("ops", None, "wk-1", "ubuntu", "ops"),  # a later node keeps the record when the login differs
        ("ops", None, "wk-1", "ops", "ops"),
        (None, None, "cp-1", "ops", "ubuntu"),  # a missing key is the schema default, and is kept
    ],
    ids=["init-record", "per-host", "later-node", "same-login", "schema-default"],
)
def test_run_keeps_the_recorded_ansible_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded, per_host, host: str, login: str, kept: str
) -> None:
    module = _load()
    cluster = _ssh_user_env(tmp_path, recorded, per_host)
    before = cluster.read_text(encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    called = _stub_host_steps(module, monkeypatch, hostname=host)

    module.run(["--env", "env-new", "--host", host, "--login-user", login])

    assert module.recorded_ssh_user(tmp_path, "env-new") == kept
    assert module.host_ssh_user(tmp_path, "env-new", host) == per_host
    if recorded is not None:
        assert cluster.read_text(encoding="utf-8") == before
    assert "assert_host_resolves" in called


@pytest.mark.parametrize(
    ("recorded", "host", "asked"), [("ops", "wk-1", "other"), (None, "cp-1", "ops")],
    ids=["recorded", "schema-default"],
)
def test_run_refuses_a_different_ansible_user_before_any_host_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded, host: str, asked: str
) -> None:
    module = _load()
    cluster = _ssh_user_env(tmp_path, recorded)
    before = cluster.read_text(encoding="utf-8")
    host_calls = _block_host_calls(module, monkeypatch)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "prompt", lambda *_args, **_kwargs: asked)

    with pytest.raises(module.PrepError, match="set ssh_user on that host"):
        module.run(["--env", "env-new", "--host", host, "--login-user", asked])
    assert host_calls == []
    assert cluster.read_text(encoding="utf-8") == before


def test_a_new_env_reaches_the_host_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env as init writes it does not refuse a real login; host checks are what run next."""
    module = _load()
    _copy_example_env(tmp_path)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(tmp_path / ".age" / "keys.txt"))
    host_calls: list[str] = []

    def stop(*_args: object, **_kwargs: object) -> None:
        host_calls.append("assert_host_resolves")
        raise module.PrepError("stopped after pre-host")

    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    monkeypatch.setattr(module, "assert_host_resolves", stop)

    with pytest.raises(module.PrepError, match="stopped after pre-host"):
        module.run(["--env", "env-new", "--host", "cp-1", "--login-user", "ubuntu"])
    assert host_calls == ["assert_host_resolves"]


def test_missing_age_keygen_exits_before_host_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """age-keygen is required before any host call."""
    module = _load()
    real_which = module.shutil.which

    def which(name: str) -> str | None:
        if name == "age-keygen":
            return None
        return real_which(name)

    host_calls = _block_host_calls(module, monkeypatch)
    monkeypatch.setattr(module.shutil, "which", which)

    with pytest.raises(module.PrepError, match="age-keygen"):
        module.run(["--env", "env-00", "--host", "cp-1", "--login-user", "ubuntu"])
    assert host_calls == []


def test_run_calls_full_schema_after_host_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After host edits, run() still applies the full env schema."""
    module = _load()
    _copy_example_env(tmp_path, "env-00")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    _stub_host_steps(module, monkeypatch)
    schema_modes: list[bool] = []

    def track_schema(repo: Path, *, pre_host: bool = False) -> None:
        schema_modes.append(pre_host)
        if pre_host:
            module.validate_env_tree(repo, pre_host=True)
            return
        raise module.PrepError("full schema failed")

    monkeypatch.setattr(module, "run_env_schema", track_schema)
    monkeypatch.setattr(module, "apply_env_edits", lambda *_a, **_k: None)

    with pytest.raises(module.PrepError, match="full schema failed"):
        module.run(["--env", "env-00", "--host", "cp-1", "--login-user", "ubuntu"])
    assert schema_modes == [True, False]


