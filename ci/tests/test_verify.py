"""Standing-state verification uses only reads and fails closed. No cluster."""

from __future__ import annotations

import importlib
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

from jarvis.env import flux_state
from jarvis.verify.backups import HOLD_NAME, SECRET_KEYS_TEMPLATE
from jarvis.verify.platform import ENABLELB, FLUX_RESOURCES
from support import REPO_ROOT

SECRET_VALUE = "SHOULD_NOT_PRINT"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

verify_tool = importlib.import_module("jarvis.verify.run")


def _completed(args: Sequence[str], value: object = "", *, returncode=0, stderr="") -> CompletedProcess[str]:
    stdout = json.dumps(value) if not isinstance(value, str) else value
    return CompletedProcess(list(args), returncode, stdout, stderr)


def _write_env(root: Path, *, marker: str = "env-test", time_zone: str | None = "Etc/UTC") -> str:
    env = "env-test"
    values = root / "envs" / env / "values"
    values.mkdir(parents=True, exist_ok=True)
    text = f"jarvis_env: {marker}\ndns_suffix: test.example\n"
    if time_zone is not None:
        text += f"time_zone: {time_zone}\n"
    (values / "cluster-vars.yaml").write_text(text, encoding="utf-8")
    (root / "envs" / env / "ca.crt").write_text("fixture CA\n", encoding="utf-8")
    return env


def _cronjob(created: str, schedule: str, zone, last, scheduled=None, active: str | None = None) -> dict:
    status: dict = {}
    if last is not None:
        status["lastSuccessfulTime"] = last
    if scheduled is not None:
        status["lastScheduleTime"] = scheduled
    if active:
        status["active"] = [{"kind": "Job", "name": f"{active}-1"}]
    spec = {"schedule": schedule, "timeZone": zone}
    return {"metadata": {"creationTimestamp": created}, "spec": spec, "status": status}


_ROOT_REVISION = "refs/heads/main@sha1:0123456789abcdef0123456789abcdef01234567"


def _flux(kind: str, ready: bool, *, artifact: dict | None = None) -> dict:
    """One Flux object named flux-system at generation 1, as kubectl lists it."""
    condition = {"type": "Ready", "status": str(ready), "message": "not Ready"}
    status: dict = {"observedGeneration": 1, "conditions": [condition]}
    spec: dict = {}
    if artifact:
        status["artifact"] = artifact
    if kind == "Kustomization":
        spec = {"interval": "10m", "sourceRef": {"kind": "GitRepository", "name": "flux-system"}}
        status |= {"lastAppliedRevision": _ROOT_REVISION, "history": [{"lastReconciled": "2026-10-31T11:00:00Z"}]}
    metadata = {"name": "flux-system", "namespace": "flux-system", "generation": 1}
    return {"kind": kind, "metadata": metadata, "spec": spec, "status": status}


