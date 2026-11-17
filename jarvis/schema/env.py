"""Env validation for envs/<env>/ (CONTRACT: schema/CONTRACT.md).

Each values file's shape is its JSON Schema's (schema/*.schema.json, applied by
jarvis.schema.values). What is here spans fields or files: names against core and the apps
repository, sealed files against what reads them, and the .sops.yaml rules. That the committed
flux/ is the render of the values is jarvis render --check's, which jarvis validate also runs.
"""

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
from jarvis.schema import values as _values
from jarvis.secrets import recipients as _recipients

_DEFAULTS = _values.defaults(_values.CLUSTER_VARS)
DEFAULT_SSH_USER = _DEFAULTS["ssh_user"]
# An env turns the off-host copy and app hosting on by name in values/switches.yaml.
OFFSITE_COPY_DIR = _switches.OFFSITE_COPY_DIR
OFFSITE_SECRET_FILE = _switches.OFFSITE_SECRET_FILE
APP_HOSTING_DIR = _switches.APP_HOSTING_DIR
# The root GitRepository includes the core source at this directory, so env files
# reach core as <root>/core/... . In the product checkout, core is the checkout itself.
CORE_SOURCE_DIR = "core"
# The root source, and the file that declares it with the core source and the root Kustomization.
ROOT_SOURCE = "flux-system"
SYNC_FILE = "sync.yaml"
# The sealed read-only deploy key an ssh:// root reads with.
DEPLOY_KEY_FILE = "flux-deploy-key.sops.yaml"
# Sealed once bootstrap-secrets has run; platform-configs applies the last two.
_BOOTSTRAP_SEALED = ("k3s-token", "cert-manager-ca", "backups-restic")
# Add-ons: one folder each under core's addons/, a kustomize Component the services
# Kustomization lists in spec.components. Slot contracts live under addons/slots/.
ADDONS_DIR = "addons"
SLOTS_DIR = "slots"
# On an add-on's Flux Kustomization: the slot it fills.
SLOT_LABEL = "addons.jarvis.io/slot"
# An add-on's sealed Secrets are under envs/<env>/secrets/<namespace>/.
ADDON_SECRETS_PATH = "./envs/${jarvis_env}/secrets/"
# Each values file and the schema that holds its shape. switches_problems applies switches'.
_FILES = {
    "values/cluster-vars.yaml": _values.CLUSTER_VARS,
    "inventory/hosts.yml": "hosts",
    _apps.APPS_VALUES: "apps",
}

# ENVS="<env> [<env>...]" limits validate_env_tree and the env-level checks to those envs, so an
# instance CI can check each env against the core tag that env pins. Unset, every env.
ENVS_VAR = "ENVS"

_ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SOPS_ENV_SECRETS = re.compile(r"envs/([A-Za-z0-9][A-Za-z0-9._-]*)/secrets/")
_K3S_VERSION = re.compile(r"^v(\d+\.\d+\.\d+)\+k3s\d+$")
_SINGLETON_ROLES = ("control_plane", "backup")


class ValidationError(Exception):
    """One or more env schema violations."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


def env_dirs(root: Path) -> list[Path]:
    """The env folders under root/envs/ the env-level checks act on."""
    base = Path(root) / "envs"
    found = sorted(path for path in base.iterdir() if path.is_dir()) if base.is_dir() else []
    wanted = os.environ.get(ENVS_VAR, "").replace(",", " ").split()
    return [path for path in found if path.name in wanted] if wanted else found


def env_flux_dirs(root: Path) -> list[Path]:
    return [path / "flux" for path in env_dirs(root) if (path / "flux").is_dir()]


def kubernetes_version_from_k3s(value: str) -> str | None:
    """Return major.minor.0 for kubeconform schemas, or None when the pin does not match."""
    match = _K3S_VERSION.fullmatch(value.strip())
    if match is None:
        return None
    major, minor, _patch = match.group(1).split(".")
    return f"{major}.{minor}.0"


def resolve_cluster_vars(data: dict[str, Any]) -> dict[str, Any]:
    """cluster-vars with the schema's defaults for the keys it leaves out."""
    return {**_DEFAULTS, **data}


