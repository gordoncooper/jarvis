"""Flux Git entry: kustomize render, schema validation, and policy checks."""

from __future__ import annotations

import atexit
import copy
import functools
import json
import posixpath
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import (
    FLUX_INSTANCE_NAME,
    FLUX_OPERATOR_ROLE_DIR,
    PLATFORM_CONTROLLER_HEALTH_CHECKS,
    REPO_ROOT,
    env_name,
    example_flux_entry,
    load_cluster_vars,
    load_role_defaults,
    standard_flux_kustomization_paths,
)

from jarvis.lib.render_cluster_vars_configmap import (
    CLUSTER_VARS_PUBLISHED,
    cluster_vars_configmap,
    render_document,
)
from jarvis.schema.env import EXAMPLE_ENV, kubernetes_version_from_k3s
from support import ExampleCopy, _kustomize_build, real_and_copy_envs

_FLUX_KUSTOMIZATION_API = "kustomize.toolkit.fluxcd.io/"
_STRICT_SUBSTITUTION = "--feature-gates=StrictPostBuildSubstitutions=true"

# datreeio/CRDs-catalog commit. Tags through v0.0.12 omit these Flux CRDs.
_FLUX_SCHEMA_REF = "3eb90d47aba8d61919d4419dd38301536e32a94b"
FLUX_SCHEMA = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/"
    f"{_FLUX_SCHEMA_REF}/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
