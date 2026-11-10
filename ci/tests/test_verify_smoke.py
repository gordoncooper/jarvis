"""jarvis verify --smoke: each probe makes its objects, checks them and removes them. No cluster."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from jarvis.verify import smoke
from jarvis.verify.common import CheckError, CheckWarning, Context
from jarvis.verify.registry import SMOKE, collect, register_smoke

TRAEFIK = "10.43.0.10"
APP_DNS = "10.43.0.53"


class Cluster:
    """Just enough API server for the probes: kubectl argv in, CompletedProcess out."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.applied: list[dict] = []
        self.namespace: dict | None = None
        self.ready_after = 1
        self.reads = 0
        self.plain_rc, self.bad_rc = 0, 1
        self.bad_stderr = 'admission webhook denied: grant: anything beyond Pod Security restricted needs the env\'s "x: privileged"'
        self.dry_runs: list[dict] = []

    def __call__(self, args, input=None):  # noqa: A002 - kubectl's own keyword
        args = list(args)
        self.calls.append(args)
        ok = lambda body="": CompletedProcess(args, 0, json.dumps(body) if not isinstance(body, str) else body, "")  # noqa: E731
        if args[:3] == ["get", "namespace", smoke.NAMESPACE]:
            if self.namespace is None:
                return CompletedProcess(args, 1, "", 'Error from server (NotFound): namespaces "platform-smoke" not found')
            return ok(self.namespace)
        if args[:2] == ["delete", "namespace"]:
            self.namespace = None
            return ok()
        if args[:2] == ["apply", "-f"]:
            body = json.loads(input)
            self.applied.append(body)
            if body["kind"] == "Namespace":
                self.namespace = body
            return ok()
        if args[:2] == ["get", "certificate"]:
            self.reads += 1
            status = "True" if self.reads >= self.ready_after else "False"
            return ok({"status": {"conditions": [{"type": "Ready", "status": status}]}})
        if args[:3] == ["get", "service", "-n"]:
            return ok({"status": {"loadBalancer": {"ingress": [{"ip": TRAEFIK}]}}})
        if args[:2] == ["get", "services"]:
            return ok({"items": [{"spec": {"type": "LoadBalancer"}, "status": {"loadBalancer": {"ingress": [{"ip": APP_DNS}]}}}]})
        if args[:2] == ["get", "pods"]:
            pod = {
                "metadata": {
                    "name": "hello-web-abc",
                    "namespace": args[3],
                    "labels": {"app": "hello-web", "pod-template-hash": "abc"},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "hello-web-abc"}],
                },
                "spec": {"nodeName": "node1", "containers": [{"name": "web", "image": "x"}]},
                "status": {"phase": "Running"},
            }
            return ok({"items": [pod]})
        if args[:2] == ["create", "--dry-run=server"]:
            body = json.loads(input)
            self.dry_runs.append(body)
            risky = len(self.dry_runs) % 2 == 0
            rc, err = (self.bad_rc, self.bad_stderr) if risky else (self.plain_rc, "" if not self.plain_rc else "denied: other")
            return CompletedProcess(args, rc, "pod/x" if not rc else "", err if rc else "")
        raise AssertionError(f"unexpected kubectl {args}")


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polls without waiting; the clock moves one poll per sleep."""
    now = [0.0]
    monkeypatch.setattr(smoke, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(smoke, "_clock", lambda: now[0])


def _ctx(tmp_path: Path, cluster: Cluster, apps_text: str | None = None) -> Context:
    values = tmp_path / "envs" / "env-test" / "values"
    values.mkdir(parents=True)
    if apps_text is not None:
        (values / "apps.yaml").write_text(apps_text, encoding="utf-8")
        (values / "switches.yaml").write_text("features: [app-hosting]\napps:\n  - hello-web\n", encoding="utf-8")
    return Context(tmp_path, "env-test", cluster, {"dns_suffix": "test.example"}, datetime(2026, 11, 9, tzinfo=UTC), {})


APPS = "versions:\n  hello-web: v0.1.0\n"


def test_the_scratch_namespace_is_made_and_removed(tmp_path: Path) -> None:
    cluster = Cluster()
    ctx = _ctx(tmp_path, cluster)
    smoke.open_scratch(ctx)
    assert cluster.namespace["metadata"]["labels"] == {smoke.OWNER_LABEL: smoke.OWNER}
    smoke.close_scratch(ctx)
    assert cluster.namespace is None


def test_a_namespace_that_is_not_verifys_is_left_alone(tmp_path: Path) -> None:
    """Rule: smoke deletes platform-smoke only when it carries verify's owner label."""
    cluster = Cluster()
    cluster.namespace = {"metadata": {"name": smoke.NAMESPACE, "labels": {}}}
    ctx = _ctx(tmp_path, cluster)
    with pytest.raises(CheckError, match="not jarvis verify's"):
        smoke.open_scratch(ctx)
    smoke.close_scratch(ctx)
    assert cluster.namespace is not None
    assert not [call for call in cluster.calls if call[0] in {"delete", "apply"}]


