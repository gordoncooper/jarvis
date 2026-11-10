"""YAML documents the repo deploys. Charts are not rendered."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

from jarvis.checks.flux_build import FluxPath, env_tree
from jarvis.checks.flux_build import is_flux_kustomization as is_flux_kustomization  # re-export
from jarvis.schema.env import ADDONS_DIR, CORE_SOURCE_DIR, ROOT_SOURCE, core_dir

# The trees the root artifact takes from the core repository.
_CORE_TREES = (CORE_SOURCE_DIR, ADDONS_DIR)

SCAN_ROOTS = ("core/", "addons/", "envs/")
# Core holds core/ and addons/ and no env; an instance repo holds envs/. require_roots requires these.
REQUIRED_ROOTS = ("core/", "addons/")
SKIP_ENV_PREFIXES = ("secrets/", "values/", "inventory/")


def load_documents(
    repo_root: Path | str, *, require_roots: bool = False
) -> list[tuple[str, dict | None, str | None]]:
    """Return (relative path, document, error). One of document or error is set.

    The checks pass require_roots=True, so a missing core/ or addons/ is an error instead of an
    empty scan; envs/ is scanned when present. Fixture trees in tests build only the roots they use.
    """
    root = Path(repo_root).resolve()
    found: list[tuple[str, dict | None, str | None]] = []
    for scan_root in SCAN_ROOTS:
        base = root / scan_root.rstrip("/")
        if not base.is_dir():
            if require_roots and scan_root in REQUIRED_ROOTS:
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


def kustomize_build(root: Path, rel: str) -> tuple[list[dict], str | None]:
    """The documents `kustomize build` renders for one of core's folders (core/... or addons/...)
    on its own, before any Flux Kustomization applies it: an add-on's wiring, the admission
    policies. What an env applies is Flux's build (flux_build.env_tree), never this."""
    rel = rel.strip().removeprefix("./")
    if rel.split("/", 1)[0] not in _CORE_TREES:
        return [], f"{rel} is not one of core's trees ({', '.join(_CORE_TREES)})"
    kustomize = shutil.which("kustomize")
    if kustomize is None:
        return [], "kustomize not found on PATH (run jarvis doctor)"
    target = core_dir(root) / rel
    proc = subprocess.run([kustomize, "build", str(target)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return [], f"kustomize build {rel} failed: {(proc.stderr or proc.stdout).strip()}"
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)], None


def core_files(root: Path, rel: str) -> tuple[list[dict], str | None]:
    """The documents in the YAML files of one of core's folders, as written: for files a
    Component lists (the app-hosting policies), which kustomize cannot build on their own."""
    rel = rel.strip().removeprefix("./")
    if rel.split("/", 1)[0] not in _CORE_TREES:
        return [], f"{rel} is not one of core's trees ({', '.join(_CORE_TREES)})"
    target = core_dir(root) / rel
    if not target.is_dir():
        return [], f"{rel} is not a folder"
    docs: list[dict] = []
    for path in sorted(target.glob("*.yaml")):
        try:
            docs.extend(doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict))
        except (OSError, yaml.YAMLError) as exc:
            return [], f"{rel}/{path.name}: {exc}"
    return docs, None


def env_flux_docs(root: Path, env_rel: str) -> tuple[list[dict], str | None]:
    """What the env's root Kustomization applies, as Flux builds it (flux_build)."""
    tree = env_tree(root, env_rel)
    return tree.env_docs, tree.error


def flux_paths(root: Path, env_rel: str, env_docs: list[dict] | None = None) -> list[FluxPath]:
    """Every Flux Kustomization under the env's root, each with the objects Flux builds for it
    (substituted). env_docs is accepted for the callers that already hold it."""
    return env_tree(root, env_rel).paths


def is_root_kustomization(doc: dict) -> bool:
    metadata = doc.get("metadata") or {}
    return is_flux_kustomization(doc) and (metadata.get("namespace"), metadata.get("name")) == (
        ROOT_SOURCE,
        ROOT_SOURCE,
    )


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
