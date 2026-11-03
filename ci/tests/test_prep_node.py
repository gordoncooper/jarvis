"""prep-node env edits, DNS fail-fast, address report, and age keys."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import require_cli
from support import REPO_ROOT, _rename_copied_env, load_tool

SCRIPT = REPO_ROOT / "tools" / "prep-node"

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
build_agent: cursor
ssh_user: example # the login
backup_path: /var/lib/jarvis/backup
"""


def _load():
    return load_tool("prep_node", SCRIPT)


def test_env_update_renames_host_and_keeps_comments(tmp_path: Path) -> None:
    module = _load()
    env = tmp_path / "envs" / "env-test"
    inventory = env / "inventory" / "hosts.yml"
    cluster = env / "values" / "cluster-vars.yaml"
    inventory.parent.mkdir(parents=True)
    cluster.parent.mkdir(parents=True)
    inventory.write_text(_HOSTS, encoding="utf-8")
    cluster.write_text(_CLUSTER, encoding="utf-8")

    module.apply_env_edits(tmp_path, "env-test", "cp-1", "node-a", "example")

    assert inventory.read_text(encoding="utf-8") == _HOSTS_RENAMED
    assert cluster.read_text(encoding="utf-8") == _CLUSTER
    inventory_mtime = inventory.stat().st_mtime_ns
    cluster_mtime = cluster.stat().st_mtime_ns

    module.apply_env_edits(tmp_path, "env-test", "node-a", "node-a", "example")

    assert inventory.read_text(encoding="utf-8") == _HOSTS_RENAMED
    assert cluster.read_text(encoding="utf-8") == _CLUSTER
    assert inventory.stat().st_mtime_ns == inventory_mtime
    assert cluster.stat().st_mtime_ns == cluster_mtime


def test_env_edits_rerender_cluster_vars_configmap(tmp_path: Path) -> None:
    module = _load()
    env = tmp_path / "envs" / "env-test"
    inventory = env / "inventory" / "hosts.yml"
    cluster = env / "values" / "cluster-vars.yaml"
    generated = env / "flux" / "generated" / "cluster-vars-configmap.yaml"
    inventory.parent.mkdir(parents=True)
    cluster.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    inventory.write_text(_HOSTS, encoding="utf-8")
    cluster.write_text(_CLUSTER, encoding="utf-8")
    generated.write_text("stale\n", encoding="utf-8")

    module.apply_env_edits(tmp_path, "env-test", "cp-1", "node-a", "example")

    from render_cluster_vars_configmap import render_document

    rendered = render_document(cluster)
    assert "backup_path: /var/lib/jarvis/backup" in rendered
    assert "ssh_user" not in rendered
    assert generated.read_text(encoding="utf-8") == rendered
    generated_mtime = generated.stat().st_mtime_ns

    module.apply_env_edits(tmp_path, "env-test", "node-a", "node-a", "example")

    assert generated.read_text(encoding="utf-8") == rendered
    assert generated.stat().st_mtime_ns == generated_mtime


def _copy_example_env(root: Path, name: str = "env-new") -> Path:
    dest = root / "envs" / name
    shutil.copytree(REPO_ROOT / "envs" / "env-00", dest)
    _rename_copied_env(dest, "env-00", name)
    return dest


def test_first_node_replaces_example_ssh_user(tmp_path: Path) -> None:
    """A fresh env-00 copy records a real login in place of the example placeholder."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    cluster = dest / "values" / "cluster-vars.yaml"
    inventory = dest / "inventory" / "hosts.yml"
    assert "ssh_user: example" in cluster.read_text(encoding="utf-8")

    module.apply_env_edits(tmp_path, "env-new", "cp-1", "cp-1", "ubuntu")

    text = cluster.read_text(encoding="utf-8")
    assert "ssh_user: ubuntu" in text
    assert "ssh_user: example" not in text
    assert "cp-1:" in inventory.read_text(encoding="utf-8")


def test_same_ssh_user_passes_on_a_second_node(tmp_path: Path) -> None:
    """The same recorded ssh_user may be used again."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    module.apply_env_edits(tmp_path, "env-new", "cp-1", "cp-1", "ubuntu")
    cluster = dest / "values" / "cluster-vars.yaml"
    before = cluster.read_text(encoding="utf-8")
    module.apply_env_edits(tmp_path, "env-new", "wk-1", "wk-1", "ubuntu")
    assert cluster.read_text(encoding="utf-8") == before


