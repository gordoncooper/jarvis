"""Env schema validation for envs/<env>/ (CONTRACT: schema/CONTRACT.md)."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import zoneinfo
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from jarvis import paths as _paths
from jarvis.apps import values as _apps
from jarvis.backups import app_credentials as _app_backups
from jarvis.backups import selection as _backup_selection
from jarvis.env import switches as _switches
from jarvis.env.runtime import instance_root  # noqa: F401  (the one INSTANCE resolver)
from jarvis.paths import PRODUCT_ROOT
from jarvis.schema import values as _values
from jarvis.secrets import age as _age
from jarvis.secrets import recipients as _recipients

_PRODUCT_ROOT = PRODUCT_ROOT

DEFAULT_SSH_USER = "ubuntu"
# Weekly repository check, hours away from midnight-to-01:00 backups and the etcd copy's prunes after each k3s snapshot.
DEFAULT_BACKUP_CHECK_SCHEDULE = "30 6 * * 3"
# The share of each node's memory no pod is scheduled into (the k3s role's eviction-hard).
DEFAULT_NODE_MEMORY_RESERVE_PERCENT = 10
# An env turns the off-host copy and app hosting on by name in values/switches.yaml; the render
# lists these in its platform-configs.
OFFSITE_COPY_DIR = _switches.OFFSITE_COPY_DIR
OFFSITE_SECRET_FILE = _switches.OFFSITE_SECRET_FILE
APP_HOSTING_DIR = _switches.APP_HOSTING_DIR
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
    "kustomization.yaml",
    "kustomizations/services.yaml",
    "platform-configs/kustomization.yaml",
)
# Sealed once bootstrap-secrets has run; platform-configs applies the last two.
_BOOTSTRAP_SEALED = ("k3s-token", "cert-manager-ca", "backups-restic")
# Add-ons: one folder each under <core>/services/, a kustomize Component the services
# Kustomization lists in spec.components. Slot contracts live under addons/slots/.
ADDONS_DIR = "addons"
SLOTS_DIR = "slots"
SERVICES_KUSTOMIZATION = _switches.SERVICES_REL
# On an add-on's Flux Kustomization: the slot it fills.
SLOT_LABEL = "addons.jarvis.io/slot"
# An add-on's sealed Secrets are under envs/<env>/secrets/<namespace>/.
ADDON_SECRETS_PATH = "./envs/${jarvis_env}/secrets/"


# ENVS="<env> [<env>...]" limits validate_env_tree and the env-level checks to those envs, so an
# instance CI can check each env against the core tag that env pins. Unset, every env.
ENVS_VAR = "ENVS"


def env_dirs(root: Path) -> list[Path]:
    """The env folders under root/envs/ the env-level checks act on."""
    base = Path(root) / "envs"
    found = sorted(path for path in base.iterdir() if path.is_dir()) if base.is_dir() else []
    wanted = os.environ.get(ENVS_VAR, "").replace(",", " ").split()
    return [path for path in found if path.name in wanted] if wanted else found


def env_flux_dirs(root: Path) -> list[Path]:
    return [path / "flux" for path in env_dirs(root) if (path / "flux").is_dir()]


_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
_HOSTNAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SOPS_ENV_SECRETS = re.compile(r"envs/([A-Za-z0-9][A-Za-z0-9._-]*)/secrets/")

# The keys, their types and simple patterns are schema/cluster-vars.schema.json's.
_CLUSTER_VARS_REQUIRED = _values.required_keys(_values.CLUSTER_VARS)
# Optional keys core sets a default for (infra/ansible/group_vars/all.yml).

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
        resolved["age_key_file"] = _age.DEFAULT_KEY_FILE
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
    selected = env_dirs(root)
    if not selected:
        errors.append(f"no env directories under {_display_path(envs_dir, root)}")
        raise ValidationError(errors)

    env_names = [path.name for path in sorted(envs_dir.iterdir()) if path.is_dir()]
    for env_dir in selected:
        env_errors, env_resolved = _validate_env(env_dir, root, pre_host=pre_host)
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
    """Validate one env and its .sops.yaml creation rules. jarvis site runs this first."""
    if not _ENV_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValidationError([f"invalid env name: {name}"])
    root = Path(repo_root).resolve()
    env_dir = root / "envs" / name
    if not env_dir.is_dir():
        raise ValidationError([f"unknown env: {name}"])
    errors, resolved = _validate_env(env_dir, root, pre_host=pre_host)
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
    """One age creation rule per env."""

    def keyed(env_name: str) -> list[dict[str, Any]]:
        needle = f"envs/{env_name}/secrets/"
        return [
            rule
            for rule in rules
            if isinstance(rule.get("path_regex"), str) and needle in rule["path_regex"]
        ]

    errors: list[str] = []
    for env_name in env_names:
        count = len(keyed(env_name))
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
) -> tuple[list[str], dict[str, Any] | None]:
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
        return errors, None

    cluster_vars = _load_yaml(cluster_vars_path, errors, repo_root)
    hosts = _load_yaml(inventory, errors, repo_root)
    if errors or cluster_vars is None or hosts is None:
        return errors, None

    errors.extend(_validate_cluster_vars(cluster_vars, _display_path(cluster_vars_path, repo_root), env_name))
    errors.extend(_validate_hosts(hosts, _display_path(inventory, repo_root), env_name))
    errors.extend(_validate_flux_env_paths(env_dir, repo_root, env_name))
    errors.extend(_validate_switches(env_dir, repo_root, env_name, bootstrapped))
    errors.extend(_validate_offsite_secret(env_dir, repo_root, env_name))
    errors.extend(_validate_app_backups(env_dir, repo_root, env_name, bootstrapped))
    errors.extend(_validate_addons(env_dir, repo_root, env_name, bootstrapped))
    errors.extend(_validate_orphan_sealed(env_dir, repo_root, env_name, cluster_vars_path))
    if not pre_host:
        # Every sealed file opens for exactly the env's recipients: the working and recovery keys.
        errors.extend(_recipients.problems(repo_root, env_dir))
    errors.extend(_validate_apps(env_dir, repo_root, cluster_vars, pre_host=pre_host))
    errors.extend(_validate_flux_entry_files(env_dir, repo_root, env_name, bootstrapped))
    errors.extend(_validate_flux_sources(env_dir, repo_root, env_name, cluster_vars))
    errors.extend(
        _validate_committed_configmap(
            env_dir, repo_root, cluster_vars_path, pre_host=pre_host
        )
    )

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


def _keys_set(data: dict[str, Any]) -> set[str]:
    """Keys with a value. A present null is unset."""
    return {key for key, value in data.items() if value is not None}


def _validate_cluster_vars(data: dict[str, Any], rel_path: str, env_name: str) -> list[str]:
    # Shape (keys, types, simple patterns) is the schema's; what follows needs more than one field.
    errors: list[str] = _values.problems(_values.CLUSTER_VARS, data, rel_path)

    for key in ("pod_cidr", "service_cidr"):
        errors.extend(_validate_cidr(rel_path, key, data.get(key)))
    errors.extend(_validate_app_dns_cluster_ip(rel_path, data))
    errors.extend(
        f"{rel_path}: {problem}"
        for problem in _apps.lan_cidrs_problems(data.get(_apps.LAN_CIDRS), data.get("pod_cidr"), data.get("service_cidr"))
    )

    # Each repository may be anyone's: an env can read the example apps, or another team's apps.
    errors.extend(_validate_repo_url(rel_path, "flux_git_repo_url", data.get("flux_git_repo_url"), ssh=True))
    errors.extend(_validate_known_hosts(rel_path, data.get("flux_known_hosts"), data.get("flux_git_repo_url")))
    errors.extend(_validate_repo_url(rel_path, "core_git_repo_url", data.get("core_git_repo_url")))
    errors.extend(_validate_repo_url(rel_path, "apps_git_repo_url", data.get("apps_git_repo_url")))

    jarvis_env = data.get("jarvis_env")
    if isinstance(jarvis_env, str) and jarvis_env and jarvis_env != env_name:
        errors.append(
            f"{rel_path}: jarvis_env must match the env directory name ({env_name})"
        )

    time_zone = data.get("time_zone")
    if isinstance(time_zone, str) and time_zone.strip():
        errors.extend(_validate_time_zone(rel_path, time_zone.strip()))

    dns_suffix = data.get("dns_suffix")
    if isinstance(dns_suffix, str) and dns_suffix.strip():
        errors.extend(_validate_dns_suffix(rel_path, dns_suffix.strip()))

    errors.extend(_validate_local_path_data(rel_path, data.get("local_path_data")))
    errors.extend(_validate_backup_path(rel_path, data.get("backup_path")))

    errors.extend(_validate_ntp_pools(rel_path, data.get("ntp_pools")))
    errors.extend(_validate_chrony_nts(rel_path, data.get("chrony_nts")))

    return errors


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


def _feature_listed(env_dir: Path, name: str) -> bool:
    """True when the env's switches list feature name: the decision its platform-configs render
    follows (env-schema fails a render that does not)."""
    data, errors = _switches.load(env_dir)
    return not errors and name in _switches.names(data, _switches.FEATURES)


def offsite_copy_listed(env_dir: Path) -> bool:
    """True when the env lists the off-host copy."""
    return _feature_listed(env_dir, _switches.OFFSITE_COPY)


def apps_server_listed(env_dir: Path) -> bool:
    """True when the env lists the apps rest-server."""
    return _feature_listed(env_dir, _switches.APPS_BACKUP_SERVER)


def _app_hosting_component(env_dir: Path, repo_root: Path, item: object) -> bool:
    path, _doc = _platform_configs(env_dir)
    target = (Path(repo_root) / APP_HOSTING_DIR).resolve()
    return isinstance(item, str) and (path.parent / item.strip()).resolve() == target


def app_hosting_listed(env_dir: Path) -> bool:
    """True when the env turns app hosting on: its switches list it under optional."""
    data, _ = _switches.load(env_dir)
    return _switches.APP_HOSTING in _switches.names(data, _switches.FEATURES)


def app_registrations(env_dir: Path, repo_root: Path | None = None) -> list[str]:
    """The app namespaces an env registers a credential for: the backups selection in
    values/apps.yaml. With no selection it registers none."""
    data, _ = _apps.load_values(env_dir)
    section = _backup_selection.load_section(data or {})
    if section is None:
        return []
    root = Path(repo_root) if repo_root is not None else env_dir.parents[1]
    apps_list = _apps.app_names(data)
    apps_root = _backup_selection.selection_apps_root(root, env_dir, apps_list)
    return _backup_selection.enabled_namespaces(section, apps_list=apps_list, apps_root=apps_root)


def backup_required_registrations(env_dir: Path, repo_root: Path) -> frozenset[str] | None:
    """App namespaces that must have a backup credential for verify, or None when selection is off."""
    data, _ = _apps.load_values(env_dir)
    section = _backup_selection.load_section(data or {})
    if section is None:
        return None
    apps_list = _apps.app_names(data)
    apps_root = _backup_selection.selection_apps_root(repo_root, env_dir, apps_list)
    return frozenset(_backup_selection.enabled_namespaces(section, apps_list=apps_list, apps_root=apps_root))


def _validate_app_backups(env_dir: Path, repo_root: Path, env_name: str, bootstrapped: bool) -> list[str]:
    """Each registered app credential is an app- namespace's, in its exact registration Component,
    with the apps server on and, once bootstrapped, its sealed file present. An app credential is
    sealed only in its own namespace's folder. With backups selection, registrations must match
    the enabled set exactly."""
    rel = _display_path(env_dir / "flux" / "platform-configs" / "kustomization.yaml", repo_root)
    errors: list[str] = []
    data, _ = _apps.load_values(env_dir)
    section = _backup_selection.load_section(data or {})
    if section is not None and _backup_selection.selection_problems(repo_root, env_dir, _apps.APPS_VALUES):
        # _validate_apps reports these; without a valid selection there is nothing to compare.
        return errors
    registered = app_registrations(env_dir, repo_root)
    if section is not None:
        expected = set(registered)
        prefix = f"{_app_backups.REGISTRATION_DIR}/"
        _path, doc = _platform_configs(env_dir)
        components = doc.get("components") or []
        wrapper = _backup_selection.WRAPPER_ENTRY
        manual: set[str] = set()
        for item in components:
            if item == wrapper or _app_hosting_component(env_dir, repo_root, item):
                continue
            if isinstance(item, str) and item.strip().removeprefix("./").startswith(prefix):
                manual.add(item.strip().removeprefix("./").removeprefix(prefix))
            else:
                errors.append(
                    f"{rel}: component {item!r} is not an app credential registration "
                    f"or {wrapper!r}; with backups in values/apps.yaml nothing else belongs there"
                )
        if manual:
            errors.append(
                f"{rel}: lists {sorted(manual)!r} under components; with backups in values/apps.yaml, "
                f"run jarvis render instead of hand-copying registrations"
            )
        if expected and wrapper not in components:
            errors.append(
                f"{rel}: backup-enabled apps need components entry {wrapper!r} from render_backup_selection.py"
            )
        if not expected and wrapper in components:
            errors.append(f"{rel}: lists {wrapper!r} but no app is backup-enabled; re-render backup selection")
    for item in registered:
        component = repo_root / _app_backups.registration_rel(env_name, item)
        if not component.is_file() or component.read_text(encoding="utf-8") != _app_backups.registration_component(item):
            errors.append(
                f"{env_name}: {_display_path(component, repo_root)} is missing or differs from "
                f"the registration jarvis secrets --app {item} writes"
            )
        sealed = repo_root / _app_backups.sealed_rel(env_name, item)
        if bootstrapped and not sealed.is_file():
            errors.append(
                f"{env_name}: {rel} registers {item}, missing required file "
                f"{_display_path(sealed, repo_root)}; run jarvis secrets --app {item}"
            )
    if registered and not apps_server_listed(env_dir):
        errors.append(
            f"{rel}: registers app credentials, and {_switches.SWITCHES_FILE} does not list "
            f"{_switches.APPS_BACKUP_SERVER}; an app's backups need the apps server"
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
    if not any(secrets.glob("*.sops.yaml")) or not offsite_copy_listed(env_dir):
        return []
    secret = secrets / OFFSITE_SECRET_FILE
    if secret.is_file():
        return []
    return [
        f"{env_name}: {_switches.SWITCHES_FILE} lists {_switches.OFFSITE_COPY}, "
        f"missing required file {_display_path(secret, repo_root)}"
    ]


def addon_names(core: Path) -> tuple[str, ...]:
    """Every add-on core ships: a folder under addons/ holding a kustomization file."""
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
    """Every slot core defines: a folder under addons/slots/."""
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


def switches_problems(env_dir: Path, repo_root: Path) -> list[str]:
    """Why the env's switches cannot render: a section that is not a list of names, or a name core
    does not ship or the apps repository does not hold. A new app deploys only once listed, and a
    listed app must exist; a missing checkout fails, like backup eligibility."""
    rel = _display_path(env_dir / _switches.SWITCHES_FILE, repo_root)
    data, errors = _switches.load(env_dir)
    if errors:
        return [f"{rel}: {problem}" for problem in errors]
    core = _paths.core_root()
    errors = [
        f"{rel}: {problem}"
        for problem in _switches.validate(data, addon_names(core))
    ]
    errors.extend(_slot_conflicts(rel, _switches.names(data, _switches.ADDONS), core))
    apps = [name for name in _switches.names(data, _switches.APPS) if _apps.APP_NAME.fullmatch(name)]
    if not apps:
        return errors
    try:
        apps_root = _backup_selection.apps_repo_root(repo_root, env_dir)
    except _backup_selection.AppsRepoError as exc:
        return [*errors, f"{rel}: {exc}"]
    errors.extend(
        f"{rel}: {_switches.APPS}: unknown name {name!r}, no {name}/app.yaml in the apps repository"
        f"{_backup_selection.STALE_HINT}"
        for name in dict.fromkeys(apps)
        if not (apps_root / name / "app.yaml").is_file()
    )
    return errors


def _validate_switches(env_dir: Path, repo_root: Path, env_name: str, bootstrapped: bool) -> list[str]:
    """An env with a flux tree or sealed files has a switches file with valid names, and commits
    the wiring exactly as render_switches.py writes it from them."""
    if not (env_dir / _switches.SWITCHES_FILE).is_file():
        if not (env_dir / "flux").is_dir() and not bootstrapped:
            return []
        return [f"{env_name}: missing required file {_display_path(env_dir / _switches.SWITCHES_FILE, repo_root)}"]
    errors = switches_problems(env_dir, repo_root)
    if _switches.APP_DNS in _switches.names(_switches.load(env_dir)[0], _switches.FEATURES):
        cluster_vars_path = env_dir / "values" / "cluster-vars.yaml"
        try:
            cluster_vars = yaml.safe_load(cluster_vars_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            cluster_vars = {}  # the cluster-vars validator reports it
        if isinstance(cluster_vars, dict) and not cluster_vars.get("app_dns_cluster_ip"):
            errors.append(
                f"{_display_path(cluster_vars_path, repo_root)}: app_dns_cluster_ip is required while "
                f"{_switches.SWITCHES_FILE} lists {_switches.APP_DNS}"
            )
    rel = _display_path(env_dir / _apps.APPS_VALUES, repo_root)
    flux = env_dir / "flux"
    # The other validators report a bad selection; without a valid one the wiring has no expected form.
    if errors or not flux.is_dir() or _apps.load_values(env_dir)[1] or _backup_selection.selection_problems(
        repo_root, env_dir, rel
    ):
        return errors
    data, _ = _switches.load(env_dir)
    wrapper = (_backup_selection.WRAPPER_ENTRY,) if backup_required_registrations(env_dir, repo_root) else ()
    for name, expected in (
        (_switches.ROOT_REL, _switches.root_kustomization(data, apps_generated=_apps.GENERATED_REL)),
        (_switches.SERVICES_REL, _switches.services(data)),
        (_switches.PLATFORM_CONFIGS_REL, _switches.platform_configs(data, extra_components=wrapper)),
    ):
        path = flux / name
        if path.is_file() and path.read_text(encoding="utf-8") != expected:
            errors.append(
                f"{_display_path(path, repo_root)}: does not match {_switches.SWITCHES_FILE}; "
                f"re-render with jarvis render --env {env_name}"
            )
    return errors


def _slot_conflicts(rel: str, listed: list[str], core: Path) -> list[str]:
    """Two listed add-ons that fill one slot; an env enables at most one per slot."""
    known = addon_names(core)
    by_slot: dict[str, list[str]] = {}
    for name in dict.fromkeys(item for item in listed if isinstance(item, str) and item in known):
        for slot in sorted(addon_slots(addon_documents(core, name))):
            by_slot.setdefault(slot, []).append(name)
    return [
        f"{rel}: add-ons {', '.join(names)} all fill slot {slot}; an env enables at most one per slot"
        for slot, names in sorted(by_slot.items())
        if len(names) > 1
    ]


def sealed_readers(env_dir: Path, repo_root: Path, cluster_vars_path: Path) -> set[Path]:
    """Every sealed file something reads: the bootstrap secrets, the deploy key of an ssh://
    root, the off-host target when the copy is listed, the files in each folder an enabled
    add-on decrypts, and each registered app credential."""
    secrets = env_dir / "secrets"
    read = {secrets / f"{name}.sops.yaml" for name in _BOOTSTRAP_SEALED}
    if _ssh_root(cluster_vars_path):
        read.add(secrets / DEPLOY_KEY_FILE)
    if offsite_copy_listed(env_dir):
        read.add(secrets / OFFSITE_SECRET_FILE)
    core = _paths.core_root()
    known = addon_names(core)
    for name in enabled_addons(env_dir):
        if isinstance(name, str) and name in known:
            for namespace in addon_secret_namespaces(addon_documents(core, name)):
                read.update((secrets / namespace).glob("*.sops.yaml"))
    try:
        registered = app_registrations(env_dir, repo_root)
    except (_backup_selection.AppsRepoError, TypeError, ValueError, yaml.YAMLError, UnicodeDecodeError):
        # The selection is unreadable; _validate_apps reports why. Every app credential counts as read.
        read.update(secrets.rglob(f"{_app_backups.secret_name('app-')}*.sops.yaml"))
        registered = []
    for namespace in registered:
        read.add(repo_root / _app_backups.sealed_rel(env_dir.name, namespace))
    return read


def _validate_orphan_sealed(env_dir: Path, repo_root: Path, env_name: str, cluster_vars_path: Path) -> list[str]:
    """A sealed file nothing reads fails: a left-over secret is a credential with no owner."""
    secrets = env_dir / "secrets"
    if not secrets.is_dir():
        return []
    read = {path.resolve() for path in sealed_readers(env_dir, repo_root, cluster_vars_path)}
    return [
        f"{env_name}: {_display_path(path, repo_root)} is sealed but nothing reads it; delete it, "
        "or enable what uses it"
        for path in sorted(secrets.rglob("*.sops.yaml"))
        if path.resolve() not in read
    ]


def _validate_addons(env_dir: Path, repo_root: Path, env_name: str, bootstrapped: bool) -> list[str]:
    """Each listed add-on exists in core, once; at most one per slot; its sealed Secrets exist once
    the env is bootstrapped. Like the off-host copy, a new env before bootstrap needs no secret."""
    rel = _display_path(env_dir / "flux" / SERVICES_KUSTOMIZATION, repo_root)
    listed = enabled_addons(env_dir)
    core = _paths.core_root()
    known = addon_names(core)
    errors: list[str] = []
    seen: set[str] = set()
    for name in listed:
        if not isinstance(name, str) or name not in known:
            errors.append(f"{rel}: unknown add-on {name!r} in spec.components (known: {', '.join(known) or 'none'})")
            continue
        if name in seen:
            errors.append(f"{rel}: add-on {name} is listed twice")
            continue
        seen.add(name)
        docs = addon_documents(core, name)
        if not bootstrapped:
            continue
        for namespace in addon_secret_namespaces(docs):
            folder = env_dir / "secrets" / namespace
            if not any(folder.glob("*.sops.yaml")):
                errors.append(
                    f"{env_name}: add-on {name} decrypts {_display_path(folder, repo_root)}, "
                    "which holds no sealed file; seal one with jarvis encrypt --namespace " + namespace
                )
    errors.extend(_slot_conflicts(rel, listed, core))
    return errors


def _validate_apps(
    env_dir: Path, repo_root: Path, cluster_vars: dict[str, Any], *, pre_host: bool
) -> list[str]:
    """values/apps.yaml lists app names once each and valid exception lines; an env that lists
    an app sets apps_git_repo_url, and its flux tree lists the committed render, which matches.
    An env with no app has neither the render nor its line."""
    rel = _display_path(env_dir / _apps.APPS_VALUES, repo_root)
    switches_rel = _display_path(env_dir / _switches.SWITCHES_FILE, repo_root)
    data, errors = _apps.load_values(env_dir)
    errors = [f"{rel}: {problem}" for problem in errors]
    if errors:
        return errors
    dns_suffix = cluster_vars.get("dns_suffix")
    errors.extend(
        f"{switches_rel if problem.startswith(_switches.APPS) else rel}: {problem}"
        for problem in _apps.validate(
            data, dns_suffix if isinstance(dns_suffix, str) else None, _apps.lan_networks(cluster_vars)
        )
    )
    errors.extend(f"{rel}: {problem}" for problem in _apps.version_problems(data))
    apps = _apps.app_names(data)
    if apps and not app_hosting_listed(env_dir):
        errors.append(
            f"{switches_rel}: lists apps, but app hosting is off; list {_switches.APP_HOSTING} "
            f"under {_switches.FEATURES}, or remove the list"
        )
    if apps and not cluster_vars.get("apps_git_repo_url"):
        errors.append(f"{switches_rel}: lists apps, so values/cluster-vars.yaml sets apps_git_repo_url")
    generated = env_dir / "flux" / _apps.GENERATED_REL
    generated_rel = _display_path(generated, repo_root)
    listing = env_dir / "flux" / "kustomization.yaml"
    doc = yaml.safe_load(listing.read_text(encoding="utf-8")) if listing.is_file() else {}
    listed = _apps.GENERATED_REL in ((doc or {}).get("resources") or [])
    errors.extend(_validate_backup_selection_render(env_dir, repo_root, env_dir.name, data, pre_host=pre_host))
    if not apps:
        if generated.is_file() or listed:
            errors.append(
                f"{generated_rel}: the env lists no app; remove it and its line in flux/kustomization.yaml"
            )
        return errors
    if not listed:
        errors.append(f"{_display_path(listing, repo_root)}: lists apps in {switches_rel}, so resources lists {_apps.GENERATED_REL}")
    if errors or pre_host:
        return errors
    if not generated.is_file() or generated.read_text(encoding="utf-8") != _apps.render(
        data, resolve_cluster_vars(cluster_vars)
    ):
        errors.append(f"{generated_rel}: missing or does not match {rel}; re-render with jarvis render")
    return errors


def _validate_backup_selection_render(
    env_dir: Path,
    repo_root: Path,
    env_name: str,
    data: dict[str, Any],
    *,
    pre_host: bool,
) -> list[str]:
    section = _backup_selection.load_section(data)
    rel = _display_path(env_dir / _apps.APPS_VALUES, repo_root)
    if section is None:
        if (env_dir / _backup_selection.GENERATED_KUSTOMIZATION).is_file():
            return [f"{rel}: {_backup_selection.SECTION_REMOVED}"]
        return []
    apps_list = _apps.app_names(data)
    errors = _backup_selection.selection_problems(repo_root, env_dir, rel)
    if errors or pre_host:
        return errors
    apps_root = _backup_selection.selection_apps_root(repo_root, env_dir, apps_list)
    generated = env_dir / _backup_selection.GENERATED_KUSTOMIZATION
    generated_rel = _display_path(generated, repo_root)
    enabled = _backup_selection.enabled_namespaces(section, apps_list=apps_list, apps_root=apps_root)
    expected = _backup_selection.render_wrapper_component(enabled, section.get("overrides"))
    if expected is None:
        if generated.is_file():
            errors.append(f"{generated_rel}: present but no app is backup-enabled; re-render backup selection")
        return errors
    for namespace in enabled:
        component = repo_root / _app_backups.registration_rel(env_name, namespace)
        if not component.is_file() or component.read_text(encoding="utf-8") != _app_backups.registration_component(
            namespace
        ):
            errors.append(
                f"{env_name}: {_display_path(component, repo_root)} is missing or differs; "
                "re-render with jarvis render"
            )
    expected_components = [
        f"../../platform-configs/{_app_backups.REGISTRATION_DIR}/{namespace}" for namespace in enabled
    ]
    if generated.is_file():
        rendered = yaml.safe_load(generated.read_text(encoding="utf-8")) or {}
        rendered_components = rendered.get("components") if isinstance(rendered, dict) else None
        if rendered_components != expected_components:
            errors.append(
                f"{generated_rel}: stale wrapper lists {rendered_components!r}, expected {expected_components!r}; "
                "re-render with jarvis render"
            )
            return errors
    if not generated.is_file() or generated.read_text(encoding="utf-8") != expected:
        errors.append(
            f"{generated_rel}: missing or does not match {rel}; re-render with jarvis render"
        )
        return errors
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
    """flux/sync.yaml is exactly what render_switches renders from cluster-vars: the root and core
    GitRepositories (the root including core at core/), and the root Kustomization applying this
    env's flux path with jarvis_env."""
    path = env_dir / "flux" / SYNC_FILE
    rel = _display_path(path, repo_root)
    if not path.is_file():
        return []  # _validate_flux_entry_files reports it
    try:
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(doc, dict)]
    except yaml.YAMLError as exc:
        return [f"{rel}: invalid YAML: {exc}"]

    del docs  # parsed only to report invalid YAML
    if path.read_text(encoding="utf-8") != _switches.sync(cluster_vars, env_name):
        return [f"{rel}: does not match values/cluster-vars.yaml; re-render with jarvis render --env {env_name}"]
    return []