class Cluster:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.marker = "env-test"
        self.flux_empty = False
        self.flux_ready = True
        # When the root source last archived an artifact; its Kustomization last reconciled at 11:00.
        self.artifact_at = "2026-10-31T10:00:00Z"
        self.secret_rc = 0
        self.node_roles = {"control_plane", "edge"}
        self.enablelb = True
        self.source = True
        self.source_schedule = "0 1 * * *"
        self.source_sync = "2026-10-31T01:01:00Z"
        self.source_created = "2026-01-01T00:00:00Z"
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
        self.pvcs = [("app", "data")]

    def kubectl(self, args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        assert input is None
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["get", "configmap", "cluster-vars"]:
            return _completed(argv, {"data": {"jarvis_env": self.marker}})
        if argv[:5] == ["get", "service", "-n", "kube-system", "traefik"]:
            # An env with no App DNS: verify reaches Traefik's ServiceLB address.
            return _completed(argv, {"status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.10"}]}}})
        if argv[:2] == ["get", flux_state.SOURCES]:
            artifact = {"revision": _ROOT_REVISION, "lastUpdateTime": self.artifact_at}
            return _completed(argv, {"items": [_flux("GitRepository", self.flux_ready, artifact=artifact)]})
        if argv[0] == "get" and argv[1] in FLUX_RESOURCES:
            kinds = {"kustomizations": "Kustomization", "gitrepositories": "GitRepository"}
            kind = kinds.get(argv[1].split(".")[0], "HelmRelease")
            return _completed(argv, {"items": [] if self.flux_empty else [_flux(kind, self.flux_ready)]})
        if argv[:3] == ["get", "secret", "backups-restic"]:
            if self.secret_rc:
                return _completed(argv, "", returncode=1, stderr="forbidden")
            output = " ".join(argv)
            if "go-template" in output or "jsonpath" in output:
                return _completed(argv, "RESTIC_PASSWORD\n")
            return _completed(argv, {"data": {"RESTIC_PASSWORD": SECRET_VALUE}})
        if argv[:2] == ["get", "nodes"]:
            labels = {f"node-role.jarvis.io/{role}": "true" for role in self.node_roles}
            if self.enablelb:
                labels[ENABLELB] = "true"
            ready = {"type": "Ready", "status": "True" if self.node_ready else "False"}
            body = {"items": [{"metadata": {"name": "n1", "labels": labels}, "status": {"conditions": [ready]}}]}
            return _completed(argv, body, returncode=self.nodes_rc, stderr="forbidden" if self.nodes_rc else "")
        if argv[:2] == ["get", "pods"]:
            if self.pods_rc:
                return _completed(argv, {"items": []}, returncode=self.pods_rc, stderr="forbidden")
            return _completed(argv, {"items": self.pods})
        if argv[:2] == ["get", "pvc"]:
            items = [{"metadata": {"namespace": ns, "name": name}, "spec": {"volumeName": f"pv-{name}"}}
                     for ns, name in self.pvcs]  # fmt: skip
            return _completed(argv, {"items": items})
        if argv[:2] == ["get", "replicationsource"]:
            if self.bad_sources_json:
                return _completed(argv, {"items": "unreadable"})
            source = {
                "metadata": {"namespace": "app", "name": "data", "creationTimestamp": self.source_created},
                "spec": {"trigger": {"schedule": self.source_schedule}},
                "status": {"lastSyncTime": self.source_sync},
            }
            return _completed(argv, {"items": [source] if self.source else []})
        if argv[:3] == ["get", "cronjob", "etcd-snapshots"]:
            active = "etcd-snapshots" if self.etcd_active else None
            job = _cronjob(self.etcd_created, self.etcd_schedule, self.etcd_zone, self.etcd_sync, self.etcd_scheduled,
                           active)  # fmt: skip
            return _completed(argv, job)
        if argv[:3] == ["get", "cronjob", "restic-check"]:
            active = "restic-check" if self.check_active else None
            job = _cronjob(self.check_created, "30 6 * * 3", "Etc/UTC", self.check_sync, self.check_scheduled, active)
            return _completed(argv, job)
        if argv[:3] == ["get", "cronjob", "restic-offsite"]:
            return _completed(argv, _cronjob("2026-01-01T00:00:00Z", "0 4 * * *", "Etc/UTC", self.offsite_sync))
        if self.hello and argv[:4] == ["get", "deployments", "-n", "hello"]:
            return _completed(argv, {"items": [{"metadata": {"name": "hello"}, "status": {"readyReplicas": 1}}]})
        if self.hello and argv[:3] == ["get", "secret", "hello-greeting"] and "go-template" in " ".join(argv):
            return _completed(argv, "greeting\n")
        if argv[:3] == ["get", "configmap", HOLD_NAME]:
            if self.hold:
                return _completed(argv, {"data": {"repos": "ns-app/pvc-data\n"}})
            return _completed(argv, "", returncode=1, stderr='Error from server (NotFound): configmap "hold" not found')
        return _completed(argv, "", returncode=1, stderr=f"unexpected {argv}")


def _canary(host, ca, *address):
    return 0, "testapp\n"


