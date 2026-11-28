"""jarvis restore on a fake cluster: it pauses the source, then writes a Direct destination."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from jarvis.backups import restore as restore_tool
from support import REPO_ROOT, completed, write_env_marker, yaml_docs


@pytest.fixture(autouse=True)
def _instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """restore checks env-00's repository marker in the instance before it reads the cluster."""
    write_env_marker(tmp_path, "env-00")
    monkeypatch.setenv("INSTANCE", str(tmp_path))


def _pvc(namespace: str, name: str, *, volume: str = "pv", **meta: dict[str, str]) -> dict:
    """A PVC made at 00:00:10, bound to volume ("" while unbound); meta holds its annotations or labels."""
    created = {"name": name, "namespace": namespace, "creationTimestamp": "2026-10-29T00:00:10Z"}
    return {"metadata": {**created, **meta}, "spec": {"volumeName": volume}}


def _mover(source: str, parallelism: int) -> dict:
    """A VolSync mover Job, which its ReplicationSource controls."""
    owner = {"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource", "name": source, "controller": True}
    return {"metadata": {"name": f"mover-{parallelism}", "ownerReferences": [owner]}, "spec": {"parallelism": parallelism}}


def _mounting(kind: str, placement: dict, *, name: str | None = None, claim="pending", replicas=1) -> dict:
    """A Deployment, StatefulSet or CronJob whose pods mount claim, placed by placement."""
    volume = {"name": "data", "persistentVolumeClaim": {"claimName": claim}}
    template = {"template": {"spec": {**placement, "volumes": [volume]}}}
    if kind == "cronjob":
        return {"metadata": {"name": name or kind}, "spec": {"jobTemplate": {"spec": template}}}
    return {"metadata": {"name": name or kind}, "spec": {"replicas": replicas, **template}}


_PLACED = {
    "nodeSelector": {"disk": "fast"},
    "affinity": {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["a"]}]}
                ]
            }
        }
    },
    "tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}],
}
_LOW = {**_PLACED, "priorityClassName": "low"}
_WRITER = _mounting("deployment", {}, name="writer", claim="drill", replicas=2)
_SNAPSHOT_LOG = "restoring abc to /data\n"
_NO_SNAPSHOT_LOG = "No eligible snapshots found\n=== No data will be restored ===\n"
_ALL = {"in_namespace": None, "all_pvcs": True}


def _not_found(argv: list[str], kind: str) -> CompletedProcess[str]:
    return completed(argv, returncode=1, stderr=f'Error from server (NotFound): {kind} "{argv[2]}" not found')


