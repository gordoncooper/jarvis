"""jarvis verify --smoke: each probe makes its objects, checks them and removes them. No cluster."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis.verify import registry, run, smoke
from jarvis.verify.common import CheckError, CheckWarning, Context
from jarvis.verify.registry import collect, register_smoke
from support import completed

TRAEFIK = "10.43.0.10"
APP_DNS = "10.43.0.53"
TOKEN = "abc123"
IMAGE = "x@sha256:" + "a" * 64


class Cluster:
    """Just enough API server for the probes: kubectl argv in, CompletedProcess out. It records each call, and each
    change: the object applied, or a delete's argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.changes: list = []
        self.namespace: dict | None = None
        self.ready_after = 1
        self.reads = 0
        self.plain_rc, self.bad_rc = 0, 1
        self.bad_stderr = 'admission webhook denied: grant: anything beyond Pod Security restricted needs the env\'s "x: privileged"'
        self.dry_runs: list[dict] = []
        # Each dry run's refusal in turn, "" admitting; None alternates plain and risky as above.
        self.refusals: list[str] | None = None
        self.route_accepted = {"type": "Accepted", "status": "True", "reason": "Accepted"}
        # More fields of the running pod's container.
        self.container: dict = {}

    def __call__(self, args, input=None):  # noqa: A002 - kubectl's own keyword
        args = list(args)
        self.calls.append(args)
        if args[:3] == ["get", "namespace", smoke.NAMESPACE]:
            if self.namespace is None:
                return completed(args, returncode=1, stderr='Error from server (NotFound): namespaces "platform-smoke" not found')
            return completed(args, self.namespace)
        if args[:2] == ["delete", "namespace"]:
            self.changes.append(args)
            self.namespace = None
            return completed(args)
        if args[:2] == ["apply", "-f"]:
            body = json.loads(input)
            self.changes.append(body)
            if body["kind"] == "Namespace":
                self.namespace = body
            return completed(args)
        if args[:2] == ["get", "certificate"]:
            self.reads += 1
            status = "True" if self.reads >= self.ready_after else "False"
            return completed(args, {"status": {"conditions": [{"type": "Ready", "status": status}]}})
        if args[:2] == ["get", "httproute"]:
            parent = {"parentRef": {"name": "jarvis", "namespace": "platform-gateway", "sectionName": "core"}}
            return completed(args, {"status": {"parents": [{**parent, "conditions": [self.route_accepted]}]}})
        if args[:3] == ["get", "service", "-n"]:
            return completed(args, {"status": {"loadBalancer": {"ingress": [{"ip": TRAEFIK}]}}})
        if args[:2] == ["get", "services"]:
            service = {"spec": {"type": "LoadBalancer"}, "status": {"loadBalancer": {"ingress": [{"ip": APP_DNS}]}}}
            return completed(args, {"items": [service]})
        if args[:2] == ["get", "pods"]:
            pod = {
                "metadata": {
                    "name": "hello-web-abc",
                    "namespace": args[3],
                    "labels": {"app": "hello-web", "pod-template-hash": "abc"},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "hello-web-abc"}],
                },
                "spec": {"nodeName": "node1", "containers": [{"name": "web", "image": IMAGE, **self.container}]},
                "status": {"phase": "Running"},
            }
            return completed(args, {"items": [pod]})
        if args[:2] == ["create", "--dry-run=server"]:
            body = json.loads(input)
            self.dry_runs.append(body)
            if self.refusals is not None:
                err = self.refusals.pop(0) if self.refusals else ""
                return completed(args, "" if err else "pod/x", returncode=1 if err else 0, stderr=err)
            risky = len(self.dry_runs) % 2 == 0
            rc, err = (self.bad_rc, self.bad_stderr) if risky else (self.plain_rc, "" if not self.plain_rc else "denied: other")
            return completed(args, "pod/x" if not rc else "", returncode=rc, stderr=err if rc else "")
        raise AssertionError(f"unexpected kubectl {args}")


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polls without waiting, the clock moving one poll per sleep, and names each probe's objects with one token."""
    now = [0.0]
    monkeypatch.setattr(smoke, "_sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(smoke, "_clock", lambda: now[0])
    monkeypatch.setattr(smoke.secrets, "token_hex", lambda nbytes: TOKEN)


def _ctx(tmp_path: Path, cluster: Cluster, apps_text: str | None = None) -> Context:
    values = tmp_path / "envs" / "env-test" / "values"
    values.mkdir(parents=True)
    if apps_text is not None:
        (values / "apps.yaml").write_text(apps_text, encoding="utf-8")
        (values / "switches.yaml").write_text("features: [app-hosting]\n", encoding="utf-8")
    return Context(tmp_path, "env-test", cluster, {"dns_suffix": "test.example"}, datetime(2026, 11, 9, tzinfo=UTC), {})


def _copy(namespace: str, **container: object) -> dict:
    """The probes' copy of the fake's running pod: no node, owner or pod-template-hash; container's fields over its own."""
    metadata = {"name": f"smoke-{TOKEN}", "namespace": namespace, "labels": {"app": "hello-web"}, "annotations": {}}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata,
            "spec": {"containers": [{"name": "web", "image": IMAGE, **container}]}}


