"""jarvis drill on a fake cluster holding core's own backup manifests: it restores a scratch volume and the etcd
copy, and always cleans up."""

from __future__ import annotations

import copy
import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from jarvis.backups import drill as drill_tool
from support import REPO_ROOT, completed, write_env_marker, yaml_docs

PAYLOAD = b"fixture payload"
SUM = hashlib.sha256(PAYLOAD.hex().encode()).hexdigest()
OTHER = "b" * 64
_PLACEMENT = ("nodeSelector", "affinity", "tolerations")


def _deployed(path: str) -> list[dict]:
    """core's manifest at path as the cluster holds it: Flux has filled in the env's backup_path."""
    return yaml_docs((REPO_ROOT / path).read_text(encoding="utf-8").replace("${backup_path}", "/fixture"))


_CRONJOB = _deployed("core/platform/backups/etcd-snapshots.yaml")[0]
_RESTIC = next(doc for doc in _deployed("core/platform/backups/rest-server.yaml") if doc["kind"] == "Deployment")


def _pod_spec(doc: dict) -> dict:
    """The pod template's spec of a CronJob or a Deployment."""
    return (doc["spec"]["jobTemplate"]["spec"] if doc["kind"] == "CronJob" else doc["spec"])["template"]["spec"]


def _placement(spec: dict) -> dict:
    return {key: spec[key] for key in _PLACEMENT if key in spec}


def _variant(doc: dict, *, unplaced: bool = False, unsized: bool = False, image: str | None = None,
             **placement: object) -> dict:
    """A copy of the live doc, its pods unplaced (no nodeSelector, affinity or tolerations), unsized or running image if
    asked, and placed by placement too."""
    doc = copy.deepcopy(doc)
    spec = _pod_spec(doc)
    if unplaced:
        for key in _PLACEMENT:
            spec.pop(key, None)
    spec.update(placement)
    if unsized:
        del spec["containers"][0]["resources"]
    if image:
        spec["containers"][0]["image"] = image
    return doc


def _not_found(argv: list[str], kind: str, name: str) -> CompletedProcess[str]:
    return completed(argv, returncode=1, stderr=f'Error from server (NotFound): {kind} "{name}" not found')


_FORBIDDEN = "Error from server (Forbidden): the server does not allow access to the requested resource"