def validate_env_tree(repo_root: Path | str, *, pre_host: bool = False) -> dict[str, dict[str, Any]]:
    """Validate every env under envs/. Returns resolved cluster-vars per env name.

    pre_host skips what holds only once the env's keys exist: the .sops.yaml rule for each env,
    and that every sealed file opens for exactly the env's recipients.
    """
    root = Path(repo_root).resolve()
    if not (root / "envs").is_dir():
        raise ValidationError(["missing envs directory: envs"])
    selected = env_dirs(root)
    if not selected:
        raise ValidationError(["no env directories under envs"])
    errors: list[str] = []
    resolved: dict[str, dict[str, Any]] = {}
    for env_dir in selected:
        env_errors, env_resolved = _validate_env(env_dir, root, pre_host=pre_host)
        errors.extend(env_errors)
        if env_resolved is not None:
            resolved[env_dir.name] = env_resolved
    errors.extend(_sops_problems(root, [path.name for path in selected], pre_host=pre_host))
    if errors:
        raise ValidationError(errors)
    return resolved


def validate_one_env(repo_root: Path | str, name: str, *, pre_host: bool = False) -> dict[str, Any]:
    """Validate one env and its .sops.yaml creation rules. jarvis site runs this first."""
    if not _ENV_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValidationError([f"invalid env name: {name}"])
    root = Path(repo_root).resolve()
    env_dir = root / "envs" / name
    if not env_dir.is_dir():
        raise ValidationError([f"unknown env: {name}"])
    errors, resolved = _validate_env(env_dir, root, pre_host=pre_host)
    errors.extend(_sops_problems(root, [name], pre_host=pre_host))
    if errors or resolved is None:
        raise ValidationError(errors or [f"{name}: invalid env"])
    return resolved


def _sops_problems(root: Path, checked: list[str], *, pre_host: bool) -> list[str]:
    """One age creation rule per checked env (at most one before its keys exist), and no rule
    for an env folder that does not exist."""
    path = root / ".sops.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except yaml.YAMLError as exc:
        return [f".sops.yaml: invalid YAML: {exc}"]
    rules = (data or {}).get("creation_rules", []) if isinstance(data or {}, dict) else None
    if not isinstance(rules, list):
        return [".sops.yaml: creation_rules must be a list"]
    regexes = [rule["path_regex"] for rule in rules if isinstance(rule, dict) and isinstance(rule.get("path_regex"), str)]
    errors = []
    for name in checked:
        count = sum(f"envs/{name}/secrets/" in regex for regex in regexes)
        if count > 1 or (not pre_host and count != 1):
            errors.append(f".sops.yaml: expected exactly one creation rule for envs/{name}/secrets/, found {count}")
    existing = {p.name for p in (root / "envs").iterdir() if p.is_dir()}
    for name in dict.fromkeys(found for regex in regexes for found in _SOPS_ENV_SECRETS.findall(regex)):
        if name not in existing:
            errors.append(f".sops.yaml: creation rule names envs/{name}/secrets/, but that env folder does not exist; remove the rule")
    return errors


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def _load(path: Path, repo_root: Path, errors: list[str]) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"{_rel(path, repo_root)}: invalid YAML: {exc}")
        return None


def _validate_env(env_dir: Path, repo_root: Path, *, pre_host: bool = False) -> tuple[list[str], dict[str, Any] | None]:
    name = env_dir.name
    secrets = env_dir / "secrets"
    # Once bootstrap-secrets has run, the k3s tokens are sealed too. prep-node checks before that.
    bootstrapped = any(secrets.glob("*.sops.yaml"))
    cluster_path = env_dir / "values" / "cluster-vars.yaml"
    required = [cluster_path, env_dir / "inventory" / "hosts.yml"]
    if bootstrapped:
        required += [secrets / f"{item}.sops.yaml" for item in _BOOTSTRAP_SEALED]
    if cluster_path.is_file() and _ssh_root(cluster_path):
        required.append(secrets / DEPLOY_KEY_FILE)
    if (env_dir / "flux").is_dir() or bootstrapped:
        required.append(env_dir / _switches.SWITCHES_FILE)
    errors = [f"{name}: missing required file {_rel(path, repo_root)}" for path in required if not path.is_file()]
    if errors:
        return errors, None

    docs: dict[str, Any] = {}
    for rel, schema in _FILES.items():
        path = env_dir / rel
        doc = _load(path, repo_root, errors) if path.is_file() else {}
        docs[rel] = {} if doc is None else doc
        errors.extend(_values.problems(schema, docs[rel], _rel(path, repo_root)))
    if errors:
        return errors, None
    cluster_vars = docs["values/cluster-vars.yaml"]
    cluster_rel = _rel(cluster_path, repo_root)

    errors.extend(f"{cluster_rel}: {problem}" for problem in _cluster_vars_problems(cluster_vars, name))
    errors.extend(_hosts_problems(docs["inventory/hosts.yml"], _rel(env_dir / "inventory" / "hosts.yml", repo_root)))
    errors.extend(switches_problems(env_dir, repo_root))
    if app_dns_listed(env_dir) and not cluster_vars.get("app_dns_cluster_ip"):
        errors.append(f"{cluster_rel}: app_dns_cluster_ip is required while {_switches.SWITCHES_FILE} lists {_switches.APP_DNS}")
    errors.extend(_apps_problems(env_dir, repo_root, cluster_vars))
    errors.extend(_sealed_problems(env_dir, repo_root, bootstrapped, cluster_path))
    if not pre_host:
        # Every sealed file opens for exactly the env's recipients: the working and recovery keys.
        errors.extend(_recipients.problems(repo_root, env_dir))
    if errors:
        return errors, None
    return [], resolve_cluster_vars(cluster_vars)


