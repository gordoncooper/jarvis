"""YAML documents the repo deploys. Charts are not rendered."""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from jarvis.schema.env import CORE_SOURCE_DIR, EXAMPLE_ENV, ROOT_SOURCE

SCAN_ROOTS = ("platform/", "services/", "envs/")
SKIP_ENV_PREFIXES = ("secrets/", "values/", "inventory/")
FLUX_KUSTOMIZATION_API = "kustomize.toolkit.fluxcd.io/"
_KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
# A postBuild variable; `$${...}` is a literal.
_VARIABLE = re.compile(r"(?<!\$)\$\{([_A-Za-z][_A-Za-z0-9]*)\}")


def load_documents(
    repo_root: Path | str, *, require_roots: bool = False
) -> list[tuple[str, dict | None, str | None]]:
    """Return (relative path, document, error). One of document or error is set.

    The checks pass require_roots=True, so a missing manifest root is an error
    instead of an empty scan. Fixture trees in tests build only the roots they use.
    """
    root = Path(repo_root).resolve()
    found: list[tuple[str, dict | None, str | None]] = []
    for scan_root in SCAN_ROOTS:
        base = root / scan_root.rstrip("/")
        if not base.is_dir():
            if require_roots:
                found.append((scan_root.rstrip("/"), None, "manifest root is missing"))
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if path.suffix not in {".yaml", ".yml"}:
                continue
            rel = path.relative_to(root).as_posix()
            if ".sops." in rel:
                continue
            if rel.startswith("envs/"):
                parts = rel.split("/", 2)
                if len(parts) == 3 and parts[2].startswith(SKIP_ENV_PREFIXES):
                    continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            try:
                docs = list(yaml.safe_load_all(text))
            except yaml.YAMLError as exc:
                found.append((rel, None, f"YAML parse error: {exc}"))
                continue
            for doc in docs:
                if isinstance(doc, dict):
                    found.append((rel, doc, None))
                elif doc is not None:
                    found.append((rel, None, f"YAML document is a {type(doc).__name__}, not a mapping"))
    return found


def source_rel(root: Path, rel: str) -> str:
    """A Flux path in the root artifact, as a path under root.

    The root source includes core at core/. An instance checkout holds the product
    there; in the product checkout, core is the checkout itself.
    """
    rel = rel.strip().removeprefix("./")
    prefix = f"{CORE_SOURCE_DIR}/"
    if rel.startswith(prefix) and not (root / CORE_SOURCE_DIR).is_dir():
        return rel.removeprefix(prefix)
    return rel


def kustomize_build(root: Path, rel: str) -> tuple[list[dict], str | None]:
    """The documents `kustomize build` renders for rel, or an error.

    A directory with no kustomization file gets what Flux generates for it: every
    YAML manifest under it.
    """
    rel = source_rel(root, rel)
    target = root / rel
    if target.is_dir() and not any((target / name).is_file() for name in _KUSTOMIZATION_FILES):
        return _generated(target), None
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


def _generated(target: Path) -> list[dict]:
    docs: list[dict] = []
    for path in sorted(item for item in target.rglob("*") if item.suffix in {".yaml", ".yml"}):
        docs.extend(
            doc
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and "kind" in doc and "apiVersion" in doc
        )
    return docs


def is_flux_kustomization(doc: dict) -> bool:
    return doc.get("kind") == "Kustomization" and str(doc.get("apiVersion", "")).startswith(
        FLUX_KUSTOMIZATION_API
    )


def is_root_kustomization(doc: dict) -> bool:
    metadata = doc.get("metadata") or {}
    return is_flux_kustomization(doc) and (metadata.get("namespace"), metadata.get("name")) == (
        ROOT_SOURCE,
        ROOT_SOURCE,
    )


def postbuild_values(doc: dict, env_docs: list[dict]) -> dict[str, str]:
    """The values a Flux Kustomization's postBuild substitutes, from the env's flux tree."""
    post_build = (doc.get("spec") or {}).get("postBuild") or {}
    values: dict[str, str] = {}
    for source in post_build.get("substituteFrom") or []:
        if not isinstance(source, dict):
            continue
        for item in env_docs:
            if (
                item.get("kind") == source.get("kind")
                and (item.get("metadata") or {}).get("name") == source.get("name")
                and namespace_of(item) == namespace_of(doc)
            ):
                values.update({str(k): str(v) for k, v in (item.get("data") or {}).items()})
    values.update({str(k): str(v) for k, v in (post_build.get("substitute") or {}).items()})
    return values


def substitute[T](node: T, values: dict[str, str]) -> T:
    """node with each ${name} that values holds replaced, as postBuild does: in the YAML text,
    which is then parsed again, so an unquoted false becomes a boolean and an empty value null.
    The result has node's shape (a dict stays a dict, a list a list)."""
    text = yaml.safe_dump(node, width=2**31 - 1)
    return yaml.safe_load(_VARIABLE.sub(lambda match: values.get(match.group(1), match.group(0)), text))


