"""The manifest checks fail when a manifest root is missing, instead of scanning nothing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from _digest_pin import scan_digest_pin
from _loadbalancer import scan_loadbalancer
from _manifest_lint import scan_manifest_lint
from _pvc_backup import scan_pvc_backup
from support import REPO_ROOT

_SCANS = {
    "digest-pin": scan_digest_pin,
    "loadbalancer": scan_loadbalancer,
    "manifest-lint": scan_manifest_lint,
    "pvc-backup": scan_pvc_backup,
}

_POLICY = """\
apiVersion: policies.kyverno.io/v1
kind: GeneratingPolicy
metadata:
  name: pvc-backup
spec:
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: [v1]
        operations: [CREATE, UPDATE]
        resources: [persistentvolumeclaims]
  variables:
    - name: source
      expression: '{"kind": "ReplicationSource"}'
"""


def _tree_with_platform_only(root: Path) -> None:
    """A tree whose only manifest root is platform/, with a valid backup policy."""
    backups = root / "platform" / "backups"
    backups.mkdir(parents=True)
    (backups / "kustomization.yaml").write_text("resources:\n  - enrollment.yaml\n", encoding="utf-8")
    (backups / "enrollment.yaml").write_text(_POLICY, encoding="utf-8")


@pytest.mark.parametrize("name", sorted(_SCANS))
def test_required_roots_fail_when_a_manifest_root_is_missing(tmp_path: Path, name: str) -> None:
    _tree_with_platform_only(tmp_path)
    reasons = {
        (item.rel_path if hasattr(item, "rel_path") else item.path): item
        for item in _SCANS[name](tmp_path, require_roots=True)
    }
    assert {"apps", "services", "envs"} <= set(reasons)
    assert "platform" not in reasons


@pytest.mark.parametrize("name", sorted(_SCANS))
def test_the_check_script_requires_every_manifest_root(tmp_path: Path, name: str) -> None:
    """The shipped check passes require_roots, so a tree missing apps/, services/ or envs/ fails."""
    _tree_with_platform_only(tmp_path)
    shutil.copytree(REPO_ROOT / "ci" / "checks", tmp_path / "ci" / "checks")
    # The check libraries import schema.env, which the scripts find under the repo root,
    # and schema.env imports the INSTANCE resolver from tools/lib.
    for rel in ("schema", "tools/lib"):
        shutil.copytree(REPO_ROOT / rel, tmp_path / rel, ignore=shutil.ignore_patterns("__pycache__"))
    proc = subprocess.run(
        ["bash", str(tmp_path / "ci" / "checks" / name)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "manifest root is missing" in proc.stdout
