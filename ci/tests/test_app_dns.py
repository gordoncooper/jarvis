"""App DNS (p0b-05): k8s_gateway chart and the coredns-custom forward."""

from __future__ import annotations

import json

import yaml

from ansible_role_proof import REPO_ROOT
from test_flux_entry import _kustomize_build


def test_app_dns_service_zone_forward_and_resources() -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(_kustomize_build("platform"))
        if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict)
    ]
    release = next(
        doc
        for doc in docs
        if doc["kind"] == "HelmRelease" and doc["metadata"]["name"] == "k8s-gateway"
    )
    values = release["spec"]["values"]
    # The chart templates a LoadBalancer Service. use_tcp on the zone opens UDP and TCP.
    assert values["serviceType"] == "LoadBalancer"
    assert values["service"]["clusterIP"] == "${app_dns_cluster_ip}"
    server = values["servers"][0]
    assert server["port"] == 53
    assert server["zones"] == [{"zone": "${dns_suffix}", "use_tcp": True}]
    plugins = {plugin["name"]: plugin for plugin in server["plugins"]}
    assert plugins["k8s_gateway"]["parameters"] == "${dns_suffix}"
    block = plugins["k8s_gateway"]["configBlock"]
    assert "resources Ingress" in block
    assert "loadBalancerAddressPreference ip" in block
    assert "health" in plugins and "ready" in plugins
    resources = values["resources"]
    assert resources["requests"]["cpu"]
    assert resources["requests"]["memory"]
    assert resources["limits"]["memory"]
    assert resources["limits"].get("cpu") is None
    assert values["livenessProbe"]["enabled"] is True
    assert values["readinessProbe"]["enabled"] is True

    custom = next(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "coredns-custom"
    )
    corefile = custom["data"]["app-dns.server"]
    assert "${dns_suffix}:53" in corefile
    assert "forward . ${app_dns_cluster_ip}" in corefile

    renovate = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("app-dns" in pattern for pattern in manager["managerFilePatterns"])
    ]
    assert len(managers) == 1
    assert any("k8s-gateway" in pattern for pattern in managers[0]["matchStrings"])