def _issued(host, ca, *address):
    return 0, "issuer=CN = fixture-ca"


def _run(
    root: Path,
    cluster: Cluster,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo_marker: str = "env-test",
    time_zone: str | None = "Etc/UTC",
    enabled: Sequence[str] | None = None,
    fetch=_canary,
    issuer=_issued,
    kubectl=None,
) -> int:
    _write_env(root, marker=repo_marker, time_zone=time_zone)
    monkeypatch.setattr(verify_tool, "_inventory", lambda _root, _env: {"n1": {"control_plane", "edge"}})
    return verify_tool.run(root, "env-test", kubectl or cluster.kubectl, fetch, issuer, now=NOW, enabled=enabled)


_DATA_FLAGS = {"-d", "--data", "--data-ascii", "--data-binary", "--data-raw", "--data-urlencode", "--json",
               "-F", "--form", "--form-string", "-T", "--upload-file"}  # fmt: skip


def _assert_read_only_calls(kubectl_calls: list[list[str]], external_calls: list[list[str]]) -> None:
    assert all(call and call[0] == "get" for call in kubectl_calls)
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
        assert not any(
            arg in _DATA_FLAGS
            or (arg.startswith(("-d", "-F", "-T")) and len(arg) > 2)
            or any(arg.startswith(f"{flag}=") for flag in _DATA_FLAGS if flag.startswith("--"))
            for arg in call
        )


