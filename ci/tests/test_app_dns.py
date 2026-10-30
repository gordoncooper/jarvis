"""App DNS (p0b-05): k8s_gateway chart and the coredns-custom forward."""

from __future__ import annotations

import json
import re

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
    assert values["serviceType"] == "LoadBalancer"
    assert values["service"]["clusterIP"] == "${app_dns_cluster_ip}"
    server = values["servers"][0]
    assert server["port"] == 53
    assert any(
        zone.get("zone") == "${dns_suffix}" and zone.get("use_tcp") is True
        for zone in server["zones"]
    )
    resources = values["resources"]
    assert resources["requests"]["cpu"]
    assert resources["requests"]["memory"]
    assert resources["limits"]["memory"]
    assert resources["limits"].get("cpu") is None
    assert values["livenessProbe"]["enabled"] is True
    assert values["readinessProbe"]["enabled"] is True
    tag, digest = values["image"]["tag"].split("@", 1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag)
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", digest)

    custom = next(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "coredns-custom"
    )
    corefile = custom["data"]["app-dns.server"]
    assert "${dns_suffix}:53" in corefile
    assert "forward . ${app_dns_cluster_ip}" in corefile

    source = (REPO_ROOT / "platform/app-dns/helmrelease.yaml").read_text(encoding="utf-8")
    renovate = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("app-dns" in pattern for pattern in manager["managerFilePatterns"])
    ]
    chart_managers = [
        manager
        for manager in managers
        if any("version:" in pattern for pattern in manager["matchStrings"])
    ]
    image_managers = [
        manager
        for manager in managers
        if any("currentDigest" in pattern for pattern in manager["matchStrings"])
    ]
    assert len(chart_managers) == 1
    assert len(image_managers) == 1

    def _matches(manager: dict) -> list[re.Match[str]]:
        found = []
        for pattern in manager["matchStrings"]:
            # Renovate uses RE2 named groups; Python's re uses ?P<name>.
            python_pattern = re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", pattern)
            match = re.search(python_pattern, source)
            if match is not None:
                found.append(match)
        return found

    chart = _matches(chart_managers[0])
    assert len(chart) == 1
    assert chart[0].group("datasource") == "docker"
    assert re.fullmatch(r"\d+\.\d+\.\d+", chart[0].group("currentValue"))
    assert chart[0].group("currentValue") == release["spec"]["chart"]["spec"]["version"]
    image = _matches(image_managers[0])
    assert len(image) == 1
    assert image[0].group("depName") == "codeberg.org/k8s-gateway/k8s_gateway"
    assert image[0].group("currentValue") == tag
    assert image[0].group("currentDigest") == digest
