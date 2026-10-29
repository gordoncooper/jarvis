"""Traefik HelmChartConfig (p0b-03): plain Ingress on the edge Service."""

from __future__ import annotations

import json
import re

import yaml

from ansible_role_proof import REPO_ROOT, load_cluster_vars, non_example_env_names
from test_flux_entry import _kustomize_build

# The example env keeps a placeholder k3s_version and is not a cluster pin.
_CHECKED_LINE = re.compile(
    r"^-\s+Checked: bundled Traefik chart (?P<chart>v\d+) on k3s "
    r"(?P<k3s>v\d+\.\d+\.\d+\+k3s\d+)\.$",
    re.MULTILINE,
)


def test_traefik_helmchartconfig_rules() -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(_kustomize_build("platform"))
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


def _checked_k3s(contract: str) -> str:
    match = _CHECKED_LINE.search(contract)
    assert match is not None, "Traefik CONTRACT.md has no checked k3s line"
    assert match.group("chart")
    return match.group("k3s")


def _k3s_mismatches(contract: str, versions: dict[str, str]) -> list[str]:
    pinned = _checked_k3s(contract)
    return sorted(env for env, version in versions.items() if version != pinned)


def test_env_k3s_version_matches_traefik_contract() -> None:
    contract = (REPO_ROOT / "platform/traefik/CONTRACT.md").read_text(encoding="utf-8")
    envs = non_example_env_names()
    assert envs
    versions = {
        env: str(load_cluster_vars(env=env)["k3s_version"])
        for env in envs
    }
    assert _k3s_mismatches(contract, versions) == []


def test_seeded_k3s_bump_without_contract_update_fails() -> None:
    contract = (REPO_ROOT / "platform/traefik/CONTRACT.md").read_text(encoding="utf-8")
    pinned = _checked_k3s(contract)
    bumped = f"{pinned[:-1]}{int(pinned[-1]) + 1}"
    assert bumped != pinned
    assert _k3s_mismatches(contract, {"env-seed": bumped}) == ["env-seed"]


def test_k3s_renovate_rule_names_the_traefik_recheck() -> None:
    renovate = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    rule = next(
        item
        for item in renovate["packageRules"]
        if item.get("matchPackageNames") == ["k3s-io/k3s"]
    )
    assert rule["enabled"] is False
    assert "prBodyNotes" not in rule
    description = rule["description"]
    assert "re-check" in description
    assert "platform/traefik" in description
    assert "CONTRACT.md" in description
