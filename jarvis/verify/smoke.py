"""Short-lived probes for jarvis verify --smoke: each makes something, checks it, and removes it.

The standing checks read the cluster. A probe proves a capability by using it once. The ones that
make objects work in namespace platform-smoke, which the run creates first and deletes at the end
(a namespace left by an interrupted run is deleted first); the admission probe is a server-side
dry run, which leaves nothing.

  platform     a Certificate from ClusterIssuer internal-ca becomes Ready
  app-dns      a new Ingress name answers at App DNS's address, with Traefik's address
  app-hosting  a copy of a running pod of a listed app is admitted, and the same copy with one
               risky item the env does not grant that app is refused for that grant
  <add-on>     the probes an add-on's verify.py registers with registry.register_smoke
"""

from __future__ import annotations

import copy
import json
import secrets
import time
from collections.abc import Callable

from jarvis.apps import values as _apps
from jarvis.env.runtime import json_object, reason
from jarvis.verify.common import CheckError, CheckWarning, Context, items
from jarvis.verify.platform import traefik_address
from jarvis.verify.registry import Check, register_smoke

NAMESPACE = "platform-smoke"
OWNER_LABEL = "jarvis.io/owner"
OWNER = "jarvis-verify-smoke"
ISSUER = "internal-ca"
APP_DNS_NAMESPACE = "platform-app-dns"
WAIT_SECONDS = 180
POLL_SECONDS = 5

# Kept as module names so the tests can replace them.
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


