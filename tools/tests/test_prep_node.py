"""prep-node env edits, DNS fail-fast, netplan rendering, and age keys."""

from __future__ import annotations

import importlib.machinery
import importlib.util
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
      data_disk:
        id: /dev/disk/by-id/example-cp-1
        wipe_confirmed: true
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
      data_disk:
        id: /dev/disk/by-id/example-cp-1
        wipe_confirmed: true
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


def test_render_netplan_pins_the_current_address() -> None:
    module = _load()
    rendered = module.render_netplan("eth0", "192.0.2.10/24", "192.0.2.1", ["192.0.2.53"])
    assert rendered == (
        "network:\n"
        "  version: 2\n"
        "  ethernets:\n"
        "    eth0:\n"
        "      dhcp4: false\n"
        "      addresses:\n"
        "        - 192.0.2.10/24\n"
        "      routes:\n"
        "        - to: default\n"
        "          via: 192.0.2.1\n"
        "      nameservers:\n"
        "        addresses:\n"
        "          - 192.0.2.53\n"
    )


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
