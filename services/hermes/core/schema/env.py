"""Env schema validation for envs/<env>/ (CONTRACT: services/hermes/core/schema/CONTRACT.md)."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

DEFAULT_SSH_USER = "ubuntu"

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
_TIER_VALUES = frozenset({"frontier", "mid", "cheap"})
_CODE_DELIVERY_ALLOWED = ("github",)
_BUILD_AGENT_ALLOWED = ("cursor",)

_CLUSTER_VARS_REQUIRED = frozenset(
    {
        "jarvis_env",
        "dns_suffix",
        "time_zone",
        "code_delivery",
        "build_agent",
        "backup_path",
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
        "k3s_version",
        "k3s_sha256",
        "pod_cidr",
        "service_cidr",
        "local_path_data",
        "flux_git_repo_url",
        "flux_git_ref",
    }
)

_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")

_HERMES_TOP_LEVEL = frozenset(
    {"bots", "cron", "bot_tier_map", "tier_model_map", "lane_model_map"}
)

_APPS_TOP_LEVEL = frozenset({"app_hosting_enabled", "apps"})


class ValidationError(Exception):
    """One or more env schema violations."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


def resolve_cluster_vars(data: dict[str, Any]) -> dict[str, Any]:
    """Apply env schema defaults to cluster-vars (authoritative for consumers)."""
    resolved = dict(data)
    if "ssh_user" not in resolved:
        resolved["ssh_user"] = DEFAULT_SSH_USER
    return resolved


def validate_env_tree(repo_root: Path | str) -> dict[str, dict[str, Any]]:
    """Validate every env under envs/. Returns resolved cluster-vars per env name."""
    root = Path(repo_root).resolve()
    envs_dir = root / "envs"
    if not envs_dir.is_dir():
        raise ValidationError([f"missing envs directory: {_display_path(envs_dir, root)}"])

    errors: list[str] = []
    resolved_by_env: dict[str, dict[str, Any]] = {}
    env_dirs = sorted(p for p in envs_dir.iterdir() if p.is_dir())
    if not env_dirs:
        errors.append(f"no env directories under {_display_path(envs_dir, root)}")
        raise ValidationError(errors)

    for env_dir in env_dirs:
        env_errors, env_resolved = _validate_env(env_dir, root)
        errors.extend(env_errors)
        if env_resolved is not None:
            resolved_by_env[env_dir.name] = env_resolved

    if errors:
        raise ValidationError(errors)
    return resolved_by_env


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def _validate_env(env_dir: Path, repo_root: Path) -> tuple[list[str], dict[str, Any] | None]:
    errors: list[str] = []
    env_name = env_dir.name
    values = env_dir / "values"
    inventory = env_dir / "inventory" / "hosts.yml"

    cluster_vars_path = values / "cluster-vars.yaml"
    hermes_path = values / "hermes.yaml"
    apps_path = values / "apps.yaml"

    for path in (cluster_vars_path, hermes_path, apps_path, inventory):
        if not path.is_file():
            errors.append(
                f"{env_name}: missing required file {_display_path(path, repo_root)}"
            )

    if errors:
        return errors, None

    cluster_vars = _load_yaml(cluster_vars_path, errors, repo_root)
    hermes = _load_yaml(hermes_path, errors, repo_root)
    apps = _load_yaml(apps_path, errors, repo_root)
    hosts = _load_yaml(inventory, errors, repo_root)
    if errors or cluster_vars is None or hermes is None or apps is None or hosts is None:
        return errors, None

    errors.extend(
        _validate_cluster_vars(
            cluster_vars,
            _display_path(cluster_vars_path, repo_root),
            env_name,
        )
    )
    errors.extend(_validate_hermes(hermes, _display_path(hermes_path, repo_root)))
    errors.extend(_validate_apps(apps, _display_path(apps_path, repo_root)))
    errors.extend(_validate_hosts(hosts, _display_path(inventory, repo_root), env_name))

    if errors:
        return errors, None
    return [], resolve_cluster_vars(cluster_vars)


def _load_yaml(
    path: Path, errors: list[str], repo_root: Path
) -> dict[str, Any] | None:
    rel = _display_path(path, repo_root)
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        errors.append(f"{rel}: invalid YAML: {exc}")
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        errors.append(f"{rel}: expected a YAML mapping at the top level")
        return None
    return data


