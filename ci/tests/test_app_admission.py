"""Hosted-app admission, offline: every rule in platform/app-hosting/policies against seeded
good and bad objects, with the pinned kyverno CLI, and tools/app-check on the same objects.

The namespaces are rendered from the real per-app wiring (platform/app-hosting/app) with the
env's values, so app-grants sees the annotations the cluster gets. The CLI cannot run two parts
as they are: it does not resolve the selector paramRef of the cluster-wide app-file binding, so
a binding naming each App stands in for it, and its namespaceObject holds only a name and
labels, so app-grants' grants variable reads the rendered annotations from a literal instead.
test_app_admission_apiserver.py proves both real ones.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT

import _apps
from _app_file import check_folder
from _manifests import substitute

APPS = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "apps"
POLICIES = REPO_ROOT / "platform" / "app-hosting" / "policies"
PRIORITY = REPO_ROOT / "platform" / "app-hosting" / "priority.yaml"
WIRING = REPO_ROOT / "platform" / "app-hosting" / "app"
SUFFIX = "env-00.example"
TENANT = "system:serviceaccount:flux-system:app-{app}"
VOLSYNC = "system:serviceaccount:volsync:volsync"
JOB_CONTROLLER = "system:serviceaccount:kube-system:job-controller"
CLUSTER_VARS = {"pod_cidr": "10.80.0.0/16", "service_cidr": "10.81.0.0/16", "lan_cidrs": ["10.200.0.0/24"],
                "apps_git_repo_url": "https://example.invalid/apps"}


def _kyverno() -> str:
    path = shutil.which("kyverno")
    if path is None:
        pytest.fail("kyverno CLI not found on PATH (run make deps)")
    return path


def _docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]


def _app(name: str, **spec: object) -> dict:
    """A fixture app's App, or a new one, as the cluster holds it (in its namespace)."""
    path = APPS / name / "app.yaml"
    app = _docs(path)[0] if path.is_file() else {
        "apiVersion": "hosting.jarvis.io/v1alpha1", "kind": "App", "metadata": {"name": name},
        "spec": {"network": {"web": {"port": 8080}}, "volumes": []},
    }
    app = copy.deepcopy(app)
    app["spec"].update(spec)
    app["metadata"]["namespace"] = f"app-{name}"
    return app


def _wiring(app: str, grants: list[str]) -> list[dict]:
    """What the app's wiring applies, rendered with the env's exception lines for it."""
    data = {"apps": [app], "exceptions": [{app: grant} for grant in grants]}
    values = {**_apps.substitutes(data, app, CLUSTER_VARS), "dns_suffix": SUFFIX, "jarvis_env": "env-00"}
    return substitute(_docs(WIRING / "namespace.yaml"), values)


def _app_file_stand_in(app: str) -> dict:
    """What the cluster-wide app-file binding resolves to in app's namespace."""
    return {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": f"app-file-{app}"},
            "spec": {"policyName": "app-file", "validationActions": ["Deny"],
                     "paramRef": {"name": app, "namespace": f"app-{app}", "parameterNotFoundAction": "Deny"},
                     "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": f"app-{app}"}}}}}


def _deploy(app: str) -> list[dict]:
    """A fixture app's deploy/ objects, as its Kustomization applies them."""
    docs = [doc for path in sorted((APPS / app / "deploy").glob("*.yaml")) for doc in _docs(path)]
    docs = substitute(docs, {"app_host": f"{app}.apps.{SUFFIX}"})
    for doc in docs:
        doc["metadata"]["namespace"] = f"app-{app}"
    return docs


_RESTRICTED = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


def _mutated(tmp_path: Path, docs: list[dict], user: str | None = None) -> list[dict]:
    """docs as ClusterPolicy app-priority leaves them at admission (kyverno apply), written by
    user (default: the tenant of the first doc's namespace)."""
    work = tmp_path / f"mutate-{len(list(tmp_path.glob('mutate-*')))}"
    work.mkdir()
    user = user or TENANT.format(app=docs[0]["metadata"]["namespace"].removeprefix("app-"))
    (work / "resources.yaml").write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    # kyverno apply loads no policy from a file that also holds the PriorityClass.
    policy = next(doc for doc in _docs(PRIORITY) if doc["kind"] == "ClusterPolicy")
    (work / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    command = [_kyverno(), "apply", str(work / "policy.yaml"), "--resource", str(work / "resources.yaml"),
               "--remove-color", "-o", str(work / "out.yaml"), "--userinfo", str(_userinfo(work / "userinfo.yaml", user))]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    assert proc.returncode == 0 and "Applying 2 policy rule(s)" in proc.stdout, proc.stdout + proc.stderr
    out = work / "out.yaml"
    changed = {(doc["kind"], doc["metadata"]["name"]): doc for doc in _docs(out)} if out.is_file() else {}
    return [changed.get((doc["kind"], doc["metadata"]["name"]), doc) for doc in docs]


def _pod(name: str, app: str, *, owner_job: bool = False, **pod: object) -> dict:
    """A Pod Security restricted pod at the hosted-app class, as app-priority leaves it."""
    spec = {
        "priorityClassName": "hosted-app",
        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "c", "image": "registry.example/x@sha256:" + "0" * 64,
                        "securityContext": copy.deepcopy(_RESTRICTED)}],
        **pod,
    }
    spec = {key: value for key, value in spec.items() if value is not None}
    metadata: dict = {"name": name, "namespace": f"app-{app}"}
    if owner_job:
        metadata["ownerReferences"] = [
            {"apiVersion": "batch/v1", "kind": "Job", "name": "volsync-src-data", "uid": "1", "controller": True}
        ]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": spec}


def _job(name: str, app: str, **pod: object) -> dict:
    template = _pod("t", app, **pod)
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": f"app-{app}", "labels": {"app.kubernetes.io/created-by": "volsync"}},
        "spec": {"template": {"spec": template["spec"]}},
    }


_PRIVILEGED = {"containers": [{"name": "c", "image": "x", "securityContext": {"privileged": True, "runAsUser": 0}}],
               "securityContext": {}}


def _object(kind: str, name: str, app: str, api: str = "v1", **spec: object) -> dict:
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": f"app-{app}"}, "spec": spec}