# Local schemas searched before the catalogs. This directory holds the
# CustomResourceDefinition schema. HelmChartConfig is not here, so the pinned
# catalog validates that kind.
FIXTURE_SCHEMA = (
    str(REPO_ROOT / "ci/tests/fixtures/schemas")
    + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
_KUBECONFORM_SKIPPED = re.compile(r"Skipped:\s*(\d+)")
# The catalog's VolSync schema predates moverVolumes, so VolSync kinds are checked
# against the CRDs of the pinned chart version, fetched from upstream and converted here.
_VOLSYNC_CRD = "https://raw.githubusercontent.com/backube/volsync/v{version}/config/crd/bases/volsync.backube_{plural}.yaml"
_VOLSYNC_KINDS = {"replicationsource": "replicationsources", "replicationdestination": "replicationdestinations"}


def _forbid_unknown_fields(node: object) -> None:
    """kubeconform -strict does not close CRD objects, so close every object that lists properties."""
    if isinstance(node, dict):
        if "properties" in node and "additionalProperties" not in node and not node.get(
            "x-kubernetes-preserve-unknown-fields"
        ):
            node["additionalProperties"] = False
        for value in node.values():
            _forbid_unknown_fields(value)
    elif isinstance(node, list):
        for value in node:
            _forbid_unknown_fields(value)


@functools.cache
def _volsync_schema_location() -> str:
    """A kubeconform schema location holding the pinned VolSync CRDs as JSON schemas."""
    helmrelease = yaml.safe_load(
        (REPO_ROOT / "platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
    )
    version = helmrelease["spec"]["chart"]["spec"]["version"]
    out = Path(tempfile.mkdtemp(prefix="volsync-schemas-"))
    atexit.register(shutil.rmtree, out, ignore_errors=True)
    (out / "volsync.backube").mkdir()
    for kind, plural in _VOLSYNC_KINDS.items():
        url = _VOLSYNC_CRD.format(version=version, plural=plural)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                crd = yaml.safe_load(response.read())
        except (OSError, yaml.YAMLError) as exc:
            pytest.fail(f"cannot fetch the VolSync CRD {url}: {exc}")
        for served in crd["spec"]["versions"]:
            schema = out / "volsync.backube" / f"{kind}_{served['name']}.json"
            body = served["schema"]["openAPIV3Schema"]
            _forbid_unknown_fields(body)
            schema.write_text(json.dumps(body), encoding="utf-8")
    return str(out) + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (run make deps)")
    return path


def _kubeconform_validate(manifests: str, kubernetes_version: str) -> None:
    kubeconform = _require_tool("kubeconform")
    proc = subprocess.run(
        [
            kubeconform,
            "-strict",
            "-summary",
            "-kubernetes-version",
            kubernetes_version,
            "-schema-location",
            "default",
            "-schema-location",
            FIXTURE_SCHEMA,
            "-schema-location",
            _volsync_schema_location(),
            "-schema-location",
            FLUX_SCHEMA,
        ],
        input=manifests,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    skipped = _KUBECONFORM_SKIPPED.search(output)
    assert skipped is not None, output
    assert int(skipped.group(1)) == 0, output


def _flux_kustomizations(env: str, *, root: Path | None = None) -> list[dict]:
    """The layer Kustomizations the env entry applies, from its kustomize build.

    The root Kustomization in sync.yaml is not a layer; the env schema checks it. Nor is a hosted
    app's wiring, which the env renders from its app list; ci/checks/app-hosting checks those.
    """
    from jarvis.checks.manifests import env_flux_docs

    docs, error = env_flux_docs(REPO_ROOT if root is None else root, f"envs/{env}/flux")
    assert error is None, error
    return [
        doc
        for doc in docs
        if doc.get("kind") == "Kustomization"
        and str(doc.get("apiVersion", "")).startswith(_FLUX_KUSTOMIZATION_API)
        and doc["metadata"]["name"] != "flux-system"
        and not doc["metadata"]["name"].endswith("-wiring")
    ]


def _flux_kustomization_errors(docs: list[dict], env: str) -> list[str]:
    """The three standard Kustomizations, in dependsOn order, with their paths.

    platform-controllers carries the HelmRelease health checks and a
    timeout. No other Kustomization carries health checks.
    """
    expected = standard_flux_kustomization_paths(env)
    chain = list(expected)
    names = [doc["metadata"]["name"] for doc in docs]
    errors = []
    if sorted(names) != sorted(chain):
        errors.append(f"Flux Kustomizations are {sorted(names)}, expected {sorted(chain)}")
    by_name = {doc["metadata"]["name"]: doc["spec"] for doc in docs}
    expected_checks = [check.flux_object() for check in PLATFORM_CONTROLLER_HEALTH_CHECKS]
    for index, name in enumerate(chain):
        spec = by_name.get(name)
        if spec is None:
            continue
        if spec.get("path") != expected[name]:
            errors.append(f"{name} path is {spec.get('path')!r}, expected {expected[name]!r}")
        depends = [] if index == 0 else [{"name": chain[index - 1]}]
        if (spec.get("dependsOn") or []) != depends:
            errors.append(f"{name} dependsOn is {spec.get('dependsOn')!r}, expected {depends!r}")
        health = spec.get("healthChecks") or []
        if name == "platform-controllers":
            if health != expected_checks:
                errors.append(
                    f"{name} healthChecks are {health!r}, expected {expected_checks!r}"
                )
            if not spec.get("timeout"):
                errors.append(f"{name} timeout is unset")
        elif health:
            errors.append(f"{name} carries healthChecks")
    return errors


def _platform_helmreleases() -> dict[str, str]:
    """HelmRelease name to metadata namespace, for objects under platform/."""
    found: dict[str, str] = {}
    for path in sorted((REPO_ROOT / "platform").rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") != "HelmRelease":
                continue
            metadata = doc.get("metadata")
            if not isinstance(metadata, dict):
                continue
            name = metadata.get("name")
            namespace = metadata.get("namespace")
            if isinstance(name, str) and isinstance(namespace, str):
                found[name] = namespace
    return found


def _helmrelease_gate_errors(found: dict[str, str]) -> list[str]:
    """Each health-check HelmRelease exists under platform/, in flux-system."""
    errors = []
    for check in PLATFORM_CONTROLLER_HEALTH_CHECKS:
        actual = found.get(check.name)
        if actual != check.namespace:
            errors.append(
                f"{check.name} health check namespace is {check.namespace!r}, "
                f"platform HelmRelease namespace is {actual!r}"
            )
    return errors


def _flux_instance(env: str, root: Path | None = None) -> dict:
    # Core's FluxInstance, at the core the env's root artifact holds (core/ in a copy).
    repo = REPO_ROOT if root is None else root
    core = repo / "core" if (repo / "core").is_dir() else repo
    path = core / "flux" / "flux-instance.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _flux_instance_errors(doc: dict, env: str, root: Path | None = None) -> list[str]:
    """Ansible applies this file once; it installs the controllers and syncs nothing."""
    role = load_role_defaults(FLUX_OPERATOR_ROLE_DIR)
    errors = []
    metadata = doc.get("metadata") or {}
    lookup = (role["flux_system_namespace"], FLUX_INSTANCE_NAME)
    if (metadata.get("namespace"), metadata.get("name")) != lookup:
        errors.append(f"metadata is not {lookup}, the flux-operator name")
    if "sync" in (doc.get("spec") or {}):
        errors.append("spec.sync is set; sync.yaml declares the sources")
    patches = doc.get("spec", {}).get("kustomize", {}).get("patches") or []
    if not any(
        _STRICT_SUBSTITUTION in str(patch.get("patch"))
        and patch.get("target") == {"kind": "Deployment", "name": "kustomize-controller"}
        for patch in patches
    ):
        errors.append("Deployment kustomize-controller does not run StrictPostBuildSubstitutions")
    return errors


def _platform_configs_errors(doc: dict, env: str) -> list[str]:
    """The env's platform-configs path is platform/configs plus that env's SOPS secrets,
    and the opt-in off-host copy. Its components (app hosting, app backup registrations) are
    env-schema's."""
    from jarvis.schema.env import CORE_SOURCE_DIR, OFFSITE_COPY_DIR

    base = f"envs/{env}/flux/platform-configs"
    resources = [posixpath.normpath(posixpath.join(base, item)) for item in doc.get("resources") or []]
    configs = f"{CORE_SOURCE_DIR}/platform/configs"
    errors = []
    keys = sorted(set(doc) - {"apiVersion", "kind", "resources", "components"})
    if keys:
        errors.append(f"keys other than resources and components: {keys}")
    if resources.count(configs) != 1:
        errors.append(f"{configs} is not listed exactly once")
    others = [
        item
        for item in resources
        if item not in (configs, f"{CORE_SOURCE_DIR}/{OFFSITE_COPY_DIR}")
        and not (posixpath.dirname(item) == f"envs/{env}/secrets" and item.endswith(".sops.yaml"))
    ]
    if others:
        errors.append(f"not platform/configs or an env SOPS secret: {others}")
    return errors


def test_cluster_vars_configmap_matches_render_script(example_copy: ExampleCopy) -> None:
    """The committed ConfigMap is the render script's output, including env-00."""
    targets = [(REPO_ROOT, EXAMPLE_ENV), *real_and_copy_envs(example_copy)]
    for root, env in targets:
        cluster_path = root / "envs" / env / "values" / "cluster-vars.yaml"
        generated_path = root / "envs" / env / "flux" / "generated" / "cluster-vars-configmap.yaml"
        assert generated_path.read_text(encoding="utf-8") == render_document(cluster_path)


def test_cluster_vars_configmap_publishes_only_the_allow_list() -> None:
    """The live ConfigMap is the allow-list. Host-only keys stay in the values file."""
    raw = load_cluster_vars()
    data = cluster_vars_configmap(raw)["data"]
    assert isinstance(data, dict)
    assert set(data) == set(CLUSTER_VARS_PUBLISHED)
    assert data["jarvis_env"] == raw["jarvis_env"]
    assert "ssh_user" not in data
    assert "ntp_pools" not in data


def test_cluster_vars_configmap_flattens_ntp_pools() -> None:
    """A list value is JSON-encoded. ntp_pools is host-only, so the proof uses a published key."""
    raw = load_cluster_vars()
    pools = raw["ntp_pools"]
    assert isinstance(pools, list) and pools
    published = cluster_vars_configmap(raw)["data"]
    assert "ntp_pools" not in published
    data = cluster_vars_configmap({**raw, "dns_suffix": pools})["data"]
    assert data["dns_suffix"] == json.dumps(pools, separators=(",", ":"))


def test_flux_instance_syncs_its_env_from_cluster_vars(example_copy: ExampleCopy) -> None:
    """The committed FluxInstance is the one Ansible applies."""
    for root, env in real_and_copy_envs(example_copy):
        assert _flux_instance_errors(_flux_instance(env, root), env, root) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (("spec", "sync"), {"kind": "GitRepository", "url": "https://example.invalid/other"}),
        (("metadata", "name"), "other"),
        (("spec", "kustomize", "patches"), []),
        (("spec", "kustomize", "patches", 0, "target", "kind"), "StatefulSet"),
    ],
)
def test_flux_instance_drift_from_cluster_vars_fails(
    tmp_path: Path, field: tuple[str | int, ...], value: object
) -> None:
    entry = example_flux_entry(tmp_path)
    assert _flux_instance_errors(entry.instance, entry.env) == []
    doc = copy.deepcopy(entry.instance)
    parent = doc
    for key in field[:-1]:
        parent = parent[key]
    parent[field[-1]] = value
    assert _flux_instance_errors(doc, entry.env)


def test_flux_entry_has_the_four_standard_kustomizations(example_copy: ExampleCopy) -> None:
    """Four layers, one path each, chained with dependsOn, HelmRelease health checks on platform-controllers."""
    for root, env in real_and_copy_envs(example_copy):
        assert _flux_kustomization_errors(_flux_kustomizations(env, root=root), env) == []


def _drop_services(docs: list[dict]) -> None:
    docs[:] = [doc for doc in docs if doc["metadata"]["name"] != "services"]


def _add_extra(docs: list[dict]) -> None:
    extra = copy.deepcopy(docs[-1])
    extra["metadata"]["name"] = "extra"
    docs.append(extra)


def _move_platform_configs(docs: list[dict]) -> None:
    next(d for d in docs if d["metadata"]["name"] == "platform-configs")["spec"]["path"] = (
        "./core/platform/configs"
    )


def _skip_platform_configs(docs: list[dict]) -> None:
    next(d for d in docs if d["metadata"]["name"] == "services")["spec"]["dependsOn"] = [
        {"name": "platform-controllers"}
    ]


@pytest.mark.parametrize(
    "mutate", [_drop_services, _add_extra, _move_platform_configs, _skip_platform_configs]
)
def test_nonstandard_flux_kustomizations_fail(tmp_path: Path, mutate) -> None:
    entry = example_flux_entry(tmp_path)
    assert _flux_kustomization_errors(entry.kustomizations, entry.env) == []
    docs = copy.deepcopy(entry.kustomizations)
    mutate(docs)
    assert _flux_kustomization_errors(docs, entry.env)


def _controllers_spec(docs: list[dict]) -> dict:
    return next(doc for doc in docs if doc["metadata"]["name"] == "platform-controllers")["spec"]


def _drop_one_health_check(docs: list[dict]) -> None:
    _controllers_spec(docs)["healthChecks"] = _controllers_spec(docs)["healthChecks"][:1]


def _add_unlisted_health_check(docs: list[dict]) -> None:
    _controllers_spec(docs)["healthChecks"] = [
        *_controllers_spec(docs)["healthChecks"],
        {
            "apiVersion": "helm.toolkit.fluxcd.io/v2",
            "kind": "HelmRelease",
            "name": "k8s-gateway",
            "namespace": "flux-system",
        },
    ]


def _drop_timeout(docs: list[dict]) -> None:
    del _controllers_spec(docs)["timeout"]


def _health_check_on_platform_configs(docs: list[dict]) -> None:
    next(doc for doc in docs if doc["metadata"]["name"] == "platform-configs")["spec"][
        "healthChecks"
    ] = [PLATFORM_CONTROLLER_HEALTH_CHECKS[0].flux_object()]


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_one_health_check,
        _add_unlisted_health_check,
        _drop_timeout,
        _health_check_on_platform_configs,
    ],
)
def test_health_checks_are_exactly_the_listed_helmreleases(tmp_path: Path, mutate) -> None:
    """platform-controllers waits for cert-manager, Kyverno and VolSync, and only those."""
    entry = example_flux_entry(tmp_path)
    assert _flux_kustomization_errors(entry.kustomizations, entry.env) == []
    docs = copy.deepcopy(entry.kustomizations)
    mutate(docs)
    assert _flux_kustomization_errors(docs, entry.env)


