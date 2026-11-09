"""Standing-state verification uses only reads and fails closed. No cluster."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from jarvis.verify.backups import HOLD_NAME, SECRET_KEYS_TEMPLATE
from jarvis.verify.platform import ENABLELB, FLUX_RESOURCES
from support import REPO_ROOT, load_tool

SECRET_VALUE = "SHOULD_NOT_PRINT"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

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
        self.etcd_scheduled: str | None = None
        self.etcd_created = "2026-01-01T00:00:00Z"
        self.check_sync: str | None = "2026-10-28T06:40:00Z"
        self.check_created = "2026-01-01T00:00:00Z"
        self.check_scheduled: str | None = None
        self.etcd_active = False
        self.check_active = False
        self.offsite_sync = "2026-10-31T04:20:00Z"
        self.hold = False
        self.bad_sources_json = False
        self.node_ready = True
        self.nodes_rc = 0
        self.pods_rc = 0
        self.pods: list[dict] = []
        self.hello = False
        self.core_users = ["restic"]
        self.apps_command = ["/bin/sh", "-c", "exec rest-server --private-repos"]
        self.apps_env_from: list[dict] = []
        self.pvcs = [("app", "data")]

    def kubectl(
        self, args: Sequence[str], input: str | None = None
    ) -> CompletedProcess[str]:
        assert input is None
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["get", "configmap", "cluster-vars"]:
            return _completed(argv, {"data": {"jarvis_env": self.marker}})
        if argv[:5] == ["get", "service", "-n", "kube-system", "traefik"]:
            # An env with no App DNS: verify reaches Traefik's ServiceLB address.
            return _completed(argv, {"status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.10"}]}}})
        if argv[0] == "get" and argv[1] in FLUX_RESOURCES:
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
            if 'index .data "htpasswd"' in output:
                lines = "".join(f"{user}:$2y$12${SECRET_VALUE}\n" for user in self.core_users)
                return _completed(argv, base64.b64encode(lines.encode()).decode())
            if "go-template" in output or "jsonpath" in output:
                return _completed(argv, "RESTIC_PASSWORD\n")
            return _completed(argv, {"data": {"RESTIC_PASSWORD": SECRET_VALUE}})
        if argv[:2] == ["get", "nodes"]:
            labels = {
                f"node-role.jarvis.io/{role}": "true" for role in self.node_roles
            }
            if self.enablelb:
                labels[ENABLELB] = "true"
            body = {
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
            }
            if self.nodes_rc:
                return _completed(
                    argv, body, returncode=self.nodes_rc, stderr="forbidden"
                )
            return _completed(argv, body)
        if argv[:2] == ["get", "pods"]:
            if self.pods_rc:
                return _completed(
                    argv, {"items": []}, returncode=self.pods_rc, stderr="forbidden"
                )
            return _completed(argv, {"items": self.pods})
        if argv[:2] == ["get", "pvc"]:
            return _completed(
                argv,
                {
                    "items": [
                        {
                            "metadata": {"namespace": namespace, "name": name},
                            "spec": {"volumeName": f"pv-{name}"},
                        }
                        for namespace, name in self.pvcs
                    ]
                },
            )
        if argv[:3] == ["get", "deployment", "restic-apps"]:
            pod = {"containers": [{"command": self.apps_command, "envFrom": self.apps_env_from}], "volumes": []}
            return _completed(argv, {"spec": {"template": {"spec": pod}}})
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
            if self.etcd_scheduled is not None:
                status["lastScheduleTime"] = self.etcd_scheduled
            if self.etcd_active:
                status["active"] = [{"kind": "Job", "name": "etcd-snapshots-1"}]
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
        if argv[:3] == ["get", "cronjob", "restic-check"]:
            check_status = {}
            if self.check_sync is not None:
                check_status["lastSuccessfulTime"] = self.check_sync
            if self.check_scheduled is not None:
                check_status["lastScheduleTime"] = self.check_scheduled
            if self.check_active:
                check_status["active"] = [{"kind": "Job", "name": "restic-check-1"}]
            return _completed(
                argv,
                {
                    "metadata": {"creationTimestamp": self.check_created},
                    "spec": {"schedule": "30 6 * * 3", "timeZone": "Etc/UTC"},
                    "status": check_status,
                },
            )
        if argv[:3] == ["get", "cronjob", "restic-offsite"]:
            return _completed(
                argv,
                {
                    "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
                    "spec": {"schedule": "0 4 * * *", "timeZone": "Etc/UTC"},
                    "status": {"lastSuccessfulTime": self.offsite_sync},
                },
            )
        if self.hello and argv[:4] == ["get", "deployments", "-n", "hello"]:
            return _completed(argv, {"items": [{"metadata": {"name": "hello"}, "status": {"readyReplicas": 1}}]})
        if self.hello and argv[:3] == ["get", "secret", "hello-greeting"] and "go-template" in " ".join(argv):
            return _completed(argv, "greeting\n")
        if argv[:3] == ["get", "configmap", HOLD_NAME]:
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
            lambda host, ca, *address: (
                fetch_code,
                "testapp\n" if not fetch_code else "certificate rejected",
            )
        ),
        issuer or (lambda host, ca, *address: (0, "issuer=CN = fixture-ca")),
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
    assert "PASS repository integrity check" in output
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
        sys.modules["jarvis.verify.platform"],
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
    registry = sys.modules["jarvis.verify.registry"]
    ran = {"extra": False}

    def extra(_ctx: object) -> list[tuple[int, str, object]]:
        def check() -> None:
            ran["extra"] = True

        return [(99, "extra check", check)]

    registry.COMPONENTS["extra"] = extra
    try:
        cluster = Cluster()
        assert _run(tmp_path, cluster, monkeypatch) == 0
        assert ran["extra"] is False
        cluster_on = Cluster()
        assert _run(tmp_path, cluster_on, monkeypatch, enabled=("extra",)) == 0
        assert ran["extra"] is True
    finally:
        registry.COMPONENTS.pop("extra", None)


def _enable_addons(root: Path, addons: list[str]) -> None:
    from support import link_core_with_fixture_addons

    link_core_with_fixture_addons(root)
    path = root / "envs" / "env-test" / "flux" / "kustomizations" / "services.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"kind": "Kustomization", "spec": {"components": addons}}), encoding="utf-8")


def test_core_only_env_passes_and_a_disabled_addon_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's checks always run. An add-on's checks load only when the env lists it."""
    core_only = Cluster()
    assert _run(tmp_path, core_only, monkeypatch) == 0
    assert not any("hello" in call for call in core_only.calls)
    _enable_addons(tmp_path, ["hello"])
    missing = Cluster()
    assert _run(tmp_path, missing, monkeypatch) == 1
    assert ["get", "deployments", "-n", "hello", "-o", "json"] in missing.calls
    ready = Cluster()
    ready.hello = True
    assert _run(tmp_path, ready, monkeypatch) == 0
    assert any(call[:2] == ["get", "pvc"] for call in ready.calls)


