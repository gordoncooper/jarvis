"""Pinned flux-operator install.yaml vs chart 0.60.0; adoption kubectl rendering."""

from __future__ import annotations

import hashlib
import shlex
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest
import yaml

from _flux_operator_install import (
    FLUX_OPERATOR_CHART_OCI,
    FLUX_OPERATOR_CHART_VERSION,
    INSTALL_CHART_DIFF_CHART_ONLY,
    INSTALL_CHART_DIFF_INSTALL_ONLY,
    INSTALL_SHA256_V0_60_0,
    INSTALL_URL_V0_60_0,
    parse_install_manifest,
    resource_keys,
)
from ansible_role_proof import (
    FLUX_OPERATOR_ROLE_DIR,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    REPO_ROOT,
    ansible_render_template,
)

HELM_ADOPT_VARS = (
    REPO_ROOT / "infra/ansible/roles/flux-operator/vars/helm_adopt_resources.yml"
)
ADOPT_SAMPLE = {
    "k3s_binary_path": "/usr/local/bin/k3s",
    "flux_operator_deployment_name": "flux-operator",
    "flux_system_namespace": "flux-system",
    "item": {
        "kind": "Deployment",
        "kubectl_resource": "deployment",
        "name": "flux-operator",
        "namespace": "flux-system",
    },
}


def _require_helm() -> str:
    path = shutil.which("helm")
    if not path:
        pytest.fail("helm not found on PATH (run make deps)")
    return path


def _download_install_manifest() -> str:
    with urllib.request.urlopen(INSTALL_URL_V0_60_0, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == INSTALL_SHA256_V0_60_0
    return data.decode("utf-8")


def _helm_template_manifest() -> str:
    helm = _require_helm()
    proc = subprocess.run(
        [
            helm,
            "template",
            "flux-operator",
            FLUX_OPERATOR_CHART_OCI,
            "--version",
            FLUX_OPERATOR_CHART_VERSION,
            "--namespace",
            "flux-system",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _assert_kubectl_verb_argv(argv: list[str], verb: str) -> None:
    assert len(argv) >= 4
    assert argv[0].endswith("k3s") or argv[0] == "k3s"
    assert argv[1] == "kubectl"
    assert argv[2] == verb
    assert argv[3] == "--overwrite"


def test_install_yaml_and_chart_resource_keys_have_known_difference() -> None:
    install_keys = resource_keys(parse_install_manifest(_download_install_manifest()))
    chart_keys = resource_keys(parse_install_manifest(_helm_template_manifest()))
    assert install_keys != chart_keys
    assert chart_keys - install_keys == INSTALL_CHART_DIFF_CHART_ONLY
    assert install_keys - chart_keys == INSTALL_CHART_DIFF_INSTALL_ONLY


def test_ansible_helm_adopt_vars_match_install_manifest() -> None:
    text = _download_install_manifest()
    install_keys = resource_keys(parse_install_manifest(text))
    vars_doc = yaml.safe_load(HELM_ADOPT_VARS.read_text(encoding="utf-8"))
    assert isinstance(vars_doc, dict)
    entries = vars_doc["flux_operator_helm_adopt_resources"]
    var_keys = frozenset(
        (item["kind"], item.get("namespace", ""), item["name"]) for item in entries
    )
    assert var_keys == install_keys


def test_adopt_label_command_renders_valid_kubectl_argv() -> None:
    rendered = ansible_render_template(
        "adopt-label.cmd.j2",
        ADOPT_SAMPLE,
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    ).strip()
    argv = shlex.split(rendered)
    _assert_kubectl_verb_argv(argv, "label")
    assert argv[-1] == "app.kubernetes.io/managed-by=Helm"
    assert "deployment/flux-operator" in argv


def test_adopt_annotate_command_renders_valid_kubectl_argv() -> None:
    rendered = ansible_render_template(
        "adopt-annotate.cmd.j2",
        ADOPT_SAMPLE,
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    ).strip()
    argv = shlex.split(rendered)
    _assert_kubectl_verb_argv(argv, "annotate")
    assert "meta.helm.sh/release-name=flux-operator" in argv
    assert "meta.helm.sh/release-namespace=flux-system" in argv


def test_adopt_label_cluster_scoped_resource_omits_namespace_flag() -> None:
    sample = {
        **ADOPT_SAMPLE,
        "item": {
            "kind": "Namespace",
            "kubectl_resource": "namespace",
            "name": "flux-system",
            "namespace": "",
        },
    }
    rendered = ansible_render_template(
        "adopt-label.cmd.j2",
        sample,
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    ).strip()
    argv = shlex.split(rendered)
    _assert_kubectl_verb_argv(argv, "label")
    assert "-n" not in argv
    assert "namespace/flux-system" in argv


def test_operator_bootstrap_uses_helm_chart_not_install_yaml() -> None:
    operator = (
        FLUX_OPERATOR_ROLE_DIR / "tasks" / "operator.yml"
    ).read_text(encoding="utf-8")
    assert "upgrade --install" in operator
    assert "charts/flux-operator" in operator
    assert "kubectl apply" not in operator
    assert "flux-operator-cluster-admin" in (
        REPO_ROOT / "infra/ansible/roles/flux-operator/tasks/operator.yml"
    ).read_text(encoding="utf-8")