@dataclass
class Cluster:
    """kubectl and jarvis restore on a fake cluster: the env's marker, the live CronJob and Deployment (core's unless
    given; cronjob None: missing), the rebuild hold, and the drill's namespace and pods, each pod's log what its
    command prints. It records each call, and each but a read as a change: the object applied or created, or the
    argv."""

    marker: str = "env-test"
    cronjob: dict | None = field(default_factory=lambda: _CRONJOB)
    deployment: dict = field(default_factory=lambda: _RESTIC)
    hold: bool = False
    namespace: bool = False
    pods: set[tuple[str, str]] = field(default_factory=set)
    forbidden: str | None = None  # the name of an object no get may read
    probe_log: str = "ABSENT\n"
    writer_sum: str = SUM
    synced: bool | None = True  # None: Kyverno hasn't made the drill's ReplicationSource again
    restore_fails: str | None = None  # jarvis restore exits 1, saying this on stderr ("": nothing)
    reader_sum: str = SUM
    etcd_log: str = f"ETCD {SUM} {SUM} etcd-snapshot-fixture\n"  # the etcd pod fails without its ETCD line
    interrupt: int | None = None  # a signal while drill waits for its reader pod
    cleanup_signal: int | None = None  # a signal while cleanup deletes the namespace
    cleanup_fails: bool = False
    stuck: str | None = None  # the name of an object whose delete times out
    calls: list[list[str]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)

    def _objects(self) -> dict[tuple[str, str | None, str], dict]:
        """What a get finds, by kind, namespace and name."""
        objects = {(doc["kind"].lower(), doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
                   for doc in (self.cronjob, self.deployment) if doc}
        objects[("configmap", "flux-system", "cluster-vars")] = {"data": {"jarvis_env": self.marker}}
        if self.synced is not None:
            synced = {"lastSyncTime": "2026-10-31T00:00:00Z"} if self.synced else {}
            objects[("replicationsource", "platform-drill", "drill")] = {"status": synced}
        if self.hold:
            objects[("configmap", "kube-system", drill_tool.HOLD_NAME)] = {"data": {"repos": "ns-demo/pvc-data\n"}}
        if self.namespace:
            objects[("namespace", None, "platform-drill")] = {"metadata": {"name": "platform-drill"}}
        for namespace, name in self.pods:
            failed = (name == "jarvis-drill-repo-cleanup" and self.cleanup_fails) or (
                name == "drill-etcd-restore" and not self.etcd_log.startswith("ETCD "))
            objects[("pod", namespace, name)] = {"spec": {"nodeName": "node-a"},
                                                 "status": {"phase": "Failed" if failed else "Succeeded"}}
        return objects

    def kubectl(self, args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        namespace = argv[argv.index("-n") + 1] if "-n" in argv else None
        if argv[0] in ("apply", "create") and argv[1:] == ["-f", "-"]:
            doc = yaml.safe_load(input or "")
            meta = doc["metadata"]
            self.applied.append(doc)
            where = f"{meta['namespace']}/{meta['name']}" if "namespace" in meta else meta["name"]
            self.changes.append(f"{argv[0]} {doc['kind']} {where}")
            if doc["kind"] == "Namespace":
                self.namespace = True
            elif doc["kind"] == "Pod":
                if argv[0] == "create" and (meta["namespace"], meta["name"]) in self.pods:
                    return completed(argv, returncode=1, stderr=f'pod "{meta["name"]}" already exists')
                self.pods = {*self.pods, (meta["namespace"], meta["name"])}
            return completed(argv, "applied\n")
        if argv[0] not in ("get", "logs"):
            self.changes.append(shlex.join(argv))
            if argv[:2] == ["delete", "namespace"] and self.cleanup_signal:
                signum, self.cleanup_signal = self.cleanup_signal, None
                signal.getsignal(signum)(signum, None)
            if argv[0] == "delete" and argv[2] == self.stuck:
                stuck = f"timed out waiting for the condition on {argv[1]}s/{argv[2]}"
                return completed(argv, returncode=1, stderr=stuck)
            if argv[:2] == ["delete", "namespace"]:
                self.namespace = False
                self.pods = {pod for pod in self.pods if pod[0] != "platform-drill"}
            elif argv[:2] == ["delete", "pod"]:
                self.pods = self.pods - {(namespace, argv[2])}
            if argv[0] in ("annotate", "delete"):
                return completed(argv, "done\n")
            return completed(argv, returncode=1, stderr=f"unexpected kubectl: {argv}")
        if self.interrupt and argv[:3] == ["get", "pod", "drill-reader"] and (namespace, argv[2]) in self.pods:
            signal.getsignal(self.interrupt)(self.interrupt, None)
        if argv[0] == "get" and argv[2] == self.forbidden:
            return completed(argv, returncode=1, stderr=_FORBIDDEN)
        if argv[0] == "logs":
            logs = {"drill-writer": f"VOLUME {self.writer_sum}\n", "drill-reader": f"VOLUME {self.reader_sum}\n",
                    "drill-etcd-restore": self.etcd_log, "jarvis-drill-repo-probe": self.probe_log}
            if (namespace, argv[1]) in self.pods and argv[1] in logs:
                return completed(argv, logs[argv[1]])
            return _not_found(argv, "pods", argv[1])
        found = self._objects().get((argv[1], namespace, argv[2]))
        return completed(argv, found) if found is not None else _not_found(argv, argv[1], argv[2])

    def restore(self, args: Sequence[str]) -> CompletedProcess[str]:
        """jarvis restore as drill runs it, one more change. It prints its PASS line even when it fails (exit 1,
        restore_fails on stderr): drill goes by the exit code."""
        self.changes.append(shlex.join(args))
        return completed(args, "PASS restore platform-drill/drill\n", returncode=int(self.restore_fails is not None),
                         stderr=self.restore_fails or "")


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cluster: Cluster, *, repo_marker: str = "env-test",
         instance_marker: str | None = None, **flags: bool) -> int:
    """drill (cleanup-only, by flags) for env-test on cluster, from a product checkout whose env-test marker is
    repo_marker, and with INSTANCE naming an instance whose marker is instance_marker, if given. Its waits give up at
    once."""
    write_env_marker(tmp_path, "env-test", repo_marker)
    monkeypatch.setattr(drill_tool, "REPO_ROOT", tmp_path)
    if instance_marker is None:
        monkeypatch.delenv("INSTANCE", raising=False)
    else:
        write_env_marker(tmp_path / "instance", "env-test", instance_marker)
        monkeypatch.setenv("INSTANCE", str(tmp_path / "instance"))
    monkeypatch.setattr(drill_tool, "WAIT_SECONDS", 0)
    return drill_tool.run(cluster.kubectl, env="env-test", restore=cluster.restore, payload=PAYLOAD, **flags)


# What a drill prints up to its restore, after it, and after the restored volume's check.
_RESTORING = [f"ORIGINAL volume checksum {SUM}", "DELETE PVC platform-drill/drill", "NODE restoring onto node-a"]
_RESTORED = [*_RESTORING, "PASS restore platform-drill/drill"]
_NO_SNAPSHOTS = "FAIL platform-drill/drill: no eligible snapshots\n"
_RESTORE_FAILED = [*_RESTORED, _NO_SNAPSHOTS.strip()]
_READ = [*_RESTORED, f"RESTORED volume checksum {SUM}"]
_RECOVERY = "RECOVERY jarvis drill --cleanup-only --env env-test"
# What it changes up to each step, and its cleanup. Every delete waits and ignores a missing object; the namespace's,
# the etcd pod's and the probe's are bounded too: a stuck one fails instead of holding the drill.
_WAIT = "--wait=true --ignore-not-found=true"
_BOUNDED = f"{_WAIT} --timeout={drill_tool.DELETE_TIMEOUT_SECONDS}s"
_PROBE = ["create Pod platform-backups/jarvis-drill-repo-probe",
          f"delete pod jarvis-drill-repo-probe -n platform-backups {_BOUNDED}"]
_TO_WRITER = [*_PROBE, "apply Namespace platform-drill", "apply PersistentVolumeClaim platform-drill/drill",
              "apply Pod platform-drill/drill-writer"]
_TO_SYNC = [*_TO_WRITER, f"delete replicationsource drill -n platform-drill {_WAIT}"]
_TO_RESTORE = [*_TO_SYNC, "annotate pvc drill -n platform-drill backup=paused --overwrite",
               f"delete pod drill-writer -n platform-drill {_WAIT}", f"delete pvc drill -n platform-drill {_WAIT}",
               "apply PersistentVolumeClaim platform-drill/drill",
               shlex.join([sys.executable, "-m", "jarvis.backups.restore", "--env", "env-test", "--namespace",
                           "platform-drill", "--node", "node-a"])]
_TO_READER = [*_TO_RESTORE, "apply Pod platform-drill/drill-reader"]
_TO_ETCD = [*_TO_READER, "create Pod platform-backups/drill-etcd-restore"]
_CLEANUP = [f"delete namespace platform-drill {_BOUNDED}",
            f"delete pod drill-etcd-restore -n platform-backups {_BOUNDED}",
            f"delete pod jarvis-drill-repo-probe -n platform-backups {_BOUNDED}",
            f"delete pod jarvis-drill-repo-cleanup -n platform-backups {_WAIT}",
            "apply Pod platform-backups/jarvis-drill-repo-cleanup",
            f"delete pod jarvis-drill-repo-cleanup -n platform-backups {_WAIT}"]


def test_drill_restores_a_scratch_volume_and_the_etcd_copy_then_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule: drill backs up a fresh scratch volume, restores it paused with jarvis restore on its node, checks it and
    the etcd copy, and cleans up, reading no Secret."""
    cluster = Cluster()
    assert _run(tmp_path, monkeypatch, cluster) == 0
    assert capsys.readouterr().out.splitlines() == [
        *_READ, f"ETCD snapshot etcd-snapshot-fixture checksum {SUM}", "PASS drill cleanup complete"]
    assert cluster.changes == [*_TO_ETCD, *_CLEANUP]
    assert {call[1] for call in cluster.calls if call[0] == "get"} == {
        "configmap", "cronjob", "deployment", "namespace", "pod", "replicationsource"}
    assert [doc["metadata"] for doc in cluster.applied if doc["kind"] != "Pod"] == [
        {"name": "platform-drill", "labels": {"jarvis.io/layer": "platform"}},
        {"name": "drill", "namespace": "platform-drill", "annotations": {}},
        {"name": "drill", "namespace": "platform-drill", "annotations": {"backup": "paused"}}]


_IMAGE = _pod_spec(_CRONJOB)["containers"][0]["image"]
_SIZED = _pod_spec(_CRONJOB)["containers"][0]["resources"]


@pytest.mark.parametrize(
    ("cronjob", "deployment", "resources"),
    [
        pytest.param(_CRONJOB, _RESTIC, _SIZED, id="core"),
        pytest.param(_variant(_CRONJOB, unplaced=True, unsized=True, image="restic/restic:0.17.3@sha256:" + "0" * 64,
                              nodeSelector={"role.env-00.example/control_plane": "true"}),
                     _variant(_RESTIC, unplaced=True, nodeSelector={"role.env-00.example/backup": "true"}), {},
                     id="older-core"),
        pytest.param(_variant(_CRONJOB, nodeSelector={"role.env-00.example/control_plane": "true"}),
                     _variant(_RESTIC, nodeSelector={"role.env-00.example/backup": "true"}), _SIZED,
                     id="every-placement-key"),
    ],
)
def test_drill_pods_run_cores_image_placed_as_the_live_templates(
    cronjob: dict, deployment: dict, resources: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: drill's pods run core's etcd copy image at the live CronJob's size, only the etcd pod naming a Secret; the
    etcd and repository pods run as root, placed as the live templates, on existing paths read-only unless cleaning."""
    etcd_copy, restic, never = _pod_spec(cronjob), _pod_spec(deployment), {"restartPolicy": "Never"}
    repository = next(volume for volume in restic["volumes"] if volume["name"] == "data")["hostPath"]["path"]
    repositories = [{"name": "data", "hostPath": {"path": repository, "type": "Directory"}}]
    snapshots = [{"name": "snapshots", "hostPath": etcd_copy["volumes"][0]["hostPath"]},
                 {"name": "restore", "emptyDir": {}}]
    scratch = {**never, "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "drill"}}]}
    on_backup = {**never, **_placement(restic), "volumes": repositories}
    data, read_only = {"name": "data", "mountPath": "/data"}, {"readOnly": True}
    cluster = Cluster(cronjob=cronjob, deployment=deployment)
    assert _run(tmp_path, monkeypatch, cluster) == 0
    pods = {doc["metadata"]["name"]: doc for doc in cluster.applied if doc["kind"] == "Pod"}
    containers = {name: pod["spec"].pop("containers") for name, pod in pods.items()}
    assert [(c["image"], c["resources"]) for [c] in containers.values()] == [(_IMAGE, resources)] * 5
    assert {name for name, [c] in containers.items() if c.get("securityContext") == {"runAsUser": 0}} == {
        "jarvis-drill-repo-probe", "drill-etcd-restore", "jarvis-drill-repo-cleanup"}
    assert {name: (pod["metadata"]["namespace"], pod["spec"]) for name, pod in pods.items()} == {
        "jarvis-drill-repo-probe": ("platform-backups", on_backup),
        "drill-writer": ("platform-drill", scratch),
        "drill-reader": ("platform-drill", scratch),
        "drill-etcd-restore": ("platform-backups", {**never, **_placement(etcd_copy), "volumes": snapshots}),
        "jarvis-drill-repo-cleanup": ("platform-backups", on_backup)}
    assert {name: each[0]["volumeMounts"] for name, each in containers.items()} == {
        "jarvis-drill-repo-probe": [{**data, **read_only}], "drill-writer": [data], "drill-reader": [data],
        "drill-etcd-restore": [{"name": "snapshots", "mountPath": "/snapshots", **read_only},
                               {"name": "restore", "mountPath": "/restore"}],
        "jarvis-drill-repo-cleanup": [data]}
    assert {name: c.get("env") for name, [c] in containers.items()} == {
        "jarvis-drill-repo-probe": None, "drill-reader": None, "jarvis-drill-repo-cleanup": None,
        "drill-writer": [{"name": "PAYLOAD", "value": PAYLOAD.hex()}],
        "drill-etcd-restore": etcd_copy["containers"][0]["env"]}
    assert "repo=/data/ns-platform-drill/pvc-drill;" in containers["jarvis-drill-repo-cleanup"][0]["command"][2]


@pytest.mark.parametrize(
    ("knobs", "code", "out", "changes"),
    [
        pytest.param({"writer_sum": OTHER}, 1, ["FAIL drill: writer checksum mismatch"], [*_TO_WRITER, *_CLEANUP],
                     id="writer-checksum-mismatch"),
        pytest.param({"synced": False}, 1, [_RESTORING[0], "FAIL drill: fresh volume backup: timed out"],
                     [*_TO_SYNC, *_CLEANUP], id="no-fresh-backup"),
        pytest.param({"synced": None}, 1, [_RESTORING[0], "FAIL drill: fresh volume backup: timed out"],
                     [*_TO_SYNC, *_CLEANUP], id="source-not-made-again"),
        pytest.param({"restore_fails": ""}, 1, [*_RESTORED, "FAIL drill: jarvis restore failed"],
                     [*_TO_RESTORE, *_CLEANUP], id="restore-fails"),
        pytest.param({"reader_sum": OTHER}, 1, [*_RESTORED, "FAIL drill: restored volume checksum mismatch"],
                     [*_TO_READER, *_CLEANUP], id="volume-checksum-mismatch"),
        pytest.param({"interrupt": signal.SIGINT}, 130, [*_RESTORED, "FAIL drill: interrupted"],
                     [*_TO_READER, *_CLEANUP], id="sigint"),
        pytest.param({"interrupt": signal.SIGTERM}, 143, [*_RESTORED, "FAIL drill: interrupted by SIGTERM"],
                     [*_TO_READER, *_CLEANUP], id="sigterm"),
        pytest.param({"interrupt": signal.SIGHUP}, 129, [*_RESTORED, "FAIL drill: interrupted by SIGHUP"],
                     [*_TO_READER, *_CLEANUP], id="sighup"),
        pytest.param({"etcd_log": f"ETCD {SUM} {OTHER} etcd-snapshot-fixture\n"}, 1,
                     [*_READ, "FAIL drill: etcd snapshot checksum mismatch"], [*_TO_ETCD, *_CLEANUP],
                     id="etcd-checksum-mismatch"),
        pytest.param({"etcd_log": f"{drill_tool.NOT_COPIED} etcd-snapshot-fixture\n"}, 1,
                     [*_READ, "FAIL drill: newest etcd snapshot etcd-snapshot-fixture is not yet in the restic copy;"
                      " run the drill again after the next etcd-snapshots run"], [*_TO_ETCD, *_CLEANUP],
                     id="etcd-not-copied"),
        pytest.param({"etcd_log": f"{drill_tool.NO_SNAPSHOT}\n"}, 1,
                     [*_READ, "FAIL drill: k3s has written no etcd snapshot yet; it writes one on its snapshot schedule"
                      " (k3s_etcd_snapshot_schedule_cron, default 00:00 and 12:00), and jarvis site takes one on a new"
                      " cluster. Run the drill again after the next etcd-snapshots run"], [*_TO_ETCD, *_CLEANUP],
                     id="no-etcd-snapshot"),
        pytest.param({"etcd_log": "open repository\nFatal: wrong password or no key found\n"}, 1,
                     [*_READ, "LOG drill-etcd-restore: open repository",
                      "LOG drill-etcd-restore: Fatal: wrong password or no key found",
                      "FAIL drill: pod platform-backups/drill-etcd-restore failed"], [*_TO_ETCD, *_CLEANUP],
                     id="etcd-pod-fails"),
        pytest.param({"restore_fails": _NO_SNAPSHOTS, "cleanup_fails": True}, 1,
                     [*_RESTORE_FAILED, "FAIL cleanup repository: pod platform-backups/jarvis-drill-repo-cleanup"
                      " failed", _RECOVERY, "FAIL drill: jarvis restore failed"], [*_TO_RESTORE, *_CLEANUP[:-1]],
                     id="repository-cleanup-fails"),
        pytest.param({"restore_fails": _NO_SNAPSHOTS, "stuck": "platform-drill"}, 1,
                     [*_RESTORE_FAILED, "FAIL cleanup namespace: delete namespace platform-drill/platform-drill: timed"
                      " out waiting for the condition on namespaces/platform-drill", _RECOVERY,
                      "FAIL drill: jarvis restore failed"], [*_TO_RESTORE, *_CLEANUP[:2]], id="namespace-delete-fails"),
        pytest.param({"interrupt": signal.SIGTERM, "cleanup_signal": signal.SIGHUP}, 143,
                     [*_RESTORED, "FAIL cleanup namespace: SIGHUP", _RECOVERY, "FAIL drill: interrupted by SIGTERM"],
                     [*_TO_READER, *_CLEANUP[:2]], id="second-signal"),
        pytest.param({"stuck": "drill-etcd-restore"}, 1,
                     [*_READ, f"ETCD snapshot etcd-snapshot-fixture checksum {SUM}", "FAIL cleanup etcd pod: delete pod"
                      " platform-backups/drill-etcd-restore: timed out waiting for the condition on"
                      " pods/drill-etcd-restore", _RECOVERY, "FAIL drill: cleanup incomplete"], [*_TO_ETCD, *_CLEANUP],
                     id="only-the-cleanup-fails"),
    ],
)
def test_drill_that_fails_after_it_starts_cleans_up(
    knobs: dict, code: int, out: list[str], changes: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule: a drill that fails once started (a check, its restore, a signal) cleans up and exits with that first
    failure, 128+n for a signal; a cleanup that can't finish prints the env-bound retry and fails a passed drill."""
    cluster = Cluster(**knobs)
    assert _run(tmp_path, monkeypatch, cluster) == code
    assert capsys.readouterr().out.splitlines() == out
    assert cluster.changes == changes


_MARKER_READ = ["get", "configmap", "cluster-vars", "-n", "flux-system", "-o", "json"]
_WRONG_REPOSITORY = "ENV=env-test does not match its repository marker"
_WRONG_CLUSTER = "refusing cluster whose environment marker is not ENV=env-test"


@pytest.mark.parametrize(
    ("run", "marker", "message", "calls"),
    [
        pytest.param({"repo_marker": "wrong"}, "env-test", _WRONG_REPOSITORY, [], id="repository-marker"),
        pytest.param({"repo_marker": "wrong", "cleanup_only": True}, "env-test", _WRONG_REPOSITORY, [],
                     id="repository-marker-cleanup-only"),
        pytest.param({"instance_marker": "wrong"}, "env-test", _WRONG_REPOSITORY, [], id="instance-marker"),
        pytest.param({}, "wrong", _WRONG_CLUSTER, [_MARKER_READ], id="cluster-marker"),
        pytest.param({"cleanup_only": True}, "wrong", _WRONG_CLUSTER, [_MARKER_READ], id="cluster-marker-cleanup-only"),
    ],
)
def test_drill_checks_both_env_markers_before_it_reads_the_cluster(
    run: dict, marker: str, message: str, calls: list[list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule: a drill or cleanup-only refuses an instance (the checkout INSTANCE names, else the product's) or a cluster
    whose env marker is not the env's before it reads anything else, so a drill's leftovers stay."""
    cluster = Cluster(marker=marker, namespace=True, pods={("platform-backups", "drill-etcd-restore")})
    assert _run(tmp_path, monkeypatch, cluster, **run) == 1
    assert capsys.readouterr().out == f"FAIL drill: {message}\n"
    assert cluster.calls == calls


_LEFTOVER = "run jarvis drill --cleanup-only --env env-test first."
_UNPLACED = "live backup workload has an unsupported pod template"
_NO_CRONJOB = 'read etcd-snapshots CronJob: Error from server (NotFound): cronjob "etcd-snapshots" not found'


@pytest.mark.parametrize(
    ("knobs", "flags", "message", "changes"),
    [
        pytest.param({"hold": True}, {}, "backup rebuild hold is present", [], id="hold"),
        pytest.param({"forbidden": drill_tool.HOLD_NAME}, {}, f"read backup rebuild hold: {_FORBIDDEN}", [],
                     id="hold-unreadable"),
        pytest.param({"namespace": True}, {}, _LEFTOVER, [], id="leftover-namespace"),
        pytest.param({"forbidden": "platform-drill"}, {}, f"read namespace platform-drill: {_FORBIDDEN}", [],
                     id="namespace-unreadable"),
        pytest.param({"pods": {("platform-backups", "drill-etcd-restore")}}, {}, _LEFTOVER, [],
                     id="leftover-etcd-pod"),
        pytest.param({"pods": {("platform-backups", "jarvis-drill-repo-cleanup")}}, {}, _LEFTOVER, [],
                     id="leftover-cleanup-pod"),
        pytest.param({"pods": {("platform-backups", "jarvis-drill-repo-probe")}}, {}, _LEFTOVER, [],
                     id="leftover-probe-pod"),
        pytest.param({"forbidden": "jarvis-drill-repo-probe"}, {},
                     f"read pod platform-backups/jarvis-drill-repo-probe: {_FORBIDDEN}", [], id="pod-unreadable"),
        pytest.param({"probe_log": "PRESENT\n"}, {}, _LEFTOVER, _PROBE, id="leftover-repository"),
        pytest.param({"probe_log": "\n"}, {}, "repository probe returned an invalid result", _PROBE,
                     id="repository-probe-unclear"),
        pytest.param({"cronjob": _variant(_CRONJOB, unplaced=True)}, {}, _UNPLACED, [], id="etcd-template-unplaced"),
        pytest.param({"deployment": _variant(_RESTIC, unplaced=True)}, {}, _UNPLACED, [],
                     id="restic-template-unplaced"),
        pytest.param({"cronjob": None}, {}, _NO_CRONJOB, [], id="no-etcd-cronjob"),
        pytest.param({"cronjob": None, "namespace": True}, {"cleanup_only": True}, _NO_CRONJOB, [],
                     id="no-etcd-cronjob-cleanup-only"),
    ],
)
def test_drill_refuses_before_any_change(
    knobs: dict, flags: dict, message: str, changes: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule: drill refuses a rebuild hold, a drill's leftovers (namespace, pods, or a repository, read by a probe pod it
    then deletes), any of them it can't read, and a live template it can't find or copy, before any other change."""
    cluster = Cluster(**knobs)
    assert _run(tmp_path, monkeypatch, cluster, **flags) == 1
    assert capsys.readouterr().out == f"FAIL drill: {message}\n"
    assert cluster.changes == changes


@pytest.mark.parametrize(
    ("knobs", "code", "out", "changes"),
    [
        pytest.param({}, 0, "PASS drill cleanup complete\n", _CLEANUP, id="leftovers-deleted"),
        pytest.param({"cleanup_fails": True}, 1,
                     "FAIL cleanup repository: pod platform-backups/jarvis-drill-repo-cleanup failed\n", _CLEANUP[:-1],
                     id="repository-cleanup-fails"),
    ],
)
def test_cleanup_only_deletes_every_drill_leftover(
    knobs: dict, code: int, out: str, changes: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule: cleanup-only deletes the drill's namespace, its pods in platform-backups and its repository, and exits 1
    when it can't."""
    leftovers = {("platform-backups", "drill-etcd-restore"), ("platform-backups", "jarvis-drill-repo-cleanup")}
    cluster = Cluster(namespace=True, pods=leftovers, **knobs)
    assert _run(tmp_path, monkeypatch, cluster, cleanup_only=True) == code
    assert capsys.readouterr().out == out
    assert cluster.changes == changes


_UNSCHEDULABLE = {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "no matching node"}
_CREATING = {"reason": "ContainerCreating"}
_NO_SECRET = {"reason": "CreateContainerConfigError", "message": 'secret "backups-restic" not found'}
_NO_CONTAINER = {"reason": "CreateContainerError", "message": "failed to mount /restore"}


@pytest.mark.parametrize(
    ("status", "result"),
    [
        pytest.param({"phase": "Pending", "containerStatuses": [{"state": {"waiting": _CREATING}}]}, False,
                     id="creating"),
        pytest.param({"phase": "Pending", "conditions": [_UNSCHEDULABLE]},
                     "pod platform-backups/drill-etcd-restore unschedulable: no matching node", id="unschedulable"),
        pytest.param({"phase": "Pending", "containerStatuses": [{"state": {"waiting": _NO_SECRET}}]},
                     'pod platform-backups/drill-etcd-restore configuration failed: secret "backups-restic" not found',
                     id="missing-secret"),
        pytest.param({"phase": "Pending", "containerStatuses": [{"state": {"waiting": _NO_CONTAINER}}]},
                     "pod platform-backups/drill-etcd-restore configuration failed: failed to mount /restore",
                     id="container-error"),
        pytest.param({"phase": "Pending", "initContainerStatuses": [{"state": {"waiting": _NO_SECRET}}]},
                     'pod platform-backups/drill-etcd-restore configuration failed: secret "backups-restic" not found',
                     id="injected-init-container-waits"),
    ],
)
def test_pod_that_cannot_start_fails_the_wait_at_once(status: dict, result: bool | str) -> None:
    """Rule: a drill pod that can't be scheduled, or whose container (an injected init one too) can't be made or waits
    on missing configuration, fails the wait at once; one still creating its container is not done yet."""
    read = ["get", "pod", "drill-etcd-restore", "-n", "platform-backups", "-o", "json"]

    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        assert (list(args), input) == (read, None)
        return completed(args, {"status": status})

    try:
        outcome: bool | str = drill_tool._pod_succeeded(kubectl, "drill-etcd-restore", "platform-backups")
    except drill_tool.DrillError as error:
        outcome = str(error)
    assert outcome == result


# cat config exits $CONFIG (10: no repository); snapshots lists one unless $NOSNAP; ls lists $LISTED (exit 1 when
# empty); restore writes a file at target + original path, as restic does, holding other bytes if $CORRUPT.
_FAKE_RESTIC = """#!/bin/sh
case "$1" in
  cat) exit "${CONFIG:-0}" ;;
  snapshots) if [ -n "$NOSNAP" ]; then echo '[]'; else echo '[{"id":"1"}]'; fi ;;
  ls) [ -n "$LISTED" ] || exit 1; echo 'snapshot 1 of [/snapshots]:'; echo "$LISTED" ;;
  restore)
    [ -z "$EMPTY" ] || exit 0
    mkdir -p "$4${6%/*}"; cp "$6" "$4$6"
    [ -z "$CORRUPT" ] || printf corrupt > "$4$6"
    [ -z "$UNREADABLE" ] || chmod 000 "$4$6" ;;
esac
"""
# The k3s snapshots on disk, by mtime: the newest sorts neither first nor last.
_K3S = {"etcd-snapshot-a": 1_000_000, "etcd-snapshot-b": 3_000_000, "etcd-snapshot-c": 2_000_000}
_NEWEST = hashlib.sha256(b"etcd-snapshot-b").hexdigest()
_NOT_COPIED = f"{drill_tool.NOT_COPIED} etcd-snapshot-b\n"


@pytest.mark.parametrize(
    ("snapshots", "restic", "code", "out"),
    [
        pytest.param(_K3S, {"LISTED": "etcd-snapshot-b"}, 0, f"ETCD {_NEWEST} {_NEWEST} etcd-snapshot-b\n",
                     id="copied"),
        pytest.param(_K3S, {"LISTED": "etcd-snapshot-b", "CORRUPT": "yes"}, 0,
                     f"ETCD {_NEWEST} {hashlib.sha256(b'corrupt').hexdigest()} etcd-snapshot-b\n",
                     id="restored-copy-differs"),
        pytest.param(_K3S, {"LISTED": "etcd-snapshot-c"}, 3, _NOT_COPIED, id="only-an-older-one-copied"),
        pytest.param(_K3S, {"LISTED": "etcd-snapshot-b.zip"}, 3, _NOT_COPIED, id="only-a-longer-name-copied"),
        pytest.param(_K3S, {"CONFIG": "10"}, 3, _NOT_COPIED, id="no-repository"),
        pytest.param(_K3S, {"NOSNAP": "yes"}, 3, _NOT_COPIED, id="empty-repository"),
        pytest.param({}, {}, 3, f"{drill_tool.NO_SNAPSHOT}\n", id="no-k3s-snapshot"),
        pytest.param(_K3S, {}, 1, "", id="ls-fails"),
        pytest.param(_K3S, {"CONFIG": "1", "NOSNAP": "yes"}, 1, "", id="unreadable-repository-looks-empty"),
        pytest.param(_K3S, {"CONFIG": "1", "LISTED": "etcd-snapshot-b"}, 1, "", id="unreadable-repository-lists-it"),
        pytest.param(_K3S, {"LISTED": "etcd-snapshot-b", "EMPTY": "yes"}, 1, "", id="restore-writes-nothing"),
        pytest.param(_K3S, {"LISTED": "etcd-snapshot-b", "UNREADABLE": "yes"}, 1, "", id="checksum-fails",
                     marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")),
    ],
)
def test_etcd_script_restores_only_a_name_the_restic_copy_lists(
    snapshots: dict[str, int], restic: dict[str, str], code: int, out: str, tmp_path: Path
) -> None:
    """Rule: the etcd pod restores the newest k3s snapshot only if restic's latest lists that very name, printing both
    checksums; else it says not copied or no snapshot, or fails on any other restic, restore or sha256sum error."""
    snaps, target, bindir = (tmp_path / name for name in ("snaps", "target", "bin"))
    for path in (snaps, target, bindir):
        path.mkdir()
    for name, mtime in snapshots.items():
        (snaps / name).write_bytes(name.encode())
        os.utime(snaps / name, (mtime, mtime))
    (bindir / "restic").write_text(_FAKE_RESTIC, encoding="utf-8")
    (bindir / "restic").chmod(0o755)
    template = {"image": "x", "resources": {}, "env": [], "nodeSelector": {}, "snapshotHostPath": {}}
    command = drill_tool._etcd_pod(template)["spec"]["containers"][0]["command"]
    script = command[2].replace("/snapshots", "\0S").replace("/restore", "\0R")
    script = script.replace("\0S", str(snaps)).replace("\0R", str(target))
    busybox = shutil.which("busybox")
    listed = {"LISTED": f"{snaps}/{restic['LISTED']}"} if "LISTED" in restic else {}
    proc = subprocess.run(
        [*([busybox, "sh"] if busybox else ["bash"]), "-c", script, *command[3:]],
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", **restic, **listed},
        capture_output=True, text=True, check=False,
    )
    assert (proc.returncode, proc.stdout) == (code, out)
