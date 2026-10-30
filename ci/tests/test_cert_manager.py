"""cert-manager controller, internal CA, and the Traefik default TLS store."""

from __future__ import annotations

import json

import yaml

from ansible_role_proof import REPO_ROOT, load_cluster_vars, non_example_env_names
from test_flux_entry import _kustomize_build

_CONTROLLERS = {
    ("ClusterRole", "", "kyverno:backup-generate"),
    ("ClusterRole", "", "kyverno:backup-namespaces"),
    ("ConfigMap", "kube-system", "coredns-custom"),
    ("CustomResourceDefinition", "", "volumesnapshots.snapshot.storage.k8s.io"),
    ("HelmChartConfig", "kube-system", "traefik"),
    ("HelmRelease", "flux-system", "cert-manager"),
    ("HelmRelease", "flux-system", "flux-operator"),
    ("HelmRelease", "flux-system", "k8s-gateway"),
    ("HelmRelease", "flux-system", "kyverno"),
    ("HelmRelease", "flux-system", "volsync"),
    ("HelmRepository", "flux-system", "backube"),
    ("HelmRepository", "flux-system", "jetstack"),
    ("HelmRepository", "flux-system", "k8s-gateway"),
    ("HelmRepository", "flux-system", "kyverno"),
    ("Namespace", "", "kyverno"),
    ("Namespace", "", "platform-app-dns"),
    ("Namespace", "", "platform-cert-manager"),
    ("Namespace", "", "volsync"),
    ("OCIRepository", "flux-system", "flux-operator"),
}

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


def test_platform_build_adds_only_cert_manager_objects() -> None:
    assert _idents("platform") == _CONTROLLERS


def test_cert_manager_release_pins_chart_and_resources() -> None:
    release = yaml.safe_load(
        (REPO_ROOT / "platform/cert-manager/controller/helmrelease.yaml").read_text(
            encoding="utf-8"
        )
    )
    chart = release["spec"]["chart"]["spec"]
    assert chart["version"] == "v1.21.3"
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
    renovate = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("cert-manager" in pattern for pattern in manager["managerFilePatterns"])
    ]
    assert len(managers) == 1
    assert any("currentValue" in pattern for pattern in managers[0]["matchStrings"])


def test_platform_configs_hold_the_issuer_wildcard_and_store() -> None:
    envs = non_example_env_names()
    assert len(envs) == 1
    env = envs[0]
    assert _idents(f"envs/{env}/flux/platform-configs") == _CONFIGS_BEFORE | _CONFIGS_ADDED
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
