"""Standing-state verification uses only reads and fails closed. No cluster."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from support import REPO_ROOT, load_tool

SECRET_VALUE = "SHOULD_NOT_PRINT"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

verify_tool = load_tool("verify_tool", REPO_ROOT / "tools" / "verify" / "run")


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
        self.etcd_sync: str | None = "2026-10-31T05:10:00Z"
        self.etcd_created = "2026-01-01T00:00:00Z"
        self.hold = False
        self.bad_sources_json = False
        self.node_ready = True
        self.pods: list[dict] = []

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
            if self.secret_rc:
                return _completed(argv, "", returncode=1, stderr="forbidden")
            output = " ".join(argv)
            if "go-template" in output or "jsonpath" in output:
                return _completed(argv, "RESTIC_PASSWORD\n")
            return _completed(argv, {"data": {"RESTIC_PASSWORD": SECRET_VALUE}})
        if argv[:2] == ["get", "nodes"]:
            labels = {
                f"role.test.example/{role}": "true" for role in self.node_roles
            }
            if self.enablelb:
                labels[verify_tool.ENABLELB] = "true"
            return _completed(
                argv,
                {
                    "items": [
                        {
                            "metadata": {"name": "n1", "labels": labels},
                            "status": {
                                "conditions": [
                                    {
                                        "type": "Ready",
                                        "status": "True" if self.node_ready else "False",
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        if argv[:2] == ["get", "pods"]:
            return _completed(argv, {"items": self.pods})
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
            status = {}
            if self.etcd_sync is not None:
                status["lastSuccessfulTime"] = self.etcd_sync
            return _completed(
                argv,
                {
                    "metadata": {"creationTimestamp": self.etcd_created},
                    "spec": {
                        "schedule": self.etcd_schedule,
                        "timeZone": self.etcd_zone,
                    },
                    "status": status,
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
    enabled: Sequence[str] | None = None,
    fetch=None,
    issuer=None,
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
        fetch
        or (
            lambda host, ca: (
                fetch_code,
                "testapp\n" if not fetch_code else "certificate rejected",
            )
        ),
        issuer or (lambda host, ca: (0, "issuer=CN = fixture-ca")),
        now=NOW,
        enabled=enabled,
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
    assert "PASS Flux Ready" in output
    assert "PASS backups-restic present" in output
    assert "PASS node roles and enablelb" in output
    assert "PASS nodes Ready" in output
    assert "PASS pod health" in output
    assert "PASS backup freshness" in output
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

    monkeypatch.setattr(
        sys.modules["verify.platform"],
        "_run",
        invoke,
    )
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


def test_disabled_component_checks_do_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = Cluster()
    assert _run(tmp_path, cluster, monkeypatch, enabled=("platform",)) == 0
    joined = [" ".join(call) for call in cluster.calls]
    assert not any("backups-restic" in text for text in joined)
    assert not any("replicationsource" in text.lower() for text in joined)
    assert not any("etcd-snapshots" in text for text in joined)
    assert any(call[:2] == ["get", "nodes"] for call in cluster.calls)


def test_young_cronjob_without_run_warns_and_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.etcd_sync = None
    cluster.etcd_created = "2026-10-31T11:00:00Z"
    assert _run(tmp_path, cluster, monkeypatch) == 0
    output = capsys.readouterr().out
    assert "WARN backup freshness: etcd snapshot copy pending first run" in output
    assert "FAIL backup freshness" not in output


def test_old_cronjob_without_success_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.etcd_sync = None
    cluster.etcd_created = "2026-10-30T00:00:00Z"
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert "etcd snapshot copy has no successful scheduled run" in capsys.readouterr().out


def test_not_ready_node_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.node_ready = False
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert "n1: not Ready" in capsys.readouterr().out


def test_crashloop_pod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.pods = [
        {
            "metadata": {"namespace": "app", "name": "web"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                ],
            },
        }
    ]
    assert _run(tmp_path, cluster, monkeypatch) != 0
    output = capsys.readouterr().out
    assert "app/web: CrashLoopBackOff" in output
    assert "spec" not in output
    assert SECRET_VALUE not in output


def test_pending_pod_past_grace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.pods = [
        {
            "metadata": {
                "namespace": "app",
                "name": "web",
                "creationTimestamp": "2026-10-31T11:54:59Z",
            },
            "status": {"phase": "Pending"},
        }
    ]
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert "app/web: Pending" in capsys.readouterr().out


def test_sleeping_kubectl_times_out_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_mod = sys.modules["lib._env"]

    def sleeper(*args: object, **kwargs: object) -> CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        assert timeout is not None
        time.sleep(0.05)
        raise env_mod.subprocess.TimeoutExpired(args[0], float(timeout))

    monkeypatch.setattr(env_mod.subprocess, "run", sleeper)
    proc = env_mod.kubectl(
        ["get", "ns"],
        repo_root=REPO_ROOT,
        kubeconfig=tmp_path / "kubeconfig",
        request_timeout="1s",
        timeout=0.01,
    )
    assert proc.returncode == 1
    assert proc.stderr == "kubectl timed out"
    assert "--request-timeout=1s" in proc.args


def test_secret_check_asks_for_key_names_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    assert _run(tmp_path, cluster, monkeypatch) == 0
    secret_calls = [
        call for call in cluster.calls if call[:3] == ["get", "secret", "backups-restic"]
    ]
    assert secret_calls
    output = " ".join(secret_calls[0])
    assert "go-template=" in output
    assert verify_tool.SECRET_KEYS_TEMPLATE in output
    assert SECRET_VALUE not in output
    assert SECRET_VALUE not in capsys.readouterr().out
    assert "-o" in secret_calls[0]
    fmt = secret_calls[0][secret_calls[0].index("-o") + 1]
    assert fmt != "json"
    assert "$k" in fmt
    assert "println $k" in fmt or "{$k}" in fmt


def test_failed_certificate_chain_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run(
            tmp_path,
            Cluster(),
            monkeypatch,
            issuer=lambda host, ca: (1, "verify error:num=19:self signed certificate"),
        )
        != 0
    )
    assert "verify error" in capsys.readouterr().out


def test_wrong_canary_body_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        _run(
            tmp_path,
            Cluster(),
            monkeypatch,
            fetch=lambda host, ca: (0, "not-the-canary\n"),
        )
        != 0
    )
    assert "unexpected body" in capsys.readouterr().out


def test_bound_pvc_without_replicationsource_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.source = False
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert "app/data: no ReplicationSource" in capsys.readouterr().out


def test_verify_kubectl_adds_timeouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    captured: list[float | None] = []

    def invoke(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        seen.append(list(args))
        timeout = kwargs.get("timeout")
        captured.append(timeout if isinstance(timeout, float) else None)
        return _completed(args)

    monkeypatch.setattr(verify_tool.run_kubectl.__globals__["subprocess"], "run", invoke)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("", encoding="utf-8")
    verify_tool.bound_kubectl(
        ["get", "nodes"],
        repo_root=REPO_ROOT,
        kubeconfig=kubeconfig,
    )
    assert seen == [
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--request-timeout=30s",
            "get",
            "nodes",
        ]
    ]
    assert captured == [60.0]
