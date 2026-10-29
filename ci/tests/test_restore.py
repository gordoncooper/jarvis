"""Restore pauses the source, then writes a Direct destination. No cluster."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import zlib
from collections.abc import Sequence
from importlib.machinery import SourceFileLoader
from pathlib import Path
from subprocess import CompletedProcess

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKSUM = "a" * 64
OTHER = "b" * 64

_RESTORE = SourceFileLoader("restore_tool", str(REPO_ROOT / "tools" / "restore"))
_RESTORE_SPEC = importlib.util.spec_from_loader(_RESTORE.name, _RESTORE)
assert _RESTORE_SPEC is not None
restore_tool = importlib.util.module_from_spec(_RESTORE_SPEC)
_RESTORE.exec_module(restore_tool)

_P0B = SourceFileLoader("verify_p0b", str(REPO_ROOT / "tools" / "verify" / "p0b"))
_P0B_SPEC = importlib.util.spec_from_loader(_P0B.name, _P0B)
assert _P0B_SPEC is not None
verify_p0b = importlib.util.module_from_spec(_P0B_SPEC)
_P0B.exec_module(verify_p0b)


def _completed(
    args: Sequence[str], stdout: str, *, returncode: int = 0, stderr: str = ""
) -> CompletedProcess[str]:
    return CompletedProcess(list(args), returncode, stdout, stderr)


def _pvc(
    namespace: str,
    name: str,
    *,
    volume: str = "pv",
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> dict:
    meta: dict = {
        "name": name,
        "namespace": namespace,
        "creationTimestamp": "2026-10-29T00:00:10Z",
    }
    if annotations:
        meta["annotations"] = annotations
    if labels:
        meta["labels"] = labels
    return {"metadata": meta, "spec": {"volumeName": volume}}


class _Cluster:
    def __init__(self, items: list[dict], *, will_pause: bool) -> None:
        self.items = items
        self.will_pause = will_pause
        self.calls: list[list[str]] = []
        self.applied: list[dict] = []
        self.paused: set[str] = set()

    def kubectl(self, args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        if argv[:2] == ["get", "pvc"]:
            items = self.items
            if "-n" in argv:
                namespace = argv[argv.index("-n") + 1]
                items = [
                    item for item in items if item["metadata"]["namespace"] == namespace
                ]
            return _completed(argv, json.dumps({"items": items}))
        if argv[:2] == ["annotate", "pvc"] and "backup=paused" in argv:
            self.paused.add(argv[2])
            return _completed(argv, "annotated\n")
        if argv[:2] == ["annotate", "pvc"] and "backup-" in argv:
            return _completed(argv, "annotated\n")
        if argv[:2] == ["get", "replicationsource"]:
            paused = self.will_pause and argv[2] in self.paused
            return _completed(argv, json.dumps({"spec": {"paused": paused}}))
        if argv[:2] == ["get", "job"]:
            return _completed(argv, json.dumps({"spec": {"parallelism": 0}, "status": {}}))
        if argv[:2] == ["get", "secret"]:
            return _completed(argv, f"secret/{argv[2]}\n")
        if argv[:2] == ["apply", "-f"]:
            assert input is not None
            self.applied.append(yaml.safe_load(input))
            return _completed(argv, "applied\n")
        if argv[:2] == ["get", "replicationdestination"]:
            manual = self.applied[-1]["spec"]["trigger"]["manual"]
            return _completed(argv, json.dumps({"status": {"lastManualSync": manual}}))
        if argv[:2] == ["delete", "replicationdestination"]:
            return _completed(argv, "deleted\n")
        return _completed(argv, "", returncode=1, stderr=f"unexpected kubectl: {argv}")


def _boom(_seconds: float) -> None:
    raise AssertionError("slept")


def test_restore_pauses_before_direct_destination(capsys) -> None:
    env = os.environ.copy()
    env.pop("APP", None)
    missing = subprocess.run(
        ["make", "restore"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert missing.returncode != 0
    assert "APP is required" in missing.stderr

    long_name = "p" * 60
    assert restore_tool.job_name("drill") == "volsync-src-drill"
    assert restore_tool.job_name(long_name) == (
        "volsync-src-" + f"{zlib.crc32(long_name.encode()) & 0xFFFFFFFF:08x}"
    )

    items = [
        _pvc("demo", "drill"),
        _pvc("other", "keep"),
        _pvc("demo", "scratch", annotations={"backup": "none"}),
        _pvc("demo", "mover", labels={"app.kubernetes.io/created-by": "volsync"}),
        _pvc("demo", "pending", volume=""),
    ]
    cluster = _Cluster(items, will_pause=True)
    assert (
        restore_tool.run(
            cluster.kubectl,
            app="demo",
            all_pvcs=False,
            monotonic=lambda: 0.0,
            sleep=_boom,
            now=lambda: 1000.0,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "PASS restore demo/drill" in out
    assert "other/keep" not in out
    assert cluster.applied[0]["metadata"]["name"] == "restore-drill"
    restic = cluster.applied[0]["spec"]["restic"]
    assert restic["copyMethod"] == "Direct"
    assert restic["enableFileDeletion"] is True
    assert restic["destinationPVC"] == "drill"
    assert restic["repository"] == "restic-drill"
    assert restic["restoreAsOf"] == "2026-10-29T00:00:09Z"
    assert set(restic) == {
        "destinationPVC",
        "repository",
        "copyMethod",
        "enableFileDeletion",
        "restoreAsOf",
    }
    assert cluster.applied[0]["spec"]["trigger"] == {"manual": "restore-1000"}
    assert "volumeSnapshotClassName" not in cluster.applied[0]["spec"]

    def at(pred) -> int:
        for index, call in enumerate(cluster.calls):
            if pred(call):
                return index
        raise AssertionError("missing call")

    pause_at = at(lambda call: "backup=paused" in call)
    apply_at = at(lambda call: call[:2] == ["apply", "-f"])
    delete_at = at(lambda call: call[:2] == ["delete", "replicationdestination"])
    resume_at = at(lambda call: "backup-" in call)
    assert pause_at < apply_at < delete_at < resume_at
    assert any(call[2] == "volsync-src-drill" for call in cluster.calls if call[:2] == ["get", "job"])

    stuck = _Cluster(items, will_pause=False)
    assert (
        restore_tool.run(
            stuck.kubectl,
            app="demo",
            all_pvcs=False,
            monotonic=lambda: 0.0,
            sleep=_boom,
            now=lambda: 1000.0,
            pause_budget_s=0,
        )
        != 0
    )
    assert stuck.applied == []
    assert "backup-" not in [arg for call in stuck.calls for arg in call]
    assert "did not pause" in capsys.readouterr().out

    everyone = _Cluster(items, will_pause=True)
    assert (
        restore_tool.run(
            everyone.kubectl,
            app=None,
            all_pvcs=True,
            monotonic=lambda: 0.0,
            sleep=_boom,
            now=lambda: 1000.0,
        )
        == 0
    )
    restored = {doc["metadata"]["namespace"] + "/" + doc["spec"]["restic"]["destinationPVC"] for doc in everyone.applied}
    assert restored == {"demo/drill", "other/keep"}


def _write_env(root: Path) -> str:
    env = "env-test"
    values = root / "envs" / env / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text("dns_suffix: test.example\n", encoding="utf-8")
    return env


def _log(second: str, *, snapshot: bool) -> str:
    lines = [
        f"{CHECKSUM}  /data/payload",
        'persistentvolumeclaim "drill" deleted',
        "restore demo/drill as-of 2026-10-29T00:00:09Z",
        f"{second}  /data/payload",
    ]
    if snapshot:
        lines.append("file:///backup/etcd-snapshots/etcd-snapshot-node-1710000000")
    return "\n".join(lines) + "\n"


def test_verify_p0b_reads_drill_log(tmp_path: Path, capsys) -> None:
    env = _write_env(tmp_path)
    log = tmp_path / "drill.log"
    log.write_text(_log(CHECKSUM, snapshot=True), encoding="utf-8")

    def fetch(host: str) -> tuple[int, str]:
        assert host == "testapp.test.example"
        return 0, "drill-ok\n"

    def issuer(host: str) -> tuple[int, str]:
        assert host == "testapp.test.example"
        return 0, "issuer=CN = test-ca"

    assert verify_p0b.run(tmp_path, env, log, fetch, issuer) == 0
    out = capsys.readouterr().out
    assert "PASS lan curl https://testapp.test.example/" in out
    assert "drill-ok" in out
    assert "PASS cert issuer issuer=CN = test-ca" in out
    assert f"PASS restore checksum {CHECKSUM}" in out
    assert "tailnet curl of https://testapp.test.example/ [G]:" in out
    assert out.splitlines()[-1] == "automatic checks passed; 1 owner items remain"

    log.write_text(_log(OTHER, snapshot=True), encoding="utf-8")
    assert verify_p0b.run(tmp_path, env, log, fetch, issuer) != 0
    bad = capsys.readouterr().out
    assert "FAIL drill log:" in bad
    assert "automatic checks passed" not in bad

    log.write_text(_log(CHECKSUM, snapshot=False), encoding="utf-8")
    assert verify_p0b.run(tmp_path, env, log, fetch, issuer) != 0
    assert "etcd snapshot" in capsys.readouterr().out