def _cluster_vars_problems(data: dict[str, Any], env_name: str) -> list[str]:
    """The rules that span fields, or need more than a pattern."""
    errors = []
    if data["jarvis_env"] != env_name:
        errors.append(f"jarvis_env must match the env directory name ({env_name})")
    try:
        zoneinfo.ZoneInfo(data["time_zone"].strip())
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        errors.append("time_zone must be an IANA time zone")
    networks = {}
    for key in ("pod_cidr", "service_cidr"):
        try:
            networks[key] = ipaddress.ip_network(data[key].strip(), strict=True)
        except ValueError:
            errors.append(f"{key} must be a CIDR")
    errors.extend(_app_dns_ip_problems(data.get("app_dns_cluster_ip"), networks.get("service_cidr")))
    errors.extend(_apps.lan_cidrs_problems(data.get(_apps.LAN_CIDRS), data["pod_cidr"], data["service_cidr"]))
    errors.extend(_known_hosts_problems(data.get("flux_known_hosts"), data["flux_git_repo_url"]))
    return errors


def _app_dns_ip_problems(value: str | None, network: Any) -> list[str]:
    """A host address inside service_cidr that is neither the Kubernetes API (the first) nor
    cluster DNS (the tenth, 10.43.0.10 for 10.43.0.0/16): CoreDNS forwards the zone to it."""
    if value is None or network is None:
        return []
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return ["app_dns_cluster_ip must be an IP address"]
    if address not in network or address in (network.network_address, network.broadcast_address):
        return ["app_dns_cluster_ip must be a host address inside service_cidr"]
    if address in (network.network_address + 1, network.network_address + 10):
        return ["app_dns_cluster_ip must not be the first or tenth address of service_cidr"]
    return []


def _known_hosts_problems(value: str | None, root_url: str) -> list[str]:
    """An ssh:// root's host is among flux_known_hosts, so Flux trusts the env repository's host.
    Unset, the role's default is GitHub's key, which serves only github.com."""
    host = urlparse(root_url).hostname if root_url.startswith("ssh://") else None
    if host is None:
        return []
    if value is None:
        return [] if host == "github.com" else [f"flux_git_repo_url is on {host}; set flux_known_hosts to its SSH host key"]
    hosts = {name for line in value.strip().splitlines() for name in line.split()[0].split(",")}
    return [] if host in hosts else [f"flux_known_hosts holds no key for {host}, the host of flux_git_repo_url"]


def _hosts_problems(data: dict[str, Any], rel: str) -> list[str]:
    """Across hosts: exactly one control_plane and one backup host, and at least one edge host."""
    roles = [role for host in data["all"]["hosts"].values() for role in host["node_roles"]]
    errors = [
        f"{rel}: expected exactly one {role} host, found {roles.count(role)}"
        for role in _SINGLETON_ROLES
        if roles.count(role) != 1
    ]
    if "edge" not in roles:
        errors.append(f"{rel}: expected at least one edge host")
    return errors


