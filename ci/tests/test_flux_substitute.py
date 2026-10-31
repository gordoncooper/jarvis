"""flux-substitute CI check (R4, D45): a ${name} Flux substitutes must be a published key."""

from __future__ import annotations

from pathlib import Path

from _flux_substitute import scan_flux_substitute

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
