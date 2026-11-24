"""Validate one hosted app's folder: its app file and the objects in deploy/, or its chart's.

CONTRACT: core/feature/app-hosting/CONTRACT.md. kubeconform holds the App to the App CRD
(core/feature/app-hosting/crd/app-crd.yaml), unknown keys refused; the CRD's one CEL rule is
the API server's to run. The objects meet the cluster's own admission as the app's tenant
applies them: MutatingPolicy app-priority, then ValidatingAdmissionPolicies app-tenant and
app-file, run by the Kyverno CLI with the App as the param. app-grants holds the env's grants,
which the folder alone cannot decide, and stays the cluster's. A deploy/ app's objects are
read as its Kustomization applies them (in app-<name>, with ${app_host}); a chart app's are the
chart's, rendered by `helm template` as helm-controller installs them (release <name> in
app-<name>, the app's values, then the env's).
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from jarvis.paths import PRODUCT_ROOT

HOSTING = PRODUCT_ROOT / "core" / "feature" / "app-hosting"
CRD_FILE = HOSTING / "crd" / "app-crd.yaml"
APP_FILE = "app.yaml"
DEPLOY_DIR = "deploy"
API_VERSION = "hosting.jarvis.io/v1alpha1"
KIND = "App"
APP_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?$")
# What an app ships, by (API group, kind); ValidatingAdmissionPolicy app-tenant holds the same list.
SHIPPED_KINDS = frozenset(
    {
        ("", "ConfigMap"),
        ("", "Secret"),
        ("", "Service"),
        ("", "PersistentVolumeClaim"),
        ("apps", "Deployment"),
        ("apps", "StatefulSet"),
        ("batch", "Job"),
        ("batch", "CronJob"),
        ("gateway.networking.k8s.io", "HTTPRoute"),
        ("policy", "PodDisruptionBudget"),
        ("autoscaling", "HorizontalPodAutoscaler"),
    }
)
# ${app_host}'s zone in the folder check: app-file holds a route's hostname to <name>[-<entry>].apps.,
# and app-grants, which the cluster alone runs, holds the rest to the env's.
FOLDER_ZONE = "env.invalid"


def check_folder(folder: Path | str, env_values: dict | None = None, k3s: str | None = None) -> list[str]:
    """Every problem with one app folder, or none. env_values: the env's values for the app's
    chart, laid over the app's own; k3s: the env's release, which the chart renders for."""
    folder = Path(folder)
    if not folder.is_dir():
        return [f"{folder}: not a folder"]
    name = folder.name
    errors: list[str] = []
    if not APP_NAME.fullmatch(name):
        errors.append(
            f"{name}: an app's folder name is its name, a DNS label of at most 59 characters (app-<name> is its namespace)"
        )
    app_path = folder / APP_FILE
    if not app_path.is_file():
        return [*errors, f"{name}: missing {APP_FILE}; nothing of an app deploys without its app file"]
    try:
        docs = [doc for doc in yaml.safe_load_all(app_path.read_text(encoding="utf-8")) if doc is not None]
    except yaml.YAMLError as exc:
        return [*errors, f"{name}/{APP_FILE}: invalid YAML: {exc}"]
    if len(docs) != 1 or not isinstance(docs[0], dict):
        return [*errors, f"{name}/{APP_FILE}: holds exactly one App"]
    app = docs[0]
    errors.extend(f"{name}/{APP_FILE}: {problem}" for problem in app_problems(app, name, k3s))
    if errors:
        return errors
    chart = app["spec"].get("chart")
    if chart and (folder / DEPLOY_DIR).exists():
        return [f"{name}/{DEPLOY_DIR}: an app with a chart ships no {DEPLOY_DIR}/; its objects are the chart's"]
    where = f"{name} chart {chart['version']}" if chart else f"{name}/{DEPLOY_DIR}"
    objects, error = chart_objects(name, chart, env_values, k3s) if chart else _deploy_objects(folder / DEPLOY_DIR, name)
    if error:
        return [f"{where}: {error}"]
    errors.extend(f"{where}: {problem}" for problem in admission_problems(app, name, objects))
    return errors


def chart_objects(
    name: str, chart: dict, env_values: dict | None = None, k3s: str | None = None
) -> tuple[list[dict], str | None]:
    """The chart's objects as helm-controller installs them for app name, at the k3s release given
    (the env's), or core's."""
    from jarvis.checks.rendered import helm_template
    from jarvis.env.k3s import core_release

    values = (yaml.safe_dump(chart.get("values") or {}), yaml.safe_dump(env_values or {}))
    kube = (k3s or core_release()[0]).split("+")[0]
    out, error = helm_template(name, f"app-{name}", (chart["url"], "--version", chart["version"]), values, kube)
    if error:
        return [], f"helm template failed: {error}"
    return [doc for doc in yaml.safe_load_all(out) if isinstance(doc, dict)], None


def web_labels(folder: Path) -> list[str]:
    """The host label of each web entry of the app in folder, which check_folder passes: <name>
    for the entry with no name and <name>-<entry> for a named one, each one label under the env's
    apps domain (core's routes, app-derive, take the same names)."""
    spec = yaml.safe_load((folder / APP_FILE).read_text(encoding="utf-8"))["spec"]
    return [folder.name + (f"-{entry['name']}" if entry.get("name") else "")
            for entry in (spec.get("network") or {}).get("web") or []]


def app_problems(app: dict, name: str, k3s: str | None = None) -> list[str]:
    """The folder's rules (one App, named after its folder, each web entry's host one label), then
    kubeconform against the App CRD."""
    from jarvis.env.k3s import core_release

    if app.get("apiVersion") != API_VERSION or app.get("kind") != KIND:
        return [f"apiVersion and kind are {API_VERSION} {KIND}"]
    metadata = app.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {"name"}:
        return ["metadata holds the name only; core sets the namespace"]
    if metadata.get("name") != name:
        return [f"metadata.name is {metadata.get('name')!r}; an app's name is its folder name, {name}"]
    errors = list(_schema_problems(json.dumps(app, sort_keys=True, default=str), k3s or core_release()[0]))
    if not errors:
        for entry in ((app["spec"].get("network") or {}).get("web") or []):
            if entry.get("name") and len(name) + 1 + len(entry["name"]) > 63:
                errors.append(f"spec.network.web: entry {entry['name']!r} makes host label {name}-{entry['name']}, longer than 63")
    return errors


# validate's env-schema and render each check every listed app, so one process checks the same App
# and objects again: each verdict is kept by its input.
@functools.cache
def _schema_problems(app: str, k3s: str) -> tuple[str, ...]:
    from jarvis.checks.rendered import kubeconform

    return tuple(kubeconform([yaml.safe_load(CRD_FILE.read_text(encoding="utf-8")), json.loads(app)], k3s))


def _deploy_objects(deploy: Path, name: str) -> tuple[list[dict], str | None]:
    """deploy/'s objects as the app's Kustomization applies them: kustomize when it holds a
    kustomization.yaml, Flux's strict substitution of the one value it gives, app_host, and its
    targetNamespace, app-<name>."""
    from jarvis.checks.rendered import _tool

    if not deploy.is_dir():
        return [], f"missing; the app's objects live in {DEPLOY_DIR}/"
    if (deploy / "kustomization.yaml").is_file():
        kustomize = shutil.which("kustomize")
        if kustomize is None:
            return [], "has a kustomization.yaml, and kustomize is not on PATH"
        proc = subprocess.run([kustomize, "build", str(deploy)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return [], f"kustomize build failed: {proc.stderr.strip()}"
        texts = [proc.stdout]
    else:
        texts = [
            path.read_text(encoding="utf-8")
            for path in sorted(deploy.rglob("*"))
            if path.is_file() and path.suffix in {".yaml", ".yml"}
        ]
    proc = subprocess.run([_tool("flux"), "envsubst", "--strict"], input="\n---\n".join(texts), capture_output=True, text=True,
                          check=False, env={"PATH": os.environ.get("PATH", ""), "app_host": f"{name}.apps.{FOLDER_ZONE}"})  # fmt: skip
    if proc.returncode:
        return [], f"Flux's substitution failed: {proc.stderr.strip()}"
    try:
        docs = [doc for doc in yaml.safe_load_all(proc.stdout) if doc is not None]
    except yaml.YAMLError as exc:
        return [], f"invalid YAML: {exc}"
    if not all(isinstance(doc, dict) for doc in docs):
        return [], "holds a YAML document that is not an object"
    return [{**doc, "metadata": {**(doc.get("metadata") or {}), "namespace": f"app-{name}"}} for doc in docs], None


def stand_in_crd(group: str, version: str, kind: str) -> dict:
    """A schemaless CRD for a kind that is not built in: the Kyverno CLI maps kinds from CRDs, and
    a loopback API server serves no other."""
    plural = kind.lower() + "s"
    return {
        "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        # A *.k8s.io group needs this annotation; the real Gateway API CRDs carry an approved one.
        "metadata": {"name": f"{plural}.{group}", "annotations": {"api-approved.kubernetes.io": "unapproved, stand-in"}},
        "spec": {"group": group, "scope": "Namespaced",
                 "names": {"kind": kind, "plural": plural, "singular": kind.lower(), "listKind": f"{kind}List"},
                 "versions": [{"name": version, "served": True, "storage": True,
                               "schema": {"openAPIV3Schema": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}}}]},
    }  # fmt: skip


def app_file_binding(name: str) -> dict:
    """The shipped app-file binding as it resolves in app-<name>: the CLI resolves no selector
    paramRef, so its paramRef names the App (test_app_admission_apiserver.py proves the shipped one)."""
    binding = next(doc for doc in yaml.safe_load_all((HOSTING / "policies" / "admission.yaml").read_text(encoding="utf-8"))
                   if (doc["kind"], doc["metadata"]["name"]) == ("ValidatingAdmissionPolicyBinding", "app-file"))
    action = binding["spec"]["paramRef"]["parameterNotFoundAction"]
    binding["spec"]["paramRef"] = {"name": name, "namespace": f"app-{name}", "parameterNotFoundAction": action}
    return binding


def userinfo(username: str) -> dict:
    return {"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "UserInfo", "metadata": {"name": "u"},
            "userInfo": {"username": username, "groups": ["system:authenticated"]}}


def mutated(docs: list[dict], username: str, names: tuple[str, ...] = ("app-priority",),
            context: tuple[dict, ...] = ()) -> tuple[list[dict], str]:
    """docs as app-hosting's MutatingPolicies names leave them at admission when username writes
    them, and the CLI's error. context: the objects their resource.get reads."""
    from jarvis.checks.rendered import _tool

    files = {"policy": [doc for path in (HOSTING / "priority.yaml", HOSTING / "lan-name.yaml")
                        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                        if doc["kind"] == "MutatingPolicy" and doc["metadata"]["name"] in names],
             "resources": docs, "userinfo": [userinfo(username)],
             "context": [{"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Context", "metadata": {"name": "c"},
                          "spec": {"resources": list(context)}}]}  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="jarvis-mutate-") as work:
        for key, value in files.items():
            (Path(work) / f"{key}.yaml").write_text(yaml.safe_dump_all(value, sort_keys=False), encoding="utf-8")
        out = Path(work) / "out.yaml"
        proc = subprocess.run(
            [_tool("kyverno"), "apply", f"{work}/policy.yaml", "--resource", f"{work}/resources.yaml", "--remove-color",
             "--userinfo", f"{work}/userinfo.yaml", "--context-file", f"{work}/context.yaml", "-o", str(out)],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if proc.returncode or ", error: 0," not in proc.stdout:
            return docs, (proc.stderr or proc.stdout).strip()
        changed = {(doc["kind"], doc["metadata"]["name"]): doc for doc in yaml.safe_load_all(out.read_text(encoding="utf-8")) if doc} if out.is_file() else {}
    return [changed.get((doc.get("kind"), (doc.get("metadata") or {}).get("name")), doc) for doc in docs], ""


def admission_problems(app: dict, name: str, objects: list[dict]) -> list[str]:
    """What app-tenant and app-file refuse of objects as the app's tenant applies them, once
    app-priority has set each workload's class: one message per policy and object. An object
    with no namespace lands in app-<name>, the release's or the Kustomization's."""
    return list(_admission(json.dumps([app, name, objects], sort_keys=True, default=str)))


@functools.cache
def _admission(key: str) -> tuple[str, ...]:
    from jarvis.checks.rendered import policies

    app, name, objects = json.loads(key)
    namespace, tenant = f"app-{name}", f"system:serviceaccount:flux-system:app-{name}"
    objects = [{**doc, "metadata": {"namespace": namespace, **(doc.get("metadata") or {})}} for doc in objects]
    objects, error = mutated(objects, tenant)
    if error:
        return (f"app-priority: {error}",)
    rules = [doc for doc in yaml.safe_load_all((HOSTING / "policies" / "admission.yaml").read_text(encoding="utf-8"))
             if doc["metadata"]["name"] == "app-tenant" or (doc["kind"], doc["metadata"]["name"]) == ("ValidatingAdmissionPolicy", "app-file")]
    values = {"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Value", "metadata": {"name": "v"},
              "namespaceSelector": [{"name": namespace, "labels": {"kubernetes.io/metadata.name": namespace, "hosting.jarvis.io/app": name}}]}
    return tuple(policies(objects, [*rules, app_file_binding(name)], {
        "--parameter-resource": [{**app, "metadata": {**app["metadata"], "namespace": namespace}}], "--values-file": [values],
        "--userinfo": [userinfo(tenant)], "--crd-paths": [stand_in_crd("gateway.networking.k8s.io", "v1", "HTTPRoute")]}))  # fmt: skip
