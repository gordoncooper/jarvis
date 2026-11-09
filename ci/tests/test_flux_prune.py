"""ci/checks/flux-prune: Flux's own objects and protected kinds keep prune: disabled."""

from __future__ import annotations

import pytest

from jarvis.checks.flux_prune import EXCEPTIONS, PRUNE, index, scan_flux_prune, unguarded_objects
from support import ExampleCopy


def test_bootstrapped_example_env_has_every_protected_object_guarded(
    example_copy: ExampleCopy,
) -> None:
    """The root path and all three layer paths build, and nothing protected is unguarded."""
    assert scan_flux_prune(example_copy.root) == []


def test_addon_secret_without_the_annotation_is_reported(tmp_path) -> None:
    """flux-prune builds each add-on Kustomization's path, so a sealed add-on Secret is checked too."""
    import shutil

    import yaml

    from jarvis.secrets.sops import encrypt_secret_to_file
    from support import bootstrap_example_copy

    copy_env = bootstrap_example_copy(tmp_path)
    assert scan_flux_prune(copy_env.root) == []
    sealed = next((copy_env.env_dir / "secrets").glob("*/*.sops.yaml"))
    doc = yaml.safe_load(sealed.read_text(encoding="utf-8"))
    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": doc["metadata"]["name"], "namespace": doc["metadata"]["namespace"]},
        "stringData": {"greeting": "placeholder"},
    }
    encrypt_secret_to_file(
        repo_root=copy_env.root,
        env=copy_env.name,
        name=sealed.name.removesuffix(".sops.yaml"),
        namespace=sealed.parent.name,
        document=document,
        sops_bin=shutil.which("sops"),
    )
    messages = [item.message for item in scan_flux_prune(copy_env.root)]
    assert any(f"Secret {sealed.parent.name}/" in item and "must carry" in item for item in messages), messages


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