def test_apply_env_edits_refuses_a_different_recorded_ssh_user(tmp_path: Path) -> None:
    """Write-time backstop: a recorded ssh_user cannot be overwritten."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    module.apply_env_edits(tmp_path, "env-new", "cp-1", "cp-1", "ops")
    before = dest.joinpath("values/cluster-vars.yaml").read_text(encoding="utf-8")
    with pytest.raises(module.PrepError, match="per-host users come later"):
        module.apply_env_edits(tmp_path, "env-new", "wk-1", "wk-1", "other")
    assert dest.joinpath("values/cluster-vars.yaml").read_text(encoding="utf-8") == before


def test_quoted_ssh_user_is_the_scalar(tmp_path: Path) -> None:
    """A quoted ssh_user value is the user, not the quotes."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    cluster = dest / "values" / "cluster-vars.yaml"
    cluster.write_text(
        cluster.read_text(encoding="utf-8").replace("ssh_user: example", 'ssh_user: "ops"'),
        encoding="utf-8",
    )
    assert module.recorded_ssh_user(tmp_path, "env-new") == "ops"
    assert not module.ssh_user_is_placeholder(tmp_path, "env-new")


def test_empty_ssh_user_is_the_schema_default(tmp_path: Path) -> None:
    """An empty ssh_user key is the schema default, not the next line's key."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    cluster = dest / "values" / "cluster-vars.yaml"
    cluster.write_text(
        cluster.read_text(encoding="utf-8").replace("ssh_user: example", "ssh_user:"),
        encoding="utf-8",
    )
    assert module.recorded_ssh_user(tmp_path, "env-new") is None
    assert module.effective_ssh_user(tmp_path, "env-new") == module.DEFAULT_SSH_USER
    assert not module.ssh_user_is_placeholder(tmp_path, "env-new")


def _ssh_snapshot() -> tuple[tuple[str, ...], bytes | None, int | None]:
    directory = Path.home() / ".ssh"
    if not directory.exists():
        return tuple(), None, None
    names = tuple(sorted(path.name for path in directory.iterdir()))
    known = directory / "known_hosts"
    if not known.is_file():
        return names, None, None
    return names, known.read_bytes(), known.stat().st_mtime_ns


def test_unresolvable_host_exits_before_any_change() -> None:
    host = "prep-node-does-not-resolve.invalid"
    etc_hosts = Path("/etc/hosts")
    before_hosts = etc_hosts.read_bytes()
    before_hosts_mtime = etc_hosts.stat().st_mtime_ns
    before_ssh = _ssh_snapshot()
    before_git = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        text=True,
    )

    result = subprocess.run(
        [str(SCRIPT), "--env", "env-00", "--host", host, "--login-user", "tester"],
        cwd=REPO_ROOT,
        input="\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "Add it on the router." in result.stderr
    assert "Nothing was changed." in result.stderr
    assert etc_hosts.read_bytes() == before_hosts
    assert etc_hosts.stat().st_mtime_ns == before_hosts_mtime
    assert _ssh_snapshot() == before_ssh
    after_git = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        text=True,
    )
    assert after_git == before_git


def test_resolved_host_missing_from_inventory_exits_before_ssh() -> None:
    etc_hosts = Path("/etc/hosts")
    before_hosts = etc_hosts.read_bytes()
    before_hosts_mtime = etc_hosts.stat().st_mtime_ns
    before_ssh = _ssh_snapshot()

    result = subprocess.run(
        [str(SCRIPT), "--env", "env-00", "--host", "localhost", "--login-user", "tester"],
        cwd=REPO_ROOT,
        input="\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "not a host in the inventory" in result.stderr
    assert etc_hosts.read_bytes() == before_hosts
    assert etc_hosts.stat().st_mtime_ns == before_hosts_mtime
    assert _ssh_snapshot() == before_ssh


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
    module = _load()
    text = module.sshd_password_off()
    assert text == "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    assert "PubkeyAuthentication" not in text


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
        module.check_clock("user", "host.example", Path("/tmp/id_ed25519.pub"))
    assert prompts == [("Continue with the clock unsynced?", False)]


def test_inventory_name_keeps_the_connected_domain() -> None:
    module = _load()
    assert module.inventory_name("node.example", "other") == "other.example"


def test_age_key_create_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating an age key refuses to replace an existing file. SOPS_AGE_KEY_FILE overrides the path."""
    require_cli("age-keygen")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    module = _load()
    path = Path.home() / ".config" / "sops" / "age" / "keys.txt"
    public = module.create_age_key(path)
    assert public.startswith("age1")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    original = path.read_bytes()
    with pytest.raises(module.PrepError):
        module.create_age_key(path)
    assert path.read_bytes() == original

    override = tmp_path / "other-key.txt"
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(override))
    assert module.age_key_path("~/.config/sops/age/keys.txt") == override


