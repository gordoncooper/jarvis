"""Hosted-app admission, offline: every rule in platform/app-hosting/policies against seeded
good and bad objects, with the pinned kyverno CLI, and tools/app-check on the same objects.

The bindings and the grants ConfigMap are rendered from the real per-app wiring
(platform/app-hosting/app) with the env's values, so the test sees what the cluster gets.
"""

from __future__ import annotations

import copy
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
WIRING = REPO_ROOT / "platform" / "app-hosting" / "app"
SUFFIX = "env-00.example"
TENANT = "system:serviceaccount:flux-system:app-{app}"
VOLSYNC = "system:serviceaccount:volsync:volsync"
JOB_CONTROLLER = "system:serviceaccount:kube-system:job-controller"
CLUSTER_VARS = {"pod_cidr": "10.80.0.0/16", "service_cidr": "10.81.0.0/16", "apps_git_repo_url": "https://example.invalid/apps"}


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
        "spec": {"webPort": 8080, "volumes": []},
    }
    app = copy.deepcopy(app)
    app["spec"].update(spec)
    app["metadata"]["namespace"] = f"app-{name}"
    return app


def _wiring(app: str, grants: list[str]) -> list[dict]:
    """What the app's wiring applies, rendered with the env's exception lines for it."""
    data = {"apps": [app], "exceptions": [{app: grant} for grant in grants]}
    values = {**_apps.substitutes(data, app, CLUSTER_VARS), "dns_suffix": SUFFIX, "jarvis_env": "env-00"}
    docs = [doc for name in ("namespace.yaml", "admission.yaml") for doc in _docs(WIRING / name)]
    return substitute(docs, values)


def _deploy(app: str) -> list[dict]:
    """A fixture app's deploy/ objects, as its Kustomization applies them."""
    docs = [doc for path in sorted((APPS / app / "deploy").glob("*.yaml")) for doc in _docs(path)]
    docs = substitute(docs, {"app_host": f"{app}.apps.{SUFFIX}"})
    for doc in docs:
        doc["metadata"]["namespace"] = f"app-{app}"
    return docs


def _pod(name: str, app: str, *, owner_job: bool = False, **pod: object) -> dict:
    spec = {
        "securityContext": {"runAsNonRoot": True},
        "containers": [{"name": "c", "image": "registry.example/x@sha256:" + "0" * 64,
                        "securityContext": {"allowPrivilegeEscalation": False}}],
        **pod,
    }
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
    "lan": (_app("lan", ports=[{"port": 8443, "protocol": "TCP", "lan": True}]), ["lan-port 8443/TCP"]),
}


def _run(tmp_path: Path, label: str, user: str | None, cases: list[tuple[dict, dict[str, str]]]) -> None:
    """kyverno test on cases: (object, {policy: pass|fail|skip}). Fails with the CLI's table."""
    work = tmp_path / label
    work.mkdir()
    wiring = [doc for app, (_doc, grants) in _ENV.items() for doc in _wiring(app, grants)]
    namespaces = [doc for doc in wiring if doc["kind"] == "Namespace"]
    bindings = [doc for doc in wiring if doc["kind"] == "ValidatingAdmissionPolicyBinding"]
    params = [doc for doc in wiring if doc["kind"] == "ConfigMap"] + [app for app, _ in _ENV.values()]
    (work / "policies.yaml").write_text(
        yaml.safe_dump_all([*_docs(POLICIES / "admission.yaml"), *bindings]), encoding="utf-8"
    )
    (work / "params.yaml").write_text(yaml.safe_dump_all(params), encoding="utf-8")
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
    assert proc.returncode == 0, proc.stdout + proc.stderr


PASS = {"app-file": "pass", "app-grants": "pass"}
FILE_FAILS = {"app-file": "fail"}
GRANT_FAILS = {"app-grants": "fail"}
BOTH_FAIL = {"app-file": "fail", "app-grants": "fail"}


