"""Flux prune exemption for objects a bad commit or a path move must not delete.

kustomize-controller reads kustomize.toolkit.fluxcd.io/prune off the live
object and skips garbage collection when the value is disabled. For each env
with a flux/ tree, this builds the root path, the three layer paths with their
components, and the path of each add-on Kustomization on the services layer, then
requires every object of a protected kind, and the root Kustomization, to
carry prune: disabled. The layer Kustomizations keep prune on.

An exception needs a reason. Objects already guarded that are not one of
those kinds stay guarded.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from _manifests import flux_paths, kind_of, kustomize_build
from schema.env import ROOT_SOURCE

PRUNE = "kustomize.toolkit.fluxcd.io/prune"
FLUX_NAMESPACE = "flux-system"
LAYERS = ("platform-controllers", "platform-configs", "services")
Key = tuple[str, str, str]

PROTECTED_KINDS = frozenset(
    {
        "Namespace",
        "CustomResourceDefinition",
        "Secret",
        "PersistentVolumeClaim",
        "HelmRelease",
        "HelmRepository",
        "OCIRepository",
        "GitRepository",
        "FluxInstance",
        "GeneratingPolicy",
        "MutatingPolicy",
        "ClusterIssuer",
    }
)

# Opt out only with a reason. The testapp canary stores nothing, so prune stays on.
EXCEPTIONS: dict[Key, str] = {
    ("Namespace", "", "platform-testapp"): (
        "testapp canary; the fixture stores nothing, so prune stays on"
    ),
}

# Guarded before the kind rule, and not a protected kind. The root Kustomization
# owns the Flux objects, so deleting it from Git must not prune the cluster.
KEPT = frozenset(
    {
        ("Kustomization", FLUX_NAMESPACE, ROOT_SOURCE),
        ("ClusterRole", "", "kyverno:backup-generate"),
        ("ConfigMap", "kube-system", "coredns-custom"),
        ("Deployment", "platform-backups", "restic"),
        ("HelmChartConfig", "kube-system", "traefik"),
    }
)


@dataclass(frozen=True)
class FluxPruneViolation:
    path: str
    message: str


def scan_flux_prune(repo_root: Path | str) -> list[FluxPruneViolation]:
    root = Path(repo_root).resolve()
    flux_dirs = sorted(path for path in root.glob("envs/*/flux") if path.is_dir())
    if not flux_dirs:
        return [FluxPruneViolation("envs", "no envs/<env>/flux tree to check")]
    violations: list[FluxPruneViolation] = []
    for flux_dir in flux_dirs:
        env_rel = flux_dir.relative_to(root).as_posix()
        applied, problems, partial = _applied(root, env_rel)
        violations.extend(FluxPruneViolation(env_rel, problem) for problem in problems)
        unguarded, absent = unguarded_objects(applied)
        if partial:
            absent = []
        violations.extend(
            FluxPruneViolation(env_rel, f"{_label(key)} must carry {PRUNE}: disabled")
            for key in unguarded
        )
        violations.extend(
            FluxPruneViolation(env_rel, f"{_label(key)} is not on any built path")
            for key in absent
        )
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[FluxPruneViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def unguarded_objects(
    applied: Mapping[Key, dict],
    *,
    exceptions: Mapping[Key, str] = EXCEPTIONS,
) -> tuple[list[Key], list[Key]]:
    """Objects missing the annotation, and KEPT objects no built path has."""
    unexplained = sorted(key for key, reason in exceptions.items() if not str(reason).strip())
    if unexplained:
        raise AssertionError(f"unexplained exception: {unexplained}")
    unguarded = [
        key
        for key, doc in applied.items()
        if key not in exceptions
        and (key[0] in PROTECTED_KINDS or key in KEPT)
        and not prune_disabled(doc)
    ]
    absent = sorted(key for key in KEPT if key not in applied)
    return sorted(unguarded), absent


def prune_disabled(doc: dict) -> bool:
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return False
    annotations = metadata.get("annotations") or {}
    return isinstance(annotations, dict) and annotations.get(PRUNE) == "disabled"


def index(docs: Iterable[dict]) -> dict[Key, dict]:
    found: dict[Key, dict] = {}
    for doc in docs:
        metadata = doc.get("metadata")
        if "kind" not in doc or not isinstance(metadata, dict) or not metadata.get("name"):
            continue
        namespace = metadata.get("namespace") or ""
        found[(kind_of(doc), str(namespace), str(metadata["name"]))] = doc
    return found


def _applied(root: Path, env_rel: str) -> tuple[dict[Key, dict], list[str], bool]:
    """Objects on the built paths, build problems, and whether a path was left out.

    The example env's platform-configs lists bootstrap secrets that are not in Git, so
    the product tree skips it; a bootstrapped copy builds it.
    """
    docs, error = kustomize_build(root, env_rel)
    if error:
        return {}, [error], True
    applied = index(docs)
    problems: list[str] = []
    for layer in LAYERS:
        doc = applied.get(("Kustomization", FLUX_NAMESPACE, layer))
        path = (doc or {}).get("spec", {}).get("path")
        if not isinstance(path, str) or not path.strip():
            problems.append(f"Kustomization {layer} has no spec.path in the env's flux tree")
    partial = False
    # Each layer with its components, and each add-on Kustomization on the services layer.
    for path in flux_paths(root, env_rel, docs):
        partial = partial or path.partial
        if path.error:
            if path.error != "spec.path is unset":
                problems.append(path.error)
            continue
        applied.update(index(path.docs))
    return applied, problems, partial or bool(problems)


def _label(key: Key) -> str:
    kind, namespace, name = key
    return f"{kind} {namespace}/{name}" if namespace else f"{kind} {name}"
