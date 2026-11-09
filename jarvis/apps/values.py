"""An env's hosted apps: its app list and exception lines, and the wiring core renders from them.

CONTRACT: core/feature/app-hosting/CONTRACT.md. env-schema validates the env file and compares
the committed render; render_apps.py writes it. The render reads only the env (names,
exception lines, the apps repository URL and the CIDRs), never an app's own folder.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from jarvis.env import switches as _switches

# envs/<env>/values/apps.yaml: exception lines and backup selection. The names are in values/switches.yaml.
APPS_VALUES = "values/apps.yaml"
# The render, under envs/<env>/flux/, which the env's flux/kustomization.yaml lists.
GENERATED_REL = "generated/apps.yaml"
# The core directory each app's wiring Kustomization applies.
WIRING_PATH = "./core/feature/app-hosting/app"
# An app name: one DNS label short enough that app-<name> is one too.
APP_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?$")
_FQDN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LAN_PORT = re.compile(r"^([0-9]{1,5})/(TCP|UDP)$")
# A LAN target's name, as an App's network.egress.lan lists it. "all" is reserved: [all] asks
# for the whole LAN, which lan-all grants.
_TARGET = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
# Exception lines: <app>: <grant>. A grant names exactly one item.
FLAG_GRANTS = ("privileged", "root", "host-network", "host-path", "lan-all", "no-uptime-check")
VALUE_GRANTS = ("hostname", "lan-port", "lan-target")
# Grants that open the LAN, so need the env's lan_cidrs.
LAN_GRANTS = ("lan-all", "lan-target")
# values/cluster-vars.yaml: the env's LAN ranges.
LAN_CIDRS = "lan_cidrs"
_PRIVATE = tuple(ipaddress.IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_KEYS = frozenset({"apps", "exceptions", "backups", "versions"})
# values/apps.yaml versions: each listed app's pinned release, <app>: v<major>.<minor>.<patch>.
# Flux reads the app at the apps repository's tag <app>/v<version>; nothing follows a branch.
VERSIONS = "versions"
_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


def wiring_name(app: str) -> str:
    return f"app-{app}-wiring"


def load_values(env_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """The env's app names from its switches, with the exception lines and backup selection from
    its app file, as one mapping, or errors. A missing file holds nothing, so no name is no apps."""
    switches, errors = _switches.load(env_dir)
    if errors:
        return {}, [f"{_switches.SWITCHES_FILE}: {problem}" for problem in errors]
    path = env_dir / APPS_VALUES
    data: Any = None
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            return {}, [f"invalid YAML: {exc}"]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, ["expected a mapping with exceptions and backups"]
    if _switches.APPS in data:
        return {}, [f"apps: app names live in {_switches.SWITCHES_FILE}, not here"]
    if _switches.APPS in switches:
        data[_switches.APPS] = switches[_switches.APPS]
    return data, []


def lan_cidrs_problems(value: Any, pod_cidr: Any, service_cidr: Any) -> list[str]:
    """lan_cidrs is a list of IPv4 ranges, each inside RFC 1918 and clear of the cluster's own
    ranges, so neither lan-all nor a lan-target inside them opens the internet or the cluster."""
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        return [f"{LAN_CIDRS} must be a non-empty list of CIDRs"]
    cluster = []
    for cidr in (pod_cidr, service_cidr):
        try:
            cluster.append(ipaddress.ip_network(str(cidr), strict=True))
        except ValueError:
            pass  # reported as pod_cidr or service_cidr
    errors: list[str] = []
    seen: set[str] = set()
    for item in value:
        try:
            network = ipaddress.ip_network(item, strict=True) if isinstance(item, str) else None
        except ValueError:
            network = None
        if not isinstance(network, ipaddress.IPv4Network):
            errors.append(f"{LAN_CIDRS}: {item!r} is not an IPv4 CIDR")
        elif not any(network.subnet_of(private) for private in _PRIVATE):
            errors.append(f"{LAN_CIDRS}: {item} is outside the private ranges 10/8, 172.16/12 and 192.168/16")
        elif any(network.overlaps(other) for other in cluster):
            errors.append(f"{LAN_CIDRS}: {item} overlaps pod_cidr or service_cidr")
        elif item in seen:
            errors.append(f"{LAN_CIDRS}: {item} is listed twice")
        else:
            seen.add(item)
    return errors


def lan_networks(cluster_vars: dict[str, Any]) -> list[str]:
    value = cluster_vars.get(LAN_CIDRS)
    return [str(item) for item in value] if isinstance(value, list) else []


def validate(data: dict[str, Any], dns_suffix: str | None, lan_cidrs: list[str] | None = None) -> list[str]:
    """The app list holds names only, each once; each exception line names a listed app and
    one known grant, once. A custom hostname stays outside the env's own zone; a LAN grant
    needs the env's lan_cidrs, and a lan-target's range sits inside them."""
    errors = [f"unknown key {key!r} (allowed: exceptions, backups, versions)" for key in sorted(set(data) - _KEYS)]
    apps = data.get("apps")
    if apps is None:
        apps = []
    if not isinstance(apps, list):
        return [*errors, "apps must be a list of app names"]
    seen: set[str] = set()
    for item in apps:
        if not isinstance(item, str) or not APP_NAME.fullmatch(item):
            errors.append(
                f"apps: {item!r} is not an app name (a DNS label of at most 59 characters, so app-<name> fits)"
            )
        elif item in seen:
            errors.append(f"apps: {item} is listed twice")
        else:
            seen.add(item)
    lines = data.get("exceptions")
    if lines is None:
        return errors
    if not isinstance(lines, list):
        return [*errors, "exceptions must be a list of '<app>: <grant>' lines"]
    granted: set[tuple[str, str]] = set()
    for line in lines:
        if not isinstance(line, dict) or len(line) != 1:
            errors.append(f"exceptions: {line!r} is not one '<app>: <grant>' line")
            continue
        ((app, grant),) = line.items()
        where = f"exceptions: {app}: {grant}"
        if not isinstance(app, str) or not APP_NAME.fullmatch(app):
            errors.append(f"{where}: {app!r} is not an app name")
            continue
        if app not in seen:
            errors.append(f"{where}: {app} is not on this env's app list")
        problem = grant_problem(grant, dns_suffix, lan_cidrs or [])
        if problem:
            errors.append(f"{where}: {problem}")
        elif (app, grant) in granted:
            errors.append(f"{where}: listed twice")
        granted.add((app, str(grant)))
    return errors


def grant_problem(grant: Any, dns_suffix: str | None, lan_cidrs: Sequence[str] = ()) -> str | None:
    if not isinstance(grant, str):
        return "a grant is a string"
    kind, _, value = grant.partition(" ")
    if kind in LAN_GRANTS and not lan_cidrs:
        return f"a LAN grant needs the env's LAN ranges: set {LAN_CIDRS} in values/cluster-vars.yaml"
    if grant in FLAG_GRANTS:
        return None
    if kind == "lan-target":
        return _lan_target_problem(value, lan_cidrs)
    if kind == "hostname":
        if not _FQDN.fullmatch(value):
            return "hostname takes one lowercase DNS name"
        if dns_suffix and (value == dns_suffix or value.endswith("." + dns_suffix)):
            return f"a custom hostname is outside {dns_suffix}; apps already get <name>.apps.{dns_suffix}"
        return None
    if kind == "lan-port":
        match = _LAN_PORT.fullmatch(value)
        if not match or not 1 <= int(match.group(1)) <= 65535:
            return "lan-port takes <port>/<TCP|UDP>"
        return None
    known = ", ".join([*FLAG_GRANTS, *(f"{name} <value>" for name in VALUE_GRANTS)])
    return f"unknown grant (known: {known})"


def _lan_target_problem(value: str, lan_cidrs: Sequence[str]) -> str | None:
    parts = value.split(" ")
    usage = "lan-target takes <name> <cidr> <port>/<TCP|UDP>"
    if len(parts) != 3:
        return usage
    name, cidr, port = parts
    if not _TARGET.fullmatch(name) or name == "all":
        return f"{usage}; the name is a DNS label other than all"
    try:
        network = ipaddress.ip_network(cidr, strict=True)
    except ValueError:
        return f"{usage}; {cidr!r} is not a CIDR"
    match = _LAN_PORT.fullmatch(port)
    if not match or not 1 <= int(match.group(1)) <= 65535:
        return usage
    if not any(network.version == 4 and network.subnet_of(lan) for lan in _networks(lan_cidrs)):
        return f"lan-target range {cidr} is outside the env's {LAN_CIDRS}"
    return None


def _networks(cidrs: Sequence[str]) -> list[ipaddress.IPv4Network]:
    """The valid IPv4 ranges among cidrs; lan_cidrs_problems reports the others."""
    found = []
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=True)
        except ValueError:
            continue
        if network.version == 4:
            found.append(network)
    return found


def app_names(data: dict[str, Any]) -> list[str]:
    apps = data.get("apps")
    return [item for item in apps if isinstance(item, str)] if isinstance(apps, list) else []


def grants_for(data: dict[str, Any], app: str) -> list[str]:
    return [
        str(grant)
        for line in data.get("exceptions") or []
        if isinstance(line, dict)
        for name, grant in line.items()
        if name == app
    ]


def version_problems(data: dict[str, Any]) -> list[str]:
    """Every listed app has a pinned version, and no version names an app the env does not list."""
    versions = data.get(VERSIONS)
    if versions is None:
        versions = {}
    if not isinstance(versions, dict):
        return [f"{VERSIONS} must map each listed app to v<major>.<minor>.<patch>"]
    apps = [item for item in data.get("apps") or [] if isinstance(item, str)]
    errors = [f"{VERSIONS}: {app} is listed but has no pinned version (its apps-repository tag {app}/v<version>)"
              for app in apps if app not in versions]
    for app, version in versions.items():
        if app not in apps:
            errors.append(f"{VERSIONS}: {app} is not a listed app")
        elif not isinstance(version, str) or not _VERSION.fullmatch(version):
            errors.append(f"{VERSIONS}: {app}: {version!r} is not v<major>.<minor>.<patch>")
    return errors


def app_ref(data: dict[str, Any], app: str) -> str:
    """The apps-repository ref Flux reads the app at: its pinned tag."""
    return f"refs/tags/{app}/{data[VERSIONS][app]}"


def substitutes(data: dict[str, Any], app: str, cluster_vars: dict[str, Any]) -> dict[str, str]:
    """The inline values one app's wiring reads: its name, its grants, and env values core
    does not publish in cluster-vars."""
    for key in ("pod_cidr", "service_cidr"):
        ipaddress.ip_network(str(cluster_vars[key]), strict=True)
    lan = lan_networks(cluster_vars)
    for cidr in lan:
        ipaddress.ip_network(cidr, strict=True)
    grants = grant_data(data, app, "")
    return {
        "app_name": app,
        "quote": '"',
        **{f"app_grant_{key.replace('-', '_')}": value for key, value in grants.items() if key != "host"},
        "app_pod_cidr": str(cluster_vars["pod_cidr"]),
        "app_service_cidr": str(cluster_vars["service_cidr"]),
        "app_lan_cidrs": ",".join(lan),
        "apps_git_repo_url": str(cluster_vars["apps_git_repo_url"]),
        "app_ref": app_ref(data, app),
    }


def grant_annotations(data: dict[str, Any], app: str, dns_suffix: str) -> dict[str, str]:
    """The annotations the app's wiring writes on its namespace from the env's exception lines."""
    return {f"hosting.jarvis.io/{key}": value for key, value in grant_data(data, app, dns_suffix).items()}


def grant_data(data: dict[str, Any], app: str, dns_suffix: str) -> dict[str, str]:
    """The app's host and the env's exception lines for it, by grant."""
    grants = grants_for(data, app)

    def values(kind: str) -> str:
        return ",".join(sorted(grant.partition(" ")[2] for grant in grants if grant.startswith(kind + " ")))

    return {
        "host": f"{app}.apps.{dns_suffix}",
        "privileged": _flag("privileged" in grants),
        "root": _flag("root" in grants),
        "host-network": _flag("host-network" in grants),
        "host-path": _flag("host-path" in grants),
        "hostnames": values("hostname"),
        "lan-ports": values("lan-port"),
        "lan-all": _flag("lan-all" in grants),
        "lan-targets": values("lan-target"),
        "no-uptime-check": _flag("no-uptime-check" in grants),
    }


def _flag(value: bool) -> str:
    return "true" if value else "false"


def wiring_documents(data: dict[str, Any], cluster_vars: dict[str, Any]) -> list[dict[str, Any]]:
    """One wiring Kustomization per listed app, in list order."""
    return [
        {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": wiring_name(app), "namespace": "flux-system"},
            "spec": {
                "interval": "10m",
                "retryInterval": "1m",
                "path": WIRING_PATH,
                "prune": True,
                "serviceAccountName": "flux-platform",
                "sourceRef": {"kind": "GitRepository", "name": "flux-system"},
                "dependsOn": [{"name": "platform-configs"}],
                "postBuild": {
                    "substitute": substitutes(data, app, cluster_vars),
                    "substituteFrom": [{"kind": "ConfigMap", "name": "cluster-vars"}],
                },
            },
        }
        for app in app_names(data)
    ]


def render(data: dict[str, Any], cluster_vars: dict[str, Any]) -> str:
    header = (
        "# Generated by jarvis render from values/switches.yaml and values/apps.yaml — do not edit.\n"
        "# One trusted wiring per listed app (core/feature/app-hosting/CONTRACT.md).\n"
    )
    docs = wiring_documents(data, cluster_vars)
    return header + yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False, explicit_start=True)
