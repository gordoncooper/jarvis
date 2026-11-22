"""Short-lived probes for jarvis verify --smoke: each makes something, checks it, and removes it.

The standing checks read the cluster. A probe proves a capability by using it once. The ones that
make objects work in namespace platform-smoke, which the run creates first (labelled core's layer,
platform, as core's own namespaces are) and deletes at the end (a namespace left by an interrupted
run is deleted first); the admission probe is a server-side dry run, which leaves nothing.

  platform     a Certificate from ClusterIssuer internal-ca becomes Ready; a copy of testapp's running
               pod is admitted, and refused by policy images-pinned once unpinned and by Pod
               Security once privileged
  app-dns      a new HTTPRoute on the core door is Accepted there, then its name answers at App DNS's
               address, with Traefik's address
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
GATEWAY_NAMESPACE, GATEWAY_NAME = "platform-gateway", "jarvis"
TESTAPP_NAMESPACE = "platform-testapp"
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


def _until(what: str | Callable[[], str], done: Callable[[], bool]) -> None:
    deadline = _clock() + WAIT_SECONDS
    while not done():
        if _clock() >= deadline:
            raise CheckError(f"{what() if callable(what) else what} within {WAIT_SECONDS} s")
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
    labels = {OWNER_LABEL: OWNER, "jarvis.io/layer": "platform"}
    body = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": labels}}
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


def _accepted(ctx: Context, name: str, seen: list[str]) -> bool:
    """Whether the route reads Accepted=True on the core door; seen keeps the last other answer."""
    proc = ctx.kubectl(["get", "httproute", name, "-n", NAMESPACE, "-o", "json"])
    if proc.returncode:
        raise CheckError(f"read HTTPRoute {NAMESPACE}/{name}: {reason(proc)}")
    for parent in (json_object(proc, f"HTTPRoute {name}").get("status") or {}).get("parents") or []:
        ref = parent.get("parentRef") or {}
        if (ref.get("name"), ref.get("namespace"), ref.get("sectionName")) != (GATEWAY_NAME, GATEWAY_NAMESPACE, "core"):
            continue
        for condition in parent.get("conditions") or []:
            if condition.get("type") == "Accepted":
                if condition.get("status") == "True":
                    return True
                seen[:] = [f"Accepted={condition.get('status')} ({condition.get('reason')})"]
    return False


def probe_app_dns(ctx: Context) -> None:
    host = f"smoke-{_token()}.{ctx.values['dns_suffix']}"
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "smoke", "namespace": NAMESPACE},
        "spec": {"ports": [{"name": "http", "port": 80}]},
    }
    route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": "smoke", "namespace": NAMESPACE},
        "spec": {
            "parentRefs": [{"name": GATEWAY_NAME, "namespace": GATEWAY_NAMESPACE, "sectionName": "core"}],
            "hostnames": [host],
            "rules": [{"backendRefs": [{"name": "smoke", "port": 80}]}],
        },
    }
    _apply(ctx, service, "create Service smoke")
    _apply(ctx, route, "create HTTPRoute smoke")
    # App DNS answers a route's name whether or not the door serves it, so the name proves the
    # path only once the core door has accepted the route.
    seen = ["no status for it"]
    door = f"{GATEWAY_NAMESPACE}/{GATEWAY_NAME} section core"
    _until(lambda: f"HTTPRoute {NAMESPACE}/smoke was not Accepted on {door} (last: {seen[0]})", lambda: _accepted(ctx, "smoke", seen))
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
    return {grant.split()[0] for grant in _apps.grants_for(data, app)}


def _running_pod(ctx: Context, namespace: str) -> dict | None:
    """A Running pod a Deployment or StatefulSet made: no Job's, no backup mover's."""
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
        pod = _running_pod(ctx, f"app-{app}")
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


def _unpinned(spec: dict) -> None:
    container = spec["containers"][0]
    container["image"] = container["image"].split("@")[0]


def probe_core_admission(ctx: Context) -> None:
    pod = _running_pod(ctx, TESTAPP_NAMESPACE)
    if pod is None:
        raise CheckError(f"{TESTAPP_NAMESPACE} has no running pod to copy")
    plain = _copy(pod, f"smoke-{_token()}")
    proc = _dry_run(ctx, plain)
    if proc.returncode:
        raise CheckError(f"a copy of running pod {TESTAPP_NAMESPACE}/{pod['metadata'].get('name')} was refused: {reason(proc)}")
    for what, change, because in (("unpinned", _unpinned, "images-pinned"), ("privileged", _privileged, "PodSecurity")):
        bad = copy.deepcopy(plain)
        change(bad["spec"])

        def refused(bad: dict = bad, what: str = what, because: str = because) -> bool:
            proc = _dry_run(ctx, bad)
            if proc.returncode and because not in (proc.stderr or ""):
                raise CheckError(f"{TESTAPP_NAMESPACE} refused the {what} copy, but not for it: {reason(proc)}")
            return proc.returncode != 0

        # Kyverno may still be loading a policy platform-configs has just applied.
        _until(f"{TESTAPP_NAMESPACE} still admitted the {what} copy", refused)


@register_smoke("platform")
def platform_probes(ctx: Context) -> list[Check]:
    return [
        (10, f"smoke: Certificate from {ISSUER} Ready", lambda: probe_certificate(ctx)),
        (15, "smoke: core admission admits testapp's copy and refuses it unpinned or privileged", lambda: probe_core_admission(ctx)),
    ]


@register_smoke("app-dns")
def app_dns_probes(ctx: Context) -> list[Check]:
    return [(20, "smoke: a new HTTPRoute name answers at App DNS", lambda: probe_app_dns(ctx))]


@register_smoke("apps")
def admission_probes(ctx: Context) -> list[Check]:
    return [(30, "smoke: app admission admits a running pod's copy and refuses an ungranted risk", lambda: probe_admission(ctx))]
