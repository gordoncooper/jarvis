"""Hosted-app admission as a real API server runs it: the cluster-wide app-file and app-grants
bindings on a throwaway kube-apiserver and etcd on loopback. The kyverno CLI does not resolve a
selector paramRef, so this is the proof of the binding itself.

ci/fetch-test-apiserver downloads the pinned binaries. The client here is a token the test
makes and a loopback URL, with no kubeconfig, so it cannot reach any other cluster.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import ci_mode
from test_app_admission import POLICIES, _app, _docs, _job, _pod, _wiring

from support import REPO_ROOT

VOLSYNC = "system:serviceaccount:volsync:volsync"
SCHEDULER = "system:kube-scheduler"
BINDER = "system:serviceaccount:kube-system:persistent-volume-binder"
PVC_PROTECTION = "system:serviceaccount:kube-system:pvc-protection-controller"
TENANT_NOTES = "system:serviceaccount:flux-system:app-notes"
RS_CONTROLLER = "system:serviceaccount:kube-system:replicaset-controller"
REVOKER = "system:serviceaccount:platform-app-hosting:app-grant-revoke"
SELECTED_NODE = "volume.kubernetes.io/selected-node"
PVC_PROTECTION_FINALIZER = "kubernetes.io/pvc-protection"

WORK = REPO_ROOT / ".scratch" / "apiserver"
CRD = REPO_ROOT / "core" / "feature" / "app-hosting" / "crd" / "app-crd.yaml"
GRANT_REVOKE = REPO_ROOT / "core" / "feature" / "app-hosting" / "grant-revoke.yaml"
WIRING_BINDING = REPO_ROOT / "core" / "feature" / "app-hosting" / "app" / "grant-revoke.yaml"
PRIVILEGED = {"containers": [{"name": "c", "image": "x", "securityContext": {"privileged": True}}]}
PATHS = {
    "Namespace": "/api/v1/namespaces",
    "CustomResourceDefinition": "/apis/apiextensions.k8s.io/v1/customresourcedefinitions",
    "ValidatingAdmissionPolicy": "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies",
    "ValidatingAdmissionPolicyBinding": "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings",
    "App": "/apis/hosting.jarvis.io/v1alpha1/namespaces/{namespace}/apps",
    "Deployment": "/apis/apps/v1/namespaces/{namespace}/deployments",
    "ClusterRole": "/apis/rbac.authorization.k8s.io/v1/clusterroles",
    "ClusterRoleBinding": "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",
    "PriorityClass": "/apis/scheduling.k8s.io/v1/priorityclasses",
    "RoleBinding": "/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings",
    "Pod": "/api/v1/namespaces/{namespace}/pods",
    "Job": "/apis/batch/v1/namespaces/{namespace}/jobs",
    "Service": "/api/v1/namespaces/{namespace}/services",
    "Ingress": "/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses",
    "NetworkPolicy": "/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
    "PersistentVolumeClaim": "/api/v1/namespaces/{namespace}/persistentvolumeclaims",
}


def _binaries() -> dict[str, str]:
    """kube-apiserver and etcd at their mise.toml pins, which make deps installs on Linux."""
    found = {name: shutil.which(name) for name in ("kube-apiserver", "etcd")}
    missing = sorted(name for name, path in found.items() if path is None)
    if missing:
        message = f"no {' or '.join(missing)} on PATH (make deps installs them on Linux)"
        if ci_mode():
            pytest.fail(f"{message} — required in CI")
        pytest.skip(message)
    return {name: str(path) for name, path in found.items()}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _eventually(check, what: str, seconds: int = 60):
    """check()'s first truthy answer. Fails naming what, once seconds pass."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = check()
        if answer:
            return answer
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for {what}")


