"""Flux Git entry (p0a-14): kustomize render, schema validation, and policy checks."""

from __future__ import annotations

import copy
import json
import posixpath
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from ansible_role_proof import (
    FLUX_OPERATOR_ROLE_DIR,
    REPO_ROOT,
    env_name,
    example_flux_entry,
    load_cluster_vars,
    load_role_defaults,
    non_example_env_names,
    standard_flux_kustomization_paths,
)
from render_cluster_vars_configmap import (
    CLUSTER_VARS_PUBLISHED,
    cluster_vars_configmap,
    render_document,
)
from schema.env import kubernetes_version_from_k3s
from support import _kustomize_build

_FLUX_KUSTOMIZATION_API = "kustomize.toolkit.fluxcd.io/"
_STRICT_SUBSTITUTION = "--feature-gates=StrictPostBuildSubstitutions=true"

# datreeio/CRDs-catalog commit. Tags through v0.0.12 omit these Flux CRDs.
_FLUX_SCHEMA_REF = "3eb90d47aba8d61919d4419dd38301536e32a94b"
FLUX_SCHEMA = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/"
    f"{_FLUX_SCHEMA_REF}/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
# Local schemas, searched before the CRD catalog. helm.cattle.io is not in
# the catalog. The catalog's VolSync schema predates moverVolumes.
HELM_CATTLE_SCHEMA = (
    str(REPO_ROOT / "ci/tests/fixtures/schemas")
    + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
_KUBECONFORM_SKIPPED = re.compile(r"Skipped:\s*(\d+)")


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (run make deps)")
    return path


def _kubeconform_validate(manifests: str, kubernetes_version: str) -> None:
    kubeconform = _require_tool("kubeconform")
    proc = subprocess.run(
        [
            kubeconform,
            "-strict",
            "-summary",
            "-kubernetes-version",
            kubernetes_version,
            "-schema-location",
            "default",
            "-schema-location",
            HELM_CATTLE_SCHEMA,
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


def _flux_kustomizations(env: str) -> list[dict]:
    """The Flux Kustomizations the env entry applies, from its kustomize build."""
    return [
        doc
        for doc in yaml.safe_load_all(_kustomize_build(f"envs/{env}/flux"))
        if isinstance(doc, dict)
        and doc.get("kind") == "Kustomization"
        and str(doc.get("apiVersion", "")).startswith(_FLUX_KUSTOMIZATION_API)
    ]


def _flux_kustomization_errors(docs: list[dict], env: str) -> list[str]:
    """The four standard Kustomizations, in dependsOn order, with their paths."""
    expected = standard_flux_kustomization_paths(env)
    chain = list(expected)
    names = [doc["metadata"]["name"] for doc in docs]
    errors = []
    if sorted(names) != sorted(chain):
        errors.append(f"Flux Kustomizations are {sorted(names)}, expected {sorted(chain)}")
    by_name = {doc["metadata"]["name"]: doc["spec"] for doc in docs}
    for index, name in enumerate(chain):
        spec = by_name.get(name)
        if spec is None:
            continue
        if spec.get("path") != expected[name]:
            errors.append(f"{name} path is {spec.get('path')!r}, expected {expected[name]!r}")
        depends = [] if index == 0 else [{"name": chain[index - 1]}]
        if (spec.get("dependsOn") or []) != depends:
            errors.append(f"{name} dependsOn is {spec.get('dependsOn')!r}, expected {depends!r}")
    return errors


def _flux_instance(env: str) -> dict:
    path = REPO_ROOT / "envs" / env / "flux" / "flux-instance.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _flux_instance_errors(doc: dict, env: str) -> list[str]:
    """Ansible applies this file once and looks it up by name; Flux then syncs it."""
    role = load_role_defaults(FLUX_OPERATOR_ROLE_DIR)
    cluster = load_cluster_vars(env=env)
    errors = []
    metadata = doc.get("metadata") or {}
    lookup = (role["flux_system_namespace"], role["flux_instance_name"])
    if (metadata.get("namespace"), metadata.get("name")) != lookup:
        errors.append(f"metadata is not {lookup}, the flux-operator lookup")
    sync = doc.get("spec", {}).get("sync") or {}
    expected = {
        "url": cluster["flux_git_repo_url"],
        "ref": cluster["flux_git_ref"],
        "path": f"envs/{env}/flux",
    }
    for key, want in expected.items():
        if sync.get(key) != want:
            errors.append(f"spec.sync.{key} is {sync.get(key)!r}, expected {want!r}")
    patches = doc.get("spec", {}).get("kustomize", {}).get("patches") or []
    if not any(
        _STRICT_SUBSTITUTION in str(patch.get("patch"))
        and patch.get("target") == {"kind": "Deployment", "name": "kustomize-controller"}
        for patch in patches
    ):
        errors.append("Deployment kustomize-controller does not run StrictPostBuildSubstitutions")
    return errors


def _platform_configs_errors(doc: dict, env: str) -> list[str]:
    """The env's platform-configs path is platform/configs plus that env's SOPS secrets."""
    base = f"envs/{env}/flux/platform-configs"
    resources = [posixpath.normpath(posixpath.join(base, item)) for item in doc.get("resources") or []]
    errors = []
    keys = sorted(set(doc) - {"apiVersion", "kind", "resources"})
    if keys:
        errors.append(f"keys other than resources: {keys}")
    if resources.count("platform/configs") != 1:
        errors.append("platform/configs is not listed exactly once")
    others = [
        item
        for item in resources
        if item != "platform/configs"
        and not (posixpath.dirname(item) == f"envs/{env}/secrets" and item.endswith(".sops.yaml"))
    ]
    if others:
        errors.append(f"not platform/configs or an env SOPS secret: {others}")
    return errors


@pytest.mark.parametrize("env", non_example_env_names())
def test_cluster_vars_configmap_matches_render_script(env: str) -> None:
    cluster_path = REPO_ROOT / "envs" / env / "values" / "cluster-vars.yaml"
    generated_path = (
        REPO_ROOT / "envs" / env / "flux" / "generated" / "cluster-vars-configmap.yaml"
    )
    expected = render_document(cluster_path)
    assert generated_path.read_text(encoding="utf-8") == expected


def test_cluster_vars_configmap_publishes_only_the_allow_list() -> None:
    """R4: the live ConfigMap is the allow-list. Host-only keys stay in the values file."""
    raw = load_cluster_vars()
    data = cluster_vars_configmap(raw)["data"]
    assert isinstance(data, dict)
    assert set(data) == set(CLUSTER_VARS_PUBLISHED)
    assert data["jarvis_env"] == raw["jarvis_env"]
    assert "ssh_user" not in data
    assert "ntp_pools" not in data


def test_cluster_vars_configmap_flattens_ntp_pools() -> None:
    """A list value is JSON-encoded. ntp_pools is host-only, so the proof uses a published key."""
    raw = load_cluster_vars()
    pools = raw["ntp_pools"]
    assert isinstance(pools, list) and pools
    published = cluster_vars_configmap(raw)["data"]
    assert "ntp_pools" not in published
    data = cluster_vars_configmap({**raw, "dns_suffix": pools})["data"]
    assert data["dns_suffix"] == json.dumps(pools, separators=(",", ":"))


@pytest.mark.parametrize("env", non_example_env_names())
def test_flux_instance_syncs_its_env_from_cluster_vars(env: str) -> None:
    """R1, D45: the committed FluxInstance is the one Ansible applies."""
    assert _flux_instance_errors(_flux_instance(env), env) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (("spec", "sync", "url"), "https://example.invalid/other"),
        (("spec", "sync", "ref"), "refs/heads/other"),
        (("spec", "sync", "path"), "envs/env-other/flux"),
        (("metadata", "name"), "other"),
        (("spec", "kustomize", "patches"), []),
        (("spec", "kustomize", "patches", 0, "target", "kind"), "StatefulSet"),
    ],
)
def test_flux_instance_drift_from_cluster_vars_fails(
    tmp_path: Path, field: tuple[str | int, ...], value: object
) -> None:
    entry = example_flux_entry(tmp_path)
    assert _flux_instance_errors(entry.instance, entry.env) == []
    doc = copy.deepcopy(entry.instance)
    parent = doc
    for key in field[:-1]:
        parent = parent[key]
    parent[field[-1]] = value
    assert _flux_instance_errors(doc, entry.env)


@pytest.mark.parametrize("env", non_example_env_names())
def test_flux_entry_has_the_four_standard_kustomizations(env: str) -> None:
    """R1: four layers, one path each, chained with dependsOn."""
    assert _flux_kustomization_errors(_flux_kustomizations(env), env) == []


def _drop_apps(docs: list[dict]) -> None:
    docs[:] = [doc for doc in docs if doc["metadata"]["name"] != "apps"]


def _add_extra(docs: list[dict]) -> None:
    extra = copy.deepcopy(docs[-1])
    extra["metadata"]["name"] = "extra"
    docs.append(extra)


def _move_platform_configs(docs: list[dict]) -> None:
    next(d for d in docs if d["metadata"]["name"] == "platform-configs")["spec"]["path"] = (
        "./platform/configs"
    )


def _skip_platform_configs(docs: list[dict]) -> None:
    next(d for d in docs if d["metadata"]["name"] == "services")["spec"]["dependsOn"] = [
        {"name": "platform-controllers"}
    ]


@pytest.mark.parametrize(
    "mutate", [_drop_apps, _add_extra, _move_platform_configs, _skip_platform_configs]
)
def test_nonstandard_flux_kustomizations_fail(tmp_path: Path, mutate) -> None:
    entry = example_flux_entry(tmp_path)
    assert _flux_kustomization_errors(entry.kustomizations, entry.env) == []
    docs = copy.deepcopy(entry.kustomizations)
    mutate(docs)
    assert _flux_kustomization_errors(docs, entry.env)


@pytest.mark.parametrize("env", non_example_env_names())
def test_env_platform_configs_adds_only_its_secrets(env: str) -> None:
    """R3: core components join platform/configs, not each env's list."""
    path = REPO_ROOT / "envs" / env / "flux" / "platform-configs" / "kustomization.yaml"
    assert _platform_configs_errors(yaml.safe_load(path.read_text(encoding="utf-8")), env) == []


@pytest.mark.parametrize(
    "seed",
    [
        {"resources": ["../../../../platform/configs", "../../../../platform/testapp"]},
        {"resources": ["../../../../platform/configs", "../../../env-other/secrets/x.sops.yaml"]},
        {"resources": ["../../secrets/x.sops.yaml"]},
        {"resources": ["../../../../platform/configs"], "patches": []},
    ],
)
def test_env_platform_configs_with_more_than_its_secrets_fails(seed: dict) -> None:
    env = env_name()
    clean = {"resources": ["../../../../platform/configs", "../../secrets/x.sops.yaml"]}
    assert _platform_configs_errors(clean, env) == []
    assert _platform_configs_errors(seed, env)


@pytest.mark.parametrize("env", non_example_env_names())
def test_platform_configs_decryption_uses_sops_age(env: str) -> None:
    doc = next(d for d in _flux_kustomizations(env) if d["metadata"]["name"] == "platform-configs")
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


def _validate_platform_configs_without_sops_secrets(
    manifests: str, kubernetes_version: str
) -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(manifests)
        if isinstance(doc, dict) and not (doc.get("kind") == "Secret" and "sops" in doc)
    ]
    rendered = yaml.safe_dump_all(docs, sort_keys=False)
    _kubeconform_validate(rendered, kubernetes_version)


