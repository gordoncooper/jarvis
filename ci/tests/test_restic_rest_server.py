"""rest-server Deployment, VolSync TZ, and the backups-restic key set."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REST_SERVER_KEYS = {
    "RESTIC_PASSWORD",
    "RESTIC_REPOSITORY",
    "RESTIC_REST_USERNAME",
    "RESTIC_REST_PASSWORD",
    "htpasswd",
}


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (run make deps)")
    return path


def _kustomize_docs(rel: str) -> list[dict]:
    kustomize = _require_tool("kustomize")
    proc = subprocess.run(
        [kustomize, "build", "--load-restrictor", "LoadRestrictionsNone", str(ROOT / rel)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def _named(docs: list[dict], kind: str, name: str) -> dict:
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_rest_server_is_one_pinned_deployment_and_service() -> None:
    docs = _kustomize_docs("platform/backups")
    assert all(doc.get("kind") != "Ingress" for doc in docs)

    namespace = _named(docs, "Namespace", "platform-backups")
    annotations = namespace["metadata"].get("annotations") or {}
    assert "volsync.backube/privileged-movers" not in annotations

    deploy = _named(docs, "Deployment", "restic")
    assert deploy["metadata"]["namespace"] == "platform-backups"
    spec = deploy["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"
    assert spec["template"]["spec"]["nodeSelector"] == {"role.${dns_suffix}/backup": "true"}
    containers = spec["template"]["spec"]["containers"]
    assert len(containers) == 1
    container = containers[0]
    assert container["name"] == "rest-server"
    assert re.fullmatch(r"restic/rest-server@sha256:[a-f0-9]{64}", container["image"])
    assert "command" not in container
    assert "args" not in container
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env == {"DATA_DIRECTORY": "/data", "PASSWORD_FILE": "/auth/htpasswd"}
    rendered = yaml.safe_dump(container)
    assert "--append-only" not in rendered
    assert "--no-auth" not in rendered
    assert "--private-repos" not in rendered
    assert container["securityContext"]["runAsUser"] == 0
    assert container["ports"] == [{"name": "http", "containerPort": 8000}]

    volumes = {item["name"]: item for item in spec["template"]["spec"]["volumes"]}
    assert volumes["data"]["hostPath"] == {
        "path": "${backup_path}/restic",
        "type": "DirectoryOrCreate",
    }
    auth = volumes["auth"]["secret"]
    assert auth["secretName"] == "backups-restic"
    assert auth["items"] == [{"key": "htpasswd", "path": "htpasswd"}]

    service = _named(docs, "Service", "restic")
    assert service["metadata"]["namespace"] == "platform-backups"
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {"app.kubernetes.io/name": "restic"}
    assert service["spec"]["ports"] == [{"name": "http", "port": 8000, "targetPort": "http"}]


def test_volsync_controller_tz_comes_from_time_zone() -> None:
    path = ROOT / "platform/backups/controller/helmrelease.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    annotations = doc["metadata"].get("annotations") or {}
    assert annotations.get("kustomize.toolkit.fluxcd.io/substitute") != "disabled"
    patch = doc["spec"]["postRenderers"][0]["kustomize"]["patches"][0]
    assert patch["target"]["kind"] == "Deployment"
    assert patch["target"]["name"] == "volsync"
    text = patch["patch"]
    assert "name: TZ" in text
    assert "value: ${time_zone}" in text
    assert "mountPath: /usr/share/zoneinfo" in text
    assert "path: /usr/share/zoneinfo" in text


def test_env_restic_secret_names_the_rest_server_keys() -> None:
    path = ROOT / "envs/env-01/secrets/backups-restic.sops.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = doc["stringData"]
    assert set(data) == REST_SERVER_KEYS
    encrypted = all(isinstance(value, str) and value.startswith("ENC[") for value in data.values())
    assert encrypted


def test_renovate_tracks_the_rest_server_digest() -> None:
    image_line = next(
        line.strip()
        for line in (ROOT / "platform/backups/rest-server.yaml").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("image:")
    )
    assert re.fullmatch(r"image: restic/rest-server@sha256:[a-f0-9]{64}", image_line)
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("rest-server" in pattern for pattern in manager.get("managerFilePatterns", []))
    ]
    assert len(managers) == 1
    manager = managers[0]
    assert manager["datasourceTemplate"] == "docker"
    assert any(
        "restic/rest-server" in pattern and "currentDigest" in pattern
        for pattern in manager["matchStrings"]
    )
