"""R31: a PVC with no ReplicationSource and no backup: none fails the check."""

from __future__ import annotations

from pathlib import Path

from _pvc_backup import scan_pvc_backup


def test_pvc_without_backup_fails_until_it_has_a_replication_source(tmp_path: Path) -> None:
    pvc = tmp_path / "platform" / "app" / "pvc.yaml"
    pvc.parent.mkdir(parents=True)
    pvc.write_text(
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
  namespace: app
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
""",
        encoding="utf-8",
    )

    violations = scan_pvc_backup(tmp_path)
    assert len(violations) == 1
    assert violations[0].namespace == "app"
    assert violations[0].name == "data"
    assert "ReplicationSource" in violations[0].reason

    source = tmp_path / "platform" / "app" / "source.yaml"
    source.write_text(
        """
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata:
  name: data
  namespace: app
spec:
  sourcePVC: data
""",
        encoding="utf-8",
    )
    assert scan_pvc_backup(tmp_path) == []
