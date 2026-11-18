"""Two etcd-snapshots copies at once (the schedule beside a manual run) both succeed: each waits for
the other's lock. The repository already exists; two inits at once are a documented limit
(core/platform/backups/CONTRACT.md).

Runs the CronJob's own script with the pinned restic against the pinned rest-server.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import yaml
from conftest import require_cli

from support import REPO_ROOT

MANIFEST = REPO_ROOT / "core/platform/backups/etcd-snapshots.yaml"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_two_copies_at_once_both_succeed(tmp_path: Path) -> None:
    rest_server, restic = require_cli("rest-server"), require_cli("restic")
    container = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "etcd-snapshot-1").write_bytes(secrets.token_bytes(4096))
    script = container["command"][-1].replace("/snapshots", str(snapshots))
    for key in ("daily", "weekly", "monthly"):
        script = script.replace("${backup_retention_" + key + "}", "1")
    port = _port()
    server = subprocess.Popen([rest_server, "--path", str(tmp_path / "data"), "--no-auth", "--listen", f"127.0.0.1:{port}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        env = {**os.environ, "PATH": f"{Path(restic).parent}:{os.environ['PATH']}", "RESTIC_PASSWORD": "throwaway",
               "RESTIC_REPOSITORY": f"rest:http://127.0.0.1:{port}/host-etcd-snapshots", "RESTIC_HOST": "etcd-snapshots"}  # fmt: skip
        subprocess.run([restic, "init"], env={**env, "RESTIC_CACHE_DIR": str(tmp_path / "cache-init")},
                       capture_output=True, check=True)  # fmt: skip
        runs = [subprocess.Popen(["sh", "-c", script], env={**env, "RESTIC_CACHE_DIR": str(tmp_path / f"cache-{i}")},
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for i in range(2)]  # fmt: skip
        results = [(run.wait(timeout=120), run.stdout.read() if run.stdout else "") for run in runs]
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert [code for code, _ in results] == [0, 0], results
