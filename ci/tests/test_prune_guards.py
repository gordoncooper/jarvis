"""Flux prune exemption for objects a path-move race must not delete.

kustomize-controller reads kustomize.toolkit.fluxcd.io/prune off the live
object and skips garbage collection when the value is disabled. The check
builds the Flux Kustomization paths, which is what the controller applies.
"""

from __future__ import annotations

import yaml

from ansible_role_proof import REPO_ROOT, non_example_env_names
from test_flux_entry import _kustomize_build

_PRUNE = "kustomize.toolkit.fluxcd.io/prune"

# (kind, namespace, name). Namespace is empty for cluster-scoped objects.
# These are the objects whose deletion costs data or wedges recovery.
_PROTECTED = {
    ("CustomResourceDefinition", "", "volumesnapshots.snapshot.storage.k8s.io"),
    ("Deployment", "platform-backups", "restic"),
    ("Namespace", "", "kyverno"),
    ("Namespace", "", "platform-backups"),
    ("Namespace", "", "volsync"),
}


def _flux_build_path(env: str, name: str) -> str:
    path = REPO_ROOT / "envs" / env / "flux" / "kustomizations" / f"{name}.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec_path = doc["spec"]["path"]
    assert isinstance(spec_path, str) and spec_path.startswith("./")
    return spec_path[2:]


def _index(manifests: str) -> dict[tuple[str, str, str], dict]:
    found: dict[tuple[str, str, str], dict] = {}
    for doc in yaml.safe_load_all(manifests):
        if not isinstance(doc, dict) or "kind" not in doc:
            continue
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            continue
        key = (
            str(doc["kind"]),
            str(metadata.get("namespace") or ""),
            str(metadata["name"]),
        )
        found[key] = doc
    return found


def test_costly_objects_keep_prune_disabled() -> None:
    envs = non_example_env_names()
    assert envs
    for env in envs:
        applied: dict[tuple[str, str, str], dict] = {}
        for name in ("platform-controllers", "platform-configs"):
            applied.update(_index(_kustomize_build(_flux_build_path(env, name))))
        missing = sorted(key for key in _PROTECTED if key not in applied)
        assert missing == [], missing
        for key in _PROTECTED:
            annotations = applied[key]["metadata"].get("annotations") or {}
            assert annotations.get(_PRUNE) == "disabled", key
