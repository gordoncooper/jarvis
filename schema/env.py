"""Env schema validation for envs/<env>/ (CONTRACT: schema/CONTRACT.md)."""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
import zoneinfo
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

_PRODUCT_ROOT = Path(__file__).resolve().parents[1]
_TOOLS_LIB = _PRODUCT_ROOT / "tools" / "lib"
if str(_TOOLS_LIB) not in sys.path:
    sys.path.insert(0, str(_TOOLS_LIB))

from _env import instance_root  # noqa: E402,F401  (the one INSTANCE resolver)
import _app_backups  # noqa: E402
import _apps  # noqa: E402

EXAMPLE_ENV = "env-00"
DEFAULT_SSH_USER = "ubuntu"
DEFAULT_AGE_KEY_FILE = "~/.config/sops/age/keys.txt"
# Weekly repository check, hours away from midnight-to-01:00 backups and the 00:05/12:05 etcd copy.
DEFAULT_BACKUP_CHECK_SCHEDULE = "30 6 * * 3"
# The share of each node's memory no pod is scheduled into (the k3s role's eviction-hard).
DEFAULT_NODE_MEMORY_RESERVE_PERCENT = 10
# An env turns the off-host copy on by listing this directory and its sealed secret in platform-configs.
OFFSITE_COPY_DIR = "platform/backups/offsite"
OFFSITE_SECRET_FILE = "backups-offsite.sops.yaml"
# An env turns app hosting on by listing this core Component in its platform-configs components.
APP_HOSTING_DIR = "platform/app-hosting"
# The root GitRepository includes the core source at this directory, so env files
# reach core as <root>/core/... . In the product checkout, core is the checkout itself.
CORE_SOURCE_DIR = "core"
# The root source and core source, and the file that declares them with the root Kustomization.
ROOT_SOURCE = "flux-system"
CORE_SOURCE = "core"
SYNC_FILE = "sync.yaml"
# The sealed read-only deploy key an ssh:// root reads with.
DEPLOY_KEY_FILE = "flux-deploy-key.sops.yaml"
# Files under flux/ that Ansible and the root Kustomization read.
_FLUX_ENTRY_FILES = (
    SYNC_FILE,
    "flux-instance.yaml",
    "kustomization.yaml",
    "identities.yaml",
    "kustomizations/platform-controllers.yaml",
    "kustomizations/platform-configs.yaml",
    "kustomizations/services.yaml",
    "kustomizations/apps.yaml",
    "platform-configs/kustomization.yaml",
)
# Sealed once bootstrap-secrets has run; platform-configs applies the last two.
_BOOTSTRAP_SEALED = ("k3s-token", "cert-manager-ca", "backups-restic")
# Add-ons: one folder each under <core>/services/, a kustomize Component the services
# Kustomization lists in spec.components. Slot contracts live under services/slots/.
ADDONS_DIR = "services"
SLOTS_DIR = "slots"
SERVICES_KUSTOMIZATION = "kustomizations/services.yaml"
# On an add-on's Flux Kustomization: the slot it fills.
SLOT_LABEL = "addons.jarvis.io/slot"
# An add-on's sealed Secrets are under envs/<env>/secrets/<namespace>/.
ADDON_SECRETS_PATH = "./envs/${jarvis_env}/secrets/"


def is_example_env(name: str) -> bool:
    """True for the never-deployed example env, which is not an identity source."""
    return name == EXAMPLE_ENV

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
_HOSTNAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SOPS_ENV_SECRETS = re.compile(r"envs/([A-Za-z0-9][A-Za-z0-9._-]*)/secrets/")

_CLUSTER_VARS_REQUIRED = frozenset(
    {
        "jarvis_env",
        "dns_suffix",
        "time_zone",
        "backup_path",
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
        "backup_schedule",
        "k3s_version",
        "k3s_sha256",
        "pod_cidr",
        "service_cidr",
        "app_dns_cluster_ip",
        "local_path_data",
        "flux_git_repo_url",
        "flux_git_ref",
        "core_git_repo_url",
        "core_git_ref",
        "ntp_pools",
        "k3s_kubelet_kube_reserved",
        "k3s_kubelet_system_reserved",
        "k3s_kubelet_eviction_hard",
    }
)

# Optional. A set value is still checked. apps_git_repo_url is required once values/apps.yaml lists an
# app, and lan_cidrs once it grants lan-all or lan-target.
_CLUSTER_VARS_OPTIONAL = frozenset(
    {
        "ssh_user",
        "age_key_file",
        "apps_git_repo_url",
        "lan_cidrs",
        "llm_provider_host",
        "chrony_nts",
        "backup_check_schedule",
        "node_memory_reserve_percent",
    }
)

