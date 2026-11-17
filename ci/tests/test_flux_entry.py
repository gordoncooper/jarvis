"""Flux Git entry: kustomize render, schema validation, and policy checks."""

from __future__ import annotations

import atexit
import functools
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import (
    REPO_ROOT,
    load_cluster_vars,
)

from jarvis.env.k3s import release as k3s_release
from jarvis.render.cluster_vars import (
    CLUSTER_VARS_PUBLISHED,
    cluster_vars_configmap,
)
from jarvis.schema.env import kubernetes_version_from_k3s
from support import ExampleCopy, real_and_copy_envs

# datreeio/CRDs-catalog commit. Tags through v0.0.12 omit these Flux CRDs.
_FLUX_SCHEMA_REF = "3eb90d47aba8d61919d4419dd38301536e32a94b"
FLUX_SCHEMA = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/"
    f"{_FLUX_SCHEMA_REF}/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
# Local schemas searched before the catalogs. This directory holds the
# CustomResourceDefinition schema. HelmChartConfig is not here, so the pinned
# catalog validates that kind.
FIXTURE_SCHEMA = (
    str(REPO_ROOT / "ci/tests/fixtures/schemas")
    + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
_KUBECONFORM_SKIPPED = re.compile(r"Skipped:\s*(\d+)")
# The catalog's VolSync schema predates moverVolumes, so VolSync kinds are checked
# against the CRDs of the pinned chart version, fetched from upstream and converted here.
_VOLSYNC_CRD = "https://raw.githubusercontent.com/backube/volsync/v{version}/config/crd/bases/volsync.backube_{plural}.yaml"
_VOLSYNC_KINDS = {"replicationsource": "replicationsources", "replicationdestination": "replicationdestinations"}


def _forbid_unknown_fields(node: object) -> None:
    """kubeconform -strict does not close CRD objects, so close every object that lists properties."""
    if isinstance(node, dict):
        if "properties" in node and "additionalProperties" not in node and not node.get(
            "x-kubernetes-preserve-unknown-fields"
        ):
            node["additionalProperties"] = False
        for value in node.values():
            _forbid_unknown_fields(value)
    elif isinstance(node, list):
        for value in node:
            _forbid_unknown_fields(value)


@functools.cache
def _volsync_schema_location() -> str:
    """A kubeconform schema location holding the pinned VolSync CRDs as JSON schemas."""
    helmrelease = yaml.safe_load(
        (REPO_ROOT / "core/platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
    )
    version = helmrelease["spec"]["chart"]["spec"]["version"]
    out = Path(tempfile.mkdtemp(prefix="volsync-schemas-"))
    atexit.register(shutil.rmtree, out, ignore_errors=True)
    (out / "volsync.backube").mkdir()
    for kind, plural in _VOLSYNC_KINDS.items():
        url = _VOLSYNC_CRD.format(version=version, plural=plural)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                crd = yaml.safe_load(response.read())
        except (OSError, yaml.YAMLError) as exc:
            pytest.fail(f"cannot fetch the VolSync CRD {url}: {exc}")
        for served in crd["spec"]["versions"]:
            schema = out / "volsync.backube" / f"{kind}_{served['name']}.json"
            body = served["schema"]["openAPIV3Schema"]
            _forbid_unknown_fields(body)
            schema.write_text(json.dumps(body), encoding="utf-8")
    return str(out) + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"


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
            FIXTURE_SCHEMA,
            "-schema-location",
            _volsync_schema_location(),
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


def _flux_instance(env: str, root: Path | None = None) -> dict:
    # Core's FluxInstance, at the core the env reads (the root artifact's core/flux).
    from jarvis import paths

    path = paths.core_root() / "core" / "flux" / "flux-instance.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def test_cluster_vars_configmap_publishes_only_the_allow_list() -> None:
    """The live ConfigMap is the allow-list. Host-only keys stay in the values file."""
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


def test_sops_secret_leaves_stay_encrypted(example_copy: ExampleCopy) -> None:
    """SOPS secret leaves stay encrypted in a bootstrapped copy, an add-on's folder included."""
    # An add-on's Secrets sit in its namespace's folder.
    paths = sorted((example_copy.env_dir / "secrets").rglob("*.sops.yaml"))
    assert any(path.parent != example_copy.env_dir / "secrets" for path in paths)
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
    gitleaks = shutil.which("gitleaks")
    age_keygen = shutil.which("age-keygen")
    if gitleaks is None or age_keygen is None:
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


def test_helmchartconfig_schema_accepts_upstream_fields_only() -> None:
    """The pinned catalog accepts upstream spec fields. A misspelled spec key fails."""
    kubeconform = _require_tool("kubeconform")
    version = kubernetes_version_from_k3s(k3s_release(load_cluster_vars())[0])
    good = """\
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
  labels:
    app: traefik
  annotations:
    example: "1"
spec:
  valuesContent: "a: b\\n"
  failurePolicy: abort
"""

    def run(text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                kubeconform,
                "-strict",
                "-kubernetes-version",
                version,
                "-schema-location",
                FLUX_SCHEMA,
                "-",
            ],
            input=text,
            capture_output=True,
            text=True,
            check=False,
        )

    passed = run(good)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    failed = run(good.replace("failurePolicy", "failurePollicy"))
    assert failed.returncode != 0, failed.stdout + failed.stderr


@pytest.mark.parametrize("kind", ["ReplicationSource", "ReplicationDestination"])
def test_volsync_spec_is_checked_against_the_upstream_crd(kind: str) -> None:
    """A VolSync spec field the pinned CRD does not know fails; a known one (moverVolumes) passes."""
    version = kubernetes_version_from_k3s(k3s_release(load_cluster_vars())[0])
    doc = {
        "apiVersion": "volsync.backube/v1alpha1",
        "kind": kind,
        "metadata": {"name": "x", "namespace": "x"},
        "spec": {"trigger": {"manual": "once"}} if kind == "ReplicationDestination" else {"sourcePVC": "x"},
    }
    doc["spec"]["restic"] = {"repository": "x", "moverVolumes": []}
    _kubeconform_validate(yaml.safe_dump(doc), version)
    doc["spec"]["notAField"] = True
    with pytest.raises(AssertionError):
        _kubeconform_validate(yaml.safe_dump(doc), version)


def validate_env_flux(root: Path, env: str) -> None:
    """Flux's build of the env's root and of each Flux path it applies, then kubeconform.

    Each stage is built with its components, and each add-on Kustomization on the
    services stage's path is built too, as on the cluster, with its postBuild values. A hosted
    app's own Kustomizations read the apps repository, which is not in this checkout.
    """
    from jarvis.checks.flux_build import env_tree
    from jarvis.checks.manifests import ROOT_SOURCE

    tree = env_tree(root, f"envs/{env}/flux")
    assert tree.error is None, tree.error
    env_docs, paths = tree.env_docs, tree.paths
    assert paths
    pin = k3s_release(load_cluster_vars(root, env=env))[0]
    assert isinstance(pin, str)
    version = kubernetes_version_from_k3s(pin)
    assert version is not None
    seen: dict[tuple[str, str, str], str] = {}
    for path in paths:
        name = path.kustomization["metadata"]["name"]
        source = (path.kustomization.get("spec") or {}).get("sourceRef") or {}
        if source.get("name", ROOT_SOURCE) != ROOT_SOURCE:
            continue
        assert path.error is None and not path.partial, f"{name}: {path.error or 'partial build'}"
        output = yaml.safe_dump_all(path.docs, sort_keys=False)
        _record_object_identities(output, name, seen)
        if isinstance((path.kustomization.get("spec") or {}).get("decryption"), dict):
            _validate_platform_configs_without_sops_secrets(output, version)
        else:
            _kubeconform_validate(output, version)
    # The root's own build holds these Kustomizations. It is not one of their paths.
    _kubeconform_validate(yaml.safe_dump_all(env_docs, sort_keys=False), version)


def test_flux_layer_kustomize_build_and_schema_validate(example_copy: ExampleCopy) -> None:
    """Each full env's Flux paths build and pass kubeconform, including a copy of env-00."""
    for root, env in real_and_copy_envs(example_copy):
        validate_env_flux(root, env)
