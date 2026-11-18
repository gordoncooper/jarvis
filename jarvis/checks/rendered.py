"""What an env deploys, checked by the upstream tools: kubeconform for schemas, the Kyverno CLI for
core's rules (rules.yaml beside this file).

The objects are Flux's build of every path on the root artifact (flux_build.env_tree) and each
HelmRelease's chart, rendered by `helm template` from the source the build names, with the
release's values, at the env's Kubernetes version. helm-controller renders the same way; its
postRenderers are not applied here. A hosted app's paths read the apps repository, so the app
checks read those. k3s's own charts (Traefik, configured by a HelmChartConfig) ship inside k3s and
are not rendered.

kubeconform reads Kubernetes' schemas, then the CRDs these objects carry themselves and the ones
the pinned flux CLI installs, then one pinned public catalog for the rest (k3s's Traefik and
HelmChartConfig kinds). Every object must find a schema. CustomResourceDefinitions are not sent:
kubernetes-json-schema has none for them, the API server checks them in e2e, and here each one's
own schema checks its objects.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from jarvis.checks.flux_build import env_tree
from jarvis.env.k3s import release
from jarvis.schema.env import env_dirs, kubernetes_version_from_k3s

RULES = Path(__file__).with_name("rules.yaml")
RULE_EXCEPTIONS = Path(__file__).with_name("rule-exceptions.yaml")
# datreeio/CRDs-catalog, pinned: tags through v0.0.12 omit kinds this reads.
CATALOG = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/3eb90d47aba8d61919d4419dd38301536e32a94b/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _schema_cache() -> Path:
    """kubeconform's download cache: the schemas it reads are fixed by the Kubernetes version and the
    catalog commit, so one cache serves every run."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "jarvis" / "kubeconform"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} is not on PATH (run jarvis doctor)")
    return path


def _key(doc: dict) -> tuple[str, str, str]:
    meta = doc.get("metadata") or {}
    return str(doc.get("kind")), str(meta.get("namespace") or ""), str(meta.get("name") or "")


def _chart(release_doc: dict, sources: dict[tuple[str, str, str], dict]) -> tuple[list[str], str | None]:
    """The chart argument and flags `helm template` takes for the release's source."""
    spec = release_doc.get("spec") or {}
    namespace = _key(release_doc)[1]
    if ref := spec.get("chartRef"):
        source = sources.get((str(ref.get("kind")), str(ref.get("namespace") or namespace), str(ref.get("name"))))
        tag = ((source or {}).get("spec") or {}).get("ref", {}).get("tag")
        if source is None or source["kind"] != "OCIRepository" or not tag:
            return [], f"chartRef {ref.get('kind')}/{ref.get('name')} is not an OCIRepository with spec.ref.tag in the build"
        return [source["spec"]["url"], "--version", str(tag)], None
    chart = (spec.get("chart") or {}).get("spec") or {}
    ref = chart.get("sourceRef") or {}
    source = sources.get(("HelmRepository", str(ref.get("namespace") or namespace), str(ref.get("name"))))
    if source is None:
        return [], f"chart sourceRef HelmRepository/{ref.get('name')} is not in the build"
    return [str(chart.get("chart")), "--repo", source["spec"]["url"], "--version", str(chart.get("version"))], None