def _ssh_root(cluster_vars_path: Path) -> bool:
    try:
        data = yaml.safe_load(cluster_vars_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return False
    url = data.get("flux_git_repo_url") if isinstance(data, dict) else None
    return isinstance(url, str) and url.startswith("ssh://")


def _feature_listed(env_dir: Path, name: str) -> bool:
    """True when the env's switches list feature name."""
    data, errors = _switches.load(env_dir)
    return not errors and name in _switches.names(data, _switches.FEATURES)


def offsite_copy_listed(env_dir: Path) -> bool:
    return _feature_listed(env_dir, _switches.OFFSITE_COPY)


def apps_server_listed(env_dir: Path) -> bool:
    return _feature_listed(env_dir, _switches.APPS_BACKUP_SERVER)


def app_hosting_listed(env_dir: Path) -> bool:
    return _feature_listed(env_dir, _switches.APP_HOSTING)


def app_dns_listed(env_dir: Path) -> bool:
    return _feature_listed(env_dir, _switches.APP_DNS)


def app_registrations(env_dir: Path, repo_root: Path | None = None) -> list[str]:
    """The app namespaces an env registers a credential for: the backups selection in
    values/apps.yaml. With no selection it registers none."""
    registered = backup_required_registrations(env_dir, Path(repo_root) if repo_root is not None else env_dir.parents[1])
    return sorted(registered or ())


def backup_required_registrations(env_dir: Path, repo_root: Path) -> frozenset[str] | None:
    """App namespaces that must have a backup credential for verify, or None when selection is off."""
    data, _ = _apps.load_values(env_dir)
    section = _backup_selection.load_section(data or {})
    if section is None:
        return None
    apps_list = _apps.app_names(data)
    apps_root = _backup_selection.selection_apps_root(repo_root, env_dir, apps_list)
    return frozenset(_backup_selection.enabled_namespaces(section, apps_list=apps_list, apps_root=apps_root))


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
    """The add-ons an env turns on, by name in its switches."""
    data, errors = _switches.load(env_dir)
    return [] if errors else _switches.names(data, _switches.ADDONS)


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
    return {
        doc["metadata"]["labels"][SLOT_LABEL]
        for doc in docs
        if doc.get("kind") == "Kustomization" and isinstance(((doc.get("metadata") or {}).get("labels") or {}).get(SLOT_LABEL), str)
    }


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
    """Why the env's switches cannot render: a shape its schema refuses, an add-on core does not
    ship, two add-ons for one slot, or an app the apps repository does not hold. A listed app must
    exist; a missing checkout fails, like backup eligibility."""
    rel = _rel(env_dir / _switches.SWITCHES_FILE, repo_root)
    data, errors = _switches.load(env_dir)
    errors = [f"{rel}: {problem}" for problem in errors] or _values.problems("switches", data, rel)
    if errors:
        return errors
    core = _paths.core_root()
    known = addon_names(core)
    by_slot: dict[str, list[str]] = {}
    for name in _switches.names(data, _switches.ADDONS):
        if name not in known:
            errors.append(f"{rel}: {_switches.ADDONS}: unknown name {name!r} (known: {', '.join(known) or 'none'})")
            continue
        for slot in addon_slots(addon_documents(core, name)):
            by_slot.setdefault(slot, []).append(name)
    errors.extend(
        f"{rel}: add-ons {', '.join(names)} all fill slot {slot}; an env enables at most one per slot"
        for slot, names in sorted(by_slot.items())
        if len(names) > 1
    )
    apps = _switches.names(data, _switches.APPS)
    if not apps:
        return errors
    try:
        apps_root = _backup_selection.apps_repo_root(repo_root, env_dir)
    except _backup_selection.AppsRepoError as exc:
        return [*errors, f"{rel}: {exc}"]
    errors.extend(
        f"{rel}: {_switches.APPS}: unknown name {name!r}, no {name}/app.yaml in the apps repository{_backup_selection.STALE_HINT}"
        for name in apps
        if not (apps_root / name / "app.yaml").is_file()
    )
    return errors


def _apps_problems(env_dir: Path, repo_root: Path, cluster_vars: dict[str, Any]) -> list[str]:
    """Each exception line is one known grant for a listed app, each listed app has a pin, and an
    env that lists apps turns app hosting on and names the apps repository."""
    rel = _rel(env_dir / _apps.APPS_VALUES, repo_root)
    switches_rel = _rel(env_dir / _switches.SWITCHES_FILE, repo_root)
    data, errors = _apps.load_values(env_dir)
    if errors:
        return [f"{rel}: {problem}" for problem in errors]
    problems = _apps.validate(data, cluster_vars["dns_suffix"], _apps.lan_networks(cluster_vars))
    errors = [f"{switches_rel if p.startswith(_switches.APPS) else rel}: {p}" for p in problems]
    errors.extend(f"{rel}: {problem}" for problem in _apps.version_problems(data))
    if _apps.app_names(data):
        if not app_hosting_listed(env_dir):
            errors.append(f"{switches_rel}: lists apps, but app hosting is off; list {_switches.APP_HOSTING} under {_switches.FEATURES}, or remove the list")
        if not cluster_vars.get("apps_git_repo_url"):
            errors.append(f"{switches_rel}: lists apps, so values/cluster-vars.yaml sets apps_git_repo_url")
    if _backup_selection.load_section(data) is not None:
        errors.extend(_backup_selection.selection_problems(repo_root, env_dir, rel))
    return errors


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
        if name in known:
            for namespace in addon_secret_namespaces(addon_documents(core, name)):
                read.update((secrets / namespace).glob("*.sops.yaml"))
    try:
        registered = app_registrations(env_dir, repo_root)
    except (_backup_selection.AppsRepoError, TypeError, ValueError, yaml.YAMLError, UnicodeDecodeError):
        # The selection is unreadable; _apps_problems reports why. Every app credential counts as read.
        read.update(secrets.rglob(f"{_app_backups.secret_name('app-')}*.sops.yaml"))
        registered = []
    for namespace in registered:
        read.add(repo_root / _app_backups.sealed_rel(env_dir.name, namespace))
    return read


def _sealed_problems(env_dir: Path, repo_root: Path, bootstrapped: bool, cluster_path: Path) -> list[str]:
    """Once bootstrapped, each thing that reads a sealed file has it: the off-host copy, an
    add-on's namespace folder, a registered app's credential. An app credential is sealed only in
    its own folder and needs the apps server. A sealed file nothing reads is a credential with no
    owner, so it fails."""
    name = env_dir.name
    secrets = env_dir / "secrets"
    errors: list[str] = []
    # A selection that cannot say which apps are enabled is _apps_problems' to report.
    unreadable = _backup_selection.selection_problems(repo_root, env_dir, _apps.APPS_VALUES)
    registered = [] if unreadable else app_registrations(env_dir, repo_root)
    if registered and not apps_server_listed(env_dir):
        errors.append(
            f"{_rel(env_dir / _switches.SWITCHES_FILE, repo_root)}: registers app credentials, and does not list "
            f"{_switches.APPS_BACKUP_SERVER}; an app's backups need the apps server"
        )
    if bootstrapped:
        wanted = [(secrets / OFFSITE_SECRET_FILE, f"{_switches.SWITCHES_FILE} lists {_switches.OFFSITE_COPY}", "")] if offsite_copy_listed(env_dir) else []
        wanted += [
            (repo_root / _app_backups.sealed_rel(name, item), f"registers {item}", f"; run jarvis secrets --app {item}")
            for item in registered
        ]
        errors.extend(f"{name}: {why}, missing required file {_rel(path, repo_root)}{hint}" for path, why, hint in wanted if not path.is_file())
        core = _paths.core_root()
        for addon in set(enabled_addons(env_dir)) & set(addon_names(core)):
            for namespace in addon_secret_namespaces(addon_documents(core, addon)):
                if not any((secrets / namespace).glob("*.sops.yaml")):
                    errors.append(
                        f"{name}: add-on {addon} decrypts {_rel(secrets / namespace, repo_root)}, "
                        f"which holds no sealed file; seal one with jarvis encrypt --namespace {namespace}"
                    )
    for path in sorted(secrets.rglob(f"{_app_backups.secret_name('app-')}*.sops.yaml")):
        namespace = path.name.removeprefix(_app_backups.secret_name("")).removesuffix(".sops.yaml")
        if path.parent != secrets / namespace:
            errors.append(f"{_rel(path, repo_root)}: an app credential is sealed only in its own folder, {_rel(secrets / namespace, repo_root)}/")
    if errors or not secrets.is_dir():
        return errors
    read = {path.resolve() for path in sealed_readers(env_dir, repo_root, cluster_path)}
    return [
        f"{name}: {_rel(path, repo_root)} is sealed but nothing reads it; delete it, or enable what uses it"
        for path in sorted(secrets.rglob("*.sops.yaml"))
        if path.resolve() not in read
    ]


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