@dataclass
class _Cluster:
    """kubectl on a fake cluster: PVC demo/drill unless items says otherwise, VolSync's sources, movers and
    destinations, the rebuild hold, and the pods and workloads of every namespace. It records each call."""

    items: list[dict] = field(default_factory=lambda: [_pvc("demo", "drill")])
    will_pause: bool = True
    logs: str = _SNAPSHOT_LOG
    hold: list[str] | None = None
    marker: str = "env-00"
    destination_done: bool = True
    drop_source_after_hold: bool = False
    interrupt_destination: bool = False
    interrupt_resume: bool = False
    jobs: list[dict] = field(default_factory=lambda: [_mover("drill", 0)])
    pods: list[dict] = field(default_factory=list)
    deployments: list[dict] = field(default_factory=list)
    statefulsets: list[dict] = field(default_factory=list)
    cronjobs: list[dict] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    binders: list[dict] = field(default_factory=list)
    bound: set[str] = field(default_factory=set)
    paused: set[str] = field(default_factory=set)
    resumed: set[str] = field(default_factory=set)

    def _pvcs(self, argv: list[str]) -> list[dict]:
        """The PVCs in argv's namespace (all of them with -A), each bound once a binder pod mounted it."""
        return [{**pvc, "spec": {"volumeName": "pv-bound"}} if pvc["metadata"]["name"] in self.bound else pvc
                for pvc in self.items if "-A" in argv or pvc["metadata"]["namespace"] == argv[argv.index("-n") + 1]]

    def kubectl(self, args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        listed = {"pods": self.pods, "deployment": self.deployments, "statefulset": self.statefulsets,
                  "cronjob": self.cronjobs, "jobs": self.jobs}  # fmt: skip
        if argv[:2] == ["get", "pvc"] and not argv[2].startswith("-"):
            return completed(argv, next(pvc for pvc in self._pvcs(argv) if pvc["metadata"]["name"] == argv[2]))
        if argv[:2] == ["get", "pvc"]:
            return completed(argv, {"items": self._pvcs(argv)})
        if argv[0] == "get" and argv[1] in listed:
            return completed(argv, {"items": listed[argv[1]]})
        if argv[:2] == ["get", "node"] and argv[2] not in {"node-a", "node-b"}:
            return _not_found(argv, "nodes")
        if argv[:2] in (["get", "node"], ["get", "secret"]):
            return completed(argv, f"{argv[1]}/{argv[2]}\n")
        if argv[:3] == ["get", "configmap", "cluster-vars"]:
            return completed(argv, {"data": {"jarvis_env": self.marker}})
        if argv[:3] == ["get", "configmap", restore_tool.HOLD_NAME]:
            if self.hold is None:
                return _not_found(argv, "configmaps")
            return completed(argv, {"data": {"repos": "\n".join(self.hold) + "\n"}})
        if argv[:2] == ["get", "replicationsource"]:
            if self.drop_source_after_hold and self.hold is None:
                return _not_found(argv, "replicationsources.volsync.backube")
            name = argv[2]
            # The rebuild hold keeps a source paused whatever its PVC's annotation says.
            paused = self.will_pause and name in self.paused and (name not in self.resumed or self.hold is not None)
            return completed(argv, {"spec": {"paused": paused}})
        if argv[:2] == ["get", "replicationdestination"]:
            status: dict = {"latestMoverStatus": {"result": "Successful", "logs": self.logs}}
            if self.destination_done:
                status["lastManualSync"] = self.applied[-1]["spec"]["trigger"]["manual"]
            return completed(argv, {"status": status})
        if argv[:2] == ["get", "kustomization"]:
            return completed(argv, {"spec": {}})
        if argv[:2] == ["annotate", "pvc"] and "backup=paused" in argv:
            self.paused.add(argv[2])
        elif argv[:2] == ["annotate", "pvc"] and "backup-" in argv:
            if self.interrupt_resume:
                self.interrupt_resume = False
                raise KeyboardInterrupt
            self.resumed.add(argv[2])
        elif argv[:2] == ["apply", "-f"]:
            doc = yaml.safe_load(input or "")
            if doc["kind"] == "ConfigMap":
                self.hold = doc["data"]["repos"].splitlines()
            elif doc["kind"] == "Pod":
                self.binders.append(doc)
                self.bound.add(doc["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])
            elif self.interrupt_destination:
                raise KeyboardInterrupt
            else:
                self.applied.append(doc)
        elif argv[:3] == ["delete", "configmap", restore_tool.HOLD_NAME]:
            self.hold = None
        elif argv[:2] not in (["delete", "replicationdestination"], ["delete", "pod"], ["scale", "deployment"],
                              ["scale", "statefulset"], ["patch", "cronjob"], ["patch", "kustomization"]):
            return completed(argv, returncode=1, stderr=f"unexpected kubectl: {argv}")
        return completed(argv, "done\n")


def _boom(_seconds: float) -> None:
    raise AssertionError("slept")


def _run(cluster: _Cluster, *, kubectl=None, **kwargs: object) -> int:
    """restore on cluster, for namespace demo unless kwargs say otherwise, with a clock that never sleeps."""
    args = {"env": "env-00", "in_namespace": "demo", "all_pvcs": False, "monotonic": lambda: 0.0, "sleep": _boom,
            "now": lambda: 1000.0, **kwargs}  # fmt: skip
    return restore_tool.run(kubectl or cluster.kubectl, **args)  # type: ignore[arg-type]


def _changes(cluster: _Cluster) -> list[list[str]]:
    """The kubectl calls that were not reads, in order."""
    return [call for call in cluster.calls if call[0] != "get"]


_AS_OF = "2026-10-29T00:00:09Z"  # a second before the PVCs were made


def _restored(*pvcs: str) -> list[str]:
    """What restore prints for each PVC it restores from the default snapshot."""
    lines = []
    for pvc in pvcs:
        lines += [f"pause {pvc}", f"restore {pvc} as-of {_AS_OF}", f"PASS restore {pvc} snapshot unknown"]
    return lines


_DRILL_WRITTEN = ["pause demo/drill", f"restore demo/drill as-of {_AS_OF}"]
_DRILL_UNDO = ["kubectl annotate pvc drill -n demo backup-",
               "kubectl delete replicationdestination restore-drill -n demo --wait=true"]  # fmt: skip
_PAUSE_DRILL = ["annotate", "pvc", "drill", "-n", "demo", "backup=paused", "--overwrite"]
_APPLY = ["apply", "-f", "-"]
_RESUME_DRILL = ["annotate", "pvc", "drill", "-n", "demo", "backup-"]
_DELETE_HOLD = ["delete", "configmap", restore_tool.HOLD_NAME, "-n", restore_tool.HOLD_NAMESPACE, "--wait=true",
                "--ignore-not-found=true"]  # fmt: skip
_DELETE_DESTINATION = ["delete", "replicationdestination", "restore-drill", "-n", "demo", "--wait=true",
                       "--ignore-not-found=true"]  # fmt: skip
# Restoring drill: the pause, the destination, its deletion (the resume comes after, unless the hold is set).
_RESTORE_DRILL = [_PAUSE_DRILL, _APPLY, _DELETE_DESTINATION]


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        pytest.param([], "one of the arguments --namespace --all is required", id="neither"),
        pytest.param(["--namespace", "demo", "--all"], "argument --all: not allowed with argument --namespace",
                     id="both"),
    ],
)
def test_cli_takes_one_namespace_or_all(argv: list[str], error: str, capsys) -> None:
    """Rule: jarvis restore restores one namespace or every PVC, so it refuses neither flag and both."""
    with pytest.raises(SystemExit) as refused:
        restore_tool.main(["--env", "env-00", *argv])
    out, err = capsys.readouterr()
    assert (refused.value.code, out, err.splitlines()[-1]) == (2, "", f"jarvis restore: error: {error}")


def test_kubectl_is_bound_to_the_environment_file(tmp_path: Path, monkeypatch, capsys) -> None:
    """Rule: restore runs kubectl with the env's own kubeconfig, ~/.kube/jarvis-<env>.yaml, never the default one
    (here the cluster marker it reads first names another env, so that is its only call)."""
    kubeconfig = tmp_path / ".kube" / "jarvis-env-00.yaml"
    kubeconfig.parent.mkdir()
    kubeconfig.touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    seen: list[list[str]] = []

    def record(argv: list[str], **_kwargs: object) -> CompletedProcess[str]:
        seen.append(argv)
        return completed(argv, {"data": {"jarvis_env": "env-01"}})

    monkeypatch.setattr(subprocess, "run", record)
    assert restore_tool.main(["--env", "env-00", "--namespace", "demo"]) == 1
    assert seen == [["kubectl", "--kubeconfig", str(kubeconfig), "get", "configmap", "cluster-vars",
                     "-n", "flux-system", "-o", "json"]]  # fmt: skip
    assert capsys.readouterr().out == (
        "SUMMARY restored=0 accepted=0 retired=0\nFAIL refusing cluster whose environment marker is not ENV=env-00\n"
    )


def test_missing_environment_kubeconfig_refuses_before_kubectl(tmp_path: Path, monkeypatch, capsys) -> None:
    """Rule: with no ~/.kube/jarvis-<env>.yaml, restore refuses before any kubectl call."""
    monkeypatch.setenv("HOME", str(tmp_path))

    def forbidden(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        raise AssertionError("kubectl must not run")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert restore_tool.main(["--env", "env-00", "--namespace", "demo"]) == 1
    assert capsys.readouterr().out == f"FAIL kubeconfig does not exist: {tmp_path}/.kube/jarvis-env-00.yaml\n"


def test_repository_environment_marker_must_match(tmp_path: Path, capsys) -> None:
    """Rule: an instance whose env-00 marker names another env refuses before any kubectl call."""
    write_env_marker(tmp_path, "env-00", "wrong-env")
    cluster = _Cluster()
    assert _run(cluster) == 1
    assert capsys.readouterr().out == (
        "SUMMARY restored=0 accepted=0 retired=0\nFAIL ENV=env-00 does not match its repository marker\n"
    )
    assert cluster.calls == []


_UNBOUND = {"items": [_pvc("demo", "drill"), _pvc("demo", "pending", volume="")]}
_HOLD = {"hold": ["ns-demo/pvc-drill"]}


@pytest.mark.parametrize(
    ("knobs", "flags", "message"),
    [
        pytest.param({"marker": "wrong-env"}, {}, "refusing cluster whose environment marker is not ENV=env-00",
                     id="cluster-marker"),
        pytest.param(_HOLD, {"in_namespace": "typo"}, "no PVC to restore", id="no-pvc-in-namespace"),
        pytest.param({}, {"retire": ["ns-other/pvc-gone"]}, "--retire and --accept-empty are only valid with --all",
                     id="retire-without-all"),
        pytest.param({}, {"accept_empty": ["demo/drill"]}, "--retire and --accept-empty are only valid with --all",
                     id="accept-empty-without-all"),
        pytest.param(_HOLD, {**_ALL, "accept_empty": ["other/missing"]}, "accepted PVC is not selected: other/missing",
                     id="accept-empty-unselected"),
        pytest.param(_HOLD, {**_ALL, "retire": ["ns-other/pvc-gone"]},
                     "retired repository is not missing from the hold: ns-other/pvc-gone", id="retire-not-in-hold"),
        pytest.param(_HOLD, {**_ALL, "retire": ["ns-demo/pvc-drill"]},
                     "retired repository is not missing from the hold: ns-demo/pvc-drill", id="retire-with-a-pvc"),
        pytest.param(_UNBOUND, {}, "demo/pending: unbound and no Deployment, StatefulSet or CronJob mounts it;"
                     " pass --node <node> to choose the node its volume lives on", id="no-workload-mounts-it"),
        pytest.param({**_UNBOUND, "deployments": [_mounting("deployment", _PLACED)],
                      "cronjobs": [_mounting("cronjob", {})]}, {},
                     "demo/pending: the workloads that mount it ask for different placement; pass --node <node>",
                     id="workloads-disagree"),
        pytest.param(_UNBOUND, {"node": "node-z"},
                     'node node-z: Error from server (NotFound): nodes "node-z" not found', id="unknown-node"),
        pytest.param(_UNBOUND, {"node": "Node_A"}, "invalid node name: Node_A", id="invalid-node-name"),
    ],
)
def test_refuses_before_any_change(knobs: dict, flags: dict, message: str, capsys) -> None:
    """Rule: restore checks the cluster's marker, its flags and every binder's node before its first change, so a
    refusal prints only the summary and its reason and changes nothing, not even demo/drill, first in line."""
    cluster = _Cluster(**knobs)
    assert _run(cluster, **flags) == 1
    assert capsys.readouterr().out == f"SUMMARY restored=0 accepted=0 retired=0\nFAIL {message}\n"
    assert _changes(cluster) == []


@pytest.mark.parametrize(
    ("as_of", "restored_as_of"),
    [
        pytest.param(None, _AS_OF, id="a-second-before-the-pvc"),
        pytest.param("2026-10-29T12:00:00Z", "2026-10-29T12:00:00Z", id="as-of-utc"),
        pytest.param("2026-10-29T13:00:00+01:00", "2026-10-29T12:00:00Z", id="as-of"),
    ],
)
def test_restore_pauses_the_source_then_writes_a_direct_destination(
    as_of: str | None, restored_as_of: str, capsys
) -> None:
    """Rule: restore pauses the source, writes the PVC in place (Direct, deleting what the snapshot lacks) from the
    last snapshot before the PVC was made, or at --as-of, then deletes the destination and resumes the source."""
    cluster = _Cluster()
    assert _run(cluster, as_of=as_of) == 0
    assert cluster.applied == [{
        "apiVersion": "volsync.backube/v1alpha1",
        "kind": "ReplicationDestination",
        "metadata": {"name": "restore-drill", "namespace": "demo"},
        "spec": {
            "trigger": {"manual": "restore-1000"},
            "restic": {"destinationPVC": "drill", "repository": "restic-drill", "copyMethod": "Direct",
                       "enableFileDeletion": True, "restoreAsOf": restored_as_of},
        },
    }]  # fmt: skip
    assert _changes(cluster) == [*_RESTORE_DRILL, _RESUME_DRILL]
    assert capsys.readouterr().out.splitlines() == [
        "pause demo/drill", f"restore demo/drill as-of {restored_as_of}", "PASS restore demo/drill snapshot unknown",
        "SUMMARY restored=1 accepted=0 retired=0",
    ]


@pytest.mark.parametrize(
    ("scope", "restored"),
    [
        pytest.param({}, ["demo/drill"], id="namespace"),
        pytest.param(_ALL, ["demo/drill", "other/keep"], id="all"),
    ],
)
def test_restore_takes_every_enrolled_pvc_in_scope(scope: dict, restored: list[str], capsys) -> None:
    """Rule: restore skips a PVC marked backup: none and VolSync's own; --namespace keeps to its namespace."""
    skipped = [_pvc("demo", "scratch", annotations={"backup": "none"}),
               _pvc("demo", "mover", labels={"app.kubernetes.io/created-by": "volsync"})]  # fmt: skip
    cluster = _Cluster([_pvc("demo", "drill"), _pvc("other", "keep"), *skipped])
    assert _run(cluster, **scope) == 0
    written = [f"{doc['metadata']['namespace']}/{doc['spec']['restic']['destinationPVC']}" for doc in cluster.applied]
    assert written == restored
    assert capsys.readouterr().out.splitlines() == [
        *_restored(*restored), f"SUMMARY restored={len(restored)} accepted=0 retired=0",
    ]


_DRILL_VOLUME = {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "drill"}}]}
_HOLDERS = [  # pods that mount drill: running, not started, terminating (still running, or done), finished
    {"metadata": {"name": "app"}, "spec": _DRILL_VOLUME, "status": {"phase": "Running"}},
    {"metadata": {"name": "starting"}, "spec": _DRILL_VOLUME},
    {"metadata": {"name": "stopping", "deletionTimestamp": "2026-10-29T00:00:00Z"}, "spec": _DRILL_VOLUME,
     "status": {"phase": "Running"}},
    {"metadata": {"name": "old-mover", "deletionTimestamp": "2026-10-29T00:00:00Z"}, "spec": _DRILL_VOLUME,
     "status": {"phase": "Succeeded"}},
    {"metadata": {"name": "done"}, "spec": _DRILL_VOLUME, "status": {"phase": "Succeeded"}},
]


