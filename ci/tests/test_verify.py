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


def _write_env(
    root: Path,
    *,
    marker: str = "env-test",
    time_zone: str | None = "Etc/UTC",
) -> str:
    env = "env-test"
    values = root / "envs" / env / "values"
    values.mkdir(parents=True, exist_ok=True)
    text = f"jarvis_env: {marker}\ndns_suffix: test.example\n"
    if time_zone is not None:
        text += f"time_zone: {time_zone}\n"
    (values / "cluster-vars.yaml").write_text(text, encoding="utf-8")
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
        self.source_schedule = "0 1 * * *"
        self.source_sync = "2026-10-31T01:01:00Z"
        self.etcd_schedule = "5 */12 * * *"
        self.etcd_zone: str | None = "Etc/UTC"
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
                        "spec": {"trigger": {"schedule": self.source_schedule}},
                        "status": {"lastSyncTime": self.source_sync},
                    }
                )
            return _completed(argv, {"items": items})
        if argv[:3] == ["get", "cronjob", "etcd-snapshots"]:
            return _completed(
                argv,
                {
                    "spec": {
                        "schedule": self.etcd_schedule,
                        "timeZone": self.etcd_zone,
                    },
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
    repo_marker: str = "env-test",
    time_zone: str | None = "Etc/UTC",
) -> int:
    _write_env(root, marker=repo_marker, time_zone=time_zone)
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


def _assert_read_only_calls(
    kubectl_calls: list[list[str]], external_calls: list[list[str]]
) -> None:
    assert all(call and call[0] in {"get", "wait"} for call in kubectl_calls)
    for call in external_calls:
        if not call or call[0] != "curl":
            continue
        method = "GET"
        for index, arg in enumerate(call):
            if arg in {"-X", "--request"}:
                method = call[index + 1].upper()
            elif arg.startswith("-X") and len(arg) > 2:
                method = arg[2:].upper()
            elif arg.startswith("--request="):
                method = arg.partition("=")[2].upper()
            elif arg in {"-I", "--head"}:
                method = "HEAD"
        assert method == "GET"
        data_flags = {
            "-d",
            "--data",
            "--data-ascii",
            "--data-binary",
            "--data-raw",
            "--data-urlencode",
            "--json",
            "-F",
            "--form",
            "--form-string",
            "-T",
            "--upload-file",
        }
        assert not any(
            arg in data_flags
            or (arg.startswith(("-d", "-F", "-T")) and len(arg) > 2)
            or any(arg.startswith(f"{flag}=") for flag in data_flags if flag.startswith("--"))
            for arg in call
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
    _assert_read_only_calls(cluster.calls, [])


def test_verify_curl_is_get_only_without_request_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_env(tmp_path)
    cluster = Cluster()
    external: list[list[str]] = []
    monkeypatch.setattr(
        verify_tool,
        "_inventory",
        lambda _root, _env: {"n1": {"control_plane", "edge"}},
    )

    def invoke(
        argv: Sequence[str], *, input: str | None = None, cwd: Path | None = None
    ) -> CompletedProcess[str]:
        del input, cwd
        call = list(argv)
        external.append(call)
        if call[0] == "curl":
            return _completed(call, "testapp\n")
        if call[:2] == ["openssl", "s_client"]:
            return _completed(call, "CERT\n")
        if call[:2] == ["openssl", "x509"]:
            return _completed(call, "issuer=CN = fixture-ca\n")
        raise AssertionError(call)

    monkeypatch.setattr(verify_tool, "_run", invoke)
    assert verify_tool.run(
        tmp_path,
        "env-test",
        cluster.kubectl,
        verify_tool._fetch,
        verify_tool._issuer,
        now=NOW,
    ) == 0
    _assert_read_only_calls(cluster.calls, external)


@pytest.mark.parametrize(
    ("kubectl_calls", "external_calls"),
    [
        ([["label", "node", "fixture", "x=y"]], []),
        ([["create", "namespace", "fixture"]], []),
        ([], [["curl", "-X", "POST", "https://fixture"]]),
        ([], [["curl", "--data", "x=y", "https://fixture"]]),
    ],
)
def test_read_only_guard_rejects_seeded_writes(
    kubectl_calls: list[list[str]], external_calls: list[list[str]]
) -> None:
    with pytest.raises(AssertionError):
        _assert_read_only_calls(kubectl_calls, external_calls)


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


def test_wrong_repository_marker_refuses_before_kubectl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    assert _run(
        tmp_path, cluster, monkeypatch, repo_marker="wrong"
    ) != 0
    assert cluster.calls == []
    assert "does not match its repository marker" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("change", "time_zone", "message"),
    [
        (
            lambda cluster: setattr(cluster, "source_schedule", "not a cron"),
            "Etc/UTC",
            "schedule",
        ),
        (
            lambda cluster: setattr(cluster, "etcd_schedule", "not a cron"),
            "Etc/UTC",
            "schedule",
        ),
        (
            lambda cluster: setattr(cluster, "source_schedule", "60 1 * * *"),
            "Etc/UTC",
            "cron field",
        ),
        (lambda cluster: None, "Not/AZone", "time zone"),
        (lambda cluster: None, None, "time_zone"),
        (
            lambda cluster: setattr(cluster, "etcd_zone", "Not/AZone"),
            "Etc/UTC",
            "time zone",
        ),
        (
            lambda cluster: setattr(cluster, "etcd_zone", None),
            "Etc/UTC",
            "no successful scheduled run",
        ),
    ],
)
def test_bad_schedule_or_timezone_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change,
    time_zone: str | None,
    message: str,
) -> None:
    cluster = Cluster()
    change(cluster)
    assert _run(
        tmp_path, cluster, monkeypatch, time_zone=time_zone
    ) != 0
    assert message in capsys.readouterr().out


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