def test_show_git_diff_does_not_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load()
    seen: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.show_git_diff(tmp_path, "env-test")
    assert seen["argv"][:3] == ["git", "--no-pager", "diff"]
    assert "envs/env-test/flux/generated/cluster-vars-configmap.yaml" in seen["argv"]


def test_create_age_key_prints_when_it_tightens_an_existing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    require_cli("age-keygen")
    module = _load()
    parent = tmp_path / "age"
    parent.mkdir()
    parent.chmod(0o755)
    public = module.create_age_key(parent / "keys.txt")
    assert public.startswith("age1")
    assert parent.stat().st_mode & 0o777 == 0o700
    assert f"Set {parent} to 0700." in capsys.readouterr().out


def test_age_mismatch_changes_neither_recipient_nor_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatched age key deletes nothing and does not edit .sops.yaml."""
    require_cli("age-keygen")
    module = _load()
    parent = tmp_path / "age"
    parent.mkdir(mode=0o700)
    key = parent / "keys.txt"
    module.create_age_key(key)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    repo = tmp_path / "repo"
    env = repo / "envs" / "env-test"
    (env / "values").mkdir(parents=True)
    (env / "secrets").mkdir()
    secret = env / "secrets" / "app.sops.yaml"
    secret.write_text("sops: {}\n", encoding="utf-8")
    cluster = env / "values" / "cluster-vars.yaml"
    cluster_text = "ssh_user: ops\nage_key_file: ~/.config/sops/age/keys.txt\n"
    cluster.write_text(cluster_text, encoding="utf-8")
    generated = env / "flux" / "generated" / "cluster-vars-configmap.yaml"
    generated.parent.mkdir(parents=True)
    generated.write_text("stale\n", encoding="utf-8")
    sops = repo / ".sops.yaml"
    sops_text = (
        "creation_rules:\n"
        "  - path_regex: envs/env-test/secrets/.*\\.sops\\.yaml$\n"
        "    age: age1old\n"
    )
    sops.write_text(sops_text, encoding="utf-8")
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: pytest.fail("confirm called"))
    with pytest.raises(module.PrepError, match="envs/env-00/secrets/README.md"):
        module.sync_age_key(repo, "env-test")
    assert sops.read_text(encoding="utf-8") == sops_text
    assert secret.read_text(encoding="utf-8") == "sops: {}\n"
    assert cluster.read_text(encoding="utf-8") == cluster_text
    assert generated.read_text(encoding="utf-8") == "stale\n"


def test_matching_age_key_does_not_warn_about_another_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    require_cli("age-keygen")
    module = _load()
    parent = tmp_path / "age"
    parent.mkdir(mode=0o700)
    key = parent / "keys.txt"
    public = module.create_age_key(key)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    repo = tmp_path / "repo"
    env = repo / "envs" / "env-test"
    (env / "values").mkdir(parents=True)
    cluster = env / "values" / "cluster-vars.yaml"
    cluster.write_text("age_key_file: ~/.config/sops/age/keys.txt\n", encoding="utf-8")
    sops = repo / ".sops.yaml"
    sops.write_text(
        "creation_rules:\n"
        f"  - path_regex: envs/env-test/secrets/.*\\.sops\\.yaml$\n    age: {public}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: pytest.fail("confirm called"))
    module.sync_age_key(repo, "env-test")
    out = capsys.readouterr().out
    assert "another env" not in out
    assert "Age recipient already matches." in out


def _age_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    require_cli("age-keygen")
    module = _load()
    parent = tmp_path / "age"
    parent.mkdir(mode=0o700)
    key = parent / "keys.txt"
    public = module.create_age_key(key)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    repo = tmp_path / "repo"
    env = repo / "envs" / "env-test"
    (env / "values").mkdir(parents=True)
    (env / "secrets").mkdir()
    secret = env / "secrets" / "app.sops.yaml"
    secret.write_text("sops: {}\n", encoding="utf-8")
    (env / "values" / "cluster-vars.yaml").write_text(
        "age_key_file: ~/.config/sops/age/keys.txt\n", encoding="utf-8"
    )
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: pytest.fail("confirm called"))
    return module, repo, public, secret


def test_missing_sops_rule_is_added_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing creation rule is written once and a second run changes nothing."""
    module, repo, public, secret = _age_fixture(tmp_path, monkeypatch)
    sops = repo / ".sops.yaml"
    sops.write_text(
        "creation_rules:\n"
        "  - path_regex: envs/other/secrets/.*\\.sops\\.yaml$\n"
        "    encrypted_regex: ^(data|stringData)$\n"
        "    age: age1keep\n",
        encoding="utf-8",
    )
    module.sync_age_key(repo, "env-test")
    text = sops.read_text(encoding="utf-8")
    assert "age: age1keep" in text
    assert f"age: {public}" in text
    assert secret.read_text(encoding="utf-8") == "sops: {}\n"
    snapshot = {path: path.read_bytes() for path in (sops, repo / "envs" / "env-test" / "values" / "cluster-vars.yaml")}
    module.sync_age_key(repo, "env-test")
    for path, blob in snapshot.items():
        assert path.read_bytes() == blob