def test_explicit_enabled_list_keeps_default_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = Cluster()
    assert _run(tmp_path, cluster, monkeypatch, enabled=("platform",)) == 0
    joined = [" ".join(call) for call in cluster.calls]
    assert any("backups-restic" in text for text in joined)
    assert any("etcd-snapshots" in text for text in joined)


# NOW is Saturday 12:00Z. One interval is 12h for the etcd copy and 7d for the weekly check.
_FIRST_RUN_CHECKS = {
    "etcd": ("backup freshness", "etcd snapshot copy"),
    "check": ("repository integrity check", "repository check"),
}
_YOUNG = "2026-10-31T11:00:00Z"
_PAST_INTERVAL = "2026-10-20T00:00:00Z"


@pytest.mark.parametrize("check", sorted(_FIRST_RUN_CHECKS))
@pytest.mark.parametrize(
    ("created", "scheduled", "active", "line"),
    [
        (_YOUNG, None, False, "WARN {label}: {what} pending first run"),
        (_YOUNG, "2026-10-31T11:05:00Z", True, "WARN {label}: {what} first run in progress"),
        (
            _YOUNG,
            "2026-10-31T11:05:00Z",
            False,
            "FAIL {label}: {what} has no successful scheduled run",
        ),
        (
            _PAST_INTERVAL,
            "2026-10-31T11:05:00Z",
            True,
            "FAIL {label}: {what} has no successful scheduled run",
        ),
        (_PAST_INTERVAL, None, False, "FAIL {label}: {what} has no successful scheduled run"),
    ],
)
def test_first_run_warns_only_while_pending_or_running_in_its_first_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    check: str,
    created: str,
    scheduled: str | None,
    active: bool,
    line: str,
) -> None:
    """Both CronJob checks share one rule: a run that finished without success, or a job
    past its first interval, fails; a pending or running first run only warns."""
    label, what = _FIRST_RUN_CHECKS[check]
    cluster = Cluster()
    setattr(cluster, f"{check}_sync", None)
    setattr(cluster, f"{check}_created", created)
    setattr(cluster, f"{check}_scheduled", scheduled)
    setattr(cluster, f"{check}_active", active)
    expected = line.format(label=label, what=what)
    assert _run(tmp_path, cluster, monkeypatch) == (0 if expected.startswith("WARN") else 1)
    output = capsys.readouterr().out
    assert expected in output
    assert f"FAIL {label}" not in output or expected.startswith("FAIL")