def test_health_check_helmreleases_exist_under_platform() -> None:
    """Each platform-controllers HelmRelease check exists under platform/, in flux-system."""
    found = _platform_helmreleases()
    assert _helmrelease_gate_errors(found) == []
    missing = dict(found)
    missing.pop(PLATFORM_CONTROLLER_HEALTH_CHECKS[0].name, None)
    assert _helmrelease_gate_errors(missing)


def test_env_00_deployment_health_check_fails() -> None:
    """A Deployment check in place of a HelmRelease check fails on the example flux tree."""
    docs = _flux_kustomizations(EXAMPLE_ENV)
    assert _flux_kustomization_errors(docs, EXAMPLE_ENV) == []
    _controllers_spec(docs)["healthChecks"][0] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "cert-manager-webhook",
        "namespace": "platform-cert-manager",
    }
    assert _flux_kustomization_errors(docs, EXAMPLE_ENV)


def test_env_platform_configs_adds_only_its_secrets(example_copy: ExampleCopy) -> None:
    """Core components join platform/configs, not each env's list."""
    for root, env in real_and_copy_envs(example_copy):
        path = root / "envs" / env / "flux" / "platform-configs" / "kustomization.yaml"
        assert _platform_configs_errors(yaml.safe_load(path.read_text(encoding="utf-8")), env) == []