class Api:
    def __init__(self, port: int, token: str) -> None:
        self.port, self.url, self.token = port, f"https://127.0.0.1:{port}", token
        # Its own self-signed certificate, on loopback. No proxy, whatever the environment says.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))

    def request(self, method: str, path: str, body: dict | None = None, *, as_user: str | None = None) -> tuple[int, bytes]:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if as_user is not None:
            headers["Impersonate-User"] = as_user
            headers["Impersonate-Group"] = "system:authenticated"
        request = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def create(self, doc: dict) -> None:
        path = PATHS[doc["kind"]]
        if "{namespace}" in path:
            path = path.format(namespace=doc["metadata"].get("namespace"))
        status, answer = self.request("POST", path, doc)
        assert status == 201, answer

    def refusal(self, doc: dict, *, as_user: str | None = None) -> str:
        """Why the server refuses doc, or '' when it admits it. A dry run keeps nothing, so a
        case can be asked again while the policies load."""
        status, answer = self.request(
            "POST", PATHS[doc["kind"]].format(namespace=doc["metadata"]["namespace"]) + "?dryRun=All", doc, as_user=as_user
        )
        return "" if status == 201 else _status_message(answer)

    def create_as(self, doc: dict, user: str) -> None:
        path = PATHS[doc["kind"]].format(namespace=doc["metadata"]["namespace"])
        status, answer = self.request("POST", path, doc, as_user=user)
        assert status == 201, answer

    def get_pvc(self, namespace: str, name: str) -> dict:
        path = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}"
        status, answer = self.request("GET", path)
        assert status == 200, answer
        return json.loads(answer)

    def update_refusal(self, doc: dict, user: str) -> str:
        namespace, name = doc["metadata"]["namespace"], doc["metadata"]["name"]
        path = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}?dryRun=All"
        status, answer = self.request("PUT", path, doc, as_user=user)
        return "" if status == 200 else _status_message(answer)


    def names(self, kind: str, namespace: str) -> set[str]:
        status, answer = self.request("GET", PATHS[kind].format(namespace=namespace))
        assert status == 200, answer
        return {item["metadata"]["name"] for item in json.loads(answer)["items"]}

    def replace(self, path: str, change) -> None:
        status, answer = self.request("GET", path)
        assert status == 200, answer
        doc = json.loads(answer)
        change(doc)
        status, answer = self.request("PUT", path, doc)
        assert status == 200, answer


def _status_message(answer: bytes) -> str:
    try:
        payload = json.loads(answer)
    except json.JSONDecodeError:
        return answer.decode("utf-8", errors="replace")
    return str(payload.get("message") or payload.get("reason") or answer)


def _volsync_owner(name: str = "data", uid: str = "a1111111-1111-1111-1111-111111111111") -> dict:
    return {"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource", "name": name, "uid": uid}


def _pvc(name: str, namespace: str, *, owner: dict | None = None, annotations: dict | None = None) -> dict:
    metadata: dict = {"name": name, "namespace": namespace}
    if owner is not None:
        metadata["ownerReferences"] = [owner]
    if annotations:
        metadata["annotations"] = annotations
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": metadata,
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
    }


def _deployment(app: str, **pod: object) -> dict:
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": f"app-{app}"},
        "spec": {"selector": {"matchLabels": {"app": "web"}},
                 "template": {"metadata": {"labels": {"app": "web"}}, "spec": _pod("t", app, **pod)["spec"]}},
    }


def _namespace(app: str, grants: list[str], *, without: str | None = None) -> dict:
    """The namespace the app's wiring renders, less one annotation if asked."""
    namespace = next(doc for doc in _wiring(app, grants) if doc["kind"] == "Namespace")
    if without:
        del namespace["metadata"]["annotations"][without]
    return namespace


def _old_binding(policy: str, app: str, params: dict) -> dict:
    """A per-app binding as earlier wiring wrote it; prune stays off, so a live env keeps it."""
    return {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": f"{policy}-{app}"},
            "spec": {"policyName": policy, "validationActions": ["Deny"], "paramRef": {**params, "parameterNotFoundAction": "Deny"},
                     "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": f"app-{app}"}}}}}


