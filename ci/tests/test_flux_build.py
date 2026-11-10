"""flux_build: the env checks read Flux's own build of each Kustomization the env applies."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
import yaml

from jarvis.checks.flux_build import env_tree
from jarvis.checks.manifests import kustomize_build
from support import copy_sample, sample_root, write_flux_root

_API = "kustomize.toolkit.fluxcd.io/v1"


def _ks(name: str, path: str, *, source: str = "flux-system", post_build: dict | None = None) -> dict:
    spec: dict = {"interval": "10m", "path": path, "prune": True, "sourceRef": {"kind": "GitRepository", "name": source}}
    if post_build is not None:
        spec["postBuild"] = post_build
    return {"apiVersion": _API, "kind": "Kustomization", "metadata": {"name": name, "namespace": "flux-system"}, "spec": spec}


def _config_map(name: str, data: dict[str, str]) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "flux-system"}, "data": data}


def _secret(name: str, data: dict[str, str]) -> dict:
    encoded = {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "flux-system"}, "data": encoded}


_TARGET = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "out", "namespace": "default"},
           "data": {"env": "${jarvis_env}", "colour": "${colour:=grey}"}}  # fmt: skip


def _repo(root: Path, root_docs: list[dict], layer_docs: list[dict] | None = None) -> Path:
    """An env whose root applies root_docs, with one plain folder core/layer holding layer_docs."""
    flux = root / "envs" / "env-test" / "flux"
    flux.mkdir(parents=True)
    (flux / "kustomization.yaml").write_text("resources: [layers.yaml]\n", encoding="utf-8")
    (flux / "layers.yaml").write_text(yaml.safe_dump_all(root_docs), encoding="utf-8")
    layer = root / "core" / "layer"
    layer.mkdir(parents=True)
    # No kustomization file: Flux generates one for a plain directory, as on the cluster.
    (layer / "objects.yaml").write_text(yaml.safe_dump_all(layer_docs or [_TARGET]), encoding="utf-8")
    write_flux_root(flux.parent)
    return root


def _layer(root: Path):
    tree = env_tree(root, "envs/env-test/flux")
    assert tree.error is None, tree.error
    return next(path for path in tree.paths if path.kustomization["metadata"]["name"] == "layer")


def _digest(base: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            digest.update(f"{path.relative_to(base)}\n".encode() + path.read_bytes())
    return digest.hexdigest()


def test_walking_the_sample_writes_nothing_to_the_repository(tmp_path: Path) -> None:
    """Flux builds in memory: the env and core's trees are byte-for-byte unchanged afterwards."""
    root = copy_sample(tmp_path / "instance")
    core = Path(__file__).resolve().parents[2]
    before = [_digest(root / "envs"), _digest(core / "core"), _digest(core / "addons")]
    tree = env_tree(root, "envs/env-00/flux")
    assert tree.error is None, tree.error
    assert [_digest(root / "envs"), _digest(core / "core"), _digest(core / "addons")] == before


def test_the_sample_tree_holds_every_layer_built() -> None:
    """The root applies the stages; each built path's Kustomizations are built in turn (the sample
    lists no add-on, so its services stage applies nothing yet)."""
    tree = env_tree(sample_root(), "envs/env-00/flux")
    assert tree.error is None, tree.error
    built = {path.kustomization["metadata"]["name"]: path for path in tree.paths}
    for stage in ("platform-controllers", "platform-configs", "services"):
        assert stage in built and built[stage].parent is None
        assert built[stage].error is None, built[stage].error
        assert bool(built[stage].docs) == (stage != "services")


def test_substitute_from_reads_the_built_config_map_and_inline_wins(tmp_path: Path) -> None:
    """A substituteFrom ConfigMap is read from what the root applies; the inline substitute wins."""
    post_build = {"substitute": {"colour": "blue"}, "substituteFrom": [{"kind": "ConfigMap", "name": "cluster-vars"}]}
    root = _repo(tmp_path, [_ks("layer", "./core/layer", post_build=post_build),
                            _config_map("cluster-vars", {"jarvis_env": "env-test", "colour": "red"})])  # fmt: skip
    (out,) = _layer(root).docs
    assert out["data"] == {"env": "env-test", "colour": "blue"}


def test_substitute_from_decodes_a_secret_and_keeps_a_default(tmp_path: Path) -> None:
    """A plain Secret's data is base64, as Flux decodes it; an unset variable with a default takes it."""
    post_build = {"substituteFrom": [{"kind": "Secret", "name": "vars"}]}
    root = _repo(tmp_path, [_ks("layer", "./core/layer", post_build=post_build), _secret("vars", {"jarvis_env": "env-test"})])
    (out,) = _layer(root).docs
    assert out["data"] == {"env": "env-test", "colour": "grey"}


def test_missing_substitution_source_fails_unless_optional(tmp_path: Path) -> None:
    """A substituteFrom ConfigMap the tree does not hold fails the path; optional ones are skipped."""
    missing = {"substitute": {"jarvis_env": "env-test"}, "substituteFrom": [{"kind": "ConfigMap", "name": "gone"}]}
    path = _layer(_repo(tmp_path / "a", [_ks("layer", "./core/layer", post_build=missing)]))
    assert path.error and "ConfigMap flux-system/gone is not in the env's flux tree" in path.error
    optional = {**missing, "substituteFrom": [{"kind": "ConfigMap", "name": "gone", "optional": True}]}
    path = _layer(_repo(tmp_path / "b", [_ks("layer", "./core/layer", post_build=optional)]))
    assert path.error is None and path.docs[0]["data"]["env"] == "env-test"


def test_variable_with_no_value_fails_the_build(tmp_path: Path) -> None:
    """Strict substitution: a variable with no value and no default fails, as the reconcile does."""
    path = _layer(_repo(tmp_path, [_ks("layer", "./core/layer", post_build={"substitute": {"colour": "x"}})]))
    assert path.error and path.error.startswith("flux build layer failed:") and "jarvis_env" in path.error


def test_kustomization_on_another_source_is_partial(tmp_path: Path) -> None:
    """A path read from another source (an app's repository) is listed, not built."""
    root = _repo(tmp_path, [_ks("app-x", "./deploy", source="app-x")])
    tree = env_tree(root, "envs/env-test/flux")
    (path,) = tree.paths
    assert path.partial and path.docs == [] and path.error is None


def test_env_without_a_root_kustomization_is_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path, [])
    (root / "envs" / "env-test" / "flux" / "sync.yaml").write_text("{}\n", encoding="utf-8")
    assert "declares no root Kustomization flux-system" in (env_tree(root, "envs/env-test/flux").error or "")


def test_a_changed_file_rebuilds_the_tree(tmp_path: Path) -> None:
    """The walk is cached per content: editing a file between checks is seen."""
    root = _repo(tmp_path, [_ks("layer", "./core/layer", post_build={"substitute": {"jarvis_env": "one"}})])
    assert _layer(root).docs[0]["data"]["env"] == "one"
    layers = root / "envs" / "env-test" / "flux" / "layers.yaml"
    layers.write_text(layers.read_text(encoding="utf-8").replace("one", "two-two"), encoding="utf-8")
    assert _layer(root).docs[0]["data"]["env"] == "two-two"


@pytest.mark.parametrize("rel", ["envs/env-00/flux", "../elsewhere", "ci/tests"])
def test_kustomize_build_reads_core_trees_only(rel: str) -> None:
    """What an env applies is Flux's build; kustomize_build is for core's own folders."""
    docs, error = kustomize_build(sample_root(), rel)
    assert docs == [] and error and "is not one of core's trees" in error
