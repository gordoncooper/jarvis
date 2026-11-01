"""Restore drill uses the real restore target and always cleans up. No cluster."""

from __future__ import annotations

import hashlib
import json
import signal
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from support import REPO_ROOT, load_tool

PAYLOAD = b"fixture payload"
SUM = hashlib.sha256(PAYLOAD.hex().encode()).hexdigest()
OTHER = "b" * 64

drill_tool = load_tool("drill_tool", REPO_ROOT / "tools" / "drill")


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
        interrupt: str | int | None = None,
        cleanup_fails: bool = False,
        namespace_delete_fails: bool = False,
        marker: str = "env-test",
        cleanup_signal: int | None = None,
        etcd_restored_sum: str = SUM,
        leftover_pods: set[str] | None = None,
        repo_exists: bool = False,
        etcd_image: str = "restic/restic@sha256:" + "a" * 64,
    ) -> None:
        self.hold = hold
        self.namespace = namespace
        self.reader_sum = reader_sum
        self.interrupt = interrupt
        self.cleanup_fails = cleanup_fails
        self.namespace_delete_fails = namespace_delete_fails
        self.marker = marker
        self.cleanup_signal = cleanup_signal
        self.etcd_restored_sum = etcd_restored_sum
        self.repo_exists = repo_exists
        self.etcd_image = etcd_image
        self.calls: list[list[str]] = []
        self.applied: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.cleanup_applied = False
        self.pods = {
            (drill_tool.BACKUP_NAMESPACE, name)
            for name in (leftover_pods or set())
        }
        self.pod_errors: dict[tuple[str, str], str] = {}
        self.secrets = {
            drill_tool.BACKUP_NAMESPACE: {"backups-restic"},
            drill_tool.NAMESPACE: {"restic-drill"},
        }

    def kubectl(
        self, args: Sequence[str], input: str | None = None
    ) -> CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["get", "configmap", "cluster-vars"]:
            return _completed(argv, {"data": {"jarvis_env": self.marker}})
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
                                                "image": self.etcd_image,
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
        if argv[:2] in (["apply", "-f"], ["create", "-f"]):
            assert input is not None
            body = yaml.safe_load(input)
            self.applied.append(body)
            if body["kind"] == "Namespace":
                self.namespace = True
            if body["kind"] == "Pod":
                name = body["metadata"]["name"]
                namespace = body["metadata"]["namespace"]
                key = (namespace, name)
                if argv[0] == "create" and key in self.pods:
                    return _completed(
                        argv, "", returncode=1, stderr=f'pod "{name}" already exists'
                    )
                self.pods.add(key)
                if name == drill_tool.CLEANUP_POD:
                    self.cleanup_applied = True
                for container in body["spec"]["containers"]:
                    for env in container.get("env") or []:
                        secret = ((env.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
                        if secret and secret not in self.secrets.get(namespace, set()):
                            self.pod_errors[(namespace, name)] = (
                                f'secret "{secret}" not found'
                            )
            return _completed(argv, "applied\n")
        if argv[:2] == ["get", "pod"]:
            name = argv[2]
            namespace = argv[argv.index("-n") + 1]
            if (namespace, name) not in self.pods:
                return _completed(
                    argv,
                    "",
                    returncode=1,
                    stderr=f'Error from server (NotFound): pod "{name}" not found',
                )
            if self.interrupt is not None and name == drill_tool.READER:
                if self.interrupt == "keyboard":
                    raise KeyboardInterrupt
                handler = signal.getsignal(int(self.interrupt))
                assert callable(handler)
                handler(int(self.interrupt), None)
            pod_error = self.pod_errors.get((namespace, name))
            if pod_error:
                return _completed(
                    argv,
                    {
                        "status": {
                            "phase": "Pending",
                            "containerStatuses": [
                                {
                                    "state": {
                                        "waiting": {
                                            "reason": "CreateContainerConfigError",
                                            "message": pod_error,
                                        }
                                    }
                                }
                            ],
                        }
                    },
                )
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
                    argv,
                    f"ETCD {SUM} {self.etcd_restored_sum}"
                    " etcd-snapshot-fixture\n",
                )
            if name == drill_tool.PROBE_POD:
                return _completed(
                    argv, "PRESENT\n" if self.repo_exists else "ABSENT\n"
                )
            return _completed(argv)
        if argv[:2] == ["get", "replicationsource"]:
            return _completed(argv, {"status": {"lastSyncTime": "2026-10-31T00:00:00Z"}})
        if argv[0] == "annotate":
            return _completed(argv, "annotated\n")
        if argv[0] == "delete":
            self.deleted.append((argv[1], argv[2]))
            if argv[1] == "namespace":
                if self.cleanup_signal is not None:
                    signum = self.cleanup_signal
                    self.cleanup_signal = None
                    handler = signal.getsignal(signum)
                    assert callable(handler)
                    handler(signum, None)
                if self.namespace_delete_fails:
                    return _completed(
                        argv, "", returncode=1, stderr="timed out waiting for namespace"
                    )
                self.namespace = False
                self.pods = {
                    key for key in self.pods if key[0] != drill_tool.NAMESPACE
                }
            if argv[1] == "pod":
                namespace = argv[argv.index("-n") + 1]
                self.pods.discard((namespace, argv[2]))
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
    assert ("pod", drill_tool.ETCD_POD) in cluster.deleted
    assert ("pod", drill_tool.PROBE_POD) in cluster.deleted
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
    etcd_pod = next(
        body
        for body in cluster.applied
        if body.get("kind") == "Pod"
        and body["metadata"]["name"] == drill_tool.ETCD_POD
    )
    assert etcd_pod["metadata"]["namespace"] == drill_tool.BACKUP_NAMESPACE
    assert not any(call[:2] == ["get", "secret"] for call in cluster.calls)
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
        (Cluster(interrupt="keyboard"), 0, "interrupted"),
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
        (
            Cluster(namespace=True),
            "run make drill-cleanup ENV=env-test first.",
        ),
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
    output = capsys.readouterr().out
    assert "FAIL drill: make restore failed" in output
    assert "RECOVERY make drill-cleanup ENV=env-test" in output


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_process_signals_cleanup_and_return_shell_signal_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signum: int,
) -> None:
    cluster = Cluster(interrupt=signum)
    code, _ = _run(tmp_path, monkeypatch, cluster)
    assert code == 128 + signum
    _assert_cleanup(cluster)
    assert signal.Signals(signum).name in capsys.readouterr().out