def _is_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_cluster_vars(
    data: dict[str, Any], rel_path: str, env_name: str
) -> list[str]:
    errors: list[str] = []
    for key in data:
        if not _SNAKE_CASE.match(key):
            errors.append(f"{rel_path}: key must be lowercase snake_case: {key}")

    missing = sorted(_CLUSTER_VARS_REQUIRED - set(data))
    for key in missing:
        errors.append(f"{rel_path}: missing required key: {key}")

    if "ssh_user" in data and not isinstance(data["ssh_user"], str):
        errors.append(f"{rel_path}: ssh_user must be a string")
    elif "ssh_user" in data and not data["ssh_user"]:
        errors.append(f"{rel_path}: ssh_user must not be empty")

    errors.extend(
        _validate_enum(rel_path, "code_delivery", data.get("code_delivery"), _CODE_DELIVERY_ALLOWED)
    )
    errors.extend(
        _validate_enum(rel_path, "build_agent", data.get("build_agent"), _BUILD_AGENT_ALLOWED)
    )

    for key in (
        "jarvis_env",
        "dns_suffix",
        "time_zone",
        "backup_path",
        "k3s_version",
        "k3s_sha256",
        "pod_cidr",
        "service_cidr",
        "local_path_data",
        "flux_git_ref",
    ):
        val = data.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"{rel_path}: {key} must be a string")
        elif isinstance(val, str) and not val.strip():
            errors.append(f"{rel_path}: {key} must not be empty")

    errors.extend(_validate_https_url(rel_path, "flux_git_repo_url", data.get("flux_git_repo_url")))

    jarvis_env = data.get("jarvis_env")
    if isinstance(jarvis_env, str) and jarvis_env and jarvis_env != env_name:
        errors.append(
            f"{rel_path}: jarvis_env must match the env directory name ({env_name})"
        )

    k3s_sha = data.get("k3s_sha256")
    if isinstance(k3s_sha, str) and k3s_sha and not _SHA256_HEX.match(k3s_sha):
        errors.append(f"{rel_path}: k3s_sha256 must be a 64-character lowercase hex string")

    local_path = data.get("local_path_data")
    if isinstance(local_path, str) and local_path and not local_path.startswith("/"):
        errors.append(f"{rel_path}: local_path_data must be an absolute path")

    for key in (
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
    ):
        val = data.get(key)
        if val is None:
            continue
        if not _is_int_not_bool(val):
            errors.append(f"{rel_path}: {key} must be an integer")
        elif val <= 0:
            errors.append(f"{rel_path}: {key} must be > 0")

    return errors


def _validate_https_url(rel_path: str, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{rel_path}: {key} must be a string"]
    if not value.strip():
        return [f"{rel_path}: {key} must not be empty"]
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return [f"{rel_path}: {key} must be an https URL"]
    return []


def _validate_enum(rel_path: str, key: str, value: Any, allowed: tuple[str, ...]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{rel_path}: {key} must be a string"]
    if value not in allowed:
        allowed_s = ", ".join(allowed)
        return [f"{rel_path}: {key}: invalid value '{value}' (allowed: {allowed_s})"]
    return []


def _validate_hermes(data: dict[str, Any], rel_path: str) -> list[str]:
    errors: list[str] = []
    for key in data:
        if not _SNAKE_CASE.match(key):
            errors.append(f"{rel_path}: key must be lowercase snake_case: {key}")
        elif key not in _HERMES_TOP_LEVEL:
            errors.append(
                f"{rel_path}: unknown key '{key}' (allowed: {', '.join(sorted(_HERMES_TOP_LEVEL))})"
            )

    bots = data.get("bots", [])
    if not isinstance(bots, list):
        errors.append(f"{rel_path}: bots must be a list")

    cron = data.get("cron", {})
    if not isinstance(cron, dict):
        errors.append(f"{rel_path}: cron must be a mapping")

    tier_map = data.get("bot_tier_map", {})
    if not isinstance(tier_map, dict):
        errors.append(f"{rel_path}: bot_tier_map must be a mapping")
    else:
        for bot, tier in tier_map.items():
            if not isinstance(tier, str) or tier not in _TIER_VALUES:
                allowed = ", ".join(sorted(_TIER_VALUES))
                errors.append(
                    f"{rel_path}: bot_tier_map[{bot!r}]: invalid tier '{tier}' (allowed: {allowed})"
                )

    for key in ("tier_model_map", "lane_model_map"):
        val = data.get(key, {})
        if val is not None and not isinstance(val, dict):
            errors.append(f"{rel_path}: {key} must be a mapping")

    return errors


def _validate_apps(data: dict[str, Any], rel_path: str) -> list[str]:
    errors: list[str] = []
    for key in data:
        if not _SNAKE_CASE.match(key):
            errors.append(f"{rel_path}: key must be lowercase snake_case: {key}")
        elif key not in _APPS_TOP_LEVEL:
            errors.append(
                f"{rel_path}: unknown key '{key}' (allowed: {', '.join(sorted(_APPS_TOP_LEVEL))})"
            )

    enabled = data.get("app_hosting_enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append(f"{rel_path}: app_hosting_enabled must be a boolean")

    apps = data.get("apps", [])
    if not isinstance(apps, list):
        errors.append(f"{rel_path}: apps must be a list")

    return errors


def _validate_hosts(data: dict[str, Any], rel_path: str, env_name: str) -> list[str]:
    errors: list[str] = []
    all_group = data.get("all")
    if not isinstance(all_group, dict):
        errors.append(f"{rel_path}: expected top-level 'all' mapping")
        return errors

    hosts = all_group.get("hosts")
    if not isinstance(hosts, dict):
        errors.append(f"{rel_path}: expected all.hosts mapping")
        return errors

    if not hosts:
        errors.append(f"{env_name}: inventory must list at least one host")

    for hostname, host_vars in hosts.items():
        if _looks_like_ip(hostname):
            errors.append(f"{rel_path}: host key must be a hostname, not an IP: {hostname}")
        if host_vars is None:
            continue
        if not isinstance(host_vars, dict):
            errors.append(f"{rel_path}: host {hostname}: vars must be a mapping if present")
            continue
        if "ansible_host" in host_vars:
            ah = host_vars["ansible_host"]
            if isinstance(ah, str) and _looks_like_ip(ah):
                errors.append(
                    f"{rel_path}: host {hostname}: ansible_host must not be a plain IP (use group_vars SOPS)"
                )
        for key, val in host_vars.items():
            if isinstance(val, str) and _looks_like_ip(val) and key in (
                "ansible_host",
                "ip",
                "ansible_ssh_host",
            ):
                errors.append(
                    f"{rel_path}: host {hostname}: {key} must not be a plain IP in inventory"
                )

    return errors


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
