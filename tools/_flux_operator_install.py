"""Parse pinned flux-operator install.yaml for Helm adoption (p0a-14)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import yaml

_K8S_KIND_TO_KUBECTL: dict[str, str] = {
    "Namespace": "namespace",
    "CustomResourceDefinition": "customresourcedefinitions",
    "ServiceAccount": "serviceaccount",
    "ClusterRole": "clusterrole",
    "ClusterRoleBinding": "clusterrolebinding",
    "Service": "service",
    "Deployment": "deployment",
}


@dataclass(frozen=True)
class InstallResource:
    kind: str
    name: str
    namespace: str

    @property
    def kubectl_resource(self) -> str:
        return _K8S_KIND_TO_KUBECTL.get(self.kind, self.kind.lower())

    def resource_key(self) -> tuple[str, str, str]:
        return (self.kind, self.namespace, self.name)


def parse_install_manifest(text: str) -> tuple[InstallResource, ...]:
    resources: list[InstallResource] = []
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        if not isinstance(kind, str) or kind in ("List",):
            continue
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            continue
        namespace = metadata.get("namespace")
        if namespace is None:
            namespace = ""
        elif not isinstance(namespace, str):
            namespace = ""
        resources.append(InstallResource(kind=kind, name=name, namespace=namespace))
    return tuple(resources)


def resource_keys(resources: Iterable[InstallResource]) -> frozenset[tuple[str, str, str]]:
    return frozenset(item.resource_key() for item in resources)


def parse_helm_template_manifest(text: str) -> frozenset[tuple[str, str, str]]:
    return resource_keys(parse_install_manifest(text))


# Chart 0.60.0 helm template keys (must match install.yaml v0.60.0 for Helm adoption).
FLUX_OPERATOR_CHART_0_60_0_RESOURCE_KEYS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("ClusterRole", "", "flux-operator-edit"),
        ("ClusterRole", "", "flux-operator-view"),
        ("ClusterRole", "", "flux-web-admin"),
        ("ClusterRole", "", "flux-web-user"),
        ("ClusterRoleBinding", "", "flux-operator-cluster-admin"),
        ("CustomResourceDefinition", "", "fluxinstances.fluxcd.controlplane.io"),
        ("CustomResourceDefinition", "", "fluxreports.fluxcd.controlplane.io"),
        (
            "CustomResourceDefinition",
            "",
            "resourcesetinputproviders.fluxcd.controlplane.io",
        ),
        ("CustomResourceDefinition", "", "resourcesets.fluxcd.controlplane.io"),
        ("Deployment", "flux-system", "flux-operator"),
        ("Namespace", "", "flux-system"),
        ("Service", "flux-system", "flux-operator"),
        ("ServiceAccount", "flux-system", "flux-operator"),
    }
)

INSTALL_SHA256_V0_60_0 = (
    "ef5f7c2ae52810d886f6bccc66a0fed3b436f3a7797e79a59c0443f191ddd02e"
)

INSTALL_URL_V0_60_0 = (
    "https://github.com/controlplaneio-fluxcd/flux-operator/releases/download/"
    "v0.60.0/install.yaml"
)
