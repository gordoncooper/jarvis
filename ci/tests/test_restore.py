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

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKSUM = "a" * 64
OTHER = "b" * 64
ETCD = "c" * 64

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


_SNAPSHOT_LOG = "restoring abc to /data\n"
_NO_SNAPSHOT_LOG = "No eligible snapshots found\n=== No data will be restored ===\n"


class _Cluster:
    def __init__(
        self,
        items: list[dict],
        *,
        will_pause: bool,
        logs: str = _SNAPSHOT_LOG,
        hold: list[str] | None = None,
        pods: list[dict] | None = None,
        drop_source_after_hold: bool = False,
        deployments: list[dict] | None = None,
        statefulsets: list[dict] | None = None,
        cronjobs: list[dict] | None = None,
        interrupt_destination: bool = False,
        env: str = "env-00",
    ) -> None:
        self.items = items
        self.will_pause = will_pause
        self.logs = logs
        self.hold = hold
        self.pods = pods or []
        self.drop_source_after_hold = drop_source_after_hold
        self.deployments = deployments or []
        self.statefulsets = statefulsets or []
        self.cronjobs = cronjobs or []
        self.interrupt_destination = interrupt_destination
        self.env = env
        self.calls: list[list[str]] = []
        self.applied: list[dict] = []
        self.paused: set[str] = set()
        self.resumed: set[str] = set()
        self.bound: set[str] = set()
        self.hold_deleted = False
        self.suspended_cronjobs: set[str] = set()

    def _pvc(self, namespace: str, name: str) -> dict:
        item = next(
            entry
            for entry in self.items
            if entry["metadata"]["namespace"] == namespace and entry["metadata"]["name"] == name
        )
        if name in self.bound and not item["spec"].get("volumeName"):
            item["spec"]["volumeName"] = "pv-bound"
        return item

    def _source_paused(self, name: str) -> bool:
        if not self.will_pause:
            return False
        annotated = name in self.paused and name not in self.resumed
        return annotated or (self.hold is not None and name in self.paused)

    def kubectl(self, args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        if argv[:2] == ["get", "pvc"] and len(argv) > 2 and not argv[2].startswith("-"):
            namespace = argv[argv.index("-n") + 1]
            return _completed(argv, json.dumps(self._pvc(namespace, argv[2])))
        if argv[:2] == ["get", "pvc"]:
            items = self.items
            if "-n" in argv:
                namespace = argv[argv.index("-n") + 1]
                items = [
                    item for item in items if item["metadata"]["namespace"] == namespace
                ]
            shown = []
            for item in items:
                if item["metadata"]["name"] in self.bound and not item["spec"].get("volumeName"):
                    item["spec"]["volumeName"] = "pv-bound"
                shown.append(item)
            return _completed(argv, json.dumps({"items": shown}))
        if argv[:2] == ["get", "pods"]:
            return _completed(argv, json.dumps({"items": self.pods}))
        if argv[:2] == ["get", "deployment"]:
            return _completed(argv, json.dumps({"items": self.deployments}))
        if argv[:2] == ["get", "statefulset"]:
            return _completed(argv, json.dumps({"items": self.statefulsets}))
        if argv[:2] == ["get", "cronjob"]:
            return _completed(argv, json.dumps({"items": self.cronjobs}))
        if argv[:3] == ["get", "configmap", "cluster-vars"]:
            return _completed(argv, json.dumps({"data": {"jarvis_env": self.env}}))
        if argv[:3] == ["get", "configmap", restore_tool.HOLD_NAME]:
            if self.hold is None:
                return _completed(
                    argv,
                    "",
                    returncode=1,
                    stderr=f'Error from server (NotFound): configmaps "{restore_tool.HOLD_NAME}" not found',
                )
            return _completed(
                argv,
                json.dumps({"data": {"repos": "\n".join(self.hold) + "\n"}}),
            )
        if argv[:2] == ["annotate", "pvc"] and "backup=paused" in argv:
            self.paused.add(argv[2])
            return _completed(argv, "annotated\n")
        if argv[:2] == ["annotate", "pvc"] and "backup-" in argv:
            self.resumed.add(argv[2])
            return _completed(argv, "annotated\n")
        if argv[:2] == ["get", "replicationsource"]:
            if self.drop_source_after_hold and self.hold_deleted:
                return _completed(
                    argv,
                    "",
                    returncode=1,
                    stderr=(
                        "Error from server (NotFound): "
                        f'replicationsources.volsync.backube "{argv[2]}" not found'
                    ),
                )
            return _completed(
                argv, json.dumps({"spec": {"paused": self._source_paused(argv[2])}})
            )
        if argv[:2] == ["get", "job"]:
            return _completed(argv, json.dumps({"spec": {"parallelism": 0}, "status": {}}))
        if argv[:2] == ["get", "secret"]:
            return _completed(argv, f"secret/{argv[2]}\n")
        if argv[0] == "logs":
            return _completed(
                argv,
                "",
                returncode=1,
                stderr="Error from server (NotFound): jobs.batch not found",
            )
        if argv[:2] == ["apply", "-f"]:
            assert input is not None
            doc = yaml.safe_load(input)
            if doc["kind"] == "ConfigMap":
                self.hold = doc["data"]["repos"].splitlines()
                self.hold_deleted = False
                return _completed(argv, "applied\n")
            if doc["kind"] == "Pod":
                claim = doc["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
                self.bound.add(claim)
                return _completed(argv, "applied\n")
            if self.interrupt_destination:
                raise KeyboardInterrupt
            self.applied.append(doc)
            return _completed(argv, "applied\n")
        if argv[:2] == ["get", "replicationdestination"]:
            manual = self.applied[-1]["spec"]["trigger"]["manual"]
            return _completed(
                argv,
                json.dumps(
                    {
                        "status": {
                            "lastManualSync": manual,
                            "latestMoverStatus": {"result": "Successful", "logs": self.logs},
                        }
                    }
                ),
            )
        if argv[:2] == ["delete", "replicationdestination"]:
            return _completed(argv, "deleted\n")
        if argv[:2] == ["delete", "pod"]:
            return _completed(argv, "deleted\n")
        if argv[:3] == ["delete", "configmap", restore_tool.HOLD_NAME]:
            self.hold = None
            self.hold_deleted = True
            return _completed(argv, "deleted\n")
        if argv[:2] == ["scale", "deployment"] or argv[:2] == ["scale", "statefulset"]:
            return _completed(argv, "scaled\n")
        if argv[:2] == ["patch", "cronjob"]:
            patch = json.loads(argv[argv.index("-p") + 1])
            if patch["spec"]["suspend"]:
                self.suspended_cronjobs.add(argv[2])
            else:
                self.suspended_cronjobs.discard(argv[2])
            return _completed(argv, "patched\n")
        if argv[:2] == ["patch", "kustomization"]:
            return _completed(argv, "patched\n")
        return _completed(argv, "", returncode=1, stderr=f"unexpected kubectl: {argv}")


def _boom(_seconds: float) -> None:
    raise AssertionError("slept")


def test_kubectl_is_bound_to_the_environment_file(monkeypatch) -> None:
    seen: list[list[str]] = []

    def invoke(args: Sequence[str], **_kwargs: object) -> CompletedProcess[str]:
        seen.append(list(args))
        return _completed(list(args), "{}")

    monkeypatch.setattr(restore_tool.subprocess, "run", invoke)
    kubeconfig = Path("/tmp/jarvis-env-test.yaml")
    restore_tool._kubectl(["get", "pvc"], kubeconfig=kubeconfig)
    assert seen == [
        ["kubectl", "--kubeconfig", str(kubeconfig), "get", "pvc"]
    ]


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
    assert "ENV is required" in missing.stderr

    missing_app = subprocess.run(
        ["make", "restore", "ENV=env-00"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert missing_app.returncode != 0
    assert "APP is required" in missing_app.stderr

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
    ]
    cluster = _Cluster(items, will_pause=True)
    assert (
        restore_tool.run(
            cluster.kubectl,
            env="env-00",
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
            env="env-00",
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
            env="env-00",
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


def _log(second: str, *, snapshot: bool, deleted: bool = True) -> str:
    lines = [
        f"{CHECKSUM}  /data/payload",
        'persistentvolumeclaim "drill" deleted'
        if deleted
        else "kubectl delete pod drill",
        "restore demo/drill as-of 2026-10-29T00:00:09Z",
        f"{second}  /data/payload",
    ]
    if snapshot:
        lines.append(f"{ETCD}  /snapshots/etcd-snapshot-node-1710000000")
        lines.append(f"{ETCD}  /restore/snapshots/etcd-snapshot-node-1710000000")
    return "\n".join(lines) + "\n"


def test_verify_p0b_reads_drill_log(tmp_path: Path, capsys) -> None:
    env = _write_env(tmp_path)
    log = tmp_path / "drill.log"
    log.write_text(_log(CHECKSUM, snapshot=True), encoding="utf-8")

    ca = tmp_path / "envs" / env / "ca.crt"
    ca.write_text("dummy\n", encoding="utf-8")

    def fetch(host: str, ca_file: Path) -> tuple[int, str]:
        assert host == "testapp.test.example"
        assert ca_file == ca
        return 0, "testapp\n"

    def issuer(host: str, ca_file: Path) -> tuple[int, str]:
        assert host == "testapp.test.example"
        assert ca_file == ca
        return 0, "issuer=CN = test-ca"

    assert verify_p0b.run(tmp_path, env, log, fetch, issuer) == 0
    out = capsys.readouterr().out
    assert "PASS lan curl https://testapp.test.example/" in out
    assert "testapp" in out.splitlines()
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

    log.write_text(_log(CHECKSUM, snapshot=True, deleted=False), encoding="utf-8")
    assert verify_p0b.run(tmp_path, env, log, fetch, issuer) != 0
    assert "PVC deletion" in capsys.readouterr().out

    digest = "d" * 64
    log.write_text(
        f"IMAGE=restic/restic@sha256:{digest}\n" + _log(CHECKSUM, snapshot=True),
        encoding="utf-8",
    )
    assert verify_p0b.run(tmp_path, env, log, fetch, issuer) == 0
    assert f"PASS restore checksum {CHECKSUM}" in capsys.readouterr().out
    assert digest not in capsys.readouterr().out


def _run(cluster: _Cluster, **kwargs: object) -> int:
    args = {
        "env": "env-00",
        "monotonic": lambda: 0.0,
        "sleep": _boom,
        "now": lambda: 1000.0,
    }
    args.update(kwargs)
    return restore_tool.run(cluster.kubectl, **args)  # type: ignore[arg-type]


def test_pass_line_names_the_snapshot(capsys) -> None:
    logs = (
        "restoring snapshot 3727b648 of [/data] at "
        "2026-10-29 00:00:00.000000000 +0000 UTC by root@volsync to .\n"
    )
    cluster = _Cluster([_pvc("demo", "drill")], will_pause=True, logs=logs)
    assert _run(cluster, app="demo", all_pvcs=False) == 0
    assert "PASS restore demo/drill snapshot 3727b648" in capsys.readouterr().out


def test_empty_mover_logs_fail(capsys) -> None:
    cluster = _Cluster([_pvc("demo", "drill")], will_pause=True, logs="")
    assert _run(cluster, app="demo", all_pvcs=False) != 0
    out = capsys.readouterr().out
    assert "mover log has no restore result" in out
    assert "PASS restore" not in out
    assert "backup-" not in [arg for call in cluster.calls for arg in call]


def test_named_restore_fails_when_nothing_matches(capsys) -> None:
    cluster = _Cluster([], will_pause=True, hold=["ns-demo/pvc-drill"])
    assert _run(cluster, app="typo", all_pvcs=False) != 0
    assert "no PVC to restore" in capsys.readouterr().out
    assert cluster.hold_deleted is False


def test_no_snapshot_fails_and_rerun_can_resume(capsys) -> None:
    items = [_pvc("demo", "drill")]
    cluster = _Cluster(items, will_pause=True, logs=_NO_SNAPSHOT_LOG)
    assert _run(cluster, app="demo", all_pvcs=False) != 0
    failed = capsys.readouterr().out
    assert "PASS restore" not in failed
    assert "no eligible snapshots" in failed
    assert "mover logs unavailable" not in failed
    assert failed.count("kubectl annotate pvc drill -n demo backup-") == 1
    assert (
        failed.count(
            "kubectl delete replicationdestination restore-drill -n demo --wait=true"
        )
        == 1
    )
    assert not any(call[0] == "logs" for call in cluster.calls)
    assert "backup-" not in [arg for call in cluster.calls for arg in call]

    cluster.logs = _SNAPSHOT_LOG
    assert _run(cluster, app="demo", all_pvcs=False) == 0
    assert "PASS restore demo/drill" in capsys.readouterr().out


def test_live_volume_uses_as_of(capsys) -> None:
    cluster = _Cluster([_pvc("demo", "drill")], will_pause=True)
    assert (
        _run(
            cluster,
            app="demo",
            all_pvcs=False,
            as_of="2026-10-29T12:00:00Z",
        )
        == 0
    )
    assert cluster.applied[0]["spec"]["restic"]["restoreAsOf"] == "2026-10-29T12:00:00Z"
    assert "as-of 2026-10-29T12:00:00Z" in capsys.readouterr().out


def test_unbound_pvc_is_paused_then_bound(capsys) -> None:
    cluster = _Cluster([_pvc("demo", "pending", volume="")], will_pause=True)
    assert _run(cluster, app="demo", all_pvcs=False) == 0
    assert "pending" in cluster.bound

    def at(pred) -> int:
        for index, call in enumerate(cluster.calls):
            if pred(call):
                return index
        raise AssertionError("missing call")

    pause_at = at(lambda call: "backup=paused" in call)
    applies = [index for index, call in enumerate(cluster.calls) if call[:2] == ["apply", "-f"]]
    assert pause_at < applies[0] < applies[1]
    assert cluster.applied[0]["spec"]["restic"]["destinationPVC"] == "pending"
    assert "PASS restore demo/pending" in capsys.readouterr().out


def test_terminating_pod_blocks_the_volume(capsys) -> None:
    holder = {
        "metadata": {"name": "app", "deletionTimestamp": "2026-10-29T00:00:00Z"},
        "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "drill"}}]},
        "status": {"phase": "Running"},
    }
    done = {
        "metadata": {"name": "old-mover"},
        "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "drill"}}]},
        "status": {"phase": "Succeeded"},
    }

    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        del input
        if list(args)[:2] == ["get", "pods"]:
            return _completed(list(args), json.dumps({"items": [holder, done]}))
        return _completed(list(args), "", returncode=1, stderr="unexpected")

    assert restore_tool.volume_holders(kubectl, "demo", "drill") == ["app"]

    cluster = _Cluster([_pvc("demo", "drill")], will_pause=True, pods=[holder])
    assert _run(cluster, app="demo", all_pvcs=False, pause_budget_s=0) != 0
    assert "pods still mounted (app)" in capsys.readouterr().out


def test_single_restore_does_not_lift_the_hold(capsys) -> None:
    cluster = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        hold=["ns-demo/pvc-drill"],
    )
    assert _run(cluster, app="demo", all_pvcs=False) == 0
    assert cluster.hold_deleted is False
    assert "backup-" not in [arg for call in cluster.calls for arg in call]
    assert "HOLD still set" in capsys.readouterr().out


def test_restore_all_keeps_hold_until_every_repo_has_a_pvc(capsys) -> None:
    kept = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        hold=["ns-demo/pvc-drill", "ns-ghost/pvc-gone"],
    )
    assert _run(kept, app=None, all_pvcs=True) != 0
    kept_out = capsys.readouterr().out
    assert "HOLD kept: no PVC for ns-ghost/pvc-gone" in kept_out
    assert "PASS restore demo/drill" in kept_out
    assert kept.hold_deleted is False
    assert "backup-" not in [arg for call in kept.calls for arg in call]

    lifted = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        hold=["ns-demo/pvc-drill"],
    )
    assert _run(lifted, app=None, all_pvcs=True) == 0
    assert lifted.hold_deleted is True

    def at(calls: list[list[str]], pred) -> int:
        for index, call in enumerate(calls):
            if pred(call):
                return index
        raise AssertionError("missing call")

    delete_at = at(
        lifted.calls,
        lambda call: call[:3] == ["delete", "configmap", restore_tool.HOLD_NAME],
    )
    resume_at = at(lifted.calls, lambda call: "backup-" in call)
    assert delete_at < resume_at
    assert "HOLD lifted" in capsys.readouterr().out


def test_restore_all_retires_a_named_repo_with_no_pvc(capsys) -> None:
    cluster = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        hold=["ns-demo/pvc-drill", "ns-ghost/pvc-gone"],
    )
    assert _run(cluster, app=None, all_pvcs=True, retire=["ns-ghost/pvc-gone"]) == 0
    assert cluster.hold_deleted is True
    assert "HOLD retired ns-ghost/pvc-gone" in capsys.readouterr().out
    assert "HOLD kept" not in capsys.readouterr().out


def test_mover_job_already_gone_uses_destination_status(capsys) -> None:
    cluster = _Cluster([_pvc("demo", "drill")], will_pause=True, logs=_NO_SNAPSHOT_LOG)
    assert _run(cluster, app="demo", all_pvcs=False) != 0
    out = capsys.readouterr().out
    assert "no eligible snapshots" in out
    assert "mover logs unavailable" not in out
    assert not any(call[0] == "logs" for call in cluster.calls)


def test_lift_does_not_treat_a_missing_source_as_unpaused(capsys) -> None:
    cluster = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        hold=["ns-demo/pvc-drill"],
        drop_source_after_hold=True,
    )
    assert _run(cluster, app=None, all_pvcs=True, pause_budget_s=0) != 0
    assert cluster.hold_deleted is False
    out = capsys.readouterr().out
    assert "source stayed paused" in out
    assert "HOLD restored after lift failure" in out


