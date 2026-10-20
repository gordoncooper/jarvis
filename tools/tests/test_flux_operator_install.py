"""Pinned flux-operator install.yaml matches chart 0.60.0 for Helm adoption."""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import yaml

from _flux_operator_install import (
    FLUX_OPERATOR_CHART_0_60_0_RESOURCE_KEYS,
    INSTALL_SHA256_V0_60_0,
    INSTALL_URL_V0_60_0,
    parse_install_manifest,
    resource_keys,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HELM_ADOPT_VARS = (
    REPO_ROOT
    / "infra/ansible/roles/flux-operator/vars/helm_adopt_resources.yml"
)


def _download_install_manifest() -> str:
    with urllib.request.urlopen(INSTALL_URL_V0_60_0, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == INSTALL_SHA256_V0_60_0
    return data.decode("utf-8")


def test_install_manifest_matches_chart_resource_keys() -> None:
    text = _download_install_manifest()
    install_keys = resource_keys(parse_install_manifest(text))
    assert install_keys == FLUX_OPERATOR_CHART_0_60_0_RESOURCE_KEYS


def test_ansible_helm_adopt_vars_match_install_manifest() -> None:
    text = _download_install_manifest()
    install_keys = resource_keys(parse_install_manifest(text))
    vars_doc = yaml.safe_load(HELM_ADOPT_VARS.read_text(encoding="utf-8"))
    assert isinstance(vars_doc, dict)
    entries = vars_doc["flux_operator_helm_adopt_resources"]
    var_keys = frozenset(
        (item["kind"], item.get("namespace", ""), item["name"]) for item in entries
    )
    assert var_keys == install_keys == FLUX_OPERATOR_CHART_0_60_0_RESOURCE_KEYS


def test_ansible_role_stamps_helm_adoption_metadata() -> None:
    adopt = (
        REPO_ROOT / "infra/ansible/roles/flux-operator/tasks/operator_adopt.yml"
    ).read_text(encoding="utf-8")
    operator = (
        REPO_ROOT / "infra/ansible/roles/flux-operator/tasks/operator.yml"
    ).read_text(encoding="utf-8")
    assert "app.kubernetes.io/managed-by=Helm" in adopt
    assert "meta.helm.sh/release-name=" in adopt
    assert "operator_adopt.yml" in operator
    assert "Stamp Helm adoption metadata on install objects" in operator
