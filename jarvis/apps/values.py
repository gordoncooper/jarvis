"""An env's hosted apps: its values/apps.yaml, and the wiring core renders from it.

CONTRACT: core/feature/app-hosting/CONTRACT.md. The file's shape is schema/apps.schema.json's;
what spans entries or needs the env's ranges is here. env-schema also checks each app at its
pinned tag; render_apps.py writes the wiring, and for an app with a chart its release, from the
env and each app file at its pinned tag.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

# envs/<env>/values/apps.yaml: one entry per app the env runs, keyed by its name.
APPS_VALUES = "values/apps.yaml"
# The render, under envs/<env>/flux/, which the env's flux/kustomization.yaml lists.
GENERATED_REL = "generated/apps.yaml"
# The core directory each app's wiring Kustomization applies: an app with a chart gets the base,
# and one with deploy/ the base and its deploy/ Kustomization, beside it.
WIRING_PATH = "./core/feature/app-hosting/app"
DEPLOY_WIRING_PATH = "./core/feature/app-hosting/app-deploy"
# Flux's annotations: keep a chart source when the app leaves the list, as every Flux source is
# kept; and apply the app's and the env's chart values as written, with no substitution.
_KEEP = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}
_VERBATIM = {"kustomize.toolkit.fluxcd.io/substitute": "disabled"}

# An app name: one DNS label short enough that app-<name> is one too.
APP_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?$")
_FQDN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LAN_PORT = re.compile(r"^([0-9]{1,5})/(TCP|UDP)$")
# A LAN target's name, as an App's network.egress.lan lists it. "all" is reserved: [all] asks
# for the whole LAN, which lan-all grants.
_TARGET = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
# Grants: each names exactly one item.
FLAG_GRANTS = ("privileged", "root", "host-network", "host-path", "lan-all")
VALUE_GRANTS = ("hostname", "lan-port", "lan-target")
# Grants that open the LAN, so need the env's lan_cidrs.
LAN_GRANTS = ("lan-all", "lan-target")
# values/cluster-vars.yaml: the env's LAN ranges.
LAN_CIDRS = "lan_cidrs"
_PRIVATE = tuple(ipaddress.IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
# Ports no lan-port grant takes. In shared-address mode an app's LAN port listens on each edge
# node's own address, beside core's doors and the node's own services (k3s's inbound ports).
RESERVED_PORTS = {
    "53/TCP": "App DNS", "53/UDP": "App DNS", "80/TCP": "core's web door", "443/TCP": "core's web door",
    "22/TCP": "SSH", "6443/TCP": "the Kubernetes API", "10250/TCP": "the kubelet",
    "2379/TCP": "etcd", "2380/TCP": "etcd", "5001/TCP": "k3s's embedded registry",
    "8472/UDP": "flannel's VXLAN", "51820/UDP": "flannel's WireGuard", "51821/UDP": "flannel's WireGuard",
}


def wiring_name(app: str) -> str:
    return f"app-{app}-wiring"


def app_release(doc: dict[str, Any]) -> bool:
    """A hosted app's chart release: HelmRelease flux-system/app-<name>, run as the app's tenant
    app-<name> into app-<name>. The app checks hold its chart to the app rules."""
    metadata, spec = doc.get("metadata") or {}, doc.get("spec") or {}
    name = metadata.get("name")
    return (doc.get("kind") == "HelmRelease" and metadata.get("namespace") == "flux-system"
            and str(name).startswith("app-") and bool(APP_NAME.fullmatch(str(name)[4:]))
            and spec.get("serviceAccountName") == spec.get("targetNamespace") == name)


def load_values(env_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """The env's apps file as {app: entry}, or errors. A missing or empty file lists no app; its
    shape is the schema's to check (env-schema)."""
    path = env_dir / APPS_VALUES
    if not path.is_file():
        return {}, []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return {}, [f"invalid YAML: {exc}"]
    if data is None:
        return {}, []
    if not isinstance(data, dict) or not all(isinstance(entry, dict) for entry in data.values()):
        return {}, ["expected one entry per app, <app>: {version: v<x.y.z>}"]
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
    """Each grant is one known item, once per app. A custom hostname stays outside the env's own
    zone; a LAN grant needs the env's lan_cidrs, and a lan-target's range sits inside them. A
    LAN port is no port core or the node holds, and no two apps hold the same one."""
    errors: list[str] = []
    ports: dict[str, str] = {}
    for app in app_names(data):
        for grant in grants_for(data, app):
            problem = grant_problem(grant, dns_suffix, lan_cidrs or [])
            if problem:
                errors.append(f"{app}: grants: {grant}: {problem}")
                continue
            kind, _, port = grant.partition(" ")
            if kind != "lan-port":
                continue
            if port in RESERVED_PORTS:
                errors.append(f"{app}: grants: {grant}: {port} is {RESERVED_PORTS[port]}'s; remap the app's listen port")
            elif port in ports:
                errors.append(f"{app}: grants: {grant}: app {ports[port]} already holds {port}; remap one listen port")
            else:
                ports[port] = app
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
    return [str(app) for app in data]


def grants_for(data: dict[str, Any], app: str) -> list[str]:
    return [str(grant) for grant in (data.get(app) or {}).get("grants") or []]


def backups_on(data: dict[str, Any], app: str) -> bool:
    """Whether the env backs up the app's backup: true volumes (schema default: on)."""
    return (data.get(app) or {}).get("backups", True) is not False


