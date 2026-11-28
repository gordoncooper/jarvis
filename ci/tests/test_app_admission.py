"""Hosted-app admission, offline: every rule in core/feature/app-hosting/policies against seeded
good and bad objects, with the pinned kyverno CLI, and the app check (jarvis/apps/app_file.py),
which runs app-tenant and app-file the same way.

The namespaces are rendered from the real per-app wiring (core/feature/app-hosting/app) with the
env's values, so app-grants sees the annotations the cluster gets. The CLI cannot run two parts
as they are: it does not resolve the selector paramRef of the cluster-wide app-file binding, so
a binding naming each App stands in for it, and its namespaceObject holds only a name and
labels, so app-grants' grants variable reads the rendered annotations from a literal instead.
test_app_admission_apiserver.py proves both real ones.
"""

from __future__ import annotations

import copy
import datetime
import ipaddress
import re
import shutil
from pathlib import Path

import pytest
import yaml

from jarvis.apps.app_file import check_folder, stand_in_crd, userinfo
from jarvis.backups.restore import _binder_body
from jarvis.checks.rendered import policies
from support import (
    BOTH_FAIL,
    FILE_FAILS,
    GRANT_FAILS,
    PASS,
    POLICIES,
    REPO_ROOT,
    SUFFIX,
    app_deploy,
    app_doc,
    app_job,
    app_mutated,
    app_pod,
    app_route,
    app_wiring,
    kyverno_generated,
    run_admission,
    yaml_docs,
)

APPS = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "apps"
TENANT = "system:serviceaccount:flux-system:app-{app}"
VOLSYNC = "system:serviceaccount:volsync:volsync"
JOB_CONTROLLER = "system:serviceaccount:kube-system:job-controller"
_POD_SECURITY = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
_PRIVILEGED = {"containers": [{"name": "c", "image": "x", "securityContext": {"privileged": True, "runAsUser": 0}}],
               "securityContext": {}}
_SKIP = {"app-file": "skip", "app-grants": "skip"}


def _object(kind: str, name: str, app: str, api: str = "v1", **spec: object) -> dict:
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": f"app-{app}"}, "spec": spec}


def _annotated(doc: dict, key: str, value: object) -> dict:
    doc["metadata"]["annotations"] = {key: value}
    return doc


def _pvc(name: str, app: str, *, unbacked: bool = False) -> dict:
    doc = _object("PersistentVolumeClaim", name, app, accessModes=["ReadWriteOnce"], resources={"requests": {"storage": "1Gi"}})
    if unbacked:
        doc["metadata"]["annotations"] = {"backup": "none"}
    return doc


