"""Digest check for the live etcd-snapshots CronJob image.

restore and drill both run a pod from that image. The check fails closed
before either tool changes the cluster.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from subprocess import CompletedProcess

from . import _env

ETCD_NAMESPACE = "platform-backups"
ETCD_CRONJOB = "etcd-snapshots"
_RESTIC_IMAGE = re.compile(r"restic/restic@sha256:[0-9a-f]{64}")

Kubectl = Callable[..., CompletedProcess[str]]


class ImageError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def etcd_snapshot_image(kubectl: Kubectl) -> str:
    """Image of CronJob platform-backups/etcd-snapshots.

    The image must be restic/restic@sha256: and 64 hex digits. A missing
    CronJob, a missing image, or any other text fails.
    """
    what = f"CronJob {ETCD_NAMESPACE}/{ETCD_CRONJOB}"
    proc = kubectl(
        ["get", "cronjob", ETCD_CRONJOB, "-n", ETCD_NAMESPACE, "-o", "json"]
    )
    if _env.not_found(proc):
        raise ImageError(f"{what} not found")
    try:
        body = _env.json_object(proc, f"read {what}")
    except _env.EnvError as exc:
        raise ImageError(str(exc)) from exc
    try:
        container = body["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        image = container.get("image") if isinstance(container, dict) else None
    except (KeyError, IndexError, TypeError) as exc:
        raise ImageError(f"{what} has no container image") from exc
    if not isinstance(image, str) or image == "":
        raise ImageError(f"{what} has no container image")
    if _RESTIC_IMAGE.fullmatch(image) is None:
        raise ImageError(
            f"{what} image must be restic/restic@sha256: and 64 hex digits"
        )
    return image