@pytest.mark.parametrize(
    "seed",
    [
        {"resources": ["../../../../core/platform/configs", "../../../../core/platform/testapp"]},
        {"resources": ["../../../../core/platform/configs", "../../../env-other/secrets/x.sops.yaml"]},
        {"resources": ["../../secrets/x.sops.yaml"]},
        {"resources": ["../../../../core/platform/configs"], "patches": []},
    ],
)
def test_env_platform_configs_with_more_than_its_secrets_fails(seed: dict) -> None:
    env = env_name()
    clean = {"resources": ["../../../../core/platform/configs", "../../secrets/x.sops.yaml"]}
    assert _platform_configs_errors(clean, env) == []
    assert _platform_configs_errors(seed, env)


def test_platform_configs_decryption_uses_sops_age(example_copy: ExampleCopy) -> None:
    """platform-configs decrypts with the sops-age secret."""
    for root, env in real_and_copy_envs(example_copy):
        _assert_platform_configs_decrypts(env, root)


def _assert_platform_configs_decrypts(env: str, root: Path | None = None) -> None:
    doc = next(
        d for d in _flux_kustomizations(env, root=root) if d["metadata"]["name"] == "platform-configs"
    )
    decryption = doc["spec"]["decryption"]
    assert decryption["provider"] == "sops"
    assert decryption["secretRef"]["name"] == "sops-age"