def test_missing_sops_file_is_created_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no .sops.yaml, one creation rule is written and a second run changes nothing."""
    module, repo, public, secret = _age_fixture(tmp_path, monkeypatch)
    sops = repo / ".sops.yaml"
    assert not sops.exists()
    module.sync_age_key(repo, "env-test")
    text = sops.read_text(encoding="utf-8")
    assert text == (
        "creation_rules:\n"
        "  - path_regex: envs/env-test/secrets/.*\\.sops\\.yaml$\n"
        "    encrypted_regex: ^(data|stringData)$\n"
        f"    age: {public}\n"
    )
    assert secret.read_text(encoding="utf-8") == "sops: {}\n"
    snapshot = sops.read_bytes()
    module.sync_age_key(repo, "env-test")
    assert sops.read_bytes() == snapshot


def test_env_schema_accepts_the_real_env_tree() -> None:
    """prep-node schema step accepts the real env tree, including env-00 (J5)."""
    module = _load()
    assert (REPO_ROOT / "envs" / "env-00").is_dir()
    module.run_env_schema(REPO_ROOT)


def test_env_schema_rejects_a_seeded_bad_copy(tmp_path: Path) -> None:
    """A seeded invalid copy of env-00 fails as PrepError, not a traceback (J5)."""
    module = _load()
    dest = tmp_path / "envs" / "env-00"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", dest)
    cluster = dest / "values" / "cluster-vars.yaml"
    original = cluster.read_text(encoding="utf-8")
    seeded = original.replace("build_agent: cursor", "build_agent: not-a-provider", 1)
    assert seeded != original
    cluster.write_text(seeded, encoding="utf-8")

    with pytest.raises(module.PrepError) as excinfo:
        module.run_env_schema(tmp_path)
    message = str(excinfo.value)
    assert "build_agent" in message
    assert "Traceback" not in message
    assert "FileNotFoundError" not in message


def test_env_schema_oserror_is_prep_error(tmp_path: Path) -> None:
    """An unreadable env tree is a PrepError message, not a traceback (J5)."""
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
        if command in {"ssh", "ssh-keyscan", "getent"}:
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
    monkeypatch.setattr(module, "sync_age_key", track("sync_age_key"))
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
    shutil.copytree(REPO_ROOT / "envs" / "env-00", dest)
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


def test_run_first_node_records_login_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First node: example placeholder plus login ubuntu records ubuntu."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    called = _stub_host_steps(module, monkeypatch, hostname="cp-1")

    module.run(["--env", "env-new", "--host", "cp-1", "--login-user", "ubuntu"])

    assert module.recorded_ssh_user(tmp_path, "env-new") == "ubuntu"
    assert "ssh_user: example" not in dest.joinpath("values/cluster-vars.yaml").read_text(
        encoding="utf-8"
    )
    assert "assert_host_resolves" in called


def test_run_second_node_keeps_recorded_user_when_login_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second node: recorded ops, login ubuntu, keeps ops."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    module.apply_env_edits(tmp_path, "env-new", "cp-1", "cp-1", "ops")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    called = _stub_host_steps(module, monkeypatch, hostname="wk-1")

    module.run(["--env", "env-new", "--host", "wk-1", "--login-user", "ubuntu"])

    assert module.recorded_ssh_user(tmp_path, "env-new") == "ops"
    assert "ssh_user: ops" in dest.joinpath("values/cluster-vars.yaml").read_text(
        encoding="utf-8"
    )
    assert "assert_host_resolves" in called


def test_run_second_node_refuses_a_different_ansible_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second node that asks for a different Ansible user makes zero host calls."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    module.apply_env_edits(tmp_path, "env-new", "cp-1", "cp-1", "ops")
    before = dest.joinpath("values/cluster-vars.yaml").read_text(encoding="utf-8")
    host_calls = _block_host_calls(module, monkeypatch)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "prompt", lambda *_args, **_kwargs: "other")

    with pytest.raises(module.PrepError, match="per-host users come later"):
        module.run(["--env", "env-new", "--host", "wk-1", "--login-user", "ubuntu"])
    assert host_calls == []
    assert dest.joinpath("values/cluster-vars.yaml").read_text(encoding="utf-8") == before


def test_run_same_ansible_user_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded ops plus login ops keeps ops."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    module.apply_env_edits(tmp_path, "env-new", "cp-1", "cp-1", "ops")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    called = _stub_host_steps(module, monkeypatch, hostname="wk-1")

    module.run(["--env", "env-new", "--host", "wk-1", "--login-user", "ops"])

    assert module.recorded_ssh_user(tmp_path, "env-new") == "ops"
    assert "assert_host_resolves" in called


def test_run_missing_ssh_user_keeps_the_schema_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ssh_user is already ubuntu; login may differ and that default is kept."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    cluster = dest / "values" / "cluster-vars.yaml"
    cluster.write_text(
        cluster.read_text(encoding="utf-8").replace("ssh_user: example\n", ""),
        encoding="utf-8",
    )
    assert module.recorded_ssh_user(tmp_path, "env-new") is None
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    _stub_host_steps(module, monkeypatch, hostname="cp-1")

    module.run(["--env", "env-new", "--host", "cp-1", "--login-user", "ops"])

    assert module.effective_ssh_user(tmp_path, "env-new") == module.DEFAULT_SSH_USER
    assert module.recorded_ssh_user(tmp_path, "env-new") == module.DEFAULT_SSH_USER


def test_run_missing_ssh_user_refuses_a_different_ansible_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for a user other than the schema default is refused with zero host calls."""
    module = _load()
    dest = _copy_example_env(tmp_path)
    cluster = dest / "values" / "cluster-vars.yaml"
    cluster.write_text(
        cluster.read_text(encoding="utf-8").replace("ssh_user: example\n", ""),
        encoding="utf-8",
    )
    host_calls = _block_host_calls(module, monkeypatch)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "prompt", lambda *_args, **_kwargs: "ops")

    with pytest.raises(module.PrepError, match="per-host users come later"):
        module.run(["--env", "env-new", "--host", "cp-1", "--login-user", "ops"])
    assert host_calls == []
    assert module.recorded_ssh_user(tmp_path, "env-new") is None


