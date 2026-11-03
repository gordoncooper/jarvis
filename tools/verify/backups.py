"""Backup standing checks. Freshness is the extension point for later checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib._env import json_object, not_found, reason

from verify.common import (
    CheckError,
    Context,
    Kubectl,
    items,
    no_success_yet,
    recent,
    scheduled_instants,
)
from verify.registry import register

HOLD_NAMESPACE = "kube-system"
HOLD_NAME = "backup-rebuild-hold"
SECRET_KEYS_TEMPLATE = "{{range $k, $_ := .data}}{{println $k}}{{end}}"
# Covers the check Job's 6h activeDeadlineSeconds (retries included) with room for a late start.
CHECK_GRACE = timedelta(hours=12)


def check_secret(kubectl: Kubectl) -> None:
    proc = kubectl(
        [
            "get",
            "secret",
            "backups-restic",
            "-n",
            "platform-backups",
            "-o",
            f"go-template={SECRET_KEYS_TEMPLATE}",
        ]
    )
    if proc.returncode:
        raise CheckError(f"read backups-restic: {reason(proc)}")
    keys = [line for line in proc.stdout.splitlines() if line]
    if not keys:
        raise CheckError("backups-restic has no data keys")


def check_backups(
    kubectl: Kubectl, *, zone_name: str, now: datetime
) -> None:
    pvcs = items(kubectl(["get", "pvc", "-A", "-o", "json"]), "list PVCs")
    sources = items(
        kubectl(["get", "replicationsource", "-A", "-o", "json"]),
        "list ReplicationSources",
    )
    indexed = {
        (
            (item.get("metadata") or {}).get("namespace"),
            (item.get("metadata") or {}).get("name"),
        ): item
        for item in sources
    }
    for pvc in pvcs:
        meta = pvc.get("metadata") or {}
        spec = pvc.get("spec") or {}
        if not spec.get("volumeName"):
            continue
        if (meta.get("annotations") or {}).get("backup") == "none":
            continue
        if (meta.get("labels") or {}).get("app.kubernetes.io/created-by") == "volsync":
            continue
        namespace, name = meta.get("namespace"), meta.get("name")
        if not namespace or not name:
            raise CheckError("PVC has no namespace or name")
        source = indexed.get((namespace, name))
        if source is None:
            raise CheckError(f"{namespace}/{name}: no ReplicationSource")
        status = source.get("status") or {}
        schedule = (((source.get("spec") or {}).get("trigger") or {}).get("schedule"))
        if not isinstance(schedule, str) or not isinstance(status.get("lastSyncTime"), str):
            raise CheckError(f"{namespace}/{name}: no completed scheduled sync")
        if not recent(status["lastSyncTime"], schedule, zone_name, now):
            raise CheckError(f"{namespace}/{name}: last successful sync is stale")
    cronjob = _cronjob(kubectl)
    spec, status = cronjob.get("spec") or {}, cronjob.get("status") or {}
    cron_zone = spec.get("timeZone")
    schedule = spec.get("schedule")
    last = status.get("lastSuccessfulTime")
    if not isinstance(schedule, str) or not isinstance(cron_zone, str):
        raise CheckError("etcd snapshot copy has no successful scheduled run")
    if not isinstance(last, str):
        no_success_yet(cronjob, schedule, cron_zone, now, "etcd snapshot copy")
    if not recent(last, schedule, cron_zone, now):
        raise CheckError("etcd snapshot copy is stale")

    hold = kubectl(
        ["get", "configmap", HOLD_NAME, "-n", HOLD_NAMESPACE, "-o", "json"]
    )
    if hold.returncode == 0:
        raise CheckError("backup rebuild hold is present")
    if not not_found(hold):
        raise CheckError(f"read backup rebuild hold: {reason(hold)}")


def _cronjob(kubectl: Kubectl, name: str = "etcd-snapshots") -> dict:
    return json_object(
        kubectl(["get", "cronjob", name, "-n", "platform-backups", "-o", "json"]),
        f"read {name} CronJob",
    )


def check_repository_integrity(kubectl: Kubectl, *, now: datetime) -> None:
    """The last restic-check success is at most one interval plus CHECK_GRACE old."""
    cronjob = _cronjob(kubectl, "restic-check")
    spec, status = cronjob.get("spec") or {}, cronjob.get("status") or {}
    schedule, zone = spec.get("schedule"), spec.get("timeZone")
    last = status.get("lastSuccessfulTime")
    if not isinstance(schedule, str) or not isinstance(zone, str):
        raise CheckError("repository check has no schedule or time zone")
    if not isinstance(last, str):
        no_success_yet(cronjob, schedule, zone, now, "repository check", CHECK_GRACE)
    try:
        completed = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckError("invalid repository check timestamp") from exc
    if completed.tzinfo is None:
        raise CheckError("repository check timestamp has no timezone")
    previous = scheduled_instants(schedule, zone, now, 2)
    if now.astimezone(timezone.utc) - completed > previous[0] - previous[1] + CHECK_GRACE:
        raise CheckError("repository check is stale; read the restic-check Job logs")


@register("backups")
def backups_checks(ctx: Context) -> list[tuple[int, str, object]]:
    return [
        (20, "backups-restic present", lambda: check_secret(ctx.kubectl)),
        (
            60,
            "backup freshness",
            lambda: check_backups(
                ctx.kubectl,
                zone_name=str(ctx.values["time_zone"]),
                now=ctx.now,
            ),
        ),
        (
            65,
            "repository integrity check",
            lambda: check_repository_integrity(ctx.kubectl, now=ctx.now),
        ),
    ]
