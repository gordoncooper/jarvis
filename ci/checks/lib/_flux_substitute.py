"""Every ${name} a Flux Kustomization substitutes is a key its env publishes.

kustomize-controller runs with StrictPostBuildSubstitutions, so an unknown
key fails only at reconcile. For each env with a flux/ tree, this builds that
tree, reads the keys each Flux Kustomization's postBuild gets from
`substitute` and `substituteFrom`, then builds the Kustomization's path (with
its components, and the add-on Kustomizations on the services stage's path) and
checks every ${name} in it, defaults included. As in Flux, `$${...}` is a
literal and objects labelled or annotated substitute: disabled are skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from _manifests import FluxPath, flux_paths, kustomize_build, namespace_of, ref_of

_SUBSTITUTE_KEY = "kustomize.toolkit.fluxcd.io/substitute"
# Each `$$` is a literal `$`, so a variable starts after an odd run of `$`.
_REFERENCE = re.compile(r"(?<!\$)(?:\$\$)*\$\{#?([_A-Za-z][_A-Za-z0-9]*)")


@dataclass(frozen=True)
class FluxSubstituteViolation:
    path: str
    message: str


def scan_flux_substitute(repo_root: Path | str) -> list[FluxSubstituteViolation]:
    root = Path(repo_root).resolve()
    violations: list[FluxSubstituteViolation] = []
    flux_dirs = sorted(path for path in root.glob("envs/*/flux") if path.is_dir())
    if not flux_dirs:
        return [FluxSubstituteViolation("envs", "no envs/<env>/flux tree to check")]
    for flux_dir in flux_dirs:
        env_rel = flux_dir.relative_to(root).as_posix()
        env_docs, error = kustomize_build(root, env_rel)
        if error:
            violations.append(FluxSubstituteViolation(env_rel, error))
            continue
        if not env_docs:
            violations.append(FluxSubstituteViolation(env_rel, "flux tree builds to no objects"))
            continue
        for path in flux_paths(root, env_rel, env_docs):
            violations.extend(_kustomization_violations(env_rel, path, env_docs))
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[FluxSubstituteViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _kustomization_violations(
    env_rel: str, path: FluxPath, env_docs: list[dict]
) -> list[FluxSubstituteViolation]:
    doc = path.kustomization
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return []
    post_build = spec.get("postBuild")
    if not isinstance(post_build, dict) or not (
        post_build.get("substitute") or post_build.get("substituteFrom")
    ):
        return []
    ref = ref_of(doc)
    keys, problems = _published_keys(post_build, namespace_of(doc), env_docs)
    violations = [FluxSubstituteViolation(env_rel, f"{ref}: {problem}") for problem in problems]
    if path.error:
        return violations + [FluxSubstituteViolation(env_rel, f"{ref}: {path.error}")]
    for item in path.docs:
        if _substitute_disabled(item):
            continue
        for name in sorted(set(_references(item)) - keys):
            violations.append(
                FluxSubstituteViolation(
                    env_rel,
                    f"{ref}: {ref_of(item)} uses ${{{name}}}, which the env does not publish",
                )
            )
    return violations


def _published_keys(
    post_build: dict, namespace: str, env_docs: list[dict]
) -> tuple[set[str], list[str]]:
    keys: set[str] = set()
    problems: list[str] = []
    inline = post_build.get("substitute")
    if isinstance(inline, dict):
        keys.update(str(key) for key in inline)
    for source in post_build.get("substituteFrom") or []:
        if not isinstance(source, dict):
            problems.append("postBuild.substituteFrom entry is not a mapping")
            continue
        kind, name = source.get("kind"), source.get("name")
        match = next(
            (
                item
                for item in env_docs
                if item.get("kind") == kind
                and (item.get("metadata") or {}).get("name") == name
                and namespace_of(item) == namespace
            ),
            None,
        )
        if match is None:
            if source.get("optional") is not True:
                problems.append(f"substituteFrom {kind} {namespace}/{name} is not in the env's flux tree")
            continue
        for field in ("data", "stringData"):
            if isinstance(match.get(field), dict):
                keys.update(str(key) for key in match[field])
    return keys, problems


def _references(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield from _REFERENCE.findall(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _references(key)
            yield from _references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _references(item)


def _substitute_disabled(doc: dict) -> bool:
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return any(
        isinstance(metadata.get(field), dict) and metadata[field].get(_SUBSTITUTE_KEY) == "disabled"
        for field in ("labels", "annotations")
    )