def _ensure_pvc_rbac(api: Api) -> None:
    """Let controller identities used in UPDATE proofs create and patch PVCs in app namespaces."""
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
        "metadata": {"name": "app-hosting-admission-test-pvc"},
        "rules": [{"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["create", "get", "update", "patch"]},
                  {"apiGroups": [""], "resources": ["pods"], "verbs": ["create"]},
                  {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create"]}],
    }
    api.create(role)
    for subject in (
        {"kind": "User", "name": VOLSYNC, "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "User", "name": SCHEDULER, "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "User", "name": BINDER, "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "User", "name": PVC_PROTECTION, "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "User", "name": TENANT_NOTES, "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "User", "name": RS_CONTROLLER, "apiGroup": "rbac.authorization.k8s.io"},
    ):
        api.create({
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": f"app-hosting-admission-test-pvc-{subject['name'].replace(':', '-').replace('/', '-')}"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role["metadata"]["name"]},
            "subjects": [subject],
        })


@pytest.fixture(scope="module")
def api():
    binaries = _binaries()
    WORK.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=WORK))
    token = secrets.token_urlsafe(24)
    revoke_token = secrets.token_urlsafe(24)
    (work / "tokens.csv").write_text(
        f"{token},test-admin,test-admin,system:masters\n{revoke_token},{REVOKER},app-grant-revoke\n", encoding="utf-8"
    )
    subprocess.run(["openssl", "genrsa", "-out", str(work / "sa.key"), "2048"], check=True, capture_output=True)
    client, peer, secure = _free_port(), _free_port(), _free_port()
    log = (work / "server.log").open("wb")
    env = {key: os.environ[key] for key in ("PATH",) if key in os.environ}
    servers = [
        subprocess.Popen([binaries["etcd"], "--data-dir", str(work / "etcd"), "--unsafe-no-fsync",
                          "--listen-client-urls", f"http://127.0.0.1:{client}", "--advertise-client-urls", f"http://127.0.0.1:{client}",
                          "--listen-peer-urls", f"http://127.0.0.1:{peer}", "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer}",
                          "--initial-cluster", f"default=http://127.0.0.1:{peer}"], stdout=log, stderr=subprocess.STDOUT, env=env),
        subprocess.Popen([binaries["kube-apiserver"], "--etcd-servers", f"http://127.0.0.1:{client}",
                          "--bind-address", "127.0.0.1", "--advertise-address", "127.0.0.1", "--secure-port", str(secure),
                          "--cert-dir", str(work / "certs"), "--token-auth-file", str(work / "tokens.csv"),
                          "--authorization-mode", "RBAC", "--anonymous-auth=false", "--allow-privileged=true",
                          "--service-cluster-ip-range", "10.0.0.0/24", "--disable-admission-plugins", "ServiceAccount",
                          "--service-account-issuer", f"https://127.0.0.1:{secure}", "--service-account-key-file", str(work / "sa.key"),
                          "--service-account-signing-key-file", str(work / "sa.key")], stdout=log, stderr=subprocess.STDOUT, env=env),
    ]
    try:
        server = Api(secure, token)
        server.account = work / "account"
        server.account.mkdir()
        (server.account / "token").write_text(revoke_token, encoding="utf-8")
        server.certs = work / "certs"

        def ready() -> bool:
            assert all(proc.poll() is None for proc in servers), (work / "server.log").read_text(encoding="utf-8")[-4000:]
            try:
                return server.request("GET", "/readyz")[0] == 200
            except OSError:
                return False

        _eventually(ready, "the API server to be ready", seconds=120)
        server.create(_docs(CRD)[0])
        server.create(_docs(CRD.parents[1] / "priority.yaml")[0])
        _ensure_pvc_rbac(server)
        # The old bindings go first: once the cases below see the new ones refuse, the server
        # has loaded these too. The grants one names a ConfigMap that does not exist, as one the
        # operator has deleted would not.
        server.create(_old_binding("app-grants", "upgraded", {"name": "app-upgraded-grants", "namespace": "flux-system"}))
        server.create(_old_binding("app-file", "upgraded", {"name": "upgraded", "namespace": "app-upgraded"}))
        for doc in _docs(POLICIES / "admission.yaml"):
            server.create(doc)
        server.create(_namespace("wired", []))
        server.create(_namespace("no-app", []))
        server.create(_namespace("granted", ["privileged"]))
        server.create(_namespace("annotation-gone", ["privileged"], without="hosting.jarvis.io/privileged"))
        server.create(_namespace("upgraded", []))
        server.create(_namespace("notes", []))
        apps = [_app("wired"), _app("granted", risky={"privileged": True}),
                _app("annotation-gone", risky={"privileged": True}), _app("upgraded"), _app("notes")]

        def served() -> bool:
            return server.request("GET", PATHS["App"].format(namespace="app-wired"))[0] == 200

        _eventually(served, "the App CRD to be served")
        for app in apps:
            server.create(app)
        yield server
    finally:
        for proc in reversed(servers):
            proc.terminate()
            proc.wait(timeout=30)
        log.close()
        shutil.rmtree(work)


def _refused(api: Api, doc: dict, because: str) -> None:
    _eventually(lambda: because in api.refusal(doc), f"{doc['metadata']['namespace']} to refuse for: {because}")


def _admitted(api: Api, doc: dict) -> None:
    reason = api.refusal(doc)
    deadline = time.monotonic() + 30
    while reason and time.monotonic() < deadline:
        time.sleep(0.2)
        reason = api.refusal(doc)
    assert not reason, reason


def test_a_labelled_namespace_with_no_app_refuses_what_a_wired_one_admits(api: Api) -> None:
    """Rule: with no per-app binding at all, an App in the object's own namespace admits a
    workload, and the same workload in a labelled namespace with no App is refused."""
    _refused(api, _deployment("no-app"), "app-file")
    _admitted(api, _deployment("wired"))


def test_a_risky_item_needs_the_namespace_annotation(api: Api) -> None:
    """Rule: with the App asking for privileged, the namespace's annotation decides: refused
    when it is missing, admitted when the wiring wrote the env's grant."""
    _refused(api, _deployment("annotation-gone", **PRIVILEGED), "'<app>: privileged' exception")
    _admitted(api, _deployment("granted", **PRIVILEGED))


def test_old_per_app_bindings_a_live_env_keeps_change_nothing(api: Api) -> None:
    """Rule: the old bindings Flux leaves behind neither widen nor break admission: a good
    workload passes and a risky one fails, with their grants ConfigMap gone."""
    _refused(api, _deployment("upgraded", **PRIVILEGED), "denied request")
    _admitted(api, _deployment("upgraded"))


def test_scheduler_prebind_update_on_volsync_cache_pvc(api: Api) -> None:
    """Rule: on UPDATE only, system:kube-scheduler may add volume.kubernetes.io/selected-node on an
    undeclared VolSync cache PVC (kubernetes v1.36.5 binder.go:450→565); without VolSync provenance
    or as the tenant, the same UPDATE is refused. volumeName UPDATE stays on binder/controller-manager."""
    ns = "app-notes"
    declare_msg = "volume the app file declares"

    # Scheduler PreBind: selected-node on a VolSync-owned undeclared cache claim.
    cache = _pvc("volsync-src-data-cache", ns, owner=_volsync_owner())
    api.create_as(cache, VOLSYNC)
    live = api.get_pvc(ns, cache["metadata"]["name"])
    update = dict(live)
    update.setdefault("metadata", {}).setdefault("annotations", {})[SELECTED_NODE] = "worker-1"
    assert not api.update_refusal(update, SCHEDULER), api.update_refusal(update, SCHEDULER)

    # Scheduler UPDATE without VolSync ownerReference (not provenance).
    plain = _pvc("sched-no-provenance", ns)
    api.create_as(plain, VOLSYNC)
    live = api.get_pvc(ns, plain["metadata"]["name"])
    bad = dict(live)
    bad.setdefault("metadata", {}).setdefault("annotations", {})[SELECTED_NODE] = "worker-1"
    assert declare_msg in api.update_refusal(bad, SCHEDULER)

    # Binder volumeName on VolSync-owned undeclared claim (separate from scheduler path).
    dst = _pvc("volsync-dst-data-cache", ns, owner=_volsync_owner(uid="b2222222-2222-2222-2222-222222222222"))
    api.create_as(dst, VOLSYNC)
    live = api.get_pvc(ns, dst["metadata"]["name"])
    bound = dict(live)
    bound.setdefault("spec", {})["volumeName"] = "pv-volsync-1"
    assert not api.update_refusal(bound, BINDER), api.update_refusal(bound, BINDER)

    # Binder UPDATE without VolSync provenance.
    bind_plain = _pvc("bind-no-provenance", ns)
    api.create_as(bind_plain, VOLSYNC)
    live = api.get_pvc(ns, bind_plain["metadata"]["name"])
    bad_bind = dict(live)
    bad_bind.setdefault("spec", {})["volumeName"] = "pv-other-1"
    assert declare_msg in api.update_refusal(bad_bind, BINDER)

    # Tenant UPDATE forging a VolSync ownerReference on an undeclared claim.
    victim = _pvc("tenant-forged-target", ns)
    api.create_as(victim, VOLSYNC)
    live = api.get_pvc(ns, victim["metadata"]["name"])
    forged = dict(live)
    forged.setdefault("metadata", {})["ownerReferences"] = [_volsync_owner(name="fake", uid="c3333333-3333-3333-3333-333333333333")]
    assert declare_msg in api.update_refusal(forged, TENANT_NOTES)

    # CREATE with scheduler identity does not use the UPDATE carve-out.
    create = _pvc("sched-create-attempt", ns, owner=_volsync_owner(uid="d4444444-4444-4444-4444-444444444444"),
                  annotations={SELECTED_NODE: "worker-1"})
    path = f"/api/v1/namespaces/{ns}/persistentvolumeclaims?dryRun=All"
    status, answer = api.request("POST", path, create, as_user=SCHEDULER)
    assert declare_msg in _status_message(answer)


def _terminating_pvc(api: Api, name: str, *, owner: dict | None = None, hold: bool = False) -> dict:
    """A claim the VolSync controller made, deleted: no controller runs here, so it stays
    Terminating on the finalizer the API server added at CREATE (and hold's, if asked)."""
    doc = _pvc(name, "app-notes", owner=owner)
    if hold:
        doc["metadata"]["finalizers"] = ["example.test/hold"]
    api.create_as(doc, VOLSYNC)
    status, answer = api.request("DELETE", f"/api/v1/namespaces/app-notes/persistentvolumeclaims/{name}")
    assert status == 200, answer
    live = api.get_pvc("app-notes", name)
    assert "deletionTimestamp" in live["metadata"] and PVC_PROTECTION_FINALIZER in live["metadata"]["finalizers"]
    return live


def _without_protection(claim: dict) -> dict:
    doc = json.loads(json.dumps(claim))
    doc["metadata"]["finalizers"] = [f for f in doc["metadata"]["finalizers"] if f != PVC_PROTECTION_FINALIZER]
    return doc


def test_a_terminating_volsync_cache_pvc_can_finish_deleting(api: Api) -> None:
    """Rule: removing only kubernetes.io/pvc-protection from a VolSync-owned undeclared claim that
    is already terminating passes, so its delete finishes; it is keyed on the change, so the
    protection controller's username does not matter and a tenant may make the same write."""
    path = "/api/v1/namespaces/app-notes/persistentvolumeclaims/cache-finishes"
    cache = _terminating_pvc(api, "cache-finishes", owner=_volsync_owner(uid="e5555555-5555-5555-5555-555555555555"))
    stripped = _without_protection(cache)
    assert not api.update_refusal(stripped, TENANT_NOTES), api.update_refusal(stripped, TENANT_NOTES)
    status, answer = api.request("PUT", path, stripped, as_user=PVC_PROTECTION)
    assert status == 200, answer
    assert api.request("GET", path)[0] == 404


def test_only_the_finalizer_on_a_terminating_volsync_claim_is_released(api: Api) -> None:
    """Rule: the carve-out needs a claim already terminating, VolSync-owned before the write, and
    a write that removes that one finalizer and changes nothing else. Each seeded write fails."""
    declare_msg = "volume the app file declares"
    owner = _volsync_owner(uid="f6666666-6666-6666-6666-666666666666")
    tenant = lambda doc: api.update_refusal(doc, TENANT_NOTES)  # noqa: E731

    # A live cache claim: same write, not terminating.
    api.create_as(_pvc("cache-live", "app-notes", owner=owner), VOLSYNC)
    assert declare_msg in tenant(_without_protection(api.get_pvc("app-notes", "cache-live")))

    # Terminating but with no VolSync owner, as it is and with one forged in the same write.
    unowned = _without_protection(_terminating_pvc(api, "cache-unowned"))
    assert declare_msg in tenant(unowned)
    unowned["metadata"]["ownerReferences"] = [owner]
    assert declare_msg in tenant(unowned)

    # Terminating with an owner that is not VolSync's.
    other = {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "uid": "a7777777-7777-7777-7777-777777777777"}
    assert declare_msg in tenant(_without_protection(_terminating_pvc(api, "cache-other-owner", owner=other)))

    cache = _terminating_pvc(api, "cache-seeded", owner=owner)
    for field, value in (("labels", {"seeded": "x"}), ("annotations", {"seeded": "x"}),
                         ("ownerReferences", [{**owner, "name": "other"}])):
        changed = _without_protection(cache)
        changed["metadata"][field] = value
        assert declare_msg in tenant(changed), field
    changed = _without_protection(cache)
    changed["spec"]["volumeName"] = "pv-seeded"
    assert declare_msg in tenant(changed)

    # Other finalizers stay: one removed in the write is refused, with or without this one.
    held = _terminating_pvc(api, "cache-held", owner=owner, hold=True)
    other_gone = json.loads(json.dumps(held))
    other_gone["metadata"]["finalizers"] = [PVC_PROTECTION_FINALIZER]
    assert declare_msg in tenant(other_gone)
    both_gone = json.loads(json.dumps(held))
    both_gone["metadata"]["finalizers"] = []
    assert declare_msg in tenant(both_gone)

    # Removing just this one passes and keeps the other. With it gone there is nothing to release.
    released = _without_protection(held)
    status, answer = api.request("PUT", "/api/v1/namespaces/app-notes/persistentvolumeclaims/cache-held", released, as_user=TENANT_NOTES)
    assert status == 200, answer
    assert declare_msg in tenant(api.get_pvc("app-notes", "cache-held"))


# Asked for and granted at first, then revoked.
REVOCABLE = ["privileged", "hostname custom.example.test", "lan-port 8443/TCP", "lan-all", "lan-target nas 10.200.0.10/32 445/TCP"]
ASKS = {"risky": {"privileged": True, "hostnames": ["custom.example.test"]}, "volumes": [{"name": "data", "backup": False}],
        "network": {"web": {"port": 8080}, "ports": [{"listen": 8443, "target": 8443, "protocol": "TCP"}]}}


def _reconcile(api: Api, *, succeeds: bool = True) -> str:
    """One run of the script the CronJob runs, as its account, against this server. Its output."""
    script = next(doc for doc in _docs(GRANT_REVOKE) if doc["kind"] == "ConfigMap")["data"]["revoke.py"]
    shutil.copy(api.certs / "apiserver.crt", api.account / "ca.crt")
    env = {"PATH": os.environ["PATH"], "KUBERNETES_SERVICE_HOST": "127.0.0.1", "KUBERNETES_SERVICE_PORT": str(api.port),
           "SERVICE_ACCOUNT_DIR": str(api.account)}
    run = subprocess.run(["python3", "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert (run.returncode == 0) == succeeds, run.stdout + run.stderr
    return run.stdout


def _cluster_rights(api: Api) -> None:
    """The revoker's cluster-wide rights, once per server. Each test that runs the reconciler
    asks for them itself, so no test leans on another having run first on its worker."""
    if api.request("GET", PATHS["ClusterRoleBinding"] + "/app-grant-revoke-namespaces")[0] == 404:
        for doc in _docs(GRANT_REVOKE):
            if doc["kind"] in ("ClusterRole", "ClusterRoleBinding"):
                api.create(doc)


def _wire_revoker(api: Api, app: str) -> None:
    """The cluster-wide rights once, and the binding the app's wiring holds."""
    _cluster_rights(api)
    binding = _docs(WIRING_BINDING)[0]
    binding["metadata"]["namespace"] = f"app-{app}"
    api.create(binding)
    _eventually(lambda: f"skipping app-{app}" not in _reconcile(api), f"the reconciler's rights in app-{app}")


def _revoke(api: Api, app: str) -> None:
    """What the wiring does when the env drops the app's exception lines."""
    wanted = _namespace(app, [])["metadata"]["annotations"]
    api.replace(f"/api/v1/namespaces/app-{app}", lambda doc: doc["metadata"]["annotations"].update(wanted))


def _lan_policy(namespace: str, name: str, **spec: object) -> dict:
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name, "namespace": namespace},
            "spec": {"podSelector": {}, **spec}}


def test_a_revoked_grant_stops_what_runs_on_it_and_keeps_the_data(api: Api) -> None:
    """Rule: once the env drops the exception lines, a run stops each running object that needs
    them and closes the LAN allows; the workload, a clean pod, VolSync's mover and the volume stay.
    The controller's replacement is refused until the grant is back. A second run changes nothing."""
    app, ns = "revoked", "app-revoked"
    api.create(_namespace(app, REVOCABLE))
    api.create(_app(app, **ASKS))
    replicas = [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "web-1", "uid": "1", "controller": True}]
    risky = _pod("web-1-a", app, **PRIVILEGED)
    risky["metadata"]["ownerReferences"] = replicas
    replacement = _pod("web-1-b", app, **PRIVILEGED)
    replacement["metadata"]["ownerReferences"] = replicas
    _eventually(lambda: not api.refusal(risky), "the App and grants to admit the pod")
    mover = _job("volsync-src-data", app, restartPolicy="Never", **PRIVILEGED)
    mover["metadata"]["ownerReferences"] = [_volsync_owner()]
    # Refused by a rule no grant decides, so a run leaves it: only a grant's denial stops anything.
    odd = _pod("odd-memory", app, containers=[{
        "name": "c", "image": "x", "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}}}])
    api.create_as(odd, VOLSYNC)
    task_pod = _pod("task-pod", app, owner_job=True, **PRIVILEGED)
    task_pod["metadata"]["ownerReferences"][0]["name"] = "task"
    service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "lan", "namespace": ns},
               "spec": {"type": "LoadBalancer", "allocateLoadBalancerNodePorts": False, "selector": {"a": "b"},
                        "ports": [{"port": 8443, "targetPort": 8443, "protocol": "TCP"}]}}
    backend = {"service": {"name": "lan", "port": {"number": 8443}}}
    ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
               "metadata": {"name": "custom", "namespace": ns, "annotations": {"traefik.ingress.kubernetes.io/router.entrypoints": "websecure"}},
               "spec": {"ingressClassName": "traefik", "rules": [{"host": "custom.example.test", "http": {
                   "paths": [{"path": "/", "pathType": "Prefix", "backend": backend}]}}]}}
    lan_range = [{"ipBlock": {"cidr": "10.200.0.0/24"}}]
    for doc in (_deployment(app, **PRIVILEGED), risky, _pod("clean", app), _pvc("data", ns, annotations={"backup": "none"}),
                _job("task", app, restartPolicy="Never", **PRIVILEGED), mover,
                _pod("mover-pod", app, owner_job=True, **PRIVILEGED), task_pod, service, ingress,
                _lan_policy(ns, "app-web", policyTypes=["Ingress"], ingress=[{"ports": [{"port": 8080, "protocol": "TCP"}]}]),
                _lan_policy(ns, "app-lan-all", policyTypes=["Egress"], egress=[{"to": lan_range}]),
                _lan_policy(ns, "app-lan", policyTypes=["Egress"], egress=[
                    {"to": [{"ipBlock": {"cidr": "10.200.0.10/32"}}], "ports": [{"port": 445, "protocol": "TCP"}]}]),
                _lan_policy(ns, "app-ports", policyTypes=["Ingress"], ingress=[{"ports": [{"port": 8443, "protocol": "TCP"}]}])):
        api.create(doc)
    _wire_revoker(api, app)

    assert "stopping" not in (out := _reconcile(api)) and "closing" not in out, out
    assert {"web-1-a", "clean", "mover-pod", "task-pod", "odd-memory"} == api.names("Pod", ns)

    _revoke(api, app)
    _eventually(lambda: "exception" in api.refusal(replacement, as_user=RS_CONTROLLER), "the revoke to refuse the replacement")
    _reconcile(api)
    assert api.names("Pod", ns) == {"clean", "mover-pod", "odd-memory"}
    assert api.names("Job", ns) == {"volsync-src-data"}
    assert api.names("Service", ns) == api.names("Ingress", ns) == set()
    assert api.names("NetworkPolicy", ns) == {"app-web"}
    assert api.names("Deployment", ns) == {"web"}
    assert api.names("PersistentVolumeClaim", ns) == {"data"}
    assert "stopping" not in (out := _reconcile(api)) and "closing" not in out, out

    restored = _namespace(app, REVOCABLE)["metadata"]["annotations"]
    api.replace(f"/api/v1/namespaces/{ns}", lambda doc: doc["metadata"]["annotations"].update(restored))
    _eventually(lambda: not api.refusal(replacement, as_user=RS_CONTROLLER), "the restored grant to admit the replacement")