@pytest.mark.parametrize(
    ("knobs", "message"),
    [
        pytest.param({"jobs": [_mover("drill", 1), _mover("keep", 0)]}, "ReplicationSource did not pause",
                     id="mover-still-running"),
        pytest.param({"will_pause": False}, "ReplicationSource did not pause", id="source-never-pauses"),
        pytest.param({"pods": _HOLDERS}, "pods still mounted (app, starting, stopping, old-mover)",
                     id="a-pod-mounts-the-pvc"),
    ],
)
def test_restore_writes_nothing_until_the_source_has_paused(knobs: dict, message: str, capsys) -> None:
    """Rule: no destination is written while the source is unpaused, its mover Job (found by its controller owner)
    runs, or a pod mounts the PVC until it finishes, even while it terminates; restore fails without resuming."""
    cluster = _Cluster(**knobs)
    assert _run(cluster, pause_budget_s=0) == 1
    assert (cluster.applied, cluster.resumed) == ([], set())
    assert capsys.readouterr().out.splitlines() == [
        "pause demo/drill", "kubectl annotate pvc drill -n demo backup-",
        "SUMMARY restored=0 accepted=0 retired=0", f"FAIL demo/drill: {message}",
    ]


def test_pass_line_names_the_snapshot(capsys) -> None:
    """Rule: the PASS line names the snapshot restic restored, read from the mover log VolSync keeps."""
    logs = "restoring snapshot 3727b648 of [/data] at 2026-10-29 00:00:00.000000000 +0000 UTC by root@volsync to .\n"
    assert _run(_Cluster(logs=logs)) == 0
    assert capsys.readouterr().out.splitlines() == [
        *_DRILL_WRITTEN, "PASS restore demo/drill snapshot 3727b648", "SUMMARY restored=1 accepted=0 retired=0",
    ]