def _container(**security: object) -> dict:
    """A Pod Security restricted container, security's keys set in its securityContext."""
    return {"name": "c", "image": "x", "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, **security}}


def _workload(kind: str, name: str, app: str, **pod: object) -> dict:
    """A Deployment, StatefulSet, Job or CronJob in app's namespace whose pod is app_pod's, pod's keys set."""
    template = {"spec": app_pod("t", app, **pod)["spec"]}
    spec = {"schedule": "0 0 * * *", "jobTemplate": {"spec": {"template": template}}} if kind == "CronJob" else {"template": template}
    api = "batch/v1" if kind in ("Job", "CronJob") else "apps/v1"
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": f"app-{app}"}, "spec": spec}


# The env: the sample apps with env-00's exception line, two apps asking for privileged mode (one granted), a LAN app.
ENV = {
    "web-demo": (app_doc(APPS, "web-demo"), []),
    "notes": (app_doc(APPS, "notes"), []),
    "photos": (app_doc(APPS, "photos"), ["host-path"]),
    "granted": (app_doc(APPS, "granted", risky={"privileged": True}), ["privileged"]),
    "ungranted": (app_doc(APPS, "ungranted", risky={"privileged": True}), []),
    "lan": (app_doc(APPS, "lan", network={"ports": [{"listen": 8443, "target": 8443, "protocol": "TCP"},
                                                   {"listen": 443, "target": 8080, "protocol": "TCP"}]}),
            ["lan-port 8443/TCP", "lan-port 443/TCP"]),
}


def test_sample_apps_pass_admission(tmp_path: Path) -> None:
    """Rule: the sample apps' objects pass the baseline (photos' hostPath with its env line) once app-priority sets
    their class; without it (Kyverno down) their workloads are refused. Routes: test_app_admission_apiserver.py."""
    docs = [doc for app in ("web-demo", "notes", "photos") for doc in app_deploy(APPS, app) if doc["kind"] != "HTTPRoute"]
    run_admission(tmp_path, ENV, "samples", None, [(doc, PASS) for doc in app_mutated(ENV, docs)])
    run_admission(tmp_path, ENV, "unmutated", None, [(doc, FILE_FAILS) for doc in docs if doc["kind"] == "Deployment"])


def test_pod_baseline_refuses_risky_items_without_a_grant(tmp_path: Path) -> None:
    """Rule: privileged, root, hostPath, host network, host ports, host PID and IPC need the file
    and the env; a restricted-style pod passes; an asked item passes only with the env's line."""
    privileged = [{"name": "c", "image": "x", "securityContext": {"privileged": True}}]
    cases = [
        (app_pod("restricted", "web-demo"), PASS),
        # jarvis restore's binder pod, at the class of the app workload that mounts its claim.
        (_binder_body("app-web-demo", "data", "registry.example/x@sha256:" + "0" * 64, {"priorityClassName": "hosted-app"}), PASS),
        (app_pod("privileged", "web-demo", containers=[_container(privileged=True)]), BOTH_FAIL),
        (app_pod("root", "web-demo", securityContext={"seccompProfile": {"type": "RuntimeDefault"}}), BOTH_FAIL),
        (app_pod("uid-zero", "web-demo", securityContext={**_POD_SECURITY, "runAsUser": 0}), BOTH_FAIL),
        (app_pod("container-uid-zero", "web-demo", containers=[_container(runAsUser=0)]), BOTH_FAIL),
        (app_pod("container-root", "web-demo", containers=[_container(runAsNonRoot=False)]), BOTH_FAIL),
        (app_pod("host-path", "web-demo", volumes=[{"name": "h", "hostPath": {"path": "/srv"}}]), BOTH_FAIL),
        (app_pod("host-network", "web-demo", hostNetwork=True), BOTH_FAIL),
        (app_pod("host-pid", "web-demo", hostPID=True), BOTH_FAIL),
        (app_pod("host-ipc", "web-demo", hostIPC=True), BOTH_FAIL),
        (app_pod("host-port", "web-demo", containers=[{**_container(), "ports": [{"containerPort": 80, "hostPort": 80}]}]), BOTH_FAIL),
        (app_pod("capability", "web-demo", containers=[_container(capabilities={"drop": ["ALL"], "add": ["SYS_ADMIN"]})]), BOTH_FAIL),
        (app_pod("no-security-context", "web-demo", containers=[{"name": "c", "image": "x"}]), BOTH_FAIL),
        (app_pod("escalation-unset", "web-demo", containers=[{"name": "c", "image": "x", "securityContext": {"capabilities": {"drop": ["ALL"]}}}]), BOTH_FAIL),
        (app_pod("escalation-allowed", "web-demo", containers=[_container(allowPrivilegeEscalation=True)]), BOTH_FAIL),
        (app_pod("unconfined", "web-demo", securityContext={**_POD_SECURITY, "seccompProfile": {"type": "Unconfined"}},
                 containers=[_container(seccompProfile={"type": "RuntimeDefault"})]), BOTH_FAIL),
        (app_pod("sysctls", "web-demo", securityContext={**_POD_SECURITY, "sysctls": [{"name": "net.core.somaxconn", "value": "1024"}]}), BOTH_FAIL),
        (app_pod("nfs", "web-demo", volumes=[{"name": "n", "nfs": {"server": "nfs.example", "path": "/"}}]), FILE_FAILS),
        (app_pod("volumes-ok", "web-demo", volumes=[{"name": "e", "emptyDir": {}}, {"name": "c", "configMap": {"name": "x"}}]), PASS),
        # Asked for and granted; granted but also root, which neither asks for; asked for and not
        # granted, by a pod and by each workload; granted for another item only.
        (app_pod("granted-privileged", "granted", containers=privileged), PASS),
        (app_pod("granted-privileged-as-root", "granted", containers=_PRIVILEGED["containers"]), BOTH_FAIL),
        (app_pod("ungranted-privileged", "ungranted", containers=privileged), GRANT_FAILS),
        *((_workload(kind, f"ungranted-{kind.lower()}", "ungranted", containers=privileged), GRANT_FAILS)
          for kind in ("Deployment", "StatefulSet", "Job", "CronJob")),
        (app_pod("photos-privileged", "photos", containers=privileged), BOTH_FAIL),
        (app_job("tenant-job", "web-demo", containers=[_container(privileged=True)]), BOTH_FAIL),
    ]
    run_admission(tmp_path, ENV, "baseline", None, cases)


def test_pod_security_restricted_beyond_the_baseline_needs_privileged(tmp_path: Path) -> None:
    """Rule: Pod Security restricted (drop ALL, add only NET_BIND_SERVICE, RuntimeDefault or Localhost seccomp,
    no unmasked /proc, unconfined AppArmor or SELinux beyond a type) holds unless both privileged lines allow more."""
    cases = [
        (app_pod("bind-service", "web-demo", containers=[_container(capabilities={"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]})]), PASS),
        (app_pod("container-seccomp", "web-demo", securityContext={"runAsNonRoot": True},
                 containers=[_container(seccompProfile={"type": "Localhost", "localhostProfile": "p.json"})]), PASS),
        (app_pod("pod-seccomp-localhost", "web-demo", securityContext={"runAsNonRoot": True, "seccompProfile": {"type": "Localhost", "localhostProfile": "p.json"}}), PASS),
        (app_pod("selinux-container-type", "web-demo", containers=[_container(seLinuxOptions={"type": "container_init_t"})]), PASS),
        (app_pod("no-drop-all", "web-demo", containers=[_container(capabilities={"drop": ["NET_RAW"]})]), BOTH_FAIL),
        (app_pod("no-drop", "web-demo", containers=[_container(capabilities={})]), BOTH_FAIL),
        (app_pod("no-capabilities", "web-demo", containers=[{"name": "c", "image": "x", "securityContext": {"allowPrivilegeEscalation": False}}]), BOTH_FAIL),
        (app_pod("no-seccomp", "web-demo", securityContext={"runAsNonRoot": True}), BOTH_FAIL),
        (app_pod("one-container-unconfined", "web-demo", containers=[_container(), _container(seccompProfile={"type": "Unconfined"})]), BOTH_FAIL),
        (app_pod("init-container-escalation", "web-demo", initContainers=[_container(allowPrivilegeEscalation=True)]), BOTH_FAIL),
        (app_pod("proc-unmasked", "web-demo", containers=[_container(procMount="Unmasked")]), BOTH_FAIL),
        (app_pod("apparmor-unconfined", "web-demo", containers=[_container(appArmorProfile={"type": "Unconfined"})]), BOTH_FAIL),
        (app_pod("pod-apparmor-unconfined", "web-demo", securityContext={**_POD_SECURITY, "appArmorProfile": {"type": "Unconfined"}}), BOTH_FAIL),
        (app_pod("selinux-user", "web-demo", securityContext={**_POD_SECURITY, "seLinuxOptions": {"user": "system_u"}}), BOTH_FAIL),
        (app_pod("selinux-role", "web-demo", containers=[_container(seLinuxOptions={"role": "system_r"})]), BOTH_FAIL),
        (app_pod("selinux-type", "web-demo", containers=[_container(seLinuxOptions={"type": "spc_t"})]), BOTH_FAIL),
        (app_pod("no-drop-granted", "granted", containers=[_container(capabilities={})]), PASS),
    ]
    for name, profile, expected in (("apparmor-unconfined-annotation", "unconfined", BOTH_FAIL),
                                    ("apparmor-other-annotation", "other", BOTH_FAIL),
                                    ("apparmor-runtime-default-annotation", "runtime/default", PASS),
                                    ("apparmor-localhost-annotation", "localhost/p", PASS)):
        cases.append((_annotated(app_pod(name, "web-demo"), "container.apparmor.security.beta.kubernetes.io/c", profile), expected))
    run_admission(tmp_path, ENV, "restricted", None, cases)


def test_app_containers_reserve_their_memory_limit(tmp_path: Path) -> None:
    """Rule: a hosted app's container memory request equals its limit (the same quantity in any
    unit, or a limit alone), so it never uses more of a node than the scheduler set aside."""
    def sized(name: str, resources: dict) -> dict:
        return app_pod(name, "web-demo", containers=[{**_container(), "resources": resources}])

    run_admission(tmp_path, ENV, "memory", None, [
        (sized("equal", {"requests": {"memory": "128Mi"}, "limits": {"memory": "0.125Gi"}}), PASS),
        (sized("limit-only", {"limits": {"memory": "128Mi"}}), PASS),
        (sized("under-limit", {"requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}}), FILE_FAILS),
    ])


def _classed(doc: dict) -> dict:
    """doc with its pod at class hosted-app, as app-priority leaves it."""
    doc = copy.deepcopy(doc)
    template = doc["spec"]["jobTemplate"]["spec"]["template"] if doc["kind"] == "CronJob" else doc["spec"]["template"]
    template["spec"]["priorityClassName"] = "hosted-app"
    return doc


def test_app_pods_run_at_the_hosted_app_class_only(tmp_path: Path) -> None:
    """Rule: app-priority sets hosted-app on every workload the tenant ships, over any class it picks, and leaves
    VolSync's movers as they are; admission refuses a pod or workload at another class or none."""
    deployment = next(doc for doc in app_deploy(APPS, "web-demo") if doc["kind"] == "Deployment")
    deployment["spec"]["template"]["spec"]["priorityClassName"] = "system-cluster-critical"
    workloads = [deployment, *(_workload(kind, name, "web-demo", priorityClassName=None)
                               for kind, name in (("StatefulSet", "db"), ("Job", "migrate"), ("CronJob", "nightly")))]
    classed = app_mutated(ENV, workloads, user=TENANT.format(app="web-demo"))
    assert classed == [_classed(doc) for doc in workloads]
    mover = app_job("volsync-src-data", "notes", priorityClassName=None)
    assert app_mutated(ENV, [mover], user=VOLSYNC) == [mover]
    for doc in workloads:
        doc["metadata"]["name"] += "-unmutated"
    run_admission(tmp_path, ENV, "priority", None, [
        *((doc, PASS) for doc in classed), *((doc, FILE_FAILS) for doc in workloads),
        (app_pod("other-class", "web-demo", priorityClassName="high"), FILE_FAILS),
        (app_pod("no-class", "web-demo", priorityClassName=None), FILE_FAILS),
    ])


def test_claims_bind_only_to_what_the_cluster_provisions(tmp_path: Path) -> None:
    """Rule: the tenant sets no volumeName, dataSource or dataSourceRef on a PVC or claim template; the volume
    binder's volumeName update passes, and the VolSync controller's own claims are carved out."""
    def claim(name: str, **spec: object) -> dict:
        doc = _pvc(name, "notes")
        doc["spec"].update(spec)
        return doc

    def statefulset(name: str, spec: dict) -> dict:
        doc = _workload("StatefulSet", name, "notes")
        doc["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "data"}, "spec": spec}]
        return doc

    # kyverno test reads each case as a CREATE; claim names stand in for the declared volume.
    notes = app_doc(APPS, "notes")
    notes["spec"]["volumes"] += [{"name": case, "backup": True} for case in ("volume-name", "data-source", "data-source-ref")]
    env = {**ENV, "notes": (notes, [])}
    run_admission(tmp_path, env, "claims", TENANT.format(app="notes"), [
        (claim("data"), PASS),
        (statefulset("sts-clean", {}), PASS),
        (claim("volume-name", volumeName="pv-other"), FILE_FAILS),
        (claim("data-source", dataSource={"kind": "PersistentVolumeClaim", "name": "data"}), FILE_FAILS),
        (claim("data-source-ref", dataSourceRef={"kind": "PersistentVolumeClaim", "name": "data"}), FILE_FAILS),
        (statefulset("sts-volume-name", {"volumeName": "pv-other"}), FILE_FAILS),
        (statefulset("sts-data-source", {"dataSource": {"kind": "PersistentVolumeClaim", "name": "data"}}), FILE_FAILS),
    ])
    # The binder's write. kyverno's CLI sends no oldObject, so a tenant's later apply of the
    # bound claim (volumeName unchanged) is proven only live.
    run_admission(tmp_path, env, "binder", "system:serviceaccount:kube-system:persistent-volume-binder",
                  [(claim("volume-name", volumeName="pv-1"), PASS)])
    restored = claim("volsync-data-dst", dataSourceRef={"kind": "ReplicationDestination", "apiGroup": "volsync.backube", "name": "d"})
    run_admission(tmp_path, ENV, "volsync-claim", VOLSYNC, [(restored, _SKIP)])


def _forged(name: str, **metadata: object) -> dict:
    """A claim in app-notes naming a VolSync owner, as a tenant can forge one."""
    doc = _pvc(name, "notes")
    doc["metadata"].update(ownerReferences=[{"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource", "name": "fake", "uid": "3"}], **metadata)
    return doc


_MOVER_JOB = app_job("volsync-src-data", "notes", **_PRIVILEGED)
_MOVER_POD = app_pod("volsync-src-data-x", "notes", owner_job=True, **_PRIVILEGED)
_CACHE = _pvc("volsync-data-cache", "notes")


@pytest.mark.parametrize(
    ("user", "cases"),
    [
        pytest.param(VOLSYNC, [(_MOVER_JOB, _SKIP), (_CACHE, _SKIP)], id="volsync"),
        pytest.param(JOB_CONTROLLER, [(_MOVER_POD, _SKIP)], id="job-controller"),
        pytest.param("system:serviceaccount:kube-system:statefulset-controller",
                     [(_pvc("data-db-0", "notes"), PASS), (_pvc("pgdata-db-0", "notes"), FILE_FAILS)], id="statefulset-controller"),
        pytest.param(TENANT.format(app="notes"), [
            (_MOVER_JOB, BOTH_FAIL), (_MOVER_POD, BOTH_FAIL), (_CACHE, FILE_FAILS), (_pvc("data-db-0", "notes"), FILE_FAILS),
            (_forged("forged-volsync"), FILE_FAILS), (_forged("forged-terminating", deletionTimestamp="2026-01-01T00:00:00Z"), FILE_FAILS),
            (_pvc("undeclared", "notes"), FILE_FAILS),
        ], id="tenant"),
    ],
)
def test_each_controller_carve_out_admits_its_controller_only(tmp_path: Path, user: str, cases: list) -> None:
    """Rule: the carve-outs (VolSync's mover Job and claims, the job controller's pod for a Job, the StatefulSet
    controller's claim <volume>-<set>-<n> of a declared volume) are those identities' only, whatever is forged."""
    run_admission(tmp_path, ENV, "carve-outs", user, cases)


def test_volumes_hosts_and_services_match_the_file_and_the_env(tmp_path: Path) -> None:
    """Rule: a PVC is a declared volume with its backup; a Service is ClusterIP with no Traefik annotation, or a granted
    LoadBalancer on network.ports (listen, protocol, numeric target); none names a host but <app>.apps.<dns suffix>."""
    lb = {"type": "LoadBalancer", "allocateLoadBalancerNodePorts": False,
          "ports": [{"port": 8443, "protocol": "TCP"}], "selector": {"app": "x"}}
    with_node_ports = {**lb, "allocateLoadBalancerNodePorts": True}
    mapped = {**lb, "ports": [{"port": 443, "protocol": "TCP", "targetPort": 8080}]}
    wrong_target = {**lb, "ports": [{"port": 443, "protocol": "TCP", "targetPort": 9090}]}
    unset_target = {**lb, "ports": [{"port": 443, "protocol": "TCP"}]}
    named_target = {**lb, "ports": [{"port": 443, "protocol": "TCP", "targetPort": "http"}]}
    other_protocol = {**lb, "ports": [{"port": 8443, "protocol": "UDP"}]}
    other_port = {**lb, "ports": [{"port": 9443, "protocol": "TCP", "targetPort": 8443}]}
    traefik_service = _annotated(_object("Service", "traefik-annotated", "web-demo", ports=[{"port": 80}]),
                                 "traefik.ingress.kubernetes.io/service.serverstransport", "x")
    claims = _workload("StatefulSet", "db", "notes")
    claims["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "pgdata"}, "spec": {}}]
    # Named as app-lan-name names them, so app-grants refuses only a port it does not grant.
    (unlisted,) = app_mutated(ENV, [_object("Service", "lb", "web-demo", **lb)])
    granted, granted_mapped, wrong_protocol, wrong_listen = app_mutated(ENV, [
        _object("Service", "lan", "lan", **lb), _object("Service", "lan-mapped", "lan", **mapped),
        _object("Service", "lan-other-protocol", "lan", **other_protocol), _object("Service", "lan-other-port", "lan", **other_port)])
    cases = [
        (_pvc("undeclared", "notes"), FILE_FAILS),
        (_pvc("cache", "notes"), FILE_FAILS),
        (_pvc("data", "notes", unbacked=True), FILE_FAILS),
        (claims, FILE_FAILS),
        (traefik_service, FILE_FAILS),
        (unlisted, BOTH_FAIL), (granted, PASS), (granted_mapped, PASS), (wrong_protocol, BOTH_FAIL), (wrong_listen, BOTH_FAIL),
        # Unnamed (Kyverno down), or named another app's or core's name: App DNS would answer it.
        (_object("Service", "lan-unnamed", "lan", **lb), GRANT_FAILS),
        (_annotated(_object("Service", "lan-other-name", "lan", **lb), "coredns.io/hostname", f"testapp.{SUFFIX}"), GRANT_FAILS),
        (_annotated(_object("Service", "web-own-name", "web-demo", ports=[{"port": 80}]), "coredns.io/hostname", f"notes.apps.{SUFFIX}"), GRANT_FAILS),
        (_annotated(_object("Service", "web-external-dns", "web-demo", ports=[{"port": 80}]),
                "external-dns.alpha.kubernetes.io/hostname", f"testapp.{SUFFIX}"), GRANT_FAILS),
        (_annotated(_object("Service", "web-external-dns-stable", "web-demo", ports=[{"port": 80}]),
                "external-dns.kubernetes.io/hostname", f"testapp.{SUFFIX}"), GRANT_FAILS),
        (_object("Service", "lan-wrong-target", "lan", **wrong_target), FILE_FAILS),
        (_object("Service", "lan-unset-target", "lan", **unset_target), FILE_FAILS),
        (_object("Service", "lan-named-target", "lan", **named_target), FILE_FAILS),
        (_object("Service", "lan-node-ports", "lan", **with_node_ports), FILE_FAILS),
        (_object("Service", "nodeport", "web-demo", type="NodePort", ports=[{"port": 80}]), {**FILE_FAILS, "app-grants": "pass"}),
        (_object("Service", "external-ips", "web-demo", ports=[{"port": 80}], externalIPs=["192.0.2.1"]), {**FILE_FAILS, "app-grants": "pass"}),
    ]
    run_admission(tmp_path, ENV, "objects", None, cases)


_SHIPS_ONLY = ("an app ships only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, HTTPRoutes,"
               " PodDisruptionBudgets, HorizontalPodAutoscalers and its App")


def _apply(resource: dict, policy: str, inputs: dict[str, list[dict]]) -> list[str]:
    """kyverno apply of policy and its binding to resource, with inputs (by flag): its JSON report's refusals."""
    return policies([resource], [doc for doc in yaml_docs(POLICIES / "admission.yaml") if doc["metadata"]["name"] == policy], inputs)


def test_an_app_cannot_write_its_own_grants_or_leave_its_namespace(tmp_path: Path) -> None:
    """Rule: the app's identity writes only its shipped kinds, a Namespace not among them, in app-<name>; a grants
    ConfigMap it ships is refused in flux-system and read by nothing in its own namespace, so it grants nothing."""
    tenant = TENANT.format(app="web-demo")
    local = {"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": "app-web-demo-grants", "namespace": "app-web-demo"}, "data": {"privileged": "true"}}
    forged = {**local, "metadata": {"name": "forged-grants", "namespace": "flux-system"}}
    network = _object("NetworkPolicy", "allow-all", "web-demo", "networking.k8s.io/v1", podSelector={})
    other = _object("ConfigMap", "elsewhere", "notes")
    own_namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "app-web-demo", "namespace": "app-web-demo"}}
    tenant_ok, tenant_fails = {"app-tenant": "pass"}, {"app-tenant": "fail"}
    run_admission(tmp_path, ENV, "tenant", tenant, [
        (local, tenant_ok), *((doc, tenant_fails) for doc in (forged, network, other, own_namespace)),
        (app_pod("privileged-after-forging", "web-demo", **_PRIVILEGED), BOTH_FAIL),
    ])
    # A Traefik route would claim any host.
    route = _object("IngressRoute", "steal", "web-demo", "traefik.io/v1alpha1", routes=[])
    inputs = {"--userinfo": [userinfo(tenant)], "--crd-paths": [stand_in_crd("traefik.io", "v1alpha1", "IngressRoute")]}
    assert _apply(route, "app-tenant", inputs) == [f"app-tenant: app-web-demo/IngressRoute/steal: {_SHIPS_ONLY}"]


def test_app_namespaces_and_apps_keep_their_names(tmp_path: Path) -> None:
    """Rule: app-<name> is labelled hosting.jarvis.io/app: <name> and no other namespace is; an
    App lives in its own namespace."""
    def namespace(name: str, label: str | None) -> dict:
        labels = {"hosting.jarvis.io/app": label} if label else {}
        return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}}

    ok, fails = {"app-identity": "pass"}, {"app-identity": "fail"}
    run_admission(tmp_path, ENV, "identity", None, [
        (namespace("app-x", "x"), ok), (namespace("app-y", None), fails), (namespace("app-z", "other"), fails),
        (namespace("monitoring", "monitoring"), fails), (namespace("monitoring-2", None), ok),
    ])
    # kyverno test cannot map a custom kind; kyverno apply reads the CRD.
    wrong = app_doc(APPS, "notes")
    wrong["metadata"]["name"] = "photos"
    crd = {"--crd-paths": yaml_docs(REPO_ROOT / "core" / "feature" / "app-hosting" / "crd" / "app-crd.yaml")}
    assert _apply(app_doc(APPS, "idcheck"), "app-identity", crd) == []
    assert _apply(wrong, "app-identity", crd) == [
        "app-identity: app-notes/App/photos: an App lives in its own namespace app-<name> and is named <name>"]


@pytest.mark.parametrize("app", ["web-demo", "notes", "photos"])
def test_app_check_passes_the_sample_apps(app: str) -> None:
    """Rule: jarvis/apps/check.py passes each sample app folder."""
    assert check_folder(APPS / app) == []


def _folder(tmp_path: Path, app: str, doc: dict) -> Path:
    """app's file and only doc in deploy/, as its author writes it: no namespace, its own route at ${app_host}."""
    folder = tmp_path / app
    (folder / "deploy").mkdir(parents=True)
    shutil.copy(APPS / app / "app.yaml", folder)
    doc = copy.deepcopy(doc)
    del doc["metadata"]["namespace"]
    if doc["kind"] == "HTTPRoute":
        doc["spec"]["hostnames"] = ["${app_host}" if host == f"{app}.apps.{SUFFIX}" else host for host in doc["spec"]["hostnames"]]
    (folder / "deploy" / "objects.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return folder


_ASKS_PRIVILEGED = ("grant: anything beyond Pod Security restricted (privileged mode, host PID or IPC, sysctls, allowPrivilegeEscalation"
                    " not false, capabilities not dropping ALL or adding more than NET_BIND_SERVICE, a seccomp profile not RuntimeDefault"
                    " or Localhost, procMount Unmasked, AppArmor unconfined, SELinux options beyond a container type) needs the app file's"
                    " risky.privileged")
_CLUSTER_IP = "an app's Services are ClusterIP, or LoadBalancer for its network.ports; no NodePort or ExternalName"


@pytest.mark.parametrize(
    ("app", "doc", "problem"),
    [
        pytest.param("web-demo", app_job("j", "web-demo", **_PRIVILEGED), f"app-file: app-web-demo/Job/j: {_ASKS_PRIVILEGED}", id="privileged"),
        pytest.param("web-demo", _object("NetworkPolicy", "n", "web-demo", "networking.k8s.io/v1"),
                     f"app-tenant: app-web-demo/NetworkPolicy: {_SHIPS_ONLY}", id="networkpolicy"),
        pytest.param("web-demo", _object("Service", "s", "web-demo", type="NodePort", ports=[{"port": 80}]),
                     f"app-file: app-web-demo/Service/s: {_CLUSTER_IP}", id="nodeport"),
        pytest.param("web-demo", _annotated(_object("Service", "s", "web-demo", type="NodePort", ports=[{"port": 80}]), "since", datetime.date(2026, 1, 1)),
                     f"app-file: app-web-demo/Service/s: {_CLUSTER_IP}", id="dated-value"),
        pytest.param("notes", app_route("r", "notes", f"notes.apps.{SUFFIX}"),
                     "app-file: app-notes/HTTPRoute/r: an app ships an HTTPRoute only for a web entry marked ownRoute; core makes every"
                     " other entry's route", id="stray-route"),
        pytest.param("web-demo", _object("IngressRoute", "r", "web-demo", "traefik.io/v1alpha1"),
                     "Error: failed to apply policies on resource r (failed to map gvk to gvr traefik.io/v1alpha1, Kind=IngressRoute:"
                     ' no matches for kind "IngressRoute" in version "traefik.io/v1alpha1")', id="ingressroute"),
    ],
)
def test_app_check_refuses_what_admission_refuses(tmp_path: Path, app: str, doc: dict, problem: str) -> None:
    """Rule: the app check holds the folder's objects to app-tenant and app-file as its tenant applies them (its route
    at ${app_host}, a YAML date as its string), and a kind the CLI cannot map stops it."""
    assert check_folder(_folder(tmp_path, app, doc)) == [f"{app}/deploy: {problem}"]


def _edit_app(folder: Path, edit) -> None:
    app = yaml.safe_load((folder / "app.yaml").read_text(encoding="utf-8"))
    edit(app)
    (folder / "app.yaml").write_text(yaml.safe_dump(app), encoding="utf-8")


def _rename(folder: Path, name: str) -> None:
    """The app renamed name, its file's name and its folder's alike."""
    _edit_app(folder, lambda app: app["metadata"].update(name=name))
    folder.rename(folder.parent / name)


# kubeconform names the App CRD's schema by its temporary copy, left out here.
_SCHEMA = "web-demo/app.yaml: App web-demo: problem validating schema. Check JSON formatting: jsonschema validation failed with 'hosting.jarvis.io/app_v1alpha1.json#' - "


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param(lambda folder: (folder / "app.yaml").unlink(), "web-demo: missing app.yaml; nothing of an app deploys without its app file", id="missing"),
        pytest.param(lambda folder: (folder / "app.yaml").write_text("kind: App\n", encoding="utf-8"),
                     "web-demo/app.yaml: apiVersion and kind are hosting.jarvis.io/v1alpha1 App", id="invalid"),
        pytest.param(lambda folder: folder.rename(folder.parent / "renamed"),
                     "renamed/app.yaml: metadata.name is 'web-demo'; an app's name is its folder name, renamed", id="misnamed"),
        pytest.param(lambda folder: _rename(folder, "a" * 60),
                     f"{'a' * 60}: an app's folder name is its name, a DNS label of at most 59 characters (app-<name> is its namespace)", id="name-too-long"),
        pytest.param(lambda folder: _edit_app(folder, lambda app: app["spec"].update(colour="blue")),
                     _SCHEMA + "at '/spec': additional properties 'colour' not allowed", id="unknown-key"),
        pytest.param(lambda folder: _edit_app(folder, lambda app: app["spec"].pop("volumes")),
                     _SCHEMA + "at '/spec': missing property 'volumes'", id="required-field"),
        pytest.param(lambda folder: _edit_app(folder, lambda app: app["spec"]["network"].update(web=[{"service": "web", "port": "eighty"}])),
                     _SCHEMA + "at '/spec/network/web/0/port': got string, want integer", id="bad-web-port"),
        pytest.param(lambda folder: _edit_app(folder, lambda app: app["spec"]["network"].update(web=[{"name": "a" * 60, "service": "web", "port": 80}])),
                     f"web-demo/app.yaml: spec.network.web: entry '{'a' * 60}' makes host label web-demo-{'a' * 60}, longer than 63", id="web-name-too-long"),
        pytest.param(lambda folder: _edit_app(folder, lambda app: app["metadata"].update(namespace="kube-system")),
                     "web-demo/app.yaml: metadata holds the name only; core sets the namespace", id="namespace-set"),
    ],
)
def test_app_check_refuses_a_missing_invalid_or_misnamed_file(tmp_path: Path, change, problem: str) -> None:
    """Rule: nothing of an app deploys without a valid app file named as its folder, a DNS label of at most 59
    characters; kubeconform holds it to the App CRD (its CEL rule: test_app_admission_apiserver.py)."""
    shutil.copytree(APPS / "web-demo", tmp_path / "web-demo")
    change(tmp_path / "web-demo")
    (folder,) = tmp_path.iterdir()
    assert [re.sub(r"file://\S*?/jarvis-schemas-\w+/", "", found) for found in check_folder(folder)] == [problem]


def _network_policy(app: str, name: str, spec: dict) -> dict:
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": f"app-{app}"}, "spec": {"podSelector": {}, **spec}}


def _namespaces(apps: dict[str, list[str]]) -> list[dict]:
    """What app-derive reads: each App's namespace, rendered with the env's lines for it, and platform-backups."""
    return [*(next(doc for doc in app_wiring(app, grants) if doc["kind"] == "Namespace") for app, grants in apps.items()),
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "platform-backups"}}]


_DERIVED = {"quota": "app", "web": "app-web", "internet": "app-internet", "lan-all": "app-lan-all", "lan": "app-lan",
            "apps": "app-apps", "callers": "app-callers", "ports": "app-ports", "backups": "app-backups",
            "route": "app-web", "route-admin": "app-web-admin", "hostnames": "app-hostnames"}


def _derive_policies() -> list[dict]:
    """app-derive, and app-derive-backups from app-hosting's backups."""
    backups = [doc for doc in yaml_docs(POLICIES.parent / "backups" / "policies.yaml") if doc["metadata"]["name"] == "app-derive-backups"]
    return [*yaml_docs(POLICIES / "derive.yaml"), *backups]


def _derive_test(tmp_path: Path, triggers: list[dict], apps: dict[str, list[str]],
                 results: list[tuple[str, tuple[dict, dict | None]]]) -> None:
    """What the app-derive policies make for the triggers: results are (object, (trigger, the
    object made, or None for none)), the object named as _DERIVED names it."""
    made = {(doc["metadata"]["namespace"], doc["kind"], doc["metadata"]["name"]): doc
            for doc in kyverno_generated(tmp_path / "derive", _derive_policies(), triggers, _namespaces(apps))}
    for item, (trigger, expected) in results:
        name = trigger["metadata"]["name"]
        namespace = name.removeprefix("backups-restic-") if trigger["kind"] == "Secret" else trigger["metadata"]["namespace"]
        kind = "ResourceQuota" if item == "quota" else "HTTPRoute" if item.startswith(("route", "hostnames")) else "NetworkPolicy"
        found = made.get((namespace, kind, _DERIVED[item]))
        if expected is None:
            assert found is None, (item, name, found)
        else:
            assert found == expected, (item, name, found)


def _quota(app: str, cpu: str, memory: str, lbs: str = "0") -> dict:
    return {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "app", "namespace": f"app-{app}"},
            "spec": {"hard": {"limits.cpu": cpu, "limits.memory": memory, "requests.cpu": cpu, "requests.memory": memory,
                              "services.loadbalancers": lbs, "services.nodeports": "0"}}}


def _derived_route(app: str, name: str, hosts: list[str], service: str, port: int, section: str = "apps") -> dict:
    return {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": {"name": name, "namespace": f"app-{app}"},
            "spec": {"parentRefs": [{"name": "jarvis", "namespace": "platform-gateway", "sectionName": section}],
                     "hostnames": hosts, "rules": [{"backendRefs": [{"name": service, "port": port}]}]}}


def test_derive_makes_what_the_app_file_asks_for(tmp_path: Path) -> None:
    """Rule: the quota, each web entry's route but an ownRoute one, and each allow come from the App's network
    section only, app to app one side each, LAN ports and custom hostnames as granted; backups from the credential."""
    notes, photos, web, lan = app_doc(APPS, "notes"), app_doc(APPS, "photos"), app_doc(APPS, "web-demo"), ENV["lan"][0]
    wiki = app_doc(APPS, "wiki", network={"web": [{"service": "wiki", "port": 80, "hostnames": ["wiki.example.org", "old.example.org"]},
                                                  {"name": "admin", "service": "wiki-admin", "port": 8080}]})
    credential = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "backups-restic-app-notes", "namespace": "platform-backups"}}
    peers = lambda names: {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": [f"app-{n}" for n in names]}]}  # noqa: E731
    web_allow = _network_policy("web-demo", "app-web", {"policyTypes": ["Ingress"], "ingress": [{
        "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                  "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}]}]})
    ports = _network_policy("lan", "app-ports", {"policyTypes": ["Ingress"], "ingress": [{
        "from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
        "ports": [{"port": 8443, "protocol": "TCP"}, {"port": 8080, "protocol": "TCP"}]}]})
    backups = _network_policy("notes", "app-backups", {"policyTypes": ["Egress"], "egress": [{
        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "platform-backups"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "restic-apps"}}}],
        "ports": [{"port": 8000, "protocol": "TCP"}]}]})
    # The same ports as lan, with the env granting one of them, or none.
    partial, ungranted = (app_doc(APPS, name, network=lan["spec"]["network"]) for name in ("lan-partial", "lan-ungranted"))
    partial_ports = _network_policy("lan-partial", "app-ports", {"policyTypes": ["Ingress"], "ingress": [{
        "from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"port": 8080, "protocol": "TCP"}]}]})
    _derive_test(tmp_path, [notes, photos, web, lan, wiki, credential, partial, ungranted],
                 {"notes": [], "photos": [], "web-demo": [], "lan": ENV["lan"][1], "wiki": ["hostname wiki.example.org"],
                  "lan-partial": ["lan-port 443/TCP"], "lan-ungranted": []}, [
        ("quota", (notes, _quota("notes", "1", "1Gi"))),
        ("quota", (lan, _quota("lan", "500m", "512Mi", "2"))),
        ("apps", (notes, _network_policy("notes", "app-apps", {"policyTypes": ["Egress"], "egress": [{"to": [{"namespaceSelector": peers(["photos"])}]}]}))),
        ("callers", (photos, _network_policy("photos", "app-callers", {"policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector": peers(["notes"])}]}]}))),
        ("apps", (photos, None)),
        ("callers", (notes, None)),
        ("web", (web, web_allow)),
        ("web", (lan, None)),
        ("ports", (lan, ports)),
        ("ports", (partial, partial_ports)),
        ("ports", (ungranted, None)),
        ("ports", (web, None)),
        ("internet", (web, None)),
        ("route", (notes, _derived_route("notes", "app-web", [f"notes.apps.{SUFFIX}"], "notes", 80))),
        ("hostnames", (notes, None)),
        ("route", (web, None)),
        ("route", (lan, None)),
        ("route", (wiki, _derived_route("wiki", "app-web", [f"wiki.apps.{SUFFIX}"], "wiki", 80))),
        ("route-admin", (wiki, _derived_route("wiki", "app-web-admin", [f"wiki-admin.apps.{SUFFIX}"], "wiki-admin", 8080))),
        ("hostnames", (wiki, _derived_route("wiki", "app-hostnames", ["wiki.example.org"], "wiki", 80, "apps-granted"))),
        ("backups", (credential, backups)),
    ])


