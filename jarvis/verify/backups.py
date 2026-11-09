"""Backup standing checks. Freshness is the extension point for later checks."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta

from jarvis.apps import values as _apps
from jarvis.backups import selection as _backup_selection
from jarvis.backups.app_credentials import APPS_SERVER, rest_username
from jarvis.env.runtime import json_object, not_found, reason
from jarvis.schema.env import app_registrations, apps_server_listed, backup_required_registrations, offsite_copy_listed
from jarvis.verify.common import (
    CheckError,
    Context,
    Kubectl,
    items,
    no_success_yet,
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


def check_app_isolation(kubectl: Kubectl, *, apps_server: bool) -> None:
    """The static half of "no app credential opens a core repository": core's rest-server has
    no app user, and the apps server, where listed, confines each user to its own folder.
    Only the user names of core's htpasswd are kept; nothing from it is printed."""
    proc = kubectl(
        ["get", "secret", "backups-restic", "-n", "platform-backups", "-o", "go-template={{index .data \"htpasswd\"}}"]
    )
    if proc.returncode:
        raise CheckError(f"read backups-restic: {reason(proc)}")
    try:
        text = base64.b64decode(proc.stdout.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise CheckError("backups-restic htpasswd is not readable") from exc
    users = [line.split(":", 1)[0] for line in text.splitlines() if line.strip()]
    if not users:
        raise CheckError("backups-restic htpasswd has no user")
    if any(user.startswith(rest_username("app-")) for user in users):
        raise CheckError("core's rest-server has an app user; app credentials belong to the apps server")
    if not apps_server:
        return
    deployment = json_object(
        kubectl(["get", "deployment", APPS_SERVER, "-n", "platform-backups", "-o", "json"]),
        f"read {APPS_SERVER} Deployment",
    )
    pod = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    command = [str(part) for item in pod.get("containers") or [] for part in (item.get("command") or []) + (item.get("args") or [])]
    if not any("--private-repos" in part for part in command):
        raise CheckError(f"{APPS_SERVER} runs without --private-repos")
    if "backups-restic" in _secret_names(pod):
        raise CheckError(f"{APPS_SERVER} mounts core's backups-restic")


def _secret_names(pod: dict) -> set[str]:
    """Every Secret a pod reads: volumes, projected volumes, env and envFrom."""
    names: set[str] = set()
    for volume in pod.get("volumes") or []:
        for source in [volume, *((volume.get("projected") or {}).get("sources") or [])]:
            names.add(str((source.get("secret") or {}).get("secretName") or (source.get("secret") or {}).get("name")))
    for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
        names.update(str(((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")) for item in container.get("env") or [])
        names.update(str((item.get("secretRef") or {}).get("name")) for item in container.get("envFrom") or [])
    return names


def check_backups(
    kubectl: Kubectl,
    *,
    zone_name: str,
    now: datetime,
    registered: frozenset[str] = frozenset(),
    required: frozenset[str] | None = None,
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
        if namespace.startswith("app-") and required is not None and namespace not in required:
            continue
        source = indexed.get((namespace, name))
        if source is None and namespace.startswith("app-") and namespace not in registered:
            raise CheckError(
                f"{namespace}/{name}: not backed up; {namespace} has no registered backup credential"
            )
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
    required: frozenset[str] | None
    checks: list[Check]
    if problems := _backup_selection.selection_problems(ctx.repo_root, env_dir, _apps.APPS_VALUES):
        def fail_selection() -> None:
            raise CheckError("; ".join(problems))

        registered = required = frozenset()
        checks = [(19, "backup selection", fail_selection)]
    else:
        registered = frozenset(app_registrations(env_dir, ctx.repo_root))
        required = backup_required_registrations(env_dir, ctx.repo_root)
        checks = []
    checks.extend(
        [
        (20, "backups-restic present", lambda: check_secret(ctx.kubectl)),
        (
            21,
            "app backup isolation",
            lambda: check_app_isolation(
                ctx.kubectl, apps_server=apps_server_listed(env_dir, ctx.repo_root)
            ),
        ),
        (
            60,
            "backup freshness",
            lambda: check_backups(
                ctx.kubectl,
                zone_name=str(ctx.values["time_zone"]),
                now=ctx.now,
                registered=registered,
                required=required,
            ),
        ),
        (
            65,
            "repository integrity check",
            lambda: check_repository_integrity(ctx.kubectl, now=ctx.now),
        ),
        ]
    )
    if offsite_copy_listed(ctx.repo_root / "envs" / ctx.env, ctx.repo_root):
        checks.append(
            (66, "off-host copy", lambda: check_offsite_copy(ctx.kubectl, now=ctx.now))
        )
    return checks