APPS = "hello-web:\n  version: v0.1.0\n"
_DELETE = ["delete", "namespace", smoke.NAMESPACE, "--ignore-not-found", "--wait=false"]
_SCRATCH = {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": smoke.NAMESPACE, "labels": {smoke.OWNER_LABEL: smoke.OWNER, "jarvis.io/layer": "platform"}}}


@pytest.mark.parametrize(
    ("existing", "changes"),
    [
        pytest.param(None, [_SCRATCH, _DELETE], id="none-there"),
        # Left by an interrupted run.
        pytest.param({"metadata": {"name": smoke.NAMESPACE, "labels": {smoke.OWNER_LABEL: smoke.OWNER}}},
                     [_DELETE, _SCRATCH, _DELETE], id="leftover"),
    ],
)
def test_the_scratch_namespace_is_made_and_removed(tmp_path: Path, existing: dict | None, changes: list) -> None:
    """Rule: platform-smoke is made labelled verify's and core's layer, and deleted at the end; a leftover of verify's
    is deleted first."""
    cluster = Cluster()
    cluster.namespace = existing
    ctx = _ctx(tmp_path, cluster)
    smoke.open_scratch(ctx)
    smoke.close_scratch(ctx)
    assert (cluster.changes, cluster.namespace) == (changes, None)


def test_a_namespace_that_is_not_verifys_is_left_alone(tmp_path: Path) -> None:
    """Rule: smoke deletes platform-smoke only when it carries verify's owner label."""
    cluster = Cluster()
    cluster.namespace = {"metadata": {"name": smoke.NAMESPACE, "labels": {}}}
    ctx = _ctx(tmp_path, cluster)
    with pytest.raises(CheckError) as raised:
        smoke.open_scratch(ctx)
    smoke.close_scratch(ctx)
    assert str(raised.value) == ("namespace platform-smoke exists and is not jarvis verify's (label jarvis.io/owner); "
                                 "nothing was changed")
    assert (cluster.changes, cluster.namespace) == ([], {"metadata": {"name": smoke.NAMESPACE, "labels": {}}})


def test_the_certificate_probe_waits_for_ready_and_gives_up(tmp_path: Path) -> None:
    """Rule: the probe's Certificate, from ClusterIssuer internal-ca, must become Ready within the wait."""
    cluster = Cluster()
    cluster.ready_after = 3
    smoke.probe_certificate(_ctx(tmp_path, cluster))
    certificate = {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                   "metadata": {"name": "smoke", "namespace": smoke.NAMESPACE},
                   "spec": {"secretName": "smoke-tls", "dnsNames": [f"smoke-{TOKEN}.test.example"],
                            "issuerRef": {"name": "internal-ca", "kind": "ClusterIssuer"}}}
    assert (cluster.changes, cluster.reads) == ([certificate], 3)
    cluster.reads, cluster.ready_after = 0, 10**6
    with pytest.raises(CheckError) as raised:
        smoke.probe_certificate(_ctx(tmp_path / "again", cluster))
    assert str(raised.value) == "Certificate platform-smoke/smoke from internal-ca was not Ready within 180 s"


HOST = f"smoke-{TOKEN}.test.example"


def test_the_app_dns_probe_wants_traefiks_address_for_the_new_name(tmp_path: Path, monkeypatch) -> None:
    """Rule: the new route's name on the core door must answer at App DNS with Traefik's address; it is asked again
    until it does."""
    asked: list[tuple[str, str]] = []
    answers = [[], [TRAEFIK]]
    monkeypatch.setattr(smoke, "_resolve", lambda server, name: asked.append((server, name)) or answers.pop(0))
    cluster = Cluster()
    smoke.probe_app_dns(_ctx(tmp_path, cluster))
    metadata = {"name": "smoke", "namespace": smoke.NAMESPACE}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": metadata, "spec": {"ports": [{"name": "http", "port": 80}]}}
    route = {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": metadata,
             "spec": {"parentRefs": [{"name": "jarvis", "namespace": "platform-gateway", "sectionName": "core"}],
                      "hostnames": [HOST], "rules": [{"backendRefs": [{"name": "smoke", "port": 80}]}]}}
    assert (cluster.changes, asked) == ([service, route], [(APP_DNS, HOST)] * 2)
    monkeypatch.setattr(smoke, "_resolve", lambda server, name: ["192.0.2.1"])
    with pytest.raises(CheckError) as raised:
        smoke.probe_app_dns(_ctx(tmp_path / "again", cluster))
    assert str(raised.value) == f"App DNS ({APP_DNS}) did not answer {HOST} with Traefik's address {TRAEFIK} within 180 s"


def test_the_app_dns_probe_refuses_a_route_the_core_door_does_not_accept(tmp_path: Path, monkeypatch) -> None:
    """Rule: App DNS answers a route's name whether or not Traefik serves it, so the name is not
    asked until the core door reads the route Accepted."""
    asked: list[str] = []
    monkeypatch.setattr(smoke, "_resolve", lambda server, name: asked.append(name) or [TRAEFIK])
    cluster = Cluster()
    cluster.route_accepted = {"type": "Accepted", "status": "False", "reason": "NotAllowedByListeners"}
    with pytest.raises(CheckError) as raised:
        smoke.probe_app_dns(_ctx(tmp_path, cluster))
    assert (str(raised.value), asked) == ("HTTPRoute platform-smoke/smoke was not Accepted on platform-gateway/jarvis "
                                          "section core (last: Accepted=False (NotAllowedByListeners)) within 180 s", [])


@pytest.mark.parametrize(
    ("container", "risky"),
    [
        pytest.param({}, {"privileged": True, "allowPrivilegeEscalation": True}, id="no-security-context"),
        # privileged beside allowPrivilegeEscalation false is an invalid pod, refused before any policy runs.
        pytest.param({"securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True}},
                     {"allowPrivilegeEscalation": True, "readOnlyRootFilesystem": True, "privileged": True},
                     id="restricted-pod"),
    ],
)
def test_admission_admits_the_copy_and_refuses_the_risky_one(tmp_path: Path, container: dict, risky: dict) -> None:
    """Rule: the copy keeps the pod's spec (not its node or owner), and the risky copy differs by one ungranted item,
    kept a valid pod beside the pod's own settings."""
    cluster = Cluster()
    cluster.container = container
    smoke.probe_admission(_ctx(tmp_path, cluster, APPS))
    assert cluster.dry_runs == [_copy("app-hello-web", **container), _copy("app-hello-web", securityContext=risky)]


@pytest.mark.parametrize(
    ("probe", "change", "message"),
    [
        pytest.param(smoke.probe_admission, {"bad_rc": 0},
                     "app-hello-web admitted a pod with privileged, which the env does not grant it", id="risky-admitted"),
        pytest.param(smoke.probe_admission, {"bad_stderr": "denied: some other rule"},
                     "app-hello-web refused the privileged pod, but not for the grant: denied: some other rule",
                     id="refused-for-another-rule"),
        pytest.param(smoke.probe_admission, {"plain_rc": 1},
                     "a copy of running pod app-hello-web/hello-web-abc was refused: denied: other", id="copy-refused"),
        pytest.param(smoke.probe_core_admission, {"refusals": [""]},
                     "platform-testapp still admitted the unpinned copy within 180 s", id="core-unpinned-admitted"),
        pytest.param(smoke.probe_core_admission, {"refusals": ["", "denied: other"]},
                     "platform-testapp refused the unpinned copy, but not for it: denied: other",
                     id="core-refused-for-another-rule"),
    ],
)
def test_admission_fails_when_the_policies_do_not_hold(tmp_path: Path, probe, change: dict, message: str) -> None:
    """Rule: an admission probe fails when a copy it wants refused is admitted, or refused for another rule, or when
    the plain copy is refused."""
    cluster = Cluster()
    vars(cluster).update(copy.deepcopy(change))
    with pytest.raises(CheckError) as raised:
        probe(_ctx(tmp_path, cluster, APPS))
    assert str(raised.value) == message


def test_admission_picks_a_risk_the_env_does_not_grant(tmp_path: Path) -> None:
    """Rule: the risky copy takes a risk the env does not grant the app; an app holding every grant warns."""
    cluster = Cluster()
    smoke.probe_admission(_ctx(tmp_path, cluster, APPS + "  grants:\n    - privileged\n"))
    plain, risky = cluster.dry_runs
    assert risky == {**plain, "spec": {**plain["spec"], "hostNetwork": True}}
    every = APPS + "  grants:\n" + "".join(f"    - {grant}\n" for grant, _ in smoke.RISKY)
    cluster = Cluster()
    with pytest.raises(CheckWarning) as raised:
        smoke.probe_admission(_ctx(tmp_path / "all", cluster, every))
    assert (str(raised.value), cluster.dry_runs) == ("app hello-web holds every risky grant; no refusal to prove", [])


PINS = "admission webhook denied the request: policy images-pinned failed: image not pinned by digest: web"
PSA = 'pods "x" is forbidden: violates PodSecurity "restricted:latest": privileged'


def test_core_admission_wants_each_copy_refused_for_its_own_reason(tmp_path: Path) -> None:
    """Rule: in platform-testapp the plain copy is admitted, the unpinned one refused by policy
    images-pinned and the privileged one by Pod Security; a refusal still loading is waited for."""
    cluster = Cluster()
    cluster.refusals = ["", "", PINS, PSA]
    smoke.probe_core_admission(_ctx(tmp_path, cluster))
    unpinned = _copy("platform-testapp", image="x")
    privileged = _copy("platform-testapp", securityContext={"privileged": True, "allowPrivilegeEscalation": True})
    assert cluster.dry_runs == [_copy("platform-testapp"), unpinned, unpinned, privileged]


def test_an_add_on_brings_its_own_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: an add-on's probes, registered with register_smoke, run under its name; a component with none adds
    nothing."""
    # A copy of the registry's probes, so this test's registration goes with it.
    monkeypatch.setattr(registry, "SMOKE", dict(registry.SMOKE))
    ran: list[str] = []

    @register_smoke("fixture-addon")
    def _probes(ctx: Context):
        return [(50, "smoke: fixture", lambda: ran.append(ctx.env))]

    probes = collect(["fixture-addon", "backups"], _ctx(tmp_path, Cluster()), smoke=True)
    assert [label for label, _ in probes] == ["smoke: fixture"]
    probes[0][1]()
    assert ran == ["env-test"]


def test_verify_smoke_removes_the_namespace_even_when_a_probe_fails(tmp_path: Path, capsys) -> None:
    """Rule: platform-smoke is deleted after the probes, whatever they found."""
    cluster = Cluster()
    cluster.ready_after, cluster.refusals = 10**6, ["", PINS, PSA]
    assert run._run_smoke(_ctx(tmp_path, cluster), ["platform"]) == 1
    assert capsys.readouterr().out == (
        "FAIL smoke: Certificate from internal-ca Ready: Certificate platform-smoke/smoke from internal-ca was not Ready "
        "within 180 s\nPASS smoke: core admission admits testapp's copy and refuses it unpinned or privileged\n"
        "PASS smoke: namespace platform-smoke removed\n"
    )
    assert cluster.namespace is None
