"""Env schema validation for envs/<env>/ (CONTRACT: services/hermes/core/schema/CONTRACT.md)."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any

import yaml

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
_TIER_VALUES = frozenset({"frontier", "mid", "cheap"})
_CODE_DELIVERY_ALLOWED = ("github",)
_BUILD_AGENT_ALLOWED = ("cursor",)

_CLUSTER_VARS_REQUIRED = frozenset(
    {
        "dns_suffix",
        "time_zone",
        "code_delivery",
        "build_agent",
        "backup_path",
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
        "k3s_version",
        "pod_cidr",
        "service_cidr",
    }
)

_HERMES_TOP_LEVEL = frozenset(
    {"bots", "cron", "bot_tier_map", "tier_model_map", "lane_model_map"}
)

_APPS_TOP_LEVEL = frozenset({"app_hosting_enabled", "apps"})


class ValidationError(Exception):
    """One or more env schema violations."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


def validate_env_tree(repo_root: Path | str) -> None:
    """Validate every env under envs/. Raises ValidationError on failure."""
    root = Path(repo_root)
    envs_dir = root / "envs"
    if not envs_dir.is_dir():
        raise ValidationError([f"missing envs directory: {envs_dir}"])

    errors: list[str] = []
    env_dirs = sorted(p for p in envs_dir.iterdir() if p.is_dir())
    if not env_dirs:
        errors.append(f"no env directories under {envs_dir}")
        raise ValidationError(errors)

    for env_dir in env_dirs:
        errors.extend(_validate_env(env_dir))

    if errors:
        raise ValidationError(errors)


def _validate_env(env_dir: Path) -> list[str]:
    errors: list[str] = []
    env_name = env_dir.name
    values = env_dir / "values"
    inventory = env_dir / "inventory" / "hosts.yml"

    cluster_vars_path = values / "cluster-vars.yaml"
    hermes_path = values / "hermes.yaml"
    apps_path = values / "apps.yaml"

    for path in (cluster_vars_path, hermes_path, apps_path, inventory):
        if not path.is_file():
            errors.append(f"{env_name}: missing required file {path.relative_to(env_dir.parent.parent)}")

    if errors:
        return errors

    cluster_vars = _load_yaml(cluster_vars_path, errors)
    hermes = _load_yaml(hermes_path, errors)
    apps = _load_yaml(apps_path, errors)
    hosts = _load_yaml(inventory, errors)
    if errors:
        return errors

    assert cluster_vars is not None and hermes is not None and apps is not None and hosts is not None

    rel_cluster = cluster_vars_path
    rel_hermes = hermes_path
    rel_apps = apps_path
    rel_hosts = inventory

    errors.extend(_validate_cluster_vars(cluster_vars, rel_cluster))
    errors.extend(_validate_hermes(hermes, rel_hermes))
    errors.extend(_validate_apps(apps, rel_apps))
    errors.extend(_validate_hosts(hosts, rel_hosts, env_name))

    return errors


def _load_yaml(path: Path, errors: list[str]) -> dict[str, Any] | list[Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        errors.append(f"{path}: invalid YAML: {exc}")
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path}: expected a YAML mapping at the top level")
        return None
    return data


def _validate_cluster_vars(data: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    for key in data:
        if not _SNAKE_CASE.match(key):
            errors.append(f"{path}: key must be lowercase snake_case: {key}")

    missing = sorted(_CLUSTER_VARS_REQUIRED - set(data))
    for key in missing:
        errors.append(f"{path}: missing required key: {key}")

    if "ssh_user" in data and not isinstance(data["ssh_user"], str):
        errors.append(f"{path}: ssh_user must be a string")
    elif "ssh_user" in data and not data["ssh_user"]:
        errors.append(f"{path}: ssh_user must not be empty")

    errors.extend(_validate_enum(path, "code_delivery", data.get("code_delivery"), _CODE_DELIVERY_ALLOWED))
    errors.extend(_validate_enum(path, "build_agent", data.get("build_agent"), _BUILD_AGENT_ALLOWED))

    for key in (
        "dns_suffix",
        "time_zone",
        "backup_path",
        "k3s_version",
        "pod_cidr",
        "service_cidr",
    ):
        val = data.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"{path}: {key} must be a string")
        elif isinstance(val, str) and not val.strip():
            errors.append(f"{path}: {key} must not be empty")

    for key in (
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
    ):
        val = data.get(key)
        if val is not None and not isinstance(val, int):
            errors.append(f"{path}: {key} must be an integer")

    return errors


