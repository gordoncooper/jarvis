"""Env schema validation for envs/<env>/ (CONTRACT: schema/CONTRACT.md)."""

from __future__ import annotations

import ipaddress
import re
import zoneinfo
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

EXAMPLE_ENV = "env-00"
DEFAULT_SSH_USER = "ubuntu"
DEFAULT_AGE_KEY_FILE = "~/.config/sops/age/keys.txt"


def is_example_env(name: str) -> bool:
    """True for the never-deployed example env, which is not an identity source."""
    return name == EXAMPLE_ENV

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
_HOSTNAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
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
        "backup_schedule",
        "k3s_version",
        "k3s_sha256",
        "pod_cidr",
        "service_cidr",
        "local_path_data",
        "flux_git_repo_url",
        "apps_git_repo_url",
        "flux_git_ref",
        "ntp_pools",
        "k3s_kubelet_kube_reserved",
        "k3s_kubelet_system_reserved",
        "k3s_kubelet_eviction_hard",
        "llm_provider_host",
    }
)

_CLUSTER_VARS_OPTIONAL = frozenset({"ssh_user", "age_key_file"})

_CLUSTER_VARS_KNOWN = _CLUSTER_VARS_REQUIRED | _CLUSTER_VARS_OPTIONAL

_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_ENV_GIT_PATH = re.compile(r"envs/([A-Za-z0-9][A-Za-z0-9._-]*)/")
_K3S_VERSION = re.compile(r"^v(\d+\.\d+\.\d+)\+k3s\d+$")

_HERMES_TOP_LEVEL = frozenset(
    {"bots", "cron", "bot_tier_map", "tier_model_map", "lane_model_map"}
)

_APPS_TOP_LEVEL = frozenset({"app_hosting_enabled", "apps"})

_NODE_ROLES = frozenset(
    {"control_plane", "workers", "storage", "backup", "gpu", "edge"}
)
_HOST_KEYS = frozenset({"node_roles", "data_disk"})
_DATA_DISK_KEYS = frozenset({"id", "wipe_confirmed"})
_SINGLETON_ROLES = ("control_plane", "backup")
_DISK_ID = re.compile(r"^/dev/disk/by-id/[A-Za-z0-9][A-Za-z0-9._+-]*$")
_DISK_PARTITION = re.compile(r"-part[0-9]+$")


