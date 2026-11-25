"""Hosted-app admission as a real API server runs it: the cluster-wide app-file and app-grants
bindings on a throwaway kube-apiserver and etcd on loopback. The kyverno CLI does not resolve a
selector paramRef, so this is the proof of the binding itself.

apiserver.py starts the pinned binaries. The client here is a token the test
makes and a loopback URL, with no kubeconfig, so it cannot reach any other cluster.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from apiserver import Api as _Api
from apiserver import eventually, serve, token

from jarvis.apps.app_file import stand_in_crd
from support import POLICIES, REPO_ROOT, SUFFIX, app_doc, app_job, app_pod, app_route, app_wiring, yaml_docs

VOLSYNC = "system:serviceaccount:volsync:volsync"
SCHEDULER = "system:kube-scheduler"
BINDER = "system:serviceaccount:kube-system:persistent-volume-binder"
PVC_PROTECTION = "system:serviceaccount:kube-system:pvc-protection-controller"
TENANT_NOTES = "system:serviceaccount:flux-system:app-notes"
TENANT_OWN = "system:serviceaccount:flux-system:app-own"
RS_CONTROLLER = "system:serviceaccount:kube-system:replicaset-controller"
REVOKER = "system:serviceaccount:platform-app-hosting:app-grant-revoke"
SELECTED_NODE = "volume.kubernetes.io/selected-node"
PROTECTION, HOLD = "kubernetes.io/pvc-protection", "example.test/hold"
VOLSYNC_OWNER = {"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource", "name": "data",
                 "uid": "a1111111-1111-1111-1111-111111111111"}

APPS = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "apps"
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
    "HTTPRoute": "/apis/gateway.networking.k8s.io/v1/namespaces/{namespace}/httproutes",
    "NetworkPolicy": "/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
    "PersistentVolumeClaim": "/api/v1/namespaces/{namespace}/persistentvolumeclaims",
}


def _denied(policy: str, message: str, binding: str = "") -> str:
    """A policy's refusal as the API server gives it, through binding (default: the policy's own)."""
    return f"ValidatingAdmissionPolicy '{policy}' with binding '{binding or policy}' denied request: {message}"


NO_APP = _denied("app-file", "failed to configure binding: no params found for policy binding with `Deny` parameterNotFoundAction")
UNDECLARED = _denied("app-file", "every PVC (and StatefulSet claim template, whose claims the StatefulSet controller names "
                     "<volume>-<set>-<n>) is a volume the app file declares; one declared backup: false carries annotation "
                     "backup: none, one declared backup: true does not")
ASK = ("grant: anything beyond Pod Security restricted (privileged mode, host PID or IPC, sysctls, allowPrivilegeEscalation "
       "not false, capabilities not dropping ALL or adding more than NET_BIND_SERVICE, a seccomp profile not RuntimeDefault "
       "or Localhost, procMount Unmasked, AppArmor unconfined, SELinux options beyond a container type) needs the app file's "
       "risky.privileged")
GRANT = "grant: anything beyond Pod Security restricted (see app-file) needs the env's grant privileged"
FILE_HOSTS = _denied("app-file", "grant: an app's HTTPRoute hostname is one of its web entries': <name>[-<entry>].apps.<dns "
                     "suffix>, or a custom hostname the entry asks for")
GRANT_HOSTS = _denied("app-grants", "grant: an app's HTTPRoute names its hostnames, each one label under apps.<dns suffix> or "
                      "a custom hostname the env grants as 'hostname <name>'")


class Api(_Api):
    def create(self, doc: dict, *, as_user: str | None = None) -> dict:
        status, answer = self.request("POST", _collection(doc), doc, as_user=as_user)
        assert status == 201, answer
        return json.loads(answer)

    def get(self, doc: dict) -> dict | None:
        """doc as the server holds it, or None once it is gone."""
        status, answer = self.request("GET", _path(doc))
        assert status in (200, 404), answer
        return json.loads(answer) if status == 200 else None

    def update(self, doc: dict, *, as_user: str | None = None) -> None:
        status, answer = self.request("PUT", _path(doc), doc, as_user=as_user)
        assert status == 200, answer

    def delete(self, doc: dict) -> None:
        status, answer = self.request("DELETE", _path(doc))
        assert status == 200, answer

    def refusal(self, doc: dict, *, as_user: str | None = None, method: str = "POST") -> str:
        """Why the server refuses doc's CREATE (or PUT) in a dry run, which keeps nothing: the causes
        it gives, each with its field if it names one; '' when it admits doc."""
        path = _collection(doc) if method == "POST" else _path(doc)
        status, answer = self.request(method, f"{path}?dryRun=All", doc, as_user=as_user)
        if status in (200, 201):
            return ""
        body = json.loads(answer)
        causes = body.get("details", {}).get("causes", [])
        return "\n".join(f"{cause['field']}: {cause['message']}" if "field" in cause else cause["message"]
                         for cause in causes) or body["message"]

    def names(self, kind: str, namespace: str) -> set[str]:
        status, answer = self.request("GET", PATHS[kind].format(namespace=namespace))
        assert status == 200, answer
        return {item["metadata"]["name"] for item in json.loads(answer)["items"]}


def _collection(doc: dict) -> str:
    return PATHS[doc["kind"]].format(namespace=doc["metadata"].get("namespace"))


def _path(doc: dict) -> str:
    return f"{_collection(doc)}/{doc['metadata']['name']}"


def _pvc(namespace: str = "app-notes", owner: dict | None = None, **metadata: object) -> dict:
    """A 1Gi claim in namespace, owned by owner if given, metadata's keys set; with no name the server names it."""
    if owner:
        metadata["ownerReferences"] = [owner]
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"generateName": "claim-", "namespace": namespace, **metadata},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}