def _answers(server: str, name: str) -> list[str]:
    """The A records server gives for name; empty when it has none (yet)."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [server]
    resolver.lifetime = 5
    try:
        return sorted(record.address for record in resolver.resolve(name, "A"))
    except dns.exception.DNSException:
        return []


_resolve: Callable[[str, str], list[str]] = _answers


def _token() -> str:
    return secrets.token_hex(3)


def _apply(ctx: Context, body: dict, what: str) -> None:
    proc = ctx.kubectl(["apply", "-f", "-"], input=json.dumps(body))
    if proc.returncode:
        raise CheckError(f"{what}: {reason(proc)}")


def _until(what: str, done: Callable[[], bool]) -> None:
    deadline = _clock() + WAIT_SECONDS
    while not done():
        if _clock() >= deadline:
            raise CheckError(f"{what} within {WAIT_SECONDS} s")
        _sleep(POLL_SECONDS)


def _namespace_owner(ctx: Context) -> str | None:
    """Who owns platform-smoke: its owner label, "" when unlabelled, None when it does not exist."""
    proc = ctx.kubectl(["get", "namespace", NAMESPACE, "-o", "json"])
    if proc.returncode:
        if "NotFound" in (proc.stderr or ""):
            return None
        raise CheckError(f"read namespace {NAMESPACE}: {reason(proc)}")
    labels = (json_object(proc, f"namespace {NAMESPACE}").get("metadata") or {}).get("labels") or {}
    return str(labels.get(OWNER_LABEL, ""))


def _remove_namespace(ctx: Context) -> None:
    proc = ctx.kubectl(["delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=false"])
    if proc.returncode:
        raise CheckError(f"delete namespace {NAMESPACE}: {reason(proc)}")
    _until(f"namespace {NAMESPACE} was not gone", lambda: _namespace_owner(ctx) is None)


def open_scratch(ctx: Context) -> None:
    owner = _namespace_owner(ctx)
    if owner not in (None, OWNER):
        raise CheckError(f"namespace {NAMESPACE} exists and is not jarvis verify's (label {OWNER_LABEL}); nothing was changed")
    if owner == OWNER:
        _remove_namespace(ctx)
    body = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": {OWNER_LABEL: OWNER}}}
    _apply(ctx, body, f"create namespace {NAMESPACE}")


def close_scratch(ctx: Context) -> None:
    if _namespace_owner(ctx) == OWNER:
        _remove_namespace(ctx)


def _ready(ctx: Context, kind: str, name: str) -> bool:
    proc = ctx.kubectl(["get", kind, name, "-n", NAMESPACE, "-o", "json"])
    if proc.returncode:
        raise CheckError(f"read {kind} {NAMESPACE}/{name}: {reason(proc)}")
    conditions = (json_object(proc, f"{kind} {name}").get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions if isinstance(c, dict))


def probe_certificate(ctx: Context) -> None:
    name = f"smoke-{_token()}"
    body = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": "smoke", "namespace": NAMESPACE},
        "spec": {
            "secretName": "smoke-tls",
            "dnsNames": [f"{name}.{ctx.values['dns_suffix']}"],
            "issuerRef": {"name": ISSUER, "kind": "ClusterIssuer"},
        },
    }
    _apply(ctx, body, "create Certificate smoke")
    _until(f"Certificate {NAMESPACE}/smoke from {ISSUER} was not Ready", lambda: _ready(ctx, "certificate", "smoke"))


def _app_dns_address(ctx: Context) -> str:
    services = items(ctx.kubectl(["get", "services", "-n", APP_DNS_NAMESPACE, "-o", "json"]), f"list {APP_DNS_NAMESPACE} services")
    for service in services:
        if (service.get("spec") or {}).get("type") != "LoadBalancer":
            continue
        for entry in ((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []:
            if isinstance(entry, dict) and entry.get("ip"):
                return str(entry["ip"])
    raise CheckError(f"App DNS has no LoadBalancer address in {APP_DNS_NAMESPACE}")


def probe_app_dns(ctx: Context) -> None:
    host = f"smoke-{_token()}.{ctx.values['dns_suffix']}"
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "smoke", "namespace": NAMESPACE},
        "spec": {"ports": [{"name": "http", "port": 80}]},
    }
    route = {"pathType": "Prefix", "path": "/", "backend": {"service": {"name": "smoke", "port": {"number": 80}}}}
    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "smoke", "namespace": NAMESPACE},
        "spec": {"ingressClassName": "traefik", "rules": [{"host": host, "http": {"paths": [route]}}]},
    }
    _apply(ctx, service, "create Service smoke")
    _apply(ctx, ingress, "create Ingress smoke")
    server, expected = _app_dns_address(ctx), traefik_address(ctx.kubectl)
    _until(f"App DNS ({server}) did not answer {host} with Traefik's address {expected}", lambda: expected in _resolve(server, host))


# One risky item each, and the env exception that grants it (core/feature/app-hosting, Config).
def _privileged(spec: dict) -> None:
    # The API server rejects privileged with allowPrivilegeEscalation false before any policy
    # sees it; with both true the pod is valid, so only admission can refuse it.
    spec["containers"][0].setdefault("securityContext", {}).update(privileged=True, allowPrivilegeEscalation=True)


def _host_network(spec: dict) -> None:
    spec["hostNetwork"] = True


def _host_path(spec: dict) -> None:
    spec.setdefault("volumes", []).append({"name": "smoke-host", "hostPath": {"path": "/tmp"}})


RISKY = (("privileged", _privileged), ("host-network", _host_network), ("host-path", _host_path))


def _granted(data: dict, app: str) -> set[str]:
    words = set()
    for line in data.get("exceptions") or []:
        if isinstance(line, dict) and isinstance(line.get(app), str):
            words.add(line[app].split()[0])
    return words


def _running_pod(ctx: Context, app: str) -> dict | None:
    """A Running pod a Deployment or StatefulSet made: no Job's, no backup mover's."""
    namespace = f"app-{app}"
    for pod in items(ctx.kubectl(["get", "pods", "-n", namespace, "-o", "json"]), f"list {namespace} pods"):
        owners = {o.get("kind") for o in (pod.get("metadata") or {}).get("ownerReferences") or [] if isinstance(o, dict)}
        if (pod.get("status") or {}).get("phase") == "Running" and owners & {"ReplicaSet", "StatefulSet"}:
            return pod
    return None


def _copy(pod: dict, name: str) -> dict:
    meta = pod.get("metadata") or {}
    spec = copy.deepcopy(pod.get("spec") or {})
    spec.pop("nodeName", None)
    labels = {key: value for key, value in (meta.get("labels") or {}).items() if key != "pod-template-hash"}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": meta.get("namespace"), "labels": labels, "annotations": dict(meta.get("annotations") or {})},
        "spec": spec,
    }


def _dry_run(ctx: Context, body: dict):
    return ctx.kubectl(["create", "--dry-run=server", "-o", "name", "-f", "-"], input=json.dumps(body))


def probe_admission(ctx: Context) -> None:
    data, errors = _apps.load_values(ctx.repo_root / "envs" / ctx.env)
    if errors:
        raise CheckError(f"values: {'; '.join(errors)}")
    for app in _apps.app_names(data):
        pod = _running_pod(ctx, app)
        if pod is None:
            continue
        risky = next(((grant, change) for grant, change in RISKY if grant not in _granted(data, app)), None)
        if risky is None:
            raise CheckWarning(f"app {app} holds every risky grant; no refusal to prove")
        grant, change = risky
        plain = _copy(pod, f"smoke-{_token()}")
        proc = _dry_run(ctx, plain)
        if proc.returncode:
            raise CheckError(f"a copy of running pod app-{app}/{pod['metadata'].get('name')} was refused: {reason(proc)}")
        bad = copy.deepcopy(plain)
        change(bad["spec"])
        proc = _dry_run(ctx, bad)
        if not proc.returncode:
            raise CheckError(f"app-{app} admitted a pod with {grant}, which the env does not grant it")
        if "grant:" not in (proc.stderr or ""):
            raise CheckError(f"app-{app} refused the {grant} pod, but not for the grant: {reason(proc)}")
        return
    raise CheckWarning("no listed app has a running pod to copy")


@register_smoke("platform")
def platform_probes(ctx: Context) -> list[Check]:
    return [(10, f"smoke: Certificate from {ISSUER} Ready", lambda: probe_certificate(ctx))]


@register_smoke("app-dns")
def app_dns_probes(ctx: Context) -> list[Check]:
    return [(20, "smoke: a new Ingress name answers at App DNS", lambda: probe_app_dns(ctx))]


@register_smoke("apps")
def admission_probes(ctx: Context) -> list[Check]:
    return [(30, "smoke: app admission admits a running pod's copy and refuses an ungranted risk", lambda: probe_admission(ctx))]
