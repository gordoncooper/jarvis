"""Backup enrollment rules: Kyverno install and the PVC GeneratingPolicies."""

from __future__ import annotations

import base64
import json
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
    namespaces = ["monitoring", "app-notes", "app-web-demo", "platform-backups", "kube-system"]
    return [*({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}} for name in namespaces),
            _credential("backups-restic", "rest:http://restic:8000/", "core"),
            _credential("backups-restic-app-notes", "rest:http://restic-apps:8000/", "notes"),
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "backup-overrides", "namespace": "platform-backups"},
             "data": {"app-notes": json.dumps({"backup_schedule": "15 4 * * *", "backup_retention_daily": 0})}},
            *extra]


def _generated(tmp_path: Path, claims: list[dict], cluster: list[dict]) -> dict[tuple[str, str, str], dict]:
    policies = flux_envsubst(_docs(ROOT / "core/platform/backups/enrollment.yaml"), _ENV)
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


