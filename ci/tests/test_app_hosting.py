"""ci/checks/app-hosting, the env's app list in env-schema, and the app wiring in flux-identity."""

from __future__ import annotations

import os
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
        (f"{_APP}/namespace.yaml", "lan-all: \"${app_grant_lan_all}\"", "lan-all: \"true\"", "annotation hosting.jarvis.io/lan-all"),
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
         "grant-without-env-line", "lan-grant-on-namespace-without-env-line", "role-grants-networkpolicies", "role-grants-traefik", "objects-outside-namespace",
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
    _edit(values, "  - lan-demo\n", "  - lan-demo\n  - extra\n")
    _edit(values, "  - hello-web\n", "")
    messages = _messages(instance)
    assert any("lists extra, and the flux tree has no app-extra-wiring" in item for item in messages), messages
    assert any("wires app hello-web, which values/apps.yaml does not list" in item for item in messages), messages


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
        ({"apps": ["web"], "exceptions": [{"web": "lan-target nas 10.99.0.10/32 445/TCP"}]}, "outside the env's lan_cidrs"),
        ({"apps": ["web"], "exceptions": [{"web": "lan-target nas 203.0.113.0/24 443/TCP"}]}, "outside the env's lan_cidrs"),
        ({"apps": ["web"], "exceptions": [{"web": "lan-target all 10.200.0.0/24 445/TCP"}]}, "other than all"),
        ({"apps": ["web"], "exceptions": [{"web": "lan-target nas 10.200.0.10/32"}]}, "lan-target takes"),
    ],
    ids=["not-a-label", "too-long", "duplicate", "size-in-list", "unknown-grant", "bad-exception-name", "unlisted-app",
         "two-grants-one-line", "hostname-in-zone", "bad-lan-port", "duplicate-line", "lan-target-off-lan",
         "lan-target-internet", "lan-target-all", "lan-target-no-port"],
)
def test_env_app_list_rejects_what_it_does_not_hold(values: dict, expected: str) -> None:
    """Rule: the app list holds names only, and each exception line one known grant for a listed
    app; a lan-target range sits inside the env's LAN ranges, so it cannot open the internet."""
    errors = _apps.validate(values, "env-00.example", ["10.200.0.0/24"])
    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize("grant", ["lan-all", "lan-target nas 10.200.0.10/32 445/TCP"])
def test_lan_grant_needs_the_env_lan_ranges(grant: str) -> None:
    """Rule: an env that grants a LAN item names its LAN ranges (lan_cidrs)."""
    errors = _apps.validate({"apps": ["web"], "exceptions": [{"web": grant}]}, "env-00.example")
    assert any("set lan_cidrs" in error for error in errors), errors


def test_lan_target_against_an_invalid_lan_range_is_reported_not_raised() -> None:
    """Rule: a lan_cidrs entry that is no network (reported as lan_cidrs) holds no lan-target."""
    errors = _apps.validate({"apps": ["web"], "exceptions": [{"web": "lan-target nas 10.200.0.10/32 445/TCP"}]},
                            "env-00.example", ["10.200.0.1/24"])
    assert any("outside the env's lan_cidrs" in error for error in errors), errors


def test_env_app_list_and_grants_that_fit_pass() -> None:
    """Rule: each known grant on a listed app passes."""
    lines = [{"web": grant} for grant in ("privileged", "root", "host-network", "host-path", "hostname photos.example.org",
                                          "lan-port 8443/TCP", "lan-all", "lan-target nas 10.200.0.10/32 445/TCP",
                                          "lan-target nas 10.200.0.10/32 137/UDP", "no-uptime-check")]
    assert _apps.validate({"apps": ["web"], "exceptions": lines}, "env-00.example", ["10.200.0.0/24"]) == []


def _schema_errors(instance: Path) -> list[str]:
    env_dir = instance / "envs" / "env-00"
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    return env_schema._validate_apps(env_dir, instance, cluster_vars, pre_host=False)


def test_env_schema_requires_the_committed_render(tmp_path: Path) -> None:
    """Rule: an env that lists apps commits the render and lists it; one that lists none has neither."""
    _product, instance = _tree(tmp_path)
    env_dir = instance / "envs" / "env-00"
    assert _schema_errors(instance) == []
    _edit(env_dir / _apps.APPS_VALUES, "  - lan-demo: lan-port 8080/TCP\n", "  - lan-demo: privileged\n")
    assert any("does not match" in error for error in _schema_errors(instance))
    _edit(env_dir / _apps.APPS_VALUES, "  - lan-demo: privileged\n", "  - lan-demo: lan-port 8080/TCP\n")
    _edit(env_dir / "flux" / "kustomization.yaml", "  - generated/apps.yaml\n", "")
    assert any("resources lists generated/apps.yaml" in error for error in _schema_errors(instance))
    (env_dir / _apps.APPS_VALUES).write_text("apps: []\n", encoding="utf-8")
    assert any("the env lists no app; remove it" in error for error in _schema_errors(instance))
    (env_dir / "flux" / _apps.GENERATED_REL).unlink()
    assert _schema_errors(instance) == []


