"""What an env's Flux Kustomizations apply, built by Flux itself (`flux build kustomization`).

The env checks read the objects the cluster would get. Flux does the work: the kustomize build of
each path (a directory with no kustomization file included), its components, and postBuild
substitution, strictly, so a variable with no value and no default fails the build as it fails
the reconcile. This module does two things Flux's offline build cannot:

- lay out the root artifact once: the env repository's files, with core's core/ and addons/
  where sync.yaml's root GitRepository includes them (fromPath core and addons);
- walk the tree. `flux build --recursive --dry-run` builds a child before its parent's
  substitution reaches it, and skips substituteFrom, so each Kustomization is built on its own:
  first the root (sync.yaml's), then every Kustomization a built path applies. Before each build,
  its substituteFrom ConfigMaps are read from the objects already built (the env's cluster-vars
  ConfigMap is one) and handed to Flux as substitute values, its inline substitute winning, as on
  the cluster.

A Kustomization that reads another source (a hosted app's, from the apps repository) is listed
with no objects, as partial: the app checks read those.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

from jarvis import paths as _paths
from jarvis.schema.env import ADDONS_DIR, CORE_SOURCE_DIR, ROOT_SOURCE, SYNC_FILE

FLUX_KUSTOMIZATION_API = "kustomize.toolkit.fluxcd.io/"
_CORE_TREES = (CORE_SOURCE_DIR, ADDONS_DIR)
# libyaml's safe loader where PyYAML has it: a build's output is most of the walk's time.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# What `flux build --dry-run` puts in place of each value of a SOPS-encrypted Secret.
_SOPS_MASK = "**SOPS**"


@dataclass(frozen=True)
class FluxPath:
    """What one Flux Kustomization applies, as Flux builds it. parent is the Kustomization whose
    path applies it, or None for one the env's flux tree applies. partial: its source is not the
    root artifact, so its objects are not built here."""

    kustomization: dict
    parent: dict | None
    docs: list[dict]
    error: str | None
    partial: bool


@dataclass(frozen=True)
class EnvTree:
    """An env's root Kustomization's objects (env_docs) and every Kustomization under it."""

    env_docs: list[dict]
    paths: list[FluxPath]
    error: str | None


def is_flux_kustomization(doc: dict) -> bool:
    return doc.get("kind") == "Kustomization" and str(doc.get("apiVersion", "")).startswith(FLUX_KUSTOMIZATION_API)


def _ns(doc: dict) -> str:
    return str((doc.get("metadata") or {}).get("namespace") or "default")


def _name(doc: dict) -> str:
    return str((doc.get("metadata") or {}).get("name") or "")


@contextlib.contextmanager
def root_artifact(root: Path, env_rel: str) -> Iterator[Path]:
    """The root artifact Flux reads for the env, in a temporary folder: the env's files, with the
    trees of the core running this code (core_root, the env's pin) where sync.yaml includes them."""
    core = _paths.core_root()
    with tempfile.TemporaryDirectory(prefix="jarvis-artifact-") as work:
        stage = Path(work)
        for tree in _CORE_TREES:
            if (core / tree).is_dir():
                (stage / tree).symlink_to((core / tree).resolve(), target_is_directory=True)
        env_dir = "/".join(env_rel.split("/")[:2])
        shutil.copytree(root / env_dir, stage / env_dir, symlinks=True)
        yield stage


def _substitute_from(doc: dict, built: list[dict]) -> tuple[dict[str, str], list[str]]:
    """doc's postBuild values as Flux merges them: each substituteFrom ConfigMap or Secret in order,
    found among the objects already built, then the inline substitute over them."""
    post_build = (doc.get("spec") or {}).get("postBuild") or {}
    values: dict[str, str] = {}
    problems: list[str] = []
    for source in post_build.get("substituteFrom") or []:
        if not isinstance(source, dict):
            problems.append("postBuild.substituteFrom entry is not a mapping")
            continue
        kind, name = source.get("kind"), source.get("name")
        match = next((item for item in built if item.get("kind") == kind and _name(item) == name and _ns(item) == _ns(doc)), None)
        if match is None:
            if source.get("optional") is not True:
                problems.append(f"substituteFrom {kind} {_ns(doc)}/{name} is not in the env's flux tree")
            continue
        fields = {**(match.get("data") or {}), **(match.get("stringData") or {})}
        if kind == "Secret" and ("sops" in match or _SOPS_MASK in fields.values()):
            problems.append(f"substituteFrom Secret {_ns(doc)}/{name} is sealed; its values cannot be read offline")
            continue
        data = {str(k): str(v) for k, v in (match.get("data") or {}).items()}
        if kind == "Secret":
            try:  # a Secret's data is base64, as Flux decodes it
                data = {k: base64.b64decode(v, validate=True).decode() for k, v in data.items()}
            except (binascii.Error, UnicodeDecodeError):
                problems.append(f"substituteFrom Secret {_ns(doc)}/{name} holds data that is not base64 text")
                continue
        values.update(data)
        values.update({str(k): str(v) for k, v in (match.get("stringData") or {}).items()})
    values.update({str(k): str(v) for k, v in (post_build.get("substitute") or {}).items()})
    return values, problems


def build(artifact: Path, doc: dict, values: dict[str, str]) -> tuple[list[dict], str | None]:
    """The objects doc applies, built by `flux build kustomization --dry-run --strict-substitute`
    from the artifact, with values as its substitute (substituteFrom resolved by the caller).
    --in-memory-build: Flux writes the kustomization file it generates for a plain directory in
    memory, never into the repository."""
    flux = shutil.which("flux")
    if flux is None:
        return [], "flux not found on PATH (run jarvis doctor)"
    spec = dict(doc.get("spec") or {})
    path = spec.get("path")
    if not isinstance(path, str) or not path.strip():
        return [], "spec.path is unset"
    post_build = {k: v for k, v in (spec.get("postBuild") or {}).items() if k != "substituteFrom"}
    if values:
        post_build["substitute"] = values
    spec["postBuild"] = post_build
    target = artifact / path.strip().removeprefix("./")
    if not target.is_dir():
        return [], f"spec.path {path} is not a directory in the root artifact"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", prefix="jarvis-ks-") as handle:
        yaml.safe_dump({**doc, "spec": spec}, handle)
        handle.flush()
        proc = subprocess.run(
            [flux, "build", "kustomization", _name(doc), "--path", str(target), "--kustomization-file", handle.name,
             "--dry-run", "--strict-substitute", "--in-memory-build"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
    if proc.returncode:
        message = (proc.stderr or proc.stdout).strip().replace(str(artifact) + "/", "")
        return [], f"flux build {_name(doc)} failed: {message}"
    return [item for item in yaml.load_all(proc.stdout, Loader=_LOADER) if isinstance(item, dict)], None


def root_kustomization(root: Path, env_rel: str) -> dict | None:
    """The root Kustomization sync.yaml declares for the env."""
    path = root / env_rel.rstrip("/") / SYNC_FILE
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError):
        return None
    return next((doc for doc in docs if isinstance(doc, dict) and is_flux_kustomization(doc) and _name(doc) == ROOT_SOURCE), None)


def _walk(root: Path, env_rel: str) -> EnvTree:
    top = root_kustomization(root, env_rel)
    if top is None:
        return EnvTree([], [], f"{env_rel}/{SYNC_FILE} declares no root Kustomization {ROOT_SOURCE}")
    with root_artifact(root, env_rel) as artifact:
        values, problems = _substitute_from(top, [])
        env_docs, error = build(artifact, top, values)
        if error or problems:
            return EnvTree(env_docs, [], "; ".join([*problems, *([error] if error else [])]))
        built = list(env_docs)
        paths: list[FluxPath] = []
        queue: list[tuple[dict, dict | None]] = [(doc, None) for doc in env_docs if is_flux_kustomization(doc) and _name(doc) != ROOT_SOURCE]
        while queue:
            doc, parent = queue.pop(0)
            source = (doc.get("spec") or {}).get("sourceRef") or {}
            if source.get("name") != ROOT_SOURCE:
                paths.append(FluxPath(doc, parent, [], None, True))
                continue
            values, problems = _substitute_from(doc, built)
            docs, error = ([], "; ".join(problems)) if problems else build(artifact, doc, values)
            paths.append(FluxPath(doc, parent, docs, error, False))
            built.extend(docs)
            queue.extend((item, doc) for item in docs if is_flux_kustomization(item))
    return EnvTree(env_docs, paths, None)


def _fingerprint(root: Path, env_rel: str) -> str:
    """Every file the walk can read for the env: its tree and core's, so a cached walk is redone
    whenever one changes (the tests edit trees between checks)."""
    digest = hashlib.sha256()
    core = _paths.core_root()
    for base in (root / env_rel.rstrip("/").rsplit("/flux", 1)[0], *(core / tree for tree in _CORE_TREES)):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            with contextlib.suppress(OSError):
                stat = path.stat()
                digest.update(f"{path}:{stat.st_mtime_ns}:{stat.st_size}\n".encode())
    return digest.hexdigest()


_CACHE: dict[tuple[str, str, str], tuple[str, EnvTree]] = {}


def env_tree(root: Path | str, env_rel: str) -> EnvTree:
    """The env's tree, built once per content: every env check reads the same walk."""
    root = Path(root).resolve()
    key = (str(root), str(_paths.core_root()), env_rel.rstrip("/"))
    stamp = _fingerprint(root, env_rel)
    cached = _CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    tree = _walk(root, env_rel.rstrip("/"))
    _CACHE[key] = (stamp, tree)
    return tree
