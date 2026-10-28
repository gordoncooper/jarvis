"""OQ7 test bundle takes its restic repo directory from cluster-vars."""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from ansible_role_proof import REPO_ROOT, load_cluster_vars, non_example_env_names


def _kustomize_build(rel_path: str) -> str:
    kustomize = shutil.which("kustomize")
    if not kustomize:
        pytest.fail("kustomize not found on PATH (run make deps)")
    proc = subprocess.run(
        [
            kustomize,
            "build",
            "--load-restrictor",
            "LoadRestrictionsNone",
            str(REPO_ROOT / rel_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def test_backup_test_repo_path_comes_from_cluster_vars() -> None:
    rendered = _kustomize_build("platform/backups/test/prep")
    host_paths = [
        doc["spec"]["hostPath"]["path"]
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict) and doc.get("kind") == "PersistentVolume"
    ]
    assert host_paths == ["${backup_path}/volsync-test"]
    for env in non_example_env_names():
        backup_path = load_cluster_vars(env=env)["backup_path"]
        assert isinstance(backup_path, str) and backup_path
        assert backup_path not in rendered
