"""Backup enrollment rules: core's PVC GeneratingPolicy and app-hosting's for hosted apps."""

from __future__ import annotations

import base64
from pathlib import Path

import sample
import yaml

from jarvis.env import scaffold
from support import REPO_ROOT as ROOT
from support import flux_envsubst, kyverno_generated, yaml_docs

# The env's backup values, as jarvis init writes them.
_ENV = {key: str(value) for key, value in yaml.safe_load(scaffold.cluster_vars(sample.spec())).items() if key.startswith("backup_")}


def _claim(name: str, namespace: str, *, bound: bool = True, **annotations: str) -> dict:
    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": name, "namespace": namespace, "annotations": annotations},
             "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}
    if bound:
        claim["spec"]["volumeName"] = f"pv-{namespace}-{name}"
    return claim


def _values(repository: str, password: str) -> dict[str, str]:
    """A restic credential's keys: the repository, and a password and login told apart by password."""
    return {"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": password,
            "RESTIC_REST_USERNAME": f"user-{password}", "RESTIC_REST_PASSWORD": f"rest-{password}"}


def _credential(name: str, repository: str, password: str) -> dict:
    data = {key: base64.b64encode(value.encode()).decode() for key, value in _values(repository, password).items()}
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "platform-backups"}, "data": data}


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
    policies = flux_envsubst([*yaml_docs(ROOT / "core/platform/backups/enrollment.yaml"),
                              *yaml_docs(ROOT / "core/feature/app-hosting/backups/policies.yaml")], _ENV)
    made = kyverno_generated(tmp_path, policies, claims, cluster)
    return {(doc["kind"], doc["metadata"]["namespace"], doc["metadata"]["name"]): doc for doc in made}


def _decided(doc: dict) -> dict:
    """A generated Secret's data, decoded, or a source's spec less the policy's own cache, prune and within
    settings, which are its to tune."""
    if doc["kind"] == "Secret":
        return {key: base64.b64decode(value).decode() for key, value in doc["data"].items()}
    restic = {key: value for key, value in doc["spec"]["restic"].items() if key not in ("cacheCapacity", "pruneIntervalDays")}
    return {**doc["spec"], "restic": {**restic, "retain": {key: value for key, value in restic["retain"].items() if key != "within"}}}


def _source(name: str, *, paused: bool = False) -> dict:
    """A claim's source: the env's schedule and retention, its own Secret, read in place (local-path has no snapshots)."""
    retain = {key: int(_ENV[f"backup_retention_{key}"]) for key in ("daily", "weekly", "monthly")}
    return {"paused": paused, "sourcePVC": name, "trigger": {"schedule": _ENV["backup_schedule"]},
            "restic": {"repository": f"restic-{name}", "copyMethod": "Direct", "retain": retain}}


def test_enrollment_generates_a_secret_and_source_per_bound_claim(tmp_path: Path) -> None:
    """Rule: in a core layer and in an app with its credential, a bound claim gets Secret restic-<pvc> (core's or that app's
    credential) and a source on the env's schedule and retention, paused if backup: paused; any other claim gets nothing."""
    claims = [
        _claim("data", "monitoring"),
        _claim("held", "monitoring", backup="paused"),
        _claim("db", "app-notes"),
        _claim("held", "app-notes", backup="paused"),
        _claim("db", "app-web-demo"),
        _claim("waiting", "monitoring", bound=False),
        _claim("waiting", "app-notes", bound=False),
        _claim("scratch", "monitoring", backup="none"),
        _claim("scratch", "app-notes", backup="none"),
        _claim("data", "by-hand"),
    ]
    movers = [_claim("volsync-cache", namespace) for namespace in ("monitoring", "app-notes")]
    for mover in movers:
        mover["metadata"]["labels"] = {"app.kubernetes.io/created-by": "volsync"}
    made = _generated(tmp_path, [*claims, *movers], _cluster())
    assert {key: _decided(doc) for key, doc in made.items()} == {
        ("Secret", "monitoring", "restic-data"): _values("rest:http://restic:8000/ns-monitoring/pvc-data", "core"),
        ("ReplicationSource", "monitoring", "data"): _source("data"),
        ("Secret", "monitoring", "restic-held"): _values("rest:http://restic:8000/ns-monitoring/pvc-held", "core"),
        ("ReplicationSource", "monitoring", "held"): _source("held", paused=True),
        ("Secret", "app-notes", "restic-db"): _values("rest:http://restic-apps:8000/ns-app-notes/pvc-db", "notes"),
        ("ReplicationSource", "app-notes", "db"): _source("db"),
        ("Secret", "app-notes", "restic-held"): _values("rest:http://restic-apps:8000/ns-app-notes/pvc-held", "notes"),
        ("ReplicationSource", "app-notes", "held"): _source("held", paused=True),
    }


def test_rebuild_hold_pauses_every_source(tmp_path: Path) -> None:
    """Rule: while kube-system/backup-rebuild-hold exists, every generated source is paused, core's and an app's."""
    hold = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "backup-rebuild-hold", "namespace": "kube-system"}}
    made = _generated(tmp_path, [_claim("data", "monitoring"), _claim("db", "app-notes")], _cluster(hold))
    assert {key: doc["spec"]["paused"] for key, doc in made.items() if key[0] == "ReplicationSource"} == {
        ("ReplicationSource", "monitoring", "data"): True, ("ReplicationSource", "app-notes", "db"): True}
