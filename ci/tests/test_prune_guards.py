"""Flux prune exemption for objects a path-move race must not delete.

kustomize-controller reads kustomize.toolkit.fluxcd.io/prune off the live
object and skips garbage collection when the value is disabled. The check
builds the platform-controllers and platform-configs paths.

Every object of a protected kind on those paths must carry prune: disabled.
An exception needs a reason. Objects already guarded that are not one of
those kinds stay guarded.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
import yaml

from ansible_role_proof import REPO_ROOT, non_example_env_names
from test_flux_entry import _kustomize_build

_PRUNE = "kustomize.toolkit.fluxcd.io/prune"
_Key = tuple[str, str, str]

_PROTECTED_KINDS = frozenset(
    {
        "Namespace",
        "CustomResourceDefinition",
        "Secret",
        "HelmRelease",
        "HelmRepository",
        "OCIRepository",
        "ClusterPolicy",
        "ClusterIssuer",
    }
)

# Opt out only with a reason. The testapp canary stores nothing, so prune stays on.
_EXCEPTIONS: dict[_Key, str] = {
    ("Namespace", "", "platform-testapp"): (
        "testapp canary; the fixture stores nothing, so prune stays on"
    ),
}

# Guarded before the kind rule, and not a protected kind.
_KEPT = frozenset(
    {
        ("ClusterRole", "", "kyverno:backup-generate"),
        ("ClusterRole", "", "kyverno:backup-namespaces"),
        ("ConfigMap", "kube-system", "coredns-custom"),
        ("Deployment", "platform-backups", "restic"),
        ("HelmChartConfig", "kube-system", "traefik"),
    }
)

# One owner re-seal. The pending envs are the ones that still have this file.
# The re-seal PR deletes the file; the kind rule then guards the CA, and
# dropping the annotation fails hard. Any other env's unguarded CA fails now.
_PENDING_CA: _Key = ("Secret", "platform-cert-manager", "cert-manager-ca")
_PENDING_FILE = "pending-prune.yaml"
_PENDING_OWNER_REASON = (
    "Owner run (D17): add kustomize.toolkit.fluxcd.io/prune: disabled "
    "under metadata on the pending CA secret, "
    "then delete envs/<env>/values/pending-prune.yaml in that same change. "
    "The SOPS MAC needs the age key."
)


def _pending_owner() -> set[tuple[str, _Key]]:
    pending: set[tuple[str, _Key]] = set()
    for env in non_example_env_names():
        path = REPO_ROOT / "envs" / env / "values" / _PENDING_FILE
        if not path.is_file():
            continue
        rows = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not rows:
            raise AssertionError(f"{path.name}: expected a non-empty list")
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != 3
                or not all(isinstance(part, str) for part in row)
            ):
                raise AssertionError(f"{path.name}: each row is kind, namespace, name")
            pending.add((env, (row[0], row[1], row[2])))
    return pending


_PENDING_OWNER = _pending_owner()


def _flux_build_path(env: str, name: str) -> str:
    path = REPO_ROOT / "envs" / env / "flux" / "kustomizations" / f"{name}.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec_path = doc["spec"]["path"]
    assert isinstance(spec_path, str) and spec_path.startswith("./")
    return spec_path[2:]


def _index(manifests: str) -> dict[_Key, dict]:
    found: dict[_Key, dict] = {}
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


def _applied(env: str) -> dict[_Key, dict]:
    applied: dict[_Key, dict] = {}
    for name in ("platform-controllers", "platform-configs"):
        applied.update(_index(_kustomize_build(_flux_build_path(env, name))))
    return applied


def _prune_disabled(doc: dict) -> bool:
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return False
    annotations = metadata.get("annotations") or {}
    return isinstance(annotations, dict) and annotations.get(_PRUNE) == "disabled"


def _unguarded(
    applied: Mapping[_Key, dict],
    *,
    exceptions: Mapping[_Key, str],
    skip: set[_Key],
) -> tuple[list[_Key], list[_Key]]:
    unexplained = sorted(key for key, reason in exceptions.items() if not str(reason).strip())
    if unexplained:
        raise AssertionError(f"unexplained exception: {unexplained}")
    unguarded = [
        key
        for key, doc in applied.items()
        if key not in exceptions
        and key not in skip
        and (key[0] in _PROTECTED_KINDS or key in _KEPT)
        and not _prune_disabled(doc)
    ]
    absent = sorted(key for key in _KEPT if key not in applied)
    return sorted(unguarded), absent


def _pending_skip(env: str, applied: Mapping[_Key, dict]) -> set[_Key]:
    return {
        key
        for pending_env, key in _PENDING_OWNER
        if pending_env == env and key in applied and not _prune_disabled(applied[key])
    }


def test_flux_paths_guard_protected_kinds() -> None:
    envs = non_example_env_names()
    assert envs
    for env in envs:
        applied = _applied(env)
        unguarded, absent = _unguarded(
            applied,
            exceptions=_EXCEPTIONS,
            skip=_pending_skip(env, applied),
        )
        assert absent == [], absent
        assert unguarded == [], unguarded
        for key in _EXCEPTIONS:
            assert key in applied, key
            assert not _prune_disabled(applied[key]), key


@pytest.mark.xfail(condition=bool(_PENDING_OWNER), strict=True, reason=_PENDING_OWNER_REASON)
def test_cert_manager_ca_prune_guard_is_pending_owner_run() -> None:
    for env, key in sorted(_PENDING_OWNER):
        applied = _applied(env)
        assert key in applied, (env, key)
        assert _prune_disabled(applied[key]), (env, key)


def test_unguarded_ca_in_another_env_is_reported() -> None:
    applied = {
        _PENDING_CA: {
            "kind": "Secret",
            "metadata": {"name": "cert-manager-ca", "namespace": "platform-cert-manager"},
        }
    }
    unguarded, _absent = _unguarded(
        applied, exceptions={}, skip=_pending_skip("env-02", applied)
    )
    assert _PENDING_CA in unguarded


@pytest.mark.parametrize("kind", sorted(_PROTECTED_KINDS))
def test_unannotated_protected_kind_is_unguarded(kind: str) -> None:
    key = (kind, "ns", "seed")
    applied = {key: {"kind": kind, "metadata": {"name": "seed", "namespace": "ns"}}}
    unguarded, _absent = _unguarded(applied, exceptions={}, skip=set())
    assert key in unguarded


def test_unexplained_exception_is_refused() -> None:
    key: _Key = ("Namespace", "", "seed")
    applied = {key: {"kind": "Namespace", "metadata": {"name": "seed"}}}
    with pytest.raises(AssertionError, match="unexplained exception"):
        _unguarded(applied, exceptions={key: "   "}, skip=set())