@dataclass(frozen=True)
class FluxPath:
    """What one Flux Kustomization applies. parent is the stage whose path applies it, or None
    for a stage in the env's flux tree. partial: the example env keeps part of it out of Git."""

    kustomization: dict
    parent: dict | None
    docs: list[dict]
    error: str | None
    partial: bool


def flux_paths(root: Path, env_rel: str, env_docs: list[dict]) -> list[FluxPath]:
    """Each stage the env's flux tree applies, then each Flux Kustomization on a stage's path.

    A stage's spec.components are built with its path, as Flux adds them to it. A
    Kustomization on a stage's path gets the stage's postBuild values first, as on the cluster.
    One that reads another source (a hosted app's, from the apps repository) is not in this
    checkout: it is listed with no objects, as partial.
    """
    found: list[FluxPath] = []
    for doc in env_docs:
        if not is_flux_kustomization(doc) or is_root_kustomization(doc):
            continue
        stage = build_flux_path(root, env_rel, doc)
        found.append(stage)
        values = postbuild_values(doc, env_docs)
        for item in stage.docs:
            if is_flux_kustomization(item):
                item = substitute(item, values)
                source = (item.get("spec") or {}).get("sourceRef")
                if isinstance(source, dict) and source.get("name") != ROOT_SOURCE:
                    found.append(FluxPath(item, doc, [], None, True))
                    continue
                found.append(build_flux_path(root, env_rel, item, parent=doc))
    return found


def build_flux_path(root: Path, env_rel: str, doc: dict, *, parent: dict | None = None) -> FluxPath:
    spec = doc.get("spec") or {}
    path = spec.get("path")
    if not isinstance(path, str) or not path.strip():
        return FluxPath(doc, parent, [], "spec.path is unset", False)
    rel = path.strip().removeprefix("./")
    partial = False
    if example_platform_configs(env_rel, path):
        # Only the core list of the example's platform-configs path is in Git.
        rel, partial = f"{CORE_SOURCE_DIR}/platform/configs", True
    elif env_rel.startswith(f"envs/{EXAMPLE_ENV}/") and rel.startswith(f"envs/{EXAMPLE_ENV}/secrets/"):
        # The example env holds no secret files; a bootstrapped copy builds this path.
        return FluxPath(doc, parent, [], None, True)
    docs, error = kustomize_build(root, rel)
    if partial and error:
        # A tree without the core list builds nothing here, as before bootstrap.
        return FluxPath(doc, parent, [], None, True)
    for component in spec.get("components") or []:
        if error:
            break
        more, error = kustomize_build(root, posixpath.join(rel, str(component)))
        docs = docs + more
    return FluxPath(doc, parent, docs, error, partial)


def example_platform_configs(env_rel: str, path: str) -> bool:
    """The example env's platform-configs path lists bootstrap secrets that are not in Git.

    A copy builds this path after bootstrap-secrets. The example tree does not.
    """
    if not env_rel.startswith(f"envs/{EXAMPLE_ENV}/"):
        return False
    return path.strip().removeprefix("./") == f"envs/{EXAMPLE_ENV}/flux/platform-configs"


def namespace_of(doc: dict) -> str:
    metadata = doc.get("metadata")
    namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
    return namespace if isinstance(namespace, str) and namespace else "default"


def kind_of(doc: dict) -> str:
    kind = doc.get("kind")
    return kind if isinstance(kind, str) and kind else "object"


def ref_of(doc: dict) -> str:
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return kind_of(doc)
    name = metadata.get("name")
    name_text = name.strip() if isinstance(name, str) and name.strip() else "?"
    namespace = metadata.get("namespace")
    if isinstance(namespace, str) and namespace.strip():
        return f"{kind_of(doc)} {namespace.strip()}/{name_text}"
    return f"{kind_of(doc)} {name_text}"


def pod_spec(doc: dict) -> dict | None:
    """Pod spec for a workload document, or None when this document is not one."""
    kind = doc.get("kind")
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        spec = {}
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}:
        template = spec.get("template")
        if isinstance(template, dict) and isinstance(template.get("spec"), dict):
            return template["spec"]
        return {}
    if kind == "CronJob":
        job = spec.get("jobTemplate")
        job_spec = job.get("spec") if isinstance(job, dict) else None
        template = job_spec.get("template") if isinstance(job_spec, dict) else None
        if isinstance(template, dict) and isinstance(template.get("spec"), dict):
            return template["spec"]
        return {}
    if kind == "Pod":
        return spec
    return None


def helm_values(doc: dict) -> tuple[dict | None, str | None]:
    """HelmRelease spec.values or HelmChartConfig valuesContent."""
    kind = doc.get("kind")
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None, None
    if kind == "HelmRelease":
        values = spec.get("values")
        if values is None:
            return None, None
        if not isinstance(values, dict):
            return None, "spec.values is not a mapping"
        return values, None
    if kind == "HelmChartConfig":
        raw = spec.get("valuesContent")
        if not isinstance(raw, str) or not raw.strip():
            return None, None
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return None, f"valuesContent YAML parse error: {exc}"
        if loaded is None:
            return None, None
        if not isinstance(loaded, dict):
            return None, "valuesContent is not a mapping"
        return loaded, None
    return None, None