def _rendered_cluster_vars_configmap(cluster_vars_path: Path) -> str:
    from jarvis.render.cluster_vars import render_document

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
        f"re-render with jarvis render"
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


_KNOWN_HOSTS_LINE = re.compile(r"^(\S+) (ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa) [A-Za-z0-9+/]+=*$")


def _validate_known_hosts(rel_path: str, value: Any, root_url: Any) -> list[str]:
    """flux_known_hosts, when set, is one or more '<host> <key type> <base64 key>' lines, and an
    ssh:// root's host is among them, so Flux trusts the env repository's host and only it.
    Unset, the role's default is GitHub's key, which serves only an ssh:// root on github.com."""
    if value is None:
        host = urlparse(root_url).hostname if isinstance(root_url, str) and root_url.startswith("ssh://") else None
        if host and host != "github.com":
            return [f"{rel_path}: flux_git_repo_url is on {host}; set flux_known_hosts to its SSH host key"]
        return []
    if not isinstance(value, str) or not value.strip():
        return [f"{rel_path}: flux_known_hosts must be a non-empty string"]
    hosts = set()
    for line in value.strip().splitlines():
        match = _KNOWN_HOSTS_LINE.match(line.strip())
        if match is None:
            return [f"{rel_path}: flux_known_hosts lines must be '<host> <key type> <base64 key>'"]
        hosts.update(match.group(1).split(","))
    if isinstance(root_url, str) and root_url.startswith("ssh://"):
        host = urlparse(root_url).hostname
        if host and host not in hosts:
            return [f"{rel_path}: flux_known_hosts holds no key for {host}, the host of flux_git_repo_url"]
    return []


def _validate_repo_url(rel_path: str, key: str, value: Any, *, ssh: bool = False) -> list[str]:
    if ssh and isinstance(value, str) and value.startswith("ssh://"):
        errors = _validate_ssh_url(rel_path, key, value)
    else:
        errors = _validate_https_url(rel_path, key, value)
    if not errors and isinstance(value, str) and value.strip() and repo_owner_handle(value) is None:
        errors.append(f"{rel_path}: {key} must include an owner and a repo")
    return errors


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