@pytest.mark.parametrize(
    ("knobs", "message"),
    [
        pytest.param({"logs": _NO_SNAPSHOT_LOG}, "no eligible snapshots", id="no-snapshot"),
        pytest.param({"logs": ""}, "mover log has no restore result", id="empty-mover-log"),
        pytest.param({"destination_done": False}, "ReplicationDestination did not finish",
                     id="destination-never-finishes"),
    ],
)
def test_restore_that_fails_after_writing_keeps_the_pause_for_a_rerun(knobs: dict, message: str, capsys) -> None:
    """Rule: a destination that never finishes, or a mover log with no restore in it (no snapshot, or a log VolSync
    could not read), fails with no PASS line and keeps the source paused for the undo plan; a rerun restores."""
    cluster = _Cluster(**knobs)
    assert _run(cluster, restore_budget_s=0) == 1
    assert capsys.readouterr().out.splitlines() == [
        *_DRILL_WRITTEN, *_DRILL_UNDO, "SUMMARY restored=0 accepted=0 retired=0", f"FAIL demo/drill: {message}",
    ]
    assert cluster.resumed == set()
    cluster.logs, cluster.destination_done = _SNAPSHOT_LOG, True
    assert _run(cluster) == 0
    assert capsys.readouterr().out.splitlines() == [*_restored("demo/drill"), "SUMMARY restored=1 accepted=0 retired=0"]
    assert cluster.resumed == {"drill"}