_CLUSTER_VARS_KNOWN = _CLUSTER_VARS_REQUIRED | _CLUSTER_VARS_OPTIONAL

_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_ENV_GIT_PATH = re.compile(r"envs/([A-Za-z0-9][A-Za-z0-9._-]*)/")
_K3S_VERSION = re.compile(r"^v(\d+\.\d+\.\d+)\+k3s\d+$")

_NODE_ROLES = frozenset({"control_plane", "workers", "backup", "gpu", "edge"})
_ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOST_KEYS = frozenset(
    {"node_roles", "data_disk", "ssh_user", "k3s_kubelet_kube_reserved",
     "k3s_kubelet_system_reserved", "k3s_kubelet_eviction_hard"}
)
_DATA_MOUNT = "/var/lib/jarvis"
_SINGLETON_ROLES = ("control_plane", "backup")


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
    if "backup_check_schedule" not in resolved:
        resolved["backup_check_schedule"] = DEFAULT_BACKUP_CHECK_SCHEDULE
    if "node_memory_reserve_percent" not in resolved:
        resolved["node_memory_reserve_percent"] = DEFAULT_NODE_MEMORY_RESERVE_PERCENT
    return resolved


def validate_env_tree(
    repo_root: Path | str, *, pre_host: bool = False
) -> dict[str, dict[str, Any]]:
    """Validate every env under envs/. Returns resolved cluster-vars per env name.

    pre_host skips the missing-recipient requirement and the generated ConfigMap
    render check. Other .sops.yaml checks still run. The full check runs both.
    """
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

    env_names = [env_dir.name for env_dir in env_dirs]
    for env_dir in env_dirs:
        env_errors, env_resolved, _owner = _validate_env(
            env_dir, root, pre_host=pre_host
        )
        errors.extend(env_errors)
        if env_resolved is not None:
            resolved_by_env[env_dir.name] = env_resolved

    rules, sops_errors = _load_sops_creation_rules(root)
    errors.extend(sops_errors)
    if rules is not None:
        errors.extend(
            _validate_sops_creation_rules(
                rules, env_names, require_recipient=not pre_host
            )
        )
        errors.extend(_validate_sops_unknown_env_rules(rules, env_names))

    if errors:
        raise ValidationError(errors)
    return resolved_by_env


def validate_one_env(repo_root: Path | str, name: str, *, pre_host: bool = False) -> dict[str, Any]:
    """Validate one env and its .sops.yaml creation rules. make site runs this first."""
    if not _ENV_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValidationError([f"invalid env name: {name}"])
    root = Path(repo_root).resolve()
    env_dir = root / "envs" / name
    if not env_dir.is_dir():
        raise ValidationError([f"unknown env: {name}"])
    errors, resolved, _owner = _validate_env(env_dir, root, pre_host=pre_host)
    rules, sops_errors = _load_sops_creation_rules(root)
    errors.extend(sops_errors)
    if rules is not None:
        errors.extend(
            _validate_sops_creation_rules(rules, [name], require_recipient=not pre_host)
        )
        env_names = [p.name for p in sorted((root / "envs").iterdir()) if p.is_dir()]
        errors.extend(_validate_sops_unknown_env_rules(rules, env_names))
    if errors or resolved is None:
        raise ValidationError(errors or [f"{name}: invalid env"])
    return resolved


