"""R4 / D45: every ${name} a Flux Kustomization substitutes is a key its env publishes.

kustomize-controller runs with StrictPostBuildSubstitutions, so an unknown
key fails only at reconcile. For each env with a flux/ tree, this builds that
tree, reads the keys each Flux Kustomization's postBuild gets from
`substitute` and `substituteFrom`, then builds the Kustomization's path and
checks every ${name} in it, defaults included. As in Flux, `$${...}` is a
literal and objects labelled or annotated substitute: disabled are skipped.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from _manifests import ref_of

_FLUX_KUSTOMIZATION_API = "kustomize.toolkit.fluxcd.io/"
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
    for flux_dir in sorted(path for path in root.glob("envs/*/flux") if path.is_dir()):
        env_rel = flux_dir.relative_to(root).as_posix()
        env_docs, error = _build(root, env_rel)
        if error:
            violations.append(FluxSubstituteViolation(env_rel, error))
            continue
        for kustomization in env_docs:
            violations.extend(_kustomization_violations(root, env_rel, kustomization, env_docs))
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[FluxSubstituteViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _kustomization_violations(
    root: Path, env_rel: str, doc: dict, env_docs: list[dict]
) -> list[FluxSubstituteViolation]:
    api = doc.get("apiVersion")
    spec = doc.get("spec")
    if doc.get("kind") != "Kustomization" or not isinstance(api, str):
        return []
    if not api.startswith(_FLUX_KUSTOMIZATION_API) or not isinstance(spec, dict):
        return []
    post_build = spec.get("postBuild")
    if not isinstance(post_build, dict) or not (
        post_build.get("substitute") or post_build.get("substituteFrom")
    ):
        return []
    ref = ref_of(doc)
    keys, problems = _published_keys(post_build, _namespace(doc), env_docs)
    violations = [FluxSubstituteViolation(env_rel, f"{ref}: {problem}") for problem in problems]
    path = spec.get("path")
    if not isinstance(path, str) or not path.strip():
        return violations + [FluxSubstituteViolation(env_rel, f"{ref}: spec.path is unset")]
    docs, error = _build(root, path.strip().removeprefix("./"))
    if error:
        return violations + [FluxSubstituteViolation(env_rel, f"{ref}: {error}")]
    for item in docs:
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
                and _namespace(item) == namespace
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


def _namespace(doc: dict) -> str:
    metadata = doc.get("metadata")
    namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
    return namespace if isinstance(namespace, str) and namespace else "default"


def _build(root: Path, rel: str) -> tuple[list[dict], str | None]:
    kustomize = shutil.which("kustomize")
    if kustomize is None:
        return [], "kustomize not found on PATH (run make deps)"
    proc = subprocess.run(
        [kustomize, "build", "--load-restrictor", "LoadRestrictionsNone", str(root / rel)],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    if proc.returncode != 0:
        return [], f"kustomize build {rel} failed: {(proc.stderr or proc.stdout).strip()}"
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)], None