def _deployment(app: str, **pod: object) -> dict:
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": f"app-{app}"},
        "spec": {"selector": {"matchLabels": {"app": "web"}},
                 "template": {"metadata": {"labels": {"app": "web"}}, "spec": app_pod("t", app, **pod)["spec"]}},
    }


def _namespace(app: str, grants: list[str], *, without: str = "") -> dict:
    """The namespace the app's wiring renders, less one annotation if asked."""
    namespace = next(doc for doc in app_wiring(app, grants) if doc["kind"] == "Namespace")
    namespace["metadata"]["annotations"].pop(without, None)
    return namespace


def _old_binding(policy: str, app: str, params: dict) -> dict:
    """A per-app binding as earlier wiring wrote it; prune stays off, so a live env keeps it."""
    return {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": f"{policy}-{app}"},
            "spec": {"policyName": policy, "validationActions": ["Deny"], "paramRef": {**params, "parameterNotFoundAction": "Deny"},
                     "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": f"app-{app}"}}}}}


@pytest.fixture(scope="module")
def api():
    admin, revoke_token = token(), token()
    with serve({admin: "test-admin,test-admin,system:masters", revoke_token: f"{REVOKER},app-grant-revoke"}) as (port, work):
        server = Api(port, admin)
        server.account = work / "account"
        server.account.mkdir()
        (server.account / "token").write_text(revoke_token, encoding="utf-8")
        shutil.copy(work / "certs" / "apiserver.crt", server.account / "ca.crt")
        server.create(yaml_docs(CRD)[0])
        # k3s's Gateway API CRDs stand in schemaless: the policies read only the route's fields.
        server.create(stand_in_crd("gateway.networking.k8s.io", "v1", "HTTPRoute"))
        server.create(yaml_docs(CRD.parents[1] / "priority.yaml")[0])
        for doc in yaml_docs(GRANT_REVOKE):
            if doc["kind"] in ("ClusterRole", "ClusterRoleBinding"):
                server.create(doc)
        # The identities the cases write as: admission, not RBAC, is what they meet.
        server.create({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": "writers"},
                       "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
                       "subjects": [{"kind": "User", "name": user, "apiGroup": "rbac.authorization.k8s.io"} for user in
                                    (VOLSYNC, SCHEDULER, BINDER, PVC_PROTECTION, TENANT_NOTES, TENANT_OWN, RS_CONTROLLER)]})
        # The old per-app bindings, then the policies, app-identity last: once it refuses a namespace, the server holds
        # every one before it. The grants binding names a ConfigMap that does not exist, as one the operator has
        # deleted would not.
        policies = sorted(yaml_docs(POLICIES / "admission.yaml"), key=lambda doc: doc["metadata"]["name"] == "app-identity")
        for doc in (_old_binding("app-grants", "upgraded", {"name": "app-upgraded-grants", "namespace": "flux-system"}),
                    _old_binding("app-file", "upgraded", {"name": "upgraded", "namespace": "app-upgraded"}), *policies):
            server.create(doc)
        unlabelled = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "app-unlabelled"}}
        identity = _denied("app-identity", "a namespace is named app-<name> exactly when it carries label hosting.jarvis.io/app: <name>")
        eventually(lambda: server.refusal(unlabelled) == identity, "the policies to be in effect")
        for namespace in (_namespace("wired", []), _namespace("no-app", []), _namespace("granted", ["privileged"]),
                          _namespace("annotation-gone", ["privileged"], without="hosting.jarvis.io/privileged"),
                          _namespace("upgraded", []), _namespace("notes", []), _namespace("own", [])):
            server.create(namespace)

        def served() -> bool:
            return all(server.request("GET", PATHS[kind].format(namespace="app-wired"))[0] == 200 for kind in ("App", "HTTPRoute"))

        eventually(served, "the App and HTTPRoute CRDs to be served")
        for app in (app_doc(APPS, "wired"), app_doc(APPS, "granted", risky={"privileged": True}),
                    app_doc(APPS, "annotation-gone", risky={"privileged": True}), app_doc(APPS, "upgraded"), app_doc(APPS, "notes"),
                    app_doc(APPS, "own", network={"web": [{"service": "own", "port": 80, "ownRoute": True}]})):
            server.create(app)
        # app-file's informer gets the Apps in the order they were made: once app-own (the last) admits a workload, it holds them all.
        eventually(lambda: not server.refusal(_deployment("own")), "app-file to read every App")
        yield server