def test_wrong_cluster_marker_refuses_before_drill_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster(marker="wrong")
    code, restores = _run(tmp_path, monkeypatch, cluster)
    assert code != 0
    assert restores == []
    assert cluster.calls == [
        ["get", "configmap", "cluster-vars", "-n", "flux-system", "-o", "json"]
    ]
    assert "environment marker is not ENV=env-test" in capsys.readouterr().out


def test_namespaced_secret_error_fails_fast() -> None:
    cluster = Cluster()
    pod = drill_tool._etcd_pod(
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
            "nodeSelector": {},
            "snapshotHostPath": {"path": "/fixture/etcd-snapshots"},
        }
    )
    pod["metadata"]["namespace"] = drill_tool.NAMESPACE
    drill_tool._apply(cluster.kubectl, pod, "fixture")
    with pytest.raises(drill_tool.DrillError, match="secret.*not found"):
        drill_tool._pod_succeeded(
            cluster.kubectl, drill_tool.ETCD_POD, drill_tool.NAMESPACE
        )


def test_unschedulable_pod_fails_fast() -> None:
    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        assert input is None
        return _completed(
            args,
            {
                "status": {
                    "phase": "Pending",
                    "conditions": [
                        {
                            "type": "PodScheduled",
                            "status": "False",
                            "reason": "Unschedulable",
                            "message": "no matching node",
                        }
                    ],
                }
            },
        )

    with pytest.raises(drill_tool.DrillError, match="unschedulable"):
        drill_tool._pod_succeeded(kubectl, "fixture", "fixture")


def test_namespace_cleanup_timeout_is_bounded_and_preserves_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster(namespace_delete_fails=True)
    code, _ = _run(tmp_path, monkeypatch, cluster, restore_rc=1)
    assert code != 0
    namespace_delete = next(
        call
        for call in cluster.calls
        if call[:3] == ["delete", "namespace", drill_tool.NAMESPACE]
    )
    assert f"--timeout={drill_tool.DELETE_TIMEOUT_SECONDS}s" in namespace_delete
    output = capsys.readouterr().out
    assert "FAIL drill: make restore failed" in output
    assert "RECOVERY make drill-cleanup ENV=env-test" in output


@pytest.mark.parametrize(
    "leftover",
    [drill_tool.ETCD_POD, drill_tool.CLEANUP_POD, drill_tool.PROBE_POD],
)
def test_preflight_refuses_leftover_backup_namespace_drill_pods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    leftover: str,
) -> None:
    cluster = Cluster(leftover_pods={leftover})
    code, restores = _run(tmp_path, monkeypatch, cluster)
    assert code != 0
    assert restores == []
    assert (
        "FAIL drill: run make drill-cleanup ENV=env-test first."
        in capsys.readouterr().out
    )


