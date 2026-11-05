"""Add-ons plug into core at one seam and stay inside it (CONTRACT: services/CONTRACT.md).

Product rules, on this checkout:

- Each add-on folder services/<name>/ holds a kustomize Component that lists
  files only, so its objects are the same whether Flux adds it to the services
  stage or this check builds it alone.
- The slot an add-on fills (label addons.jarvis.io/slot on its Flux
  Kustomization) has a folder under services/slots/. No add-on is named like a slot.
- Core (CORE_DIRS) never names an add-on. It names slots and contracts.

Env rules, for each env with a flux/ tree, on each add-on its services
Kustomization lists in spec.components:

- The services Kustomization runs as the platform account.
- The wiring holds only the add-on's Namespaces (labelled with its name, none
  reserved), one ServiceAccount in flux-system (its tenant), Roles and
  RoleBindings in its own Namespaces, and Flux Kustomizations in flux-system that
  run as the tenant. Nothing else, so nothing cluster-scoped but its Namespaces.
  The Namespaces, account, Roles and RoleBindings carry prune: disabled: when the
  account is gone, kustomize-controller skips an add-on Kustomization's cleanup.
  Those Kustomizations depend only on each other, never on another add-on.
- A RoleBinding binds only the tenant, to a Role the add-on ships in that namespace.
- A Role grants nothing on RBAC, Flux, admission or CRD APIs, on namespaces, or by wildcard.
- The Namespaces are named <name> or <name>-<part>, so an add-on never adopts
  core's or another add-on's namespace.
- Each Kustomization reads the root source and sets no patches, images,
  components, name affixes, kubeConfig or postBuild.substituteFrom (optional or
  not; inline postBuild.substitute only), so what this check builds is what
  Flux applies. A decrypting one reads envs/<env>/secrets/<one of its
  namespaces>; any other reads under core/services/<name>/ (path normalised).
- Every object an add-on Kustomization applies is namespaced, lands in the
  add-on's own namespaces, and is not RBAC or a Flux object.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from _manifests import (
    build_flux_path,
    is_flux_kustomization,
    kustomize_build,
    namespace_of,
    postbuild_values,
    ref_of,
    substitute,
)
from schema.env import (
    ADDON_SECRETS_PATH,
    ADDONS_DIR,
    CORE_SOURCE_DIR,
    addon_documents,
    addon_names,
    addon_slots,
    core_dir,
    slot_names,
)

# Core: these trees name slots and contracts, never an add-on.
CORE_DIRS = ("platform", "tools", "schema", "infra", "ci/checks")
ADDON_LABEL = "addons.jarvis.io/name"
_PRUNE = "kustomize.toolkit.fluxcd.io/prune"
FLUX_NAMESPACE = "flux-system"
PLATFORM_SERVICE_ACCOUNT = "flux-platform"
SERVICES_STAGE = (FLUX_NAMESPACE, "services")
# app-<name> is for hosted apps; the others are core's.
RESERVED_PREFIXES = ("app-", "platform-", "kube-", "flux-")
RESERVED_NAMESPACES = frozenset({"default"})
_WIRING_KINDS = frozenset({"Namespace", "ServiceAccount", "Role", "RoleBinding"})
_RBAC_KINDS = frozenset({"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"})
_FORBIDDEN_GROUPS = frozenset(
    {"*", "rbac.authorization.k8s.io", "apiextensions.k8s.io", "admissionregistration.k8s.io"}
)
_FORBIDDEN_RESOURCES = frozenset({"*", "namespaces"})
# Kinds with no namespace that an add-on's path could carry.
CLUSTER_SCOPED_KINDS = frozenset(
    {
        "Namespace",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "MutatingWebhookConfiguration",
        "ValidatingWebhookConfiguration",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "PersistentVolume",
        "StorageClass",
        "PriorityClass",
        "IngressClass",
        "RuntimeClass",
        "APIService",
        "ClusterPolicy",
        "ClusterIssuer",
        "VolumeSnapshotClass",
        "Node",
    }
)
_TOKEN = r"A-Za-z0-9_\-"


@dataclass(frozen=True)
class AddonViolation:
    path: str
    message: str


def scan_addons(product_root: Path | str, env_root: Path | str) -> list[AddonViolation]:
    """Product rules on product_root, env rules on every env under env_root."""
    violations = list(_product_problems(Path(product_root).resolve()))
    root = Path(env_root).resolve()
    for flux_dir in sorted(path for path in root.glob("envs/*/flux") if path.is_dir()):
        env_rel = flux_dir.relative_to(root).as_posix()
        violations.extend(AddonViolation(env_rel, problem) for problem in _env_problems(root, env_rel))
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[AddonViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _product_problems(core: Path) -> Iterator[AddonViolation]:
    names = addon_names(core)
    slots = slot_names(core)
    for name in names:
        rel = f"{ADDONS_DIR}/{name}"
        listing = yaml.safe_load((core / rel / "kustomization.yaml").read_text(encoding="utf-8")) or {}
        if listing.get("kind") != "Component":
            yield AddonViolation(rel, "kustomization.yaml must be a kustomize Component")
        extra = sorted(set(listing) - {"apiVersion", "kind", "resources"})
        if extra:
            yield AddonViolation(rel, f"the Component lists files only, not {', '.join(extra)}")
        for item in listing.get("resources") or []:
            if not (core / rel / str(item)).is_file():
                yield AddonViolation(rel, f"the Component lists {item}, which is not a file in {rel}/")
        if name in slots:
            yield AddonViolation(rel, f"an add-on is not named like slot {name}")
        for slot in sorted(addon_slots(addon_documents(core, name)) - set(slots)):
            yield AddonViolation(rel, f"fills slot {slot}, which has no folder under {ADDONS_DIR}/slots/")
    if names:
        yield from _core_mentions(core, names)


def _core_mentions(core: Path, names: tuple[str, ...]) -> Iterator[AddonViolation]:
    pattern = re.compile(
        rf"(?<![{_TOKEN}])(?:{'|'.join(re.escape(name) for name in names)})(?![{_TOKEN}])", re.IGNORECASE
    )
    for top in CORE_DIRS:
        base = core / top
        if not base.is_dir():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file() and "__pycache__" not in item.parts):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                match = pattern.search(line)
                if match:
                    rel = path.relative_to(core).as_posix()
                    yield AddonViolation(
                        f"{rel}:{line_no}",
                        f"core names add-on {match.group(0)!r}; refer to its slot or contract instead",
                    )


def _env_problems(root: Path, env_rel: str) -> Iterator[str]:
    env_docs, error = kustomize_build(root, env_rel)
    if error:
        yield error
        return
    stage = next(
        (doc for doc in env_docs if is_flux_kustomization(doc) and (namespace_of(doc), _name(doc)) == SERVICES_STAGE),
        None,
    )
    if stage is None:
        return
    components = (stage.get("spec") or {}).get("components") or []
    if components and (stage.get("spec") or {}).get("serviceAccountName") != PLATFORM_SERVICE_ACCOUNT:
        yield (
            f"{ref_of(stage)}: lists add-ons, so it runs as {PLATFORM_SERVICE_ACCOUNT}; "
            "it creates their namespaces and tenant identities"
        )
    known = addon_names(core_dir(root))
    values = postbuild_values(stage, env_docs)
    for component in components:
        name = str(component).strip().removeprefix("./")
        if name not in known:
            continue  # env-schema reports an unknown add-on
        wiring, error = kustomize_build(root, f"{CORE_SOURCE_DIR}/{ADDONS_DIR}/{name}")
        if error:
            yield f"add-on {name}: {error}"
            continue
        namespaces = {_name(doc) for doc in wiring if doc.get("kind") == "Namespace"}
        yield from (f"add-on {name}: {problem}" for problem in _wiring_problems(name, wiring, namespaces))
        for doc in wiring:
            if is_flux_kustomization(doc):
                path = build_flux_path(root, env_rel, substitute(doc, values), parent=stage)
                yield from (f"add-on {name}: {problem}" for problem in _tenant_path_problems(path, namespaces))


def _wiring_problems(name: str, docs: list[dict], namespaces: set[str]) -> Iterator[str]:
    if not namespaces:
        yield "declares no Namespace"
    accounts = [doc for doc in docs if doc.get("kind") == "ServiceAccount"]
    if len(accounts) != 1 or namespace_of(accounts[0]) != FLUX_NAMESPACE:
        yield f"declares {len(accounts)} ServiceAccounts; it declares exactly one, its tenant, in {FLUX_NAMESPACE}"
        return
    tenant = _name(accounts[0])
    if tenant != f"addon-{name}":
        yield f"{ref_of(accounts[0])}: the tenant account is named addon-{name}"
    roles = {(namespace_of(doc), _name(doc)) for doc in docs if doc.get("kind") == "Role"}
    for doc in docs:
        ref, kind = ref_of(doc), doc.get("kind")
        annotations = (doc.get("metadata") or {}).get("annotations") or {}
        if kind in _WIRING_KINDS and annotations.get(_PRUNE) != "disabled":
            # Flux skips an add-on Kustomization's cleanup once its account is gone.
            yield f"{ref}: lacks {_PRUNE}: disabled, so turning the add-on off could remove it before its workloads"
        if kind == "Namespace":
            namespace = _name(doc)
            if namespace in RESERVED_NAMESPACES or namespace.startswith(RESERVED_PREFIXES):
                yield f"{ref}: the name is reserved ({', '.join(RESERVED_PREFIXES)} and {', '.join(sorted(RESERVED_NAMESPACES))})"
            elif namespace != name and not namespace.startswith(f"{name}-"):
                # Only the add-on's own names, so it never adopts a core or another add-on's namespace.
                yield f"{ref}: an add-on's namespaces are named {name} or {name}-<part>"
            if ((doc.get("metadata") or {}).get("labels") or {}).get(ADDON_LABEL) != name:
                yield f"{ref}: label {ADDON_LABEL} must be {name}"
        elif kind in {"Role", "RoleBinding"} and namespace_of(doc) not in namespaces:
            yield f"{ref}: is outside the add-on's namespaces {sorted(namespaces)}"
        elif kind == "Role":
            yield from (f"{ref}: {problem}" for problem in _rule_problems(doc))
        elif kind == "RoleBinding":
            role = doc.get("roleRef") or {}
            if role.get("kind") != "Role" or (namespace_of(doc), role.get("name")) not in roles:
                yield f"{ref}: binds a role the add-on does not ship in {namespace_of(doc)}"
            subjects = doc.get("subjects") or []
            if subjects != [{"kind": "ServiceAccount", "name": tenant, "namespace": FLUX_NAMESPACE}]:
                yield f"{ref}: binds subjects other than {FLUX_NAMESPACE}/{tenant}"
        elif is_flux_kustomization(doc):
            yield from (f"{ref}: {problem}" for problem in _kustomization_problems(name, doc, tenant, namespaces))
            own = {_name(item) for item in docs if is_flux_kustomization(item)}
            for dependency in (doc.get("spec") or {}).get("dependsOn") or []:
                if isinstance(dependency, dict) and dependency.get("name") not in own:
                    yield f"{ref}: depends on {dependency.get('name')}; an add-on waits only on its own Kustomizations"
        elif kind not in _WIRING_KINDS:
            yield (
                f"{ref}: an add-on's wiring holds only its Namespaces, tenant account, Roles, "
                "RoleBindings and Flux Kustomizations; a cluster-scoped or other object is not admitted"
            )


def _rule_problems(role: dict) -> Iterator[str]:
    for rule in role.get("rules") or []:
        groups = set(rule.get("apiGroups") or [])
        resources = set(rule.get("resources") or [])
        bad = sorted(group for group in groups if group in _FORBIDDEN_GROUPS or "fluxcd" in group)
        if bad:
            yield f"grants API group {', '.join(bad)}"
        if resources & _FORBIDDEN_RESOURCES:
            yield f"grants {', '.join(sorted(resources & _FORBIDDEN_RESOURCES))}"
        if "*" in (rule.get("verbs") or []):
            yield "grants every verb"


def _kustomization_problems(name: str, doc: dict, tenant: str, namespaces: set[str]) -> Iterator[str]:
    spec = doc.get("spec") or {}
    if namespace_of(doc) != FLUX_NAMESPACE:
        yield f"lives outside {FLUX_NAMESPACE}"
    if _name(doc) != f"addon-{name}" and not _name(doc).startswith(f"addon-{name}-"):
        yield f"is named addon-{name} or addon-{name}-<part>"
    if spec.get("serviceAccountName") != tenant:
        yield f"runs as {spec.get('serviceAccountName')!r}, not the add-on's tenant {tenant}"
    target = spec.get("targetNamespace")
    if target is not None and target not in namespaces:
        yield f"targetNamespace {target} is not one of the add-on's namespaces"
    if spec.get("sourceRef") != {"kind": "GitRepository", "name": FLUX_NAMESPACE}:
        yield f"reads {spec.get('sourceRef')!r}; an add-on reads the root source GitRepository {FLUX_NAMESPACE}"
    # What this check builds is what Flux applies: no field that rewrites the objects or the target.
    for field in sorted({"patches", "images", "components", "kubeConfig", "namePrefix", "nameSuffix"} & set(spec)):
        yield f"sets spec.{field}; an add-on's objects are the files under its path, as they are"
    # Tenant containment: substitution sources. The controller reads them with its own
    # rights in flux-system, so an add-on names none; it takes inline values only.
    sources = ((spec.get("postBuild") or {}).get("substituteFrom")) or []
    for source in sources:
        what = f"{source.get('kind')} {source.get('name')}" if isinstance(source, dict) else repr(source)
        yield (
            f"postBuild.substituteFrom names {what}; an add-on Kustomization takes no substitution "
            "source, only inline postBuild.substitute values the services stage fills"
        )
    raw = str(spec.get("path") or "")
    path = "./" + posixpath.normpath(raw.removeprefix("./"))
    home = f"./{CORE_SOURCE_DIR}/{ADDONS_DIR}/{name}"
    if spec.get("decryption"):
        if path.removeprefix(ADDON_SECRETS_PATH) not in namespaces or not path.startswith(ADDON_SECRETS_PATH):
            yield f"decrypts {raw}; an add-on's sealed Secrets are under {ADDON_SECRETS_PATH}<its namespace>"
    elif path != home and not path.startswith(home + "/"):
        yield f"path {raw} is outside the add-on's home {home}"


def _tenant_path_problems(path, namespaces: set[str]) -> Iterator[str]:
    ref = ref_of(path.kustomization)
    if path.error:
        yield f"{ref}: {path.error}"
        return
    target = (path.kustomization.get("spec") or {}).get("targetNamespace")
    for item in path.docs:
        kind = item.get("kind")
        what = f"{ref} applies {ref_of(item)}"
        if kind in CLUSTER_SCOPED_KINDS or str(kind).startswith("Cluster"):
            yield f"{what}, which is cluster-scoped; the tenant cannot apply it"
        elif kind in _RBAC_KINDS or "fluxcd" in str(item.get("apiVersion", "")):
            yield f"{what}; an add-on's own objects carry no RBAC or Flux objects"
        else:
            namespace = target or (item.get("metadata") or {}).get("namespace")
            if namespace not in namespaces:
                yield f"{what} in namespace {namespace or 'unset'}, outside the add-on's namespaces {sorted(namespaces)}"


def _name(doc: dict) -> str:
    metadata = doc.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) else ""
