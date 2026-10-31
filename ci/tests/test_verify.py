"""Standing-state verification uses only reads and fails closed. No cluster."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from subprocess import CompletedProcess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_VALUE = "SHOULD_NOT_PRINT"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

_LOADER = SourceFileLoader("verify_tool", str(REPO_ROOT / "tools" / "verify" / "run"))
_SPEC = importlib.util.spec_from_loader(_LOADER.name, _LOADER)
assert _SPEC is not None
verify_tool = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(verify_tool)


def _completed(
    args: Sequence[str], value: object = "", *, returncode: int = 0, stderr: str = ""
) -> CompletedProcess[str]:
    stdout = json.dumps(value) if not isinstance(value, str) else value
    return CompletedProcess(list(args), returncode, stdout, stderr)


def _write_env(root: Path) -> str:
    env = "env-test"
    values = root / "envs" / env / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text(
        "jarvis_env: env-test\n"
        "dns_suffix: test.example\n"
        "time_zone: Etc/UTC\n",
        encoding="utf-8",
    )
    (root / "envs" / env / "ca.crt").write_text("fixture CA\n", encoding="utf-8")
    return env


class Cluster:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.marker = "env-test"
        self.flux_empty = False
        self.flux_ready = True
        self.secret_rc = 0
        self.node_roles = {"control_plane", "edge"}
        self.enablelb = True
        self.source = True
        self.source_sync = "2026-10-31T01:01:00Z"
        self.etcd_sync = "2026-10-31T05:10:00Z"
        self.hold = False
        self.bad_sources_json = False

    def kubectl(
        self, args: Sequence[str], input: str | None = None
    ) -> CompletedProcess[str]:
        assert input is None
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["get", "configmap", "cluster-vars"]:
            return _completed(argv, {"data": {"jarvis_env": self.marker}})
        if argv[0] == "get" and argv[1] in verify_tool.FLUX_RESOURCES:
            return _completed(argv, {"items": [] if self.flux_empty else [{}]})
        if argv[0] == "wait":
            return _completed(
                argv,
                "",
                returncode=0 if self.flux_ready else 1,
                stderr="not Ready",
            )
        if argv[:3] == ["get", "secret", "backups-restic"]:
            return _completed(
                argv,
                {"data": {"RESTIC_PASSWORD": SECRET_VALUE}},
                returncode=self.secret_rc,
                stderr="forbidden" if self.secret_rc else "",
            )
        if argv[:2] == ["get", "nodes"]:
            labels = {
                f"role.test.example/{role}": "true" for role in self.node_roles
            }
            if self.enablelb:
                labels[verify_tool.ENABLELB] = "true"
            return _completed(
                argv,
                {"items": [{"metadata": {"name": "n1", "labels": labels}}]},
            )
        if argv[:2] == ["get", "pvc"]:
            return _completed(
                argv,
                {
                    "items": [
                        {
                            "metadata": {"namespace": "app", "name": "data"},
                            "spec": {"volumeName": "pv-data"},
                        }
                    ]
                },
            )
        if argv[:2] == ["get", "replicationsource"]:
            if self.bad_sources_json:
                return _completed(argv, {"items": "unreadable"})
            items = []
            if self.source:
                items.append(
                    {
                        "metadata": {"namespace": "app", "name": "data"},
                        "spec": {"trigger": {"schedule": "0 1 * * *"}},
                        "status": {"lastSyncTime": self.source_sync},
                    }
                )
            return _completed(argv, {"items": items})
        if argv[:3] == ["get", "cronjob", "etcd-snapshots"]:
            return _completed(
                argv,
                {
                    "spec": {"schedule": "5 */12 * * *", "timeZone": "Etc/UTC"},
                    "status": {"lastSuccessfulTime": self.etcd_sync},
                },
            )
        if argv[:3] == ["get", "configmap", verify_tool.HOLD_NAME]:
            if self.hold:
                return _completed(argv, {"data": {"repos": "ns-app/pvc-data\n"}})
            return _completed(
                argv,
                "",
                returncode=1,
                stderr='Error from server (NotFound): configmap "hold" not found',
            )
        return _completed(argv, "", returncode=1, stderr=f"unexpected {argv}")


def _run(
    root: Path,
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetch_code: int = 0,
) -> int:
    monkeypatch.setattr(
        verify_tool,
        "_inventory",
        lambda _root, _env: {"n1": {"control_plane", "edge"}},
    )
    return verify_tool.run(
        root,
        "env-test",
        cluster.kubectl,
        lambda host, ca: (
            fetch_code,
            "testapp\n" if not fetch_code else "certificate rejected",
        ),
        lambda host, ca: (0, "issuer=CN = fixture-ca"),
        now=NOW,
    )


def test_verify_passes_without_writes_or_secret_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_env(tmp_path)
    cluster = Cluster()
    assert _run(tmp_path, cluster, monkeypatch) == 0
    output = capsys.readouterr().out
    assert SECRET_VALUE not in output
    assert "owner" not in output.lower()
    assert "PASS LAN HTTPS and certificate issuer=issuer=CN = fixture-ca" in output
    writes = {"apply", "patch", "annotate", "delete", "exec", "port-forward", "scale"}
    assert not any(call[0] in writes for call in cluster.calls)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda cluster: setattr(cluster, "flux_empty", True), "none found"),
        (lambda cluster: setattr(cluster, "flux_ready", False), "not Ready"),
        (lambda cluster: setattr(cluster, "secret_rc", 1), "forbidden"),
        (
            lambda cluster: setattr(cluster, "node_roles", {"control_plane"}),
            "role labels",
        ),
        (
            lambda cluster: setattr(cluster, "enablelb", False),
            "enablelb nodes",
        ),
        (lambda cluster: setattr(cluster, "source", False), "no ReplicationSource"),
        (
            lambda cluster: setattr(cluster, "source_sync", "2026-10-27T01:00:00Z"),
            "last successful sync is stale",
        ),
        (
            lambda cluster: setattr(cluster, "etcd_sync", "2026-10-29T05:00:00Z"),
            "etcd snapshot copy is stale",
        ),
        (lambda cluster: setattr(cluster, "hold", True), "rebuild hold is present"),
        (
            lambda cluster: setattr(cluster, "bad_sources_json", True),
            "invalid items",
        ),
    ],
)
def test_each_bad_standing_state_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change,
    message: str,
) -> None:
    _write_env(tmp_path)
    cluster = Cluster()
    change(cluster)
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert message in capsys.readouterr().out


def test_ca_mismatch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_env(tmp_path)
    assert _run(tmp_path, Cluster(), monkeypatch, fetch_code=1) != 0
    assert "certificate rejected" in capsys.readouterr().out


def test_wrong_cluster_marker_stops_after_marker_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_env(tmp_path)
    cluster = Cluster()
    cluster.marker = "wrong"
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert cluster.calls == [
        ["get", "configmap", "cluster-vars", "-n", "flux-system", "-o", "json"]
    ]
    assert "environment marker is not ENV=env-test" in capsys.readouterr().out


def test_kubectl_uses_environment_kubeconfig(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setenv("HOME", str(tmp_path))

    def invoke(args: Sequence[str], **_kwargs: object) -> CompletedProcess[str]:
        seen.append(list(args))
        return _completed(args)

    monkeypatch.setattr(verify_tool.run_kubectl.__globals__["subprocess"], "run", invoke)
    kubeconfig = verify_tool.kubeconfig_for("fixture")
    verify_tool.run_kubectl(
        ["get", "nodes"],
        repo_root=REPO_ROOT,
        kubeconfig=kubeconfig,
    )
    assert seen == [
        [
            "kubectl",
            "--kubeconfig",
            str(tmp_path / ".kube" / "jarvis-fixture.yaml"),
            "get",
            "nodes",
        ]
    ]