def test_an_app_cannot_keep_a_grant_by_dropping_its_ask(api: Api) -> None:
    """Rule: a risky item needs both the ask and the grant, and a denial from either stops the
    running pod. Dropping the ask first, so only app-file refuses, keeps nothing."""
    app, ns = "retracted", "app-retracted"
    api.create(_namespace(app, ["privileged"]))
    api.create(_app(app, risky={"privileged": True}))
    pod = _pod("web", app, **PRIVILEGED)
    _eventually(lambda: not api.refusal(pod), "the App and grant to admit the pod")
    api.create(pod)
    _wire_revoker(api, app)
    path = PATHS["App"].format(namespace=ns) + f"/{app}"
    api.replace(path, lambda doc: doc["spec"].pop("risky"))
    _eventually(lambda: "risky.privileged" in api.refusal({**pod, "metadata": {"name": "next", "namespace": ns}}), "app-file to refuse")
    _reconcile(api)
    assert api.names("Pod", ns) == set()


def test_a_tenant_job_cannot_pose_as_a_volsync_mover(api: Api) -> None:
    """Rule: the reconciler leaves a Job owned by a VolSync object alone, so a tenant's Job names no owner."""
    job = _job("mover-look-alike", "notes", restartPolicy="Never")
    job["metadata"]["ownerReferences"] = [_volsync_owner()]
    _eventually(lambda: "names no owner" in api.refusal(job, as_user=TENANT_NOTES), "app-tenant to refuse the forged owner")
    del job["metadata"]["ownerReferences"]
    assert "names no owner" not in api.refusal(job, as_user=TENANT_NOTES)


