"""Every Flux Kustomization and HelmRelease runs as an identity it was given.

With multi-tenancy lockdown on, kustomize-controller and helm-controller run an
object that names no service account as the `default` account of its
namespace, and refuse a source or dependency in another namespace. For each env
with a flux/ tree, this builds that tree and the path of each Flux
Kustomization in it, then checks:

- Every FluxInstance sets spec.cluster.multitenant: true.
- Every Kustomization and HelmRelease sets spec.serviceAccountName, and the
  env's flux tree declares that ServiceAccount in the object's namespace, or,
  for an add-on's Kustomization, the services stage's path does. The root sync
  applies that tree with the controller's own identity: the root Kustomization
  flux-system/flux-system names kustomize-controller, and its path (the tree
  itself) is not checked as a tenant path.
- Only PLATFORM_KUSTOMIZATIONS, and each hosted app's wiring (app-<name>-wiring
  applying APP_WIRING_PATH from the root source, nothing more), run as
  PLATFORM_SERVICE_ACCOUNT, and of the objects on their paths only those on
  PLATFORM_PATHS. A Kustomization named like a wiring that is not one fails. No other account a Flux
  object runs as is bound by a ClusterRoleBinding, by a RoleBinding in the
  namespace the Flux objects live in, or to cluster-admin, admin or edit.
- No Kustomization's path declares the account it runs as, or a binding for it.
- Only Kustomizations in FLUX_NAMESPACE set spec.decryption: the env key lives
  there, and a tenant namespace never gets it.
- sourceRef, chart sourceRef, chartRef, dependsOn and healthChecks stay in the
  object's namespace. Flux allows a cross-namespace health check; this check
  does not.

Each stage's path is built with its spec.components, and so is the path of each
Kustomization on a stage's path (an add-on's own Kustomizations), one level down.
Deeper levels are not walked: ci/checks/addons forbids Flux objects on an
add-on's own paths, which keeps the decryption rule complete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from _manifests import FluxPath, flux_paths, kustomize_build, namespace_of, postbuild_values, ref_of, substitute
from schema.env import env_flux_dirs

PLATFORM_SERVICE_ACCOUNT = "flux-platform"
FLUX_NAMESPACE = "flux-system"
# (namespace, name) of the Flux Kustomizations that may run as the platform account.
# services applies add-on namespaces and tenant identities, which no tenant may create.
SERVICES_STAGE = (FLUX_NAMESPACE, "services")
PLATFORM_KUSTOMIZATIONS = frozenset(
    {(FLUX_NAMESPACE, "platform-controllers"), (FLUX_NAMESPACE, "platform-configs"), SERVICES_STAGE}
)
# Stages whose path's objects may run as the platform account too. The services
# stage's path holds add-on Kustomizations, which run as their own tenant accounts.
PLATFORM_PATHS = frozenset({(FLUX_NAMESPACE, "platform-controllers"), (FLUX_NAMESPACE, "platform-configs")})
# A hosted app's wiring, app-<name>-wiring, applies core's app template as the platform
# account and nothing else: it creates the app's namespace and tenant identity. The app's own
# Kustomizations on that path run as the tenant.
APP_WIRING_PATH = "./core/platform/optional/app-hosting/app"
_APP_WIRING_NAME = re.compile(r"^app-[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?-wiring$")
# The root Kustomization applies the env's flux tree, which declares every other
# account, so it runs as the controller's own identity, which the FluxInstance creates.
ROOT_KUSTOMIZATION = (FLUX_NAMESPACE, FLUX_NAMESPACE)
CONTROLLER_SERVICE_ACCOUNT = "kustomize-controller"

_FLUX_APIS = {
    "Kustomization": "kustomize.toolkit.fluxcd.io/",
    "HelmRelease": "helm.toolkit.fluxcd.io/",
}
_BINDING_KINDS = {"RoleBinding", "ClusterRoleBinding"}
# Built-in roles too broad for a tenant in any namespace.
_BROAD_ROLES = {"cluster-admin", "admin", "edit"}


@dataclass(frozen=True)
class FluxIdentityViolation:
    path: str
    message: str


def scan_flux_identity(repo_root: Path | str) -> list[FluxIdentityViolation]:
    root = Path(repo_root).resolve()
    violations: list[FluxIdentityViolation] = []
    for flux_dir in env_flux_dirs(root):
        env_rel = flux_dir.relative_to(root).as_posix()
        env_docs, error = kustomize_build(root, env_rel)
        if error:
            violations.append(FluxIdentityViolation(env_rel, error))
            continue
        problems = list(_env_problems(root, env_rel, env_docs))
        violations.extend(FluxIdentityViolation(env_rel, problem) for problem in problems)
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[FluxIdentityViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _accounts(docs: Iterable[dict]) -> set[tuple[str, str]]:
    return {(namespace_of(doc), _name(doc)) for doc in docs if doc.get("kind") == "ServiceAccount"}


def _env_problems(root: Path, env_rel: str, env_docs: list[dict]) -> Iterator[str]:
    accounts = _accounts(env_docs)
    flux_docs = [doc for doc in env_docs if _is_flux(doc)]
    for doc in env_docs:
        if doc.get("kind") == "FluxInstance":
            cluster = (doc.get("spec") or {}).get("cluster") or {}
            if cluster.get("multitenant") is not True:
                yield f"{ref_of(doc)}: spec.cluster.multitenant is not true"
    paths = flux_paths(root, env_rel, env_docs)
    # A stage's objects as the cluster gets them, with the stage's postBuild values.
    stage_docs = {
        id(path): substitute(path.docs, postbuild_values(path.kustomization, env_docs))
        for path in paths
        if path.parent is None
    }
    # The services stage and each app's wiring, as the platform account, declare each add-on's
    # and each app's tenant account and its bindings.
    wiring = [
        item
        for path in paths
        if path.parent is None and (_wires_addons(path.kustomization) or _wires_app(path.kustomization))
        for item in stage_docs[id(path)]
    ]
    accounts |= _accounts(wiring)
    on_paths = [item for path in paths if path.parent is None for item in stage_docs[id(path)] if _is_flux(item)]
    tenants = {
        (namespace_of(doc), account)
        for doc in [*flux_docs, *on_paths]
        if isinstance(account := _spec(doc).get("serviceAccountName"), str)
        and account != PLATFORM_SERVICE_ACCOUNT
    }
    for doc in [*flux_docs, *on_paths]:
        if doc.get("kind") == "Kustomization" and _spec(doc).get("decryption") and namespace_of(doc) != FLUX_NAMESPACE:
            yield (
                f"{ref_of(doc)}: sets spec.decryption outside {FLUX_NAMESPACE}; "
                f"only {FLUX_NAMESPACE} decrypts with the env key"
            )
    for doc in [*env_docs, *wiring]:
        if doc.get("kind") not in _BINDING_KINDS:
            continue
        role = (doc.get("roleRef") or {}).get("name")
        for namespace, account in sorted(tenants):
            if not _binds(doc, namespace, account):
                continue
            who = f"{ref_of(doc)}: binds {namespace}/{account}"
            if doc.get("kind") == "ClusterRoleBinding":
                yield f"{who} cluster-wide"
            elif namespace_of(doc) == namespace:
                yield f"{who} in its own namespace, where the Flux objects live"
            if role in _BROAD_ROLES:
                yield f"{who} to {role}"
    for doc in flux_docs:
        if _is_root_kustomization(doc):
            account = _spec(doc).get("serviceAccountName")
            if account != CONTROLLER_SERVICE_ACCOUNT:
                yield f"{ref_of(doc)}: spec.serviceAccountName is {account!r}, expected {CONTROLLER_SERVICE_ACCOUNT!r}"
            continue
        yield from _object_problems(
            doc, accounts, may_run_as_platform=_is_platform_kustomization(doc) or _wires_app(doc)
        )
        if _APP_WIRING_NAME.fullmatch(_name(doc)) and not _wires_app(doc):
            yield (
                f"{ref_of(doc)}: an app's wiring runs as {PLATFORM_SERVICE_ACCOUNT} in {FLUX_NAMESPACE} "
                f"and applies {APP_WIRING_PATH} from the root source, with no components or patches"
            )
    for path in paths:
        yield from _path_problems(path, accounts, stage_docs.get(id(path), path.docs))


def _object_problems(
    doc: dict, accounts: set[tuple[str, str]], *, may_run_as_platform: bool
) -> Iterator[str]:
    ref = ref_of(doc)
    namespace = namespace_of(doc)
    spec = _spec(doc)
    account = spec.get("serviceAccountName")
    if not isinstance(account, str) or not account.strip():
        yield f"{ref}: spec.serviceAccountName is unset"
    else:
        if (namespace, account) not in accounts:
            yield f"{ref}: ServiceAccount {namespace}/{account} is not declared in the env's flux tree"
        if account == PLATFORM_SERVICE_ACCOUNT and not may_run_as_platform:
            allowed = ", ".join(f"{ns}/{name}" for ns, name in sorted(PLATFORM_KUSTOMIZATIONS))
            paths = ", ".join(f"{ns}/{name}" for ns, name in sorted(PLATFORM_PATHS))
            yield (
                f"{ref}: only Kustomizations {allowed}, and the objects on the paths of "
                f"{paths}, run as {PLATFORM_SERVICE_ACCOUNT}"
            )
    for field, target in _references(spec):
        if target != namespace:
            yield f"{ref}: {field} points to namespace {target}"


def _path_problems(path: FluxPath, accounts: set[tuple[str, str]], docs: list[dict]) -> list[str]:
    kustomization = path.kustomization
    ref = ref_of(kustomization)
    if path.error:
        return [] if path.error == "spec.path is unset" else [f"{ref}: {path.error}"]
    spec = _spec(kustomization)
    namespace = namespace_of(kustomization)
    account = spec.get("serviceAccountName")
    own = f"{namespace}/{account}, the account it runs as"
    platform = (namespace, _name(kustomization)) in PLATFORM_PATHS
    problems: list[str] = []
    for item in docs:
        if _is_flux(item) and path.parent is None:
            problems.extend(
                f"{ref} path: {problem}"
                for problem in _object_problems(item, accounts, may_run_as_platform=platform)
            )
        if not isinstance(account, str) or not account:
            continue
        kind = item.get("kind")
        if kind == "ServiceAccount" and (namespace_of(item), _name(item)) == (namespace, account):
            problems.append(f"{ref}: its path declares ServiceAccount {own}")
        elif kind in _BINDING_KINDS and _binds(item, namespace, account):
            problems.append(f"{ref}: its path declares {ref_of(item)}, a binding for {own}")
    return problems


def _references(spec: dict) -> Iterator[tuple[str, str]]:
    """(field, namespace) for each reference that names a namespace."""
    chart = spec.get("chart")
    chart_spec = chart.get("spec") if isinstance(chart, dict) else None
    chart_source = chart_spec.get("sourceRef") if isinstance(chart_spec, dict) else None
    single = (
        ("spec.sourceRef", spec.get("sourceRef")),
        ("spec.chartRef", spec.get("chartRef")),
        ("spec.chart.spec.sourceRef", chart_source),
    )
    for field, ref in single:
        if isinstance(ref, dict) and isinstance(ref.get("namespace"), str):
            yield field, ref["namespace"]
    for field in ("dependsOn", "healthChecks"):
        for ref in spec.get(field) or []:
            if isinstance(ref, dict) and isinstance(ref.get("namespace"), str):
                yield f"spec.{field}", ref["namespace"]


def _binds(binding: dict, namespace: str, account: str) -> bool:
    """The binding grants its role to that ServiceAccount, by name or by group."""
    default_namespace = namespace_of(binding) if binding.get("kind") == "RoleBinding" else ""
    groups = {
        "system:authenticated",
        "system:serviceaccounts",
        f"system:serviceaccounts:{namespace}",
    }
    for subject in binding.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        kind, name = subject.get("kind"), subject.get("name")
        if kind == "ServiceAccount":
            if name == account and (subject.get("namespace") or default_namespace) == namespace:
                return True
        elif kind == "User" and name == f"system:serviceaccount:{namespace}:{account}":
            return True
        elif kind == "Group" and name in groups:
            return True
    return False


def _is_flux(doc: dict) -> bool:
    api = _FLUX_APIS.get(str(doc.get("kind")))
    return api is not None and str(doc.get("apiVersion", "")).startswith(api)


def _is_root_kustomization(doc: dict) -> bool:
    return doc.get("kind") == "Kustomization" and (namespace_of(doc), _name(doc)) == ROOT_KUSTOMIZATION


def _is_platform_kustomization(doc: dict) -> bool:
    key = (namespace_of(doc), _name(doc))
    return doc.get("kind") == "Kustomization" and key in PLATFORM_KUSTOMIZATIONS


def _wires_addons(doc: dict) -> bool:
    """The services stage, running as the platform account: its path declares tenant accounts."""
    return (namespace_of(doc), _name(doc)) == SERVICES_STAGE and _spec(doc).get(
        "serviceAccountName"
    ) == PLATFORM_SERVICE_ACCOUNT


def _wires_app(doc: dict) -> bool:
    """A hosted app's wiring: app-<name>-wiring in flux-system, as the platform account, applying
    core's app template from the root source and nothing else."""
    spec = _spec(doc)
    return (
        doc.get("kind") == "Kustomization"
        and namespace_of(doc) == FLUX_NAMESPACE
        and bool(_APP_WIRING_NAME.fullmatch(_name(doc)))
        and spec.get("serviceAccountName") == PLATFORM_SERVICE_ACCOUNT
        and spec.get("path") == APP_WIRING_PATH
        and spec.get("sourceRef") == {"kind": "GitRepository", "name": FLUX_NAMESPACE}
        and not {"components", "patches", "images", "namePrefix", "nameSuffix", "kubeConfig", "targetNamespace"} & set(spec)
    )


def _spec(doc: dict) -> dict:
    spec = doc.get("spec")
    return spec if isinstance(spec, dict) else {}


def _name(doc: dict) -> str:
    metadata = doc.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) else ""