def test_first_node_placeholder_ssh_user_reaches_host_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh copy does not refuse a real login; host checks are what run next."""
    module = _load()
    _copy_example_env(tmp_path)
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
    dest = tmp_path / "envs" / "env-00"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", dest)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    _accept_default_ansible_user(module, monkeypatch)
    schema_modes: list[bool] = []

    def track_schema(repo: Path, *, pre_host: bool = False) -> None:
        schema_modes.append(pre_host)
        if pre_host:
            module.validate_env_tree(repo, pre_host=True)
            return
        raise module.PrepError("full schema failed")

    class _Session:
        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "run_env_schema", track_schema)
    monkeypatch.setattr(module, "assert_host_resolves", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "ensure_public_key", lambda: Path("/tmp/id.pub"))
    monkeypatch.setattr(module, "public_key_text", lambda _p: "ssh-ed25519 AAAA")
    monkeypatch.setattr(module, "ensure_host_key", lambda _h: None)
    monkeypatch.setattr(module, "open_session", lambda *_a, **_k: _Session())
    monkeypatch.setattr(module, "install_login_key", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "ensure_sudo", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "configure_hostname", lambda *_a, **_k: "cp-1")
    monkeypatch.setattr(module, "ensure_ansible_user", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "install_minimum", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "report_address", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "proof", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "offer_ssh_passwords_off", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "check_clock", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "apply_env_edits", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "sync_age_key", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "show_git_diff", lambda *_a, **_k: None)

    with pytest.raises(module.PrepError, match="full schema failed"):
        module.run(["--env", "env-00", "--host", "cp-1", "--login-user", "ubuntu"])
    assert schema_modes == [True, False]


def test_doc_order_copy_passes_pre_host_without_creation_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copied env-00 with no creation rule yet passes pre-host; after edits, full."""
    require_cli("age-keygen")
    module = _load()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    root = tmp_path / "repo"
    dest = root / "envs" / "env-new"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", dest)
    _rename_copied_env(dest, "env-00", "env-new")
    cluster = dest / "values" / "cluster-vars.yaml"
    original = cluster.read_text(encoding="utf-8")
    edited = original.replace("dns_suffix: env-00.example", "dns_suffix: lab.example", 1)
    assert edited != original
    cluster.write_text(edited, encoding="utf-8")

    module.run_env_schema(root, pre_host=True)

    module.apply_env_edits(root, "env-new", "cp-1", "cp-1", "ubuntu")
    monkeypatch.setattr(module, "confirm", lambda *_args, **_kwargs: True)
    module.sync_age_key(root, "env-new")
    module.run_env_schema(root)