def test_preflight_refuses_leftover_repository_path_and_cleans_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster(repo_exists=True)
    code, restores = _run(tmp_path, monkeypatch, cluster)
    assert code != 0
    assert restores == []
    assert ("pod", drill_tool.PROBE_POD) in cluster.deleted
    probe = next(
        body
        for body in cluster.applied
        if body.get("kind") == "Pod"
        and body["metadata"]["name"] == drill_tool.PROBE_POD
    )
    mount = probe["spec"]["containers"][0]["volumeMounts"][0]
    assert mount == {"name": "data", "mountPath": "/data", "readOnly": True}
    assert probe["spec"]["volumes"][0]["hostPath"]["type"] == "Directory"
    assert (
        "FAIL drill: run make drill-cleanup ENV=env-test first."
        in capsys.readouterr().out
    )


def _run_cleanup_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cluster: Cluster,
    *,
    repo_marker: str = "env-test",
) -> int:
    _write_env(tmp_path)
    if repo_marker != "env-test":
        values = tmp_path / "envs" / "env-test" / "values" / "cluster-vars.yaml"
        values.write_text(f"jarvis_env: {repo_marker}\n", encoding="utf-8")
    monkeypatch.setattr(drill_tool, "REPO_ROOT", tmp_path)
    return drill_tool.run(cluster.kubectl, env="env-test", cleanup_only=True)


def test_cleanup_only_checks_markers_and_deletes_all_drill_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster(
        namespace=True,
        leftover_pods={drill_tool.ETCD_POD, drill_tool.CLEANUP_POD},
    )
    assert _run_cleanup_only(tmp_path, monkeypatch, cluster) == 0
    assert ("namespace", drill_tool.NAMESPACE) in cluster.deleted
    assert ("pod", drill_tool.ETCD_POD) in cluster.deleted
    assert ("pod", drill_tool.CLEANUP_POD) in cluster.deleted
    assert ("pod", drill_tool.PROBE_POD) in cluster.deleted
    cleanup = next(
        body
        for body in cluster.applied
        if body.get("kind") == "Pod"
        and body["metadata"]["name"] == drill_tool.CLEANUP_POD
    )
    assert "ns-platform-drill/pvc-drill" in cleanup["spec"]["containers"][0]["command"][2]
    marker_at = cluster.calls.index(
        ["get", "configmap", "cluster-vars", "-n", "flux-system", "-o", "json"]
    )
    first_delete = next(
        index for index, call in enumerate(cluster.calls) if call[0] == "delete"
    )
    assert marker_at < first_delete
    assert "PASS drill cleanup complete" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("repo_marker", "cluster_marker"),
    [("wrong", "env-test"), ("env-test", "wrong")],
)
def test_cleanup_only_refuses_wrong_env_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_marker: str,
    cluster_marker: str,
) -> None:
    cluster = Cluster(
        marker=cluster_marker,
        namespace=True,
        leftover_pods={drill_tool.ETCD_POD},
    )
    assert _run_cleanup_only(
        tmp_path, monkeypatch, cluster, repo_marker=repo_marker
    ) != 0
    assert not any(call[0] == "delete" for call in cluster.calls)


def _mutating(calls: list[list[str]]) -> list[list[str]]:
    verbs = {"annotate", "apply", "create", "delete", "patch", "scale"}
    return [call for call in calls if call and call[0] in verbs]


def test_trailing_restic_image_fails_before_any_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R33: a restic image with trailing text changes nothing, including cleanup-only."""
    image = "restic/restic@sha256:" + ("a" * 64) + "\n"
    drill = Cluster(etcd_image=image)
    code, restores = _run(tmp_path / "drill", monkeypatch, drill)
    assert code != 0
    assert restores == []
    assert _mutating(drill.calls) == []
    assert "must be restic/restic@sha256:" in capsys.readouterr().out

    cleanup = Cluster(namespace=True, etcd_image=image)
    assert _run_cleanup_only(tmp_path / "cleanup", monkeypatch, cleanup) != 0
    assert _mutating(cleanup.calls) == []
    assert "must be restic/restic@sha256:" in capsys.readouterr().out


def test_etcd_checksum_mismatch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster(etcd_restored_sum=OTHER)
    code, _ = _run(tmp_path, monkeypatch, cluster)
    assert code != 0
    assert "etcd snapshot checksum mismatch" in capsys.readouterr().out
    _assert_cleanup(cluster)


def test_second_signal_during_cleanup_preserves_original_exit_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster(interrupt=signal.SIGTERM, cleanup_signal=signal.SIGHUP)
    code, _ = _run(tmp_path, monkeypatch, cluster)
    assert code == 128 + signal.SIGTERM
    output = capsys.readouterr().out
    assert "FAIL drill: interrupted by SIGTERM" in output
    assert "RECOVERY make drill-cleanup ENV=env-test" in output