class ValidationError(Exception):
    """One or more env schema violations."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


def kubernetes_version_from_k3s(value: str) -> str | None:
    """Return major.minor.0 for kubeconform schemas, or None when the pin does not match."""
    match = _K3S_VERSION.fullmatch(value.strip())
    if match is None:
        return None
    major, minor, _patch = match.group(1).split(".")
    return f"{major}.{minor}.0"


def resolve_cluster_vars(data: dict[str, Any]) -> dict[str, Any]:
    """Apply env schema defaults to cluster-vars (authoritative for consumers)."""
    resolved = dict(data)
    if "ssh_user" not in resolved:
        resolved["ssh_user"] = DEFAULT_SSH_USER
    if "age_key_file" not in resolved:
        resolved["age_key_file"] = DEFAULT_AGE_KEY_FILE
    return resolved


def validate_env_tree(repo_root: Path | str) -> dict[str, dict[str, Any]]:
    """Validate every env under envs/. Returns resolved cluster-vars per env name."""
    root = Path(repo_root).resolve()
    envs_dir = root / "envs"
    if not envs_dir.is_dir():
        raise ValidationError([f"missing envs directory: {_display_path(envs_dir, root)}"])

    errors: list[str] = []
    resolved_by_env: dict[str, dict[str, Any]] = {}
    owners_by_env: dict[str, str] = {}
    env_dirs = sorted(p for p in envs_dir.iterdir() if p.is_dir())
    if not env_dirs:
        errors.append(f"no env directories under {_display_path(envs_dir, root)}")
        raise ValidationError(errors)

    for env_dir in env_dirs:
        env_errors, env_resolved, owner = _validate_env(env_dir, root)
        errors.extend(env_errors)
        if env_resolved is not None:
            resolved_by_env[env_dir.name] = env_resolved
        if owner and not is_example_env(env_dir.name):
            owners_by_env[env_dir.name] = owner

    errors.extend(_cross_env_owner_errors(owners_by_env))
    errors.extend(
        _validate_sops_creation_rules(root, [env_dir.name for env_dir in env_dirs])
    )

    if errors:
        raise ValidationError(errors)
    return resolved_by_env


def _validate_sops_creation_rules(repo_root: Path, env_names: list[str]) -> list[str]:
    """One age creation rule per non-example env, and none for the example env."""
    path = repo_root / ".sops.yaml"
    if not path.is_file():
        rules: list[dict[str, Any]] = []
    else:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            return [f".sops.yaml: invalid YAML: {exc}"]
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return [".sops.yaml: expected a YAML mapping"]
        raw = data.get("creation_rules", [])
        if not isinstance(raw, list):
            return [".sops.yaml: creation_rules must be a list"]
        rules = [rule for rule in raw if isinstance(rule, dict)]

    def keyed(env_name: str) -> list[dict[str, Any]]:
        needle = f"envs/{env_name}/secrets/"
        return [
            rule
            for rule in rules
            if isinstance(rule.get("path_regex"), str) and needle in rule["path_regex"]
        ]

    errors: list[str] = []
    names = list(env_names)
    if EXAMPLE_ENV not in names:
        names.append(EXAMPLE_ENV)
    for env_name in names:
        count = len(keyed(env_name))
        if is_example_env(env_name):
            if count:
                errors.append(
                    f".sops.yaml: {env_name} must not have a creation rule, found {count}"
                )
            continue
        if count != 1:
            errors.append(
                f".sops.yaml: expected exactly one creation rule for "
                f"envs/{env_name}/secrets/, found {count}"
            )
    return errors


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def _validate_env(
    env_dir: Path, repo_root: Path
) -> tuple[list[str], dict[str, Any] | None, str | None]:
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
        return errors, None, None

    cluster_vars = _load_yaml(cluster_vars_path, errors, repo_root)
    hermes = _load_yaml(hermes_path, errors, repo_root)
    apps = _load_yaml(apps_path, errors, repo_root)
    hosts = _load_yaml(inventory, errors, repo_root)
    if errors or cluster_vars is None or hermes is None or apps is None or hosts is None:
        return errors, None, None

    cluster_errors, owner = _validate_cluster_vars(
        cluster_vars,
        _display_path(cluster_vars_path, repo_root),
        env_name,
    )
    errors.extend(cluster_errors)
    errors.extend(_validate_hermes(hermes, _display_path(hermes_path, repo_root)))
    errors.extend(_validate_apps(apps, _display_path(apps_path, repo_root)))
    errors.extend(_validate_hosts(hosts, _display_path(inventory, repo_root), env_name))
    errors.extend(_validate_flux_env_paths(env_dir, repo_root, env_name))

    if errors:
        return errors, None, owner
    return [], resolve_cluster_vars(cluster_vars), owner


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
) -> tuple[list[str], str | None]:
    errors: list[str] = []
    allowed = ", ".join(sorted(_CLUSTER_VARS_KNOWN))
    for key in data:
        if not _SNAKE_CASE.match(key):
            errors.append(f"{rel_path}: key must be lowercase snake_case: {key}")
        elif key not in _CLUSTER_VARS_KNOWN:
            errors.append(f"{rel_path}: unknown key '{key}' (allowed: {allowed})")

    missing = sorted(_CLUSTER_VARS_REQUIRED - set(data))
    for key in missing:
        errors.append(f"{rel_path}: missing required key: {key}")

    if is_example_env(env_name):
        for key in sorted(_CLUSTER_VARS_KNOWN - set(data)):
            errors.append(f"{rel_path}: {EXAMPLE_ENV} must set every known key: {key}")

    if "ssh_user" in data and not isinstance(data["ssh_user"], str):
        errors.append(f"{rel_path}: ssh_user must be a string")
    elif "ssh_user" in data and not data["ssh_user"]:
        errors.append(f"{rel_path}: ssh_user must not be empty")

    if "age_key_file" in data and not isinstance(data["age_key_file"], str):
        errors.append(f"{rel_path}: age_key_file must be a string")
    elif "age_key_file" in data and not data["age_key_file"].strip():
        errors.append(f"{rel_path}: age_key_file must not be empty")

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

    for key in ("pod_cidr", "service_cidr"):
        errors.extend(_validate_cidr(rel_path, key, data.get(key)))

    flux_errors, flux_owner = _validate_repo_url(
        rel_path, "flux_git_repo_url", data.get("flux_git_repo_url")
    )
    apps_errors, apps_owner = _validate_repo_url(
        rel_path, "apps_git_repo_url", data.get("apps_git_repo_url")
    )
    errors.extend(flux_errors)
    errors.extend(apps_errors)
    owner: str | None = None
    if flux_owner and apps_owner:
        if flux_owner != apps_owner:
            errors.append(
                f"{rel_path}: flux_git_repo_url and apps_git_repo_url must use the same owner handle"
            )
        else:
            owner = flux_owner

    errors.extend(
        _validate_bare_hostname(rel_path, "llm_provider_host", data.get("llm_provider_host"))
    )

    jarvis_env = data.get("jarvis_env")
    if isinstance(jarvis_env, str) and jarvis_env and jarvis_env != env_name:
        errors.append(
            f"{rel_path}: jarvis_env must match the env directory name ({env_name})"
        )

    k3s_sha = data.get("k3s_sha256")
    if isinstance(k3s_sha, str) and k3s_sha and not _SHA256_HEX.match(k3s_sha):
        errors.append(f"{rel_path}: k3s_sha256 must be a 64-character lowercase hex string")

    time_zone = data.get("time_zone")
    if isinstance(time_zone, str) and time_zone.strip():
        errors.extend(_validate_time_zone(rel_path, time_zone.strip()))

    k3s_version = data.get("k3s_version")
    if isinstance(k3s_version, str) and k3s_version.strip():
        if kubernetes_version_from_k3s(k3s_version) is None:
            errors.append(
                f"{rel_path}: k3s_version must be a k3s version string "
                "(v<major>.<minor>.<patch>+k3s<n>)"
            )

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

    if "backup_schedule" in data:
        schedule = data["backup_schedule"]
        if not isinstance(schedule, str) or len(schedule.split()) != 5:
            errors.append(f"{rel_path}: backup_schedule must be a five-field cron")

    errors.extend(_validate_ntp_pools(rel_path, data.get("ntp_pools")))

    for key in (
        "k3s_kubelet_kube_reserved",
        "k3s_kubelet_system_reserved",
        "k3s_kubelet_eviction_hard",
    ):
        val = data.get(key)
        if val is None:
            continue
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{rel_path}: {key} must be a non-empty string")

    return errors, owner


def _validate_time_zone(rel_path: str, value: str) -> list[str]:
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return [f"{rel_path}: time_zone must be an IANA time zone"]
    return []


def _validate_cidr(rel_path: str, key: str, value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        ipaddress.ip_network(value.strip(), strict=True)
    except ValueError:
        return [f"{rel_path}: {key} must be a CIDR"]
    return []


def _validate_flux_env_paths(env_dir: Path, repo_root: Path, env_name: str) -> list[str]:
    if is_example_env(env_name):
        return []
    flux = env_dir / "flux"
    if not flux.is_dir():
        return []
    errors: list[str] = []
    seen: set[str] = set()
    for path in sorted(item for item in flux.rglob("*") if item.is_file()):
        rel = _display_path(path, repo_root)
        text = path.read_text(encoding="utf-8")
        for found in _ENV_GIT_PATH.findall(text):
            if found == env_name:
                continue
            message = f"{rel}: envs/{found}/ must use this env's name ({env_name})"
            if message not in seen:
                seen.add(message)
                errors.append(message)
    return errors


def _validate_ntp_pools(rel_path: str, value: Any) -> list[str]:
    if value is None:
        return [f"{rel_path}: missing required key: ntp_pools"]
    if not isinstance(value, list) or not value:
        return [f"{rel_path}: ntp_pools must be a non-empty list"]
    errors: list[str] = []
    for idx, pool in enumerate(value):
        if not isinstance(pool, str) or not pool.strip():
            errors.append(f"{rel_path}: ntp_pools[{idx}] must be a non-empty string")
            continue
        if not _HOSTNAME.match(pool.strip()):
            errors.append(f"{rel_path}: ntp_pools[{idx}] must be a hostname or pool name")
    return errors


def repo_owner_handle(url: str) -> str | None:
    """Return the first path segment when url is https and names owner/repo."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0]


