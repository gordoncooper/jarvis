"""ci/checks/app-hosting, the env's app list in env-schema, and the app wiring in flux-identity."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT

from _app_hosting import scan_app_hosting
from _flux_identity import scan_flux_identity
import _apps
from schema import env as env_schema

EXAMPLE = REPO_ROOT / "envs" / "env-00"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A product copy (platform, services, apps) and an instance holding env-00 with its apps."""
    product, instance = tmp_path / "product", tmp_path / "instance"
    for name in ("platform", "services", "apps"):
        shutil.copytree(REPO_ROOT / name, product / name)
    (instance / "envs").mkdir(parents=True)
    shutil.copytree(EXAMPLE, instance / "envs" / "env-00")
    (instance / "core").symlink_to(product)
    return product, instance


def _messages(instance: Path) -> list[str]:
    return [f"{item.path}: {item.message}" for item in scan_app_hosting(instance)]


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_listed_apps_with_core_wiring_pass(tmp_path: Path) -> None:
    """Rule: the example env's three apps, wired by core's template, pass."""
    _product, instance = _tree(tmp_path)
    assert _messages(instance) == []


_APP = "platform/app-hosting/app"


@pytest.mark.parametrize(
    ("file", "old", "new", "expected"),
    [
        (f"{_APP}/baseline.yaml", "  policyTypes: [Ingress, Egress]\n---", "  policyTypes: [Ingress]\n---", "default-deny selects every pod"),
        (f"{_APP}/baseline.yaml", "name: default-deny", "name: deny-later", "default-deny selects every pod"),
        (f"{_APP}/namespace.yaml", "hosting.jarvis.io/app: ${app_name}", "hosting.jarvis.io/app: other", "label hosting.jarvis.io/app"),
        (f"{_APP}/namespace.yaml", "    kustomize.toolkit.fluxcd.io/prune: disabled\n", "", "could delete its volumes"),
        (f"{_APP}/admission.yaml", "parameterNotFoundAction: Deny", "parameterNotFoundAction: Allow", "refusing when they are missing"),
        (f"{_APP}/admission.yaml", "host-path: \"${app_grant_host_path}\"", "host-path: \"true\"", "not the env's exception lines"),
        (f"{_APP}/tenant.yaml", "resources: [jobs, cronjobs]", "resources: [jobs, cronjobs, networkpolicies]", "batch/networkpolicies"),
        (f"{_APP}/tenant.yaml", "  - apiGroups: [hosting.jarvis.io]", "  - apiGroups: [traefik.io]\n    resources: [ingressroutes]\n    verbs: [create]\n  - apiGroups: [hosting.jarvis.io]", "traefik.io/ingressroutes"),
        (f"{_APP}/flux.yaml", "  targetNamespace: app-${app_name}\n  sourceRef:\n    kind: GitRepository\n    name: app-${app_name}-file", "  targetNamespace: default\n  sourceRef:\n    kind: GitRepository\n    name: app-${app_name}-file", "objects land in app-"),
        (f"{_APP}/flux.yaml", "    substitute:\n      app_host:", "    substituteFrom:\n      - kind: ConfigMap\n        name: cluster-vars\n    substitute:\n      app_host:", "no substitution source"),
        (f"{_APP}/flux.yaml", "  dependsOn:\n    - name: app-${app_name}-file", "  decryption:\n    provider: sops\n  dependsOn:\n    - name: app-${app_name}-file", "decrypts nothing"),
        (f"{_APP}/flux.yaml", "    name: app-${app_name}\n  dependsOn", "    name: flux-system\n  dependsOn", "reads its own source"),
        (f"{_APP}/tenant.yaml", "  name: app\n  namespace: app-${app_name}\n  annotations", "  name: app\n  namespace: kube-system\n  annotations", "lives in app-"),
        (f"{_APP}/kustomization.yaml", "  - flux.yaml", "  - flux.yaml\n  - extra.yaml", "holds no ClusterRole"),
    ],
    ids=["default-deny-ingress-only", "no-default-deny", "namespace-label", "namespace-prunable", "binding-allows-missing-params",
         "grant-without-env-line", "role-grants-networkpolicies", "role-grants-traefik", "objects-outside-namespace",
         "substitution-source", "decryption", "other-source", "role-elsewhere", "extra-kind"],
)
def test_app_wiring_outside_the_contract_fails(tmp_path: Path, file: str, old: str, new: str, expected: str) -> None:
    """Rule: each part of an app's wiring fails on its seeded breach."""
    product, instance = _tree(tmp_path)
    (product / _APP / "extra.yaml").write_text(
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: app-${app_name}\n", encoding="utf-8"
    )
    _edit(product / file, old, new)
    messages = _messages(instance)
    assert any(expected in item for item in messages), messages