def app_ref(data: dict[str, Any], app: str) -> str:
    """The apps-repository ref Flux reads the app at: its pinned tag."""
    return f"refs/tags/{app}/{data[app]['version']}"


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
        **{f"app_grant_{key.replace('-', '_')}": value for key, value in grants.items() if key != "apps-domain"},
        "app_pod_cidr": str(cluster_vars["pod_cidr"]),
        "app_service_cidr": str(cluster_vars["service_cidr"]),
        "app_lan_cidrs": ",".join(lan),
        "apps_git_repo_url": str(cluster_vars["apps_git_repo_url"]),
        "app_ref": app_ref(data, app),
    }


def grant_annotations(data: dict[str, Any], app: str, dns_suffix: str) -> dict[str, str]:
    """The annotations the app's wiring writes on its namespace from the env's grants."""
    return {f"hosting.jarvis.io/{key}": value for key, value in grant_data(data, app, dns_suffix).items()}


def grant_data(data: dict[str, Any], app: str, dns_suffix: str) -> dict[str, str]:
    """The env's apps domain and its grants for the app, by grant."""
    grants = grants_for(data, app)

    def values(kind: str) -> str:
        return ",".join(sorted(grant.partition(" ")[2] for grant in grants if grant.startswith(kind + " ")))

    return {
        "apps-domain": f"apps.{dns_suffix}",
        "privileged": _flag("privileged" in grants),
        "root": _flag("root" in grants),
        "host-network": _flag("host-network" in grants),
        "host-path": _flag("host-path" in grants),
        "hostnames": values("hostname"),
        "lan-ports": values("lan-port"),
        "lan-all": _flag("lan-all" in grants),
        "lan-targets": values("lan-target"),
    }


def _flag(value: bool) -> str:
    return "true" if value else "false"


def wiring_documents(data: dict[str, Any], cluster_vars: dict[str, Any], charts: dict[str, dict]) -> list[dict[str, Any]]:
    """One wiring Kustomization per listed app, in list order."""
    return [
        {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": wiring_name(app), "namespace": "flux-system"},
            "spec": {
                "interval": "10m",
                "retryInterval": "1m",
                "path": WIRING_PATH if app in charts else DEPLOY_WIRING_PATH,
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


def _retry() -> dict[str, Any]:
    """A failed install or upgrade (the app's admission params not applied yet, say) is retried,
    not stalled. Helm does not wait on the objects, as an app's deploy/ Kustomization does not."""
    return {"disableWait": True, "strategy": {"name": "RetryOnFailure", "retryInterval": "1m"}}


def chart_documents(app: str, chart: dict[str, Any], env_values: dict[str, Any] | None) -> list[dict[str, Any]]:
    """An app's chart, installed by helm-controller as the app's tenant into app-<name>: its source,
    the app file's values as a ConfigMap, and the release, the env's values inline on top. They live
    in flux-system, where the tenant writes nothing."""
    name = f"app-{app}"
    meta = {"name": name, "namespace": "flux-system"}
    source = {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "OCIRepository", "metadata": {**meta, "annotations": _KEEP},
              "spec": {"interval": "30m", "url": chart["url"], "ref": {"tag": chart["version"]},
                       "layerSelector": {"mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip", "operation": "copy"}}}
    values = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{name}-values", "namespace": "flux-system", "annotations": _VERBATIM},
              "data": {"values.yaml": yaml.safe_dump(chart.get("values") or {}, sort_keys=False)}}
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {**meta, "annotations": _VERBATIM},
        "spec": {
            "interval": "10m", "releaseName": app, "serviceAccountName": name, "targetNamespace": name, "storageNamespace": name,
            "chartRef": {"kind": "OCIRepository", "name": name}, "install": _retry(), "upgrade": _retry(),
            "valuesFrom": [{"kind": "ConfigMap", "name": f"{name}-values"}],
            **({"values": env_values} if env_values else {}),
        },
    }  # fmt: skip
    return [source, values, release]


def charts(data: dict[str, Any], apps_root: Path) -> dict[str, dict]:
    """The chart of each listed app whose app file names one, read where apps_root holds it."""
    found = {}
    for app in app_names(data):
        spec = yaml.safe_load((apps_root / app / "app.yaml").read_text(encoding="utf-8"))["spec"]
        if spec.get("chart"):
            found[app] = spec["chart"]
    return found


class _Dumper(yaml.SafeDumper):
    """safe_dump, with a values file in a ConfigMap written as a block, as it reads in the app file."""


_Dumper.add_representer(
    str, lambda dumper, text: dumper.represent_scalar("tag:yaml.org,2002:str", text, style="|" if "\n" in text else None)
)


def render(data: dict[str, Any], cluster_vars: dict[str, Any], apps_root: Path) -> str:
    header = (
        "# Generated by jarvis render from values/apps.yaml — do not edit.\n"
        "# One trusted wiring per listed app, and each chart's release (core/feature/app-hosting/CONTRACT.md).\n"
    )
    found = charts(data, apps_root)
    docs = wiring_documents(data, cluster_vars, found)
    for app, chart in found.items():
        docs += chart_documents(app, chart, (data[app] or {}).get("values"))
    return header + yaml.dump_all(docs, Dumper=_Dumper, sort_keys=False, default_flow_style=False, explicit_start=True)