def test_printed_hold_recovery_preserves_repo_lines(capsys) -> None:
    hold = ["ns-demo/pvc-drill", "ns-other/pvc-data"]
    restore_tool._print_hold_recovery(hold, [], kubeconfig=None)
    lines = capsys.readouterr().out.splitlines()
    start = lines.index("cat <<'JARVIS_REBUILD_HOLD' | kubectl apply -f -") + 1
    end = lines.index("JARVIS_REBUILD_HOLD", start)
    configmap = yaml.safe_load("\n".join(lines[start:end]))

    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        assert input is None
        assert list(args) == [
            "get",
            "configmap",
            restore_tool.HOLD_NAME,
            "-n",
            restore_tool.HOLD_NAMESPACE,
            "-o",
            "json",
        ]
        return _completed(list(args), json.dumps(configmap))

    assert restore_tool._hold_repos(kubectl) == hold


def test_restore_all_fails_closed_on_no_snapshot(capsys) -> None:
    cluster = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        logs=_NO_SNAPSHOT_LOG,
        hold=["ns-demo/pvc-drill"],
    )
    assert _run(cluster, app=None, all_pvcs=True) != 0
    out = capsys.readouterr().out
    assert "no eligible snapshots" in out
    assert "PASS restore" not in out
    assert cluster.hold_deleted is False
    assert not any("backup-" in call for call in cluster.calls)
    assert "SUMMARY restored=0 accepted=0 retired=0" in out


