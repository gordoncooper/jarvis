"""Rules from the platform-backups contract for the rest-server and etcd copy."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from support import REPO_ROOT as ROOT
from support import ExampleCopy, _kustomize_build, real_and_copy_envs

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
    assert re.fullmatch(
        r"restic/rest-server:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", container["image"]
    )
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
    resources = _docs(ROOT / "platform/backups/controller/helmrelease.yaml")[0]["spec"]["values"][
        "resources"
    ]
    assert resources["limits"]["cpu"] is None
    assert resources["limits"]["memory"]
    assert resources["requests"]["memory"]


def test_renovate_tracks_the_rest_server_digest() -> None:
    image_line = next(
        line.strip()
        for line in (ROOT / "platform/backups/rest-server.yaml").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("image:")
    )
    assert re.fullmatch(
        r"image: restic/rest-server:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", image_line
    )
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
        "restic/rest-server" in pattern
        and "currentDigest" in pattern
        and "currentValue" in pattern
        for pattern in manager["matchStrings"]
    )


def _cron_fields(expr: str) -> list[str]:
    fields = expr.split()
    assert len(fields) == 5, expr
    return fields


def test_etcd_snapshot_cronjob_copies_after_k3s() -> None:
    """The copy follows the k3s cron, reads that directory, and keeps one restic group."""
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/k3s/defaults/main.yml").read_text(encoding="utf-8")
    )
    k3s_cron = _cron_fields(defaults["k3s_etcd_snapshot_schedule_cron"])
    k3s_config = (ROOT / "infra/ansible/roles/k3s/templates/config.yaml.j2").read_text(
        encoding="utf-8"
    )

    docs: list[dict] = []
    for path in sorted((ROOT / "platform/backups").rglob("*.yaml")):
        docs.extend(_docs(path))
    snapshot_path = "${backup_path}/etcd-snapshots"
    job = next(
        doc
        for doc in docs
        if doc.get("kind") == "CronJob"
        and any(
            vol.get("hostPath", {}).get("path") == snapshot_path
            for vol in doc["spec"]["jobTemplate"]["spec"]["template"]["spec"].get("volumes")
            or []
        )
    )
    assert f"etcd-snapshot-dir: {snapshot_path.replace('${backup_path}', '{{ backup_path }}')}" in k3s_config

    spec = job["spec"]
    assert spec["timeZone"] == "${time_zone}"
    schedule = _cron_fields(spec["schedule"])
    assert schedule[1:] == k3s_cron[1:]
    assert schedule[0].isdigit() and k3s_cron[0].isdigit()
    assert 1 <= int(schedule[0]) - int(k3s_cron[0]) <= 15

    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["role.${dns_suffix}/control_plane"] == "true"
    container = pod["containers"][0]
    assert re.fullmatch(r"restic/restic:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", container["image"])
    volume = next(item for item in pod["volumes"] if item.get("hostPath", {}).get("path") == snapshot_path)
    mount = next(item for item in container["volumeMounts"] if item["name"] == volume["name"])
    assert mount["readOnly"] is True

    forget = "\n".join(str(part) for part in container["command"]).split("restic forget", 1)[1]
    for key in (
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
    ):
        assert f"${{{key}}}" in forget

    env = {item["name"]: item for item in container["env"]}
    prefix = env["RESTIC_REPOSITORY_PREFIX"]["valueFrom"]["secretKeyRef"]
    assert (prefix["name"], prefix["key"]) == ("backups-restic", "RESTIC_REPOSITORY")
    assert env["RESTIC_REPOSITORY"]["value"] == "$(RESTIC_REPOSITORY_PREFIX)host-etcd-snapshots"
    host = env["RESTIC_HOST"]["value"]
    assert isinstance(host, str) and host and "$" not in host

    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any(
            "etcd-snapshots" in pattern and "config" not in pattern
            for pattern in manager.get("managerFilePatterns", [])
        )
    ]
    assert len(managers) == 1
    assert managers[0]["datasourceTemplate"] == "docker"
    assert any(
        "restic/restic" in pattern and "currentDigest" in pattern and "currentValue" in pattern
        for pattern in managers[0]["matchStrings"]
    )


def _etcd_kubectl(image: str):
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from lib._etcd_image import etcd_snapshot_image

    body = {
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {"spec": {"containers": [{"image": image}]}},
                }
            }
        }
    }

    def kubectl(args: list[str]) -> CompletedProcess[str]:
        return CompletedProcess(args, 0, json.dumps(body), "")

    return etcd_snapshot_image, kubectl


def test_versioned_etcd_image_is_accepted() -> None:
    """restore and drill accept a release tag or a digest-only restic image."""
    digest = "sha256:" + ("ab" * 32)
    for image in (
        f"restic/restic:0.19.1@{digest}",
        f"restic/restic:v0.19.1@{digest}",
        f"restic/restic@{digest}",
    ):
        read, kubectl = _etcd_kubectl(image)
        assert read(kubectl) == image
    committed = next(
        line.strip().removeprefix("image:").strip()
        for line in (ROOT / "platform/backups/etcd-snapshots.yaml")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip().startswith("image:") and "restic/restic" in line
    )
    read, kubectl = _etcd_kubectl(committed)
    assert read(kubectl) == committed


@pytest.mark.parametrize("tag", ["latest", "edge", "Latest", "0.18"])
def test_nonrelease_etcd_tag_is_rejected(tag: str) -> None:
    """latest, edge, Latest, and a partial tag are not a restic release."""
    image = f"restic/restic:{tag}@sha256:" + ("ab" * 32)
    read, kubectl = _etcd_kubectl(image)
    from lib._etcd_image import ImageError

    with pytest.raises(ImageError, match="must be restic/restic@sha256:"):
        read(kubectl)


def _restic_check(root: Path, env: str) -> dict:
    build = _kustomize_build(f"envs/{env}/flux/platform-configs", root=root)
    return next(
        doc
        for doc in yaml.safe_load_all(build)
        if isinstance(doc, dict)
        and doc.get("kind") == "CronJob"
        and doc["metadata"]["name"] == "restic-check"
    )


def test_restic_check_reads_repositories_without_a_lock(example_copy: ExampleCopy) -> None:
    """The check takes no lock and mounts the repositories read-only, so it never blocks,
    deletes, prunes or unlocks anything; it has no API token or capabilities, and runs the
    etcd copy's pinned image with a memory cap."""
    etcd = _docs(ROOT / "platform/backups/etcd-snapshots.yaml")[0]
    etcd_image = etcd["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
    for root, env in real_and_copy_envs(example_copy):
        pod = _restic_check(root, env)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert container["image"] == etcd_image
        assert re.fullmatch(r"restic/restic:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", container["image"])
        assert container["resources"]["requests"]["memory"]
        assert container["resources"]["limits"]["memory"]
        assert pod["nodeSelector"] == {"role.${dns_suffix}/backup": "true"}
        # No API token and no capabilities: root may only read files it owns.
        assert pod["automountServiceAccountToken"] is False
        security = container["securityContext"]
        assert security["capabilities"]["drop"] == ["ALL"]
        assert "add" not in security["capabilities"]
        assert security["allowPrivilegeEscalation"] is False
        assert re.search(r"\brestic\b[^\n]*--no-lock\b[^\n]*\bcheck\b", container["command"][-1])
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        for volume in pod["volumes"]:
            if "hostPath" in volume:
                assert volume["hostPath"]["path"] == "${backup_path}/restic"
                assert mounts[volume["name"]]["readOnly"] is True


# Exits 10 for a missing repository and 1 for one marked broken; prints a file name it must not leak.
_FAKE_CHECK = """#!/bin/sh
echo "$2 $5" >> "$LOG"
echo "tree 1: file private-file-name"
[ -d "$2" ] || exit 10
[ ! -e "$2/broken" ] || exit 1
"""


@pytest.mark.parametrize(
    ("repos", "code", "lines"),
    [
        (
            ["host-etcd-snapshots", "ns-a/pvc-ok", "ns-b/pvc-bad"],
            1,
            [
                "PASS host-etcd-snapshots",
                "PASS ns-a/pvc-ok",
                "FAIL ns-b/pvc-bad: restic check exited 1",
            ],
        ),
        (["host-etcd-snapshots"], 0, ["PASS host-etcd-snapshots"]),
        ([], 1, ["FAIL host-etcd-snapshots: restic check exited 10"]),
    ],
)
def test_restic_check_script_checks_every_repository_and_prints_only_results(
    tmp_path: Path, repos: list[str], code: int, lines: list[str]
) -> None:
    """Every PVC repository and the etcd repository is checked on a rotating slice; a missing
    etcd repository fails, an empty PVC glob does not, and restic's own output never reaches the log."""
    data, bindir = tmp_path / "data", tmp_path / "bin"
    bindir.mkdir()
    data.mkdir()
    for repo in repos:
        (data / repo).mkdir(parents=True)
    if "ns-b/pvc-bad" in repos:
        (data / "ns-b/pvc-bad/broken").touch()
    (bindir / "restic").write_text(_FAKE_CHECK, encoding="utf-8")
    (bindir / "restic").chmod(0o755)
    job = _docs(ROOT / "platform/backups/restic-check.yaml")[0]["spec"]["jobTemplate"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    env ={item["name"]: item["value"] for item in container["env"] if "value" in item}
    env.update(PATH=f"{bindir}:{os.environ['PATH']}", LOG=str(tmp_path / "log"))
    script = container["command"][-1].replace("/repositories", str(data))
    proc = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == code
    assert proc.stdout.splitlines() == lines
    assert "private-file-name" not in proc.stdout + proc.stderr
    subsets = {line.split()[1] for line in (tmp_path / "log").read_text().splitlines()}
    assert len(subsets) == 1
    slice_, total = subsets.pop().removeprefix("--read-data-subset=").split("/")
    assert 1 <= int(slice_) <= int(total) == int(env["READ_DATA_SLICES"])


def test_restic_check_starts_clear_of_backups_and_the_etcd_copy(
    example_copy: ExampleCopy,
) -> None:
    """In every env, its backup_check_schedule starts at least 3 hours from every nightly
    backup and etcd copy start."""
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from schema.env import resolve_cluster_vars
    from verify.common import scheduled_instants

    etcd_schedule = _docs(ROOT / "platform/backups/etcd-snapshots.yaml")[0]["spec"]["schedule"]
    for root, env in real_and_copy_envs(example_copy):
        assert _restic_check(root, env)["spec"]["schedule"] == "${backup_check_schedule}"
        values = resolve_cluster_vars(
            yaml.safe_load(
                (root / "envs" / env / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")
            )
        )
        check = values["backup_check_schedule"]
        run = scheduled_instants(check, "Etc/UTC", datetime(2026, 11, 2, tzinfo=timezone.utc), 1)[0]
        for other in (values["backup_schedule"], etcd_schedule):
            for instant in scheduled_instants(other, "Etc/UTC", run + timedelta(days=2), 8):
                assert abs(instant - run) >= timedelta(hours=3), (env, other)