def test_helmrelease_substitute_disabled() -> None:
    path = REPO_ROOT / "platform" / "flux-operator" / "helmrelease.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert (
        doc["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/substitute"]
        == "disabled"
    )


def _assert_sops_leaf(path: Path, section: str, node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_sops_leaf(path, f"{section}.{key}", value)
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            _assert_sops_leaf(path, f"{section}[{index}]", value)
        return
    if isinstance(node, str) and node.startswith("ENC["):
        return
    raise AssertionError(f"{path}: {section} is not an ENC value")


def test_sops_secret_leaves_stay_encrypted(example_copy: ExampleCopy) -> None:
    """SOPS secret leaves stay encrypted in every real env and in a bootstrapped copy."""
    paths = sorted(REPO_ROOT.glob("envs/*/secrets/**/*.sops.yaml"))
    # An add-on's Secrets sit in its namespace's folder.
    paths.extend(sorted((example_copy.env_dir / "secrets").rglob("*.sops.yaml")))
    assert any(path.parent != example_copy.env_dir / "secrets" for path in paths)
    assert paths
    for path in paths:
        docs = [
            doc
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if doc is not None
        ]
        assert docs, path
        for doc in docs:
            assert isinstance(doc, dict), path
            assert isinstance(doc.get("sops"), dict), path
            sections = [key for key in ("data", "stringData") if key in doc]
            assert sections, path
            for key in sections:
                _assert_sops_leaf(path, key, doc[key])


def test_secret_scan_rejects_planted_age_key(tmp_path: Path) -> None:
    gitleaks = Path.home() / ".local" / "bin" / "gitleaks"
    age_keygen = shutil.which("age-keygen")
    if not gitleaks.is_file() or age_keygen is None:
        pytest.fail("gitleaks and age-keygen are required (run make deps)")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [age_keygen, "-o", str(tmp_path / "key.txt")],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "key.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "user.name=fixture",
            "commit",
            "-m",
            "plant",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    proc = subprocess.run(
        [str(gitleaks), "detect", "--source", str(tmp_path), "--redact"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0


def _object_identity(doc: object) -> tuple[str, str, str] | None:
    if not isinstance(doc, dict) or "kind" not in doc:
        return None
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return None
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        return None
    namespace = metadata.get("namespace")
    if not isinstance(namespace, str):
        namespace = ""
    kind = doc["kind"]
    assert isinstance(kind, str)
    return (kind, namespace, name)


def _validate_platform_configs_without_sops_secrets(
    manifests: str, kubernetes_version: str
) -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(manifests)
        if isinstance(doc, dict) and not (doc.get("kind") == "Secret" and "sops" in doc)
    ]
    rendered = yaml.safe_dump_all(docs, sort_keys=False)
    _kubeconform_validate(rendered, kubernetes_version)


def _record_object_identities(
    output: str, layer: str, seen: dict[tuple[str, str, str], str]
) -> None:
    for doc in yaml.safe_load_all(output):
        ident = _object_identity(doc)
        if ident is None:
            continue
        owner = seen.get(ident)
        assert owner is None, f"{ident} rendered by both {owner} and {layer}"
        seen[ident] = layer


def test_helmchartconfig_schema_accepts_upstream_fields_only() -> None:
    """The pinned catalog accepts upstream spec fields. A misspelled spec key fails."""
    kubeconform = _require_tool("kubeconform")
    version = kubernetes_version_from_k3s(load_cluster_vars()["k3s_version"])
    good = """\
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
  labels:
    app: traefik
  annotations:
    example: "1"
spec:
  valuesContent: "a: b\\n"
  failurePolicy: abort
"""

    def run(text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                kubeconform,
                "-strict",
                "-kubernetes-version",
                version,
                "-schema-location",
                FLUX_SCHEMA,
                "-",
            ],
            input=text,
            capture_output=True,
            text=True,
            check=False,
        )

    passed = run(good)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    failed = run(good.replace("failurePolicy", "failurePollicy"))
    assert failed.returncode != 0, failed.stdout + failed.stderr


def test_kubernetes_version_matches_k3s_pin() -> None:
    assert kubernetes_version_from_k3s("v9.8.7+k3s2") == "9.8.0"
    assert kubernetes_version_from_k3s("v9.8.7") is None


@pytest.mark.parametrize("kind", ["ReplicationSource", "ReplicationDestination"])
def test_volsync_spec_is_checked_against_the_upstream_crd(kind: str) -> None:
    """A VolSync spec field the pinned CRD does not know fails; a known one (moverVolumes) passes."""
    version = kubernetes_version_from_k3s(load_cluster_vars()["k3s_version"])
    doc = {
        "apiVersion": "volsync.backube/v1alpha1",
        "kind": kind,
        "metadata": {"name": "x", "namespace": "x"},
        "spec": {"trigger": {"manual": "once"}} if kind == "ReplicationDestination" else {"sourcePVC": "x"},
    }
    doc["spec"]["restic"] = {"repository": "x", "moverVolumes": []}
    _kubeconform_validate(yaml.safe_dump(doc), version)
    doc["spec"]["notAField"] = True
    with pytest.raises(AssertionError):
        _kubeconform_validate(yaml.safe_dump(doc), version)


def validate_env_flux(root: Path, env: str) -> None:
    """Kustomize-build the env flux entry and each Flux path, then kubeconform.

    Each stage is built with its components, and each add-on Kustomization on the
    services stage's path is built too, as on the cluster, with its postBuild values. A hosted
    app's own Kustomizations read the apps repository, which is not in this checkout.
    """
    from jarvis.checks.manifests import ROOT_SOURCE, env_flux_docs, flux_paths, postbuild_values, substitute

    env_rel = f"envs/{env}/flux"
    env_docs, error = env_flux_docs(root, env_rel)
    assert error is None, error
    paths = flux_paths(root, env_rel, env_docs)
    assert paths
    pin = load_cluster_vars(root, env=env)["k3s_version"]
    assert isinstance(pin, str)
    version = kubernetes_version_from_k3s(pin)
    assert version is not None
    seen: dict[tuple[str, str, str], str] = {}
    for path in paths:
        name = path.kustomization["metadata"]["name"]
        source = (path.kustomization.get("spec") or {}).get("sourceRef") or {}
        if source.get("name", ROOT_SOURCE) != ROOT_SOURCE:
            continue
        assert path.error is None and not path.partial, f"{name}: {path.error or 'partial build'}"
        output = yaml.safe_dump_all(
            substitute(path.docs, postbuild_values(path.kustomization, env_docs)), sort_keys=False
        )
        _record_object_identities(output, name, seen)
        if isinstance((path.kustomization.get("spec") or {}).get("decryption"), dict):
            _validate_platform_configs_without_sops_secrets(output, version)
        else:
            _kubeconform_validate(output, version)
    # The env flux directory holds these Kustomizations. It is not one of their paths.
    _kubeconform_validate(_kustomize_build(f"envs/{env}/flux", root=root), version)


def test_flux_layer_kustomize_build_and_schema_validate(example_copy: ExampleCopy) -> None:
    """Each full env's Flux paths build and pass kubeconform, including a copy of env-00."""
    for root, env in real_and_copy_envs(example_copy):
        validate_env_flux(root, env)
