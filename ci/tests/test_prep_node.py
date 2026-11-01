"""prep-node env edits, DNS fail-fast, address report, and age keys."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import require_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
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

_CLUSTER_EDITED = """\
# cluster header
build_agent: cursor
ssh_user: ops # the login
backup_path: /var/lib/jarvis/backup
"""


def _load():
    loader = importlib.machinery.SourceFileLoader("prep_node", str(SCRIPT))
    spec = importlib.util.spec_from_loader("prep_node", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


def test_env_update_renames_host_and_keeps_comments(tmp_path: Path) -> None:
    module = _load()
    env = tmp_path / "envs" / "env-test"
    inventory = env / "inventory" / "hosts.yml"
    cluster = env / "values" / "cluster-vars.yaml"
    inventory.parent.mkdir(parents=True)
    cluster.parent.mkdir(parents=True)
    inventory.write_text(_HOSTS, encoding="utf-8")
    cluster.write_text(_CLUSTER, encoding="utf-8")

    module.apply_env_edits(tmp_path, "env-test", "cp-1", "node-a", "ops")

    assert inventory.read_text(encoding="utf-8") == _HOSTS_RENAMED
    assert cluster.read_text(encoding="utf-8") == _CLUSTER_EDITED
    inventory_mtime = inventory.stat().st_mtime_ns
    cluster_mtime = cluster.stat().st_mtime_ns

    module.apply_env_edits(tmp_path, "env-test", "node-a", "node-a", "ops")

    assert inventory.read_text(encoding="utf-8") == _HOSTS_RENAMED
    assert cluster.read_text(encoding="utf-8") == _CLUSTER_EDITED
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

    module.apply_env_edits(tmp_path, "env-test", "cp-1", "node-a", "ops")

    from render_cluster_vars_configmap import render_document

    rendered = render_document(cluster)
    assert "backup_path: /var/lib/jarvis/backup" in rendered
    assert "ssh_user" not in rendered
    assert generated.read_text(encoding="utf-8") == rendered
    generated_mtime = generated.stat().st_mtime_ns

    module.apply_env_edits(tmp_path, "env-test", "node-a", "node-a", "ops")

    assert generated.read_text(encoding="utf-8") == rendered
    assert generated.stat().st_mtime_ns == generated_mtime


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


def test_age_key_create_and_recipient_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    repo = tmp_path / "repo"
    env = repo / "envs" / "env-test"
    (env / "secrets").mkdir(parents=True)
    kept_dir = repo / "envs" / "other" / "secrets"
    kept_dir.mkdir(parents=True)
    secret = env / "secrets" / "app.sops.yaml"
    secret.write_text("sops: {}\n", encoding="utf-8")
    kept = kept_dir / "app.sops.yaml"
    kept.write_text("sops: {}\n", encoding="utf-8")
    sops = repo / ".sops.yaml"
    sops.write_text(
        "# keep\n"
        "creation_rules:\n"
        "  - path_regex: envs/env-test/secrets/.*\\.sops\\.yaml$\n"
        "    age: age1old\n"
        "  - path_regex: envs/other/secrets/.*\\.sops\\.yaml$\n"
        "    age: age1keep\n",
        encoding="utf-8",
    )
    updated = module.set_sops_recipient(sops.read_text(encoding="utf-8"), "env-test", public)
    assert f"age: {public}" in updated
    assert "age: age1keep" in updated
    assert "# keep" in updated
    assert "age: age1old" not in updated
    sops.write_text(updated, encoding="utf-8")
    module.delete_env_secrets(repo, "env-test")
    assert not secret.exists()
    assert kept.read_text(encoding="utf-8") == "sops: {}\n"


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


def test_existing_age_key_mismatch_warns_and_keeps_swap_default_no(
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
    (env / "secrets").mkdir()
    secret = env / "secrets" / "app.sops.yaml"
    secret.write_text("sops: {}\n", encoding="utf-8")
    cluster = env / "values" / "cluster-vars.yaml"
    cluster.write_text("ssh_user: ops\nage_key_file: ~/.config/sops/age/keys.txt\n", encoding="utf-8")
    generated = env / "flux" / "generated" / "cluster-vars-configmap.yaml"
    generated.parent.mkdir(parents=True)
    generated.write_text("stale\n", encoding="utf-8")
    sops = repo / ".sops.yaml"
    sops.write_text(
        "creation_rules:\n"
        "  - path_regex: envs/env-test/secrets/.*\\.sops\\.yaml$\n"
        "    age: age1old\n",
        encoding="utf-8",
    )
    prompts: list[tuple[str, bool]] = []

    def fake_confirm(question: str, default_yes: bool = False) -> bool:
        prompts.append((question, default_yes))
        return False

    monkeypatch.setattr(module, "confirm", fake_confirm)
    module.sync_age_key(repo, "env-test")
    out = capsys.readouterr().out
    assert "another env" in out
    assert "SOPS_AGE_KEY_FILE" in out
    assert prompts == [
        ("Set the recipient to this public key and delete this env's encrypted secrets?", False)
    ]
    assert "age: age1old" in sops.read_text(encoding="utf-8")
    assert secret.read_text(encoding="utf-8") == "sops: {}\n"
    assert public.startswith("age1")
    from render_cluster_vars_configmap import render_document

    assert generated.read_text(encoding="utf-8") == render_document(cluster)


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