def _load_sops_creation_rules(
    repo_root: Path,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Load .sops.yaml creation rules. None means the file is unusable."""
    path = repo_root / ".sops.yaml"
    if not path.is_file():
        return [], []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [f".sops.yaml: invalid YAML: {exc}"]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, [".sops.yaml: expected a YAML mapping"]
    raw = data.get("creation_rules", [])
    if not isinstance(raw, list):
        return None, [".sops.yaml: creation_rules must be a list"]
    return [rule for rule in raw if isinstance(rule, dict)], []


def _validate_sops_creation_rules(
    rules: list[dict[str, Any]], env_names: list[str], *, require_recipient: bool
) -> list[str]:
    """One age creation rule per non-example env, and none for the example env."""

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
        if count > 1 or (require_recipient and count != 1):
            errors.append(
                f".sops.yaml: expected exactly one creation rule for "
                f"envs/{env_name}/secrets/, found {count}"
            )
    return errors


def _validate_sops_unknown_env_rules(
    rules: list[dict[str, Any]], env_names: list[str]
) -> list[str]:
    """A creation rule must name an env folder that exists."""
    known = set(env_names)
    errors: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        regex = rule.get("path_regex")
        if not isinstance(regex, str):
            continue
        for name in _SOPS_ENV_SECRETS.findall(regex):
            if name in known or name in seen:
                continue
            seen.add(name)
            errors.append(
                f".sops.yaml: creation rule names envs/{name}/secrets/, "
                f"but that env folder does not exist; remove the rule"
            )
    return errors


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def _validate_env(
    env_dir: Path, repo_root: Path, *, pre_host: bool = False
) -> tuple[list[str], dict[str, Any] | None, str | None]:
    errors: list[str] = []
    env_name = env_dir.name
    values = env_dir / "values"
    inventory = env_dir / "inventory" / "hosts.yml"

    cluster_vars_path = values / "cluster-vars.yaml"

    secrets = env_dir / "secrets"
    # Once bootstrap-secrets has run, the k3s tokens are sealed too. prep-node checks before that.
    bootstrapped = any(secrets.glob("*.sops.yaml"))
    sealed = (
        tuple(secrets / f"{name}.sops.yaml" for name in _BOOTSTRAP_SEALED) if bootstrapped else ()
    )
    # An ssh:// root reads the env repo with the sealed deploy key.
    if cluster_vars_path.is_file() and _ssh_root(cluster_vars_path):
        sealed = (*sealed, secrets / DEPLOY_KEY_FILE)
    for path in (cluster_vars_path, inventory, *sealed):
        if not path.is_file():
            errors.append(
                f"{env_name}: missing required file {_display_path(path, repo_root)}"
            )

    if errors:
        return errors, None, None

    cluster_vars = _load_yaml(cluster_vars_path, errors, repo_root)
    hosts = _load_yaml(inventory, errors, repo_root)
    if errors or cluster_vars is None or hosts is None:
        return errors, None, None

    cluster_errors, owner = _validate_cluster_vars(
        cluster_vars,
        _display_path(cluster_vars_path, repo_root),
        env_name,
    )
    errors.extend(cluster_errors)
    errors.extend(_validate_hosts(hosts, _display_path(inventory, repo_root), env_name))
    errors.extend(_validate_flux_env_paths(env_dir, repo_root, env_name))
    errors.extend(_validate_offsite_secret(env_dir, repo_root, env_name))
    errors.extend(_validate_app_backups(env_dir, repo_root, env_name, bootstrapped))
    errors.extend(_validate_addons(env_dir, repo_root, env_name, bootstrapped))
    errors.extend(_validate_apps(env_dir, repo_root, cluster_vars, pre_host=pre_host))
    errors.extend(_validate_flux_entry_files(env_dir, repo_root, env_name, bootstrapped))
    errors.extend(_validate_flux_sources(env_dir, repo_root, env_name, cluster_vars))
    errors.extend(
        _validate_committed_configmap(
            env_dir, repo_root, cluster_vars_path, pre_host=pre_host
        )
    )

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


def _keys_set(data: dict[str, Any]) -> set[str]:
    """Keys with a value. A present null is unset."""
    return {key for key, value in data.items() if value is not None}


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

    missing = sorted(_CLUSTER_VARS_REQUIRED - _keys_set(data))
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
    errors.extend(_validate_app_dns_cluster_ip(rel_path, data))
    errors.extend(
        f"{rel_path}: {problem}"
        for problem in _apps.lan_cidrs_problems(data.get(_apps.LAN_CIDRS), data.get("pod_cidr"), data.get("service_cidr"))
    )

    flux_errors, flux_owner = _validate_repo_url(
        rel_path, "flux_git_repo_url", data.get("flux_git_repo_url"), ssh=True
    )
    core_errors, _core_owner = _validate_repo_url(
        rel_path, "core_git_repo_url", data.get("core_git_repo_url")
    )
    errors.extend(core_errors)
    val = data.get("core_git_ref")
    if val is not None and (not isinstance(val, str) or not val.strip()):
        errors.append(f"{rel_path}: core_git_ref must be a non-empty string")
    apps_errors, apps_owner = _validate_repo_url(
        rel_path, "apps_git_repo_url", data.get("apps_git_repo_url")
    )
    errors.extend(flux_errors)
    errors.extend(apps_errors)
    owner: str | None = None
    if flux_owner and apps_owner and flux_owner != apps_owner:
        errors.append(
            f"{rel_path}: flux_git_repo_url and apps_git_repo_url must use the same owner handle"
        )
    elif flux_owner:
        # apps_git_repo_url is optional. The env owner still comes from flux.
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

    dns_suffix = data.get("dns_suffix")
    if isinstance(dns_suffix, str) and dns_suffix.strip():
        errors.extend(_validate_dns_suffix(rel_path, dns_suffix.strip()))

    k3s_version = data.get("k3s_version")
    if isinstance(k3s_version, str) and k3s_version.strip():
        if kubernetes_version_from_k3s(k3s_version) is None:
            errors.append(
                f"{rel_path}: k3s_version must be a k3s version string "
                "(v<major>.<minor>.<patch>+k3s<n>)"
            )

    errors.extend(_validate_local_path_data(rel_path, data.get("local_path_data")))
    errors.extend(_validate_backup_path(rel_path, data.get("backup_path")))

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

    reserve = data.get("node_memory_reserve_percent")
    if reserve is not None and (not _is_int_not_bool(reserve) or not 0 <= reserve <= 50):
        errors.append(f"{rel_path}: node_memory_reserve_percent must be an integer from 0 to 50")

    for key in ("backup_schedule", "backup_check_schedule"):
        schedule = data.get(key)
        if schedule is not None and (
            not isinstance(schedule, str) or len(schedule.split()) != 5
        ):
            errors.append(f"{rel_path}: {key} must be a five-field cron")

    errors.extend(_validate_ntp_pools(rel_path, data.get("ntp_pools")))
    errors.extend(_validate_chrony_nts(rel_path, data.get("chrony_nts")))

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


def _validate_dns_suffix(rel_path: str, value: str) -> list[str]:
    if len(value) > 253 or not value or value.startswith(".") or value.endswith("."):
        return [f"{rel_path}: dns_suffix must be a lowercase DNS name"]
    if not all(_DNS_LABEL.fullmatch(label) for label in value.split(".")):
        return [f"{rel_path}: dns_suffix must be a lowercase DNS name"]
    return []


def _validate_app_dns_cluster_ip(rel_path: str, data: dict[str, Any]) -> list[str]:
    """ClusterIP for App DNS. CoreDNS forward takes an address."""
    key = "app_dns_cluster_ip"
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, str):
        return [f"{rel_path}: {key} must be a string"]
    if not value.strip():
        return [f"{rel_path}: {key} must not be empty"]
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return [f"{rel_path}: {key} must be an IP address"]

    cidr = data.get("service_cidr")
    if not isinstance(cidr, str) or not cidr.strip():
        return []
    try:
        network = ipaddress.ip_network(cidr.strip(), strict=True)
    except ValueError:
        return []
    if addr not in network:
        return [f"{rel_path}: {key} must be inside service_cidr"]
    if addr == network.network_address or addr == network.broadcast_address:
        return [f"{rel_path}: {key} must be a host address inside service_cidr"]
    first = next(network.hosts(), None)
    if first is not None and addr == first:
        return [f"{rel_path}: {key} must not be the first address of service_cidr"]
    # k3s cluster DNS is the tenth address of the service CIDR (10.43.0.10
    # when the CIDR is 10.43.0.0/16). network_address + 10 is that address.
    tenth = network.network_address + 10
    if tenth in network and addr == tenth:
        return [f"{rel_path}: {key} must not be the tenth address of service_cidr"]
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


def _platform_configs(env_dir: Path) -> tuple[Path, dict]:
    path = env_dir / "flux" / "platform-configs" / "kustomization.yaml"
    if not path.is_file():
        return path, {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return path, doc if isinstance(doc, dict) else {}


def _core_dir_listed(env_dir: Path, repo_root: Path, rel: str) -> bool:
    """True when the env's platform-configs list includes core directory rel."""
    path, doc = _platform_configs(env_dir)
    target = (Path(repo_root) / CORE_SOURCE_DIR / rel).resolve()
    return any(
        isinstance(item, str) and (path.parent / item).resolve() == target
        for item in doc.get("resources") or []
    )


def offsite_copy_listed(env_dir: Path, repo_root: Path) -> bool:
    """True when the env's platform-configs list includes the off-host copy directory."""
    return _core_dir_listed(env_dir, repo_root, OFFSITE_COPY_DIR)


def apps_server_listed(env_dir: Path, repo_root: Path) -> bool:
    """True when the env runs the apps rest-server (its platform-configs list the directory)."""
    return _core_dir_listed(env_dir, repo_root, _app_backups.APPS_SERVER_DIR)


def _app_hosting_component(env_dir: Path, repo_root: Path, item: object) -> bool:
    path, _doc = _platform_configs(env_dir)
    target = (Path(repo_root) / CORE_SOURCE_DIR / APP_HOSTING_DIR).resolve()
    return isinstance(item, str) and (path.parent / item.strip()).resolve() == target


def app_hosting_listed(env_dir: Path, repo_root: Path) -> bool:
    """True when the env turns app hosting on: its platform-configs components list the core
    app-hosting Component."""
    _path, doc = _platform_configs(env_dir)
    components = doc.get("components")
    return isinstance(components, list) and any(
        _app_hosting_component(env_dir, repo_root, item) for item in components
    )


def app_registrations(env_dir: Path, repo_root: Path | None = None) -> list[object]:
    """The app credentials an env registers: its platform-configs components other than the
    app-hosting switch, as <registration dir>/<namespace>. An entry of another shape is
    returned as is for the check."""
    _path, doc = _platform_configs(env_dir)
    components = doc.get("components")
    if not isinstance(components, list):
        return []
    root = Path(repo_root) if repo_root is not None else env_dir.parents[1]
    prefix = f"{_app_backups.REGISTRATION_DIR}/"
    return [
        item.strip().removeprefix("./").removeprefix(prefix)
        if isinstance(item, str) and item.strip().removeprefix("./").startswith(prefix)
        else item
        for item in components
        if not _app_hosting_component(env_dir, root, item)
    ]


def _validate_app_backups(env_dir: Path, repo_root: Path, env_name: str, bootstrapped: bool) -> list[str]:
    """Each registered app credential is an app- namespace's, in its exact registration Component,
    with the apps server on and, once bootstrapped, its sealed file present. An app credential is
    sealed only in its own namespace's folder."""
    rel = _display_path(env_dir / "flux" / "platform-configs" / "kustomization.yaml", repo_root)
    errors: list[str] = []
    registered = app_registrations(env_dir, repo_root)
    for item in registered:
        if not isinstance(item, str) or not _app_backups.APP_NAMESPACE.fullmatch(item):
            errors.append(
                f"{rel}: component {item!r} is not an app credential registration "
                f"({_app_backups.REGISTRATION_DIR}/app-<name>); only hosted apps register one"
            )
            continue
        component = repo_root / _app_backups.registration_rel(env_name, item)
        if not component.is_file() or component.read_text(encoding="utf-8") != _app_backups.registration_component(item):
            errors.append(
                f"{env_name}: {_display_path(component, repo_root)} is missing or differs from "
                f"the registration make bootstrap-secrets APP={item} writes"
            )
        sealed = repo_root / _app_backups.sealed_rel(env_name, item)
        if bootstrapped and not sealed.is_file():
            errors.append(
                f"{env_name}: {rel} registers {item}, missing required file "
                f"{_display_path(sealed, repo_root)}; run make bootstrap-secrets APP={item}"
            )
    if registered and not apps_server_listed(env_dir, repo_root):
        errors.append(
            f"{rel}: registers app credentials but does not list "
            f"{_app_backups.APPS_SERVER_DIR}; an app's backups need the apps server"
        )
    secrets = env_dir / "secrets"
    for path in sorted(secrets.rglob(f"{_app_backups.secret_name('app-')}*.sops.yaml")):
        namespace = path.name.removeprefix(_app_backups.secret_name("")).removesuffix(".sops.yaml")
        if path.parent != secrets / namespace:
            errors.append(
                f"{_display_path(path, repo_root)}: an app credential is sealed only in its own "
                f"folder, {_display_path(secrets / namespace, repo_root)}/"
            )
    return errors


def _validate_offsite_secret(env_dir: Path, repo_root: Path, env_name: str) -> list[str]:
    """A bootstrapped env that lists the off-host copy holds its sealed target secret. Like the
    k3s tokens, it is not required before bootstrap-secrets, when prep-node checks a new env."""
    secrets = env_dir / "secrets"
    if not any(secrets.glob("*.sops.yaml")) or not offsite_copy_listed(env_dir, repo_root):
        return []
    secret = secrets / OFFSITE_SECRET_FILE
    if secret.is_file():
        return []
    return [
        f"{env_name}: platform-configs lists {OFFSITE_COPY_DIR}, "
        f"missing required file {_display_path(secret, repo_root)}"
    ]


def core_dir(repo_root: Path | str) -> Path:
    """Core as the env's Flux paths see it: <root>/core when the instance holds it there (its CI
    does), otherwise the product checkout running this code."""
    root = Path(repo_root)
    return root / CORE_SOURCE_DIR if (root / CORE_SOURCE_DIR).is_dir() else _PRODUCT_ROOT


def addon_names(core: Path) -> tuple[str, ...]:
    """Every add-on core ships: a folder under services/ holding a kustomization file."""
    base = core / ADDONS_DIR
    if not base.is_dir():
        return ()
    return tuple(
        sorted(
            child.name
            for child in base.iterdir()
            if child.is_dir() and child.name != SLOTS_DIR and (child / "kustomization.yaml").is_file()
        )
    )


def slot_names(core: Path) -> tuple[str, ...]:
    """Every slot core defines: a folder under services/slots/."""
    base = core / ADDONS_DIR / SLOTS_DIR
    return tuple(sorted(child.name for child in base.iterdir() if child.is_dir())) if base.is_dir() else ()


def enabled_addons(env_dir: Path) -> list[str]:
    """The add-ons an env turns on: spec.components of its services Kustomization. None is core-only."""
    path = env_dir / "flux" / SERVICES_KUSTOMIZATION
    if not path.is_file():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = doc.get("spec") if isinstance(doc, dict) else None
    components = spec.get("components") if isinstance(spec, dict) else None
    if not isinstance(components, list):
        return []
    return [item.strip().removeprefix("./") if isinstance(item, str) else item for item in components]


def addon_documents(core: Path, name: str) -> list[dict]:
    """The objects in the files an add-on's Component lists. Add-on wiring lists files only."""
    folder = core / ADDONS_DIR / name
    listing = yaml.safe_load((folder / "kustomization.yaml").read_text(encoding="utf-8"))
    docs: list[dict] = []
    for item in (listing or {}).get("resources") or []:
        path = folder / str(item)
        if path.is_file():
            docs.extend(doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict))
    return docs