def _validate_repo_url(rel_path: str, key: str, value: Any) -> tuple[list[str], str | None]:
    errors = _validate_https_url(rel_path, key, value)
    if errors or not isinstance(value, str) or not value.strip():
        return errors, None
    owner = repo_owner_handle(value)
    if owner is None:
        errors.append(f"{rel_path}: {key} must include an owner and a repo")
        return errors, None
    return errors, owner


def _validate_bare_hostname(rel_path: str, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{rel_path}: {key} must be a string"]
    if not value.strip():
        return [f"{rel_path}: {key} must not be empty"]
    if _looks_like_ip(value) or not _HOSTNAME.match(value):
        return [f"{rel_path}: {key} must be a bare hostname"]
    return []


def _cross_env_owner_errors(owners_by_env: dict[str, str]) -> list[str]:
    if len(set(owners_by_env.values())) <= 1:
        return []
    detail = ", ".join(f"{name}={owners_by_env[name]}" for name in sorted(owners_by_env))
    return [f"repo owner handle must be the same across non-example envs: {detail}"]


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
    for key in sorted(set(data) - {"all"}):
        errors.append(f"{rel_path}: unknown key '{key}' (allowed: all)")

    all_group = data.get("all")
    if not isinstance(all_group, dict):
        errors.append(f"{rel_path}: expected top-level 'all' mapping")
        return errors

    for key in sorted(set(all_group) - {"hosts"}):
        errors.append(f"{rel_path}: unknown key '{key}' under all (allowed: hosts)")

    hosts = all_group.get("hosts")
    if not isinstance(hosts, dict):
        errors.append(f"{rel_path}: expected all.hosts mapping")
        return errors

    if not hosts:
        errors.append(f"{env_name}: inventory must list at least one host")

    role_counts = {role: 0 for role in _SINGLETON_ROLES}
    declared_roles: set[str] = set()
    allowed_keys = ", ".join(sorted(_HOST_KEYS))
    allowed_roles = ", ".join(sorted(_NODE_ROLES))

    for hostname, host_vars in hosts.items():
        if not isinstance(hostname, str) or _looks_like_ip(hostname):
            errors.append(f"{rel_path}: host key must be a hostname, not an IP: {hostname}")
        if not isinstance(host_vars, dict):
            errors.append(f"{rel_path}: host {hostname}: node_roles is required")
            continue

        for key in sorted(set(host_vars) - _HOST_KEYS):
            errors.append(
                f"{rel_path}: host {hostname}: unknown key '{key}' (allowed: {allowed_keys})"
            )

        roles = host_vars.get("node_roles")
        if not isinstance(roles, list) or not roles:
            errors.append(f"{rel_path}: host {hostname}: node_roles must be a non-empty list")
        else:
            seen: set[str] = set()
            for role in roles:
                if not isinstance(role, str) or role not in _NODE_ROLES:
                    errors.append(
                        f"{rel_path}: host {hostname}: unknown role '{role}' "
                        f"(allowed: {allowed_roles})"
                    )
                    continue
                if role in seen:
                    errors.append(f"{rel_path}: host {hostname}: duplicate role '{role}'")
                    continue
                seen.add(role)
                declared_roles.add(role)
                if role in role_counts:
                    role_counts[role] += 1

        if "data_disk" in host_vars:
            errors.extend(_validate_data_disk(host_vars["data_disk"], rel_path, str(hostname)))

    for role, count in role_counts.items():
        if count != 1:
            errors.append(f"{rel_path}: expected exactly one {role} host, found {count}")

    if "edge" not in declared_roles:
        errors.append(f"{rel_path}: expected at least one edge host")

    if is_example_env(env_name):
        for role in sorted(_NODE_ROLES - declared_roles):
            errors.append(f"{rel_path}: {EXAMPLE_ENV} must declare every known role: {role}")

    return errors


def _validate_data_disk(disk: Any, rel_path: str, hostname: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(disk, dict):
        errors.append(f"{rel_path}: host {hostname}: data_disk must be a mapping")
        return errors

    allowed = ", ".join(sorted(_DATA_DISK_KEYS))
    for key in sorted(set(disk) - _DATA_DISK_KEYS):
        errors.append(
            f"{rel_path}: host {hostname}: data_disk unknown key '{key}' (allowed: {allowed})"
        )
    for key in sorted(_DATA_DISK_KEYS - set(disk)):
        errors.append(f"{rel_path}: host {hostname}: data_disk missing required key: {key}")

    disk_id = disk.get("id")
    if "id" in disk and (
        not isinstance(disk_id, str)
        or _DISK_ID.match(disk_id) is None
        or _DISK_PARTITION.search(disk_id) is not None
    ):
        errors.append(
            f"{rel_path}: host {hostname}: data_disk.id must be a whole-disk /dev/disk/by-id path"
        )

    if "wipe_confirmed" in disk and not isinstance(disk.get("wipe_confirmed"), bool):
        errors.append(
            f"{rel_path}: host {hostname}: data_disk.wipe_confirmed must be a boolean"
        )
    return errors


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
