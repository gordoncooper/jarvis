"""Standing-state verification uses only reads, prints one line per check and fails closed. No cluster."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from jarvis.env import flux_state
from jarvis.verify import platform as verify_platform
from jarvis.verify import registry
from jarvis.verify import run as verify_tool
from jarvis.verify.backups import HOLD_NAME, SECRET_KEYS_TEMPLATE
from jarvis.verify.platform import ENABLELB, FLUX_RESOURCES
from support import REPO_ROOT, completed, write_env_marker

SECRET_VALUE = "SHOULD_NOT_PRINT"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


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


def _pod(phase: str, created: str | None = "2026-10-31T11:59:50Z", waiting: str | None = None) -> dict:
    metadata = {"namespace": "app", "name": "web", **({"creationTimestamp": created} if created else {})}
    status: dict = {"phase": phase}
    if waiting:
        status["containerStatuses"] = [{"state": {"waiting": {"reason": waiting}}}]
    return {"metadata": metadata, "status": status}


@dataclass
class Cluster:
    """A sound env for verify: its own files, kubectl on its cluster and the canary's HTTPS answers. Each field is one
    piece a case can change. It records each kubectl call."""

    # The env's files: its marker and time zone in cluster-vars, and its switches and apps files when given.
    repo_marker: str = "env-test"
    time_zone: str | None = "Etc/UTC"
    switches: str | None = None
    apps: str | None = None
    marker: str = "env-test"
    flux_empty: bool = False
    flux_ready: bool = True
    # When the root source last archived an artifact; its Kustomization last reconciled at 11:00.
    artifact_at: str = "2026-10-31T10:00:00Z"
    secret_rc: int = 0
    node_roles: set[str] = field(default_factory=lambda: {"control_plane", "edge"})
    enablelb: bool = True
    source: bool = True
    source_schedule: str = "0 1 * * *"
    source_sync: str | None = "2026-10-31T01:01:00Z"
    source_created: str = "2026-01-01T00:00:00Z"
    etcd_schedule: str = "5 */12 * * *"
    etcd_zone: str | None = "Etc/UTC"
    etcd_sync: str | None = "2026-10-31T05:10:00Z"
    etcd_scheduled: str | None = None
    etcd_created: str = "2026-01-01T00:00:00Z"
    etcd_active: bool = False
    check_sync: str | None = "2026-10-28T06:40:00Z"
    check_created: str = "2026-01-01T00:00:00Z"
    check_scheduled: str | None = None
    check_active: bool = False
    offsite_sync: str = "2026-10-31T04:20:00Z"
    hold: bool = False
    # How the hold's read fails when there is no hold.
    hold_error: str = 'Error from server (NotFound): configmap "hold" not found'
    bad_sources_json: bool = False
    node_ready: bool = True
    # More labels on node n1.
    labels: dict[str, str] = field(default_factory=dict)
    # The kubelet's last Lease renewal, and the API server's identity Lease's: the cluster's now.
    lease_renewed: str | None = "2026-10-31T11:59:50.000000Z"
    control_plane_renewed: str | None = "2026-10-31T11:59:58.000000Z"
    nodes_rc: int = 0
    pods_rc: int = 0
    pods: list[dict] = field(default_factory=list)
    hello: bool = False
    pvcs: list[tuple[str, str]] = field(default_factory=lambda: [("app", "data")])
    canary: tuple[int, str] = (0, "testapp\n")
    issued: tuple[int, str] = (0, "issuer=CN = fixture-ca")
    calls: list[list[str]] = field(default_factory=list)

    def fetch(self, host: str, ca: Path, *address: str) -> tuple[int, str]:
        return self.canary

    def issuer(self, host: str, ca: Path, *address: str) -> tuple[int, str]:
        return self.issued

    def kubectl(self, args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        assert input is None
        argv = list(args)
        self.calls.append(argv)
        # Each read verify makes, whole: its namespace and -A are what it checks.
        read = " ".join(argv)
        if read == "get configmap cluster-vars -n flux-system -o json":
            return completed(argv, {"data": {"jarvis_env": self.marker}})
        if read == "get service -n kube-system traefik -o json":
            # An env with no App DNS: verify reaches Traefik's ServiceLB address.
            return completed(argv, {"status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.10"}]}}})
        if read == f"get {flux_state.SOURCES} -A -o json":
            artifact = {"revision": _ROOT_REVISION, "lastUpdateTime": self.artifact_at}
            return completed(argv, {"items": [_flux("GitRepository", self.flux_ready, artifact=artifact)]})
        if read in {f"get {resource} -A -o json" for resource in FLUX_RESOURCES}:
            kinds = {"kustomizations": "Kustomization", "gitrepositories": "GitRepository"}
            kind = kinds.get(argv[1].split(".")[0], "HelmRelease")
            return completed(argv, {"items": [] if self.flux_empty else [_flux(kind, self.flux_ready)]})
        if argv[:3] == ["get", "secret", "backups-restic"]:
            if self.secret_rc:
                return completed(argv, "", returncode=1, stderr="forbidden")
            if "go-template" in read or "jsonpath" in read:
                return completed(argv, "RESTIC_PASSWORD\n")
            return completed(argv, {"data": {"RESTIC_PASSWORD": SECRET_VALUE}})
        if read == "get nodes -o json":
            labels = {f"node-role.jarvis.io/{role}": "true" for role in self.node_roles} | self.labels
            if self.enablelb:
                labels[ENABLELB] = "true"
            ready = {"type": "Ready", "status": "True" if self.node_ready else "False"}
            body = {"items": [{"metadata": {"name": "n1", "labels": labels}, "status": {"conditions": [ready]}}]}
            return completed(argv, body, returncode=self.nodes_rc, stderr="forbidden" if self.nodes_rc else "")
        if read == "get leases -n kube-system -o json":
            spec = {"leaseDurationSeconds": 3600, "renewTime": self.control_plane_renewed}
            return completed(argv, {"items": [{"metadata": {"name": "apiserver-a1"}, "spec": spec}]})
        if read == "get leases -n kube-node-lease -o json":
            spec = {"holderIdentity": "n1", "leaseDurationSeconds": 40, "renewTime": self.lease_renewed}
            leases = [{"metadata": {"name": "n1"}, "spec": spec}] if self.lease_renewed else []
            return completed(argv, {"items": leases})
        if read == "get pods -A -o json":
            if self.pods_rc:
                return completed(argv, {"items": []}, returncode=self.pods_rc, stderr="forbidden")
            return completed(argv, {"items": self.pods})
        if read == "get pvc -A -o json":
            items = [{"metadata": {"namespace": ns, "name": name}, "spec": {"volumeName": f"pv-{name}"}}
                     for ns, name in self.pvcs]  # fmt: skip
            return completed(argv, {"items": items})
        if read == "get replicationsource -A -o json":
            if self.bad_sources_json:
                return completed(argv, {"items": "unreadable"})
            source = {
                "metadata": {"namespace": "app", "name": "data", "creationTimestamp": self.source_created},
                "spec": {"trigger": {"schedule": self.source_schedule}},
                "status": {"lastSyncTime": self.source_sync},
            }
            return completed(argv, {"items": [source] if self.source else []})
        if read == "get cronjob etcd-snapshots -n platform-backups -o json":
            active = "etcd-snapshots" if self.etcd_active else None
            job = _cronjob(self.etcd_created, self.etcd_schedule, self.etcd_zone, self.etcd_sync, self.etcd_scheduled,
                           active)  # fmt: skip
            return completed(argv, job)
        if read == "get cronjob restic-check -n platform-backups -o json":
            active = "restic-check" if self.check_active else None
            job = _cronjob(self.check_created, "30 6 * * 3", "Etc/UTC", self.check_sync, self.check_scheduled, active)
            return completed(argv, job)
        if read == "get cronjob restic-offsite -n platform-backups -o json":
            return completed(argv, _cronjob("2026-01-01T00:00:00Z", "0 4 * * *", "Etc/UTC", self.offsite_sync))
        if self.hello and argv[:4] == ["get", "deployments", "-n", "hello"]:
            return completed(argv, {"items": [{"metadata": {"name": "hello"}, "status": {"readyReplicas": 1}}]})
        if self.hello and argv[:3] == ["get", "secret", "hello-greeting"] and "go-template" in read:
            return completed(argv, "greeting\n")
        if read == f"get configmap {HOLD_NAME} -n kube-system -o json":
            if self.hold:
                return completed(argv, {"data": {"repos": "ns-app/pvc-data\n"}})
            return completed(argv, "", returncode=1, stderr=self.hold_error)
        return completed(argv, "", returncode=1, stderr=f"unexpected {argv}")


def _run(root: Path, cluster: Cluster, monkeypatch: pytest.MonkeyPatch, *, kubectl=None, fetch=None, issuer=None,
         enabled: Sequence[str] | None = None) -> int:
    """jarvis verify of env-test, its files written as the cluster's fields say."""
    env_dir = write_env_marker(root, marker=cluster.repo_marker, dns_suffix="test.example", time_zone=cluster.time_zone)
    (env_dir / "ca.crt").write_text("fixture CA\n", encoding="utf-8")
    for name, text in (("switches", cluster.switches), ("apps", cluster.apps)):
        if text is not None:
            (env_dir / "values" / f"{name}.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(verify_tool, "_inventory", lambda _root, _env: {"n1": {"control_plane", "edge"}})
    return verify_tool.run(root, "env-test", kubectl or cluster.kubectl, fetch or cluster.fetch, issuer or cluster.issuer,
                           now=NOW, enabled=enabled)


# The line each check prints for a sound env, in verify's order; None: a check such an env does not run.
_LINES = {
    "kubelets": None, "flux": "PASS Flux Ready", "secret": "PASS backups-restic present",
    "roles": "PASS node roles and enablelb", "nodes": "PASS nodes Ready", "pods": "PASS pod health",
    "freshness": "PASS backup freshness", "hello": None, "check": "PASS repository integrity check",
    "offsite": None, "extra": None, "https": "PASS LAN HTTPS and certificate issuer=issuer=CN = fixture-ca",
}


def _out(**lines: str) -> str:
    """verify's whole output: each check's line for a sound env, or the line given in its place."""
    return "".join(f"{line}\n" for key, sound in _LINES.items() if (line := lines.get(key, sound)))


def test_verify_passes_without_writes_or_secret_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: a sound env passes every check on reads alone, and the restic Secret is read for its key names only."""
    cluster = Cluster()
    assert _run(tmp_path, cluster, monkeypatch) == 0
    assert capsys.readouterr().out == _out()
    assert {call[0] for call in cluster.calls} == {"get"}
    key_names = ["get", "secret", "backups-restic", "-n", "platform-backups", "-o", f"go-template={SECRET_KEYS_TEMPLATE}"]
    assert [call for call in cluster.calls if call[:3] == key_names[:3]] == [key_names] and "$k" in SECRET_KEYS_TEMPLATE


@pytest.mark.parametrize(
    ("switches", "resolve", "connect"),
    [
        pytest.param(None, ["--resolve", "testapp.test.example:443:192.0.2.10"], "192.0.2.10:443", id="no-app-dns"),
        pytest.param("features: [app-dns]\n", [], "testapp.test.example:443", id="app-dns"),
    ],
)
def test_https_is_a_get_and_a_chain_check_on_the_env_ca_alone(tmp_path, monkeypatch, capsys, switches, resolve,
                                                              connect) -> None:
    """Rule: the canary is a plain GET and its chain is verified on the env's CA alone; with no App DNS both reach
    Traefik's address."""
    answers = {"curl": "testapp\n", "s_client": "CERT\n", "x509": "issuer=CN = fixture-ca\n"}
    runs: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(verify_platform, "_run", lambda argv, *, input=None, cwd=None: runs.append((list(argv), input))
                        or completed(argv, answers[argv[1] if argv[0] == "openssl" else argv[0]]))
    cluster = Cluster(switches=switches)
    assert _run(tmp_path, cluster, monkeypatch, fetch=verify_tool._fetch, issuer=verify_tool._issuer) == 0
    ca = str(tmp_path / "envs" / "env-test" / "ca.crt")
    assert runs == [
        (["curl", "-fsS", "--max-time", str(verify_platform.HTTP_TIMEOUT_SECONDS), "--cacert", ca, *resolve,
          "https://testapp.test.example/"], None),
        (["openssl", "s_client", "-connect", connect, "-servername", "testapp.test.example", "-CAfile", ca, "-no-CApath",
          "-no-CAstore", "-verify_return_error"], ""),
        (["openssl", "x509", "-noout", "-issuer"], "CERT\n"),
    ]
    assert (capsys.readouterr().out, {call[0] for call in cluster.calls}) == (_out(), {"get"})


_TOO_FRESH = ("FAIL too freshly started to judge: n1: {}. Run verify again once every kubelet reports; if this "
              "persists, check that node's clock against the control plane's\n")


# Each case: the fields it changes in a sound env, and verify's whole output.
_STATES = {
    "no-flux-objects": ({"flux_empty": True},
                        _out(flux="FAIL Flux Ready: kustomizations.kustomize.toolkit.fluxcd.io: none found")),
    "flux-not-ready": ({"flux_ready": False}, _out(flux="FAIL Flux Ready: Kustomization flux-system/flux-system: not "
                       "Ready; GitRepository flux-system/flux-system: not Ready; HelmRelease flux-system/flux-system: not Ready")),
    # A core pin move: the root source's new artifact keeps its revision, and the Kustomization
    # still reads Ready from before it.
    "flux-behind-its-artifact": ({"artifact_at": "2026-10-31T11:30:00Z"}, _out(flux="FAIL Flux Ready: Kustomization "
                                 "flux-system/flux-system: has not applied flux-system's artifact of 2026-10-31T11:30:00Z "
                                 "yet (it reconciles every 10m)")),
    "secret-forbidden": ({"secret_rc": 1}, _out(secret="FAIL backups-restic present: read backups-restic: forbidden")),
    "role-labels": ({"node_roles": {"control_plane"}}, _out(roles="FAIL node roles and enablelb: n1: role labels "
                    "['control_plane'] != node_roles ['control_plane', 'edge']")),
    "no-enablelb": ({"enablelb": False}, _out(roles="FAIL node roles and enablelb: enablelb nodes [] != edge nodes ['n1']")),
    "role-label-not-true": ({"labels": {"node-role.jarvis.io/gpu": "false"}}, _out(roles="FAIL node roles and enablelb: n1: "
                            "role labels ['control_plane', 'edge'] != node_roles ['control_plane', 'edge']")),
    "node-not-ready": ({"node_ready": False}, _out(nodes="FAIL nodes Ready: n1: not Ready")),
    "nodes-error": ({"nodes_rc": 1}, _out(kubelets="FAIL kubelets reporting: list nodes: forbidden",
                    roles="FAIL node roles and enablelb: list nodes: forbidden", nodes="FAIL nodes Ready: list nodes: forbidden")),
    # Right after a boot the kubelet has not renewed its Lease, so no check runs on older status. The Lease is judged
    # by the cluster's clock, kube-system's newest Lease: with none verify fails; in the last case it is a day behind.
    "kubelet-not-reported": ({"lease_renewed": "2026-10-31T11:59:00Z"}, _TOO_FRESH.format(
                             "its kubelet last reported at 2026-10-31T11:59:00Z, 58 s before the control plane")),
    "no-node-lease": ({"lease_renewed": None}, _TOO_FRESH.format("its kubelet has not reported")),
    "no-cluster-clock": ({"control_plane_renewed": None},
                         _out(kubelets="FAIL kubelets reporting: no Lease in kube-system to read the cluster's time from")),
    "cluster-clock-behind": ({"lease_renewed": "2026-10-30T11:59:50Z", "control_plane_renewed": "2026-10-30T12:00:00Z"},
                             _out()),
    "pods-error": ({"pods_rc": 1}, _out(pods="FAIL pod health: list pods: forbidden")),
    # A pod's spec is never printed.
    "crashloop": ({"pods": [_pod("Running", waiting="CrashLoopBackOff")]}, _out(pods="FAIL pod health: app/web: CrashLoopBackOff")),
    "pending-past-grace": ({"pods": [_pod("Pending", "2026-10-31T11:54:59Z")]}, _out(pods="FAIL pod health: app/web: Pending")),
    "pending-in-grace": ({"pods": [_pod("Pending", "2026-10-31T11:56:00Z")]}, _out()),
    "pending-no-timestamp": ({"pods": [_pod("Pending", None)]}, _out(pods="FAIL pod health: app/web: Pending")),
    "no-source": ({"source": False}, _out(freshness="FAIL backup freshness: app/data: no ReplicationSource")),
    "stale-source": ({"source_sync": "2026-10-27T01:00:00Z"},
                     _out(freshness="FAIL backup freshness: app/data: last successful sync is stale")),
    "source-one-run-behind": ({"source_sync": "2026-10-30T01:30:00Z"}, _out()),
    "sources-not-json": ({"bad_sources_json": True}, _out(freshness="FAIL backup freshness: list ReplicationSources: invalid items")),
    "stale-etcd-copy": ({"etcd_sync": "2026-10-29T05:00:00Z"}, _out(freshness="FAIL backup freshness: etcd snapshot copy is stale")),
    "rebuild-hold": ({"hold": True}, _out(freshness="FAIL backup freshness: backup rebuild hold is present")),
    "hold-unreadable": ({"hold_error": "Error from server (Forbidden): configmaps is forbidden"},
                        _out(freshness="FAIL backup freshness: read backup rebuild hold: Error from server (Forbidden): "
                             "configmaps is forbidden")),
    # A volume's source made inside its first interval, as a restore leaves it, warns until it syncs (daily at 01:00);
    # past that interval it fails, and a hold still fails.
    "restored": ({"source_sync": None, "source_created": "2026-10-31T09:00:00Z"},
                 _out(freshness="WARN backup freshness: app/data pending first scheduled sync")),
    "never-synced": ({"source_sync": None, "source_created": "2026-10-29T09:00:00Z"},
                     _out(freshness="FAIL backup freshness: app/data: no completed scheduled sync")),
    "restored-under-hold": ({"source_sync": None, "source_created": "2026-10-31T09:00:00Z", "hold": True},
                            _out(freshness="FAIL backup freshness: backup rebuild hold is present")),
    # An app- volume is never backed up with core's credential; one with no source yet is the apps component's.
    "app-volume-without-a-source": ({"switches": "features: [app-hosting]\n", "pvcs": [("app", "data"), ("app-shop", "db")]},
                                    _out()),
    # A schedule or time zone verify cannot read fails closed.
    "source-not-cron": ({"source_schedule": "not a cron"},
                        _out(freshness="FAIL backup freshness: invalid five-field schedule 'not a cron'")),
    "etcd-not-cron": ({"etcd_schedule": "not a cron"}, _out(freshness="FAIL backup freshness: invalid five-field schedule 'not a cron'")),
    "cron-field-out-of-range": ({"source_schedule": "60 1 * * *"},
                                _out(freshness="FAIL backup freshness: unsupported cron field '60'")),
    "bad-env-zone": ({"time_zone": "Not/AZone"}, _out(freshness="FAIL backup freshness: invalid backup timestamp or time zone")),
    "no-env-zone": ({"time_zone": None}, _out(freshness="FAIL backup freshness: 'time_zone'")),
    "bad-etcd-zone": ({"etcd_zone": "Not/AZone"}, _out(freshness="FAIL backup freshness: invalid backup timestamp or time zone")),
    "no-etcd-zone": ({"etcd_zone": None},
                     _out(freshness="FAIL backup freshness: etcd snapshot copy has no successful scheduled run")),
    # One interval plus grace: the weekly check is Wednesday 06:30, so 7d12h before NOW; an env that lists the
    # off-host copy gets its last success checked the same way.
    "check-in-grace": ({"check_sync": "2026-10-24T01:00:00Z"}, _out()),
    "check-stale": ({"check_sync": "2026-10-23T23:00:00Z"},
                    _out(check="FAIL repository integrity check: repository check is stale; read the restic-check Job logs")),
    "offsite-fresh": ({"switches": "features: [offsite-backup-copy]\n"}, _out(offsite="PASS off-host copy")),
    "offsite-stale": ({"switches": "features: [offsite-backup-copy]\n", "offsite_sync": "2026-10-29T04:20:00Z"},
                      _out(offsite="FAIL off-host copy: off-host copy is stale; read the restic-offsite Job logs")),
    "certificate-rejected": ({"canary": (1, "certificate rejected")},
                             _out(https="FAIL LAN HTTPS and certificate: LAN HTTPS testapp.test.example: certificate rejected")),
    "chain-unverified": ({"issued": (1, "verify error:num=19:self signed certificate")}, _out(https="FAIL LAN HTTPS and "
                         "certificate: certificate chain testapp.test.example: verify error:num=19:self signed certificate")),
    "wrong-canary": ({"canary": (0, "not-the-canary\n")},
                     _out(https="FAIL LAN HTTPS and certificate: LAN HTTPS testapp.test.example: unexpected body")),
}


@pytest.mark.parametrize(("knobs", "out"), _STATES.values(), ids=_STATES)
def test_each_standing_state_prints_its_line(tmp_path, monkeypatch, capsys, knobs: dict, out: str) -> None:
    """Rule: each standing check prints one line, PASS, WARN or FAIL, from reads alone, and any FAIL exits 1. Each
    case changes one piece of a sound env, past a limit or just inside it."""
    cluster = Cluster(**knobs)
    assert _run(tmp_path, cluster, monkeypatch) == int("FAIL " in out)
    assert capsys.readouterr().out == out
    assert {call[0] for call in cluster.calls} == {"get"}


# NOW is Saturday 12:00Z. One interval is 12h for the etcd copy and 7d for the weekly check.
_FIRST_RUN_CHECKS = {
    "etcd": ("freshness", "backup freshness", "etcd snapshot copy"),
    "check": ("check", "repository integrity check", "repository check"),
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
    """Rule: both CronJob checks share one rule: a run that finished without success, or a job past its first
    interval, fails; a pending or running first run only warns."""
    key, label, what = _FIRST_RUN_CHECKS[check]
    line = (line[check] if isinstance(line, dict) else line).format(label=label, what=what)
    fields = {"sync": None, "created": created, "scheduled": scheduled, "active": active}
    cluster = Cluster(**{f"{check}_{name}": value for name, value in fields.items()})
    assert _run(tmp_path, cluster, monkeypatch) == int(line.startswith("FAIL"))
    assert capsys.readouterr().out == _out(**{key: line})


_MARKER_READ = ["get", "configmap", "cluster-vars", "-n", "flux-system", "-o", "json"]
_UNREADABLE = "features: [\n"


def _yaml_error(text: str) -> str:
    with pytest.raises(yaml.YAMLError) as raised:
        yaml.safe_load(text)
    return str(raised.value)


@pytest.mark.parametrize(
    ("knobs", "calls", "out"),
    [
        pytest.param({"repo_marker": "wrong"}, [], "FAIL environment: ENV=env-test does not match its repository marker\n",
                     id="repository-marker"),
        pytest.param({"marker": "wrong"}, [_MARKER_READ],
                     "FAIL environment: refusing cluster whose environment marker is not ENV=env-test\n", id="cluster-marker"),
        # A file verify cannot read never reads as an env that enables nothing.
        pytest.param({"switches": _UNREADABLE}, [_MARKER_READ], "FAIL verify components: values/switches.yaml: invalid "
                     f"YAML: {_yaml_error(_UNREADABLE)}\n", id="unreadable-switches"),
        pytest.param({"apps": _UNREADABLE}, [_MARKER_READ], f"FAIL verify components: values: invalid YAML: "
                     f"{_yaml_error(_UNREADABLE)}\n", id="unreadable-apps"),
    ],
)
def test_verify_refuses_before_any_check(tmp_path, monkeypatch, capsys, knobs: dict, calls: list, out: str) -> None:
    """Rule: the env's marker, the cluster's, and the env's switches and apps files are read before any standing check;
    the first that is wrong or unreadable fails verify, and nothing more is read."""
    cluster = Cluster(**knobs)
    assert _run(tmp_path, cluster, monkeypatch) == 1
    assert (cluster.calls, capsys.readouterr().out) == (calls, out)


def test_kubectl_uses_the_environment_kubeconfig_and_adds_timeouts(monkeypatch, tmp_path: Path) -> None:
    """Rule: kubectl runs on the env's kubeconfig, and verify's bound kubectl adds a request and a process timeout."""
    seen: list[tuple[list[str], object]] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: seen.append((list(args), kwargs["timeout"])) or completed(args))
    kubeconfig = verify_tool.kubeconfig_for("fixture")
    verify_tool.run_kubectl(["get", "nodes"], repo_root=REPO_ROOT, kubeconfig=kubeconfig)
    verify_tool.bound_kubectl(["get", "nodes"], repo_root=REPO_ROOT, kubeconfig=kubeconfig)
    assert kubeconfig == tmp_path / ".kube" / "jarvis-fixture.yaml"
    assert seen == [
        (["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes"], None),
        (["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s", "get", "nodes"], 60.0),
    ]


def test_a_kubectl_that_times_out_is_a_clean_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: a kubectl call past verify's timeout fails as one line, with no traceback."""

    def run(argv: list[str], *, timeout: float | None, **kwargs: object) -> CompletedProcess[str]:
        # A call without a timeout answers at once, so only a timeout verify passes can expire.
        if timeout is None:
            return completed(argv, {})
        raise subprocess.TimeoutExpired(argv, timeout)

    def kubectl(args: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        return verify_tool.bound_kubectl(args, input=input, repo_root=tmp_path, kubeconfig=tmp_path / "kubeconfig")

    monkeypatch.setattr(subprocess, "run", run)
    assert _run(tmp_path, Cluster(), monkeypatch, kubectl=kubectl) == 1
    assert capsys.readouterr() == ("FAIL environment: read cluster environment marker: kubectl timed out\n", "")


@pytest.mark.parametrize(
    ("knobs", "enabled", "out"),
    [
        pytest.param({}, None, _out(), id="core-only"),
        pytest.param({}, ("platform",), _out(), id="platform-listed"),
        pytest.param({}, ("extra",), _out(extra="PASS extra check"), id="extra-enabled"),
        pytest.param({"switches": "addons: [hello]\n"}, None, _out(hello="FAIL add-on hello Ready: list hello deployments: "
                     "unexpected ['get', 'deployments', '-n', 'hello', '-o', 'json']"), id="addon-missing"),
        pytest.param({"switches": "addons: [hello]\n", "hello": True}, None, _out(hello="PASS add-on hello Ready"),
                     id="addon-ready"),
    ],
)
def test_core_checks_always_run_and_others_only_when_enabled(tmp_path, monkeypatch, capsys, knobs: dict, enabled,
                                                             out: str) -> None:
    """Rule: core's components always run, an explicit list included; another runs only when enabled: an add-on the
    env lists (the test core holds the fixture add-on) or a component named."""
    monkeypatch.setitem(registry.COMPONENTS, "extra", lambda _ctx: [(99, "extra check", lambda: None)])
    assert _run(tmp_path, Cluster(**knobs), monkeypatch, enabled=enabled) == int("FAIL " in out)
    assert capsys.readouterr().out == out


def test_verify_starts_from_its_real_entry_point(tmp_path: Path) -> None:
    """Rule: jarvis verify starts from the installed CLI, with no test's import path to lean on."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    done = subprocess.run(
        [str(Path(sys.executable).parent / "jarvis"), "verify", "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert done.returncode == 0 and done.stdout.startswith("usage: jarvis verify "), done
