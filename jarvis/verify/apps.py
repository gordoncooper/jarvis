"""Hosted-app standing checks (core/feature/app-hosting/CONTRACT.md, Health). Read-only.

jarvis verify runs this component only for an env whose values/apps.yaml lists an app.
"""

from __future__ import annotations

from jarvis.apps import values as _apps
from jarvis.verify.backups import backed_up
from jarvis.verify.common import CheckError, CheckWarning, Context, Kubectl, items
from jarvis.verify.registry import Check, register

BASELINE_POLICIES = ("default-deny", "cluster-dns", "same-namespace")


def _list(kubectl: Kubectl, kind: str, what: str) -> list[dict]:
    return items(kubectl(["get", kind, "-A", "-o", "json"]), what)


def _key(item: dict) -> tuple[str, str]:
    meta = item.get("metadata") or {}
    return str(meta.get("namespace") or ""), str(meta.get("name") or "")


def check_app_baseline(kubectl: Kubectl, *, data: dict, dns_suffix: str) -> None:
    """The cluster-wide admission bindings exist. Every listed app's namespace carries its label,
    network baseline and LimitRange; its annotations hold exactly the env's grants; each web
    entry its App does not route itself has core's route; every HTTPRoute hostname in it is a web
    entry's <name>[-<entry>].apps.<dns suffix> or a granted hostname. An app- namespace the env no
    longer lists is reported."""
    namespaces = {_key(item)[1]: item for item in _list(kubectl, "namespaces", "list namespaces")}
    policies = {_key(item) for item in _list(kubectl, "networkpolicies", "list NetworkPolicies")}
    ranges = {_key(item) for item in _list(kubectl, "limitranges", "list LimitRanges")}
    bindings = {_key(item)[1] for item in _list(kubectl, "validatingadmissionpolicybindings", "list admission bindings")}
    routes = _list(kubectl, "httproutes.gateway.networking.k8s.io", "list HTTPRoutes")
    web = {_key(app)[0]: ((app.get("spec") or {}).get("network") or {}).get("web") or []
           for app in _list(kubectl, "apps.hosting.jarvis.io", "list Apps")}
    problems = [f"no ValidatingAdmissionPolicyBinding {policy}" for policy in ("app-file", "app-grants") if policy not in bindings]
    listed = _apps.app_names(data)
    for app in listed:
        namespace = f"app-{app}"
        found = namespaces.get(namespace)
        if found is None:
            problems.append(f"{namespace}: no namespace")
            continue
        meta = found.get("metadata") or {}
        if (meta.get("labels") or {}).get("hosting.jarvis.io/app") != app:
            problems.append(f"{namespace}: label hosting.jarvis.io/app is not {app}")
        problems.extend(f"{namespace}: no NetworkPolicy {name}" for name in BASELINE_POLICIES if (namespace, name) not in policies)
        if (namespace, "app") not in ranges:
            problems.append(f"{namespace}: no LimitRange app")
        annotations = meta.get("annotations") or {}
        for key, value in _apps.grant_annotations(data, app, dns_suffix).items():
            if annotations.get(key) != value:
                problems.append(f"{namespace}: annotation {key} is {annotations.get(key)!r}, not the env's {value!r}")
        entries = web.get(namespace, [])
        suffixes = [f"-{entry['name']}" if entry.get("name") else "" for entry in entries]
        allowed = {f"{app}{suffix}.apps.{dns_suffix}" for suffix in suffixes}
        allowed |= set(filter(None, _apps.grant_data(data, app, dns_suffix)["hostnames"].split(",")))
        names = {_key(route)[1] for route in routes if _key(route)[0] == namespace}
        problems.extend(f"{namespace}: no HTTPRoute app-web{suffix}" for entry, suffix in zip(entries, suffixes, strict=True)
                        if not entry.get("ownRoute") and f"app-web{suffix}" not in names)
        for route in routes:
            if _key(route)[0] != namespace:
                continue
            for host in (route.get("spec") or {}).get("hostnames") or []:
                if host not in allowed:
                    problems.append(f"{namespace}/{_key(route)[1]}: hostname {host!r} is not a web entry's or granted")
    if problems:
        raise CheckError("; ".join(problems))
    leftover = sorted(name for name in namespaces if name.startswith("app-") and name.removeprefix("app-") not in listed)
    if leftover:
        raise CheckWarning(
            f"{', '.join(leftover)}: the env no longer lists these apps; delete what they left "
            "(docs/add-a-hosted-app.md, Turn it off)"
        )


def check_declared_unbacked(kubectl: Kubectl) -> None:
    """An app PVC annotated backup: none is one its App declares backup: false, and is reported
    as declared. Any other app PVC with that annotation fails."""
    declared = {
        (_key(app)[0], volume.get("name"))
        for app in _list(kubectl, "apps.hosting.jarvis.io", "list Apps")
        for volume in (app.get("spec") or {}).get("volumes") or []
        if volume.get("backup") is False
    }
    reported, problems = [], []
    for pvc in _list(kubectl, "pvc", "list PVCs"):
        namespace, name = _key(pvc)
        if not namespace.startswith("app-") or ((pvc.get("metadata") or {}).get("annotations") or {}).get("backup") != "none":
            continue
        if (pvc.get("metadata") or {}).get("labels", {}).get("app.kubernetes.io/created-by") == "volsync":
            continue
        # A StatefulSet's claim is <volume>-<set>-<n>.
        ok = any(ns == namespace and (name == volume or name.startswith(f"{volume}-")) for ns, volume in declared)
        (reported if ok else problems).append(f"{namespace}/{name}")
    if problems:
        raise CheckError(f"{', '.join(problems)}: annotated backup: none, and the app file does not declare them backup: false")
    if reported:
        raise CheckWarning(f"declared not backed up by the app file: {', '.join(reported)}")


def check_app_backups(kubectl: Kubectl, *, on: list[str]) -> None:
    """Each volume backups cover in the namespace of an app whose backups are on has a
    ReplicationSource. An app with them off is not checked. The backups component checks every
    source's freshness."""
    sources = {_key(item) for item in _list(kubectl, "replicationsource", "list ReplicationSources")}
    problems = [
        f"{namespace}/{name}"
        for namespace, name in map(_key, backed_up(_list(kubectl, "pvc", "list PVCs")))
        if namespace in on and (namespace, name) not in sources
    ]
    if problems:
        raise CheckError(f"{', '.join(problems)}: the app's backups are on, and no ReplicationSource")


@register("apps")
def apps_checks(ctx: Context) -> list[Check]:
    env_dir = ctx.repo_root / "envs" / ctx.env
    data, errors = _apps.load_values(env_dir)
    if errors:
        raise CheckError(f"values/apps.yaml: {'; '.join(errors)}")
    on = [f"app-{app}" for app in _apps.app_names(data) if _apps.backups_on(data, app)]
    return [
        (70, "hosted app baseline", lambda: check_app_baseline(ctx.kubectl, data=data, dns_suffix=str(ctx.values["dns_suffix"]))),
        (71, "hosted app volumes", lambda: check_declared_unbacked(ctx.kubectl)),
        (72, "hosted app backups", lambda: check_app_backups(ctx.kubectl, on=on)),
    ]
