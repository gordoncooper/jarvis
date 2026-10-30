"""Backup enrollment rules: Kyverno install and the PVC ClusterPolicy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import non_example_env_names
from test_flux_entry import _kustomize_build

ROOT = Path(__file__).resolve().parents[2]


def _docs(path: Path) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def _rule(policy: dict, name: str) -> dict:
    return next(rule for rule in policy["spec"]["rules"] if rule["name"] == name)


def test_kyverno_chart_pins_and_controllers() -> None:
    release = _docs(ROOT / "platform/kyverno/helmrelease.yaml")[0]
    chart = release["spec"]["chart"]["spec"]
    assert chart["version"] == "3.9.2"
    assert chart["sourceRef"]["name"] == "kyverno"
    values = release["spec"]["values"]
    assert values["cleanupController"]["enabled"] is False
    assert values["reportsController"]["enabled"] is False
    reporting = values["features"]["reporting"]
    assert reporting == {
        "validate": False,
        "mutate": False,
        "mutateExisting": False,
        "imageVerify": False,
        "generate": False,
    }
    images = [
        values["admissionController"]["container"]["image"],
        values["admissionController"]["initContainer"]["image"],
        values["backgroundController"]["image"],
    ]
    for image in images:
        assert image["registry"] == "ghcr.io"
        assert re.fullmatch(r"v1\.19\.2@sha256:[a-f0-9]{64}", image["tag"])
    # D30. Init container and webhooksCleanup. The migration hook is excluded:
    # crds is a subchart, and Helm keeps null under that key, so cpu: null
    # would render as a limit of 0 and the post-upgrade Job would be rejected.
    for block in (
        values["admissionController"]["initContainer"]["resources"],
        values["webhooksCleanup"]["resources"],
    ):
        assert block["limits"]["cpu"] is None
        assert block["limits"]["memory"]
        assert block["requests"]["memory"]
    assert "crds" not in values
    controllers = yaml.safe_load(
        (ROOT / "platform/kustomization.yaml").read_text(encoding="utf-8")
    )
    assert "kyverno" in controllers["resources"]


def test_kyverno_roles_aggregate_secret_and_replicationsource_permissions() -> None:
    roles = {
        doc["metadata"]["name"]: doc
        for doc in _docs(ROOT / "platform/kyverno/rbac.yaml")
    }
    generate = roles["kyverno:backup-generate"]
    labels = generate["metadata"]["labels"]
    assert labels["rbac.kyverno.io/aggregate-to-admission-controller"] == "true"
    assert labels["rbac.kyverno.io/aggregate-to-background-controller"] == "true"
    by_resource = {tuple(rule["resources"]): rule for rule in generate["rules"]}
    assert "create" in by_resource[("secrets",)]["verbs"]
    assert "get" in by_resource[("secrets",)]["verbs"]
    assert "create" in by_resource[("replicationsources",)]["verbs"]
    namespaces = roles["kyverno:backup-namespaces"]
    assert (
        namespaces["metadata"]["labels"]["rbac.kyverno.io/aggregate-to-background-controller"]
        == "true"
    )
    assert "rbac.kyverno.io/aggregate-to-admission-controller" not in namespaces["metadata"]["labels"]
    assert "update" in namespaces["rules"][0]["verbs"]
    assert namespaces["rules"][0]["resources"] == ["namespaces"]


def test_renovate_tracks_the_kyverno_chart_and_images() -> None:
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any(
            "kyverno" in pattern and "helmrelease" in pattern
            for pattern in manager["managerFilePatterns"]
        )
    ]
    assert len(managers) == 2
    assert any(manager.get("datasourceTemplate") == "docker" for manager in managers)
    assert any(
        "currentDigest" in pattern and "currentValue" in pattern
        for manager in managers
        for pattern in manager["matchStrings"]
    )
    groups = [rule for rule in renovate["packageRules"] if rule.get("groupName") == "kyverno-image"]
    assert len(groups) == 1
    assert "ghcr.io/kyverno/background-controller" in groups[0]["matchDepNames"]


def test_enrollment_policy_rules() -> None:
    policy = next(
        doc
        for doc in _docs(ROOT / "platform/backups/enrollment.yaml")
        if doc.get("kind") == "ClusterPolicy"
    )
    assert policy["metadata"]["name"] == "pvc-backup"
    assert policy["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"] == "Enabled"
    assert "failurePolicy" not in policy["spec"]
    assert policy["spec"]["webhookConfiguration"]["failurePolicy"] == "Ignore"
    assert policy["spec"].get("background", True) is True
    secret, source, mutate = (
        _rule(policy, "restic-secret"),
        _rule(policy, "replication-source"),
        _rule(policy, "privileged-movers"),
    )
    assert secret["match"] == source["match"] == mutate["match"]
    assert secret["exclude"] == source["exclude"] == mutate["exclude"]
    excluded = secret["exclude"]["any"]
    assert {"annotations": {"backup": "none"}} in [
        item["resources"] for item in excluded
    ]
    assert {"selector": {"matchLabels": {"app.kubernetes.io/created-by": "volsync"}}} in [
        item["resources"] for item in excluded
    ]
    assert secret["preconditions"] == source["preconditions"] == {
        "all": [
            {
                "key": "{{ request.object.spec.volumeName || '' }}",
                "operator": "NotEquals",
                "value": "",
            }
        ]
    }

    assert secret["generate"]["generateExisting"] is True
    assert secret["generate"]["synchronize"] is True
    assert secret["generate"]["name"].startswith("restic-")

    assert source["generate"]["kind"] == "ReplicationSource"
    assert source["generate"]["generateExisting"] is True
    assert source["generate"]["synchronize"] is True
    spec = source["generate"]["data"]["spec"]
    assert "manual" not in spec["trigger"]
    pause = next(entry for entry in source["context"] if entry["name"] == "pauseBackup")
    assert pause["variable"]["default"] == "false"
    assert "annotations.backup" in pause["variable"]["jmesPath"]
    assert "'paused'" in pause["variable"]["jmesPath"]
    assert spec["paused"] == "{{ to_boolean(pauseBackup) }}"
    assert spec["restic"]["retain"]["within"] == "7d"
    assert "rest:http://" not in (ROOT / "platform/backups/enrollment.yaml").read_text(encoding="utf-8")

    assert mutate["mutate"]["mutateExistingOnPolicyUpdate"] is True
    target = mutate["mutate"]["targets"][0]
    assert target["kind"] == "Namespace"
    assert target["name"] == "{{ request.object.metadata.namespace }}"
    annotation = mutate["mutate"]["patchStrategicMerge"]["metadata"]["annotations"]
    assert annotation["volsync.backube/privileged-movers"] == "true"


@pytest.mark.parametrize("env", non_example_env_names())
def test_enrollment_policy_is_on_the_platform_configs_build(env: str) -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(_kustomize_build(f"envs/{env}/flux/platform-configs"))
        if isinstance(doc, dict)
    ]
    policies = [doc for doc in docs if doc.get("kind") == "ClusterPolicy"]
    assert [doc["metadata"]["name"] for doc in policies] == ["pvc-backup"]
    assert not any(doc.get("kind") == "ReplicationSource" for doc in docs)
    controllers = [
        doc
        for doc in yaml.safe_load_all(_kustomize_build("platform"))
        if isinstance(doc, dict)
    ]
    assert any(
        doc.get("kind") == "HelmRelease" and doc["metadata"]["name"] == "kyverno"
        for doc in controllers
    )
