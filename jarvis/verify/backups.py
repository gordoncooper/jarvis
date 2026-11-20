"""Backup standing checks. Freshness is the extension point for later checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jarvis.env.runtime import json_object, not_found, reason
from jarvis.schema.env import offsite_copy_listed
from jarvis.verify.common import (
    CheckError,
    CheckWarning,
    Context,
    Kubectl,
    items,
    no_success_yet,
    pending_first_run,
    recent,
    scheduled_instants,
)
from jarvis.verify.registry import Check, register

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


def backed_up(pvcs: list[dict]) -> list[dict]:
    """The claims backups cover: bound, not annotated backup: none, and not a VolSync mover's own."""
    return [
        pvc for pvc in pvcs
        if (pvc.get("spec") or {}).get("volumeName")
        and ((pvc.get("metadata") or {}).get("annotations") or {}).get("backup") != "none"
        and ((pvc.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/created-by") != "volsync"
    ]


def check_backups(kubectl: Kubectl, *, zone_name: str, now: datetime) -> None:
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
    # First runs WARN once every FAIL below has had its say: a hold or a stale copy still fails.
    pending: list[str] = []
    for pvc in backed_up(pvcs):
        meta = pvc.get("metadata") or {}
        namespace, name = meta.get("namespace"), meta.get("name")
        if not namespace or not name:
            raise CheckError("PVC has no namespace or name")
        source = indexed.get((namespace, name))
        # A hosted app's volume without a source is the apps component's to report.
        if source is None and namespace.startswith("app-"):
            continue
        if source is None:
            raise CheckError(f"{namespace}/{name}: no ReplicationSource")
        status = source.get("status") or {}
        schedule = (((source.get("spec") or {}).get("trigger") or {}).get("schedule"))
        if not isinstance(schedule, str):
            raise CheckError(f"{namespace}/{name}: no completed scheduled sync")
        if not isinstance(status.get("lastSyncTime"), str):
            # A source made inside its first interval, as on a restored volume, has not synced yet.
            created = (source.get("metadata") or {}).get("creationTimestamp")
            if isinstance(created, str) and pending_first_run(created, schedule, zone_name, now):
                pending.append(f"{namespace}/{name} pending first scheduled sync")
                continue
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
        try:
            no_success_yet(cronjob, schedule, cron_zone, now, "etcd snapshot copy")
        except CheckWarning as exc:
            pending.append(str(exc))
    elif not recent(last, schedule, cron_zone, now):
        raise CheckError("etcd snapshot copy is stale")

    hold = kubectl(
        ["get", "configmap", HOLD_NAME, "-n", HOLD_NAMESPACE, "-o", "json"]
    )
    if hold.returncode == 0:
        raise CheckError("backup rebuild hold is present")
    if not not_found(hold):
        raise CheckError(f"read backup rebuild hold: {reason(hold)}")
    if pending:
        raise CheckWarning("; ".join(pending))


def _cronjob(kubectl: Kubectl, name: str = "etcd-snapshots") -> dict:
    return json_object(
        kubectl(["get", "cronjob", name, "-n", "platform-backups", "-o", "json"]),
        f"read {name} CronJob",
    )


def _last_success_fresh(kubectl: Kubectl, name: str, what: str, now: datetime) -> None:
    """CronJob name's last success is at most one interval plus CHECK_GRACE old."""
    cronjob = _cronjob(kubectl, name)
    spec, status = cronjob.get("spec") or {}, cronjob.get("status") or {}
    schedule, zone = spec.get("schedule"), spec.get("timeZone")
    last = status.get("lastSuccessfulTime")
    if not isinstance(schedule, str) or not isinstance(zone, str):
        raise CheckError(f"{what} has no schedule or time zone")
    if not isinstance(last, str):
        no_success_yet(cronjob, schedule, zone, now, what, CHECK_GRACE)
    try:
        completed = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckError(f"invalid {what} timestamp") from exc
    if completed.tzinfo is None:
        raise CheckError(f"{what} timestamp has no timezone")
    previous = scheduled_instants(schedule, zone, now, 2)
    if now.astimezone(UTC) - completed > previous[0] - previous[1] + CHECK_GRACE:
        raise CheckError(f"{what} is stale; read the {name} Job logs")


def check_repository_integrity(kubectl: Kubectl, *, now: datetime) -> None:
    _last_success_fresh(kubectl, "restic-check", "repository check", now)


def check_offsite_copy(kubectl: Kubectl, *, now: datetime) -> None:
    """Only for an env whose platform-configs list includes the off-host copy."""
    _last_success_fresh(kubectl, "restic-offsite", "off-host copy", now)


@register("backups")
def backups_checks(ctx: Context) -> list[Check]:
    env_dir = ctx.repo_root / "envs" / ctx.env
    checks: list[Check] = [
        (20, "backups-restic present", lambda: check_secret(ctx.kubectl)),
        (
            60,
            "backup freshness",
            lambda: check_backups(ctx.kubectl, zone_name=str(ctx.values["time_zone"]), now=ctx.now),
        ),
        (65, "repository integrity check", lambda: check_repository_integrity(ctx.kubectl, now=ctx.now)),
    ]
    if offsite_copy_listed(env_dir):
        checks.append((66, "off-host copy", lambda: check_offsite_copy(ctx.kubectl, now=ctx.now)))
    return checks
