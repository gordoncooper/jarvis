"""Rules from the platform-backups contract for the rest-server and etcd copy."""

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


def _cron_fields(expr: str) -> list[str]:
    fields = expr.split()
    assert len(fields) == 5, expr
    return fields


def _secret_ref(entry: dict) -> tuple[str, str]:
    ref = entry["valueFrom"]["secretKeyRef"]
    return ref["name"], ref["key"]


def test_etcd_snapshot_cronjob_copies_after_k3s() -> None:
    """The copy follows the k3s cron, reads that directory, and keeps one restic group."""
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/k3s/defaults/main.yml").read_text(encoding="utf-8")
    )
    k3s_cron = _cron_fields(defaults["k3s_etcd_snapshot_schedule_cron"])
    k3s_config = (ROOT / "infra/ansible/roles/k3s/templates/config.yaml.j2").read_text(
        encoding="utf-8"
    )
    assert "etcd-snapshot-dir: {{ backup_path }}/etcd-snapshots" in k3s_config

    docs: list[dict] = []
    for path in sorted((ROOT / "platform/backups").rglob("*.yaml")):
        docs.extend(_docs(path))
    jobs = [doc for doc in docs if doc.get("kind") == "CronJob"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["metadata"]["name"] == "etcd-snapshots"
    assert job["metadata"]["namespace"] == "platform-backups"
    spec = job["spec"]
    assert spec["timeZone"] == "${time_zone}"
    assert spec["concurrencyPolicy"] == "Forbid"
    schedule = _cron_fields(spec["schedule"])
    assert schedule[1:] == k3s_cron[1:]
    assert schedule[0].isdigit() and k3s_cron[0].isdigit()
    assert 1 <= int(schedule[0]) - int(k3s_cron[0]) <= 15

    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["role.${dns_suffix}/control_plane"] == "true"
    assert pod["restartPolicy"] == "OnFailure"
    container = pod["containers"][0]
    assert re.fullmatch(r"restic/restic@sha256:[a-f0-9]{64}", container["image"])
    assert container["securityContext"]["runAsUser"] == 0

    mount = next(item for item in container["volumeMounts"] if item["name"] == "snapshots")
    assert mount["readOnly"] is True
    volume = next(item for item in pod["volumes"] if item["name"] == mount["name"])
    assert volume["hostPath"]["path"] == "${backup_path}/etcd-snapshots"
    assert volume["hostPath"]["type"] == "Directory"

    script = "\n".join(str(part) for part in container["command"])
    assert script.index("restic cat config") < script.index("restic init")
    assert "10" in script[: script.index("restic init")]
    assert f"restic backup {mount['mountPath']}" in script
    assert script.index("restic forget") < script.index("restic prune")
    for key in (
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
    ):
        assert f"${{{key}}}" in script.split("restic forget", 1)[1].split("restic prune", 1)[0]

    env = {item["name"]: item for item in container["env"]}
    assert "htpasswd" not in env
    for name, key in (
        ("RESTIC_PASSWORD", "RESTIC_PASSWORD"),
        ("RESTIC_REST_USERNAME", "RESTIC_REST_USERNAME"),
        ("RESTIC_REST_PASSWORD", "RESTIC_REST_PASSWORD"),
        ("RESTIC_REPOSITORY_PREFIX", "RESTIC_REPOSITORY"),
    ):
        assert _secret_ref(env[name]) == ("backups-restic", key)
    assert env["RESTIC_REPOSITORY"]["value"] == "$(RESTIC_REPOSITORY_PREFIX)host-etcd-snapshots"
    host = env["RESTIC_HOST"]["value"]
    assert isinstance(host, str) and host and "$" not in host

    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("etcd-snapshots" in pattern for pattern in manager.get("managerFilePatterns", []))
    ]
    assert len(managers) == 1
    assert managers[0]["datasourceTemplate"] == "docker"
    assert any(
        "restic/restic" in pattern and "currentDigest" in pattern
        for pattern in managers[0]["matchStrings"]
    )

    contract = (ROOT / "platform/backups/CONTRACT.md").read_text(encoding="utf-8")
    assert "host-etcd-snapshots" in contract
    assert "--cluster-reset-restore-path" in contract
    assert "not disk-failure protection" in contract
    texts = [(ROOT / "platform/backups/rest-server.yaml").read_text(encoding="utf-8")]
    readmes = sorted((ROOT / "envs").glob("*/secrets/README.md"))
    assert readmes
    texts.extend(path.read_text(encoding="utf-8") for path in readmes)
    for text in texts:
        assert "<namespace>/<pvc>" not in text
        assert "ns-<namespace>/pvc-<pvc>" in text
