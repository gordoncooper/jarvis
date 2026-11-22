"""Backup enrollment rules: core's PVC GeneratingPolicy and app-hosting's for hosted apps."""

from __future__ import annotations

import base64
from pathlib import Path

import yaml

from support import REPO_ROOT as ROOT
from support import (
    flux_envsubst,
    kyverno_generated,
    sample_root,
)


def _docs(path: Path) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


_ENV = {key: str(value) for key, value in yaml.safe_load(
    (sample_root() / "envs/env-00/values/cluster-vars.yaml").read_text(encoding="utf-8")).items()
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
    # A core layer's namespace, two apps', core's own, k3s's, and one made by hand.
    namespaces = {"monitoring": {"jarvis.io/layer": "feature"}, "app-notes": {"hosting.jarvis.io/app": "notes"},
                  "app-web-demo": {"hosting.jarvis.io/app": "web-demo"}, "platform-backups": {"jarvis.io/layer": "platform"},
                  "kube-system": {}, "by-hand": {}}  # fmt: skip
    return [*({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}} for name, labels in namespaces.items()),
            _credential("backups-restic", "rest:http://restic:8000/", "core"),
            _credential("backups-restic-app-notes", "rest:http://restic-apps:8000/", "notes"),
            # Every cluster has this one; the CLI cannot list a kind its context holds none of.
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "kube-root-ca.crt", "namespace": "kube-system"}},
            *extra]


def _generated(tmp_path: Path, claims: list[dict], cluster: list[dict]) -> dict[tuple[str, str, str], dict]:
    policies = flux_envsubst([*_docs(ROOT / "core/platform/backups/enrollment.yaml"),
                              *_docs(ROOT / "core/feature/app-hosting/backups/policies.yaml")], _ENV)
    made = kyverno_generated(tmp_path, policies, claims, cluster)
    return {(doc["kind"], doc["metadata"]["namespace"], doc["metadata"]["name"]): doc for doc in made}


def _source(name: str, *, paused: bool = False) -> dict:
    return {"paused": paused, "sourcePVC": name, "trigger": {"schedule": _ENV["backup_schedule"]},
            "restic": {"repository": f"restic-{name}", "copyMethod": "Direct", "cacheCapacity": "1Gi", "pruneIntervalDays": 7,
                       "retain": {"daily": int(_ENV["backup_retention_daily"]),
                                  "weekly": int(_ENV["backup_retention_weekly"]),
                                  "monthly": int(_ENV["backup_retention_monthly"]), "within": "7d"}}}


def test_enrollment_generates_a_secret_and_source_per_bound_claim(tmp_path: Path) -> None:
    """Rule: a bound claim gets Secret restic-<pvc> and ReplicationSource <pvc> on the env's
    schedule and retention; a claim in a core layer's namespace reads core's credential, an app-
    claim only its own; an app with no credential, a namespace with no layer, an unbound claim,
    backup: none and a mover's claim get nothing; backup: paused pauses its source."""
    claims = [
        _claim("data", "monitoring"),
        _claim("held", "monitoring", backup="paused"),
        _claim("db", "app-notes"),
        _claim("db", "app-web-demo"),
        _claim("waiting", "monitoring", bound=False),
        _claim("scratch", "monitoring", backup="none"),
        _claim("data", "by-hand"),
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
    assert made[("ReplicationSource", "app-notes", "db")]["spec"] == _source("db")


def test_rebuild_hold_pauses_every_source(tmp_path: Path) -> None:
    """Rule: while kube-system/backup-rebuild-hold exists, every generated source is paused."""
    hold = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "backup-rebuild-hold", "namespace": "kube-system"}}
    made = _generated(tmp_path, [_claim("data", "monitoring"), _claim("db", "app-notes")], _cluster(hold))
    sources = [doc for doc in made.values() if doc["kind"] == "ReplicationSource"]
    assert len(sources) == 2
    assert all(doc["spec"]["paused"] is True for doc in sources)