@pytest.mark.parametrize(
    ("accept_empty", "rc", "lines", "hold", "resumed"),
    [
        pytest.param([], 1, [*_DRILL_UNDO, "SUMMARY restored=0 accepted=0 retired=0",
                             "FAIL demo/drill: no eligible snapshots"], ["ns-demo/pvc-drill"], set(), id="no-snapshot"),
        pytest.param(["demo/drill"], 0,
                     ["ACCEPT empty demo/drill", "HOLD lifted", "SUMMARY restored=0 accepted=1 retired=0"],
                     None, {"drill"}, id="accepted-empty"),
    ],
)
def test_restore_all_fails_closed_on_no_snapshot(
    accept_empty: list[str], rc: int, lines: list[str], hold: list[str] | None, resumed: set[str], capsys
) -> None:
    """Rule: in restore-all, a PVC with no snapshot fails the run and keeps the hold and the pause, unless
    --accept-empty names it: then it counts as accepted and the hold is lifted."""
    cluster = _Cluster(logs=_NO_SNAPSHOT_LOG, hold=["ns-demo/pvc-drill"])
    assert _run(cluster, **_ALL, accept_empty=accept_empty) == rc
    assert capsys.readouterr().out.splitlines() == [*_DRILL_WRITTEN, *lines]
    assert (cluster.hold, cluster.resumed) == (hold, resumed)


def test_restore_all_on_an_empty_env_is_a_clean_no_op(capsys) -> None:
    """Rule: restore-all on a fresh env (no hold, no PVC) says so, exits 0 and changes nothing."""
    cluster = _Cluster([])
    assert _run(cluster, **_ALL) == 0
    assert capsys.readouterr().out == "nothing to restore: no rebuild hold and no PVC\n"
    assert _changes(cluster) == []


def _placement(pod: dict) -> dict:
    """Where a binder pod runs: its scheduling fields."""
    keys = {"nodeName", "nodeSelector", "affinity", "tolerations", "priorityClassName"}
    return {key: value for key, value in pod["spec"].items() if key in keys}


