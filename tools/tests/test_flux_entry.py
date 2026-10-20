"""Flux Git entry (p0a-14): kustomize render, schema validation, and policy checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from _flux_entry import (
    FLUX_KUSTOMIZATION_CHAIN,
    FLUX_KUSTOMIZATION_PATHS,
    kustomize_build_paths,
)
from ansible_role_proof import (
    FLUX_OPERATOR_ROLE_TEMPLATES,
    REPO_ROOT,
    ansible_render_template,
    env_name,
    load_cluster_vars,
)
from render_cluster_vars_configmap import cluster_vars_configmap, render_document

FLUX_SCHEMA = (
    "https://raw.githubusercontent.com/fluxcd/flux2/v2.8.3/schemas/"
    "{{ .ResourceKind }}_{{ .ResourceAPIVersion }}.json"
)
def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (run make deps)")
    return path


def _kustomize_build(rel_path: str) -> str:
    kustomize = _require_tool("kustomize")
    proc = subprocess.run(
        [
            kustomize,
            "build",
            "--load-restrictor",
            "LoadRestrictionsNone",
            str(REPO_ROOT / rel_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _kubeconform_validate(manifests: str, *, ignore_missing: bool = False) -> None:
    kubeconform = _require_tool("kubeconform")
    cmd = [
        kubeconform,
        "-strict",
        "-summary",
        "-kubernetes-version",
        "1.31.0",
        "-schema-location",
        "default",
        "-schema-location",
        FLUX_SCHEMA,
    ]
    if ignore_missing:
        cmd.append("-ignore-missing-schemas")
    proc = subprocess.run(
        cmd,
        input=manifests,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _load_flux_kustomizations() -> list[dict[str, object]]:
    env = env_name()
    docs: list[dict[str, object]] = []
    for name in FLUX_KUSTOMIZATION_CHAIN:
        path = REPO_ROOT / "envs" / env / "flux" / "kustomizations" / f"{name}.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(doc, dict)
        docs.append(doc)
    return docs


def test_cluster_vars_configmap_matches_render_script() -> None:
    env = env_name()
    cluster_path = REPO_ROOT / "envs" / env / "values" / "cluster-vars.yaml"
    generated_path = (
        REPO_ROOT / "envs" / env / "flux" / "generated" / "cluster-vars-configmap.yaml"
    )
    expected = render_document(cluster_path)
    assert generated_path.read_text(encoding="utf-8") == expected


def test_cluster_vars_configmap_flattens_ntp_pools() -> None:
    env = env_name()
    cluster_path = REPO_ROOT / "envs" / env / "values" / "cluster-vars.yaml"
    raw = yaml.safe_load(cluster_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    doc = cluster_vars_configmap(raw)
    data = doc["data"]
    assert isinstance(data, dict)
    assert data["ntp_pools"] == json.dumps(raw["ntp_pools"], separators=(",", ":"))


def test_git_flux_instance_matches_ansible_template() -> None:
    env = env_name()
    git_path = REPO_ROOT / "envs" / env / "flux" / "flux-instance.yaml"
    git_text = git_path.read_text(encoding="utf-8")
    if git_text.startswith("#"):
        git_text = "\n".join(
            line for line in git_text.splitlines() if not line.startswith("#")
        ).lstrip()
    cluster = load_cluster_vars()
    variables = {
        "flux_instance_name": "flux",
        "flux_system_namespace": "flux-system",
        "flux_distribution_version": "2.8.x",
        "flux_git_repo_url": cluster["flux_git_repo_url"],
        "flux_git_ref": cluster["flux_git_ref"],
        "jarvis_env": env,
    }
    rendered = ansible_render_template(
        "flux-instance.yaml.j2",
        variables,
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    )
    rendered_no_comment = "\n".join(
        line for line in rendered.splitlines() if not line.startswith("#")
    ).lstrip()
    assert yaml.safe_load(git_text) == yaml.safe_load(rendered_no_comment)


def test_flux_kustomization_depends_on_chain() -> None:
    docs = _load_flux_kustomizations()
    assert docs[0]["metadata"]["name"] == "platform-controllers"
    assert docs[0].get("spec", {}).get("dependsOn") in (None, [])
    for idx in range(1, len(FLUX_KUSTOMIZATION_CHAIN)):
        depends = docs[idx].get("spec", {}).get("dependsOn")
        assert depends == [{"name": FLUX_KUSTOMIZATION_CHAIN[idx - 1]}]


def test_flux_kustomization_paths() -> None:
    env = env_name()
    for doc in _load_flux_kustomizations():
        name = doc["metadata"]["name"]
        assert isinstance(name, str)
        expected = FLUX_KUSTOMIZATION_PATHS[name].format(env=env)
        assert doc["spec"]["path"] == expected


def test_platform_configs_decryption_uses_sops_age() -> None:
    doc = _load_flux_kustomizations()[1]
    decryption = doc["spec"]["decryption"]
    assert decryption["provider"] == "sops"
    assert decryption["secretRef"]["name"] == "sops-age"


def test_helmrelease_substitute_disabled() -> None:
    path = REPO_ROOT / "platform" / "flux-operator" / "helmrelease.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert (
        doc["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/substitute"]
        == "disabled"
    )


def test_backups_restic_secret_stays_sops_encrypted_in_git() -> None:
    env = env_name()
    secret_path = REPO_ROOT / "envs" / env / "secrets" / "backups-restic.sops.yaml"
    text = secret_path.read_text(encoding="utf-8")
    assert "sops:" in text
    assert "ENC[AES256_GCM" in text
    assert "RESTIC_PASSWORD" in text


def test_placeholder_configmaps_use_unique_names() -> None:
    services = yaml.safe_load(
        (REPO_ROOT / "services" / "placeholder.yaml").read_text(encoding="utf-8")
    )
    apps = yaml.safe_load(
        (REPO_ROOT / "apps" / "placeholder.yaml").read_text(encoding="utf-8")
    )
    assert services["metadata"]["name"] == "placeholder-services"
    assert apps["metadata"]["name"] == "placeholder-apps"


def _object_identity(doc: object) -> tuple[str, str, str] | None:
    if not isinstance(doc, dict) or "kind" not in doc:
        return None
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return None
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        return None
    namespace = metadata.get("namespace")
    if not isinstance(namespace, str):
        namespace = ""
    kind = doc["kind"]
    assert isinstance(kind, str)
    return (kind, namespace, name)


def test_flux_layers_do_not_render_duplicate_objects() -> None:
    env = env_name()
    layers = {
        "platform-controllers": "platform/flux-operator",
        "platform-configs": f"envs/{env}/flux/platform-configs",
        "services": "services",
        "apps": "apps",
    }
    seen: dict[tuple[str, str, str], str] = {}
    for layer, rel_path in layers.items():
        output = _kustomize_build(rel_path)
        for doc in yaml.safe_load_all(output):
            ident = _object_identity(doc)
            if ident is None:
                continue
            owner = seen.get(ident)
            assert owner is None, f"{ident} rendered by both {owner} and {layer}"
            seen[ident] = layer


@pytest.mark.parametrize("rel_path", ["services", "apps"])
def test_layer_kustomize_build_and_schema_validate(rel_path: str) -> None:
    output = _kustomize_build(rel_path)
    _kubeconform_validate(output)


def test_platform_flux_operator_kustomize_build_and_schema_validate() -> None:
    output = _kustomize_build("platform/flux-operator")
    _kubeconform_validate(output, ignore_missing=True)


def test_env_flux_entry_kustomize_build_and_schema_validate() -> None:
    env = env_name()
    output = _kustomize_build(f"envs/{env}/flux")
    _kubeconform_validate(output, ignore_missing=True)


def test_platform_configs_overlay_kustomize_build_and_schema_validate() -> None:
    env = env_name()
    output = _kustomize_build(f"envs/{env}/flux/platform-configs")
    docs = [
        doc
        for doc in yaml.safe_load_all(output)
        if isinstance(doc, dict) and not (doc.get("kind") == "Secret" and "sops" in doc)
    ]
    rendered = yaml.safe_dump_all(docs, sort_keys=False)
    _kubeconform_validate(rendered, ignore_missing=True)


def test_all_flux_kustomization_paths_kustomize_build() -> None:
    env = env_name()
    for rel in kustomize_build_paths(env):
        _kustomize_build(rel)

