"""The restic image restore's and drill's pods run: the etcd copy's, as core's manifest names it in
the env's pinned core. images-pinned holds that image to a digest at admission."""

from __future__ import annotations

import yaml

from jarvis import paths as _paths


def etcd_snapshot_image() -> str:
    """The image of CronJob platform-backups/etcd-snapshots in core/platform/backups/etcd-snapshots.yaml."""
    path = _paths.core_root() / "core" / "platform" / "backups" / "etcd-snapshots.yaml"
    cronjob = yaml.safe_load(path.read_text(encoding="utf-8"))
    return cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
