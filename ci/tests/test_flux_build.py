"""flux_build: the env checks read Flux's own build of each Kustomization the env applies."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
import yaml

from jarvis.checks.flux_build import EnvTree, env_tree
from jarvis.checks.manifests import kustomize_build
from support import REPO_ROOT, copy_sample, core_at, sample_root, write_flux_root

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


def _repo(root: Path, root_docs: list[dict]) -> Path:
    """An env whose root applies root_docs, with one plain folder core/layer holding _TARGET."""
    flux = root / "envs" / "env-test" / "flux"
    flux.mkdir(parents=True)
    (flux / "kustomization.yaml").write_text("resources: [layers.yaml]\n", encoding="utf-8")
    (flux / "layers.yaml").write_text(yaml.safe_dump_all(root_docs), encoding="utf-8")
    layer = root / "core" / "layer"
    layer.mkdir(parents=True)
    # No kustomization file: Flux generates one for a plain directory, as on the cluster.
    (layer / "objects.yaml").write_text(yaml.safe_dump(_TARGET), encoding="utf-8")
    write_flux_root(flux.parent)
    return root


def _tree(root: Path):
    """The env's tree, against root's own core trees (core/layer)."""
    with core_at(root):
        return env_tree(root, "envs/env-test/flux")


def _layer(root: Path):
    tree = _tree(root)
    assert tree.error is None, tree.error
    return next(path for path in tree.paths if path.kustomization["metadata"]["name"] == "layer")


def _digest(base: Path) -> str:
    digest = hashlib.sha256()
    # __pycache__: another xdist worker importing a fixture's verify.py writes it mid-hash.
    for path in sorted(base.rglob("*")):
        if path.is_file() and not {".git", "__pycache__"} & set(path.parts):
            digest.update(f"{path.relative_to(base)}\n".encode() + path.read_bytes())
    return digest.hexdigest()


def test_walking_the_sample_writes_nothing_to_the_repository(tmp_path: Path) -> None:
    """Rule: Flux builds in memory: the env and core's trees are byte-for-byte unchanged afterwards."""
    root = copy_sample(tmp_path / "instance")
    trees = [root, REPO_ROOT / "core", REPO_ROOT / "addons", REPO_ROOT / "ci" / "tests" / "fixtures" / "addons"]
    before = [_digest(tree) for tree in trees]
    tree = env_tree(root, "envs/env-00/flux")
    assert tree.error is None and any(path.docs for path in tree.paths), tree.error
    assert [_digest(tree) for tree in trees] == before


def test_the_sample_tree_holds_every_layer_built() -> None:
    """Rule: the root applies the stages, each built (the sample lists no add-on, so its services stage
    applies nothing yet)."""
    tree = env_tree(sample_root(), "envs/env-00/flux")
    assert tree.error is None, tree.error
    built = {path.kustomization["metadata"]["name"]: path for path in tree.paths}
    for stage in ("platform-controllers", "platform-configs", "services"):
        assert stage in built and built[stage].parent is None
        assert built[stage].error is None, built[stage].error
        assert bool(built[stage].docs) == (stage != "services")


_FROM_GONE = {"substitute": {"jarvis_env": "env-test"}, "substituteFrom": [{"kind": "ConfigMap", "name": "gone"}]}
_DEFAULTED = {"env": "env-test", "colour": "grey"}


@pytest.mark.parametrize(
    ("post_build", "sources", "data", "error"),
    [
        pytest.param({"substitute": {"colour": "blue"}, "substituteFrom": [{"kind": "ConfigMap", "name": "cluster-vars"}]},
                     [_config_map("cluster-vars", {"jarvis_env": "env-test", "colour": "red"})],
                     {"env": "env-test", "colour": "blue"}, None, id="config-map-inline-wins"),
        pytest.param({"substituteFrom": [{"kind": "Secret", "name": "vars"}]}, [_secret("vars", {"jarvis_env": "env-test"})],
                     _DEFAULTED, None, id="secret-decoded-default-kept"),
        pytest.param(_FROM_GONE, [], None, "substituteFrom ConfigMap flux-system/gone is not in the env's flux tree",
                     id="missing-source"),
        pytest.param({**_FROM_GONE, "substituteFrom": [{"kind": "ConfigMap", "name": "gone", "optional": True}]}, [],
                     _DEFAULTED, None, id="optional-source-skipped"),
        pytest.param({"substitute": {"colour": "x"}}, [], None, "flux build layer failed: ✗ var substitution failed for"
                     " 'out': envsubst error: variable substitution failed: variable not set (strict mode): \"jarvis_env\"",
                     id="variable-without-value"),
    ],
)
def test_a_layer_is_built_with_its_post_build_values(
    tmp_path: Path, post_build: dict, sources: list[dict], data: dict | None, error: str | None
) -> None:
    """Rule: substituteFrom reads what the root built (a Secret decoded) and the inline substitute wins; a
    missing source fails unless optional, and a variable with no value fails the build, as the reconcile does."""
    path = _layer(_repo(tmp_path, [_ks("layer", "./core/layer", post_build=post_build), *sources]))
    assert ([doc["data"] for doc in path.docs], path.error) == ([data] if data else [], error)


def test_kustomization_on_another_source_is_partial(tmp_path: Path) -> None:
    """Rule: a path read from another source (an app's repository) is listed, not built."""
    (path,) = _tree(_repo(tmp_path, [_ks("app-x", "./deploy", source="app-x")])).paths
    assert path.partial and path.docs == [] and path.error is None


@pytest.mark.parametrize(("name", "text", "error"), [
    pytest.param("sync.yaml", "{}\n", "envs/env-test/flux/sync.yaml declares no root Kustomization flux-system", id="no-root"),
    pytest.param("kustomization.yaml", "resources: [gone.yaml]\n", "flux build flux-system failed: ✗ failed to generate"
                 " kustomization.yaml: lstat envs/env-test/flux/gone.yaml: no such file or directory <nil>", id="root-does-not-build"),
])  # fmt: skip
def test_an_env_whose_root_does_not_build_is_an_error(tmp_path: Path, name: str, text: str, error: str) -> None:
    """Rule: an env whose sync.yaml declares no root Kustomization, or whose root does not build, is an error with
    nothing built; flux's message names the env's files, not the temporary artifact's."""
    root = _repo(tmp_path, [])
    (root / "envs" / "env-test" / "flux" / name).write_text(text, encoding="utf-8")
    assert _tree(root) == EnvTree([], [], error)


def test_a_changed_file_rebuilds_the_tree(tmp_path: Path) -> None:
    """Rule: the walk is cached per content: editing a file between checks is seen."""
    root = _repo(tmp_path, [_ks("layer", "./core/layer", post_build={"substitute": {"jarvis_env": "one"}})])
    assert _layer(root).docs[0]["data"]["env"] == "one"
    layers = root / "envs" / "env-test" / "flux" / "layers.yaml"
    layers.write_text(layers.read_text(encoding="utf-8").replace("one", "two-two"), encoding="utf-8")
    assert _layer(root).docs[0]["data"]["env"] == "two-two"


@pytest.mark.parametrize("rel", ["envs/env-00/flux", "../elsewhere", "ci/tests"])
def test_kustomize_build_reads_core_trees_only(rel: str) -> None:
    """Rule: what an env applies is Flux's build; kustomize_build is for core's own folders."""
    assert kustomize_build(REPO_ROOT, rel) == ([], f"{rel} is not one of core's trees (core, addons)")
