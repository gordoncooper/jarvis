"""prep-node env edits and the DNS fail-fast."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

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