def test_sample_apps_pass_admission(tmp_path: Path) -> None:
    """Rule: the sample apps' objects, the hostPath one with its env line, pass the baseline."""
    cases = [(doc, PASS) for app in ("web-demo", "notes", "photos") for doc in _deploy(app)]
    _run(tmp_path, "samples", None, cases)


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
    <name>.apps.<suffix>; Services are ClusterIP or a granted lan LoadBalancer."""
    lb = {"type": "LoadBalancer", "allocateLoadBalancerNodePorts": False,
          "ports": [{"port": 8443, "protocol": "TCP"}], "selector": {"app": "x"}}
    with_node_ports = {**lb, "allocateLoadBalancerNodePorts": True}
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
        (_object("Service", "lan-node-ports", "lan", **with_node_ports), FILE_FAILS),
        (_object("Service", "nodeport", "web-demo", type="NodePort", ports=[{"port": 80}]), GRANT_FAILS),
        (_object("Service", "external-ips", "web-demo", ports=[{"port": 80}], externalIPs=["192.0.2.1"]), GRANT_FAILS),
    ]
    _run(tmp_path, "objects", None, cases)


def test_an_app_cannot_write_its_own_grants_or_leave_its_namespace(tmp_path: Path) -> None:
    """Rule: the app's identity writes only its shipped kinds in app-<name>. A grants ConfigMap it
    ships is refused in flux-system and ignored in its own namespace (the binding reads
    flux-system), so an exception line in the app's own folder grants nothing."""
    tenant = TENANT.format(app="web-demo")
    local = {"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": "app-web-demo-grants", "namespace": "app-web-demo"}, "data": {"privileged": "true"}}
    forged = copy.deepcopy(local)
    forged["metadata"] = {"name": "forged-grants", "namespace": "flux-system"}
    network = _object("NetworkPolicy", "allow-all", "web-demo", "networking.k8s.io/v1", podSelector={})
    other = _object("ConfigMap", "elsewhere", "notes")
    tenant_ok = {"app-tenant": "pass"}
    tenant_fails = {"app-tenant": "fail"}
    _run(tmp_path, "tenant", tenant, [
        (local, tenant_ok), (forged, tenant_fails), (network, tenant_fails), (other, tenant_fails),
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
        ("web-demo", _object("Service", "s", "web-demo", type="LoadBalancer", allocateLoadBalancerNodePorts=False, ports=[{"port": 80}]), "not a lan port"),
        ("lan", _object("Service", "s", "lan", type="LoadBalancer", ports=[{"port": 8443, "protocol": "TCP"}]), "allocateLoadBalancerNodePorts: false"),
        ("web-demo", _object("NetworkPolicy", "n", "web-demo", "networking.k8s.io/v1"), "an app ships only"),
        ("web-demo", _object("IngressRoute", "r", "web-demo", "traefik.io/v1alpha1"), "an app ships only"),
        ("web-demo", {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "flux-system"}}, "lands in namespace app-web-demo"),
    ],
    ids=["privileged", "bare-pod", "host-network", "root", "host-path", "undeclared-pvc", "unbacked-without-annotation",
         "host-outside-apps", "nodeport", "loadbalancer-not-lan", "loadbalancer-node-ports", "networkpolicy", "ingressroute", "other-namespace"],
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
        (lambda folder: _edit_app(folder, lambda app: app["spec"].update(webPort="eighty")), "webPort is a port number"),
        (lambda folder: _edit_app(folder, lambda app: app["metadata"].update(namespace="kube-system")), "metadata holds the name only"),
    ],
    ids=["missing", "invalid", "misnamed", "unknown-key", "required-field", "bad-web-port", "namespace-set"],
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


def test_derive_makes_what_the_app_file_asks_for(tmp_path: Path) -> None:
    """Rule: the quota and each allow come from the App (with its env override); an allow the
    file does not ask for is not made; app-to-app opens one side per file; the backups allow
    follows the registered credential."""
    notes, photos, web = _app("notes"), _app("photos"), _app("web-demo")
    offline = _app("offline", internet=True, envs={"env-00": {"internet": False, "resources": {"memory": "256Mi"}}})
    credential = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "backups-restic-app-notes", "namespace": "platform-backups"}}
    notes_ns = {"hosting.jarvis.io/env": "env-00", "hosting.jarvis.io/pod-cidr": "10.80.0.0/16", "hosting.jarvis.io/service-cidr": "10.81.0.0/16"}
    rules = [{"name": rule, "values": {"notes": notes_ns}} for rule in ("quota", "internet")]
    quota = lambda app, cpu, memory, lbs="0": {  # noqa: E731
        "apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "app", "namespace": f"app-{app}"},
        "spec": {"hard": {"limits.cpu": cpu, "limits.memory": memory, "requests.cpu": cpu, "requests.memory": memory,
                          "services.loadbalancers": lbs, "services.nodeports": "0"}}}
    peers = lambda names: {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": [f"app-{n}" for n in names]}]}  # noqa: E731
    internet = _network_policy("notes", "app-internet", {"policyTypes": ["Egress"], "egress": [{
        "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                                                         "169.254.0.0/16", "10.80.0.0/16", "10.81.0.0/16"]}}],
        "ports": [{"port": 443, "protocol": "TCP"}]}]})
    web_allow = _network_policy("web-demo", "app-web", {"policyTypes": ["Ingress"], "ingress": [{
        "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                  "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}],
        "ports": [{"port": 8080, "protocol": "TCP"}]}]})
    backups = _network_policy("notes", "app-backups", {"policyTypes": ["Egress"], "egress": [{
        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "platform-backups"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "restic-apps"}}}],
        "ports": [{"port": 8000, "protocol": "TCP"}]}]})
    _generate_test(tmp_path, POLICIES / "derive.yaml", [notes, photos, web, credential],
                   {"policies": [{"name": "app-derive", "rules": rules}]}, [
        ("quota", (notes, quota("notes", "1", "1Gi"))),
        ("internet", (notes, internet)),
        ("talks-to", (notes, _network_policy("notes", "app-talks-to", {"policyTypes": ["Egress"], "egress": [{"to": [{"namespaceSelector": peers(["photos"])}]}]}))),
        ("accepts-from", (photos, _network_policy("photos", "app-accepts-from", {"policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector": peers(["notes"])}]}]}))),
        ("talks-to", (photos, None)),
        ("accepts-from", (notes, None)),
        ("web", (web, web_allow)),
        ("lan", (web, None)),
        ("backups", (credential, backups)),
    ])
    # The env override: no internet on env-00 for an app that asks for it elsewhere.
    _generate_test(tmp_path / "override", POLICIES / "derive.yaml", [offline],
                   {"policies": [{"name": "app-derive", "rules": [{"name": rule, "values": {"notes": notes_ns}} for rule in ("quota", "internet")]}]}, [
        ("quota", (offline, quota("offline", "500m", "256Mi"))),
        ("internet", (offline, None)),
    ])


def test_privileged_movers_only_for_an_app_with_a_credential(tmp_path: Path) -> None:
    """Rule: pvc-backup marks an app- namespace for privileged movers only once the env registers
    the app's credential; a core namespace as before. The rule as an env with app hosting on
    renders it."""
    from test_app_hosting import render_platform

    enrollment = tmp_path / "pvc-backup.yaml"
    enrollment.write_text(yaml.safe_dump(render_platform(tmp_path / "render", hosting=True)[("ClusterPolicy", "pvc-backup")]),
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