def addon_slots(docs: list[dict]) -> set[str]:
    """Slots an add-on fills, from the label on its Flux Kustomizations."""
    found: set[str] = set()
    for doc in docs:
        labels = (doc.get("metadata") or {}).get("labels") or {}
        if doc.get("kind") == "Kustomization" and isinstance(labels.get(SLOT_LABEL), str):
            found.add(labels[SLOT_LABEL])
    return found


def addon_secret_namespaces(docs: list[dict]) -> list[str]:
    """Namespaces whose sealed Secrets an add-on's decrypting Kustomizations read from the env."""
    found = []
    for doc in docs:
        spec = doc.get("spec") or {}
        path = spec.get("path")
        if doc.get("kind") == "Kustomization" and spec.get("decryption") and isinstance(path, str):
            if path.startswith(ADDON_SECRETS_PATH):
                found.append(path.removeprefix(ADDON_SECRETS_PATH).strip("/"))
    return found


def _validate_addons(env_dir: Path, repo_root: Path, env_name: str, bootstrapped: bool) -> list[str]:
    """Each listed add-on exists in core, once; at most one per slot; its sealed Secrets exist once
    the env is bootstrapped. Like the off-host copy, a new env before bootstrap needs no secret."""
    rel = _display_path(env_dir / "flux" / SERVICES_KUSTOMIZATION, repo_root)
    listed = enabled_addons(env_dir)
    core = core_dir(repo_root)
    known = addon_names(core)
    errors: list[str] = []
    seen: set[str] = set()
    by_slot: dict[str, list[str]] = {}
    for name in listed:
        if not isinstance(name, str) or name not in known:
            errors.append(f"{rel}: unknown add-on {name!r} in spec.components (known: {', '.join(known) or 'none'})")
            continue
        if name in seen:
            errors.append(f"{rel}: add-on {name} is listed twice")
            continue
        seen.add(name)
        docs = addon_documents(core, name)
        for slot in sorted(addon_slots(docs)):
            by_slot.setdefault(slot, []).append(name)
        if not bootstrapped or is_example_env(env_name):
            continue
        for namespace in addon_secret_namespaces(docs):
            folder = env_dir / "secrets" / namespace
            if not any(folder.glob("*.sops.yaml")):
                errors.append(
                    f"{env_name}: add-on {name} decrypts {_display_path(folder, repo_root)}, "
                    "which holds no sealed file; seal one with make seal NAMESPACE=" + namespace
                )
    for slot, names in sorted(by_slot.items()):
        if len(names) > 1:
            errors.append(
                f"{rel}: add-ons {', '.join(names)} all fill slot {slot}; an env enables at most one per slot"
            )
    return errors


