"""pvc-backup CI check (p0a-09)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from _pvc_backup import scan_pvc_backup

REPO_ROOT = Path(__file__).resolve().parents[2]
PVC_BACKUP_CHECK = REPO_ROOT / "tools" / "checks" / "pvc-backup"


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_repo_tree_passes_pvc_backup_check():
    assert scan_pvc_backup(REPO_ROOT) == []


def test_pvc_backup_check_executable_passes_in_repo():
    subprocess.run([str(PVC_BACKUP_CHECK)], cwd=REPO_ROOT, check=True)


def test_pvc_with_matching_replication_source_passes(tmp_path: Path):
    _write(
        tmp_path,
        "platform/backups/pvc.yaml",
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
  namespace: platform-test
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
""",
    )
    _write(
        tmp_path,
        "platform/backups/source.yaml",
        """
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata:
  name: data-backup
  namespace: platform-test
spec:
  sourcePVC: data
""",
    )
    assert scan_pvc_backup(tmp_path) == []


def test_observability_pvc_with_backup_none_passes(tmp_path: Path):
    _write(
        tmp_path,
        "services/observability/victoria-metrics/storage.yaml",
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: vmstorage
  namespace: observability
  annotations:
    jarvis.io/backup: none
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
""",
    )
    assert scan_pvc_backup(tmp_path) == []


def test_pvc_without_backup_fails(tmp_path: Path):
    _write(
        tmp_path,
        "platform/backups/pvc.yaml",
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: lonely
  namespace: platform-test
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
""",
    )
    violations = scan_pvc_backup(tmp_path)
    assert len(violations) == 1
    assert "missing VolSync" in violations[0].reason


def test_backup_none_outside_allow_list_fails(tmp_path: Path):
    _write(
        tmp_path,
        "platform/backups/pvc.yaml",
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: hermes-data
  namespace: hermes-ops
  annotations:
    jarvis.io/backup: none
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 5Gi
""",
    )
    violations = scan_pvc_backup(tmp_path)
    assert len(violations) == 1
    assert "VictoriaMetrics" in violations[0].reason


def test_replication_source_wrong_namespace_fails(tmp_path: Path):
    _write(
        tmp_path,
        "platform/backups/pvc.yaml",
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
  namespace: app-a
""",
    )
    _write(
        tmp_path,
        "platform/backups/source.yaml",
        """
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata:
  name: data-backup
  namespace: app-b
spec:
  sourcePVC: data
""",
    )
    violations = scan_pvc_backup(tmp_path)
    assert len(violations) == 1
