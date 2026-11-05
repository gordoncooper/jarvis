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
# ClusterPolicy app-priority sets it on every workload; app-file refuses another.
PRIORITY_CLASS = "hosted-app"
# The only router annotations an app's Ingress carries (app-file holds the same pairs).
INGRESS_ANNOTATIONS = {
    "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
    "traefik.ingress.kubernetes.io/router.tls": "true",
}
# The CRD's CEL rules, by rule text. A rule added to the CRD fails here until it has its twin.
_RULES = {
    "!('all' in self) || self.size() == 1": lambda value: "all" not in value or len(value) == 1,
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
    lan = {(port["listen"], port["protocol"]): port["target"] for port in (spec.get("network") or {}).get("ports") or []}
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
            annotations = (_template(doc).get("metadata") or {}).get("annotations") or {}
            for item, field in _risky_uses(_pod_spec(doc), annotations):
                if not risky.get(field):
                    yield f"{ref}: uses {item}, which needs the app file's risky.{field} and the env's exception line"
            yield from _memory_problems(ref, _pod_spec(doc))
            if _pod_spec(doc).get("priorityClassName") not in (None, PRIORITY_CLASS):
                yield f"{ref}: sets priorityClassName; core runs every app pod at {PRIORITY_CLASS} and refuses another class"
        for claim in _claims(doc):
            yield from _claim_problems(ref, claim, volumes)
        annotations = metadata.get("annotations") or {}
        if kind == "Ingress":
            yield from _ingress_problems(ref, doc.get("spec") or {}, hostnames)
            yield from _router_problems(ref, doc.get("spec") or {}, annotations)
        if kind == "Service":
            yield from _service_problems(ref, doc.get("spec") or {}, lan)
            if any(_traefik_key(key) for key in annotations):
                yield f"{ref}: carries no Traefik annotation"


def _template(doc: dict) -> dict:
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return spec.get("template") or {}


def _pod_spec(doc: dict) -> dict:
    return _template(doc).get("spec") or {}


_SECCOMP = frozenset({"RuntimeDefault", "Localhost"})
_SELINUX_TYPES = frozenset({"", "container_t", "container_init_t", "container_kvm_t", "container_engine_t"})


def _beyond_restricted(pod: dict, pod_context: dict, contexts: list[dict], annotations: dict) -> bool:
    """Whether the pod needs more than Pod Security restricted (admission's usesPrivileged)."""
    pod_seccomp = (pod_context.get("seccompProfile") or {}).get("type") in _SECCOMP
    selinux = [options for options in (pod_context.get("seLinuxOptions"), *(c.get("seLinuxOptions") for c in contexts)) if options]
    return bool(
        pod.get("hostPID") or pod.get("hostIPC") or "sysctls" in pod_context or _unconfined(pod_context)
        or (pod_context.get("appArmorProfile") or {}).get("type") == "Unconfined"
        or any(key.startswith("container.apparmor.security.beta.kubernetes.io/")
               and not (value == "runtime/default" or str(value).startswith("localhost/"))
               for key, value in annotations.items())
        or any(options.get("type", "") not in _SELINUX_TYPES or options.get("user") or options.get("role") for options in selinux)
        or any(
            context.get("privileged")
            or context.get("allowPrivilegeEscalation") is not False
            or ((context["seccompProfile"].get("type") not in _SECCOMP) if context.get("seccompProfile") else not pod_seccomp)
            or "ALL" not in ((context.get("capabilities") or {}).get("drop") or [])
            or any(cap != "NET_BIND_SERVICE" for cap in (context.get("capabilities") or {}).get("add") or [])
            or context.get("procMount", "Default") != "Default"
            or (context.get("appArmorProfile") or {}).get("type") == "Unconfined"
            for context in contexts
        )
    )


def _risky_uses(pod: dict, annotations: dict | None = None) -> Iterator[tuple[str, str]]:
    containers = [*(pod.get("containers") or []), *(pod.get("initContainers") or [])]
    contexts = [container.get("securityContext") or {} for container in containers]
    pod_context = pod.get("securityContext") or {}
    if _beyond_restricted(pod, pod_context, contexts, annotations or {}):
        yield (
            "more than Pod Security restricted (privileged mode, host PID or IPC, sysctls, allowPrivilegeEscalation "
            "not false, capabilities not dropping ALL or adding more than NET_BIND_SERVICE, a seccomp profile not "
            "RuntimeDefault or Localhost, procMount Unmasked, AppArmor unconfined, or SELinux options beyond a container type)"
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


_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|)$")
_MEMORY_FACTOR = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
                  "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}


def _bytes(quantity: object) -> float | str:
    """A memory quantity in bytes, or the text itself when it is no plain quantity."""
    match = _MEMORY.fullmatch(str(quantity).strip())
    return float(match.group(1)) * _MEMORY_FACTOR[match.group(2)] if match else str(quantity)


def _memory_problems(ref: str, pod: dict) -> Iterator[str]:
    """admission's app-file: a container's memory request is its limit (a limit alone passes)."""
    for container in [*(pod.get("containers") or []), *(pod.get("initContainers") or [])]:
        resources = container.get("resources") or {}
        request = (resources.get("requests") or {}).get("memory")
        limit = (resources.get("limits") or {}).get("memory")
        if request is not None and (limit is None or _bytes(request) != _bytes(limit)):
            yield (f"{ref}: container {container.get('name')} requests memory {request} with limit {limit}; "
                   "a hosted app's memory request equals its limit (or set the limit alone)")


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
    spec = claim.get("spec") or {}
    chosen = sorted(key for key in ("dataSource", "dataSourceRef") if key in spec) + (["volumeName"] if spec.get("volumeName") else [])
    if chosen:
        yield f"{ref}: claim {claim_name} sets {', '.join(chosen)}; the cluster provisions and binds an app's volumes"
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


def _traefik_key(key: str) -> bool:
    return key.startswith("traefik.") or "traefik.io/" in key


def _router_problems(ref: str, spec: dict, annotations: dict) -> Iterator[str]:
    if spec.get("ingressClassName") != "traefik":
        yield f"{ref}: sets ingressClassName: traefik"
    if annotations.get("traefik.ingress.kubernetes.io/router.entrypoints") != "websecure":
        yield f"{ref}: sets annotation traefik.ingress.kubernetes.io/router.entrypoints: websecure (no plain-HTTP route)"
    for key, value in annotations.items():
        router = _traefik_key(key) or "ingress.kubernetes.io/" in key or key == "kubernetes.io/ingress.class"
        if router and INGRESS_ANNOTATIONS.get(key) != str(value):
            yield (f"{ref}: annotation {key} is not allowed; of router annotations an app's Ingress carries only "
                   + " and ".join(f"{k}: {v!r}" for k, v in INGRESS_ANNOTATIONS.items()))


def _service_problems(ref: str, spec: dict, lan: dict[tuple[int, str], int]) -> Iterator[str]:
    kind = spec.get("type", "ClusterIP")
    if spec.get("externalIPs"):
        yield f"{ref}: sets no externalIPs"
    if kind == "ClusterIP":
        return
    if kind != "LoadBalancer":
        yield f"{ref}: type {kind}; an app's Services are ClusterIP, or LoadBalancer for its network.ports"
        return
    if spec.get("allocateLoadBalancerNodePorts") is not False:
        yield f"{ref}: a LoadBalancer sets allocateLoadBalancerNodePorts: false, so no node port opens on every node"
    for port in spec.get("ports") or []:
        key = (port.get("port"), port.get("protocol", "TCP"))
        if key not in lan:
            yield f"{ref}: LoadBalancer port {port.get('port')} is not a listen port in the app file's network.ports"
        elif port.get("targetPort", port.get("port")) != lan[key]:
            yield f"{ref}: LoadBalancer port {port.get('port')} sets targetPort {lan[key]}, the number network.ports gives as target"
