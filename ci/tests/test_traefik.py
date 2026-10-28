"""Traefik HelmChartConfig (p0b-03): plain Ingress on the edge Service."""

from __future__ import annotations

import yaml

from test_flux_entry import _kustomize_build


def test_traefik_helmchartconfig_rules() -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(_kustomize_build("platform/flux-operator"))
        if isinstance(doc, dict)
    ]
    assert not any(
        str(doc.get("apiVersion", "")).startswith("gateway.networking.k8s.io")
        for doc in docs
    )
    kube_system = [
        doc
        for doc in docs
        if isinstance(doc.get("metadata"), dict)
        and doc["metadata"].get("namespace") == "kube-system"
    ]
    assert [doc["kind"] for doc in kube_system] == ["HelmChartConfig"]
    values = yaml.safe_load(kube_system[0]["spec"]["valuesContent"])
    assert values["providers"]["kubernetesGateway"]["enabled"] is False
    assert (
        "--providers.kubernetesCRD.defaultTLSResourcesNamespace=platform-cert-manager"
        in values["additionalArguments"]
    )
    assert values["service"]["spec"]["type"] == "LoadBalancer"
    assert values["resources"]["limits"]["memory"]
    assert values["resources"]["requests"]["cpu"]
    assert "cpu" not in values["resources"]["limits"]
    # Chart defaults set both probes. An override must not turn them off.
    assert values.get("readinessProbe", True)
    assert values.get("livenessProbe", True)
