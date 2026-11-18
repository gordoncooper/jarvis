"""Small readers for Kubernetes documents, and `kustomize build` of one of core's own folders."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

from jarvis.schema.env import ADDONS_DIR, CORE_SOURCE_DIR

# The trees the root artifact takes from the core repository.
_CORE_TREES = (CORE_SOURCE_DIR, ADDONS_DIR)


def kustomize_build(core: Path, rel: str) -> tuple[list[dict], str | None]:
    """The documents `kustomize build` renders for one of core's own folders (core/... or
    addons/...) on its own, before any Flux Kustomization applies it: an add-on's wiring, the apps
    rest-server. What an env applies is Flux's build (flux_build.env_tree), never this."""
    rel = rel.strip().removeprefix("./")
    if rel.split("/", 1)[0] not in _CORE_TREES:
        return [], f"{rel} is not one of core's trees ({', '.join(_CORE_TREES)})"
    kustomize = shutil.which("kustomize")
    if kustomize is None:
        return [], "kustomize not found on PATH (run jarvis doctor)"
    proc = subprocess.run([kustomize, "build", str(core / rel)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return [], f"kustomize build {rel} failed: {(proc.stderr or proc.stdout).strip()}"
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)], None


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
