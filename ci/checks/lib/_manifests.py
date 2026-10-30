"""YAML documents the repo deploys. Charts are not rendered."""

from __future__ import annotations

from pathlib import Path

import yaml

SCAN_ROOTS = ("apps/", "platform/", "services/", "envs/")
SKIP_ENV_PREFIXES = ("secrets/", "values/", "inventory/")


def load_documents(repo_root: Path | str) -> list[tuple[str, dict | None, str | None]]:
    """Return (relative path, document, error). One of document or error is set."""
    root = Path(repo_root).resolve()
    found: list[tuple[str, dict | None, str | None]] = []
    for scan_root in SCAN_ROOTS:
        base = root / scan_root.rstrip("/")
        if not base.is_dir():
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
    return found


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
