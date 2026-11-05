"""Hosted-app admission as a real API server runs it: the cluster-wide app-file and app-grants
bindings on a throwaway kube-apiserver and etcd on loopback. The kyverno CLI does not resolve a
selector paramRef, so this is the proof of the binding itself.

ci/fetch-test-apiserver downloads the pinned binaries. The client here is a token the test
makes and a loopback URL, with no kubeconfig, so it cannot reach any other cluster.
"""

from __future__ import annotations

import json
import os
import re
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
from support import REPO_ROOT
from test_app_admission import POLICIES, _app, _docs, _pod, _wiring

WORK = REPO_ROOT / ".scratch" / "apiserver"
CRD = REPO_ROOT / "platform" / "app-hosting" / "crd" / "app-crd.yaml"
PRIVILEGED = {"containers": [{"name": "c", "image": "x", "securityContext": {"privileged": True}}]}
PATHS = {
    "Namespace": "/api/v1/namespaces",
    "CustomResourceDefinition": "/apis/apiextensions.k8s.io/v1/customresourcedefinitions",
    "ValidatingAdmissionPolicy": "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies",
    "ValidatingAdmissionPolicyBinding": "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings",
    "App": "/apis/hosting.jarvis.io/v1alpha1/namespaces/{namespace}/apps",
    "Deployment": "/apis/apps/v1/namespaces/{namespace}/deployments",
}


def _binaries() -> Path:
    text = (REPO_ROOT / "tools" / "versions.env").read_text(encoding="utf-8")
    pins = [re.search(rf"^{key}=(\S+)$", text, re.M).group(1) for key in ("kubectl_version", "etcd_version")]
    folder = WORK / "-".join(pins)
    if not ((folder / "kube-apiserver").is_file() and (folder / "etcd").is_file()):
        message = f"no pinned API server in {folder} (run ci/fetch-test-apiserver)"
        if ci_mode():
            pytest.fail(f"{message} — required in CI")
        pytest.skip(message)
    return folder


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
        self.url, self.token = f"https://127.0.0.1:{port}", token
        # Its own self-signed certificate, on loopback. No proxy, whatever the environment says.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        request = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def create(self, doc: dict) -> None:
        path = PATHS[doc["kind"]].format(namespace=doc["metadata"].get("namespace"))
        status, answer = self.request("POST", path, doc)
        assert status == 201, answer

    def refusal(self, doc: dict) -> str:
        """Why the server refuses doc, or '' when it admits it. A dry run keeps nothing, so a
        case can be asked again while the policies load."""
        status, answer = self.request("POST", PATHS[doc["kind"]].format(namespace=doc["metadata"]["namespace"]) + "?dryRun=All", doc)
        return "" if status == 201 else str(json.loads(answer).get("message"))


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


@pytest.fixture(scope="module")
def api():
    folder = _binaries()
    WORK.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=WORK))
    token = secrets.token_urlsafe(24)
    (work / "tokens.csv").write_text(f"{token},test-admin,test-admin,system:masters\n", encoding="utf-8")
    subprocess.run(["openssl", "genrsa", "-out", str(work / "sa.key"), "2048"], check=True, capture_output=True)
    client, peer, secure = _free_port(), _free_port(), _free_port()
    log = (work / "server.log").open("wb")
    env = {key: os.environ[key] for key in ("PATH",) if key in os.environ}
    servers = [
        subprocess.Popen([str(folder / "etcd"), "--data-dir", str(work / "etcd"), "--unsafe-no-fsync",
                          "--listen-client-urls", f"http://127.0.0.1:{client}", "--advertise-client-urls", f"http://127.0.0.1:{client}",
                          "--listen-peer-urls", f"http://127.0.0.1:{peer}", "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer}",
                          "--initial-cluster", f"default=http://127.0.0.1:{peer}"], stdout=log, stderr=subprocess.STDOUT, env=env),
        subprocess.Popen([str(folder / "kube-apiserver"), "--etcd-servers", f"http://127.0.0.1:{client}",
                          "--bind-address", "127.0.0.1", "--advertise-address", "127.0.0.1", "--secure-port", str(secure),
                          "--cert-dir", str(work / "certs"), "--token-auth-file", str(work / "tokens.csv"),
                          "--authorization-mode", "RBAC", "--anonymous-auth=false", "--allow-privileged=true",
                          "--service-cluster-ip-range", "10.0.0.0/24", "--disable-admission-plugins", "ServiceAccount",
                          "--service-account-issuer", f"https://127.0.0.1:{secure}", "--service-account-key-file", str(work / "sa.key"),
                          "--service-account-signing-key-file", str(work / "sa.key")], stdout=log, stderr=subprocess.STDOUT, env=env),
    ]
    try:
        server = Api(secure, token)

        def ready() -> bool:
            assert all(proc.poll() is None for proc in servers), (work / "server.log").read_text(encoding="utf-8")[-4000:]
            try:
                return server.request("GET", "/readyz")[0] == 200
            except OSError:
                return False

        _eventually(ready, "the API server to be ready", seconds=120)
        server.create(_docs(CRD)[0])
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
        apps = [_app("wired"), _app("granted", risky={"privileged": True}),
                _app("annotation-gone", risky={"privileged": True}), _app("upgraded")]

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
