"""Validate one hosted app's folder: its app file and the objects in deploy/.

CONTRACT: platform/app-hosting/CONTRACT.md. The schema is the App CRD's
(platform/app-hosting/crd/app-crd.yaml), read here, with unknown keys refused; the API
server prunes them unread. The deploy/ rules are ValidatingAdmissionPolicies app-tenant and
app-file as far as the folder alone decides them: a risky item the file does not ask for
fails here, and one it asks for still needs the env's exception line in the cluster.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator

import yaml

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
CRD_FILE = PRODUCT_ROOT / "platform" / "app-hosting" / "crd" / "app-crd.yaml"
APP_FILE = "app.yaml"
DEPLOY_DIR = "deploy"
API_VERSION = "hosting.jarvis.io/v1alpha1"
KIND = "App"
HOST_VARIABLE = "${app_host}"
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
        ("networking.k8s.io", "Ingress"),
        ("policy", "PodDisruptionBudget"),
        ("autoscaling", "HorizontalPodAutoscaler"),
    }
)
_WORKLOADS = {"Deployment", "StatefulSet", "Job", "CronJob"}
# The CRD's CEL rules, by rule text. A rule added to the CRD fails here until it has its twin.
_RULES = {
    "type(self) == int ? (self >= 1 && self <= 65535) : self == 'none'": lambda value: (
        1 <= value <= 65535 if _is_int(value) else value == "none"
    ),
}


def check_folder(folder: Path | str) -> list[str]:
    """Every problem with one app folder, or none."""
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
    errors.extend(f"{name}/{APP_FILE}: {problem}" for problem in app_problems(app, name))
    if errors:
        return errors
    objects, error = _deploy_objects(folder / DEPLOY_DIR)
    if error:
        return [f"{name}/{DEPLOY_DIR}: {error}"]
    errors.extend(f"{name}/{DEPLOY_DIR}: {problem}" for problem in deploy_problems(app, name, objects))
    return errors


def app_problems(app: dict, name: str) -> list[str]:
    errors: list[str] = []
    if app.get("apiVersion") != API_VERSION or app.get("kind") != KIND:
        errors.append(f"apiVersion and kind are {API_VERSION} {KIND}")
    metadata = app.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {"name"}:
        errors.append("metadata holds the name only; core sets the namespace")
    elif metadata.get("name") != name:
        errors.append(f"metadata.name is {metadata.get('name')!r}; an app's name is its folder name, {name}")
    extra = sorted(set(app) - {"apiVersion", "kind", "metadata", "spec"})
    if extra:
        errors.append(f"unknown key(s) {', '.join(extra)}")
    schema = _crd_schema()["properties"]["spec"]
    errors.extend(_schema_problems(app.get("spec"), schema, "spec"))
    return errors


def _crd_schema() -> dict:
    crd = yaml.safe_load(CRD_FILE.read_text(encoding="utf-8"))
    return crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _schema_problems(value: Any, schema: dict, where: str) -> Iterator[str]:
    """The OpenAPI subset the App CRD uses, with unknown keys refused."""
    if value is None:
        yield f"{where}: is required"
        return
    if schema.get("x-kubernetes-int-or-string"):
        if not (_is_int(value) or isinstance(value, str)):
            yield f"{where}: is a number or a string"
            return
    else:
        kind = schema.get("type")
        ok = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": _is_int(value),
            "boolean": isinstance(value, bool),
        }.get(kind, True)
        if not ok:
            yield f"{where}: is a {kind}"
            return
    for rule in schema.get("x-kubernetes-validations") or []:
        check = _RULES.get(rule["rule"])
        if check is None:
            raise ValueError(f"the App CRD's rule {rule['rule']!r} has no twin in {__file__}")
        if not check(value):
            yield f"{where}: {rule['message']}"
    if "enum" in schema and value not in schema["enum"]:
        yield f"{where}: is one of {', '.join(map(str, schema['enum']))}"
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            yield f"{where}: is at most {schema['maxLength']} characters"
        if "pattern" in schema and not re.search(schema["pattern"], value):
            yield f"{where}: {value!r} does not match {schema['pattern']}"
    if _is_int(value):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            yield f"{where}: is from {schema.get('minimum')} to {schema.get('maximum')}"
    if isinstance(value, dict):
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            yield f"{where}: has at most {schema['maxProperties']} keys"
        properties = schema.get("properties")
        extra = schema.get("additionalProperties")
        for key in schema.get("required") or []:
            if key not in value:
                yield f"{where}.{key}: is required"
        for key, item in value.items():
            if properties is not None and key in properties:
                yield from _schema_problems(item, properties[key], f"{where}.{key}")
            elif isinstance(extra, dict):
                yield from _schema_problems(item, extra, f"{where}.{key}")
            else:
                yield f"{where}: unknown key {key!r}"
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            yield f"{where}: has at most {schema['maxItems']} items"
        for index, item in enumerate(value):
            yield from _schema_problems(item, schema.get("items") or {}, f"{where}[{index}]")
        list_type = schema.get("x-kubernetes-list-type")
        keys = schema.get("x-kubernetes-list-map-keys") or []
        seen = [tuple(item.get(key) for key in keys) if isinstance(item, dict) else item for item in value]
        if list_type in {"set", "map"} and len(seen) != len(set(map(str, seen))):
            yield f"{where}: holds each entry once"


def _deploy_objects(deploy: Path) -> tuple[list[dict], str | None]:
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
    try:
        docs = [doc for text in texts for doc in yaml.safe_load_all(text) if doc is not None]
    except yaml.YAMLError as exc:
        return [], f"invalid YAML: {exc}"
    if not all(isinstance(doc, dict) for doc in docs):
        return [], "holds a YAML document that is not an object"
    return docs, None


def deploy_problems(app: dict, name: str, objects: list[dict]) -> Iterator[str]:
    spec = app.get("spec") or {}
    risky = spec.get("risky") or {}
    volumes = {volume["name"]: volume["backup"] for volume in spec.get("volumes") or []}
    lan = {(port["port"], port["protocol"]) for port in spec.get("ports") or [] if port.get("lan")}
    hostnames = set(risky.get("hostnames") or [])
    for doc in objects:
        kind = str(doc.get("kind"))
        group = str(doc.get("apiVersion", "")).rpartition("/")[0]
        metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        ref = f"{kind} {metadata.get('name')}"
        if (group, kind) not in SHIPPED_KINDS:
            yield (
                f"{ref}: an app ships only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, "
                "Jobs, CronJobs, Ingresses, PodDisruptionBudgets and HorizontalPodAutoscalers"
            )
            continue
        if metadata.get("namespace") not in (None, f"app-{name}"):
            yield f"{ref}: lands in namespace app-{name} only; leave metadata.namespace unset"
        if kind in _WORKLOADS:
            yield from _volume_problems(ref, _pod_spec(doc))
            for item, field in _risky_uses(_pod_spec(doc)):
                if not risky.get(field):
                    yield f"{ref}: uses {item}, which needs the app file's risky.{field} and the env's exception line"
        for claim in _claims(doc):
            yield from _claim_problems(ref, claim, volumes)
        if kind == "Ingress":
            yield from _ingress_problems(ref, doc.get("spec") or {}, hostnames)
        if kind == "Service":
            yield from _service_problems(ref, doc.get("spec") or {}, lan)


def _pod_spec(doc: dict) -> dict:
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return ((spec.get("template") or {}).get("spec")) or {}


def _risky_uses(pod: dict) -> Iterator[tuple[str, str]]:
    containers = [*(pod.get("containers") or []), *(pod.get("initContainers") or [])]
    contexts = [container.get("securityContext") or {} for container in containers]
    pod_context = pod.get("securityContext") or {}
    if pod.get("hostPID") or pod.get("hostIPC") or "sysctls" in pod_context or _unconfined(pod_context) or any(
        context.get("privileged")
        or context.get("allowPrivilegeEscalation") is not False
        or _unconfined(context)
        or any(cap != "NET_BIND_SERVICE" for cap in (context.get("capabilities") or {}).get("add") or [])
        for context in contexts
    ):
        yield (
            "privileged mode, host PID or IPC, allowPrivilegeEscalation not false, added capabilities, "
            "an unconfined seccomp profile or sysctls"
        ), "privileged"
    if pod.get("hostNetwork") or any(
        port.get("hostPort") for container in containers for port in container.get("ports") or []
    ):
        yield "host network or host ports", "hostNetwork"
    if any("hostPath" in volume for volume in pod.get("volumes") or []):
        yield "a hostPath volume", "hostPath"
    non_root = all(
        context["runAsNonRoot"] if "runAsNonRoot" in context else pod_context.get("runAsNonRoot") is True
        for context in contexts
    )
    if pod_context.get("runAsUser") == 0 or any(context.get("runAsUser") == 0 for context in contexts) or not non_root:
        yield "root (every container needs runAsNonRoot: true and no runAsUser 0)", "root"


def _unconfined(context: dict) -> bool:
    return (context.get("seccompProfile") or {}).get("type") == "Unconfined"


# Volume types a pod may use; others mount from the node's network, past every NetworkPolicy.
VOLUME_TYPES = frozenset(
    {"configMap", "secret", "emptyDir", "persistentVolumeClaim", "projected", "downwardAPI", "csi", "ephemeral", "hostPath"}
)


def _volume_problems(ref: str, pod: dict) -> Iterator[str]:
    for volume in pod.get("volumes") or []:
        if not VOLUME_TYPES & set(volume):
            yield f"{ref}: volume {volume.get('name')} is not one of {', '.join(sorted(VOLUME_TYPES - {'hostPath'}))}"


def _claims(doc: dict) -> list[dict]:
    if doc.get("kind") == "PersistentVolumeClaim":
        return [doc]
    if doc.get("kind") == "StatefulSet":
        return list((doc.get("spec") or {}).get("volumeClaimTemplates") or [])
    return []


def _claim_problems(ref: str, claim: dict, volumes: dict[str, bool]) -> Iterator[str]:
    metadata = claim.get("metadata") or {}
    claim_name = metadata.get("name")
    if claim_name not in volumes:
        yield f"{ref}: claim {claim_name} is not a volume the app file declares"
        return
    unbacked = (metadata.get("annotations") or {}).get("backup") == "none"
    if volumes[claim_name] == unbacked:
        want = "carries no backup: none annotation" if volumes[claim_name] else "carries annotation backup: none"
        yield f"{ref}: claim {claim_name} is declared backup: {str(volumes[claim_name]).lower()}, so it {want}"


def _ingress_problems(ref: str, spec: dict, hostnames: set[str]) -> Iterator[str]:
    if "defaultBackend" in spec or "tls" in spec:
        yield f"{ref}: sets no default backend and no tls; the apps wildcard serves {HOST_VARIABLE}"
    rules = spec.get("rules") or []
    if not rules:
        yield f"{ref}: names its host in a rule"
    for rule in rules:
        host = rule.get("host")
        if host != HOST_VARIABLE and host not in hostnames:
            yield f"{ref}: host {host!r} is {HOST_VARIABLE} (<name>.apps.<dns suffix>) or one of risky.hostnames"


def _service_problems(ref: str, spec: dict, lan: set[tuple[int, str]]) -> Iterator[str]:
    kind = spec.get("type", "ClusterIP")
    if spec.get("externalIPs"):
        yield f"{ref}: sets no externalIPs"
    if kind == "ClusterIP":
        return
    if kind != "LoadBalancer":
        yield f"{ref}: type {kind}; an app's Services are ClusterIP, or LoadBalancer for its lan ports"
        return
    if spec.get("allocateLoadBalancerNodePorts") is not False:
        yield f"{ref}: a LoadBalancer sets allocateLoadBalancerNodePorts: false, so no node port opens on every node"
    for port in spec.get("ports") or []:
        if (port.get("port"), port.get("protocol", "TCP")) not in lan:
            yield f"{ref}: LoadBalancer port {port.get('port')} is not a lan port in the app file"
