"""Shared types and helpers for standing-state checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lib._env import json_object

Kubectl = Callable[..., CompletedProcess[str]]
Fetch = Callable[[str, Path], tuple[int, str]]
Issuer = Callable[[str, Path], tuple[int, str]]


class CheckError(Exception):
    pass


class CheckWarning(Exception):
    pass


@dataclass(frozen=True)
class Context:
    repo_root: Path
    env: str
    kubectl: Kubectl
    values: dict
    now: datetime
    roles: dict[str, set[str]]


def items(proc: CompletedProcess[str], what: str) -> list[dict]:
    body = json_object(proc, what)
    listed = body.get("items")
    if not isinstance(listed, list) or any(not isinstance(item, dict) for item in listed):
        raise CheckError(f"{what}: invalid items")
    return listed


def _number(token: str, names: dict[str, int] | None) -> int:
    folded = token.lower()
    if names and folded in names:
        return names[folded]
    return int(token)


def _cron_field(
    text: str, low: int, high: int, names: dict[str, int] | None = None
) -> tuple[set[int], bool]:
    values: set[int] = set()
    wildcard = text.startswith("*")
    try:
        for part in text.split(","):
            base, slash, step_text = part.partition("/")
            step = int(step_text) if slash else 1
            if step < 1:
                raise ValueError
            if base == "*":
                start, stop = low, high
            elif "-" in base:
                first, last = base.split("-", 1)
                start, stop = _number(first, names), _number(last, names)
            else:
                start = _number(base, names)
                stop = high if slash else start
            if start < low or stop > high or start > stop:
                raise ValueError
            values.update(range(start, stop + 1, step))
    except (ValueError, TypeError) as exc:
        raise CheckError(f"unsupported cron field {text!r}") from exc
    return values, wildcard


def _cron(schedule: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    parts = schedule.split()
    if len(parts) != 5:
        raise CheckError(f"invalid five-field schedule {schedule!r}")
    months = dict(zip("jan feb mar apr may jun jul aug sep oct nov dec".split(), range(1, 13)))
    days = dict(zip("sun mon tue wed thu fri sat".split(), range(7)))
    minute, _ = _cron_field(parts[0], 0, 59)
    hour, _ = _cron_field(parts[1], 0, 23)
    dom, dom_any = _cron_field(parts[2], 1, 31)
    month, _ = _cron_field(parts[3], 1, 12, months)
    dow, dow_any = _cron_field(parts[4], 0, 7, days)
    if 7 in dow:
        dow.add(0)
        dow.discard(7)
    return minute, hour, dom, month, dow, dom_any, dow_any


def scheduled_instants(
    schedule: str, zone_name: str, now: datetime, count: int
) -> list[datetime]:
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise CheckError("invalid backup timestamp or time zone") from exc
    minute, hour, dom, month, dow, dom_any, dow_any = _cron(schedule)
    cursor = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    previous: list[datetime] = []
    for _ in range(2 * 366 * 24 * 60):
        local = cursor.astimezone(zone)
        dom_match = local.day in dom
        dow_match = ((local.weekday() + 1) % 7) in dow
        day_match = (
            dom_match and dow_match
            if dom_any or dow_any
            else dom_match or dow_match
        )
        if (
            local.minute in minute
            and local.hour in hour
            and local.month in month
            and day_match
        ):
            previous.append(cursor)
            if len(previous) == count:
                return previous
        cursor = cursor.fromtimestamp(cursor.timestamp() - 60, timezone.utc)
    raise CheckError(f"schedule {schedule!r} has fewer than {count} runs in two years")


def recent(stamp: str, schedule: str, zone_name: str, now: datetime) -> bool:
    try:
        completed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckError("invalid backup timestamp or time zone") from exc
    if completed.tzinfo is None:
        raise CheckError("backup timestamp has no timezone")
    previous = scheduled_instants(schedule, zone_name, now, 2)
    return completed.astimezone(timezone.utc) >= previous[1]


def pending_first_run(
    created_stamp: str,
    schedule: str,
    zone_name: str,
    now: datetime,
    grace: timedelta = timedelta(0),
) -> bool:
    try:
        created = datetime.fromisoformat(created_stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckError("invalid backup timestamp or time zone") from exc
    if created.tzinfo is None:
        raise CheckError("backup timestamp has no timezone")
    previous = scheduled_instants(schedule, zone_name, now, 2)
    interval = previous[0] - previous[1]
    age = now.astimezone(timezone.utc) - created.astimezone(timezone.utc)
    return age < interval + grace


def no_success_yet(
    cronjob: dict,
    schedule: str,
    zone_name: str,
    now: datetime,
    what: str,
    grace: timedelta = timedelta(0),
) -> None:
    """A CronJob with no successful run: WARN inside its first interval plus grace while its
    first run is not scheduled yet or still running; FAIL once a run finished without success,
    or after the first interval plus grace."""
    status = cronjob.get("status") or {}
    created = (cronjob.get("metadata") or {}).get("creationTimestamp")
    if isinstance(created, str) and pending_first_run(
        created, schedule, zone_name, now, grace
    ):
        if status.get("active"):
            raise CheckWarning(f"{what} first run in progress")
        if not isinstance(status.get("lastScheduleTime"), str):
            raise CheckWarning(f"{what} pending first run")
    raise CheckError(f"{what} has no successful scheduled run")