def _ingress(name: str, app: str, host: str, **spec: object) -> dict:
    rule = {"host": host, "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {"service": {"name": "web", "port": {"number": 80}}}}]}}
    return _object("Ingress", name, app, "networking.k8s.io/v1", rules=[rule], **spec)


def _pvc(name: str, app: str, *, unbacked: bool = False) -> dict:
    doc = _object("PersistentVolumeClaim", name, app, accessModes=["ReadWriteOnce"], resources={"requests": {"storage": "1Gi"}})
    if unbacked:
        doc["metadata"]["annotations"] = {"backup": "none"}
    return doc


# The env for these cases: the three sample apps with env-00's exception line, and two apps
# that ask for privileged mode, one granted and one not.
_ENV = {
    "web-demo": (_app("web-demo"), []),
    "notes": (_app("notes"), []),
    "photos": (_app("photos"), ["host-path"]),
    "granted": (_app("granted", risky={"privileged": True}), ["privileged"]),
    "ungranted": (_app("ungranted", risky={"privileged": True}), []),
    "lan": (_app("lan", network={"ports": [{"listen": 8443, "target": 8443, "protocol": "TCP"},
                                           {"listen": 443, "target": 8080, "protocol": "TCP"}]}),
            ["lan-port 8443/TCP", "lan-port 443/TCP"]),
}


def _run(tmp_path: Path, label: str, user: str | None, cases: list[tuple[dict, dict[str, str]]]) -> None:
    """kyverno test on cases: (object, {policy: pass|fail|skip}). Fails with the CLI's table."""
    work = tmp_path / label
    work.mkdir()
    namespaces = [doc for app, (_doc, grants) in _ENV.items() for doc in _wiring(app, grants)]
    policies = [doc for doc in _docs(POLICIES / "admission.yaml")
                if (doc["kind"], doc["metadata"]["name"]) != ("ValidatingAdmissionPolicyBinding", "app-file")]
    annotations = {doc["metadata"]["name"]: doc["metadata"]["annotations"] for doc in namespaces}
    for policy in policies:
        if (policy["kind"], policy["metadata"]["name"]) == ("ValidatingAdmissionPolicy", "app-grants"):
            grants = next(item for item in policy["spec"]["variables"] if item["name"] == "grants")
            grants["expression"] = f"{json.dumps(annotations)}[namespaceObject.metadata.name]"
    (work / "policies.yaml").write_text(
        yaml.safe_dump_all([*policies, *(_app_file_stand_in(app) for app in _ENV)]), encoding="utf-8"
    )
    (work / "params.yaml").write_text(yaml.safe_dump_all([app for app, _ in _ENV.values()]), encoding="utf-8")
    (work / "resources.yaml").write_text(yaml.safe_dump_all([doc for doc, _ in cases]), encoding="utf-8")
    selector = [
        {"name": doc["metadata"]["name"], "labels": {**doc["metadata"]["labels"], "kubernetes.io/metadata.name": doc["metadata"]["name"]}}
        for doc in namespaces
    ] + [{"name": name, "labels": {"kubernetes.io/metadata.name": name}} for name in ("flux-system", "default")]
    (work / "values.yaml").write_text(
        yaml.safe_dump({"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Value", "metadata": {"name": "v"}, "namespaceSelector": selector}),
        encoding="utf-8",
    )
    results = [
        {"isValidatingAdmissionPolicy": True, "kind": doc["kind"], "policy": policy, "resources": [doc["metadata"]["name"]], "result": result}
        for doc, expected in cases
        for policy, result in expected.items()
    ]
    test = {
        "apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Test", "metadata": {"name": label},
        "policies": ["policies.yaml"], "resources": ["resources.yaml"], "paramResources": ["params.yaml"],
        "variables": "values.yaml", "results": results,
    }
    if user is not None:
        test["userinfo"] = _userinfo(work / "userinfo.yaml", user).name
    (work / "kyverno-test.yaml").write_text(yaml.safe_dump(test), encoding="utf-8")
    proc = subprocess.run([_kyverno(), "test", str(work), "--remove-color", "--detailed-results"],
                          capture_output=True, text=True, check=False)
    wrong = [line for line in proc.stdout.splitlines() if "Want " in line]
    assert proc.returncode == 0, "\n".join(wrong) or proc.stdout + proc.stderr


PASS = {"app-file": "pass", "app-grants": "pass"}
FILE_FAILS = {"app-file": "fail"}
GRANT_FAILS = {"app-grants": "fail"}
BOTH_FAIL = {"app-file": "fail", "app-grants": "fail"}


def test_sample_apps_pass_admission(tmp_path: Path) -> None:
    """Rule: the sample apps' objects, the hostPath one with its env line, pass the baseline once
    app-priority has set their class; without it (Kyverno down) their workloads are refused."""
    docs = [doc for app in ("web-demo", "notes", "photos") for doc in _deploy(app)]
    _run(tmp_path, "samples", None, [(doc, PASS) for doc in _mutated(tmp_path, docs)])
    _run(tmp_path, "unmutated", None, [(doc, FILE_FAILS) for doc in docs if doc["kind"] == "Deployment"])


def test_pod_baseline_refuses_risky_items_without_a_grant(tmp_path: Path) -> None:
    """Rule: privileged, root, hostPath, host network, host ports, host PID and IPC need the file
    and the env; a restricted-style pod passes; an asked item passes only with the env's line."""
    host_path = {"volumes": [{"name": "h", "hostPath": {"path": "/srv"}}]}
    cases = [
        (_pod("restricted", "web-demo"), PASS),
        (_pod("privileged", "web-demo", **_PRIVILEGED), BOTH_FAIL),
        (_pod("root", "web-demo", securityContext={}), BOTH_FAIL),
        (_pod("uid-zero", "web-demo", securityContext={"runAsNonRoot": True, "runAsUser": 0}), BOTH_FAIL),
        (_pod("host-path", "web-demo", **host_path), BOTH_FAIL),
        (_pod("host-network", "web-demo", hostNetwork=True), BOTH_FAIL),
        (_pod("host-pid", "web-demo", hostPID=True), BOTH_FAIL),
        (_pod("host-ipc", "web-demo", hostIPC=True), BOTH_FAIL),
        (_pod("host-port", "web-demo", containers=[{"name": "c", "image": "x", "ports": [{"containerPort": 80, "hostPort": 80}]}]), BOTH_FAIL),
        (_pod("capability", "web-demo", containers=[{"name": "c", "image": "x", "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"add": ["SYS_ADMIN"]}}}]), BOTH_FAIL),
        (_pod("escalation-unset", "web-demo", containers=[{"name": "c", "image": "x"}]), BOTH_FAIL),
        (_pod("unconfined", "web-demo", securityContext={"runAsNonRoot": True, "seccompProfile": {"type": "Unconfined"}}), BOTH_FAIL),
        (_pod("sysctls", "web-demo", securityContext={"runAsNonRoot": True, "sysctls": [{"name": "net.core.somaxconn", "value": "1024"}]}), BOTH_FAIL),
        (_pod("nfs", "web-demo", volumes=[{"name": "n", "nfs": {"server": "nfs.example", "path": "/"}}]), FILE_FAILS),
        (_pod("volumes-ok", "web-demo", volumes=[{"name": "e", "emptyDir": {}}, {"name": "c", "configMap": {"name": "x"}}]), PASS),
        # Asked for and granted; granted but also root, which neither asks for; asked for and not
        # granted; granted for another item only.
        (_pod("granted-privileged", "granted", containers=[{"name": "c", "image": "x", "securityContext": {"privileged": True}}]), PASS),
        (_pod("granted-privileged-as-root", "granted", **_PRIVILEGED), BOTH_FAIL),
        (_pod("ungranted-privileged", "ungranted", containers=[{"name": "c", "image": "x", "securityContext": {"privileged": True}}]), GRANT_FAILS),
        (_pod("photos-privileged", "photos", containers=[{"name": "c", "image": "x", "securityContext": {"privileged": True}}]), BOTH_FAIL),
        (_job("tenant-job", "web-demo", **_PRIVILEGED), BOTH_FAIL),
    ]
    _run(tmp_path, "baseline", None, cases)


def _container(**security: object) -> dict:
    return {"name": "c", "image": "x", "securityContext": {**copy.deepcopy(_RESTRICTED), **security}}


def test_pod_security_restricted_beyond_the_baseline_needs_privileged(tmp_path: Path) -> None:
    """Rule: Pod Security restricted holds every app pod: each container drops ALL (adding back
    only NET_BIND_SERVICE), a RuntimeDefault or Localhost seccomp profile is set, and no
    unmasked /proc, unconfined AppArmor or SELinux option beyond a container type; anything
    beyond needs risky.privileged and the env's privileged line."""
    no_seccomp = {"runAsNonRoot": True}
    cases = [
        (_pod("bind-service", "web-demo", containers=[_container(capabilities={"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]})]), PASS),
        (_pod("container-seccomp", "web-demo", securityContext=no_seccomp,
              containers=[_container(seccompProfile={"type": "Localhost", "localhostProfile": "p.json"})]), PASS),
        (_pod("selinux-container-type", "web-demo", containers=[_container(seLinuxOptions={"type": "container_init_t"})]), PASS),
        (_pod("no-drop-all", "web-demo", containers=[_container(capabilities={"drop": ["NET_RAW"]})]), BOTH_FAIL),
        (_pod("no-capabilities", "web-demo", containers=[{"name": "c", "image": "x", "securityContext": {"allowPrivilegeEscalation": False}}]), BOTH_FAIL),
        (_pod("no-seccomp", "web-demo", securityContext=no_seccomp), BOTH_FAIL),
        (_pod("one-container-unconfined", "web-demo", containers=[_container(), _container(seccompProfile={"type": "Unconfined"})]), BOTH_FAIL),
        (_pod("proc-unmasked", "web-demo", containers=[_container(procMount="Unmasked")]), BOTH_FAIL),
        (_pod("apparmor-unconfined", "web-demo", containers=[_container(appArmorProfile={"type": "Unconfined"})]), BOTH_FAIL),
        (_pod("selinux-user", "web-demo", securityContext={**no_seccomp, "seccompProfile": {"type": "RuntimeDefault"},
                                                            "seLinuxOptions": {"user": "system_u"}}), BOTH_FAIL),
        (_pod("selinux-type", "web-demo", containers=[_container(seLinuxOptions={"type": "spc_t"})]), BOTH_FAIL),
        (_pod("no-drop-granted", "granted", containers=[_container(capabilities={})]), PASS),
    ]
    for name, profile, expected in (("apparmor-unconfined-annotation", "unconfined", BOTH_FAIL),
                                    ("apparmor-other-annotation", "other", BOTH_FAIL),
                                    ("apparmor-localhost-annotation", "localhost/p", PASS)):
        annotated = _pod(name, "web-demo")
        annotated["metadata"]["annotations"] = {"container.apparmor.security.beta.kubernetes.io/c": profile}
        cases.append((annotated, expected))
    _run(tmp_path, "restricted", None, cases)


def test_app_containers_reserve_their_memory_limit(tmp_path: Path) -> None:
    """Rule: a hosted app's container memory request equals its limit (the same quantity in any
    unit, or a limit alone), so it never uses more of a node than the scheduler set aside."""
    def sized(name: str, resources: dict) -> dict:
        return _pod(name, "web-demo", containers=[{**_container(), "resources": resources}])

    _run(tmp_path, "memory", None, [
        (sized("equal", {"requests": {"memory": "128Mi"}, "limits": {"memory": "0.125Gi"}}), PASS),
        (sized("limit-only", {"limits": {"memory": "128Mi"}}), PASS),
        (sized("under-limit", {"requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}}), FILE_FAILS),
    ])


def test_app_pods_run_at_the_hosted_app_class_only(tmp_path: Path) -> None:
    """Rule: app-priority sets hosted-app on every workload the tenant ships, over any class it
    picks; admission refuses a pod or workload at another class or none. The VolSync
    controller's movers are left as they are."""
    deployment = next(doc for doc in _deploy("web-demo") if doc["kind"] == "Deployment")
    deployment["spec"]["template"]["spec"]["priorityClassName"] = "system-cluster-critical"
    cron = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "nightly", "namespace": "app-web-demo"},
            "spec": {"schedule": "0 0 * * *", "jobTemplate": {"spec": {"template": {"spec": {
                **{k: v for k, v in _pod("t", "web-demo")["spec"].items() if k != "priorityClassName"}, "restartPolicy": "Never"}}}}}}
    mutated, mutated_cron = _mutated(tmp_path, [deployment, cron], user=TENANT.format(app="web-demo"))
    assert mutated["spec"]["template"]["spec"]["priorityClassName"] == "hosted-app"
    assert mutated_cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["priorityClassName"] == "hosted-app"
    mover = _job("volsync-src-data", "notes")
    del mover["spec"]["template"]["spec"]["priorityClassName"]
    assert "priorityClassName" not in _mutated(tmp_path, [mover], user=VOLSYNC)[0]["spec"]["template"]["spec"]
    deployment["metadata"]["name"] = "unmutated"
    _run(tmp_path, "priority", None, [
        (mutated, PASS), (mutated_cron, PASS), (deployment, FILE_FAILS),
        (_pod("other-class", "web-demo", priorityClassName="high"), FILE_FAILS),
        (_pod("no-class", "web-demo", priorityClassName=None), FILE_FAILS),
    ])


def test_ingress_keeps_only_the_web_router_annotations(tmp_path: Path) -> None:
    """Rule: an app's Ingress is on class traefik with entrypoint websecure and carries, of router
    annotations, only that and tls true; other annotations (and Flux's labels) pass. A Service
    carries no Traefik annotation."""
    host = f"web-demo.apps.{SUFFIX}"
    ok = {"traefik.ingress.kubernetes.io/router.entrypoints": "websecure", "traefik.ingress.kubernetes.io/router.tls": "true"}

    def ingress(name: str, annotations: dict, **spec: object) -> dict:
        spec = {key: value for key, value in {"ingressClassName": "traefik", **spec}.items() if value is not None}
        doc = _ingress(name, "web-demo", host, **spec)
        doc["metadata"]["annotations"] = annotations
        doc["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "app-web-demo", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
        return doc

    service = _object("Service", "s", "web-demo", ports=[{"port": 80}])
    service["metadata"]["annotations"] = {"traefik.ingress.kubernetes.io/service.serverstransport": "x"}
    _run(tmp_path, "ingress", None, [
        (ingress("flux-applied", {**ok, "kustomize.toolkit.fluxcd.io/ssa": "Merge", "example.org/owner": "me"}), PASS),
        (ingress("entrypoint-only", {"traefik.ingress.kubernetes.io/router.entrypoints": "websecure"}), PASS),
        (ingress("no-entrypoint", {}), FILE_FAILS),
        (ingress("middleware", {**ok, "traefik.ingress.kubernetes.io/router.middlewares": "kube-system-x@kubernetescrd"}), FILE_FAILS),
        (ingress("other-entrypoint", {**ok, "traefik.ingress.kubernetes.io/router.entrypoints": "web"}), FILE_FAILS),
        (ingress("other-controller", {"nginx.ingress.kubernetes.io/server-snippet": "x"}), FILE_FAILS),
        (ingress("legacy-class", {"kubernetes.io/ingress.class": "other"}), FILE_FAILS),
        (ingress("no-class", ok, ingressClassName=None), FILE_FAILS),
        (ingress("other-class", ok, ingressClassName="nginx"), FILE_FAILS),
        (service, FILE_FAILS),
    ])


def test_claims_bind_only_to_what_the_cluster_provisions(tmp_path: Path) -> None:
    """Rule: the tenant sets no volumeName, dataSource or dataSourceRef on a PVC or claim
    template; the volume binder's volumeName update passes, and so does a later apply of the
    bound claim; the VolSync controller's own claims are carved out."""
    def claim(name: str, **spec: object) -> dict:
        doc = _pvc("data", "notes")
        doc["metadata"]["name"] = name
        doc["spec"].update(spec)
        return doc

    tenant = TENANT.format(app="notes")
    template = _job("t", "notes")["spec"]["template"]
    sts = lambda name, spec: _object("StatefulSet", name, "notes", "apps/v1", serviceName="db", selector={}, template=template,  # noqa: E731
                                     volumeClaimTemplates=[{"metadata": {"name": "data"}, "spec": spec}])
    # kyverno test reads each case as a CREATE; claim names stand in for the declared volume.
    for case in ("volume-name", "data-source", "data-source-ref"):
        _ENV["notes"][0]["spec"]["volumes"].append({"name": case, "backup": True})
    try:
        _run(tmp_path, "claims", tenant, [
            (claim("data"), PASS),
            (sts("sts-clean", {}), PASS),
            (claim("volume-name", volumeName="pv-other"), FILE_FAILS),
            (claim("data-source", dataSource={"kind": "PersistentVolumeClaim", "name": "data"}), FILE_FAILS),
            (claim("data-source-ref", dataSourceRef={"kind": "PersistentVolumeClaim", "name": "data"}), FILE_FAILS),
            (sts("sts-volume-name", {"volumeName": "pv-other"}), FILE_FAILS),
            (sts("sts-data-source", {"dataSource": {"kind": "PersistentVolumeClaim", "name": "data"}}), FILE_FAILS),
        ])
        # The binder's write. kyverno's CLI sends no oldObject, so a tenant's later apply of the
        # bound claim (volumeName unchanged) is proven only live.
        _run(tmp_path, "binder", "system:serviceaccount:kube-system:persistent-volume-binder",
             [(claim("volume-name", volumeName="pv-1"), PASS)])
    finally:
        del _ENV["notes"][0]["spec"]["volumes"][-3:]
    restored = claim("volsync-data-dst", dataSourceRef={"kind": "ReplicationDestination", "apiGroup": "volsync.backube", "name": "d"})
    _run(tmp_path, "volsync-claim", VOLSYNC, [(restored, {"app-file": "skip", "app-grants": "skip"})])


def test_binder_updates_volsync_cache_pvcs(tmp_path: Path) -> None:
    """Rule: the volume binder's PreBind UPDATE of a VolSync-owned cache PVC (ownerReference to
    ReplicationSource or ReplicationDestination) passes even when undeclared; the tenant's CREATE
    of an undeclared PVC still fails."""
    cache = _pvc("volsync-src-data-cache", "notes")
    cache["metadata"]["ownerReferences"] = [
        {"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource", "name": "data", "uid": "1"}
    ]
    undeclared = _pvc("undeclared", "notes")
    binder = "system:serviceaccount:kube-system:persistent-volume-binder"
    tenant = TENANT.format(app="notes")
    # The binder's work on a VolSync cache PVC passes (provenance via ownerReference).
    _run(tmp_path, "binder-volsync", binder, [(cache, PASS)])
    # The tenant's undeclared PVC still fails.
    _run(tmp_path, "tenant-undeclared", tenant, [(undeclared, FILE_FAILS)])


def test_volsync_mover_passes_and_a_look_alike_does_not(tmp_path: Path) -> None:
    """Rule: the VolSync controller's privileged mover Job, and the job controller's pod for it,
    pass; the same Job or Pod from the app's own identity is refused."""
    mover_job = _job("volsync-src-data", "notes", **_PRIVILEGED)
    mover_pod = _pod("volsync-src-data-x", "notes", owner_job=True, **_PRIVILEGED)
    cache = _pvc("volsync-data-cache", "notes")
    skip = {"app-file": "skip", "app-grants": "skip"}
    _run(tmp_path, "volsync", VOLSYNC, [(mover_job, skip), (cache, skip)])
    _run(tmp_path, "job-controller", JOB_CONTROLLER, [(mover_pod, skip)])
    _run(tmp_path, "look-alike", TENANT.format(app="notes"), [(mover_job, BOTH_FAIL), (mover_pod, BOTH_FAIL), (cache, FILE_FAILS),
                                                         (_pvc("data-db-0", "notes"), FILE_FAILS)])


def test_statefulset_controller_claims_of_a_declared_volume_pass(tmp_path: Path) -> None:
    """Rule: the StatefulSet controller's claim <volume>-<set>-<n> of a declared template passes;
    one of an undeclared template does not."""
    _run(tmp_path, "sts", "system:serviceaccount:kube-system:statefulset-controller",
         [(_pvc("data-db-0", "notes"), PASS), (_pvc("pgdata-db-0", "notes"), FILE_FAILS)])


def test_volumes_hosts_and_services_match_the_file_and_the_env(tmp_path: Path) -> None:
    """Rule: a PVC is a declared volume with the declared backup; an Ingress host is
    <name>.apps.<suffix>; Services are ClusterIP, or a LoadBalancer on the file's network.ports
    (listen, protocol, numeric target) that the env grants as lan-port."""
    lb = {"type": "LoadBalancer", "allocateLoadBalancerNodePorts": False,
          "ports": [{"port": 8443, "protocol": "TCP"}], "selector": {"app": "x"}}
    with_node_ports = {**lb, "allocateLoadBalancerNodePorts": True}
    mapped = {**lb, "ports": [{"port": 443, "protocol": "TCP", "targetPort": 8080}]}
    wrong_target = {**lb, "ports": [{"port": 443, "protocol": "TCP", "targetPort": 9090}]}
    unset_target = {**lb, "ports": [{"port": 443, "protocol": "TCP"}]}
    named_target = {**lb, "ports": [{"port": 443, "protocol": "TCP", "targetPort": "http"}]}
    other_protocol = {**lb, "ports": [{"port": 8443, "protocol": "UDP"}]}
    cases = [
        (_pvc("undeclared", "notes"), FILE_FAILS),
        (_pvc("cache", "notes"), FILE_FAILS),
        (_pvc("data", "notes", unbacked=True), FILE_FAILS),
        (_object("StatefulSet", "db", "notes", "apps/v1", serviceName="db", selector={},
                 template=_job("t", "notes")["spec"]["template"],
                 volumeClaimTemplates=[{"metadata": {"name": "pgdata"}, "spec": {}}]), FILE_FAILS),
        (_ingress("core-host", "web-demo", f"grafana.{SUFFIX}"), BOTH_FAIL),
        (_ingress("other-app-host", "web-demo", f"notes.apps.{SUFFIX}"), BOTH_FAIL),
        (_ingress("look-alike-host", "web-demo", f"web-demo.apps.{SUFFIX}.evil.example"), GRANT_FAILS),
        (_ingress("own-tls", "web-demo", f"web-demo.apps.{SUFFIX}", tls=[{"hosts": [f"web-demo.apps.{SUFFIX}"], "secretName": "x"}]), GRANT_FAILS),
        (_object("Service", "lb", "web-demo", **lb), BOTH_FAIL),
        (_object("Service", "lan", "lan", **lb), PASS),
        (_object("Service", "lan-mapped", "lan", **mapped), PASS),
        (_object("Service", "lan-wrong-target", "lan", **wrong_target), FILE_FAILS),
        (_object("Service", "lan-unset-target", "lan", **unset_target), FILE_FAILS),
        (_object("Service", "lan-named-target", "lan", **named_target), FILE_FAILS),
        (_object("Service", "lan-other-protocol", "lan", **other_protocol), BOTH_FAIL),
        (_object("Service", "lan-node-ports", "lan", **with_node_ports), FILE_FAILS),
        (_object("Service", "nodeport", "web-demo", type="NodePort", ports=[{"port": 80}]), GRANT_FAILS),
        (_object("Service", "external-ips", "web-demo", ports=[{"port": 80}], externalIPs=["192.0.2.1"]), GRANT_FAILS),
    ]
    _run(tmp_path, "objects", None, cases)


def test_an_app_cannot_write_its_own_grants_or_leave_its_namespace(tmp_path: Path) -> None:
    """Rule: the app's identity writes only its shipped kinds in app-<name>, and a Namespace is
    not one, so it cannot write its own grants. A grants ConfigMap it ships is refused in
    flux-system and read by nothing in its own namespace, so an exception line in the app's own
    folder grants nothing."""
    tenant = TENANT.format(app="web-demo")
    local = {"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": "app-web-demo-grants", "namespace": "app-web-demo"}, "data": {"privileged": "true"}}
    forged = copy.deepcopy(local)
    forged["metadata"] = {"name": "forged-grants", "namespace": "flux-system"}
    network = _object("NetworkPolicy", "allow-all", "web-demo", "networking.k8s.io/v1", podSelector={})
    other = _object("ConfigMap", "elsewhere", "notes")
    own_namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "app-web-demo", "namespace": "app-web-demo"}}
    tenant_ok = {"app-tenant": "pass"}
    tenant_fails = {"app-tenant": "fail"}
    _run(tmp_path, "tenant", tenant, [
        (local, tenant_ok), (forged, tenant_fails), (network, tenant_fails), (other, tenant_fails),
        (own_namespace, tenant_fails),
        (_pod("privileged-after-forging", "web-demo", **_PRIVILEGED), BOTH_FAIL),
    ])
    # A Traefik route would claim any host; kyverno apply reads a stand-in CRD for its kind.
    route = _object("IngressRoute", "steal", "web-demo", "traefik.io/v1alpha1", routes=[])
    crd = tmp_path / "ingressroute-crd.yaml"
    crd.write_text(yaml.safe_dump({
        "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "metadata": {"name": "ingressroutes.traefik.io"},
        "spec": {"group": "traefik.io", "scope": "Namespaced",
                 "names": {"kind": "IngressRoute", "plural": "ingressroutes", "singular": "ingressroute", "listKind": "IngressRouteList"},
                 "versions": [{"name": "v1alpha1", "served": True, "storage": True,
                               "schema": {"openAPIV3Schema": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}}}]},
    }), encoding="utf-8")
    assert _apply(tmp_path, "app-tenant", route, user=tenant, crd=crd) == "fail"


def _userinfo(path: Path, user: str) -> Path:
    path.write_text(
        yaml.safe_dump({"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "UserInfo", "metadata": {"name": "u"},
                        "userInfo": {"username": user, "groups": ["system:authenticated"]}}),
        encoding="utf-8",
    )
    return path


def test_app_namespaces_and_apps_keep_their_names(tmp_path: Path) -> None:
    """Rule: app-<name> is labelled hosting.jarvis.io/app: <name> and no other namespace is; an
    App lives in its own namespace."""
    def namespace(name: str, label: str | None) -> dict:
        labels = {"hosting.jarvis.io/app": label} if label else {}
        return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}}

    ok, fails = {"app-identity": "pass"}, {"app-identity": "fail"}
    _run(tmp_path, "identity", None, [
        (namespace("app-x", "x"), ok), (namespace("app-y", None), fails), (namespace("app-z", "other"), fails),
        (namespace("monitoring", "monitoring"), fails), (namespace("monitoring-2", None), ok),
    ])
    # kyverno test cannot map a custom kind; kyverno apply reads the CRD.
    wrong = _app("notes")
    wrong["metadata"]["name"] = "photos"
    assert _apply(tmp_path, "app-identity", _app("idcheck")) == "pass"
    assert _apply(tmp_path, "app-identity", wrong) == "fail"


def _apply(tmp_path: Path, policy: str, resource: dict, *, user: str | None = None, crd: Path | None = None) -> str:
    """kyverno apply of one policy (and its cluster-wide binding) to one resource: pass or fail."""
    docs = [doc for doc in _docs(POLICIES / "admission.yaml") if doc["metadata"]["name"] == policy]
    work = tmp_path / f"apply-{resource['metadata']['name']}"
    work.mkdir()
    (work / "policy.yaml").write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    (work / "resource.yaml").write_text(yaml.safe_dump(resource), encoding="utf-8")
    command = [_kyverno(), "apply", str(work / "policy.yaml"), "--resource", str(work / "resource.yaml"), "--remove-color",
               "--crd-paths", str(crd or REPO_ROOT / "platform" / "app-hosting" / "crd" / "app-crd.yaml")]
    if user is not None:
        command += ["--userinfo", str(_userinfo(work / "userinfo.yaml", user))]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    summary = proc.stdout + proc.stderr
    if "pass: 1, fail: 0" in summary:
        return "pass"
    if "pass: 0, fail: 1" in summary:
        return "fail"
    raise AssertionError(summary)


@pytest.mark.parametrize("app", ["web-demo", "notes", "photos"])
def test_app_check_passes_the_sample_apps(app: str) -> None:
    """Rule: tools/app-check passes each sample app folder."""
    assert check_folder(APPS / app) == []


def _folder(tmp_path: Path, app: dict, objects: list[dict], extra: dict[str, str] | None = None) -> Path:
    folder = tmp_path / app["metadata"]["name"]
    (folder / "deploy").mkdir(parents=True)
    app = copy.deepcopy(app)
    app["metadata"].pop("namespace", None)
    (folder / "app.yaml").write_text(yaml.safe_dump(app), encoding="utf-8")
    for doc in objects:
        doc = copy.deepcopy(doc)
        if doc["metadata"].get("namespace") == f"app-{app['metadata']['name']}":
            doc["metadata"].pop("namespace")
        if doc["kind"] == "Ingress":
            for rule in doc["spec"]["rules"]:
                rule["host"] = rule["host"].replace(f"{app['metadata']['name']}.apps.{SUFFIX}", "${app_host}")
        (folder / "deploy" / "objects.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    for name, text in (extra or {}).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


@pytest.mark.parametrize(
    ("app", "obj", "expected"),
    [
        ("web-demo", _job("j", "web-demo", **_PRIVILEGED), "risky.privileged"),
        ("web-demo", _pod("p", "web-demo"), "an app ships only"),
        ("web-demo", _job("j", "web-demo", hostNetwork=True), "risky.hostNetwork"),
        ("web-demo", _job("j", "web-demo", securityContext={}), "risky.root"),
        ("web-demo", _job("j", "web-demo", volumes=[{"name": "h", "hostPath": {"path": "/"}}]), "risky.hostPath"),
        ("notes", _pvc("undeclared", "notes"), "not a volume the app file declares"),
        ("notes", _pvc("cache", "notes"), "carries annotation backup: none"),
        ("web-demo", _ingress("i", "web-demo", f"grafana.{SUFFIX}"), "is ${app_host}"),
        ("web-demo", _object("Service", "s", "web-demo", type="NodePort", ports=[{"port": 80}]), "type NodePort"),
        ("web-demo", _object("Service", "s", "web-demo", type="LoadBalancer", allocateLoadBalancerNodePorts=False, ports=[{"port": 80}]), "not a listen port"),
        ("lan", _object("Service", "s", "lan", type="LoadBalancer", allocateLoadBalancerNodePorts=False,
                        ports=[{"port": 443, "protocol": "TCP", "targetPort": 9090}]), "the number network.ports gives as target"),
        ("lan", _object("Service", "s", "lan", type="LoadBalancer", ports=[{"port": 8443, "protocol": "TCP"}]), "allocateLoadBalancerNodePorts: false"),
        ("web-demo", _object("NetworkPolicy", "n", "web-demo", "networking.k8s.io/v1"), "an app ships only"),
        ("web-demo", _object("IngressRoute", "r", "web-demo", "traefik.io/v1alpha1"), "an app ships only"),
        ("web-demo", {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "flux-system"}}, "lands in namespace app-web-demo"),
        ("web-demo", _job("j", "web-demo", containers=[_container(capabilities={"drop": ["NET_RAW"]})]), "more than Pod Security restricted"),
        ("web-demo", _job("j", "web-demo", securityContext={"runAsNonRoot": True}), "more than Pod Security restricted"),
        ("web-demo", _job("j", "web-demo", containers=[_container(procMount="Unmasked")]), "more than Pod Security restricted"),
        ("web-demo", _job("j", "web-demo", priorityClassName="high"), "refuses another class"),
        ("web-demo", _job("j", "web-demo", containers=[{**_container(), "resources": {
            "requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}}}]), "memory request equals its limit"),
        ("web-demo", _ingress("i", "web-demo", f"web-demo.apps.{SUFFIX}"), "sets ingressClassName: traefik"),
        ("web-demo", {**_ingress("i", "web-demo", f"web-demo.apps.{SUFFIX}", ingressClassName="traefik"),
                      "metadata": {"name": "i", "annotations": {"traefik.ingress.kubernetes.io/router.middlewares": "x"}}},
         "router.middlewares is not allowed"),
        ("web-demo", {**_object("Service", "s", "web-demo", ports=[{"port": 80}]),
                      "metadata": {"name": "s", "annotations": {"traefik.ingress.kubernetes.io/service.sticky.cookie": "true"}}},
         "carries no Traefik annotation"),
        ("notes", {**_pvc("data", "notes"), "spec": {"volumeName": "pv-other"}}, "sets volumeName"),
        ("notes", {**_pvc("data", "notes"), "spec": {"dataSourceRef": {"kind": "PersistentVolumeClaim", "name": "x"}}}, "sets dataSourceRef"),
    ],
    ids=["privileged", "bare-pod", "host-network", "root", "host-path", "undeclared-pvc", "unbacked-without-annotation",
         "host-outside-apps", "nodeport", "loadbalancer-not-listed", "loadbalancer-wrong-target", "loadbalancer-node-ports",
         "networkpolicy", "ingressroute", "other-namespace", "no-drop-all", "no-seccomp", "proc-unmasked", "other-priority-class", "memory-request-under-limit",
         "ingress-no-class", "ingress-middleware", "service-traefik-annotation", "pvc-volume-name", "pvc-data-source-ref"],
)
def test_app_check_refuses_what_admission_refuses(tmp_path: Path, app: str, obj: dict, expected: str) -> None:
    """Rule: tools/app-check fails, on the folder alone, each object the cluster refuses for the
    file's sake (the cases above)."""
    problems = check_folder(_folder(tmp_path, _ENV[app][0], [obj]))
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda folder: (folder / "app.yaml").unlink(), "missing app.yaml"),
        (lambda folder: (folder / "app.yaml").write_text("kind: App\n", encoding="utf-8"), "apiVersion and kind"),
        (lambda folder: folder.rename(folder.parent / "renamed") and None, "folder name"),
        (lambda folder: _edit_app(folder, lambda app: app["spec"].update(colour="blue")), "unknown key 'colour'"),
        (lambda folder: _edit_app(folder, lambda app: app["spec"].pop("volumes")), "spec.volumes: is required"),
        (lambda folder: _edit_app(folder, lambda app: app["spec"]["network"]["web"].update(port="eighty")), "spec.network.web.port: is a integer"),
        (lambda folder: _edit_app(folder, lambda app: app["spec"]["network"]["web"].update(healthPath="health")), "does not match"),
        (lambda folder: _edit_app(folder, lambda app: app["spec"].update(webPort=8080)), "unknown key 'webPort'"),
        (lambda folder: _edit_app(folder, lambda app: app["spec"]["network"].update(egress={"lan": ["all", "nas"]})), "lan is [all] or a list"),
        (lambda folder: _edit_app(folder, lambda app: app["spec"]["network"].update(ports=[{"listen": 80, "protocol": "TCP"}])), "target: is required"),
        (lambda folder: _edit_app(folder, lambda app: app["metadata"].update(namespace="kube-system")), "metadata holds the name only"),
    ],
    ids=["missing", "invalid", "misnamed", "unknown-key", "required-field", "bad-web-port", "health-path-not-a-path",
         "old-web-port-key", "lan-all-and-names", "port-without-target", "namespace-set"],
)
def test_app_check_refuses_a_missing_invalid_or_misnamed_file(tmp_path: Path, change, expected: str) -> None:
    """Rule: nothing of an app deploys without a valid app file whose name is its folder's."""
    folder = tmp_path / "web-demo"
    shutil.copytree(APPS / "web-demo", folder)
    change(folder)
    target = folder if folder.is_dir() else tmp_path / "renamed"
    problems = check_folder(target)
    assert any(expected in problem for problem in problems), problems


def _edit_app(folder: Path, edit) -> None:
    app = yaml.safe_load((folder / "app.yaml").read_text(encoding="utf-8"))
    edit(app)
    (folder / "app.yaml").write_text(yaml.safe_dump(app), encoding="utf-8")


def test_app_name_longer_than_a_namespace_allows_fails(tmp_path: Path) -> None:
    """Rule: app-<name> fits one DNS label, so a name is at most 59 characters."""
    name = "a" * 60
    folder = _folder(tmp_path, {**_app("x"), "metadata": {"name": name}}, [])
    assert any("at most 59 characters" in problem for problem in check_folder(folder))


def test_app_file_and_app_grants_share_their_match_and_variables() -> None:
    """Rule: the two halves of the baseline see the same objects the same way; YAML anchors do
    not cross documents, so the file repeats them and this holds the copies equal."""
    policies = {doc["metadata"]["name"]: doc["spec"] for doc in _docs(POLICIES / "admission.yaml")
                if doc["kind"] == "ValidatingAdmissionPolicy"}
    for field in ("matchConstraints", "matchConditions", "variables", "failurePolicy"):
        assert policies["app-file"][field] == policies["app-grants"][field], field


def _generate_test(tmp_path: Path, policy: Path, resources: list[dict], values: dict, results: list[tuple[str, dict | None]]) -> None:
    """kyverno test of a generate or mutate-existing policy: results are (rule, expected object or None for skip)."""
    work = tmp_path / policy.stem
    work.mkdir(parents=True)
    (work / "resources.yaml").write_text(yaml.safe_dump_all(resources), encoding="utf-8")
    (work / "values.yaml").write_text(
        yaml.safe_dump({"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Value", "metadata": {"name": "v"}, **values}), encoding="utf-8"
    )
    entries = []
    for index, (rule, (trigger, expected)) in enumerate(results):
        entry = {"policy": yaml.safe_load(policy.read_text(encoding="utf-8"))["metadata"]["name"],
                 "rule": rule, "kind": trigger["kind"], "resources": [trigger["metadata"]["name"]],
                 "result": "pass" if expected is not None else "skip"}
        if expected is not None:
            name = f"expected-{index}.yaml"
            (work / name).write_text(yaml.safe_dump(expected), encoding="utf-8")
            entry["generatedResource" if expected["kind"] != "Namespace" else "patchedResources"] = name
        entries.append(entry)
    test = {"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Test", "metadata": {"name": policy.stem},
            "policies": [str(policy)], "resources": ["resources.yaml"], "variables": "values.yaml", "results": entries}
    targets = [doc for doc in resources if doc["kind"] == "Namespace"]
    if targets:
        (work / "targets.yaml").write_text(yaml.safe_dump_all(targets), encoding="utf-8")
        test["targetResources"] = ["targets.yaml"]
        test["resources"] = ["triggers.yaml"]
        (work / "triggers.yaml").write_text(yaml.safe_dump_all([doc for doc in resources if doc["kind"] != "Namespace"]), encoding="utf-8")
    (work / "kyverno-test.yaml").write_text(yaml.safe_dump(test), encoding="utf-8")
    proc = subprocess.run([_kyverno(), "test", str(work), "--remove-color", "--detailed-results"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _network_policy(app: str, name: str, spec: dict) -> dict:
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": f"app-{app}"}, "spec": {"podSelector": {}, **spec}}


def _namespace_notes(apps: dict[str, list[str]]) -> dict:
    """kyverno test values: each App's namespace annotations, as its wiring renders them with
    the env's exception lines for it."""
    return {"policies": [{"name": "app-derive", "resources": [
        {"name": app, "values": {"notes": next(doc for doc in _wiring(app, grants) if doc["kind"] == "Namespace")["metadata"]["annotations"]}}
        for app, grants in apps.items()]}]}


def _quota(app: str, cpu: str, memory: str, lbs: str = "0") -> dict:
    return {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "app", "namespace": f"app-{app}"},
            "spec": {"hard": {"limits.cpu": cpu, "limits.memory": memory, "requests.cpu": cpu, "requests.memory": memory,
                              "services.loadbalancers": lbs, "services.nodeports": "0"}}}


def test_derive_makes_what_the_app_file_asks_for(tmp_path: Path) -> None:
    """Rule: the quota and each allow come from the App's network section (with its env
    override); an allow the file does not ask for is not made; app-to-app opens one side per
    file, so traffic flows only when both list each other; a LAN port opens only when the env
    grants it as lan-port; the backups allow follows the registered credential."""
    notes, photos, web, lan = _app("notes"), _app("photos"), _app("web-demo"), _ENV["lan"][0]
    offline = _app("offline", network={"egress": {"internet": True}},
                   envs={"env-00": {"network": {"egress": {"internet": False}}, "resources": {"memory": "256Mi"}}})
    credential = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "backups-restic-app-notes", "namespace": "platform-backups"}}
    peers = lambda names: {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": [f"app-{n}" for n in names]}]}  # noqa: E731
    web_allow = _network_policy("web-demo", "app-web", {"policyTypes": ["Ingress"], "ingress": [{
        "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                  "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}],
        "ports": [{"port": 8080, "protocol": "TCP"}]}]})
    ports = _network_policy("lan", "app-ports", {"policyTypes": ["Ingress"], "ingress": [{
        "from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
        "ports": [{"port": 8443, "protocol": "TCP"}, {"port": 8080, "protocol": "TCP"}]}]})
    backups = _network_policy("notes", "app-backups", {"policyTypes": ["Egress"], "egress": [{
        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "platform-backups"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "restic-apps"}}}],
        "ports": [{"port": 8000, "protocol": "TCP"}]}]})
    # The same ports as lan, with the env granting one of them, or none.
    partial, ungranted = (_app(name, network=lan["spec"]["network"]) for name in ("lan-partial", "lan-ungranted"))
    partial_ports = _network_policy("lan-partial", "app-ports", {"policyTypes": ["Ingress"], "ingress": [{
        "from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"port": 8080, "protocol": "TCP"}]}]})
    _generate_test(tmp_path, POLICIES / "derive.yaml", [notes, photos, web, lan, offline, credential, partial, ungranted],
                   _namespace_notes({"notes": [], "photos": [], "web-demo": [], "lan": _ENV["lan"][1], "offline": [],
                                     "lan-partial": ["lan-port 443/TCP"], "lan-ungranted": []}), [
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
        # The env override: no internet on env-00 for an app that asks for it elsewhere.
        ("quota", (offline, _quota("offline", "500m", "256Mi"))),
        ("internet", (offline, None)),
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
    """Rule: internet is any port to a public address, never the LAN or the cluster; the whole
    LAN opens only for lan: [all] with the env's lan-all; a named LAN target opens only on the
    ports the env's lan-target lines give it, and only when the App lists it."""
    surfer = _app("surfer", network={"egress": {"internet": True}})
    whole, whole_ungranted = (_app(name, network={"egress": {"lan": ["all"]}}) for name in ("whole", "whole-ungranted"))
    named = _app("named", network={"egress": {"lan": ["nas", "printer"]}})
    named_ungranted = _app("named-ungranted", network={"egress": {"lan": ["nas"]}})
    internet = _network_policy("surfer", "app-internet", {"policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {
        "cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
                                        "10.80.0.0/16", "10.81.0.0/16", "10.200.0.0/24"]}}]}]})
    lan_all = _network_policy("whole", "app-lan-all", {"policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {"cidr": "10.200.0.0/24"}}]}]})
    nas = _network_policy("named", "app-lan", {"policyTypes": ["Egress"], "egress": [
        {"to": [{"ipBlock": {"cidr": "10.200.0.10/32"}}], "ports": [{"port": 137, "protocol": "UDP"}]},
        {"to": [{"ipBlock": {"cidr": "10.200.0.10/32"}}], "ports": [{"port": 445, "protocol": "TCP"}]}]})
    _generate_test(tmp_path, POLICIES / "derive.yaml", [surfer, whole, whole_ungranted, named, named_ungranted],
                   _namespace_notes({"surfer": [], "whole": ["lan-all"], "whole-ungranted": _LAN_TARGETS,
                                     "named": _LAN_TARGETS, "named-ungranted": ["lan-all"]}), [
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
    # What the generated allows let through (kyverno test held them equal to these).
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


def test_uptime_opt_out_reaches_the_namespace_the_uptime_check_reads() -> None:
    """Rule: the uptime check is on unless the env has '<app>: no-uptime-check'; the line reaches
    the app's namespace, where the uptime check reads it."""
    def opt_out(lines: list[str]) -> str:
        return _wiring("notes", lines)[0]["metadata"]["annotations"]["hosting.jarvis.io/no-uptime-check"]

    assert opt_out([]) == "false"
    assert opt_out(["no-uptime-check"]) == "true"


@pytest.mark.parametrize("hosting", [True, False], ids=["hosting-on", "hosting-off"])
def test_privileged_movers_only_for_an_app_with_a_credential(tmp_path: Path, hosting: bool) -> None:
    """Rule: pvc-backup marks an app- namespace for privileged movers only once the env registers
    the app's credential, also for a namespace left after app hosting is switched off; a core
    namespace as before. The rule as the env renders it."""
    from test_app_hosting import render_platform

    enrollment = tmp_path / "pvc-backup.yaml"
    enrollment.write_text(yaml.safe_dump(render_platform(tmp_path / "render", hosting=hosting)[("ClusterPolicy", "pvc-backup")]),
                          encoding="utf-8")
    namespace = lambda name: {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}  # noqa: E731
    marked = lambda name: {**namespace(name), "metadata": {"name": name, "annotations": {"volsync.backube/privileged-movers": "true"}}}  # noqa: E731
    registered, unregistered, core = _pvc("data", "notes"), _pvc("data", "web-demo"), _pvc("data", "x")
    core["metadata"]["namespace"] = "monitoring"
    for claim in (registered, unregistered, core):
        claim["metadata"]["name"] = f"data-{claim['metadata']['namespace']}"
    values = {"policies": [{"name": "pvc-backup", "resources": [
        {"name": registered["metadata"]["name"], "values": {"appCredential": "true"}},
        {"name": unregistered["metadata"]["name"], "values": {"appCredential": "false"}},
        {"name": core["metadata"]["name"], "values": {"appCredential": "false"}},
    ]}]}
    _generate_test(tmp_path, enrollment, [registered, unregistered, core, namespace("app-notes"), namespace("app-web-demo"), namespace("monitoring")],
                   values, [
        ("privileged-movers", (registered, marked("app-notes"))),
        ("privileged-movers", (unregistered, None)),
        ("privileged-movers", (core, marked("monitoring"))),
    ])