class FluxBuildTarget(NamedTuple):
    name: str
    path: str
    strip_sops_secrets: bool


def flux_kustomization_targets(flux_dir: Path) -> list[FluxBuildTarget]:
    """Paths from Flux Kustomization objects under an env's flux directory."""
    targets: list[FluxBuildTarget] = []
    if not flux_dir.is_dir():
        return targets
    for path in sorted(item for item in flux_dir.rglob("*.yaml") if item.is_file()):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") != "Kustomization":
                continue
            api = doc.get("apiVersion")
            if not isinstance(api, str) or not api.startswith("kustomize.toolkit.fluxcd.io/"):
                continue
            metadata = doc.get("metadata")
            spec = doc.get("spec")
            assert isinstance(metadata, dict) and isinstance(spec, dict)
            name = metadata.get("name")
            rel = spec.get("path")
            assert isinstance(name, str) and name
            assert isinstance(rel, str) and rel
            targets.append(
                FluxBuildTarget(
                    name,
                    rel.removeprefix("./"),
                    isinstance(spec.get("decryption"), dict),
                )
            )
    return targets


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


def test_kubernetes_version_matches_k3s_pin() -> None:
    assert kubernetes_version_from_k3s("v9.8.7+k3s2") == "9.8.0"
    assert kubernetes_version_from_k3s("v9.8.7") is None


@pytest.mark.parametrize("env", non_example_env_names())
def test_flux_layer_kustomize_build_and_schema_validate(env: str) -> None:
    flux_dir = REPO_ROOT / "envs" / env / "flux"
    targets = flux_kustomization_targets(flux_dir)
    assert targets
    pin = load_cluster_vars(env=env)["k3s_version"]
    assert isinstance(pin, str)
    version = kubernetes_version_from_k3s(pin)
    assert version is not None
    seen: dict[tuple[str, str, str], str] = {}
    for target in targets:
        output = _kustomize_build(target.path)
        _record_object_identities(output, target.name, seen)
        if target.strip_sops_secrets:
            _validate_platform_configs_without_sops_secrets(output, version)
        else:
            _kubeconform_validate(output, version)
    # The env flux directory holds these Kustomizations. It is not one of their paths.
    _kubeconform_validate(_kustomize_build(f"envs/{env}/flux"), version)