def test_a_leftover_from_an_interrupted_run_is_deleted_first(tmp_path: Path) -> None:
    cluster = Cluster()
    cluster.namespace = {"metadata": {"name": smoke.NAMESPACE, "labels": {smoke.OWNER_LABEL: smoke.OWNER}}}
    smoke.open_scratch(_ctx(tmp_path, cluster))
    assert ["delete", "namespace", smoke.NAMESPACE, "--ignore-not-found", "--wait=false"] in cluster.calls
    assert cluster.namespace is not None


def test_the_certificate_probe_waits_for_ready_and_gives_up(tmp_path: Path) -> None:
    cluster = Cluster()
    cluster.ready_after = 3
    smoke.probe_certificate(_ctx(tmp_path, cluster))
    cert = next(body for body in cluster.applied if body["kind"] == "Certificate")
    assert cert["spec"]["issuerRef"] == {"name": "internal-ca", "kind": "ClusterIssuer"}
    assert cert["metadata"]["namespace"] == smoke.NAMESPACE
    cluster.reads, cluster.ready_after = 0, 10**6
    with pytest.raises(CheckError, match="was not Ready within"):
        smoke.probe_certificate(_ctx(tmp_path / "again", cluster))


def test_the_app_dns_probe_wants_traefiks_address_for_the_new_name(tmp_path: Path, monkeypatch) -> None:
    asked: list[tuple[str, str]] = []
    answers = [[], [TRAEFIK]]
    monkeypatch.setattr(smoke, "_resolve", lambda server, name: asked.append((server, name)) or answers.pop(0))
    cluster = Cluster()
    smoke.probe_app_dns(_ctx(tmp_path, cluster))
    ingress = next(body for body in cluster.applied if body["kind"] == "Ingress")
    host = ingress["spec"]["rules"][0]["host"]
    assert host.endswith(".test.example") and asked == [(APP_DNS, host)] * 2
    monkeypatch.setattr(smoke, "_resolve", lambda server, name: ["192.0.2.1"])
    with pytest.raises(CheckError, match="did not answer"):
        smoke.probe_app_dns(_ctx(tmp_path / "again", cluster))


def test_admission_admits_the_copy_and_refuses_the_risky_one(tmp_path: Path) -> None:
    """Rule: the copy keeps the pod's spec (not its node or owner), the risky copy differs by one
    ungranted item, and the refusal must be for the grant."""
    cluster = Cluster()
    smoke.probe_admission(_ctx(tmp_path, cluster, APPS))
    plain, bad = cluster.dry_runs
    assert "nodeName" not in plain["spec"] and "ownerReferences" not in plain["metadata"]
    assert plain["metadata"]["namespace"] == "app-hello-web" and "pod-template-hash" not in plain["metadata"]["labels"]
    assert bad["spec"]["containers"][0]["securityContext"] == {"privileged": True}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"bad_rc": 0}, "admitted a pod with privileged"),
        ({"bad_stderr": "denied: some other rule"}, "not for the grant"),
        ({"plain_rc": 1}, "copy of running pod"),
    ],
)
def test_admission_fails_when_the_policies_do_not_hold(tmp_path: Path, change: dict, message: str) -> None:
    cluster = Cluster()
    for key, value in change.items():
        setattr(cluster, key, value)
    with pytest.raises(CheckError, match=message):
        smoke.probe_admission(_ctx(tmp_path, cluster, APPS))


def test_admission_picks_a_risk_the_env_does_not_grant(tmp_path: Path) -> None:
    cluster = Cluster()
    smoke.probe_admission(_ctx(tmp_path, cluster, APPS + "exceptions:\n  - hello-web: privileged\n"))
    assert cluster.dry_runs[1]["spec"].get("hostNetwork") is True
    every = APPS + "exceptions:\n" + "".join(f"  - hello-web: {grant}\n" for grant, _ in smoke.RISKY)
    with pytest.raises(CheckWarning, match="every risky grant"):
        smoke.probe_admission(_ctx(tmp_path / "all", Cluster(), every))


def test_an_add_on_brings_its_own_probe(tmp_path: Path) -> None:
    ran: list[str] = []

    @register_smoke("fixture-addon")
    def _probes(ctx: Context):
        return [(50, "smoke: fixture", lambda: ran.append(ctx.env))]

    try:
        probes = collect(["fixture-addon", "backups"], _ctx(tmp_path, Cluster()), smoke=True)
        assert [label for label, _ in probes] == ["smoke: fixture"]
        probes[0][1]()
        assert ran == ["env-test"]
    finally:
        SMOKE.pop("fixture-addon")


def test_verify_smoke_removes_the_namespace_even_when_a_probe_fails(tmp_path: Path, capsys) -> None:
    from jarvis.verify import run

    cluster = Cluster()
    cluster.ready_after = 10**6
    assert run._run_smoke(_ctx(tmp_path, cluster), ["platform"]) == 1
    out = capsys.readouterr().out
    assert "FAIL smoke: Certificate" in out and "PASS smoke: namespace platform-smoke removed" in out
    assert cluster.namespace is None