@pytest.mark.parametrize(
    ("created", "code", "line"),
    [
        (
            "2026-10-24T06:00:00Z",
            0,
            "WARN repository integrity check: repository check first run in progress",
        ),
        (
            "2026-10-23T23:00:00Z",
            1,
            "FAIL repository integrity check: repository check has no successful scheduled run",
        ),
    ],
)
def test_repository_check_first_run_gets_the_same_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    created: str,
    code: int,
    line: str,
) -> None:
    """A first run still going just past one interval after creation warns, not fails."""
    cluster = Cluster()
    cluster.check_sync = None
    cluster.check_created = created
    cluster.check_scheduled = "2026-10-31T06:30:00Z"
    cluster.check_active = True
    assert _run(tmp_path, cluster, monkeypatch) == code
    assert line in capsys.readouterr().out


def test_etcd_copy_first_run_gets_no_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unlike the repository check, a first run still going 30 minutes past one 12h interval fails."""
    cluster = Cluster()
    cluster.etcd_sync = None
    cluster.etcd_created = "2026-10-30T23:30:00Z"
    cluster.etcd_scheduled = "2026-10-31T05:05:00Z"
    cluster.etcd_active = True
    assert _run(tmp_path, cluster, monkeypatch) == 1
    assert "FAIL backup freshness: etcd snapshot copy has no successful scheduled run" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("last", "code", "line"),
    [
        ("2026-10-24T01:00:00Z", 0, "PASS repository integrity check"),
        ("2026-10-23T23:00:00Z", 1, "FAIL repository integrity check: repository check is stale"),
    ],
)
def test_repository_check_freshness_allows_one_interval_plus_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    last: str,
    code: int,
    line: str,
) -> None:
    """The weekly check is Wednesday 06:30, so one interval plus grace is 7d12h before NOW."""
    cluster = Cluster()
    cluster.check_sync = last
    assert _run(tmp_path, cluster, monkeypatch) == code
    assert line in capsys.readouterr().out


@pytest.mark.parametrize(
    ("last", "code", "line"),
    [
        ("2026-10-31T04:20:00Z", 0, "PASS off-host copy"),
        ("2026-10-29T04:20:00Z", 1, "FAIL off-host copy: off-host copy is stale"),
    ],
)
def test_offsite_copy_freshness_is_checked_when_the_env_lists_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    last: str,
    code: int,
    line: str,
) -> None:
    """An env that lists the off-host copy gets its last success checked like restic-check."""
    configs = tmp_path / "envs" / "env-test" / "flux" / "platform-configs"
    configs.mkdir(parents=True)
    (configs / "kustomization.yaml").write_text(
        "resources:\n  - ../../../../core/feature/offsite-backup-copy\n", encoding="utf-8"
    )
    cluster = Cluster()
    cluster.offsite_sync = last
    assert _run(tmp_path, cluster, monkeypatch) == code
    assert line in capsys.readouterr().out


def test_unreadable_switches_fail_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule: a switches file verify cannot read fails; it never reads as an env with no app."""
    values = tmp_path / "envs" / "env-test" / "values"
    values.mkdir(parents=True)
    (values / "switches.yaml").write_text("apps: [\n", encoding="utf-8")
    assert _run(tmp_path, Cluster(), monkeypatch, enabled=None) == 1
    assert "FAIL verify components" in capsys.readouterr().out