def test_a_labelled_namespace_with_no_app_refuses_what_a_wired_one_admits(api: Api) -> None:
    """Rule: with no per-app binding at all, an App in the object's own namespace admits a
    workload, and the same workload in a labelled namespace with no App is refused."""
    assert api.refusal(_deployment("no-app")) == NO_APP
    assert api.refusal(_deployment("wired")) == ""


def test_a_risky_item_needs_the_namespace_annotation(api: Api) -> None:
    """Rule: with the App asking for privileged, the namespace's annotation decides: refused
    when it is missing, admitted when the wiring wrote the env's grant."""
    assert api.refusal(_deployment("annotation-gone", **PRIVILEGED)) == _denied("app-grants", GRANT)
    assert api.refusal(_deployment("granted", **PRIVILEGED)) == ""


@pytest.mark.parametrize(("name", "app", "hostnames", "user", "refusals"), [
    pytest.param("app-web", "notes", [f"notes.apps.{SUFFIX}"], None, {""}, id="core-route"),
    pytest.param("app-web", "notes", [f"notes.apps.{SUFFIX}"], TENANT_NOTES, {_denied("app-file", (
        "an app ships an HTTPRoute only for a web entry marked ownRoute; core makes every other entry's route"))}, id="stray-route"),
    pytest.param("own", "own", [f"own.apps.{SUFFIX}"], TENANT_OWN, {""}, id="own-route"),
    pytest.param("core-host", "notes", [f"grafana.{SUFFIX}"], None, {FILE_HOSTS, GRANT_HOSTS}, id="core-host"),
    pytest.param("other-app-host", "notes", [f"wired.apps.{SUFFIX}"], None, {FILE_HOSTS}, id="other-app-host"),
    pytest.param("no-such-entry", "notes", [f"notes-admin.apps.{SUFFIX}"], None, {FILE_HOSTS}, id="no-such-entry"),
    pytest.param("deeper-host", "notes", [f"x.notes.apps.{SUFFIX}"], None, {FILE_HOSTS, GRANT_HOSTS}, id="deeper-host"),
    pytest.param("two-labels", "notes", [f"notes.apps.x.apps.{SUFFIX}"], None, {GRANT_HOSTS}, id="two-labels-under-the-apps-domain"),
    pytest.param("look-alike-host", "notes", [f"notes.apps.{SUFFIX}.evil.example"], None, {GRANT_HOSTS}, id="look-alike-host"),
    pytest.param("second-host", "notes", [f"notes.apps.{SUFFIX}", f"wired.apps.{SUFFIX}"], None, {FILE_HOSTS}, id="second-host"),
    pytest.param("no-hostnames", "notes", [], None, {GRANT_HOSTS}, id="no-hostnames"),
])
def test_an_apps_routes_name_only_its_web_entries_hostnames(api: Api, name: str, app: str, hostnames: list[str],
                                                            user: str | None, refusals: set[str]) -> None:
    """Rule: a route in an app's namespace names its hostnames, each a web entry's <name>[-<entry>].apps.<suffix> or a custom
    one an entry asks for and the env grants; core's route (app-web, as app-derive names it) passes, an app's own only for ownRoute."""
    assert api.refusal(app_route(name, app, *hostnames), as_user=user) in refusals