def _validate_apps(
    env_dir: Path, repo_root: Path, cluster_vars: dict[str, Any], *, pre_host: bool
) -> list[str]:
    """values/apps.yaml lists app names once each and valid exception lines; an env that lists
    an app sets apps_git_repo_url, and its flux tree lists the committed render, which matches.
    An env with no app has neither the render nor its line."""
    rel = _display_path(env_dir / _apps.APPS_VALUES, repo_root)
    data, errors = _apps.load_values(env_dir)
    errors = [f"{rel}: {problem}" for problem in errors]
    if errors:
        return errors
    dns_suffix = cluster_vars.get("dns_suffix")
    errors.extend(
        f"{rel}: {problem}"
        for problem in _apps.validate(
            data, dns_suffix if isinstance(dns_suffix, str) else None, _apps.lan_networks(cluster_vars)
        )
    )
    apps = _apps.app_names(data)
    if apps and not app_hosting_listed(env_dir, repo_root):
        errors.append(
            f"{rel}: lists apps, but app hosting is off; list ../../../../{CORE_SOURCE_DIR}/{APP_HOSTING_DIR} "
            "under components in flux/platform-configs/kustomization.yaml, or remove the list"
        )
    if apps and not cluster_vars.get("apps_git_repo_url"):
        errors.append(f"{rel}: lists apps, so values/cluster-vars.yaml sets apps_git_repo_url")
    generated = env_dir / "flux" / _apps.GENERATED_REL
    generated_rel = _display_path(generated, repo_root)
    listing = env_dir / "flux" / "kustomization.yaml"
    doc = yaml.safe_load(listing.read_text(encoding="utf-8")) if listing.is_file() else {}
    listed = _apps.GENERATED_REL in ((doc or {}).get("resources") or [])
    if not apps:
        if generated.is_file() or listed:
            errors.append(
                f"{generated_rel}: the env lists no app; remove it and its line in flux/kustomization.yaml"
            )
        return errors
    if not listed:
        errors.append(f"{_display_path(listing, repo_root)}: lists apps in {rel}, so resources lists {_apps.GENERATED_REL}")
    if errors or pre_host:
        return errors
    if not generated.is_file() or generated.read_text(encoding="utf-8") != _apps.render(
        data, resolve_cluster_vars(cluster_vars)
    ):
        errors.append(f"{generated_rel}: missing or does not match {rel}; re-render with tools/lib/render_apps.py")
    return errors