def _pinned(node: str) -> dict:
    """A binder's placement on a node the operator named: matched by name, whatever its taints."""
    term = {"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}
    required = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [term]}}
    return {"affinity": {"nodeAffinity": required}, "tolerations": [{"operator": "Exists"}]}


def test_unbound_pvc_is_paused_then_bound(capsys) -> None:
    """Rule: an unbound PVC is paused, then bound on --node by a binder pod, then restored; --node was needed, so
    no "no PVC needed it" line."""
    cluster = _Cluster([_pvc("demo", "pending", volume="")])
    assert _run(cluster, node="node-a") == 0
    assert _changes(cluster) == [
        ["annotate", "pvc", "pending", "-n", "demo", "backup=paused", "--overwrite"],
        _APPLY,
        ["delete", "pod", "restore-bind-pending", "-n", "demo", "--wait=false", "--ignore-not-found=true"],
        _APPLY,
        ["delete", "replicationdestination", "restore-pending", "-n", "demo", "--wait=true", "--ignore-not-found=true"],
        ["annotate", "pvc", "pending", "-n", "demo", "backup-"],
    ]
    assert [_placement(pod) for pod in cluster.binders] == [_pinned("node-a")]
    assert [doc["spec"]["restic"]["destinationPVC"] for doc in cluster.applied] == ["pending"]
    assert capsys.readouterr().out.splitlines() == [
        *_restored("demo/pending"), "SUMMARY restored=1 accepted=0 retired=0",
    ]


def test_node_is_unused_when_every_selected_pvc_is_bound(capsys) -> None:
    """Rule: --node is checked even when every selected PVC is bound, then one line says no PVC needed it."""
    cluster = _Cluster([_pvc("demo", "bound")])
    assert _run(cluster, node="node-a") == 0
    assert ["get", "node", "node-a", "-o", "name"] in cluster.calls
    assert cluster.binders == []
    assert capsys.readouterr().out.splitlines() == [
        "node node-a: no PVC needed it", *_restored("demo/bound"), "SUMMARY restored=1 accepted=0 retired=0",
    ]


def test_restore_all_node_pins_every_unbound_pvc(capsys) -> None:
    """Rule: a restore-all run with one --node pins every unbound PVC's binder pod to that node."""
    cluster = _Cluster([_pvc("demo", "one", volume=""), _pvc("other", "two", volume=""), _pvc("demo", "bound")])
    assert _run(cluster, **_ALL, node="node-a") == 0
    binders = [(pod["metadata"]["namespace"], pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"],
                _placement(pod)) for pod in cluster.binders]  # fmt: skip
    assert binders == [("demo", "one", _pinned("node-a")), ("other", "two", _pinned("node-a"))]
    assert capsys.readouterr().out.splitlines() == [
        *_restored("demo/bound", "demo/one", "other/two"), "SUMMARY restored=3 accepted=0 retired=0",
    ]


def test_bind_pod_runs_the_etcd_copys_image(capsys) -> None:
    """Rule: the binder runs the image core's etcd-snapshots manifest names, with no read of the live CronJob."""
    [cronjob] = yaml_docs(REPO_ROOT / "core/platform/backups/etcd-snapshots.yaml")
    image = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
    cluster = _Cluster([_pvc("demo", "one", volume=""), _pvc("demo", "two", volume="")])
    assert _run(cluster, node="node-a") == 0
    assert [pod["spec"]["containers"][0]["image"] for pod in cluster.binders] == [image] * 2
    assert ["get", "cronjob", "etcd-snapshots"] not in [call[:3] for call in cluster.calls]
    assert capsys.readouterr().out.splitlines() == [
        *_restored("demo/one", "demo/two"), "SUMMARY restored=2 accepted=0 retired=0",
    ]


@pytest.mark.parametrize(
    ("kind", "template", "node", "placement"),
    [
        pytest.param("deployment", _LOW, None, _LOW, id="deployment"),
        pytest.param("statefulset", _LOW, None, _LOW, id="statefulset"),
        pytest.param("cronjob", _LOW, None, _LOW, id="cronjob"),
        pytest.param("deployment", _PLACED, None, _PLACED, id="no-priority-class"),
        pytest.param("deployment", {"nodeName": "node-b"}, None, _pinned("node-b"), id="pinned-by-node-name"),
        pytest.param("deployment", _LOW, "node-b", {**_pinned("node-b"), "priorityClassName": "low"}, id="node-wins"),
    ],
)
def test_binder_placement_comes_from_the_workload_or_the_node(
    kind: str, template: dict, node: str | None, placement: dict
) -> None:
    """Rule: local-path binds where the binder runs, so it takes the placement and PriorityClass of the workload that
    mounts the PVC; a nodeName (which skips the scheduler) becomes node affinity, and --node wins."""
    cluster = _Cluster([], **{f"{kind}s": [_mounting(kind, template)]})
    assert restore_tool.binder_placement(cluster.kubectl, "demo", "pending", node) == placement


