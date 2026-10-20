"""Parse pinned flux-operator install.yaml and chart templates (p0a-14)."""

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
    "NetworkPolicy": "networkpolicy",
}

# install.yaml v0.60.0 vs chart 0.60.0 helm template (verified in test).
INSTALL_CHART_DIFF_CHART_ONLY: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("ClusterRoleBinding", "", "flux-operator"),
        ("NetworkPolicy", "flux-system", "flux-operator-web"),
    }
)
INSTALL_CHART_DIFF_INSTALL_ONLY: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("ClusterRoleBinding", "", "flux-operator-cluster-admin"),
        ("Namespace", "", "flux-system"),
    }
)

FLUX_OPERATOR_CHART_VERSION = "0.60.0"
FLUX_OPERATOR_CHART_OCI = "oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator"

INSTALL_SHA256_V0_60_0 = (
    "ef5f7c2ae52810d886f6bccc66a0fed3b436f3a7797e79a59c0443f191ddd02e"
)

INSTALL_URL_V0_60_0 = (
    "https://github.com/controlplaneio-fluxcd/flux-operator/releases/download/"
    "v0.60.0/install.yaml"
)


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