def test_a_tenant_adds_no_finalizer_to_what_the_reconciler_deletes(api: Api) -> None:
    """Rule: a finalizer would leave a deleted Job or Ingress in place, so app-tenant refuses one."""
    job = _job("held", "notes", restartPolicy="Never")
    job["metadata"]["finalizers"] = ["example.test/hold"]
    _eventually(lambda: "adds no finalizer" in api.refusal(job, as_user=TENANT_NOTES), "app-tenant to refuse the finalizer")
    del job["metadata"]["finalizers"]
    assert "adds no finalizer" not in api.refusal(job, as_user=TENANT_NOTES)


def test_a_pods_own_grace_period_does_not_outlast_the_revoke(api: Api) -> None:
    """Rule: a stopped pod gets the reconciler's grace period, not the one its manifest asks for,
    also when its deletion has already begun on the long one, as garbage collection's does."""
    app, ns = "slow", "app-slow"
    api.create(_namespace(app, ["privileged"]))
    api.create(_app(app, risky={"privileged": True}))
    decade = 315360000
    pods = [_pod(name, app, nodeName="node-1", terminationGracePeriodSeconds=decade, **PRIVILEGED) for name in ("fresh", "begun")]
    _eventually(lambda: not api.refusal(pods[0]), "the App and grant to admit the pod")
    for pod in pods:
        api.create(pod)
    _wire_revoker(api, app)
    path = PATHS["Pod"].format(namespace=ns)
    assert api.request("DELETE", f"{path}/begun")[0] == 200
    _revoke(api, app)
    _eventually(lambda: "exception" in api.refusal({**pods[0], "metadata": {"name": "next", "namespace": ns}}), "the revoke to refuse")
    _reconcile(api)
    for name in ("fresh", "begun"):
        assert json.loads(api.request("GET", f"{path}/{name}")[1])["metadata"]["deletionGracePeriodSeconds"] == 30