def _list_apps_server(root: Path) -> None:
    configs = root / "envs" / "env-test" / "flux" / "platform-configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "kustomization.yaml").write_text("resources:\n  - ../../../../core/feature/apps-backup-server\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("core_users", "apps_command", "line"),
    [
        (["restic"], ["/bin/sh", "-c", "exec rest-server --private-repos"], "PASS app backup isolation"),
        (
            ["restic", "ns-app-shop"],
            ["/bin/sh", "-c", "exec rest-server --private-repos"],
            "FAIL app backup isolation: core's rest-server has an app user",
        ),
        (
            ["restic"],
            ["/bin/sh", "-c", "exec rest-server"],
            "FAIL app backup isolation: restic-apps runs without --private-repos",
        ),
    ],
)
def test_app_isolation_static_half(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    core_users: list[str],
    apps_command: list[str],
    line: str,
) -> None:
    """No app user in core's htpasswd, and the apps server confines each user; nothing of the
    htpasswd is printed."""
    _list_apps_server(tmp_path)
    cluster = Cluster()
    cluster.core_users, cluster.apps_command = core_users, apps_command
    assert _run(tmp_path, cluster, monkeypatch) == (0 if line.startswith("PASS") else 1)
    output = capsys.readouterr().out
    assert line in output
    assert SECRET_VALUE not in output


def test_apps_server_reading_core_secret_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The apps server never reads backups-restic, through a volume or the environment."""
    _list_apps_server(tmp_path)
    cluster = Cluster()
    cluster.apps_env_from = [{"secretRef": {"name": "backups-restic"}}]
    assert _run(tmp_path, cluster, monkeypatch) == 1
    assert "FAIL app backup isolation: restic-apps mounts core's backups-restic" in capsys.readouterr().out


def test_app_pvc_without_a_registered_credential_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An app- PVC is never backed up with core's credential, so without its own it fails verify."""
    _list_apps_server(tmp_path)
    cluster = Cluster()
    cluster.pvcs = [("app", "data"), ("app-shop", "db")]
    assert _run(tmp_path, cluster, monkeypatch) == 1
    assert (
        "FAIL backup freshness: app-shop/db: not backed up; app-shop has no registered backup credential"
        in capsys.readouterr().out
    )


def test_repository_check_deadline_fits_inside_verify_grace() -> None:
    """A check Job still inside its deadline never makes verify report it stale."""
    job = yaml.safe_load(
        (REPO_ROOT / "core/platform/backups/restic-check.yaml").read_text(encoding="utf-8")
    )["spec"]["jobTemplate"]["spec"]
    grace = sys.modules["jarvis.verify.backups"].CHECK_GRACE
    assert 0 < job["activeDeadlineSeconds"] < grace.total_seconds()


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
            "metadata": {
                "namespace": "app",
                "name": "web",
                "creationTimestamp": "2026-10-31T11:59:50Z",
            },
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