def test_listed_app_without_wiring_and_wiring_without_listing_fail(tmp_path: Path) -> None:
    """Rule: the env runs exactly the apps it lists."""
    _product, instance = _tree(tmp_path)
    values = instance / "envs" / "env-00" / _apps.APPS_VALUES
    _edit(values, "  - photos\n", "  - photos\n  - extra\n")
    _edit(values, "  - web-demo\n", "")
    messages = _messages(instance)
    assert any("lists extra, and the flux tree has no app-extra-wiring" in item for item in messages), messages
    assert any("wires app web-demo, which values/apps.yaml does not list" in item for item in messages), messages


def test_flux_identity_holds_the_wiring_to_core_template(tmp_path: Path) -> None:
    """Rule: only app-<name>-wiring applying core's app template runs as the platform account."""
    _product, instance = _tree(tmp_path)
    generated = instance / "envs" / "env-00" / "flux" / _apps.GENERATED_REL
    _edit(generated, "path: ./core/platform/app-hosting/app", "path: ./core/platform")
    messages = [item.message for item in scan_flux_identity(instance)]
    assert any("an app's wiring runs as flux-platform" in item for item in messages), messages


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"apps": ["Web_Demo"]}, "is not an app name"),
        ({"apps": ["a" * 60]}, "is not an app name"),
        ({"apps": ["web", "web"]}, "listed twice"),
        ({"apps": ["web"], "sizes": {"web": "big"}}, "unknown key 'sizes'"),
        ({"apps": ["web"], "exceptions": [{"web": "trust"}]}, "unknown grant"),
        ({"apps": ["web"], "exceptions": [{"Web!": "privileged"}]}, "is not an app name"),
        ({"apps": ["web"], "exceptions": [{"other": "privileged"}]}, "not on this env's app list"),
        ({"apps": ["web"], "exceptions": [{"web": "privileged", "x": "root"}]}, "is not one '<app>: <grant>' line"),
        ({"apps": ["web"], "exceptions": [{"web": "hostname grafana.env-00.example"}]}, "outside env-00.example"),
        ({"apps": ["web"], "exceptions": [{"web": "lan-port 70000/TCP"}]}, "lan-port takes"),
        ({"apps": ["web"], "exceptions": [{"web": "privileged"}, {"web": "privileged"}]}, "listed twice"),
    ],
    ids=["not-a-label", "too-long", "duplicate", "size-in-list", "unknown-grant", "bad-exception-name", "unlisted-app",
         "two-grants-one-line", "hostname-in-zone", "bad-lan-port", "duplicate-line"],
)
def test_env_app_list_rejects_what_it_does_not_hold(values: dict, expected: str) -> None:
    """Rule: the app list holds names only, and each exception line one known grant for a listed app."""
    errors = _apps.validate(values, "env-00.example")
    assert any(expected in error for error in errors), errors


def test_env_app_list_and_grants_that_fit_pass() -> None:
    """Rule: each known grant on a listed app passes."""
    lines = [{"web": grant} for grant in ("privileged", "root", "host-network", "host-path", "hostname photos.example.org", "lan-port 8443/TCP")]
    assert _apps.validate({"apps": ["web"], "exceptions": lines}, "env-00.example") == []


def _schema_errors(instance: Path) -> list[str]:
    env_dir = instance / "envs" / "env-00"
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    return env_schema._validate_apps(env_dir, instance, cluster_vars, pre_host=False)


def test_env_schema_requires_the_committed_render(tmp_path: Path) -> None:
    """Rule: an env that lists apps commits the render and lists it; one that lists none has neither."""
    _product, instance = _tree(tmp_path)
    env_dir = instance / "envs" / "env-00"
    assert _schema_errors(instance) == []
    _edit(env_dir / _apps.APPS_VALUES, "  - photos: host-path\n", "  - photos: privileged\n")
    assert any("does not match" in error for error in _schema_errors(instance))
    _edit(env_dir / _apps.APPS_VALUES, "  - photos: privileged\n", "  - photos: host-path\n")
    _edit(env_dir / "flux" / "kustomization.yaml", "  - generated/apps.yaml\n", "")
    assert any("resources lists generated/apps.yaml" in error for error in _schema_errors(instance))
    (env_dir / _apps.APPS_VALUES).write_text("apps: []\n", encoding="utf-8")
    assert any("the env lists no app; remove it" in error for error in _schema_errors(instance))
    (env_dir / "flux" / _apps.GENERATED_REL).unlink()
    assert _schema_errors(instance) == []


def test_env_schema_requires_the_apps_repository_for_an_app(tmp_path: Path) -> None:
    """Rule: an env that lists an app names the apps repository it reads."""
    _product, instance = _tree(tmp_path)
    _edit(instance / "envs" / "env-00" / "values" / "cluster-vars.yaml", "apps_git_repo_url:", "# apps_git_repo_url:")
    assert any("sets apps_git_repo_url" in error for error in _schema_errors(instance))
