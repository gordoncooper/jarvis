"""flux-substitute CI check: a ${name} Flux substitutes must be a published key."""

from __future__ import annotations

import shutil
from pathlib import Path

from jarvis.checks.flux_substitute import scan_flux_substitute
from jarvis.schema.env import EXAMPLE_ENV
from support import REPO_ROOT, ExampleCopy, bootstrap_example_copy

_ENV_FLUX = {
    "kustomization.yaml": "resources: [vars.yaml, layer.yaml]\n",
    "vars.yaml": """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-vars
  namespace: flux-system
data:
  dns_suffix: test.example
""",
    "layer.yaml": """
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: layer
  namespace: flux-system
spec:
  path: ./layer
  postBuild:
    substituteFrom:
      - kind: ConfigMap
        name: cluster-vars
""",
}


def _repo(root: Path, layer: str, **env_overrides: str) -> Path:
    files = {f"envs/env-test/flux/{name}": text for name, text in _ENV_FLUX.items()}
    files.update({f"envs/env-test/flux/{name}": text for name, text in env_overrides.items()})
    files["layer/kustomization.yaml"] = "resources: [app.yaml]\n"
    files["layer/app.yaml"] = layer
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _config_map(name: str, data: str, metadata: str = "") -> str:
    return (
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\n  namespace: demo\n"
        f"{metadata}data:\n{data}"
    )


def test_r4_published_escaped_and_disabled_references_pass(tmp_path: Path) -> None:
    layer = "\n---\n".join(
        (
            _config_map("known", '  host: "app.${dns_suffix}"\n'),
            _config_map("escaped", '  script: "echo $${not_a_cluster_var}"\n'),
            _config_map(
                "annotated",
                '  host: "${not_a_cluster_var}"\n',
                "  annotations:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\n",
            ),
            _config_map(
                "labelled",
                '  host: "${not_a_cluster_var}"\n',
                "  labels:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\n",
            ),
        )
    )
    assert scan_flux_substitute(_repo(tmp_path, layer)) == []


def test_r4_unknown_substitution_key_fails(tmp_path: Path) -> None:
    layer = _config_map(
        "seeded",
        '  host: "${not_a_cluster_var}.${dns_suffix}"\n  other: "${also_unknown:=fallback}"\n',
    )
    violations = scan_flux_substitute(_repo(tmp_path, layer))
    assert [item.path for item in violations] == ["envs/env-test/flux"] * 2
    assert "ConfigMap demo/seeded uses ${also_unknown}" in violations[0].message
    assert "ConfigMap demo/seeded uses ${not_a_cluster_var}" in violations[1].message


def test_r4_unknown_key_on_a_nested_addon_path_fails(tmp_path: Path) -> None:
    """An add-on's Kustomization on a stage's path is built and checked against its own postBuild."""
    inner = (
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\n"
        "metadata:\n  name: addon-demo\n  namespace: flux-system\nspec:\n  path: ./inner\n"
        "  postBuild:\n    substituteFrom:\n      - kind: ConfigMap\n        name: cluster-vars\n"
    )
    repo = _repo(tmp_path, inner)
    (repo / "inner").mkdir()
    (repo / "inner" / "kustomization.yaml").write_text("resources: [app.yaml]\n", encoding="utf-8")
    (repo / "inner" / "app.yaml").write_text(_config_map("known", '  host: "x.${dns_suffix}"\n'), encoding="utf-8")
    assert scan_flux_substitute(repo) == []
    (repo / "inner" / "app.yaml").write_text(_config_map("seeded", '  host: "${not_published}"\n'), encoding="utf-8")
    violations = scan_flux_substitute(repo)
    assert any("addon-demo: ConfigMap demo/seeded uses ${not_published}" in item.message for item in violations)


def test_r4_missing_substitute_from_source_fails(tmp_path: Path) -> None:
    layer = _config_map("known", '  host: "app.${dns_suffix}"\n')
    repo = _repo(tmp_path, layer, **{"kustomization.yaml": "resources: [layer.yaml]\n"})
    violations = scan_flux_substitute(repo)
    assert any(
        "substituteFrom ConfigMap flux-system/cluster-vars is not in the env's flux tree"
        in item.message
        for item in violations
    )
    assert any("uses ${dns_suffix}" in item.message for item in violations)