def test_restore_binds_an_unbound_pvc_where_its_workload_runs(capsys) -> None:
    """Rule: with no --node, the binder pod restore applies runs with the placement and PriorityClass of the
    workload that mounts the PVC."""
    cluster = _Cluster([_pvc("demo", "pending", volume="")], deployments=[_mounting("deployment", _LOW)])
    assert _run(cluster) == 0
    assert [_placement(pod) for pod in cluster.binders] == [_LOW]
    assert capsys.readouterr().out.splitlines() == [
        *_restored("demo/pending"), "SUMMARY restored=1 accepted=0 retired=0",
    ]


def test_pvc_unbound_after_selection_fails_without_a_binder(capsys) -> None:
    """Rule: a PVC bound when selected but unbound when its turn comes fails: it has no decided placement, so
    restore does not bind it blind."""
    cluster = _Cluster()

    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        if list(args)[:3] == ["get", "pvc", "drill"]:
            return completed(args, _pvc("demo", "drill", volume=""))
        return cluster.kubectl(args, input)

    assert _run(cluster, kubectl=kubectl) == 1
    assert cluster.binders == []
    assert capsys.readouterr().out.splitlines() == [
        "pause demo/drill", "kubectl annotate pvc drill -n demo backup-",
        "SUMMARY restored=0 accepted=0 retired=0", "FAIL demo/drill: was bound when selected and is unbound now",
    ]


@pytest.mark.parametrize(
    ("hold", "flags", "rc", "lines", "changes"),
    [
        pytest.param(["ns-demo/pvc-drill"], {}, 0,
                     ["HOLD still set; backup=paused remains until jarvis restore --all lifts it",
                      "SUMMARY restored=1 accepted=0 retired=0"], _RESTORE_DRILL, id="namespace-restore"),
        pytest.param(["ns-demo/pvc-drill", "ns-ghost/pvc-gone"], _ALL, 1,
                     ["HOLD kept: no PVC for ns-ghost/pvc-gone", "HOLD kept: backup=paused remains",
                      "SUMMARY restored=1 accepted=0 retired=0", "FAIL rebuild hold kept"], _RESTORE_DRILL,
                     id="a-repo-has-no-pvc"),
        pytest.param(["ns-demo/pvc-drill"], _ALL, 0, ["HOLD lifted", "SUMMARY restored=1 accepted=0 retired=0"],
                     [*_RESTORE_DRILL, _DELETE_HOLD, _RESUME_DRILL], id="every-repo-restored"),
        pytest.param(["ns-demo/pvc-drill", "ns-ghost/pvc-gone"], {**_ALL, "retire": ["ns-ghost/pvc-gone"]}, 0,
                     ["HOLD retired ns-ghost/pvc-gone", "HOLD lifted", "SUMMARY restored=1 accepted=0 retired=1"],
                     [*_RESTORE_DRILL, _DELETE_HOLD, _RESUME_DRILL], id="the-rest-retired"),
    ],
)
def test_hold_stays_until_restore_all_restores_or_retires_every_repo(
    hold: list[str], flags: dict, rc: int, lines: list[str], changes: list[list[str]], capsys
) -> None:
    """Rule: the rebuild hold keeps every source paused until restore --all has restored or retired each repo in it;
    then restore deletes the hold before it resumes the sources (deleting it does not re-run Kyverno's generate)."""
    cluster = _Cluster(hold=hold)
    assert _run(cluster, **flags) == rc
    assert capsys.readouterr().out.splitlines() == [*_restored("demo/drill"), *lines]
    assert _changes(cluster) == changes


# A lift that fails: drill restored, the hold deleted and drill's resume tried, then its pause and the hold put back.
_LIFT_UNDONE = [*_RESTORE_DRILL, _DELETE_HOLD, _RESUME_DRILL, _PAUSE_DRILL, _APPLY]


def test_lift_does_not_treat_a_missing_source_as_unpaused(capsys) -> None:
    """Rule: a source gone once the hold is lifted is not taken as resumed: restore puts the pause and the hold back
    and fails."""
    cluster = _Cluster(hold=["ns-demo/pvc-drill"], drop_source_after_hold=True)
    assert _run(cluster, **_ALL, pause_budget_s=0) == 1
    assert (_changes(cluster), cluster.hold) == (_LIFT_UNDONE, ["ns-demo/pvc-drill"])
    assert capsys.readouterr().out.splitlines() == [
        *_restored("demo/drill"),
        "HOLD restored after lift failure; backup=paused remains",
        "SUMMARY restored=1 accepted=0 retired=0",
        "FAIL demo/drill: source stayed paused after the hold was lifted",
    ]