def _allows(policy: dict, address: str, port: int, protocol: str) -> bool:
    """Whether a NetworkPolicy's egress lets a pod reach address:port/protocol."""
    ip = ipaddress.ip_address(address)
    for rule in policy["spec"].get("egress") or []:
        ports = rule.get("ports")
        if ports and not any(p["port"] == port and p.get("protocol", "TCP") == protocol for p in ports):
            continue
        for peer in rule.get("to") or []:
            block = peer.get("ipBlock")
            if block and ip in ipaddress.ip_network(block["cidr"]) and not any(
                    ip in ipaddress.ip_network(cidr) for cidr in block.get("except") or []):
                return True
    return False


_LAN_TARGETS = ["lan-target nas 10.200.0.10/32 445/TCP", "lan-target nas 10.200.0.10/32 137/UDP",
                "lan-target scanner 10.200.0.20/32 80/TCP"]


def test_derive_opens_internet_and_lan_only_as_asked_and_granted(tmp_path: Path) -> None:
    """Rule: internet is any port to a public address, never the LAN or the cluster; the whole LAN opens only for
    lan: [all] with the env's lan-all, a target only when the App lists it, on the ports its lan-target lines give."""
    surfer = app_doc(APPS, "surfer", network={"egress": {"internet": True}})
    whole, whole_ungranted = (app_doc(APPS, name, network={"egress": {"lan": ["all"]}}) for name in ("whole", "whole-ungranted"))
    named = app_doc(APPS, "named", network={"egress": {"lan": ["nas", "printer"]}})
    named_ungranted = app_doc(APPS, "named-ungranted", network={"egress": {"lan": ["nas"]}})
    internet = _network_policy("surfer", "app-internet", {"policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {
        "cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
                                        "10.80.0.0/16", "10.81.0.0/16", "10.200.0.0/24"]}}]}]})
    lan_all = _network_policy("whole", "app-lan-all", {"policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {"cidr": "10.200.0.0/24"}}]}]})
    nas = _network_policy("named", "app-lan", {"policyTypes": ["Egress"], "egress": [
        {"to": [{"ipBlock": {"cidr": "10.200.0.10/32"}}], "ports": [{"port": 137, "protocol": "UDP"}]},
        {"to": [{"ipBlock": {"cidr": "10.200.0.10/32"}}], "ports": [{"port": 445, "protocol": "TCP"}]}]})
    _derive_test(tmp_path, [surfer, whole, whole_ungranted, named, named_ungranted],
                 {"surfer": [], "whole": ["lan-all"], "whole-ungranted": _LAN_TARGETS,
                  "named": _LAN_TARGETS, "named-ungranted": ["lan-all"]}, [
        ("internet", (surfer, internet)),
        ("lan-all", (surfer, None)),
        ("lan", (surfer, None)),
        ("lan-all", (whole, lan_all)),
        ("lan-all", (whole_ungranted, None)),
        ("lan", (whole_ungranted, None)),
        ("lan", (named, nas)),
        ("lan-all", (named, None)),
        ("lan", (named_ungranted, None)),
        ("lan-all", (named_ungranted, None)),
    ])
    # What the generated allows let through (kyverno apply made them equal to these).
    assert _allows(internet, "203.0.113.7", 22, "TCP") and _allows(internet, "198.51.100.9", 5000, "UDP")
    for address in ("10.200.0.10", "192.168.1.1", "10.80.0.5", "10.81.0.1", "169.254.169.254", "100.64.0.1"):
        assert not _allows(internet, address, 443, "TCP"), address
    assert _allows(lan_all, "10.200.0.99", 8123, "TCP") and not _allows(lan_all, "203.0.113.7", 443, "TCP")
    assert _allows(nas, "10.200.0.10", 445, "TCP") and _allows(nas, "10.200.0.10", 137, "UDP")
    assert not _allows(nas, "10.200.0.10", 22, "TCP") and not _allows(nas, "10.200.0.20", 80, "TCP")
    # No allow an app's file derives reaches another app's pod (the env's pod range), so an
    # app's LAN port, open to any address, is not open to other apps' pods.
    for allow in (internet, lan_all, nas):
        assert not _allows(allow, "10.80.3.4", 8443, "TCP")


def test_app_namespace_lets_backup_movers_run_privileged() -> None:
    """Rule: the app's namespace lets VolSync's movers run privileged (local-path ignores fsGroup); who may make a
    ReplicationSource there: test_backup_enrollment.py."""
    namespace = next(doc for doc in app_wiring("notes", []) if doc["kind"] == "Namespace")
    assert namespace["metadata"]["annotations"]["volsync.backube/privileged-movers"] == "true"