def test_example_env_platform_configs_is_not_built_when_secrets_are_absent(tmp_path: Path) -> None:
    """The example flux tree lists bootstrap secrets that do not exist yet.

    A non-example env with the same missing file still fails the build.
    """
    def tree(env: str) -> dict[str, str]:
        return {
            f"envs/{env}/flux/kustomization.yaml": "resources: [vars.yaml, configs.yaml]\n",
            f"envs/{env}/flux/vars.yaml": (
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
                "  name: cluster-vars\n  namespace: flux-system\ndata:\n"
                "  dns_suffix: test.example\n"
            ),
            f"envs/{env}/flux/configs.yaml": (
                "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\n"
                "metadata:\n  name: platform-configs\n  namespace: flux-system\nspec:\n"
                f"  path: ./envs/{env}/flux/platform-configs\n"
                "  postBuild:\n    substituteFrom:\n"
                "      - kind: ConfigMap\n        name: cluster-vars\n"
            ),
            f"envs/{env}/flux/platform-configs/kustomization.yaml": (
                "resources: [../../secrets/missing.sops.yaml]\n"
            ),
        }

    files: dict[str, str] = {}
    files.update(tree(EXAMPLE_ENV))
    files.update(tree("env-test"))
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    violations = scan_flux_substitute(tmp_path)
    assert violations
    assert {item.path for item in violations} == {"envs/env-test/flux"}


def test_a_tree_with_no_env_flux_directory_fails(tmp_path: Path) -> None:
    """Nothing to check is a failure, not a pass."""
    (tmp_path / "core" / "platform").mkdir(parents=True)
    assert [item.message for item in scan_flux_substitute(tmp_path)] == [
        "no envs/<env>/flux tree to check"
    ]


def test_an_env_flux_tree_that_builds_to_nothing_fails(tmp_path: Path) -> None:
    flux = tmp_path / "envs" / "env-test" / "flux"
    flux.mkdir(parents=True)
    (flux / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    assert scan_flux_substitute(tmp_path)


def test_bootstrapped_copy_scans_platform_configs(example_copy: ExampleCopy) -> None:
    """A copy scans core/platform/configs. That repo contains only the copy."""
    assert [path.name for path in (example_copy.root / "envs").iterdir()] == [example_copy.name]
    assert scan_flux_substitute(example_copy.root) == []


def test_undefined_var_in_platform_configs_fails_without_a_real_env(tmp_path: Path) -> None:
    """An undefined ${var} in core/platform/configs fails on a repo that has only the copy."""
    copy = bootstrap_example_copy(tmp_path)
    assert [path.name for path in (copy.root / "envs").iterdir()] == [copy.name]
    assert scan_flux_substitute(copy.root) == []
    _seed_unknown_platform_config(copy.root)
    violations = scan_flux_substitute(copy.root)
    assert any("uses ${not_a_cluster_var}" in item.message for item in violations)


def _seed_unknown_platform_config(root: Path) -> None:
    # The env's platform-configs builds core/platform/configs. core links to the
    # real core, so swap in a core dir that links everything but a copied platform.
    core = root / "core"
    if core.is_symlink():
        core.unlink()
        core.mkdir()
        for entry in (REPO_ROOT / "core").iterdir():
            if entry.name != "platform":
                (core / entry.name).symlink_to(entry)
        shutil.copytree(REPO_ROOT / "core" / "platform", core / "platform", symlinks=True)
    configs = core / "platform" / "configs"
    (configs / "seed.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: seeded-unknown\n"
        "  namespace: demo\n"
        "data:\n"
        "  host: ${not_a_cluster_var}\n",
        encoding="utf-8",
    )
    listing = configs / "kustomization.yaml"
    listing.write_text(listing.read_text(encoding="utf-8") + "  - seed.yaml\n", encoding="utf-8")
