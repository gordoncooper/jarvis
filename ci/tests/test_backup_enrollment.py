"""Backup enrollment rules: Kyverno install and the PVC GeneratingPolicies."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import yaml

from jarvis.checks.manifests import substitute
from support import REPO_ROOT as ROOT
from support import ExampleCopy, _kustomize_build, kyverno_generated, real_and_copy_envs, renovate_python_pattern


def _docs(path: Path) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def test_kyverno_chart_pins_and_controllers() -> None:
    source = (ROOT / "core/platform/kyverno/helmrelease.yaml").read_text(encoding="utf-8")
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
        (ROOT / "core/platform/kustomization.yaml").read_text(encoding="utf-8")
    )
    assert "kyverno" in controllers["resources"]


def test_kyverno_roles_aggregate_secret_and_replicationsource_permissions() -> None:
    roles = {
        doc["metadata"]["name"]: doc
        for doc in _docs(ROOT / "core/platform/kyverno/rbac.yaml")
    }
    assert set(roles) == {"kyverno:backup-generate"}
    generate = roles["kyverno:backup-generate"]
    labels = generate["metadata"]["labels"]
    assert labels["rbac.kyverno.io/aggregate-to-admission-controller"] == "true"
    assert labels["rbac.kyverno.io/aggregate-to-background-controller"] == "true"
    by_resource = {tuple(rule["resources"]): rule for rule in generate["rules"]}
    assert "create" in by_resource[("secrets",)]["verbs"]
    assert "list" in by_resource[("secrets",)]["verbs"]
    # A forbidden list errors the policy, so the role must list ConfigMaps (no resourceNames).
    assert "list" in by_resource[("configmaps",)]["verbs"]
    assert "resourceNames" not in by_resource[("configmaps",)]
    assert "create" in by_resource[("replicationsources",)]["verbs"]


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
    groups = [rule for rule in renovate["packageRules"] if rule.get("groupName") == "kyverno"]
    assert len(groups) == 1
    assert "ghcr.io/kyverno/background-controller" in groups[0]["matchDepNames"]


_ENV = {key: str(value) for key, value in yaml.safe_load(
    (ROOT / "envs/env-00/values/cluster-vars.yaml").read_text(encoding="utf-8")).items()
    if key.startswith("backup_")}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _claim(name: str, namespace: str, *, bound: bool = True, **annotations: str) -> dict:
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": name, "namespace": namespace, "annotations": annotations},
             "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}
    if bound:
        claim["spec"]["volumeName"] = f"pv-{namespace}-{name}"
    return claim


def _credential(name: str, repository: str, password: str) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "platform-backups"},
            "data": {"RESTIC_REPOSITORY": _b64(repository), "RESTIC_PASSWORD": _b64(password),
                     "RESTIC_REST_USERNAME": _b64(f"user-{password}"), "RESTIC_REST_PASSWORD": _b64(f"rest-{password}")}}


def _cluster(*extra: dict) -> list[dict]:
    namespaces = ["monitoring", "app-notes", "app-web-demo", "platform-backups", "kube-system"]
    return [*({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}} for name in namespaces),
            _credential("backups-restic", "rest:http://restic:8000/", "core"),
            _credential("backups-restic-app-notes", "rest:http://restic-apps:8000/", "notes"),
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "backup-overrides", "namespace": "platform-backups"},
             "data": {"app-notes": json.dumps({"backup_schedule": "15 4 * * *", "backup_retention_daily": 0})}},
            *extra]


def _generated(tmp_path: Path, claims: list[dict], cluster: list[dict]) -> dict[tuple[str, str, str], dict]:
    policies = [substitute(doc, _ENV) for doc in _docs(ROOT / "core/platform/backups/enrollment.yaml")]
    made = kyverno_generated(tmp_path, policies, claims, cluster)
    return {(doc["kind"], doc["metadata"]["namespace"], doc["metadata"]["name"]): doc for doc in made}


def _source(name: str, *, paused: bool = False, schedule: str | None = None, daily: str | None = None) -> dict:
    return {"paused": paused, "sourcePVC": name, "trigger": {"schedule": schedule or _ENV["backup_schedule"]},
            "restic": {"repository": f"restic-{name}", "copyMethod": "Direct", "cacheCapacity": "1Gi", "pruneIntervalDays": 7,
                       "retain": {"daily": int(daily or _ENV["backup_retention_daily"]),
                                  "weekly": int(_ENV["backup_retention_weekly"]),
                                  "monthly": int(_ENV["backup_retention_monthly"]), "within": "7d"}}}


def test_enrollment_generates_a_secret_and_source_per_bound_claim(tmp_path: Path) -> None:
    """Rule: a bound claim gets Secret restic-<pvc> and ReplicationSource <pvc>; a core claim
    reads core's credential and the env's values, an app- claim only its own credential and its
    override; an app with no credential, an unbound claim, backup: none and a mover's claim get
    nothing; backup: paused pauses its source."""
    claims = [
        _claim("data", "monitoring"),
        _claim("held", "monitoring", backup="paused"),
        _claim("db", "app-notes"),
        _claim("db", "app-web-demo"),
        _claim("waiting", "monitoring", bound=False),
        _claim("scratch", "monitoring", backup="none"),
    ]
    mover = _claim("volsync-cache", "monitoring")
    mover["metadata"]["labels"] = {"app.kubernetes.io/created-by": "volsync"}
    made = _generated(tmp_path, [*claims, mover], _cluster())
    assert set(made) == {
        ("Secret", "monitoring", "restic-data"), ("ReplicationSource", "monitoring", "data"),
        ("Secret", "monitoring", "restic-held"), ("ReplicationSource", "monitoring", "held"),
        ("Secret", "app-notes", "restic-db"), ("ReplicationSource", "app-notes", "db"),
    }
    core = made[("Secret", "monitoring", "restic-data")]["data"]
    assert base64.b64decode(core["RESTIC_REPOSITORY"]).decode() == "rest:http://restic:8000/ns-monitoring/pvc-data"
    assert base64.b64decode(core["RESTIC_PASSWORD"]).decode() == "core"
    app = made[("Secret", "app-notes", "restic-db")]["data"]
    assert base64.b64decode(app["RESTIC_REPOSITORY"]).decode() == "rest:http://restic-apps:8000/ns-app-notes/pvc-db"
    assert {base64.b64decode(value).decode() for value in app.values()} & {"core", "user-core", "rest-core"} == set()
    assert made[("ReplicationSource", "monitoring", "data")]["spec"] == _source("data")
    assert made[("ReplicationSource", "monitoring", "held")]["spec"] == _source("held", paused=True)
    assert made[("ReplicationSource", "app-notes", "db")]["spec"] == _source("db", schedule="15 4 * * *", daily="0")
    assert "rest:http://" not in (ROOT / "core/platform/backups/enrollment.yaml").read_text(encoding="utf-8")


def test_rebuild_hold_pauses_every_source(tmp_path: Path) -> None:
    """Rule: while kube-system/backup-rebuild-hold exists, every generated source is paused."""
    hold = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "backup-rebuild-hold", "namespace": "kube-system"}}
    made = _generated(tmp_path, [_claim("data", "monitoring"), _claim("db", "app-notes")], _cluster(hold))
    sources = [doc for doc in made.values() if doc["kind"] == "ReplicationSource"]
    assert len(sources) == 2
    assert all(doc["spec"]["paused"] is True for doc in sources)


def test_enrollment_policies_sync_and_skip_prune() -> None:
    """Rule: both policies generate for existing claims and keep what they made in sync; Flux
    force-replaces them and never prunes them."""
    policies = _docs(ROOT / "core/platform/backups/enrollment.yaml")
    assert [(doc["kind"], doc["metadata"]["name"]) for doc in policies] == [
        ("GeneratingPolicy", "pvc-backup"), ("GeneratingPolicy", "pvc-backup-app")]
    for policy in policies:
        assert policy["metadata"]["annotations"] == {
            "kustomize.toolkit.fluxcd.io/force": "Enabled", "kustomize.toolkit.fluxcd.io/prune": "disabled"}
        assert policy["spec"]["evaluation"] == {"generateExisting": {"enabled": True}, "synchronize": {"enabled": True}}


def test_enrollment_policy_is_on_the_platform_configs_build(example_copy: ExampleCopy) -> None:
    """The enrollment policies are on the platform-configs build, no ClusterPolicy is, and no
    ReplicationSource is."""
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
    names = {doc["metadata"]["name"] for doc in docs if doc.get("kind") == "GeneratingPolicy"}
    assert {"pvc-backup", "pvc-backup-app"} <= names
    assert not any(doc.get("kind") == "ClusterPolicy" for doc in docs)
    assert not any(doc.get("kind") == "ReplicationSource" for doc in docs)
    controllers = [
        doc
        for doc in yaml.safe_load_all(_kustomize_build("core/platform"))
        if isinstance(doc, dict)
    ]
    assert any(
        doc.get("kind") == "HelmRelease" and doc["metadata"]["name"] == "kyverno"
        for doc in controllers
    )
