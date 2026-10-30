"""Flux prune exemption for objects a path-move race must not delete.

kustomize-controller reads kustomize.toolkit.fluxcd.io/prune off the live
object and skips garbage collection when the value is disabled. The check
builds the Flux Kustomization paths, which is what the controller applies.
"""

from __future__ import annotations

import pytest
import yaml

from ansible_role_proof import REPO_ROOT, non_example_env_names
from test_flux_entry import _kustomize_build

_PRUNE = "kustomize.toolkit.fluxcd.io/prune"

# (kind, namespace, name). Namespace is empty for cluster-scoped objects.
# A guarded Namespace does not cover the HelmRelease. Uninstall of kyverno
# or volsync deletes chart-templated CRDs and every object of those kinds.
_PROTECTED = {
    ("ClusterRole", "", "kyverno:backup-generate"),
    ("ClusterRole", "", "kyverno:backup-namespaces"),
    ("ConfigMap", "kube-system", "coredns-custom"),
    ("CustomResourceDefinition", "", "volumesnapshots.snapshot.storage.k8s.io"),
    ("Deployment", "platform-backups", "restic"),
    ("HelmChartConfig", "kube-system", "traefik"),
    ("HelmRelease", "flux-system", "cert-manager"),
    ("HelmRelease", "flux-system", "flux-operator"),
    ("HelmRelease", "flux-system", "k8s-gateway"),
    ("HelmRelease", "flux-system", "kyverno"),
    ("HelmRelease", "flux-system", "volsync"),
    ("HelmRepository", "flux-system", "backube"),
    ("HelmRepository", "flux-system", "jetstack"),
    ("HelmRepository", "flux-system", "k8s-gateway"),
    ("HelmRepository", "flux-system", "kyverno"),
    ("Namespace", "", "kyverno"),
    ("Namespace", "", "platform-app-dns"),
    ("Namespace", "", "platform-cert-manager"),
    ("Namespace", "", "platform-backups"),
    ("Namespace", "", "volsync"),
    ("OCIRepository", "flux-system", "flux-operator"),
}

# Listed with the other guards. The annotation is an owner run (D17): the
# SOPS MAC needs the age key, so the file is not edited here.
_OWNER_RUN = {
    ("Secret", "platform-backups", "backups-restic"),
}
_OWNER_RUN_REASON = (
    "Owner run (D17): add kustomize.toolkit.fluxcd.io/prune: disabled "
    "under metadata in envs/<env>/secrets/backups-restic.sops.yaml. "
    "The SOPS MAC needs the age key."
)


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


def _applied(env: str) -> dict[tuple[str, str, str], dict]:
    applied: dict[tuple[str, str, str], dict] = {}
    for name in ("platform-controllers", "platform-configs"):
        applied.update(_index(_kustomize_build(_flux_build_path(env, name))))
    return applied


def test_kustomize_build_keeps_prune_disabled_on_guarded_objects() -> None:
    envs = non_example_env_names()
    assert envs
    for env in envs:
        applied = _applied(env)
        missing = sorted(key for key in _PROTECTED if key not in applied)
        assert missing == [], missing
        for key in _PROTECTED:
            annotations = applied[key]["metadata"].get("annotations") or {}
            assert annotations.get(_PRUNE) == "disabled", key


def test_backups_restic_secret_is_listed_for_prune_guard() -> None:
    envs = non_example_env_names()
    assert envs
    for env in envs:
        applied = _applied(env)
        missing = sorted(key for key in _OWNER_RUN if key not in applied)
        assert missing == [], missing
        for key in _OWNER_RUN:
            annotations = applied[key]["metadata"].get("annotations") or {}
            if annotations.get(_PRUNE) != "disabled":
                pytest.xfail(_OWNER_RUN_REASON)
            assert annotations.get(_PRUNE) == "disabled", key
