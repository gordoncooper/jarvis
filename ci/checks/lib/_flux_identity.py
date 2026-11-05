"""Every Flux Kustomization and HelmRelease runs as an identity it was given.

With multi-tenancy lockdown on, kustomize-controller and helm-controller run an
object that names no service account as the `default` account of its
namespace, and refuse a source or dependency in another namespace. For each env
with a flux/ tree, this builds that tree and the path of each Flux
Kustomization in it, then checks:

- Every FluxInstance sets spec.cluster.multitenant: true.
- Every Kustomization and HelmRelease sets spec.serviceAccountName, and the
  env's flux tree declares that ServiceAccount in the object's namespace. The
  root sync applies that tree with the controller's own identity: the root
  Kustomization flux-system/flux-system names kustomize-controller, and its
  path (the tree itself) is not checked as a tenant path.
- Only PLATFORM_KUSTOMIZATIONS, and the objects on their paths, run as
  PLATFORM_SERVICE_ACCOUNT. No other account a Flux object runs as is bound by
  a ClusterRoleBinding, by a RoleBinding in the namespace the Flux objects
  live in, or to cluster-admin, admin or edit.
- No Kustomization's path declares the account it runs as, or a binding for it.
- sourceRef, chart sourceRef, chartRef, dependsOn and healthChecks stay in the
  object's namespace. Flux allows a cross-namespace health check; this check
  does not.

A Kustomization on a path is checked, but its own path is not built.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from _manifests import example_platform_configs, kustomize_build, namespace_of, ref_of

PLATFORM_SERVICE_ACCOUNT = "flux-platform"
# (namespace, name) of the Flux Kustomizations that may run as the platform account.
PLATFORM_KUSTOMIZATIONS = frozenset(
    {("flux-system", "platform-controllers"), ("flux-system", "platform-configs")}
)
# The core list the example env's platform-configs path applies. The example
# env's own list also names bootstrap secrets that are not in Git.
_EXAMPLE_PLATFORM_CONFIGS_CORE = "platform/configs"
# The root Kustomization applies the env's flux tree, which declares every other
# account, so it runs as the controller's own identity, which the FluxInstance creates.
ROOT_KUSTOMIZATION = ("flux-system", "flux-system")
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
    for flux_dir in sorted(path for path in root.glob("envs/*/flux") if path.is_dir()):
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


def _env_problems(root: Path, env_rel: str, env_docs: list[dict]) -> Iterator[str]:
    accounts = {
        (namespace_of(doc), _name(doc))
        for doc in env_docs
        if doc.get("kind") == "ServiceAccount"
    }
    flux_docs = [doc for doc in env_docs if _is_flux(doc)]
    for doc in env_docs:
        if doc.get("kind") == "FluxInstance":
            cluster = (doc.get("spec") or {}).get("cluster") or {}
            if cluster.get("multitenant") is not True:
                yield f"{ref_of(doc)}: spec.cluster.multitenant is not true"
    tenants = {
        (namespace_of(doc), account)
        for doc in flux_docs
        if isinstance(account := _spec(doc).get("serviceAccountName"), str)
        and account != PLATFORM_SERVICE_ACCOUNT
    }
    for doc in env_docs:
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
        platform = _is_platform_kustomization(doc)
        yield from _object_problems(doc, accounts, may_run_as_platform=platform)
        if doc.get("kind") == "Kustomization":
            yield from _path_problems(root, env_rel, doc, accounts, platform)


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
            yield (
                f"{ref}: only Kustomizations {allowed} and the objects on their paths "
                f"run as {PLATFORM_SERVICE_ACCOUNT}"
            )
    for field, target in _references(spec):
        if target != namespace:
            yield f"{ref}: {field} points to namespace {target}"


def _path_problems(
    root: Path, env_rel: str, kustomization: dict, accounts: set[tuple[str, str]], platform: bool
) -> list[str]:
    ref = ref_of(kustomization)
    spec = _spec(kustomization)
    path = spec.get("path")
    if not isinstance(path, str) or not path.strip():
        return []
    rel = path.strip().removeprefix("./")
    if example_platform_configs(env_rel, path):
        rel = _EXAMPLE_PLATFORM_CONFIGS_CORE
    docs, error = kustomize_build(root, rel)
    if error:
        return [f"{ref}: {error}"]
    namespace = namespace_of(kustomization)
    account = spec.get("serviceAccountName")
    own = f"{namespace}/{account}, the account it runs as"
    problems: list[str] = []
    for item in docs:
        if _is_flux(item):
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


def _spec(doc: dict) -> dict:
    spec = doc.get("spec")
    return spec if isinstance(spec, dict) else {}


def _name(doc: dict) -> str:
    metadata = doc.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) else ""
