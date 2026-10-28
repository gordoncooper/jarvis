"""Rules from the platform-backups contract for the rest-server."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_FLAGS = ("--append-only", "--no-auth", "--private-repos")


def _docs(path: Path) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def test_rest_server_contract_rules() -> None:
    docs: list[dict] = []
    for path in sorted((ROOT / "platform/backups").rglob("*.yaml")):
        docs.extend(_docs(path))
    assert all(doc.get("kind") != "Ingress" for doc in docs)

    namespace = next(
        doc
        for doc in docs
        if doc.get("kind") == "Namespace" and doc["metadata"]["name"] == "platform-backups"
    )
    annotations = namespace["metadata"].get("annotations") or {}
    assert "volsync.backube/privileged-movers" not in annotations

    deploy = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "restic"
    )
    pod = deploy["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["role.${dns_suffix}/backup"] == "true"
    container = pod["containers"][0]
    assert re.fullmatch(r"restic/rest-server@sha256:[a-f0-9]{64}", container["image"])
    rendered = "\n".join(
        [
            *(container.get("command") or []),
            *(container.get("args") or []),
            *(
                f"{item.get('name')}={item.get('value')}"
                for item in container.get("env") or []
            ),
        ]
    )
    for flag in _FLAGS:
        assert flag not in rendered


def test_volsync_controller_tz_comes_from_time_zone() -> None:
    text = (ROOT / "platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
    assert "name: TZ" in text
    assert "value: ${time_zone}" in text
    assert "mountPath: /usr/share/zoneinfo" in text
    assert "path: /usr/share/zoneinfo" in text


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
