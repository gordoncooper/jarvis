"""ci/checks/flux-prune: Flux's own objects and protected kinds keep prune: disabled."""

from __future__ import annotations

import pytest
from _flux_prune import EXCEPTIONS, PRUNE, index, scan_flux_prune, unguarded_objects

from support import ExampleCopy


def test_bootstrapped_example_env_has_every_protected_object_guarded(
    example_copy: ExampleCopy,
) -> None:
    """The root path and all four layer paths build, and nothing protected is unguarded."""
    assert scan_flux_prune(example_copy.root) == []


def test_dropped_annotation_on_a_flux_object_is_reported() -> None:
    """The failing case: a GitRepository, FluxInstance and the root Kustomization without prune: disabled."""
    guarded = {"annotations": {PRUNE: "disabled"}}
    docs = [
        {"kind": "GitRepository", "metadata": {"name": "flux-system", "namespace": "flux-system"}},
        {"kind": "FluxInstance", "metadata": {"name": "flux", "namespace": "flux-system", **guarded}},
        {"kind": "Kustomization", "metadata": {"name": "flux-system", "namespace": "flux-system"}},
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "data", "namespace": "ns"}},
    ]
    unguarded, _absent = unguarded_objects(index(docs))
    assert unguarded == [
        ("GitRepository", "flux-system", "flux-system"),
        ("Kustomization", "flux-system", "flux-system"),
        ("PersistentVolumeClaim", "ns", "data"),
    ]


def test_testapp_exception_stays_and_a_reasonless_exception_is_refused() -> None:
    key = ("Namespace", "", "platform-testapp")
    applied = index([{"kind": "Namespace", "metadata": {"name": "platform-testapp"}}])
    assert key in EXCEPTIONS
    assert key not in unguarded_objects(applied)[0]
    with pytest.raises(AssertionError, match="unexplained exception"):
        unguarded_objects(applied, exceptions={key: "   "})
