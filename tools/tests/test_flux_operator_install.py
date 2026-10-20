"""Flux Operator Ansible helm bootstrap aligns with Git HelmRelease (p0a-14)."""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from ansible_role_proof import (
    FLUX_OPERATOR_ROLE_DIR,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    REPO_ROOT,
    ansible_render_template,
)

ROLE_DEFAULTS = FLUX_OPERATOR_ROLE_DIR / "defaults" / "main.yml"
OCI_REPOSITORY = REPO_ROOT / "platform" / "flux-operator" / "oci-repository.yaml"
HELM_RELEASE = REPO_ROOT / "platform" / "flux-operator" / "helmrelease.yaml"
FLUX_OPERATOR_CHART_OCI = "oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator"


def _load_role_defaults() -> dict[str, object]:
    data = yaml.safe_load(ROLE_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _helm_upgrade_variables() -> dict[str, object]:
    defaults = _load_role_defaults()
    return {
        "helm_binary_path": defaults["helm_binary_path"],
        "flux_operator_deployment_name": defaults["flux_operator_deployment_name"],
        "flux_operator_chart_version": defaults["flux_operator_chart_version"],
        "flux_system_namespace": defaults["flux_system_namespace"],
        "flux_operator_helm_timeout": defaults["flux_operator_helm_timeout"],
    }


def test_ansible_chart_pin_matches_git_helmrelease() -> None:
    defaults = _load_role_defaults()
    oci = yaml.safe_load(OCI_REPOSITORY.read_text(encoding="utf-8"))
    release = yaml.safe_load(HELM_RELEASE.read_text(encoding="utf-8"))
    assert isinstance(oci, dict)
    assert isinstance(release, dict)

    chart_version = defaults["flux_operator_chart_version"]
    assert oci["spec"]["ref"]["tag"] == chart_version
    assert release["metadata"]["name"] == defaults["flux_operator_deployment_name"]
    assert release["metadata"]["namespace"] == defaults["flux_system_namespace"]
    assert release["spec"]["releaseName"] == defaults["flux_operator_deployment_name"]
    assert release["spec"]["chartRef"]["name"] == defaults["flux_operator_deployment_name"]


def test_helm_upgrade_command_renders_expected_argv() -> None:
    variables = _helm_upgrade_variables()
    rendered = ansible_render_template(
        "helm-upgrade.cmd.j2",
        variables,
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    ).strip()
    argv = shlex.split(rendered)
    defaults = _load_role_defaults()
    chart_version = defaults["flux_operator_chart_version"]
    assert argv[0] == defaults["helm_binary_path"]
    assert argv[1:5] == [
        "upgrade",
        "--install",
        defaults["flux_operator_deployment_name"],
        FLUX_OPERATOR_CHART_OCI,
    ]
    version_index = argv.index("--version")
    assert argv[version_index + 1] == chart_version
    assert "--namespace" in argv
    ns_index = argv.index("--namespace")
    assert argv[ns_index + 1] == defaults["flux_system_namespace"]