def test_an_apps_lan_egress_is_all_or_target_names(api: Api) -> None:
    """Rule: the App CRD's own CEL rule, which the app check's kubeconform does not run:
    network.egress.lan is [all] or a list of target names, never both."""
    app = app_doc(APPS, "wired", network={"web": [{"service": "web", "port": 80}], "egress": {"lan": ["all", "nas"]}})
    assert api.refusal(app) == "spec.network.egress.lan: Invalid value: lan is [all] or a list of target names"


def test_old_per_app_bindings_a_live_env_keeps_change_nothing(api: Api) -> None:
    """Rule: the old bindings Flux leaves behind neither widen nor break admission: a good
    workload passes and a risky one fails, with their grants ConfigMap gone."""
    risky = {_denied(policy, message, binding) for policy, message in (("app-file", ASK), ("app-grants", GRANT))
             for binding in (policy, f"{policy}-upgraded")}
    assert api.refusal(_deployment("upgraded", **PRIVILEGED)) in risky
    assert api.refusal(_deployment("upgraded")) == ""


def _claim(api: Api, owner: dict | None, *finalizers: str, deleted: bool = True) -> dict:
    """An undeclared claim in app-notes as the VolSync controller made it, live. Deleted, it stays
    Terminating on the PVC protection finalizer the API server added: no controller runs here."""
    claim = api.create(_pvc(owner=owner, finalizers=list(finalizers)), as_user=VOLSYNC)
    if deleted:
        api.delete(claim)
        claim = api.get(claim)
        assert "deletionTimestamp" in claim["metadata"] and claim["metadata"]["finalizers"] == [*finalizers, PROTECTION], claim
    return claim


@pytest.mark.parametrize(("user", "owner", "metadata", "spec", "refusal"), [
    pytest.param(SCHEDULER, VOLSYNC_OWNER, {"annotations": {SELECTED_NODE: "worker-1"}}, {}, "", id="scheduler-on-a-volsync-claim"),
    pytest.param(SCHEDULER, {**VOLSYNC_OWNER, "kind": "ReplicationDestination"}, {"annotations": {SELECTED_NODE: "worker-1"}}, {},
                 "", id="scheduler-on-a-volsync-restore-claim"),
    pytest.param(SCHEDULER, None, {"annotations": {SELECTED_NODE: "worker-1"}}, {}, UNDECLARED, id="scheduler-on-another-claim"),
    pytest.param(BINDER, VOLSYNC_OWNER, {}, {"volumeName": "pv-volsync-1"}, "", id="binder-on-a-volsync-claim"),
    pytest.param(BINDER, None, {}, {"volumeName": "pv-other-1"}, UNDECLARED, id="binder-on-another-claim"),
    pytest.param(TENANT_NOTES, None, {"ownerReferences": [VOLSYNC_OWNER]}, {}, UNDECLARED, id="tenant-forging-a-volsync-owner"),
])
def test_scheduler_prebind_update_on_volsync_cache_pvc(api: Api, user: str, owner: dict | None, metadata: dict, spec: dict,
                                                       refusal: str) -> None:
    """Rule: on UPDATE only, the scheduler may add volume.kubernetes.io/selected-node and the binder set volumeName on an
    undeclared VolSync claim (kubernetes v1.36.5 binder.go:450→565); on another claim, as the tenant or as a CREATE, not."""
    live, made = _claim(api, owner, deleted=False), _pvc(owner=owner)
    for claim in (live, made):
        claim["metadata"].update(metadata)
        claim["spec"].update(spec)
    assert api.refusal(live, as_user=user, method="PUT") == refusal
    assert api.refusal(made, as_user=user) == UNDECLARED