def test_interrupt_during_hold_resume_restores_hold_and_reraises(capsys) -> None:
    """Rule: an interrupt while restore-all resumes the sources puts the pause and the hold back, then re-raises."""
    cluster = _Cluster(hold=["ns-demo/pvc-drill"], interrupt_resume=True)
    with pytest.raises(KeyboardInterrupt):
        _run(cluster, **_ALL)
    assert (_changes(cluster), cluster.hold) == (_LIFT_UNDONE, ["ns-demo/pvc-drill"])
    assert capsys.readouterr().out.splitlines() == [
        *_restored("demo/drill"), "HOLD restored after lift failure; backup=paused remains",
    ]


def test_printed_hold_recovery_preserves_repo_lines(capsys) -> None:
    """Rule: the recovery printed when restore cannot put the hold back re-creates it with every repo line."""
    hold = ["ns-demo/pvc-drill", "ns-other/pvc-data"]
    restore_tool._print_hold_recovery(hold, [], kubeconfig=None)
    lines = capsys.readouterr().out.splitlines()
    start = lines.index("cat <<'JARVIS_REBUILD_HOLD' | kubectl apply -f -") + 1
    end = lines.index("JARVIS_REBUILD_HOLD", start)
    configmap = yaml.safe_load("\n".join(lines[start:end]))

    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        assert input is None
        assert list(args) == ["get", "configmap", restore_tool.HOLD_NAME, "-n", restore_tool.HOLD_NAMESPACE,
                              "-o", "json"]  # fmt: skip
        return completed(args, configmap)

    assert restore_tool._hold_repos(kubectl) == hold


@pytest.mark.parametrize(
    "kustomizations", [pytest.param(["demo"], id="flux-managed"), pytest.param([], id="no-kustomization")]
)
def test_restore_suspends_and_restores_real_workloads(kustomizations: list[str], capsys) -> None:
    """Rule: restore suspends the PVC's Kustomization, if any, and stops its writers (Deployment scaled to 0, CronJob
    suspended) before the pause, then starts them after the resume; an unrelated CronJob's Kustomization is not read."""
    cronjob = _mounting("cronjob", {}, name="scheduled-writer", claim="drill")
    cronjob["spec"]["suspend"] = False
    in_flux_system = {"kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
    unrelated = {
        "metadata": {
            "name": "other-schedule",
            "labels": {"kustomize.toolkit.fluxcd.io/name": "other", **in_flux_system},
        },
        "spec": {"jobTemplate": {"spec": {"template": {"spec": {"volumes": []}}}}},
    }
    labels = {"kustomize.toolkit.fluxcd.io/name": "demo", **in_flux_system} if kustomizations else {}
    cluster = _Cluster([_pvc("demo", "drill", labels=labels)], deployments=[_WRITER], cronjobs=[cronjob, unrelated])
    assert _run(cluster) == 0
    suspend = ["patch", "cronjob", "scheduled-writer", "-n", "demo", "--type", "merge", "-p"]
    flux = [["patch", "kustomization", name, "-n", "flux-system", "--type", "merge", "-p"] for name in kustomizations]
    assert [[*call[:-1], json.loads(call[-1])] if call[0] == "patch" else call for call in _changes(cluster)] == [
        *([*patch, {"spec": {"suspend": True}}] for patch in flux),
        [*suspend, {"spec": {"suspend": True}}],
        ["scale", "deployment", "writer", "-n", "demo", "--replicas=0"],
        *_RESTORE_DRILL,
        _RESUME_DRILL,
        ["scale", "deployment", "writer", "-n", "demo", "--replicas=2"],
        [*suspend, {"spec": {"suspend": False}}],
        *([*patch, {"spec": {"suspend": False}}] for patch in flux),
    ]
    assert [call[2] for call in cluster.calls if call[:2] == ["get", "kustomization"]] == kustomizations
    assert capsys.readouterr().out.splitlines() == [*_restored("demo/drill"), "SUMMARY restored=1 accepted=0 retired=0"]


def test_interrupt_prints_complete_undo_plan(capsys) -> None:
    """Rule: an interrupt mid-restore prints every command that undoes what restore changed, each for the env's
    kubeconfig, then re-raises."""
    cronjob = _mounting("cronjob", {}, name="scheduled-writer", claim="drill")
    cluster = _Cluster(deployments=[_WRITER], cronjobs=[cronjob], interrupt_destination=True)
    with pytest.raises(KeyboardInterrupt):
        _run(cluster, kubeconfig=Path("/home/op/.kube/jarvis-env-00.yaml"))
    undo = [
        "annotate pvc drill -n demo backup-",
        "scale deployment writer -n demo --replicas=2",
        """patch cronjob scheduled-writer -n demo --type merge -p '{"spec":{"suspend":false}}'""",
        "delete replicationdestination restore-drill -n demo --wait=true",
    ]
    assert capsys.readouterr().out.splitlines() == [
        *_DRILL_WRITTEN, *(f"kubectl --kubeconfig /home/op/.kube/jarvis-env-00.yaml {command}" for command in undo),
    ]
