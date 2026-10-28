"""R31: every PVC has a VolSync ReplicationSource or backup: none."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

BACKUP_NONE_ANNOTATION = "backup"
BACKUP_NONE_VALUE = "none"

# Observability volumes may opt out (R31). Anywhere else, backup: none fails.
BACKUP_NONE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "services/observability/victoria-metrics/",
    "services/observability/victoria-logs/",
)

_SCAN_ROOTS = ("apps/", "platform/", "services/", "envs/")
_SKIP_ENV_PREFIXES = ("secrets/", "values/", "inventory/")


@dataclass(frozen=True)
class PvcRef:
    rel_path: str
    namespace: str
    name: str
    origin: str
    backup_none: bool


@dataclass(frozen=True)
class PvcBackupViolation:
    rel_path: str
    namespace: str
    name: str
    origin: str
    reason: str


def backup_none_allowed(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    return any(normalized.startswith(prefix) for prefix in BACKUP_NONE_ALLOWED_PREFIXES)


def has_backup_none(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    annotations = metadata.get("annotations") or {}
    if not isinstance(annotations, dict):
        return False
    value = annotations.get(BACKUP_NONE_ANNOTATION)
    return isinstance(value, str) and value.strip().lower() == BACKUP_NONE_VALUE


def scan_pvc_backup(repo_root: Path | str) -> list[PvcBackupViolation]:
    """Return PVCs with no ReplicationSource and no allowed backup: none mark."""
    root = Path(repo_root).resolve()
    pvcs: list[PvcRef] = []
    sources: set[tuple[str, str]] = set()

    for rel_path in _manifest_yaml_files(root):
        try:
            text = (root / rel_path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                _collect(doc, rel_path, pvcs, sources)

    violations: list[PvcBackupViolation] = []
    for pvc in pvcs:
        if pvc.backup_none:
            if not backup_none_allowed(pvc.rel_path):
                violations.append(
                    PvcBackupViolation(
                        pvc.rel_path,
                        pvc.namespace,
                        pvc.name,
                        pvc.origin,
                        "backup: none is only allowed for VictoriaMetrics and VictoriaLogs",
                    )
                )
            continue
        if (pvc.namespace, pvc.name) not in sources:
            violations.append(
                PvcBackupViolation(
                    pvc.rel_path,
                    pvc.namespace,
                    pvc.name,
                    pvc.origin,
                    "missing ReplicationSource (spec.sourcePVC) and backup: none",
                )
            )

    return sorted(
        violations,
        key=lambda item: (item.rel_path, item.namespace, item.name, item.reason),
    )


def format_violations(violations: Iterable[PvcBackupViolation]) -> list[str]:
    return [
        f"{item.rel_path}: {item.origin} {item.namespace}/{item.name}: {item.reason}"
        for item in violations
    ]


def _collect(
    doc: dict[str, Any],
    rel_path: str,
    pvcs: list[PvcRef],
    sources: set[tuple[str, str]],
) -> None:
    kind = doc.get("kind")
    if kind == "PersistentVolumeClaim":
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        if isinstance(name, str) and name:
            pvcs.append(
                PvcRef(
                    rel_path,
                    _namespace(doc),
                    name,
                    "PersistentVolumeClaim",
                    has_backup_none(meta),
                )
            )
        return
    if kind == "ReplicationSource":
        source_pvc = (doc.get("spec") or {}).get("sourcePVC")
        if isinstance(source_pvc, str) and source_pvc.strip():
            sources.add((_namespace(doc), source_pvc.strip()))
        return
    if kind != "StatefulSet":
        return
    meta = doc.get("metadata") or {}
    sts_name = meta.get("name")
    if not isinstance(sts_name, str) or not sts_name:
        return
    spec = doc.get("spec") or {}
    replicas = spec.get("replicas", 1)
    if not isinstance(replicas, int) or replicas < 0:
        replicas = 1
    namespace = _namespace(doc)
    for template in spec.get("volumeClaimTemplates") or []:
        if not isinstance(template, dict):
            continue
        template_meta = template.get("metadata") or {}
        template_name = template_meta.get("name")
        if not isinstance(template_name, str) or not template_name:
            continue
        marked_none = has_backup_none(template_meta)
        for ordinal in range(replicas):
            pvcs.append(
                PvcRef(
                    rel_path,
                    namespace,
                    f"{template_name}-{sts_name}-{ordinal}",
                    "volumeClaimTemplate",
                    marked_none,
                )
            )


def _namespace(doc: dict[str, Any]) -> str:
    namespace = (doc.get("metadata") or {}).get("namespace")
    if isinstance(namespace, str) and namespace.strip():
        return namespace.strip()
    return "default"


def _manifest_yaml_files(repo_root: Path) -> list[str]:
    paths: list[str] = []
    for scan_root in _SCAN_ROOTS:
        base = repo_root / scan_root.rstrip("/")
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
                continue
            rel = path.relative_to(repo_root).as_posix()
            if rel.endswith(".sops.yaml") or ".sops." in rel:
                continue
            if rel.startswith("envs/"):
                rest = rel.split("/", 2)
                if len(rest) == 3 and rest[2].startswith(_SKIP_ENV_PREFIXES):
                    continue
            paths.append(rel)
    return paths
