"""R31: every PVC must have a VolSync ReplicationSource or a backup:none mark."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

# Annotation on the PVC (or volumeClaimTemplate metadata) marking rebuildable storage.
BACKUP_NONE_ANNOTATION = "jarvis.io/backup"
BACKUP_NONE_VALUE = "none"

# §6 observability components that may use backup:none (R31).
BACKUP_NONE_ALLOWED_PATH_PREFIXES: tuple[str, ...] = (
    "services/observability/victoria-metrics/",
    "services/observability/victoria-logs/",
)

_SCAN_ROOTS = ("platform/", "services/", "envs/")
_SKIP_UNDER_ENVS = (
    "secrets/",
    "values/",
    "inventory/",
)


@dataclass(frozen=True)
class PvcRef:
    """A PVC declared in Git (standalone or volumeClaimTemplate)."""

    rel_path: str
    namespace: str
    name: str
    origin: str  # "PersistentVolumeClaim" or "volumeClaimTemplate"
    backup_none: bool


@dataclass(frozen=True)
class PvcBackupViolation:
    rel_path: str
    namespace: str
    name: str
    origin: str
    reason: str


def backup_none_allowed_for_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    return any(
        normalized.startswith(prefix) for prefix in BACKUP_NONE_ALLOWED_PATH_PREFIXES
    )


def has_backup_none_mark(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    annotations = metadata.get("annotations") or {}
    if not isinstance(annotations, dict):
        return False
    value = annotations.get(BACKUP_NONE_ANNOTATION)
    return isinstance(value, str) and value.strip().lower() == BACKUP_NONE_VALUE


def scan_pvc_backup(repo_root: Path | str) -> list[PvcBackupViolation]:
    """Return violations for PVCs missing backup coverage or misusing backup:none."""
    root = Path(repo_root).resolve()
    pvcs: list[PvcRef] = []
    backup_sources: list[tuple[str, str]] = []

    for rel_path in _manifest_yaml_files(root):
        abs_path = root / rel_path
        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            _collect_from_doc(doc, rel_path, pvcs, backup_sources)

    source_set = frozenset(backup_sources)
    violations: list[PvcBackupViolation] = []

    for pvc in pvcs:
        if pvc.backup_none:
            if not backup_none_allowed_for_path(pvc.rel_path):
                violations.append(
                    PvcBackupViolation(
                        pvc.rel_path,
                        pvc.namespace,
                        pvc.name,
                        pvc.origin,
                        "backup:none only allowed for VictoriaMetrics and VictoriaLogs",
                    ),
                )
            continue

        key = (pvc.namespace, pvc.name)
        if key not in source_set:
            violations.append(
                PvcBackupViolation(
                    pvc.rel_path,
                    pvc.namespace,
                    pvc.name,
                    pvc.origin,
                    "missing VolSync ReplicationSource (spec.sourcePVC) and backup:none mark",
                ),
            )

    return sorted(
        violations,
        key=lambda v: (v.rel_path, v.namespace, v.name, v.reason),
    )


def _collect_from_doc(
    doc: dict[str, Any],
    rel_path: str,
    pvcs: list[PvcRef],
    backup_sources: list[tuple[str, str]],
) -> None:
    kind = doc.get("kind")
    if kind == "PersistentVolumeClaim":
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        if isinstance(name, str) and name:
            pvcs.append(
                PvcRef(
                    rel_path,
                    _doc_namespace(doc),
                    name,
                    "PersistentVolumeClaim",
                    has_backup_none_mark(meta),
                ),
            )
    elif kind == "ReplicationSource":
        spec = doc.get("spec") or {}
        source_pvc = spec.get("sourcePVC")
        if isinstance(source_pvc, str) and source_pvc.strip():
            backup_sources.append((_doc_namespace(doc), source_pvc.strip()))
    elif kind == "StatefulSet":
        ns = _doc_namespace(doc)
        for tpl in (doc.get("spec") or {}).get("volumeClaimTemplates") or []:
            if not isinstance(tpl, dict):
                continue
            tpl_meta = tpl.get("metadata") or {}
            tpl_name = tpl_meta.get("name")
            if isinstance(tpl_name, str) and tpl_name:
                pvcs.append(
                    PvcRef(
                        rel_path,
                        ns,
                        tpl_name,
                        "volumeClaimTemplate",
                        has_backup_none_mark(tpl_meta),
                    ),
                )


def _doc_namespace(doc: dict[str, Any]) -> str:
    meta = doc.get("metadata") or {}
    ns = meta.get("namespace")
    if isinstance(ns, str) and ns.strip():
        return ns.strip()
    return "default"


def _manifest_yaml_files(repo_root: Path) -> list[str]:
    paths: list[str] = []
    for scan_root in _SCAN_ROOTS:
        base = repo_root / scan_root.rstrip("/")
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in (".yaml", ".yml"):
                continue
            rel = path.relative_to(repo_root).as_posix()
            if rel.startswith("envs/"):
                rest = rel[len("envs/") :]
                if "/" in rest:
                    segment = rest.split("/", 1)[1]
                    if any(segment.startswith(skip) for skip in _SKIP_UNDER_ENVS):
                        continue
            paths.append(rel)
    return paths


def format_violations(violations: Iterable[PvcBackupViolation]) -> list[str]:
    lines: list[str] = []
    for v in violations:
        loc = f"{v.namespace}/{v.name}"
        lines.append(f"{v.rel_path}: {v.origin} {loc}: {v.reason}")
    return lines