def test_verify_passes_without_writes_or_secret_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Every check passes on reads alone, and the restic Secret is asked for its key names only."""
    cluster = Cluster()
    assert _run(tmp_path, cluster, monkeypatch) == 0
    output = capsys.readouterr().out
    assert SECRET_VALUE not in output
    assert "owner" not in output.lower()
    for line in ("Flux Ready", "backups-restic present", "node roles and enablelb", "nodes Ready", "pod health",
                 "backup freshness", "repository integrity check",
                 "LAN HTTPS and certificate issuer=issuer=CN = fixture-ca"):
        assert f"PASS {line}" in output  # fmt: skip
    _assert_read_only_calls(cluster.calls, [])
    secret_calls = [call for call in cluster.calls if call[:3] == ["get", "secret", "backups-restic"]]
    assert secret_calls
    fmt = secret_calls[0][secret_calls[0].index("-o") + 1]
    assert fmt.startswith("go-template=") and SECRET_KEYS_TEMPLATE in fmt and "$k" in fmt
    assert SECRET_VALUE not in " ".join(secret_calls[0])


def test_verify_curl_is_get_only_without_request_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Cluster()
    external: list[list[str]] = []

    def invoke(argv: Sequence[str], *, input: str | None = None, cwd: Path | None = None) -> CompletedProcess[str]:
        call = list(argv)
        external.append(call)
        if call[0] == "curl":
            return _completed(call, "testapp\n")
        if call[:2] == ["openssl", "s_client"]:
            return _completed(call, "CERT\n")
        if call[:2] == ["openssl", "x509"]:
            return _completed(call, "issuer=CN = fixture-ca\n")
        raise AssertionError(call)

    monkeypatch.setattr(sys.modules["jarvis.verify.platform"], "_run", invoke)
    assert _run(tmp_path, cluster, monkeypatch, fetch=verify_tool._fetch, issuer=verify_tool._issuer) == 0
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
def test_read_only_guard_rejects_seeded_writes(kubectl_calls: list[list[str]], external_calls: list[list[str]]) -> None:
    with pytest.raises(AssertionError):
        _assert_read_only_calls(kubectl_calls, external_calls)


def _pod(phase: str, created: str | None = "2026-10-31T11:59:50Z", waiting: str | None = None) -> dict:
    metadata = {"namespace": "app", "name": "web", **({"creationTimestamp": created} if created else {})}
    status: dict = {"phase": phase}
    if waiting:
        status["containerStatuses"] = [{"state": {"waiting": {"reason": waiting}}}]
    return {"metadata": metadata, "status": status}


# Each row: the Cluster field to set, its value, a line the output must hold, and one it must not.
_BAD_STATES = {
    "no-flux-objects": ("flux_empty", True, "none found", None),
    "flux-not-ready": ("flux_ready", False, "not Ready", None),
    # A core pin move: the root source's new artifact keeps its revision, and the Kustomization
    # still reads Ready from before it.
    "flux-behind-its-artifact": ("artifact_at", "2026-10-31T11:30:00Z", "has not applied flux-system's artifact", None),
    "secret-forbidden": ("secret_rc", 1, "forbidden", None),
    "role-labels": ("node_roles", {"control_plane"}, "role labels", None),
    "no-enablelb": ("enablelb", False, "enablelb nodes", None),
    "no-source": ("source", False, "app/data: no ReplicationSource", None),
    "stale-source": ("source_sync", "2026-10-27T01:00:00Z", "last successful sync is stale", None),
    "stale-etcd-copy": ("etcd_sync", "2026-10-29T05:00:00Z", "etcd snapshot copy is stale", None),
    "rebuild-hold": ("hold", True, "rebuild hold is present", None),
    "sources-not-json": ("bad_sources_json", True, "invalid items", None),
    "node-not-ready": ("node_ready", False, "n1: not Ready", None),
    "nodes-error": ("nodes_rc", 1, "FAIL nodes Ready", "PASS node roles and enablelb"),
    "pods-error": ("pods_rc", 1, "FAIL pod health", None),
    # A pod's spec is never printed.
    "crashloop": ("pods", [_pod("Running", waiting="CrashLoopBackOff")], "app/web: CrashLoopBackOff", "spec"),
    "pending-past-grace": ("pods", [_pod("Pending", "2026-10-31T11:54:59Z")], "app/web: Pending", None),
    "pending-no-timestamp": ("pods", [_pod("Pending", None)], "app/web: Pending", None),
}


@pytest.mark.parametrize(("field", "value", "message", "absent"), _BAD_STATES.values(), ids=_BAD_STATES)
def test_each_bad_standing_state_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, field: str, value, message: str, absent: str | None
) -> None:
    cluster = Cluster()
    setattr(cluster, field, value)
    assert _run(tmp_path, cluster, monkeypatch) != 0
    output = capsys.readouterr().out
    assert message in output
    assert f"PASS {message.removeprefix('FAIL ')}" not in output and SECRET_VALUE not in output
    assert absent is None or absent not in output


def test_a_pending_pod_within_its_grace_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    cluster = Cluster()
    cluster.pods = [_pod("Pending", "2026-10-31T11:56:00Z")]
    assert _run(tmp_path, cluster, monkeypatch) == 0
    assert "PASS pod health" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("fetch", "issuer", "message"),
    [
        (lambda host, ca, *address: (1, "certificate rejected"), _issued, "certificate rejected"),
        (_canary, lambda host, ca, *address: (1, "verify error:num=19:self signed certificate"), "verify error"),
        (lambda host, ca, *address: (0, "not-the-canary\n"), _issued, "unexpected body"),
    ],
    ids=["certificate-rejected", "chain-unverified", "wrong-canary"],
)
def test_https_fails_on_a_bad_certificate_or_canary(tmp_path, monkeypatch, capsys, fetch, issuer, message) -> None:
    assert _run(tmp_path, Cluster(), monkeypatch, fetch=fetch, issuer=issuer) != 0
    assert message in capsys.readouterr().out


def test_wrong_cluster_marker_stops_after_marker_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    cluster = Cluster()
    cluster.marker = "wrong"
    assert _run(tmp_path, cluster, monkeypatch) != 0
    assert cluster.calls == [["get", "configmap", "cluster-vars", "-n", "flux-system", "-o", "json"]]
    assert "environment marker is not ENV=env-test" in capsys.readouterr().out


def test_wrong_repository_marker_refuses_before_kubectl(tmp_path: Path, monkeypatch, capsys) -> None:
    cluster = Cluster()
    assert _run(tmp_path, cluster, monkeypatch, repo_marker="wrong") != 0
    assert cluster.calls == []
    assert "does not match its repository marker" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("change", "time_zone", "message"),
    [
        (lambda cluster: setattr(cluster, "source_schedule", "not a cron"), "Etc/UTC", "schedule"),
        (lambda cluster: setattr(cluster, "etcd_schedule", "not a cron"), "Etc/UTC", "schedule"),
        (lambda cluster: setattr(cluster, "source_schedule", "60 1 * * *"), "Etc/UTC", "cron field"),
        (lambda cluster: None, "Not/AZone", "time zone"),
        (lambda cluster: None, None, "time_zone"),
        (lambda cluster: setattr(cluster, "etcd_zone", "Not/AZone"), "Etc/UTC", "time zone"),
        (lambda cluster: setattr(cluster, "etcd_zone", None), "Etc/UTC", "no successful scheduled run"),
    ],
    ids=[
        "source-not-cron", "etcd-not-cron", "cron-field-out-of-range", "bad-env-zone", "no-env-zone",
        "bad-etcd-zone", "no-etcd-zone",
    ],
)
def test_bad_schedule_or_timezone_fails_closed(tmp_path: Path, monkeypatch, capsys, change, time_zone, message) -> None:
    cluster = Cluster()
    change(cluster)
    assert _run(tmp_path, cluster, monkeypatch, time_zone=time_zone) != 0
    assert message in capsys.readouterr().out


def test_kubectl_uses_the_environment_kubeconfig_and_adds_timeouts(monkeypatch, tmp_path: Path) -> None:
    seen: list[tuple[list[str], object]] = []

    def invoke(args: Sequence[str], **kwargs: object) -> CompletedProcess[str]:
        seen.append((list(args), kwargs.get("timeout")))
        return _completed(args)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(verify_tool.run_kubectl.__globals__["subprocess"], "run", invoke)
    kubeconfig = verify_tool.kubeconfig_for("fixture")
    assert kubeconfig == tmp_path / ".kube" / "jarvis-fixture.yaml"
    verify_tool.run_kubectl(["get", "nodes"], repo_root=REPO_ROOT, kubeconfig=kubeconfig)
    kubeconfig.parent.mkdir()
    kubeconfig.write_text("", encoding="utf-8")
    verify_tool.bound_kubectl(["get", "nodes"], repo_root=REPO_ROOT, kubeconfig=kubeconfig)
    assert [argv for argv, _timeout in seen] == [
        ["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes"],
        ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s", "get", "nodes"],
    ]
    assert seen[1][1] == 60.0


def test_disabled_component_checks_do_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = sys.modules["jarvis.verify.registry"]
    ran = {"extra": False}
    registry.COMPONENTS["extra"] = lambda _ctx: [(99, "extra check", lambda: ran.update(extra=True))]
    try:
        assert _run(tmp_path, Cluster(), monkeypatch) == 0
        assert ran["extra"] is False
        assert _run(tmp_path, Cluster(), monkeypatch, enabled=("extra",)) == 0
        assert ran["extra"] is True
    finally:
        registry.COMPONENTS.pop("extra", None)


def _switches(root: Path, key: str, name: str) -> None:
    """The env's switches list name under key (features or addons), as its owner decides it."""
    path = root / "envs" / "env-test" / "values" / "switches.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None) or {}
    data[key] = [*(data.get(key) or []), name]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_core_only_env_passes_and_a_disabled_addon_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Core's checks always run. An add-on's checks load only when the env lists it (the test core
    holds the fixture add-ons)."""
    core_only = Cluster()
    assert _run(tmp_path, core_only, monkeypatch) == 0
    assert not any("hello" in call for call in core_only.calls)
    _switches(tmp_path, "addons", "hello")
    missing = Cluster()
    assert _run(tmp_path, missing, monkeypatch) == 1
    assert ["get", "deployments", "-n", "hello", "-o", "json"] in missing.calls
    ready = Cluster()
    ready.hello = True
    assert _run(tmp_path, ready, monkeypatch) == 0
    assert any(call[:2] == ["get", "pvc"] for call in ready.calls)


def test_explicit_enabled_list_keeps_default_components(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
_NO_RUN = "FAIL {label}: {what} has no successful scheduled run"
_IN_PROGRESS = "WARN {label}: {what} first run in progress"


@pytest.mark.parametrize("check", sorted(_FIRST_RUN_CHECKS))
@pytest.mark.parametrize(
    ("created", "scheduled", "active", "line"),
    [
        (_YOUNG, None, False, "WARN {label}: {what} pending first run"),
        (_YOUNG, "2026-10-31T11:05:00Z", True, _IN_PROGRESS),
        (_YOUNG, "2026-10-31T11:05:00Z", False, _NO_RUN),
        (_PAST_INTERVAL, "2026-10-31T11:05:00Z", True, _NO_RUN),
        (_PAST_INTERVAL, None, False, _NO_RUN),
        # A first run still going just past one interval: the weekly check gets a grace, the 12h etcd copy none.
        ("2026-10-24T06:00:00Z", "2026-10-31T06:30:00Z", True, {"check": _IN_PROGRESS, "etcd": _NO_RUN}),
        ("2026-10-23T23:00:00Z", "2026-10-31T06:30:00Z", True, _NO_RUN),
        ("2026-10-30T23:30:00Z", "2026-10-31T05:05:00Z", True, {"check": _IN_PROGRESS, "etcd": _NO_RUN}),
    ],
    ids=[
        "pending", "running", "finished-unsuccessful", "running-past-interval", "never-ran",
        "just-past-one-interval", "past-the-grace", "etcd-past-12h",
    ],
)
def test_first_run_warns_only_while_pending_or_running_in_its_first_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, check: str, created: str, scheduled, active: bool, line
) -> None:
    """Both CronJob checks share one rule: a run that finished without success, or a job
    past its first interval, fails; a pending or running first run only warns."""
    label, what = _FIRST_RUN_CHECKS[check]
    line = line[check] if isinstance(line, dict) else line
    cluster = Cluster()
    for field, value in (("sync", None), ("created", created), ("scheduled", scheduled), ("active", active)):
        setattr(cluster, f"{check}_{field}", value)
    expected = line.format(label=label, what=what)
    assert _run(tmp_path, cluster, monkeypatch) == (0 if expected.startswith("WARN") else 1)
    output = capsys.readouterr().out
    assert expected in output
    assert f"FAIL {label}" not in output or expected.startswith("FAIL")


@pytest.mark.parametrize(
    ("created", "hold", "line"),
    [
        # The volume's source syncs daily at 01:00; NOW is 12:00.
        ("2026-10-31T09:00:00Z", False, "WARN backup freshness: app/data pending first scheduled sync"),
        ("2026-10-29T09:00:00Z", False, "FAIL backup freshness: app/data: no completed scheduled sync"),
        ("2026-10-31T09:00:00Z", True, "FAIL backup freshness: backup rebuild hold is present"),
    ],
    ids=["restored", "never-synced", "restored-under-hold"],
)
def test_a_restored_volume_warns_until_its_first_scheduled_sync(tmp_path, monkeypatch, capsys, created, hold, line):
    """Rule: a volume's source made inside its first interval, as a restore leaves it, warns until
    it syncs, as the other first runs do; past that interval it fails, and a hold still fails."""
    cluster = Cluster()
    cluster.source_sync, cluster.source_created, cluster.hold = None, created, hold
    assert _run(tmp_path, cluster, monkeypatch) == (0 if line.startswith("WARN") else 1)
    assert line in capsys.readouterr().out


@pytest.mark.parametrize(
    ("feature", "field", "last", "line"),
    [
        # The weekly check is Wednesday 06:30, so one interval plus grace is 7d12h before NOW.
        (None, "check_sync", "2026-10-24T01:00:00Z", "PASS repository integrity check"),
        (None, "check_sync", "2026-10-23T23:00:00Z", "FAIL repository integrity check: repository check is stale"),
        # An env that lists the off-host copy gets its last success checked like restic-check.
        ("offsite-backup-copy", "offsite_sync", "2026-10-31T04:20:00Z", "PASS off-host copy"),
        ("offsite-backup-copy", "offsite_sync", "2026-10-29T04:20:00Z", "FAIL off-host copy: off-host copy is stale"),
    ],
    ids=["check-in-grace", "check-stale", "offsite-fresh", "offsite-stale"],
)
def test_copy_freshness_allows_one_interval_plus_grace(tmp_path, monkeypatch, capsys, feature, field, last, line):
    if feature:
        _switches(tmp_path, "features", feature)
    cluster = Cluster()
    setattr(cluster, field, last)
    assert _run(tmp_path, cluster, monkeypatch) == (0 if line.startswith("PASS") else 1)
    assert line in capsys.readouterr().out


def test_unreadable_switches_fail_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: a switches or apps file verify cannot read fails; it never reads as an env that
    enables nothing."""
    values = tmp_path / "envs" / "env-test" / "values"
    values.mkdir(parents=True)
    for name in ("switches.yaml", "apps.yaml"):
        (values / name).write_text("features: [\n", encoding="utf-8")
        assert _run(tmp_path, Cluster(), monkeypatch) == 1
        assert "FAIL verify components" in capsys.readouterr().out
        (values / name).unlink()


