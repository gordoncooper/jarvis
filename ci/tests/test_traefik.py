"""Traefik HelmChartConfig (p0b-03): plain Ingress, default TLSStore, edge ports."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import REPO_ROOT, non_example_env_names
from test_flux_entry import _kustomize_build

_SECRET_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_PLATFORM_CONTROLLERS = "platform-controllers"


def _docs(manifests: str) -> list[dict[str, object]]:
    return [doc for doc in yaml.safe_load_all(manifests) if isinstance(doc, dict)]


def _traefik_config(docs: list[dict[str, object]]) -> dict[str, object]:
    kube_system = [
        doc
        for doc in docs
        if isinstance(doc.get("metadata"), dict)
        and doc["metadata"].get("namespace") == "kube-system"
    ]
    assert len(kube_system) == 1
    doc = kube_system[0]
    assert doc["apiVersion"] == "helm.cattle.io/v1"
    assert doc["kind"] == "HelmChartConfig"
    assert doc["metadata"]["name"] == "traefik"
    return doc


def test_traefik_helmchartconfig_is_plain_ingress() -> None:
    docs = _docs(_kustomize_build("platform/flux-operator"))
    for doc in docs:
        api = doc.get("apiVersion")
        assert not (isinstance(api, str) and api.startswith("gateway.networking.k8s.io"))
        assert doc.get("kind") not in {"Gateway", "GatewayClass", "HTTPRoute"}
        metadata = doc.get("metadata")
        if doc.get("kind") == "ConfigMap" and isinstance(metadata, dict):
            name = metadata.get("name")
            assert not (isinstance(name, str) and name.startswith("placeholder"))

    config = _traefik_config(docs)
    values_content = config["spec"]["valuesContent"]
    assert isinstance(values_content, str)
    values = yaml.safe_load(values_content)
    assert isinstance(values, dict)

    assert values["ingressClass"]["name"] == "traefik"
    assert values["ingressClass"]["isDefaultClass"] is True
    assert values["providers"]["kubernetesGateway"]["enabled"] is False
    assert values["gateway"]["enabled"] is False
    assert values["gatewayClass"]["enabled"] is False
    assert values["experimental"]["kubernetesGateway"]["enabled"] is False
    assert values["service"]["spec"]["type"] == "LoadBalancer"
    assert "ports" not in values
    assert "hostPort" not in values_content

    assert values["readinessProbe"]
    assert values["livenessProbe"]
    resources = values["resources"]
    assert resources["requests"]["memory"] == "128Mi"
    assert resources["requests"]["cpu"] == "100m"
    assert resources["limits"]["memory"] == "256Mi"
    assert "cpu" not in resources["limits"]

    secret_name = values["tlsStore"]["default"]["defaultCertificate"]["secretName"]
    assert isinstance(secret_name, str) and _SECRET_NAME.fullmatch(secret_name)

    yaml_hits = [
        path
        for path in REPO_ROOT.joinpath("platform").rglob("*.yaml")
        if secret_name in path.read_text(encoding="utf-8")
    ]
    assert yaml_hits == [REPO_ROOT / "platform" / "traefik" / "helmchartconfig.yaml"]
    contract = (REPO_ROOT / "platform" / "traefik" / "CONTRACT.md").read_text(
        encoding="utf-8"
    )
    assert contract.count(secret_name) == 1
    assert secret_name not in values_content.replace(
        f"secretName: {secret_name}", "", 1
    )


@pytest.mark.parametrize("env", non_example_env_names())
def test_platform_controllers_keeps_one_flux_path(env: str) -> None:
    path = (
        REPO_ROOT
        / "envs"
        / env
        / "flux"
        / "kustomizations"
        / f"{_PLATFORM_CONTROLLERS}.yaml"
    )
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    assert doc["spec"]["path"] == "./platform/flux-operator"
    resources = yaml.safe_load(
        (REPO_ROOT / "platform" / "flux-operator" / "kustomization.yaml").read_text(
            encoding="utf-8"
        )
    )["resources"]
    assert "../traefik" in resources
    flux_dir = path.parent
    traefik_paths = [
        item
        for item in flux_dir.glob("*.yaml")
        if "traefik" in item.read_text(encoding="utf-8")
    ]
    assert traefik_paths == []
