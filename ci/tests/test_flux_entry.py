"""Flux Git entry (p0a-14): kustomize render, schema validation, and policy checks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import (
    FLUX_OPERATOR_ROLE_DIR,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    REPO_ROOT,
    ansible_render_template,
    env_name,
    load_cluster_vars,
    non_example_env_names,
)
from render_cluster_vars_configmap import cluster_vars_configmap, render_document

FLUX_KUSTOMIZATION_CHAIN: tuple[str, ...] = (
    "platform-controllers",
    "platform-configs",
    "services",
    "apps",
)

# datreeio/CRDs-catalog commit. Tags through v0.0.12 omit these Flux CRDs.
_FLUX_SCHEMA_REF = "3eb90d47aba8d61919d4419dd38301536e32a94b"
FLUX_SCHEMA = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/"
    f"{_FLUX_SCHEMA_REF}/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
_KUBECONFORM_SKIPPED = re.compile(r"Skipped:\s*(\d+)")


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


def _kubeconform_validate(manifests: str) -> None:
    kubeconform = _require_tool("kubeconform")
    proc = subprocess.run(
        [
            kubeconform,
            "-strict",
            "-summary",
            "-kubernetes-version",
            "1.31.0",
            "-schema-location",
            "default",
            "-schema-location",
            FLUX_SCHEMA,
        ],
        input=manifests,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    skipped = _KUBECONFORM_SKIPPED.search(output)
    assert skipped is not None, output
    assert int(skipped.group(1)) == 0, output


def _load_flux_kustomizations(env: str) -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    for name in FLUX_KUSTOMIZATION_CHAIN:
        path = REPO_ROOT / "envs" / env / "flux" / "kustomizations" / f"{name}.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(doc, dict)
        docs.append(doc)
    return docs


@pytest.mark.parametrize("env", non_example_env_names())
def test_cluster_vars_configmap_matches_render_script(env: str) -> None:
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


def _flux_distribution_version() -> str:
    defaults = yaml.safe_load(
        (FLUX_OPERATOR_ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert isinstance(defaults, dict)
    version = defaults["flux_distribution_version"]
    parts = version.split(".") if isinstance(version, str) else []
    assert len(parts) == 3 and all(part.isdigit() for part in parts)
    return version


@pytest.mark.parametrize("env", non_example_env_names())
def test_git_flux_instance_matches_ansible_template(env: str) -> None:
    git_path = REPO_ROOT / "envs" / env / "flux" / "flux-instance.yaml"
    git_text = git_path.read_text(encoding="utf-8")
    if git_text.startswith("#"):
        git_text = "\n".join(
            line for line in git_text.splitlines() if not line.startswith("#")
        ).lstrip()
    cluster = load_cluster_vars(env=env)
    variables = {
        "flux_instance_name": "flux",
        "flux_system_namespace": "flux-system",
        "flux_distribution_version": _flux_distribution_version(),
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
    assert "--feature-gates=StrictPostBuildSubstitutions=true" in rendered_no_comment


@pytest.mark.parametrize("env", non_example_env_names())
def test_flux_kustomization_depends_on_chain(env: str) -> None:
    docs = _load_flux_kustomizations(env)
    assert docs[0]["metadata"]["name"] == "platform-controllers"
    assert docs[0].get("spec", {}).get("dependsOn") in (None, [])
    for idx in range(1, len(FLUX_KUSTOMIZATION_CHAIN)):
        depends = docs[idx].get("spec", {}).get("dependsOn")
        assert depends == [{"name": FLUX_KUSTOMIZATION_CHAIN[idx - 1]}]


@pytest.mark.parametrize("env", non_example_env_names())
def test_platform_configs_decryption_uses_sops_age(env: str) -> None:
    doc = _load_flux_kustomizations(env)[1]
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


def _assert_sops_leaf(path: Path, section: str, node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_sops_leaf(path, f"{section}.{key}", value)
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            _assert_sops_leaf(path, f"{section}[{index}]", value)
        return
    if isinstance(node, str) and node.startswith("ENC["):
        return
    raise AssertionError(f"{path}: {section} is not an ENC value")


def test_sops_secret_leaves_stay_encrypted() -> None:
    paths = sorted(REPO_ROOT.glob("envs/*/secrets/*.sops.yaml"))
    assert paths
    for path in paths:
        docs = [
            doc
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if doc is not None
        ]
        assert docs, path
        for doc in docs:
            assert isinstance(doc, dict), path
            assert isinstance(doc.get("sops"), dict), path
            sections = [key for key in ("data", "stringData") if key in doc]
            assert sections, path
            for key in sections:
                _assert_sops_leaf(path, key, doc[key])


def test_secret_scan_rejects_planted_age_key(tmp_path: Path) -> None:
    gitleaks = Path.home() / ".local" / "bin" / "gitleaks"
    age_keygen = shutil.which("age-keygen")
    if not gitleaks.is_file() or age_keygen is None:
        pytest.fail("gitleaks and age-keygen are required (run make deps)")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [age_keygen, "-o", str(tmp_path / "key.txt")],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "key.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "user.name=fixture",
            "commit",
            "-m",
            "plant",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    proc = subprocess.run(
        [str(gitleaks), "detect", "--source", str(tmp_path), "--redact"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0


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


def _validate_platform_configs_without_sops_secrets(manifests: str) -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(manifests)
        if isinstance(doc, dict) and not (doc.get("kind") == "Secret" and "sops" in doc)
    ]
    rendered = yaml.safe_dump_all(docs, sort_keys=False)
    _kubeconform_validate(rendered)


def _record_object_identities(
    output: str, layer: str, seen: dict[tuple[str, str, str], str]
) -> None:
    for doc in yaml.safe_load_all(output):
        ident = _object_identity(doc)
        if ident is None:
            continue
        owner = seen.get(ident)
        assert owner is None, f"{ident} rendered by both {owner} and {layer}"
        seen[ident] = layer


@pytest.mark.parametrize("env", non_example_env_names())
def test_flux_layer_kustomize_build_and_schema_validate(env: str) -> None:
    # layer, path, strip sops secrets, count toward the cross-layer identity check.
    # The env flux entry is schema-checked and is not one of those four layers.
    layers = (
        ("services", "services", False, True),
        ("apps", "apps", False, True),
        ("platform-controllers", "platform/flux-operator", False, True),
        ("platform-configs", f"envs/{env}/flux/platform-configs", True, True),
        ("env-flux-entry", f"envs/{env}/flux", False, False),
    )
    seen: dict[tuple[str, str, str], str] = {}
    for layer, rel_path, strip_sops_secrets, track_identity in layers:
        output = _kustomize_build(rel_path)
        if track_identity:
            _record_object_identities(output, layer, seen)
        if strip_sops_secrets:
            _validate_platform_configs_without_sops_secrets(output)
        else:
            _kubeconform_validate(output)
