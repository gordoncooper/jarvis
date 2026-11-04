"""YAML documents the repo deploys. Charts are not rendered."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

from schema.env import CORE_SOURCE_DIR, EXAMPLE_ENV

SCAN_ROOTS = ("apps/", "platform/", "services/", "envs/")
SKIP_ENV_PREFIXES = ("secrets/", "values/", "inventory/")


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
    """The documents `kustomize build` renders for rel, or an error."""
    rel = source_rel(root, rel)
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