def _ssh_root(cluster_vars_path: Path) -> bool:
    try:
        data = yaml.safe_load(cluster_vars_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return False
    url = data.get("flux_git_repo_url") if isinstance(data, dict) else None
    return isinstance(url, str) and url.startswith("ssh://")


def _validate_flux_entry_files(
    env_dir: Path, repo_root: Path, env_name: str, bootstrapped: bool
) -> list[str]:
    """Every Flux entry file exists once the env has a flux/ folder or sealed files. A new env
    that has neither yet (prep-node checks it before the copy is edited) needs none."""
    flux = env_dir / "flux"
    if not flux.is_dir() and not bootstrapped:
        return []
    return [
        f"{env_name}: missing required file {_display_path(flux / name, repo_root)}"
        for name in _FLUX_ENTRY_FILES
        if not (flux / name).is_file()
    ]


def _validate_flux_sources(
    env_dir: Path, repo_root: Path, env_name: str, cluster_vars: dict[str, Any]
) -> list[str]:
    """flux/sync.yaml declares the root and core GitRepositories from cluster-vars, the root
    includes core at CORE_SOURCE_DIR, and the root Kustomization applies this env's flux path."""
    path = env_dir / "flux" / SYNC_FILE
    rel = _display_path(path, repo_root)
    if not path.is_file():
        return []  # _validate_flux_entry_files reports it
    try:
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
    except yaml.YAMLError as exc:
        return [f"{rel}: invalid YAML: {exc}"]

    def find(kind: str, name: str) -> dict:
        for doc in docs:
            meta = doc.get("metadata") or {}
            if doc.get("kind") == kind and meta.get("name") == name and meta.get("namespace") == ROOT_SOURCE:
                return doc.get("spec") or {}
        return {}

    root, core, sync = find("GitRepository", ROOT_SOURCE), find("GitRepository", CORE_SOURCE), find("Kustomization", ROOT_SOURCE)
    root_url = cluster_vars.get("flux_git_repo_url")
    want = {
        f"GitRepository {ROOT_SOURCE} spec.url": (root.get("url"), root_url, "flux_git_repo_url"),
        f"GitRepository {ROOT_SOURCE} spec.ref.name": ((root.get("ref") or {}).get("name"), cluster_vars.get("flux_git_ref"), "flux_git_ref"),
        f"GitRepository {CORE_SOURCE} spec.url": (core.get("url"), cluster_vars.get("core_git_repo_url"), "core_git_repo_url"),
        f"GitRepository {CORE_SOURCE} spec.ref.name": ((core.get("ref") or {}).get("name"), cluster_vars.get("core_git_ref"), "core_git_ref"),
    }
    errors = [
        f"{rel}: {field} is {got!r}, expected {expected!r}; set it to {key} in values/cluster-vars.yaml"
        for field, (got, expected, key) in want.items()
        if expected is not None and got != expected
    ]
    include = {"repository": {"name": CORE_SOURCE}, "toPath": CORE_SOURCE_DIR}
    if include not in (root.get("include") or []):
        errors.append(f"{rel}: GitRepository {ROOT_SOURCE} must include {CORE_SOURCE} at toPath {CORE_SOURCE_DIR}")
    if core.get("secretRef"):
        errors.append(f"{rel}: GitRepository {CORE_SOURCE} is public and takes no secretRef")
    if isinstance(root_url, str) and root_url.startswith("ssh://") and not root.get("secretRef"):
        errors.append(f"{rel}: GitRepository {ROOT_SOURCE} reads an ssh:// URL and needs spec.secretRef")
    if sync.get("path") != f"./envs/{env_name}/flux" or (sync.get("sourceRef") or {}).get("name") != ROOT_SOURCE:
        errors.append(f"{rel}: Kustomization {ROOT_SOURCE} must apply ./envs/{env_name}/flux from {ROOT_SOURCE}")
    return errors


def _rendered_cluster_vars_configmap(cluster_vars_path: Path) -> str:
    from render_cluster_vars_configmap import render_document

    return render_document(cluster_vars_path)


def _validate_committed_configmap(
    env_dir: Path,
    repo_root: Path,
    cluster_vars_path: Path,
    *,
    pre_host: bool,
) -> list[str]:
    """The committed ConfigMap must match an in-memory render of cluster-vars."""
    if pre_host:
        return []
    generated = env_dir / "flux" / "generated" / "cluster-vars-configmap.yaml"
    if not generated.is_file():
        return []
    rel = _display_path(generated, repo_root)
    try:
        rendered = _rendered_cluster_vars_configmap(cluster_vars_path)
    except SystemExit as exc:
        detail = str(exc).strip() or "cluster-vars render failed"
        return [f"{rel}: {detail}"]
    if generated.read_text(encoding="utf-8") == rendered:
        return []
    return [
        f"{rel}: does not match values/cluster-vars.yaml; "
        f"re-render with tools/lib/render_cluster_vars_configmap.py"
    ]


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


def _validate_chrony_nts(rel_path: str, value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    return [f"{rel_path}: chrony_nts must be true or false"]


def repo_owner_handle(url: str) -> str | None:
    """Return the first path segment when url is https or ssh and names owner/repo."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("https", "ssh") or not parsed.netloc:
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0]


def _validate_repo_url(
    rel_path: str, key: str, value: Any, *, ssh: bool = False
) -> tuple[list[str], str | None]:
    if ssh and isinstance(value, str) and value.startswith("ssh://"):
        errors = _validate_ssh_url(rel_path, key, value)
    else:
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


def _validate_ssh_url(rel_path: str, key: str, value: str) -> list[str]:
    """ssh://user@host/owner/repo, the form Flux reads with a deploy key."""
    parsed = urlparse(value)
    if not parsed.hostname or parsed.password is not None or "?" in value or "#" in value:
        return [f"{rel_path}: {key} must be ssh://<user>@<host>/<owner>/<repo>"]
    return []


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
    if parsed.username is not None or parsed.password is not None:
        return [f"{rel_path}: {key} must not include userinfo"]
    if "?" in value:
        return [f"{rel_path}: {key} must not include a query"]
    if "#" in value:
        return [f"{rel_path}: {key} must not include a fragment"]
    return []


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
        for key in (
            "ssh_user",
            "k3s_kubelet_kube_reserved",
            "k3s_kubelet_system_reserved",
            "k3s_kubelet_eviction_hard",
        ):
            value = host_vars.get(key)
            if key in host_vars and (not isinstance(value, str) or not value.strip()):
                errors.append(f"{rel_path}: host {hostname}: {key} must be a non-empty string")

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
    if disk is True:
        return []
    return [f"{rel_path}: host {hostname}: data_disk must be true"]


def _has_dot_segment(value: str) -> bool:
    wrapped = f"/{value.strip('/')}/"
    return "/../" in wrapped or "/./" in wrapped


def _validate_local_path_data(rel_path: str, value: Any) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    if not value.startswith("/"):
        return [f"{rel_path}: local_path_data must be an absolute path"]
    path = value.rstrip("/") or "/"
    if path == _DATA_MOUNT or not path.startswith(_DATA_MOUNT + "/") or _has_dot_segment(path):
        return [f"{rel_path}: local_path_data must be a subdirectory of {_DATA_MOUNT}"]
    return []


def _validate_backup_path(rel_path: str, value: Any) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    if not value.startswith("/"):
        return [f"{rel_path}: backup_path must be an absolute path"]
    if _has_dot_segment(value):
        return [f"{rel_path}: backup_path must not contain a '.' or '..' path segment"]
    return []


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    ns = parser.parse_args(argv)
    try:
        validate_one_env(instance_root(Path(".")), ns.env)
    except ValidationError as exc:
        print(exc, flush=True)
        return 1
    return 0
