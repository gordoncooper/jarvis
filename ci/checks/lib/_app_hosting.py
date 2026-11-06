"""Each hosted app an env lists gets core's whole wiring, unchanged (CONTRACT: platform/app-hosting/CONTRACT.md).

For each env with a flux/ tree, on each app wiring Kustomization (app-<name>-wiring), this
builds its path with its postBuild values and checks what the cluster gets:

- The wiring is for a hosted app on the apps list in values/switches.yaml, and the env lists no app
  without one.
- Namespace app-<name>, labelled hosting.jarvis.io/app: <name>, kept by prune: disabled, its
  grant annotations equal to the env's exception lines for the app, and its host annotation
  <name>.apps.<dns suffix>.
- The network baseline: default-deny (both directions, no allow), cluster-dns and
  same-namespace, and LimitRange app, all in app-<name>.
- The tenant flux-system/app-<name> holds one Role in app-<name>, on the kinds an app ships
  only, bound to it alone. The one other binding there gives the grant reconciler its role.
- The app's two Kustomizations run as that tenant, target app-<name>, read only the app's
  own sources, decrypt nothing and take no substitution source, only app_host.
- Nothing else: the wiring's kinds are these.

And for an env that lists an app: core's policies hold the cluster-wide bindings app-file (the
App in the object's own namespace, refusing when there is none) and app-grants, each on every
namespace labelled hosting.jarvis.io/app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from _manifests import flux_paths, kustomize_build, namespace_of, postbuild_values, ref_of, substitute

import _apps  # tools/lib, which schema.env (through _manifests) puts on the path
from _app_file import SHIPPED_KINDS
from schema.env import CORE_SOURCE_DIR

FLUX_NAMESPACE = "flux-system"
_PRUNE = "kustomize.toolkit.fluxcd.io/prune"
POLICIES = f"{CORE_SOURCE_DIR}/platform/app-hosting/policies"
_KINDS = frozenset(
    {
        "Namespace",
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "NetworkPolicy",
        "LimitRange",
        "GitRepository",
        "Kustomization",
    }
)
GRANT_REVOKE = "app-grant-revoke"
_REVOKE_ROLE = {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": GRANT_REVOKE}
_REVOKE_SUBJECTS = [{"kind": "ServiceAccount", "name": GRANT_REVOKE, "namespace": "platform-app-hosting"}]
_LABELLED = {"namespaceSelector": {"matchExpressions": [{"key": "hosting.jarvis.io/app", "operator": "Exists"}]}}
_BINDINGS = {
    "app-file": {"policyName": "app-file", "validationActions": ["Deny"], "matchResources": _LABELLED,
                 "paramRef": {"selector": {}, "parameterNotFoundAction": "Deny"}},
    "app-grants": {"policyName": "app-grants", "validationActions": ["Deny"], "matchResources": _LABELLED},
}
_KUSTOMIZATION_FIELDS = frozenset(
    {"interval", "retryInterval", "path", "prune", "serviceAccountName", "targetNamespace", "sourceRef", "dependsOn", "postBuild"}
)
# Role groups and resources an app may hold, from the kinds it ships.
_PLURALS = {
    "ConfigMap": "configmaps", "Secret": "secrets", "Service": "services", "PersistentVolumeClaim": "persistentvolumeclaims",
    "Deployment": "deployments", "StatefulSet": "statefulsets", "Job": "jobs", "CronJob": "cronjobs", "Ingress": "ingresses",
    "PodDisruptionBudget": "poddisruptionbudgets", "HorizontalPodAutoscaler": "horizontalpodautoscalers",
}
ROLE_RESOURCES = frozenset({(group, _PLURALS[kind]) for group, kind in SHIPPED_KINDS} | {("hosting.jarvis.io", "apps")})


@dataclass(frozen=True)
class AppHostingViolation:
    path: str
    message: str


def scan_app_hosting(env_root: Path | str) -> list[AppHostingViolation]:
    root = Path(env_root).resolve()
    violations: list[AppHostingViolation] = []
    for flux_dir in sorted(path for path in root.glob("envs/*/flux") if path.is_dir()):
        env_rel = flux_dir.relative_to(root).as_posix()
        violations.extend(AppHostingViolation(env_rel, problem) for problem in _env_problems(root, env_rel))
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[AppHostingViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _env_problems(root: Path, env_rel: str) -> Iterator[str]:
    env_docs, error = kustomize_build(root, env_rel)
    if error:
        yield error
        return
    data, errors = _apps.load_values((root / env_rel).parent)
    if errors:
        return  # env-schema reports the file
    listed = _apps.app_names(data)
    cluster_vars = next(
        (doc.get("data") or {} for doc in env_docs if doc.get("kind") == "ConfigMap" and _name(doc) == "cluster-vars"), {}
    )
    wired: set[str] = set()
    if listed:
        # From core's folder: the example env's platform-configs stage builds only the core list.
        policies, error = kustomize_build(root, POLICIES)
        yield from [error] if error else binding_problems(policies)
    for path in flux_paths(root, env_rel, env_docs):
        name = _name(path.kustomization)
        if path.parent is not None or not (name.startswith("app-") and name.endswith("-wiring")):
            continue
        app = name.removeprefix("app-").removesuffix("-wiring")
        wired.add(app)
        if app not in listed:
            yield f"{ref_of(path.kustomization)}: wires app {app}, which the apps list in values/switches.yaml does not hold"
        if path.error:
            yield f"{ref_of(path.kustomization)}: {path.error}"
            continue
        docs = substitute(path.docs, postbuild_values(path.kustomization, env_docs))
        grants = _apps.grant_annotations(data, app, cluster_vars.get("dns_suffix", ""))
        yield from (f"app {app}: {problem}" for problem in wiring_problems(app, docs, grants))
    for app in listed:
        if app not in wired:
            yield f"the apps list in values/switches.yaml holds {app}, and the flux tree has no {_apps.wiring_name(app)}"


def binding_problems(docs: list[dict]) -> Iterator[str]:
    found = {_name(doc): doc for doc in docs if doc.get("kind") == "ValidatingAdmissionPolicyBinding"}
    for name, want in _BINDINGS.items():
        if name not in found:
            yield f"has no ValidatingAdmissionPolicyBinding {name}"
        elif found[name].get("spec") != want:
            yield f"{ref_of(found[name])}: binds {name} to every labelled namespace, refusing where params are missing"


def wiring_problems(app: str, docs: list[dict], grants: dict[str, str]) -> Iterator[str]:
    namespace, tenant = f"app-{app}", f"app-{app}"
    found = {(str(doc.get("kind")), namespace_of(doc), _name(doc)): doc for doc in docs}

    def need(kind: str, ns: str, name: str) -> dict | None:
        doc = found.get((kind, ns, name))
        if doc is None:
            problems.append(f"has no {kind} {ns}/{name}")
        return doc

    problems: list[str] = []
    for doc in docs:
        if doc.get("kind") not in _KINDS:
            problems.append(f"{ref_of(doc)}: an app's wiring holds no {doc.get('kind')}")
    ns = next((doc for doc in docs if doc.get("kind") == "Namespace"), None)
    if ns is None or _name(ns) != namespace:
        problems.append(f"declares no Namespace {namespace}")
    else:
        meta = ns.get("metadata") or {}
        if (meta.get("labels") or {}).get("hosting.jarvis.io/app") != app:
            problems.append(f"Namespace {namespace}: label hosting.jarvis.io/app is {app}")
        annotations = meta.get("annotations") or {}
        if annotations.get(_PRUNE) != "disabled":
            problems.append(f"Namespace {namespace}: lacks {_PRUNE}: disabled, so dropping the app could delete its volumes")
        # app-grants admits and app-derive opens the LAN from these, so they are the env's grants exactly.
        for key, value in grants.items():
            if annotations.get(key) != value:
                problems.append(f"Namespace {namespace}: annotation {key} is not the env's grant {value!r}")
    deny = found.get(("NetworkPolicy", namespace, "default-deny"))
    deny_spec = (deny or {}).get("spec") or {}
    if deny is None or deny_spec != {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}:
        problems.append(f"NetworkPolicy {namespace}/default-deny selects every pod and denies both directions, with no allow")
    for name in ("cluster-dns", "same-namespace"):
        need("NetworkPolicy", namespace, name)
    need("LimitRange", namespace, "app")
    problems.extend(_tenant_problems(app, docs, namespace, tenant))
    yield from problems


def _tenant_problems(app: str, docs: list[dict], namespace: str, tenant: str) -> Iterator[str]:
    accounts = [doc for doc in docs if doc.get("kind") == "ServiceAccount"]
    if [(namespace_of(doc), _name(doc)) for doc in accounts] != [(FLUX_NAMESPACE, tenant)]:
        yield f"declares ServiceAccounts {[ref_of(doc) for doc in accounts]}; exactly one, its tenant {FLUX_NAMESPACE}/{tenant}"
    for doc in docs:
        kind = doc.get("kind")
        if kind == "Role":
            if namespace_of(doc) != namespace:
                yield f"{ref_of(doc)}: a tenant Role lives in {namespace} only"
            for rule in doc.get("rules") or []:
                held = {(group, resource) for group in rule.get("apiGroups") or [] for resource in rule.get("resources") or []}
                extra = sorted(f"{group or 'core'}/{resource}" for group, resource in held - ROLE_RESOURCES)
                if extra:
                    yield f"{ref_of(doc)}: grants {', '.join(extra)}, which an app does not ship"
                if "*" in (rule.get("verbs") or []):
                    yield f"{ref_of(doc)}: grants every verb"
        elif kind == "RoleBinding" and _name(doc) == GRANT_REVOKE:
            if namespace_of(doc) != namespace or doc.get("roleRef") != _REVOKE_ROLE or doc.get("subjects") != _REVOKE_SUBJECTS:
                yield f"{ref_of(doc)}: binds ClusterRole {GRANT_REVOKE} to platform-app-hosting/{GRANT_REVOKE}, in {namespace} only"
        elif kind == "RoleBinding":
            if namespace_of(doc) != namespace or (doc.get("roleRef") or {}).get("kind") != "Role":
                yield f"{ref_of(doc)}: binds a Role in {namespace} only"
            if doc.get("subjects") != [{"kind": "ServiceAccount", "name": tenant, "namespace": FLUX_NAMESPACE}]:
                yield f"{ref_of(doc)}: binds only {FLUX_NAMESPACE}/{tenant}"
        elif kind == "Kustomization":
            yield from (f"{ref_of(doc)}: {problem}" for problem in _kustomization_problems(app, doc, namespace, tenant))
        elif kind == "GitRepository" and _name(doc) not in {f"app-{app}", f"app-{app}-file"}:
            yield f"{ref_of(doc)}: an app's sources are app-{app} and app-{app}-file"


def _kustomization_problems(app: str, doc: dict, namespace: str, tenant: str) -> Iterator[str]:
    spec = doc.get("spec") or {}
    if namespace_of(doc) != FLUX_NAMESPACE or _name(doc) not in {f"app-{app}", f"app-{app}-file"}:
        yield f"an app's Kustomizations are {FLUX_NAMESPACE}/app-{app} and app-{app}-file"
    if spec.get("serviceAccountName") != tenant:
        yield f"runs as {spec.get('serviceAccountName')!r}, not the app's tenant {tenant}"
    if spec.get("targetNamespace") != namespace:
        yield f"targets {spec.get('targetNamespace')!r}; an app's objects land in {namespace} only"
    source = spec.get("sourceRef") or {}
    if source.get("kind") != "GitRepository" or source.get("name") != _name(doc):
        yield f"reads {source}; an app's Kustomization reads its own source, {_name(doc)}"
    extra = sorted(set(spec) - _KUSTOMIZATION_FIELDS)
    if extra:
        yield f"sets {', '.join(extra)}; an app's Kustomization decrypts nothing and rewrites nothing"
    post_build = spec.get("postBuild") or {}
    if set(post_build) - {"substitute"} or set(post_build.get("substitute") or {}) - {"app_host"}:
        yield "takes only the inline postBuild value app_host, and no substitution source"


def _name(doc: dict) -> str:
    metadata = doc.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) else ""