def _validate_enum(path: Path, key: str, value: Any, allowed: tuple[str, ...]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{path}: {key} must be a string"]
    if value not in allowed:
        allowed_s = ", ".join(allowed)
        return [f"{path}: {key}: invalid value '{value}' (allowed: {allowed_s})"]
    return []


def _validate_hermes(data: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    for key in data:
        if not _SNAKE_CASE.match(key):
            errors.append(f"{path}: key must be lowercase snake_case: {key}")
        elif key not in _HERMES_TOP_LEVEL:
            errors.append(f"{path}: unknown key '{key}' (allowed: {', '.join(sorted(_HERMES_TOP_LEVEL))})")

    bots = data.get("bots", [])
    if not isinstance(bots, list):
        errors.append(f"{path}: bots must be a list")

    cron = data.get("cron", {})
    if not isinstance(cron, dict):
        errors.append(f"{path}: cron must be a mapping")

    tier_map = data.get("bot_tier_map", {})
    if not isinstance(tier_map, dict):
        errors.append(f"{path}: bot_tier_map must be a mapping")
    else:
        for bot, tier in tier_map.items():
            if not isinstance(tier, str) or tier not in _TIER_VALUES:
                allowed = ", ".join(sorted(_TIER_VALUES))
                errors.append(
                    f"{path}: bot_tier_map[{bot!r}]: invalid tier '{tier}' (allowed: {allowed})"
                )

    for key in ("tier_model_map", "lane_model_map"):
        val = data.get(key, {})
        if val is not None and not isinstance(val, dict):
            errors.append(f"{path}: {key} must be a mapping")

    return errors


def _validate_apps(data: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    for key in data:
        if not _SNAKE_CASE.match(key):
            errors.append(f"{path}: key must be lowercase snake_case: {key}")
        elif key not in _APPS_TOP_LEVEL:
            errors.append(f"{path}: unknown key '{key}' (allowed: {', '.join(sorted(_APPS_TOP_LEVEL))})")

    enabled = data.get("app_hosting_enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append(f"{path}: app_hosting_enabled must be a boolean")

    apps = data.get("apps", [])
    if not isinstance(apps, list):
        errors.append(f"{path}: apps must be a list")

    return errors


def _validate_hosts(data: dict[str, Any], path: Path, env_name: str) -> list[str]:
    errors: list[str] = []
    all_group = data.get("all")
    if not isinstance(all_group, dict):
        errors.append(f"{path}: expected top-level 'all' mapping")
        return errors

    hosts = all_group.get("hosts")
    if not isinstance(hosts, dict):
        errors.append(f"{path}: expected all.hosts mapping")
        return errors

    if not hosts:
        errors.append(f"{env_name}: inventory must list at least one host")

    for hostname, host_vars in hosts.items():
        if _looks_like_ip(hostname):
            errors.append(f"{path}: host key must be a hostname, not an IP: {hostname}")
        if host_vars is None:
            continue
        if not isinstance(host_vars, dict):
            errors.append(f"{path}: host {hostname}: vars must be a mapping if present")
            continue
        if "ansible_host" in host_vars:
            ah = host_vars["ansible_host"]
            if isinstance(ah, str) and _looks_like_ip(ah):
                errors.append(
                    f"{path}: host {hostname}: ansible_host must not be a plain IP (use group_vars SOPS)"
                )
        for key, val in host_vars.items():
            if isinstance(val, str) and _looks_like_ip(val) and key in ("ansible_host", "ip", "ansible_ssh_host"):
                errors.append(f"{path}: host {hostname}: {key} must not be a plain IP in inventory")

    return errors


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
