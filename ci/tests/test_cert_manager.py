"""cert-manager controller, internal CA, and the Traefik default TLS store."""

from __future__ import annotations

import json
import re

import yaml

from ansible_role_proof import REPO_ROOT, load_cluster_vars, non_example_env_names
from support import _kustomize_build, renovate_python_pattern

_CONFIGS_BEFORE = {
    ("ClusterPolicy", "", "pvc-backup"),
    ("CronJob", "platform-backups", "etcd-snapshots"),
    ("Deployment", "platform-backups", "restic"),
    ("Namespace", "", "platform-backups"),
    ("Secret", "platform-backups", "backups-restic"),
    ("Service", "platform-backups", "restic"),
}
_CONFIGS_ADDED = {
    ("Certificate", "platform-cert-manager", "wildcard"),
    ("ClusterIssuer", "", "internal-ca"),
    ("Secret", "platform-cert-manager", "cert-manager-ca"),
    ("TLSStore", "platform-cert-manager", "default"),
}
# platform/testapp is rendered on the platform-configs path.
_TESTAPP = {
    ("ConfigMap", "platform-testapp", "testapp"),
    ("Deployment", "platform-testapp", "testapp"),
    ("Ingress", "platform-testapp", "testapp"),
    ("Namespace", "", "platform-testapp"),
    ("Service", "platform-testapp", "testapp"),
}


def _idents(path: str) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for doc in yaml.safe_load_all(_kustomize_build(path)):
        if not isinstance(doc, dict) or "kind" not in doc:
            continue
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            continue
        namespace = metadata.get("namespace") or ""
        found.add((str(doc["kind"]), str(namespace), str(metadata["name"])))
    return found


def test_controller_list_lives_on_platform() -> None:
    platform = yaml.safe_load(
        (REPO_ROOT / "platform/kustomization.yaml").read_text(encoding="utf-8")
    )
    assert platform["resources"] == [
        "flux-operator",
        "traefik",
        "backups/controller",
        "kyverno",
        "cert-manager/controller",
        "app-dns",
    ]
    operator = yaml.safe_load(
        (REPO_ROOT / "platform/flux-operator/kustomization.yaml").read_text(encoding="utf-8")
    )
    assert operator["resources"] == ["oci-repository.yaml", "helmrelease.yaml"]