_APP_PIECES = {
    ("CustomResourceDefinition", "apps.hosting.jarvis.io"),
    ("ClusterRole", "kyverno:app-derive"),
    ("ValidatingAdmissionPolicy", "app-identity"),
    ("ValidatingAdmissionPolicy", "app-tenant"),
    ("ValidatingAdmissionPolicy", "app-file"),
    ("ValidatingAdmissionPolicy", "app-grants"),
    ("ValidatingAdmissionPolicyBinding", "app-identity"),
    ("ValidatingAdmissionPolicyBinding", "app-tenant"),
    ("ClusterPolicy", "app-derive"),
    ("Certificate", "wildcard-apps"),
}


def render_platform(tmp_path: Path, *, hosting: bool) -> dict[tuple[str, str], dict]:
    """What platform-controllers and platform-configs render, with or without the switch."""
    from support import _kustomize_build

    work = tmp_path / ("on" if hosting else "off")
    work.mkdir(parents=True)
    rel = lambda path: os.path.relpath(REPO_ROOT / path, work)  # noqa: E731
    listing = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
               "resources": [rel("platform"), rel("platform/configs")]}
    if hosting:
        listing["components"] = [rel("platform/app-hosting")]
    (work / "kustomization.yaml").write_text(yaml.safe_dump(listing), encoding="utf-8")
    docs = [doc for doc in yaml.safe_load_all(_kustomize_build(str(work), root=work)) if isinstance(doc, dict)]
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def _rule(policy: dict, name: str) -> dict:
    return next(rule for rule in policy["spec"]["rules"] if rule["name"] == name)


def test_app_hosting_off_renders_none_of_it(tmp_path: Path) -> None:
    """Rule: without the switch, an env gets no app-hosting object, no apps certificate in the
    TLSStore, and pvc-backup's privileged-movers as core has it."""
    off = render_platform(tmp_path, hosting=False)
    assert not _APP_PIECES & set(off)
    assert "certificates" not in off[("TLSStore", "default")]["spec"]
    enrollment = yaml.safe_load((REPO_ROOT / "platform/backups/enrollment.yaml").read_text(encoding="utf-8"))
    assert _rule(off[("ClusterPolicy", "pvc-backup")], "privileged-movers") == _rule(enrollment, "privileged-movers")
    assert "preconditions" not in _rule(enrollment, "privileged-movers")


def test_app_hosting_on_renders_all_of_it(tmp_path: Path) -> None:
    """Rule: with the switch, the env gets every app-hosting object, the apps certificate in the
    TLSStore, and privileged-movers gated on the app's credential; nothing else changes."""
    on, off = render_platform(tmp_path, hosting=True), render_platform(tmp_path, hosting=False)
    assert _APP_PIECES <= set(on)
    assert set(on) - _APP_PIECES == set(off)
    assert on[("TLSStore", "default")]["spec"]["certificates"] == [{"secretName": "wildcard-apps-tls"}]
    movers = _rule(on[("ClusterPolicy", "pvc-backup")], "privileged-movers")
    assert [entry["name"] for entry in movers["context"]] == ["appCredential"]
    assert len(movers["preconditions"]["any"]) == 2
    others = lambda policy: [rule for rule in policy["spec"]["rules"] if rule["name"] != "privileged-movers"]  # noqa: E731
    assert others(on[("ClusterPolicy", "pvc-backup")]) == others(off[("ClusterPolicy", "pvc-backup")])
    for key in set(off) - {("TLSStore", "default"), ("ClusterPolicy", "pvc-backup")}:
        assert on[key] == off[key], key


def test_env_schema_rejects_apps_while_hosting_is_off(tmp_path: Path) -> None:
    """Rule: an env that lists apps turns app hosting on; the list is not silently ignored."""
    _product, instance = _tree(tmp_path)
    configs = instance / "envs" / "env-00" / "flux" / "platform-configs" / "kustomization.yaml"
    _edit(configs, "components:\n  - ../../../../core/platform/app-hosting\n", "")
    assert any("app hosting is off" in error for error in _schema_errors(instance))


def test_env_schema_requires_the_apps_repository_for_an_app(tmp_path: Path) -> None:
    """Rule: an env that lists an app names the apps repository it reads."""
    _product, instance = _tree(tmp_path)
    _edit(instance / "envs" / "env-00" / "values" / "cluster-vars.yaml", "apps_git_repo_url:", "# apps_git_repo_url:")
    assert any("sets apps_git_repo_url" in error for error in _schema_errors(instance))
