"""An env's hosted apps: its app list and exception lines, and the wiring core renders from them.

CONTRACT: platform/app-hosting/CONTRACT.md. env-schema validates the env file and compares
the committed render; render_apps.py writes it. The render reads only the env (names,
exception lines, the apps repository URL and the CIDRs), never an app's own folder.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any

import yaml

# envs/<env>/values/apps.yaml. Absent or empty means no apps.
APPS_VALUES = "values/apps.yaml"
# The render, under envs/<env>/flux/, which the env's flux/kustomization.yaml lists.
GENERATED_REL = "generated/apps.yaml"
# The core directory each app's wiring Kustomization applies.
WIRING_PATH = "./core/platform/app-hosting/app"
# An app name: one DNS label short enough that app-<name> is one too.
APP_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?$")
_FQDN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LAN_PORT = re.compile(r"^([0-9]{1,5})/(TCP|UDP)$")
# Exception lines: <app>: <grant>. A grant names exactly one item.
FLAG_GRANTS = ("privileged", "root", "host-network", "host-path")
VALUE_GRANTS = ("hostname", "lan-port")
_KEYS = frozenset({"apps", "exceptions"})


def wiring_name(app: str) -> str:
    return f"app-{app}-wiring"


def load_values(env_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """The env's app file as a mapping, or errors. A missing file is no apps."""
    path = env_dir / APPS_VALUES
    if not path.is_file():
        return {}, []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return {}, [f"invalid YAML: {exc}"]
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, ["expected a mapping with apps and exceptions"]
    return data, []


def validate(data: dict[str, Any], dns_suffix: str | None) -> list[str]:
    """The app list holds names only, each once; each exception line names a listed app and
    one known grant, once. A custom hostname stays outside the env's own zone."""
    errors = [f"unknown key {key!r} (allowed: apps, exceptions)" for key in sorted(set(data) - _KEYS)]
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
        problem = grant_problem(grant, dns_suffix)
        if problem:
            errors.append(f"{where}: {problem}")
        elif (app, grant) in granted:
            errors.append(f"{where}: listed twice")
        granted.add((app, str(grant)))
    return errors


def grant_problem(grant: Any, dns_suffix: str | None) -> str | None:
    if not isinstance(grant, str):
        return "a grant is a string"
    if grant in FLAG_GRANTS:
        return None
    kind, _, value = grant.partition(" ")
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


def substitutes(data: dict[str, Any], app: str, cluster_vars: dict[str, Any]) -> dict[str, str]:
    """The inline values one app's wiring reads: its name, its grants, and env values core
    does not publish in cluster-vars."""
    for key in ("pod_cidr", "service_cidr"):
        ipaddress.ip_network(str(cluster_vars[key]), strict=True)
    grants = grant_data(data, app, "")
    return {
        "app_name": app,
        **{f"app_grant_{key.replace('-', '_')}": value for key, value in grants.items() if key != "host"},
        "app_pod_cidr": str(cluster_vars["pod_cidr"]),
        "app_service_cidr": str(cluster_vars["service_cidr"]),
        "apps_git_repo_url": str(cluster_vars["apps_git_repo_url"]),
    }


def grant_data(data: dict[str, Any], app: str, dns_suffix: str) -> dict[str, str]:
    """The data of ConfigMap flux-system/app-<name>-grants the app's wiring writes."""
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
        "# Generated by tools/lib/render_apps.py from values/apps.yaml — do not edit.\n"
        "# One trusted wiring per listed app (platform/app-hosting/CONTRACT.md).\n"
    )
    docs = wiring_documents(data, cluster_vars)
    return header + yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False, explicit_start=True)
