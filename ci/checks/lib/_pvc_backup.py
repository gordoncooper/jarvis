"""R31: the backup ClusterPolicy enrolls PVCs. Git does not write ReplicationSources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from _manifests import load_documents

BACKUP_NONE_ANNOTATION = "backup"
BACKUP_NONE_VALUE = "none"

# Observability volumes may opt out (R31). Anywhere else, backup: none fails.
BACKUP_NONE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "services/observability/victoria-metrics/",
    "services/observability/victoria-logs/",
)

_HANDWRITTEN_ROOTS = ("services/", "apps/")
_POLICY_KUSTOMIZATION = "platform/backups/kustomization.yaml"
_POLICY_NAME = "pvc-backup"
_VOLSYNC_KIND = re.compile(
    r"(?m)^[ \t]*kind:[ \t]*(ReplicationSource|ReplicationDestination)[ \t]*$"
)
_RESTIC_REPOSITORY_URL = re.compile(r"rest:https?://")


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
    """Policy present, opt-out allow-list, and no hand-written backup objects."""
    root = Path(repo_root).resolve()
    violations: list[PvcBackupViolation] = []
    if not _enrollment_policy_listed(root):
        violations.append(
            PvcBackupViolation(
                _POLICY_KUSTOMIZATION,
                "cluster",
                _POLICY_NAME,
                "ClusterPolicy",
                "backup enrollment ClusterPolicy is missing",
            )
        )

    for rel_path, doc, error in load_documents(root):
        if doc is None:
            violations.append(
                PvcBackupViolation(rel_path, "-", "-", "document", error or "unreadable YAML")
            )
            continue
        violations.extend(_document_violations(doc, rel_path))

    violations.extend(_handwritten_violations(root))
    return sorted(
        violations,
        key=lambda item: (item.rel_path, item.namespace, item.name, item.reason),
    )


def format_violations(violations: Iterable[PvcBackupViolation]) -> list[str]:
    return [
        f"{item.rel_path}: {item.origin} {item.namespace}/{item.name}: {item.reason}"
        for item in violations
    ]


def _enrollment_policy_listed(repo_root: Path) -> bool:
    kustomization = repo_root / _POLICY_KUSTOMIZATION
    if not kustomization.is_file():
        return False
    loaded = yaml.safe_load(kustomization.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return False
    resources = loaded.get("resources") or []
    if not isinstance(resources, list):
        return False
    for item in resources:
        if not isinstance(item, str) or "/" in item or item.startswith(".."):
            continue
        path = repo_root / "platform" / "backups" / item
        if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(_is_enrollment_policy(doc) for doc in yaml.safe_load_all(text)):
            return True
    return False


def _is_enrollment_policy(doc: object) -> bool:
    """The named ClusterPolicy with a rule that generates a ReplicationSource."""
    if not isinstance(doc, dict) or doc.get("kind") != "ClusterPolicy" or _name(doc) != _POLICY_NAME:
        return False
    spec = doc.get("spec")
    rules = spec.get("rules") if isinstance(spec, dict) else None
    return isinstance(rules, list) and any(
        isinstance(rule, dict)
        and isinstance(rule.get("generate"), dict)
        and rule["generate"].get("kind") == "ReplicationSource"
        for rule in rules
    )


def _document_violations(doc: dict[str, Any], rel_path: str) -> list[PvcBackupViolation]:
    kind = doc.get("kind")
    if kind == "PersistentVolumeClaim":
        return _backup_none_violation(doc, rel_path, "PersistentVolumeClaim", _name(doc))
    if kind == "ReplicationSource":
        return [
            PvcBackupViolation(
                rel_path,
                _namespace(doc),
                _name(doc) or "-",
                "ReplicationSource",
                "ReplicationSource must not be written in Git",
            )
        ]
    if kind != "StatefulSet":
        return []
    violations: list[PvcBackupViolation] = []
    spec = doc.get("spec") or {}
    for template in spec.get("volumeClaimTemplates") or []:
        if not isinstance(template, dict):
            continue
        template_meta = template.get("metadata") or {}
        template_name = template_meta.get("name")
        if not isinstance(template_name, str) or not template_name:
            continue
        if not has_backup_none(template_meta):
            continue
        violations.extend(
            _backup_none_violation(doc, rel_path, "volumeClaimTemplate", template_name)
        )
    return violations


def _backup_none_violation(
    doc: dict[str, Any],
    rel_path: str,
    origin: str,
    name: str,
) -> list[PvcBackupViolation]:
    if not name:
        return []
    metadata = doc.get("metadata") if origin == "PersistentVolumeClaim" else None
    if origin == "volumeClaimTemplate":
        marked = True
    else:
        marked = has_backup_none(metadata if isinstance(metadata, dict) else None)
    if not marked or backup_none_allowed(rel_path):
        return []
    return [
        PvcBackupViolation(
            rel_path,
            _namespace(doc),
            name,
            origin,
            "backup: none is only allowed for VictoriaMetrics and VictoriaLogs",
        )
    ]


def _handwritten_violations(repo_root: Path) -> list[PvcBackupViolation]:
    violations: list[PvcBackupViolation] = []
    for prefix in _HANDWRITTEN_ROOTS:
        base = repo_root / prefix.rstrip("/")
        if not base.is_dir():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            rel = path.relative_to(repo_root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for kind in sorted(set(_VOLSYNC_KIND.findall(text))):
                violations.append(
                    PvcBackupViolation(
                        rel,
                        "-",
                        kind,
                        "file",
                        f"hand-written {kind}",
                    )
                )
            if _RESTIC_REPOSITORY_URL.search(text):
                violations.append(
                    PvcBackupViolation(
                        rel,
                        "-",
                        "RESTIC_REPOSITORY",
                        "file",
                        "hand-written restic repository URL",
                    )
                )
    return violations


def _name(doc: dict[str, Any]) -> str:
    name = (doc.get("metadata") or {}).get("name")
    if isinstance(name, str):
        return name.strip()
    return ""


def _namespace(doc: dict[str, Any]) -> str:
    namespace = (doc.get("metadata") or {}).get("namespace")
    if isinstance(namespace, str) and namespace.strip():
        return namespace.strip()
    return "default"