def test_pending_pod_within_grace_passes(
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
                "creationTimestamp": "2026-10-31T11:56:00Z",
            },
            "status": {"phase": "Pending"},
        }
    ]
    assert _run(tmp_path, cluster, monkeypatch) == 0
    output = capsys.readouterr().out
    assert "PASS pod health" in output
    assert "FAIL pod health" not in output


def test_pending_pod_without_creation_timestamp_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.pods = [
        {
            "metadata": {"namespace": "app", "name": "web"},
            "status": {"phase": "Pending"},
        }
    ]
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert "app/web: Pending" in capsys.readouterr().out


def test_get_pods_error_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.pods_rc = 1
    assert _run(tmp_path, cluster, monkeypatch) != 0
    output = capsys.readouterr().out
    assert "FAIL pod health" in output
    assert "PASS pod health" not in output


def test_get_nodes_error_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster = Cluster()
    cluster.nodes_rc = 1
    assert _run(tmp_path, cluster, monkeypatch) != 0
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "PASS nodes Ready" not in output
    assert "PASS node roles and enablelb" not in output


def test_sleeping_kubectl_times_out_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_mod = sys.modules["jarvis.lib.env"]
    monkeypatch.setattr(verify_tool, "KUBECTL_TIMEOUT", 0.05)

    def sleeper(argv: object, **kwargs: object) -> CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        if timeout is None:
            return _completed(list(argv) if isinstance(argv, Sequence) else [], {})
        time.sleep(float(timeout) + 0.05)
        raise env_mod.subprocess.TimeoutExpired(argv, float(timeout))

    monkeypatch.setattr(env_mod.subprocess, "run", sleeper)

    def kubectl(
        args: Sequence[str], input: str | None = None
    ) -> CompletedProcess[str]:
        return verify_tool.bound_kubectl(
            args,
            input=input,
            repo_root=tmp_path,
            kubeconfig=tmp_path / "kubeconfig",
        )

    _write_env(tmp_path)
    monkeypatch.setattr(
        verify_tool,
        "_inventory",
        lambda _root, _env: {"n1": {"control_plane", "edge"}},
    )
    rc = verify_tool.run(
        tmp_path,
        "env-test",
        kubectl,
        lambda host, ca, *address: (0, "testapp\n"),
        lambda host, ca, *address: (0, "issuer=CN = fixture-ca"),
        now=NOW,
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL" in captured.out
    assert "timed out" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


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
    assert SECRET_KEYS_TEMPLATE in output
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
            issuer=lambda host, ca, *address: (1, "verify error:num=19:self signed certificate"),
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
            fetch=lambda host, ca, *address: (0, "not-the-canary\n"),
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


def test_verify_starts_from_its_real_entry_point(tmp_path: Path) -> None:
    """Rule: make verify runs tools/verify/run as a script, with no test's import path to lean on."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/verify/run"), "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr


def test_https_without_app_dns_connects_to_traefiks_address(tmp_path: Path) -> None:
    """Rule: with no App DNS, the canary and its certificate are read at Traefik's address."""
    from jarvis.verify import platform as verify_platform

    env = tmp_path / "envs" / "env-test"
    env.mkdir(parents=True)
    (env / "ca.crt").write_text("ca\n", encoding="utf-8")
    seen: list[tuple] = []
    canary = "testapp\n"

    def fetch(host, ca, *address):
        seen.append(("fetch", host, *address))
        return 0, canary

    def issuer(host, ca, *address):
        seen.append(("issuer", host, *address))
        return 0, "issuer=CN = fixture-ca"

    cluster = Cluster()
    address = verify_platform.traefik_address(cluster.kubectl)
    assert address == "192.0.2.10"
    verify_platform.check_https(tmp_path, "env-test", "env-test.example", fetch, issuer, address=address)
    assert seen == [("fetch", "testapp.env-test.example", address), ("issuer", "testapp.env-test.example", address)]
    seen.clear()
    verify_platform.check_https(tmp_path, "env-test", "env-test.example", fetch, issuer)
    assert seen == [("fetch", "testapp.env-test.example"), ("issuer", "testapp.env-test.example")]