def test_restore_all_accepts_an_explicit_empty_volume(capsys) -> None:
    cluster = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        logs=_NO_SNAPSHOT_LOG,
        hold=["ns-demo/pvc-drill"],
    )
    assert (
        _run(
            cluster,
            app=None,
            all_pvcs=True,
            accept_empty=["demo/drill"],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "ACCEPT empty demo/drill" in out
    assert "SUMMARY restored=0 accepted=1 retired=0" in out
    assert cluster.hold_deleted is True


def test_wrong_cluster_marker_refuses_before_mutation(capsys) -> None:
    cluster = _Cluster([_pvc("demo", "drill")], will_pause=True, env="wrong-env")
    assert _run(cluster, app="demo", all_pvcs=False) != 0
    assert "environment marker is not ENV=env-00" in capsys.readouterr().out
    mutating = {"annotate", "apply", "delete", "patch", "scale"}
    assert not any(call[0] in mutating for call in cluster.calls)


def test_restore_suspends_and_restores_real_workloads(capsys) -> None:
    pod_spec = {
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "drill"}}
        ]
    }
    deployment = {
        "metadata": {"name": "writer"},
        "spec": {"replicas": 2, "template": {"spec": pod_spec}},
    }
    cronjob = {
        "metadata": {"name": "scheduled-writer"},
        "spec": {
            "suspend": False,
            "jobTemplate": {"spec": {"template": {"spec": pod_spec}}},
        },
    }
    cluster = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        deployments=[deployment],
        cronjobs=[cronjob],
    )
    assert _run(cluster, app="demo", all_pvcs=False) == 0
    assert cluster.suspended_cronjobs == set()
    assert [
        call for call in cluster.calls if call[:3] == ["scale", "deployment", "writer"]
    ] == [
        ["scale", "deployment", "writer", "-n", "demo", "--replicas=0"],
        ["scale", "deployment", "writer", "-n", "demo", "--replicas=2"],
    ]
    cron_patches = [
        json.loads(call[call.index("-p") + 1])["spec"]["suspend"]
        for call in cluster.calls
        if call[:3] == ["patch", "cronjob", "scheduled-writer"]
    ]
    assert cron_patches == [True, False]
    assert "PASS restore demo/drill" in capsys.readouterr().out


def test_interrupt_prints_complete_undo_plan(capsys) -> None:
    pod_spec = {
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "drill"}}
        ]
    }
    cluster = _Cluster(
        [_pvc("demo", "drill")],
        will_pause=True,
        deployments=[
            {
                "metadata": {"name": "writer"},
                "spec": {"replicas": 2, "template": {"spec": pod_spec}},
            }
        ],
        cronjobs=[
            {
                "metadata": {"name": "scheduled-writer"},
                "spec": {
                    "jobTemplate": {"spec": {"template": {"spec": pod_spec}}},
                },
            }
        ],
        interrupt_destination=True,
    )
    with pytest.raises(KeyboardInterrupt):
        _run(cluster, app="demo", all_pvcs=False)
    out = capsys.readouterr().out
    assert "kubectl annotate pvc drill -n demo backup-" in out
    assert "kubectl scale deployment writer -n demo --replicas=2" in out
    assert "kubectl patch cronjob scheduled-writer -n demo" in out
    assert "kubectl delete replicationdestination restore-drill -n demo" in out
