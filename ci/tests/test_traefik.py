"""Traefik HelmChartConfig: plain Ingress on the edge Service."""

from __future__ import annotations

import json
import re

import yaml
from ansible_role_proof import REPO_ROOT, load_cluster_vars

from jarvis.lib.k3s import release as k3s_release
from support import ExampleCopy, _kustomize_build, real_and_copy_envs

# The example env's pin is the contract pin. The check runs on real envs and a copy.
_CHECKED_LINE = re.compile(
    r"^-\s+Checked: bundled Traefik chart (?P<chart>v\d+) on k3s "
    r"(?P<k3s>v\d+\.\d+\.\d+\+k3s\d+)\.$",
    re.MULTILINE,
)


def test_traefik_helmchartconfig_rules() -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(_kustomize_build("platform") + "\n---\n" + _kustomize_build("platform/optional/app-dns"))
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
    assert {(doc["kind"], doc["metadata"]["name"]) for doc in kube_system} == {
        ("HelmChartConfig", "traefik"),
        ("ConfigMap", "coredns-custom"),
    }
    chart = next(doc for doc in kube_system if doc["kind"] == "HelmChartConfig")
    values = yaml.safe_load(chart["spec"]["valuesContent"])
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


def test_env_k3s_version_matches_traefik_contract(example_copy: ExampleCopy) -> None:
    """Each full env's k3s_version is the pin the Traefik contract checks."""
    contract = (REPO_ROOT / "platform/traefik/CONTRACT.md").read_text(encoding="utf-8")
    versions = {
        env: k3s_release(load_cluster_vars(root, env=env))[0]
        for root, env in real_and_copy_envs(example_copy)
    }
    assert versions
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
    assert rule["dependencyDashboardApproval"] is True
    assert "enabled" not in rule
    assert "prBodyNotes" not in rule
    description = rule["description"]
    assert "re-check" in description
    assert "platform/traefik" in description
    assert "CONTRACT.md" in description
