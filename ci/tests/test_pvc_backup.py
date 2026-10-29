"""R31: enrollment is the ClusterPolicy. Git does not carry per-PVC backup objects."""

from __future__ import annotations

from pathlib import Path

from _pvc_backup import scan_pvc_backup

_POLICY = """\
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: pvc-backup
spec:
  rules:
    - name: replication-source
      generate:
        kind: ReplicationSource
"""

_PVC = """\
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
"""


def _policy(root: Path) -> None:
    dest = root / "platform" / "backups"
    dest.mkdir(parents=True)
    (dest / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - enrollment.yaml\n",
        encoding="utf-8",
    )
    (dest / "enrollment.yaml").write_text(_POLICY, encoding="utf-8")


def _reasons(root: Path) -> list[str]:
    return [item.reason for item in scan_pvc_backup(root)]


def test_pvc_without_a_replication_source_passes_when_the_policy_is_present(tmp_path: Path) -> None:
    _policy(tmp_path)
    pvc = tmp_path / "platform" / "app" / "pvc.yaml"
    pvc.parent.mkdir(parents=True)
    pvc.write_text(_PVC, encoding="utf-8")
    assert scan_pvc_backup(tmp_path) == []


def test_statefulset_needs_no_per_ordinal_replication_source(tmp_path: Path) -> None:
    _policy(tmp_path)
    sts = tmp_path / "services" / "app" / "sts.yaml"
    sts.parent.mkdir(parents=True)
    sts.write_text(
        """
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
  namespace: app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: db
  template:
    metadata:
      labels:
        app: db
    spec:
      containers:
        - name: db
          image: example
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 1Gi
""",
        encoding="utf-8",
    )
    assert scan_pvc_backup(tmp_path) == []


def test_missing_policy_fails(tmp_path: Path) -> None:
    pvc = tmp_path / "platform" / "app" / "pvc.yaml"
    pvc.parent.mkdir(parents=True)
    pvc.write_text(_PVC, encoding="utf-8")
    reasons = _reasons(tmp_path)
    assert reasons == ["backup enrollment ClusterPolicy is missing"]


def test_backup_none_outside_the_allow_list_fails(tmp_path: Path) -> None:
    _policy(tmp_path)
    pvc = tmp_path / "platform" / "app" / "pvc.yaml"
    pvc.parent.mkdir(parents=True)
    pvc.write_text(
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
  namespace: app
  annotations:
    backup: none
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
    assert "VictoriaMetrics" in violations[0].reason


def test_backup_none_under_the_allow_list_passes(tmp_path: Path) -> None:
    _policy(tmp_path)
    pvc = tmp_path / "services" / "observability" / "victoria-metrics" / "pvc.yaml"
    pvc.parent.mkdir(parents=True)
    pvc.write_text(
        """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
  namespace: observe
  annotations:
    backup: none
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
""",
        encoding="utf-8",
    )
    assert scan_pvc_backup(tmp_path) == []


def test_hand_written_replication_source_fails(tmp_path: Path) -> None:
    _policy(tmp_path)
    source = tmp_path / "platform" / "app" / "source.yaml"
    source.parent.mkdir(parents=True)
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
    assert any("must not be written in Git" in reason for reason in _reasons(tmp_path))


def test_services_file_with_a_replication_source_fails(tmp_path: Path) -> None:
    _policy(tmp_path)
    source = tmp_path / "services" / "app" / "backup.txt"
    source.parent.mkdir(parents=True)
    source.write_text("kind: ReplicationSource\n", encoding="utf-8")
    assert "hand-written ReplicationSource" in _reasons(tmp_path)


def test_apps_file_with_a_replication_destination_fails(tmp_path: Path) -> None:
    _policy(tmp_path)
    dest = tmp_path / "apps" / "demo" / "restore.yaml"
    dest.parent.mkdir(parents=True)
    dest.write_text(
        "apiVersion: volsync.backube/v1alpha1\nkind: ReplicationDestination\n",
        encoding="utf-8",
    )
    assert "hand-written ReplicationDestination" in _reasons(tmp_path)


def test_services_file_with_a_restic_repository_url_fails(tmp_path: Path) -> None:
    _policy(tmp_path)
    note = tmp_path / "services" / "app" / "repo.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "RESTIC_REPOSITORY=rest:http://restic.example/ns-app/pvc-data\n",
        encoding="utf-8",
    )
    assert "hand-written restic repository URL" in _reasons(tmp_path)


def test_nested_generate_kind_in_the_policy_is_not_a_git_replication_source(tmp_path: Path) -> None:
    _policy(tmp_path)
    assert scan_pvc_backup(tmp_path) == []
