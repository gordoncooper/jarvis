"""Backup enrollment rules: Kyverno install and the PVC ClusterPolicy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from support import ExampleCopy, real_and_copy_envs
from support import REPO_ROOT as ROOT
from support import _kustomize_build, renovate_python_pattern


def _docs(path: Path) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def _rule(policy: dict, name: str) -> dict:
    return next(rule for rule in policy["spec"]["rules"] if rule["name"] == name)


def test_kyverno_chart_pins_and_controllers() -> None:
    source = (ROOT / "platform/kyverno/helmrelease.yaml").read_text(encoding="utf-8")
    release = next(doc for doc in yaml.safe_load_all(source) if isinstance(doc, dict))
    chart = release["spec"]["chart"]["spec"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", chart["version"])
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    chart_managers = [
        manager
        for manager in renovate["customManagers"]
        if any(
            "kyverno" in pattern and "helmrelease" in pattern
            for pattern in manager["managerFilePatterns"]
        )
        and any("registryUrl" in pattern for pattern in manager["matchStrings"])
    ]
    assert len(chart_managers) == 1
    chart_pattern = renovate_python_pattern(chart_managers[0]["matchStrings"][0])
    chart_found = list(re.finditer(chart_pattern, source))
    assert len(chart_found) == 1
    assert chart_found[0].group("currentValue") == chart["version"]
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
    tags = set()
    for image in images:
        assert image["registry"] == "ghcr.io"
        tag, digest = image["tag"].split("@", 1)
        assert re.fullmatch(r"v\d+\.\d+\.\d+", tag)
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
        tags.add(tag)
    assert len(tags) == 1
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
    # A forbidden read silently means no override, so the role must name the ConfigMap the policy reads.
    assert "backup-overrides" in by_resource[("configmaps",)]["resourceNames"]
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
    assert secret["exclude"] == source["exclude"]
    # Core's credential never reaches a hosted app's namespace; the -app rules cover those.
    assert {"namespaces": ["app-*"]} in [item["resources"] for item in secret["exclude"]["any"]]
    assert mutate["exclude"]["any"] == [
        item for item in secret["exclude"]["any"] if item["resources"] != {"namespaces": ["app-*"]}
    ]
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


def test_app_pvc_rules_use_only_that_apps_credential() -> None:
    """An app- PVC's Secret is built from platform-backups/backups-restic-<namespace> only, and
    neither its Secret nor its ReplicationSource is generated while that credential is missing.
    The ReplicationSource is the core one, hold and pause included."""
    policy = next(
        doc for doc in _docs(ROOT / "platform/backups/enrollment.yaml") if doc.get("kind") == "ClusterPolicy"
    )
    secret, source = _rule(policy, "restic-secret-app"), _rule(policy, "replication-source-app")
    url = "/api/v1/namespaces/platform-backups/secrets/backups-restic-{{ request.object.metadata.namespace }}"
    for rule in (secret, source):
        assert rule["match"]["any"][0]["resources"]["namespaces"] == ["app-*"]
        assert [entry["apiCall"]["urlPath"] for entry in rule["context"] if "/secrets/" in entry.get("apiCall", {}).get("urlPath", "")] == [url]
        assert len(rule["preconditions"]["all"]) == 2
    assert next(entry for entry in secret["context"] if entry["name"] == "appRestic")["apiCall"]["default"] == {}
    assert {"key": "{{ appRestic.RESTIC_PASSWORD || '' }}", "operator": "NotEquals", "value": ""} in secret["preconditions"]["all"]
    assert {"key": "{{ appCredential }}", "operator": "Equals", "value": "true"} in source["preconditions"]["all"]
    assert all("backupsRestic" not in value for value in secret["generate"]["data"]["data"].values())
    assert secret["generate"]["name"] == _rule(policy, "restic-secret")["generate"]["name"]
    assert source["generate"] == _rule(policy, "replication-source")["generate"]


def test_enrollment_policy_is_on_the_platform_configs_build(example_copy: ExampleCopy) -> None:
    """ClusterPolicy pvc-backup is on the platform-configs build, and not a ReplicationSource."""
    for root, env in real_and_copy_envs(example_copy):
        _assert_enrollment_on_platform_configs(root, env)


def _assert_enrollment_on_platform_configs(root, env: str) -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(
            _kustomize_build(f"envs/{env}/flux/platform-configs", root=root)
        )
        if isinstance(doc, dict)
    ]
    policies = [doc for doc in docs if doc.get("kind") == "ClusterPolicy"]
    # app-derive and app-priority are the hosted apps' (platform/app-hosting, when the env turns
    # it on); they make no backup object.
    from schema.env import app_hosting_listed

    hosting = ["app-derive", "app-priority"] if app_hosting_listed(root / "envs" / env) else []
    assert sorted(doc["metadata"]["name"] for doc in policies) == [*hosting, "pvc-backup"]
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