def test_a_deleted_app_keeps_nothing_running(api: Api) -> None:
    """Rule: with the App gone app-file refuses for no params. The server reports one denial and
    promises no order, so that one stops the pod too, or deleting the App could hide a revoke. The
    grant stays here so app-file is the only refusal."""
    app, ns = "app-gone", "app-app-gone"
    api.create(_namespace(app, ["privileged"]))
    api.create(_app(app, risky={"privileged": True}))
    pod = _pod("web", app, **PRIVILEGED)
    _eventually(lambda: not api.refusal(pod), "the App and grant to admit the pod")
    api.create(pod)
    _wire_revoker(api, app)
    assert api.request("DELETE", PATHS["App"].format(namespace=ns) + f"/{app}")[0] == 200
    _eventually(lambda: api.refusal({**pod, "metadata": {"name": "next", "namespace": ns}}), "the bindings to refuse")
    _reconcile(api)
    assert api.names("Pod", ns) == set()


def test_a_probe_with_no_policy_answer_fails_the_run(api: Api) -> None:
    """Rule: a run that cannot ask the API server about an object reports failure; it neither
    stops the object nor passes as healthy."""
    app, ns = "unasked", "app-unasked"
    _cluster_rights(api)
    api.create(_namespace(app, []))
    api.create(_app(app))
    pod = _pod("web", app)
    _eventually(lambda: not api.refusal(pod), "the App to admit the pod")
    api.create(pod)
    api.create({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "revoke-test-read-only"},
                "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["get", "list"]}]})
    binding = _docs(WIRING_BINDING)[0]
    binding["metadata"]["namespace"] = ns
    binding["roleRef"]["name"] = "revoke-test-read-only"
    api.create(binding)
    _eventually(lambda: "skipping app-unasked" not in _reconcile(api, succeeds=False), "the read-only rights to reach the reconciler")
    assert api.names("Pod", ns) == {"web"}
    assert api.request("DELETE", PATHS["RoleBinding"].format(namespace=ns) + "/app-grant-revoke")[0] == 200
    _eventually(lambda: "skipping app-unasked" in _reconcile(api), "the binding to go")