@functools.cache
def _template(release_name: str, namespace: str, chart: tuple[str, ...], values: str, kube: str) -> tuple[str, str]:
    """helm template's output and error for one release; the same release renders once per run."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", prefix="jarvis-values-") as handle:
        handle.write(values)
        handle.flush()
        proc = subprocess.run(
            [_tool("helm"), "template", release_name, *chart, "--namespace", namespace, "--values", handle.name,
             "--kube-version", kube, "--include-crds", "--skip-tests"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
    return (proc.stdout, "") if proc.returncode == 0 else ("", proc.stderr.strip())


def env_objects(root: Path, env: str) -> tuple[list[dict], list[str], str]:
    """Every object the env's Flux paths apply, its charts rendered, and the env's k3s version."""
    values = yaml.safe_load((root / "envs" / env / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")) or {}
    k3s = release(values)[0]
    tree = env_tree(root, f"envs/{env}/flux")
    if tree.error:
        return [], [tree.error], k3s
    problems = [f"{p.kustomization['metadata']['name']}: {p.error}" for p in tree.paths if p.error]
    docs = tree.env_docs + [doc for path in tree.paths for doc in path.docs]
    sources = {_key(doc): doc for doc in docs if doc.get("kind") in ("OCIRepository", "HelmRepository")}
    rendered: list[dict] = []
    for doc in docs:
        if doc.get("kind") != "HelmRelease":
            continue
        _, namespace, name = _key(doc)
        spec = doc.get("spec") or {}
        chart, error = _chart(doc, sources)
        if spec.get("valuesFrom"):
            error = "valuesFrom is read on the cluster, so its chart cannot be rendered here"
        if not error:
            out, error = _template(
                str(spec.get("releaseName") or name), str(spec.get("targetNamespace") or namespace), tuple(chart),
                yaml.safe_dump(spec.get("values") or {}), k3s.split("+")[0],
            )  # fmt: skip
            rendered += [item for item in yaml.load_all(out, Loader=_LOADER) if isinstance(item, dict)]
        if error:
            problems.append(f"HelmRelease {namespace}/{name}: {error}")
    return docs + rendered, problems, k3s


def _forbid_unknown_fields(node: object) -> None:
    """kubeconform -strict does not close CRD objects, so close every object that lists properties."""
    if isinstance(node, dict):
        if "properties" in node and "additionalProperties" not in node and not node.get("x-kubernetes-preserve-unknown-fields"):
            node["additionalProperties"] = False
        for value in node.values():
            _forbid_unknown_fields(value)
    elif isinstance(node, list):
        for value in node:
            _forbid_unknown_fields(value)


def _crd_schemas(docs: list[dict], out: Path) -> None:
    """A kubeconform schema location from the CRDs among docs and those the flux CLI installs."""
    proc = subprocess.run([_tool("flux"), "install", "--export"], capture_output=True, text=True, check=False)
    if proc.returncode:
        raise RuntimeError(f"flux install --export failed: {proc.stderr.strip()}")
    flux = proc.stdout
    crds = [doc for doc in [*docs, *yaml.load_all(flux, Loader=_LOADER)] if isinstance(doc, dict) and doc.get("kind") == "CustomResourceDefinition"]
    for crd in crds:
        spec = crd["spec"]
        for version in spec.get("versions") or []:
            schema = (version.get("schema") or {}).get("openAPIV3Schema")
            if not schema:
                continue
            _forbid_unknown_fields(schema)
            path = out / spec["group"] / f"{spec['names']['kind'].lower()}_{version['name']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(schema), encoding="utf-8")


def kubeconform(docs: list[dict], k3s: str) -> list[str]:
    """Each object against its schema, strictly. CRDs only give schemas; sealed Secrets are left out
    (their data is SOPS's)."""
    kube = kubernetes_version_from_k3s(k3s)
    if kube is None:
        return [f"k3s version {k3s} names no Kubernetes release"]
    with tempfile.TemporaryDirectory(prefix="jarvis-schemas-") as work:
        _crd_schemas(docs, Path(work))
        docs = [doc for doc in docs if doc.get("kind") != "CustomResourceDefinition" and not (doc.get("kind") == "Secret" and "sops" in doc)]
        proc = subprocess.run(
            [_tool("kubeconform"), "-strict", "-output", "json", "-kubernetes-version", kube,
             "-cache", str(_schema_cache()),
             "-schema-location", "default",
             "-schema-location", work + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
             "-schema-location", CATALOG],
            input=yaml.safe_dump_all(docs, sort_keys=False), capture_output=True, text=True, check=False,
        )  # fmt: skip
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [proc.stderr.strip() or "kubeconform printed no report"]
    problems = [f"{item['kind']} {item['name']}: {item['msg']}" for item in report.get("resources") or []]
    return problems or ([proc.stderr.strip() or "kubeconform failed"] if proc.returncode else [])


@functools.cache
def _ruled_groups() -> frozenset[str]:
    """The API groups the rules match. The CLI's time grows with every object it reads, so it reads
    only objects in these groups; the CLI itself matches kinds within them."""
    rules = [doc for doc in yaml.safe_load_all(RULES.read_text(encoding="utf-8")) if isinstance(doc, dict)]
    return frozenset(
        group for rule in rules for match in rule["spec"]["matchConstraints"]["resourceRules"] for group in match["apiGroups"]
    )


def policies(docs: list[dict]) -> list[str]:
    """Each object against core's rules, with the Kyverno CLI."""
    docs = [doc for doc in docs if str(doc.get("apiVersion", "")).rpartition("/")[0] in _ruled_groups()]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", prefix="jarvis-objects-") as handle:
        yaml.safe_dump_all(docs, handle, sort_keys=False)
        handle.flush()
        proc = subprocess.run(
            [_tool("kyverno"), "apply", str(RULES), "--exception", str(RULE_EXCEPTIONS), "--resource", handle.name,
             "--policy-report", "--output-format", "json", "--remove-color"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
    start = proc.stdout.find("{")
    try:
        report = json.loads(proc.stdout[start:]) if start >= 0 else {}
    except json.JSONDecodeError:
        report = {}
    if "results" not in report:
        return [(proc.stderr or proc.stdout).strip() or "kyverno printed no report"]
    found = []
    for result in report["results"]:
        if result.get("result") in ("fail", "error"):
            for res in result.get("resources") or [{}]:
                where = "/".join(part for part in (res.get("namespace"), res.get("kind"), res.get("name")) if part)
                found.append(f"{result.get('policy')}: {where}: {result.get('message')}")
    return sorted(found)


def _each_env(instance: Path, check) -> list[str]:
    problems: list[str] = []
    for env_dir in env_dirs(instance):
        try:
            docs, build_problems, k3s = env_objects(instance, env_dir.name)
            found = [*build_problems, *check(docs, k3s)]
        except (OSError, RuntimeError, yaml.YAMLError) as exc:
            found = [str(exc)]
        problems += [f"envs/{env_dir.name}: {line}" for line in found]
    return problems


def scan_kubeconform(instance: Path) -> list[str]:
    return _each_env(instance, kubeconform)


def scan_policies(instance: Path) -> list[str]:
    return _each_env(instance, lambda docs, _k3s: policies(docs))
