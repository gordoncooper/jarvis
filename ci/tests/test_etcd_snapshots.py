"""Two etcd-snapshots copies at once (the schedule beside a manual run) both succeed: each waits for
the other's lock. The repository already exists; two inits at once are a documented limit
(core/platform/backups/CONTRACT.md).

Runs the CronJob's own script with the pinned restic against the pinned rest-server.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import yaml
from conftest import require_cli

from support import REPO_ROOT, free_port, wait_listening

MANIFEST = REPO_ROOT / "core/platform/backups/etcd-snapshots.yaml"


def test_two_copies_at_once_both_succeed(tmp_path: Path) -> None:
    """Rule: a second copy started while one runs waits for its lock (--retry-lock), and both succeed."""
    rest_server, restic = require_cli("rest-server"), require_cli("restic")
    container = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "etcd-snapshot-1").write_bytes(secrets.token_bytes(4096))
    script = container["command"][-1].replace("/snapshots", str(snapshots))
    for key in ("daily", "weekly", "monthly"):
        script = script.replace("${backup_retention_" + key + "}", "1")
    port = free_port()
    server = subprocess.Popen([rest_server, "--path", str(tmp_path / "data"), "--no-auth", "--listen", f"127.0.0.1:{port}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        wait_listening(port, server)
        env = {**os.environ, "PATH": f"{Path(restic).parent}:{os.environ['PATH']}", "RESTIC_PASSWORD": "throwaway",
               "RESTIC_REPOSITORY": f"rest:http://127.0.0.1:{port}/host-etcd-snapshots", "RESTIC_HOST": "etcd-snapshots"}  # fmt: skip
        subprocess.run([restic, "init"], env={**env, "RESTIC_CACHE_DIR": str(tmp_path / "cache-init")},
                       capture_output=True, check=True)  # fmt: skip
        runs = [subprocess.Popen(["sh", "-c", script], env={**env, "RESTIC_CACHE_DIR": str(tmp_path / f"cache-{i}")},
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for i in range(2)]  # fmt: skip
        outputs = [run.communicate(timeout=120)[0] for run in runs]
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert [run.returncode for run in runs] == [0, 0], outputs
