"""Restore drill uses the real restore target and always cleans up. No cluster."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Sequence
from importlib.machinery import SourceFileLoader
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = b"fixture payload"
SUM = hashlib.sha256(PAYLOAD.hex().encode()).hexdigest()
OTHER = "b" * 64

_LOADER = SourceFileLoader("drill_tool", str(REPO_ROOT / "tools" / "drill"))
_SPEC = importlib.util.spec_from_loader(_LOADER.name, _LOADER)
assert _SPEC is not None
drill_tool = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(drill_tool)


def _completed(
    args: Sequence[str], value: object = "", *, returncode: int = 0, stderr: str = ""
) -> CompletedProcess[str]:
    stdout = json.dumps(value) if not isinstance(value, str) else value
    return CompletedProcess(list(args), returncode, stdout, stderr)


def _write_env(root: Path) -> None:
    values = root / "envs" / "env-test" / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text(
        "jarvis_env: env-test\n", encoding="utf-8"
    )


class Cluster:
    def __init__(
        self,
        *,
        hold: bool = False,
        namespace: bool = False,
        reader_sum: str = SUM,
        interrupt: bool = False,
        cleanup_fails: bool = False,
    ) -> None:
        self.hold = hold
        self.namespace = namespace
        self.reader_sum = reader_sum
        self.interrupt = interrupt
        self.cleanup_fails = cleanup_fails
        self.calls: list[list[str]] = []
        self.applied: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.cleanup_applied = False

    def kubectl(
        self, args: Sequence[str], input: str | None = None
    ) -> CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["get", "configmap", "cluster-vars"]:
            return _completed(argv, {"data": {"jarvis_env": "env-test"}})
        if argv[:3] == ["get", "cronjob", "etcd-snapshots"]:
            return _completed(
                argv,
                {
                    "spec": {
                        "jobTemplate": {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "nodeSelector": {"role.test/control_plane": "true"},
                                        "containers": [
                                            {
                                                "image": "restic/restic@sha256:" + "a" * 64,
                                                "env": [
                                                    {
                                                        "name": "RESTIC_PASSWORD",
                                                        "valueFrom": {
                                                            "secretKeyRef": {
                                                                "name": "backups-restic",
                                                                "key": "RESTIC_PASSWORD",
                                                            }
                                                        },
                                                    }
                                                ],
                                            }
                                        ],
                                        "volumes": [
                                            {
                                                "name": "snapshots",
                                                "hostPath": {
                                                    "path": "/fixture/etcd-snapshots",
                                                    "type": "Directory",
                                                },
                                            }
                                        ],
                                    }
                                }
                            }
                        }
                    }
                },
            )
        if argv[:3] == ["get", "deployment", "restic"]:
            return _completed(
                argv,
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "nodeSelector": {"role.test/backup": "true"},
                                "containers": [
                                    {
                                        "volumeMounts": [
                                            {"name": "data", "mountPath": "/data"}
                                        ]
                                    }
                                ],
                                "volumes": [
                                    {
                                        "name": "data",
                                        "hostPath": {
                                            "path": "/fixture/restic",
                                            "type": "DirectoryOrCreate",
                                        },
                                    }
                                ],
                            }
                        }
                    }
                },
            )
        if argv[:3] == ["get", "configmap", drill_tool.HOLD_NAME]:
            if self.hold:
                return _completed(argv, {"data": {"repos": "existing"}})
            return _completed(
                argv,
                "",
                returncode=1,
                stderr="Error from server (NotFound): configmap not found",
            )
        if argv[:2] == ["get", "namespace"]:
            if self.namespace:
                return _completed(argv, {"metadata": {"name": drill_tool.NAMESPACE}})
            return _completed(
                argv,
                "",
                returncode=1,
                stderr="Error from server (NotFound): namespace not found",
            )
        if argv[:2] == ["apply", "-f"]:
            assert input is not None
            body = yaml.safe_load(input)
            self.applied.append(body)
            if body["kind"] == "Namespace":
                self.namespace = True
            if body["kind"] == "Pod" and body["metadata"]["name"] == drill_tool.CLEANUP_POD:
                self.cleanup_applied = True
            return _completed(argv, "applied\n")
        if argv[:2] == ["get", "pod"]:
            name = argv[2]
            if self.interrupt and name == drill_tool.READER:
                raise KeyboardInterrupt
            phase = (
                "Failed"
                if self.cleanup_fails and name == drill_tool.CLEANUP_POD
                else "Succeeded"
            )
            return _completed(argv, {"status": {"phase": phase}})
        if argv[0] == "logs":
            name = argv[1]
            if name == drill_tool.WRITER:
                return _completed(argv, f"VOLUME {SUM}\n")
            if name == drill_tool.READER:
                return _completed(argv, f"VOLUME {self.reader_sum}\n")
            if name == drill_tool.ETCD_POD:
                return _completed(
                    argv, f"ETCD {SUM} {SUM} etcd-snapshot-fixture\n"
                )
            return _completed(argv)
        if argv[:2] == ["get", "replicationsource"]:
            return _completed(argv, {"status": {"lastSyncTime": "2026-10-31T00:00:00Z"}})
        if argv[0] == "annotate":
            return _completed(argv, "annotated\n")
        if argv[0] == "delete":
            self.deleted.append((argv[1], argv[2]))
            if argv[1] == "namespace":
                self.namespace = False
            return _completed(argv, "deleted\n")
        return _completed(argv, "", returncode=1, stderr=f"unexpected {argv}")


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cluster: Cluster,
    *,
    restore_rc: int = 0,
) -> tuple[int, list[list[str]]]:
    _write_env(tmp_path)
    monkeypatch.setattr(drill_tool, "REPO_ROOT", tmp_path)
    restores: list[list[str]] = []

    def restore(args: Sequence[str]) -> CompletedProcess[str]:
        restores.append(list(args))
        return _completed(args, "PASS restore platform-drill/drill\n", returncode=restore_rc)

    return (
        drill_tool.run(
            cluster.kubectl,
            env="env-test",
            restore=restore,
            payload=PAYLOAD,
        ),
        restores,
    )


def _assert_cleanup(cluster: Cluster) -> None:
    assert cluster.cleanup_applied
    assert ("namespace", drill_tool.NAMESPACE) in cluster.deleted
    cleanup = next(
        body
        for body in cluster.applied
        if body.get("kind") == "Pod"
        and body["metadata"]["name"] == drill_tool.CLEANUP_POD
    )
    assert cleanup["metadata"]["namespace"] == drill_tool.BACKUP_NAMESPACE
    assert cleanup["spec"]["volumes"][0]["hostPath"]["path"] == "/fixture/restic"
    assert "ns-platform-drill/pvc-drill" in cleanup["spec"]["containers"][0]["command"][2]


def test_drill_calls_real_restore_and_cleans_repository_in_cluster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    code, restores = _run(tmp_path, monkeypatch, cluster)
    assert code == 0
    assert restores == [
        ["make", "restore", "ENV=env-test", "APP=platform-drill"]
    ]
    _assert_cleanup(cluster)
    output = capsys.readouterr().out
    assert f"ORIGINAL volume checksum {SUM}" in output
    assert f"RESTORED volume checksum {SUM}" in output
    assert f"ETCD snapshot etcd-snapshot-fixture checksum {SUM}" in output
    assert "PASS drill cleanup complete" in output


@pytest.mark.parametrize(
    ("cluster", "restore_rc", "message"),
    [
        (Cluster(reader_sum=OTHER), 0, "restored volume checksum mismatch"),
        (Cluster(), 1, "make restore failed"),
        (Cluster(interrupt=True), 0, "interrupted"),
    ],
)
def test_failure_and_interrupt_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: Cluster,
    restore_rc: int,
    message: str,
) -> None:
    code, _ = _run(
        tmp_path, monkeypatch, cluster, restore_rc=restore_rc
    )
    assert code != 0
    _assert_cleanup(cluster)
    assert message in capsys.readouterr().out


@pytest.mark.parametrize(
    ("cluster", "message"),
    [
        (Cluster(hold=True), "backup rebuild hold is present"),
        (Cluster(namespace=True), "namespace platform-drill already exists"),
    ],
)
def test_existing_hold_or_namespace_refuses_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cluster: Cluster,
    message: str,
) -> None:
    code, restores = _run(tmp_path, monkeypatch, cluster)
    assert code != 0
    assert restores == []
    assert not cluster.cleanup_applied
    assert message in capsys.readouterr().out


def test_cleanup_failure_prints_exact_env_bound_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster(cleanup_fails=True)
    code, _ = _run(tmp_path, monkeypatch, cluster, restore_rc=1)
    assert code != 0
    assert ("namespace", drill_tool.NAMESPACE) in cluster.deleted
    assert (
        "RECOVERY python3 tools/drill --env env-test --cleanup-only"
        in capsys.readouterr().out
    )