def test_core_freshness_leaves_an_app_volume_without_a_source_to_the_apps_component(tmp_path, monkeypatch, capsys):
    """An app- PVC is never backed up with core's credential; one with no source yet is the
    apps component's to report, so core's freshness check passes."""
    _switches(tmp_path, "features", "app-hosting")
    cluster = Cluster()
    cluster.pvcs = [("app", "data"), ("app-shop", "db")]
    assert _run(tmp_path, cluster, monkeypatch) == 0
    assert "PASS backup freshness" in capsys.readouterr().out


def test_sleeping_kubectl_times_out_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    env_mod = sys.modules["jarvis.env.runtime"]
    monkeypatch.setattr(verify_tool, "KUBECTL_TIMEOUT", 0.05)

    def sleeper(argv: object, **kwargs: object) -> CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        if timeout is None:
            return _completed(list(argv) if isinstance(argv, Sequence) else [], {})
        time.sleep(float(timeout) + 0.05)
        raise env_mod.subprocess.TimeoutExpired(argv, float(timeout))

    monkeypatch.setattr(env_mod.subprocess, "run", sleeper)

    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        return verify_tool.bound_kubectl(args, input=input, repo_root=tmp_path, kubeconfig=tmp_path / "kubeconfig")

    assert _run(tmp_path, Cluster(), monkeypatch, kubectl=kubectl) == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out and "timed out" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_verify_starts_from_its_real_entry_point(tmp_path: Path) -> None:
    """Rule: jarvis verify starts from the installed CLI, with no test's import path to lean on."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    done = subprocess.run(
        [str(Path(sys.executable).parent / "jarvis"), "verify", "--help"],
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

    def fetch(host, ca, *address):
        seen.append(("fetch", host, *address))
        return _canary(host, ca)

    def issuer(host, ca, *address):
        seen.append(("issuer", host, *address))
        return _issued(host, ca)

    address = verify_platform.traefik_address(Cluster().kubectl)
    assert address == "192.0.2.10"
    verify_platform.check_https(tmp_path, "env-test", "env-test.example", fetch, issuer, address=address)
    assert seen == [("fetch", "testapp.env-test.example", address), ("issuer", "testapp.env-test.example", address)]
    seen.clear()
    verify_platform.check_https(tmp_path, "env-test", "env-test.example", fetch, issuer)
    assert seen == [("fetch", "testapp.env-test.example"), ("issuer", "testapp.env-test.example")]