def test_platform_build_includes_the_cert_manager_controller() -> None:
    """The platform build includes cert-manager's namespace, chart source, and release."""
    docs = {
        (doc["kind"], doc["metadata"].get("namespace") or "", doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(_kustomize_build("platform"))
        if isinstance(doc, dict)
        and (doc.get("metadata") or {}).get("name")
    }
    assert docs[("Namespace", "", "platform-cert-manager")]["metadata"]["name"] == (
        "platform-cert-manager"
    )
    assert docs[("HelmRepository", "flux-system", "jetstack")]["spec"]["url"] == (
        "https://charts.jetstack.io"
    )
    release = docs[("HelmRelease", "flux-system", "cert-manager")]
    assert release["spec"]["targetNamespace"] == "platform-cert-manager"
    assert release["spec"]["chart"]["spec"]["sourceRef"] == {
        "kind": "HelmRepository",
        "name": "jetstack",
        "namespace": "flux-system",
    }


def test_cert_manager_release_pins_chart_and_resources() -> None:
    release = yaml.safe_load(
        (REPO_ROOT / "platform/cert-manager/controller/helmrelease.yaml").read_text(
            encoding="utf-8"
        )
    )
    chart = release["spec"]["chart"]["spec"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", chart["version"])
    assert chart["sourceRef"] == {
        "kind": "HelmRepository",
        "name": "jetstack",
        "namespace": "flux-system",
    }
    assert release["spec"]["targetNamespace"] == "platform-cert-manager"
    values = release["spec"]["values"]
    assert values["crds"]["enabled"] is True
    blocks = [
        values["resources"],
        values["webhook"]["resources"],
        values["cainjector"]["resources"],
        values["startupapicheck"]["resources"],
    ]
    for block in blocks:
        assert block["requests"]["cpu"]
        assert block["requests"]["memory"]
        assert block["limits"]["memory"]
        assert "cpu" not in block["limits"]
    images = [
        values["image"],
        values["webhook"]["image"],
        values["cainjector"]["image"],
        values["startupapicheck"]["image"],
        values["acmesolver"]["image"],
    ]
    tags = {image["tag"] for image in images}
    for image in images:
        assert re.fullmatch(r"v\d+\.\d+\.\d+", image["tag"])
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", image["digest"])
    renovate = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("cert-manager" in pattern for pattern in manager["managerFilePatterns"])
    ]
    chart_managers = [
        manager
        for manager in managers
        if any("registryUrl" in pattern for pattern in manager["matchStrings"])
    ]
    assert len(chart_managers) == 1
    assert any("currentValue" in pattern for pattern in chart_managers[0]["matchStrings"])
    source = (REPO_ROOT / "platform/cert-manager/controller/helmrelease.yaml").read_text(
        encoding="utf-8"
    )
    chart_pattern = renovate_python_pattern(chart_managers[0]["matchStrings"][0])
    chart_found = list(re.finditer(chart_pattern, source))
    assert len(chart_found) == 1
    assert chart_found[0].group("currentValue") == chart["version"]
    image_managers = [
        manager
        for manager in managers
        if any("currentDigest" in pattern for pattern in manager["matchStrings"])
    ]
    assert len(image_managers) == 1
    python_pattern = renovate_python_pattern(image_managers[0]["matchStrings"][0])
    found = list(re.finditer(python_pattern, source))
    assert len(found) == 5
    assert len(tags) == 1
    assert {match.group("currentValue") for match in found} == tags
    assert {match.group("depName") for match in found} == {
        "quay.io/jetstack/cert-manager-controller",
        "quay.io/jetstack/cert-manager-webhook",
        "quay.io/jetstack/cert-manager-cainjector",
        "quay.io/jetstack/cert-manager-startupapicheck",
        "quay.io/jetstack/cert-manager-acmesolver",
    }


def test_platform_configs_hold_the_issuer_wildcard_and_store() -> None:
    envs = non_example_env_names()
    assert len(envs) == 1
    env = envs[0]
    assert _idents(f"envs/{env}/flux/platform-configs") == _CONFIGS_BEFORE | _CONFIGS_ADDED | _TESTAPP
    text = (REPO_ROOT / "platform/cert-manager/config/internal-ca.yaml").read_text(
        encoding="utf-8"
    )
    assert '"*.${dns_suffix}"' in text
    suffix = str(load_cluster_vars(env=env)["dns_suffix"])
    for path in (REPO_ROOT / "platform/cert-manager").rglob("*"):
        if path.is_file():
            assert suffix not in path.read_text(encoding="utf-8")
    docs = {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(_kustomize_build(f"envs/{env}/flux/platform-configs"))
        if isinstance(doc, dict) and doc.get("kind") in {"ClusterIssuer", "Certificate", "TLSStore"}
    }
    assert docs[("ClusterIssuer", "internal-ca")]["spec"]["ca"]["secretName"] == "cert-manager-ca"
    certificate = docs[("Certificate", "wildcard")]
    assert certificate["metadata"]["namespace"] == "platform-cert-manager"
    assert certificate["spec"]["secretName"] == "wildcard-tls"
    assert certificate["spec"]["issuerRef"] == {"name": "internal-ca", "kind": "ClusterIssuer"}
    assert certificate["spec"]["dnsNames"] == ["*.${dns_suffix}"]
    store = docs[("TLSStore", "default")]
    assert store["metadata"]["namespace"] == "platform-cert-manager"
    assert store["spec"]["defaultCertificate"]["secretName"] == "wildcard-tls"