def test_a_terminating_volsync_cache_pvc_can_finish_deleting(api: Api) -> None:
    """Rule: a write that removes only kubernetes.io/pvc-protection from a terminating VolSync claim passes, whoever
    makes it, so its delete finishes, or waits on its other finalizers with nothing left to release."""
    claim = _claim(api, VOLSYNC_OWNER)
    claim["metadata"]["finalizers"] = []
    assert api.refusal(claim, as_user=TENANT_NOTES, method="PUT") == ""
    api.update(claim, as_user=PVC_PROTECTION)
    assert api.get(claim) is None

    held = _claim(api, VOLSYNC_OWNER, HOLD)
    held["metadata"]["finalizers"] = [HOLD]
    api.update(held, as_user=TENANT_NOTES)
    assert api.refusal(api.get(held), as_user=TENANT_NOTES, method="PUT") == UNDECLARED


@pytest.mark.parametrize(("owner", "deleted", "finalizers", "metadata", "spec"), [
    pytest.param(VOLSYNC_OWNER, False, (), {"finalizers": []}, {}, id="not-terminating"),
    pytest.param(None, True, (), {"finalizers": []}, {}, id="no-owner"),
    pytest.param(None, True, (), {"finalizers": [], "ownerReferences": [VOLSYNC_OWNER]}, {}, id="owner-forged-in-the-write"),
    pytest.param({"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "uid": "a7777777-7777-7777-7777-777777777777"},
                 True, (), {"finalizers": []}, {}, id="another-owner"),
    pytest.param({**VOLSYNC_OWNER, "apiVersion": "volsync.example/v1alpha1"}, True, (), {"finalizers": []}, {}, id="look-alike-owner"),
    pytest.param({**VOLSYNC_OWNER, "kind": "ReplicationSchedule"}, True, (), {"finalizers": []}, {}, id="volsync-owner-of-another-kind"),
    pytest.param(VOLSYNC_OWNER, True, (), {"finalizers": [], "labels": {"seeded": "x"}}, {}, id="labels-changed"),
    pytest.param(VOLSYNC_OWNER, True, (), {"finalizers": [], "annotations": {"seeded": "x"}}, {}, id="annotations-changed"),
    pytest.param(VOLSYNC_OWNER, True, (), {"finalizers": [], "ownerReferences": [{**VOLSYNC_OWNER, "name": "other"}]}, {},
                 id="owner-changed"),
    pytest.param(VOLSYNC_OWNER, True, (), {"finalizers": []}, {"volumeName": "pv-seeded"}, id="volume-named"),
    pytest.param(VOLSYNC_OWNER, True, (HOLD,), {"finalizers": [PROTECTION]}, {}, id="another-finalizer-removed"),
    pytest.param(VOLSYNC_OWNER, True, (HOLD,), {"finalizers": []}, {}, id="both-finalizers-removed"),
])
def test_only_the_finalizer_on_a_terminating_volsync_claim_is_released(api: Api, owner: dict | None, deleted: bool,
                                                                       finalizers: tuple[str, ...], metadata: dict,
                                                                       spec: dict) -> None:
    """Rule: the carve-out needs a claim already terminating, VolSync-owned before the write, and a
    write that removes that one finalizer and changes nothing else. Each seeded write fails."""
    claim = _claim(api, owner, *finalizers, deleted=deleted)
    claim["metadata"].update(metadata)
    claim["spec"].update(spec)
    assert api.refusal(claim, as_user=TENANT_NOTES, method="PUT") == UNDECLARED


# Asked for and granted at first, then revoked.
REVOCABLE = ["privileged", "hostname custom.example.test", "lan-port 8443/TCP", "lan-all", "lan-target nas 10.200.0.10/32 445/TCP"]
ASKS = {"risky": {"privileged": True}, "volumes": [{"name": "data", "backup": False}],
        "network": {"web": [{"service": "web", "port": 80, "ownRoute": True, "hostnames": ["custom.example.test"]}],
                    "ports": [{"listen": 8443, "target": 8443, "protocol": "TCP"}]}}


def _running(api: Api, app: str, grants: list[str], docs: list[dict], **asks: object) -> None:
    """app's namespace with the env's grants and its App asking for asks, then docs, once admission admits the first."""
    api.create(_namespace(app, grants))
    api.create(app_doc(APPS, app, **asks))
    eventually(lambda: not api.refusal(docs[0]), f"the App and grants to admit {app}'s objects")
    for doc in docs:
        api.create(doc)


def _wire_revoker(api: Api, app: str, role: str = "") -> dict:
    """The binding the app's wiring holds (to role instead, if given), once the reconciler's account can use it."""
    binding = yaml_docs(WIRING_BINDING)[0]
    binding["metadata"]["namespace"] = f"app-{app}"
    binding["roleRef"]["name"] = role or binding["roleRef"]["name"]
    api.create(binding)
    paths = ("/api/v1/namespaces", f"/api/v1/namespaces/app-{app}/pods?limit=1")
    eventually(lambda: all(api.request("GET", path, as_user=REVOKER)[0] == 200 for path in paths),
               f"the reconciler's rights in app-{app}")
    return binding


def _reconcile(api: Api, *, succeeds: bool = True) -> str:
    """One run of the script the CronJob runs, as its account, against this server. Its output."""
    script = next(doc for doc in yaml_docs(GRANT_REVOKE) if doc["kind"] == "ConfigMap")["data"]["revoke.py"]
    env = {"PATH": os.environ["PATH"], "KUBERNETES_SERVICE_HOST": "127.0.0.1", "KUBERNETES_SERVICE_PORT": str(api.port),
           "SERVICE_ACCOUNT_DIR": str(api.account)}
    run = subprocess.run(["python3", "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert (run.returncode == 0) == succeeds, run.stdout + run.stderr
    return run.stdout


def _grant(api: Api, app: str, grants: list[str]) -> None:
    """What the wiring writes when the env's exception lines for the app become grants."""
    wanted = _namespace(app, grants)
    namespace = api.get(wanted)
    namespace["metadata"]["annotations"].update(wanted["metadata"]["annotations"])
    api.update(namespace)


def _lan_policy(namespace: str, name: str, **spec: object) -> dict:
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name, "namespace": namespace},
            "spec": {"podSelector": {}, **spec}}


def test_a_revoked_grant_stops_what_runs_on_it_and_keeps_the_data(api: Api) -> None:
    """Rule: once the env drops the exception lines, a run stops what runs on them and closes the LAN allows, keeping the
    workload, a clean pod, VolSync's mover and the volume; the replacement waits for the grant; a second run changes nothing."""
    app, ns = "revoked", "app-revoked"
    replicas = [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "web-1", "uid": "1", "controller": True}]
    risky, replacement = (app_pod(name, app, **PRIVILEGED) for name in ("web-1-a", "web-1-b"))
    risky["metadata"]["ownerReferences"] = replacement["metadata"]["ownerReferences"] = replicas
    mover = app_job("volsync-src-data", app, restartPolicy="Never", **PRIVILEGED)
    mover["metadata"]["ownerReferences"] = [VOLSYNC_OWNER]
    task_pod = app_pod("task-pod", app, owner_job=True, **PRIVILEGED)
    task_pod["metadata"]["ownerReferences"][0]["name"] = "task"
    # Named as MutatingPolicy app-lan-name leaves it; this API server runs no Kyverno.
    service = {"apiVersion": "v1", "kind": "Service",
               "metadata": {"name": "lan", "namespace": ns, "annotations": {"coredns.io/hostname": f"{app}.apps.{SUFFIX}"}},
               "spec": {"type": "LoadBalancer", "allocateLoadBalancerNodePorts": False, "selector": {"a": "b"},
                        "ports": [{"port": 8443, "targetPort": 8443, "protocol": "TCP"}]}}
    lan_range = [{"ipBlock": {"cidr": "10.200.0.0/24"}}]
    _running(api, app, REVOCABLE, [
        _deployment(app, **PRIVILEGED), risky, app_pod("clean", app), _pvc(ns, name="data", annotations={"backup": "none"}),
        app_job("task", app, restartPolicy="Never", **PRIVILEGED), mover, app_pod("mover-pod", app, owner_job=True, **PRIVILEGED),
        task_pod, service, app_route("custom", app, "custom.example.test"),
        _lan_policy(ns, "app-web", policyTypes=["Ingress"], ingress=[{"ports": [{"port": 8080, "protocol": "TCP"}]}]),
        _lan_policy(ns, "app-lan-all", policyTypes=["Egress"], egress=[{"to": lan_range}]),
        _lan_policy(ns, "app-lan", policyTypes=["Egress"], egress=[
            {"to": [{"ipBlock": {"cidr": "10.200.0.10/32"}}], "ports": [{"port": 445, "protocol": "TCP"}]}]),
        _lan_policy(ns, "app-ports", policyTypes=["Ingress"], ingress=[{"ports": [{"port": 8443, "protocol": "TCP"}]}]),
    ], **ASKS)
    # Refused by a rule no grant decides, so a run leaves it: only a grant's denial stops anything.
    odd = app_pod("odd-memory", app, containers=[{
        "name": "c", "image": "x", "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}}}])
    assert api.refusal(odd) == _denied(
        "app-file", "a hosted app's container sets a memory limit equal to its memory request (or a limit alone)")
    api.create(odd, as_user=VOLSYNC)
    _wire_revoker(api, app)
    kinds = ("Pod", "Job", "Service", "HTTPRoute", "NetworkPolicy", "Deployment", "PersistentVolumeClaim")
    held = lambda: {kind: api.names(kind, ns) for kind in kinds}  # noqa: E731
    kept = held()

    assert "stopping" not in (out := _reconcile(api)) and "closing" not in out, out
    assert held() == kept
    _grant(api, app, [])
    eventually(lambda: api.refusal(replacement, as_user=RS_CONTROLLER) == _denied("app-grants", GRANT),
               "the revoke to refuse the replacement")
    assert "stopping" in (out := _reconcile(api)) and "closing" in out, out
    assert held() == {**kept, "Pod": {"clean", "mover-pod", "odd-memory"}, "Job": {"volsync-src-data"}, "Service": set(),
                      "HTTPRoute": set(), "NetworkPolicy": {"app-web"}}
    assert "stopping" not in (out := _reconcile(api)) and "closing" not in out, out

    _grant(api, app, REVOCABLE)
    eventually(lambda: not api.refusal(replacement, as_user=RS_CONTROLLER), "the restored grant to admit the replacement")


def test_an_app_cannot_keep_a_grant_by_dropping_its_ask(api: Api) -> None:
    """Rule: a risky item needs both the ask and the grant, and a denial from either stops the
    running pod. Dropping the ask first, so only app-file refuses, keeps nothing."""
    pod = app_pod("web", "retracted", **PRIVILEGED)
    _running(api, "retracted", ["privileged"], [pod], risky={"privileged": True})
    _wire_revoker(api, "retracted")
    app = api.get(app_doc(APPS, "retracted"))
    del app["spec"]["risky"]
    api.update(app)
    eventually(lambda: api.refusal({**pod, "metadata": {"name": "next", "namespace": "app-retracted"}}) == _denied("app-file", ASK),
               "app-file to refuse")
    _reconcile(api)
    assert api.names("Pod", "app-retracted") == set()


@pytest.mark.parametrize(("name", "field", "value", "message"), [
    pytest.param("mover-look-alike", "ownerReferences", [VOLSYNC_OWNER], "an app's Job names no owner", id="a-volsync-owner"),
    pytest.param("held", "finalizers", [HOLD], "an app adds no finalizer to its Jobs, Services and HTTPRoutes", id="a-finalizer"),
])
def test_a_tenants_job_names_no_owner_and_adds_no_finalizer(api: Api, name: str, field: str, value: list, message: str) -> None:
    """Rule: the reconciler leaves a Job that a VolSync object owns, and a finalizer would keep one it
    deletes in place, so app-tenant refuses a tenant's Job with either."""
    job = app_job(name, "notes", restartPolicy="Never")
    assert api.refusal(job, as_user=TENANT_NOTES) == ""
    job["metadata"][field] = value
    assert api.refusal(job, as_user=TENANT_NOTES) == _denied("app-tenant", message)


def test_a_pods_own_grace_period_does_not_outlast_the_revoke(api: Api) -> None:
    """Rule: a stopped pod gets the reconciler's grace period, not the one its manifest asks for,
    also when its deletion has already begun on the long one, as garbage collection's does."""
    decade = 315360000
    pods = [app_pod(name, "slow", nodeName="node-1", terminationGracePeriodSeconds=decade, **PRIVILEGED) for name in ("fresh", "begun")]
    _running(api, "slow", ["privileged"], pods, risky={"privileged": True})
    _wire_revoker(api, "slow")
    api.delete(pods[1])
    _grant(api, "slow", [])
    eventually(lambda: api.refusal({**pods[0], "metadata": {"name": "next", "namespace": "app-slow"}}) == _denied("app-grants", GRANT),
               "the revoke to refuse")
    _reconcile(api)
    assert [api.get(pod)["metadata"]["deletionGracePeriodSeconds"] for pod in pods] == [30, 30]


def test_a_deleted_app_keeps_nothing_running(api: Api) -> None:
    """Rule: with the App gone app-file refuses for no params. The server reports one denial in no set order, so that one stops
    the pod too, or deleting the App could hide a revoke; the grant stays here, so app-file is the only refusal."""
    pod = app_pod("web", "app-gone", **PRIVILEGED)
    _running(api, "app-gone", ["privileged"], [pod], risky={"privileged": True})
    _wire_revoker(api, "app-gone")
    api.delete(app_doc(APPS, "app-gone"))
    eventually(lambda: api.refusal({**pod, "metadata": {"name": "next", "namespace": "app-app-gone"}}) == NO_APP,
               "the bindings to refuse")
    _reconcile(api)
    assert api.names("Pod", "app-app-gone") == set()


def test_a_probe_with_no_policy_answer_fails_the_run(api: Api) -> None:
    """Rule: a run that cannot ask the API server about an object reports failure; it neither
    stops the object nor passes as healthy."""
    api.create({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "revoke-test-read-only"},
                "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["get", "list"]}]})
    _running(api, "unasked", [], [app_pod("web", "unasked")])
    binding = _wire_revoker(api, "unasked", "revoke-test-read-only")
    out = _reconcile(api, succeeds=False)
    assert ('app-unasked: PATCH /api/v1/namespaces/app-unasked/pods/web: 403 pods "web" is forbidden: User '
            f'"{REVOKER}" cannot patch resource "pods" in API group "" in the namespace "app-unasked"') in out.splitlines(), out
    api.delete(binding)
    eventually(lambda: api.request("GET", "/api/v1/namespaces/app-unasked/pods?limit=1", as_user=REVOKER)[0] == 403,
               "the binding to go")
    assert "skipping app-unasked: not a listed app" in _reconcile(api).splitlines()
