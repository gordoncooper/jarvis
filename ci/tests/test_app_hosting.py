"""the app-hosting check (jarvis validate), the env's apps in env-schema, and the app wiring in flux-identity."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from jarvis.apps import values as _apps
from jarvis.checks.app_hosting import scan_app_hosting
from jarvis.checks.flux_identity import scan_flux_identity
from jarvis.env import switches as _switches
from jarvis.schema import env as env_schema
from support import REPO_ROOT, core_at, sample_root

FIXTURE_APPS = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"

EXAMPLE = sample_root() / "envs" / "env-00"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A product copy (platform, services) and an instance holding env-00 with its apps."""
    product, instance = tmp_path / "product", tmp_path / "instance"
    for name in ("core", "addons"):
        shutil.copytree(REPO_ROOT / name, product / name)
    (instance / "envs").mkdir(parents=True)
    shutil.copytree(EXAMPLE, instance / "envs" / "env-00")
    return product, instance


def _messages(instance: Path) -> list[str]:
    """The check on instance, against the product copy _tree made beside it."""
    with core_at(instance.parent / "product"):
        return [f"{item.path}: {item.message}" for item in scan_app_hosting(instance)]


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_listed_apps_with_core_wiring_pass(tmp_path: Path) -> None:
    """Rule: the example env's three apps, wired by core's template, pass."""
    _product, instance = _tree(tmp_path)
    assert _messages(instance) == []


_APP = "core/feature/app-hosting/app"
_POLICIES = "core/feature/app-hosting/policies/admission.yaml"


@pytest.mark.parametrize(
    ("file", "old", "new", "expected"),
    [
        (f"{_APP}/baseline.yaml", "  policyTypes: [Ingress, Egress]\n---", "  policyTypes: [Ingress]\n---", "default-deny selects every pod"),
        (f"{_APP}/namespace.yaml", "hosting.jarvis.io/app: ${app_name}", "hosting.jarvis.io/app: other", "label hosting.jarvis.io/app"),
        (f"{_APP}/namespace.yaml", "    kustomize.toolkit.fluxcd.io/prune: disabled\n", "", "could delete its volumes"),
        (_POLICIES, "parameterNotFoundAction: Deny", "parameterNotFoundAction: Allow", "refusing where params are missing"),
        (_POLICIES, "  name: app-grants\n  annotations:\n    kustomize.toolkit.fluxcd.io/prune: disabled\nspec:\n  policyName: app-grants",
         "  name: app-grants-all\n  annotations:\n    kustomize.toolkit.fluxcd.io/prune: disabled\nspec:\n  policyName: app-grants", "has no ValidatingAdmissionPolicyBinding app-grants"),
        (f"{_APP}/namespace.yaml", "host-path: ${quote}${app_grant_host_path}${quote}", "host-path: \"true\"", "annotation hosting.jarvis.io/host-path"),
        (f"{_APP}/namespace.yaml", "root: ${quote}${app_grant_root}${quote}", "root: \"${app_grant_root}\"", "annotation hosting.jarvis.io/root"),
        (f"{_APP}/tenant.yaml", "resources: [jobs, cronjobs]", "resources: [jobs, cronjobs, networkpolicies]", "batch/networkpolicies"),
        (f"{_APP}/flux.yaml", "  targetNamespace: app-${app_name}\n  sourceRef:\n    kind: GitRepository\n    name: app-${app_name}-file", "  targetNamespace: default\n  sourceRef:\n    kind: GitRepository\n    name: app-${app_name}-file", "objects land in app-"),
        (f"{_APP}/flux.yaml", "    substitute:\n      app_host:", "    substituteFrom:\n      - kind: ConfigMap\n        name: cluster-vars\n    substitute:\n      app_host:", "no substitution source"),
        (f"{_APP}/flux.yaml", "  dependsOn:\n    - name: app-${app_name}-file", "  decryption:\n    provider: sops\n  dependsOn:\n    - name: app-${app_name}-file", "decrypts nothing"),
        (f"{_APP}/flux.yaml", "    name: app-${app_name}\n  dependsOn", "    name: flux-system\n  dependsOn", "reads its own source"),
        (f"{_APP}/tenant.yaml", "  name: app\n  namespace: app-${app_name}\n  annotations", "  name: app\n  namespace: kube-system\n  annotations", "lives in app-"),
        (f"{_APP}/kustomization.yaml", "  - flux.yaml", "  - flux.yaml\n  - extra.yaml", "holds no ClusterRole"),
        (f"{_APP}/grant-revoke.yaml", "  name: app-grant-revoke\nsubjects", "  name: cluster-admin\nsubjects", "binds ClusterRole app-grant-revoke"),
    ],
    ids=["default-deny-ingress-only", "namespace-label", "namespace-prunable", "binding-allows-missing-params", "no-grants-binding",
         "grant-without-env-line", "flag-a-boolean-after-substitution",
         "role-grants-networkpolicies", "objects-outside-namespace",
         "substitution-source", "decryption", "other-source", "role-elsewhere", "extra-kind", "revoker-role"],
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
    _edit(values, "hello-web:\n", "extra:\n")
    messages = _messages(instance)
    assert any("lists extra, and the flux tree has no app-extra-wiring" in item for item in messages), messages
    assert any("wires app hello-web, which values/apps.yaml does not list" in item for item in messages), messages


def test_flux_identity_holds_the_wiring_to_core_template(tmp_path: Path) -> None:
    """Rule: only app-<name>-wiring applying core's app template runs as the platform account."""
    _product, instance = _tree(tmp_path)
    generated = instance / "envs" / "env-00" / "flux" / _apps.GENERATED_REL
    _edit(generated, "path: ./core/feature/app-hosting/app", "path: ./core/platform")
    messages = [item.message for item in scan_flux_identity(instance)]
    assert any("an app's wiring runs as flux-platform" in item for item in messages), messages


def _env(**grants: list[str]) -> dict:
    return {app: {"version": "v0.1.0", "grants": lines} for app, lines in grants.items()}


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (_env(web=["trust"]), "unknown grant"),
        (_env(web=["hostname grafana.env-00.example"]), "outside env-00.example"),
        (_env(web=["lan-port 70000/TCP"]), "lan-port takes"),
        (_env(web=["lan-target nas 10.99.0.10/32 445/TCP"]), "outside the env's lan_cidrs"),
        (_env(web=["lan-target all 10.200.0.0/24 445/TCP"]), "other than all"),
        (_env(web=["lan-target nas 10.200.0.10/32"]), "lan-target takes"),
        (_env(web=["lan-port 443/TCP"]), "443/TCP is core's web door's"),
        (_env(web=["lan-port 6443/TCP"]), "the Kubernetes API's"),
        (_env(web=["lan-port 8472/UDP"]), "flannel's VXLAN's"),
        (_env(web=["lan-port 8080/TCP"], db=["lan-port 8080/TCP"]), "app web already holds 8080/TCP"),
    ],
    ids=["unknown-grant", "hostname-in-zone", "bad-lan-port", "lan-target-off-lan", "lan-target-all", "lan-target-no-port",
         "core-port", "node-port", "node-udp-port", "port-held-twice"],
)
def test_env_grants_reject_what_they_do_not_hold(values: dict, expected: str) -> None:
    """Rule: each grant is one known item; a lan-target range sits inside the env's LAN ranges, so
    it cannot open the internet; a LAN port is no port core or the node holds, and one app's only."""
    errors = _apps.validate(values, "env-00.example", ["10.200.0.0/24"])
    assert any(expected in error for error in errors), errors


def test_lan_grant_needs_the_env_lan_ranges() -> None:
    """Rule: an env that grants a LAN item names its LAN ranges (lan_cidrs)."""
    errors = _apps.validate(_env(web=["lan-all"]), "env-00.example")
    assert any("set lan_cidrs" in error for error in errors), errors


def test_lan_target_against_an_invalid_lan_range_is_reported_not_raised() -> None:
    """Rule: a lan_cidrs entry that is no network (reported as lan_cidrs) holds no lan-target."""
    errors = _apps.validate(_env(web=["lan-target nas 10.200.0.10/32 445/TCP"]), "env-00.example", ["10.200.0.1/24"])
    assert any("outside the env's lan_cidrs" in error for error in errors), errors


def test_env_grants_that_fit_pass() -> None:
    """Rule: each known grant passes, and a protocol core does not hold on a port passes."""
    lines = ["privileged", "root", "host-network", "host-path", "hostname photos.example.org", "lan-port 8443/TCP",
             "lan-port 443/UDP", "lan-all", "lan-target nas 10.200.0.10/32 445/TCP", "lan-target nas 10.200.0.10/32 137/UDP"]
    assert _apps.validate(_env(web=lines), "env-00.example", ["10.200.0.0/24"]) == []


def _schema_errors(instance: Path, *, bootstrapped: bool = False) -> list[str]:
    env_dir = instance / "envs" / "env-00"
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    return env_schema._apps_problems(env_dir, instance, cluster_vars, bootstrapped)


_APP_PIECES = {
    ("CustomResourceDefinition", "apps.hosting.jarvis.io"),
    ("ClusterRole", "kyverno:app-derive"),
    ("ValidatingAdmissionPolicy", "app-identity"),
    ("ValidatingAdmissionPolicy", "app-tenant"),
    ("ValidatingAdmissionPolicy", "app-file"),
    ("ValidatingAdmissionPolicy", "app-grants"),
    ("ValidatingAdmissionPolicyBinding", "app-identity"),
    ("ValidatingAdmissionPolicyBinding", "app-tenant"),
    ("ValidatingAdmissionPolicyBinding", "app-file"),
    ("ValidatingAdmissionPolicyBinding", "app-grants"),
    ("GeneratingPolicy", "app-derive"),
    ("GeneratingPolicy", "app-derive-backups"),
    ("GeneratingPolicy", "pvc-backup-app"),
    ("Deployment", "restic-apps"),
    ("Service", "restic-apps"),
    ("MutatingPolicy", "app-priority"),
    ("PriorityClass", "hosted-app"),
    ("Certificate", "wildcard-apps"),
    ("Namespace", "platform-app-hosting"),
    ("ServiceAccount", "app-grant-revoke"),
    ("ClusterRole", "app-grant-revoke"),
    ("ClusterRole", "app-grant-revoke-namespaces"),
    ("ClusterRoleBinding", "app-grant-revoke-namespaces"),
    ("ConfigMap", "app-grant-revoke"),
    ("CronJob", "app-grant-revoke"),
}


def render_platform(tmp_path: Path, *, hosting: bool) -> dict[tuple[str, str], dict]:
    """What platform-controllers and platform-configs render, with or without the switch."""
    from support import _kustomize_build

    work = tmp_path / ("on" if hosting else "off")
    work.mkdir(parents=True)
    rel = lambda path: os.path.relpath(REPO_ROOT / path, work)  # noqa: E731
    listing = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
               "resources": [rel("core/platform"), rel("core/platform/configs")]}
    if hosting:
        listing["components"] = [rel("core/feature/app-hosting")]
    (work / "kustomization.yaml").write_text(yaml.safe_dump(listing), encoding="utf-8")
    docs = [doc for doc in yaml.safe_load_all(_kustomize_build(str(work), root=work)) if isinstance(doc, dict)]
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def test_app_hosting_off_renders_none_of_it(tmp_path: Path) -> None:
    """Rule: without the switch, an env gets no app-hosting object and no apps door: the Gateway
    has the core listener only."""
    off = render_platform(tmp_path, hosting=False)
    assert not _APP_PIECES & set(off)
    assert [listener["name"] for listener in off[("Gateway", "jarvis")]["spec"]["listeners"]] == ["core"]


def test_app_hosting_on_renders_all_of_it(tmp_path: Path) -> None:
    """Rule: with the switch, the env gets every app-hosting object and the apps door beside the
    core one; nothing else changes (pvc-backup's credential guard is core's, on or off)."""
    on, off = render_platform(tmp_path, hosting=True), render_platform(tmp_path, hosting=False)
    assert _APP_PIECES <= set(on)
    assert set(on) - _APP_PIECES == set(off)
    gateway = ("Gateway", "jarvis")
    assert on[gateway]["spec"]["listeners"][0] == off[gateway]["spec"]["listeners"][0]
    assert [listener["name"] for listener in on[gateway]["spec"]["listeners"]] == ["core", "apps", "apps-granted"]
    for key in set(off) - {gateway}:
        assert on[key] == off[key], key


def test_env_schema_rejects_apps_while_hosting_is_off(tmp_path: Path) -> None:
    """Rule: an env that lists apps turns app hosting on; the list is not silently ignored."""
    _product, instance = _tree(tmp_path)
    _edit(instance / "envs" / "env-00" / _switches.SWITCHES_FILE, "  - app-hosting\n", "")
    assert any("app hosting is off" in error for error in _schema_errors(instance))


def test_env_schema_requires_the_apps_repository_for_an_app(tmp_path: Path) -> None:
    """Rule: an env that lists an app names the apps repository it reads."""
    _product, instance = _tree(tmp_path)
    _edit(instance / "envs" / "env-00" / "values" / "cluster-vars.yaml", "apps_git_repo_url:", "# apps_git_repo_url:")
    assert any("sets apps_git_repo_url" in error for error in _schema_errors(instance))


def test_each_app_is_checked_at_its_pinned_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: validate reads each listed app as the env pins it and holds it to core's app rules:
    an app that core's release refuses fails the env, so a core pin move past an app fails CI."""
    _product, instance = _tree(tmp_path)
    apps = tmp_path / "apps"
    shutil.copytree(FIXTURE_APPS, apps)
    monkeypatch.setenv("APPS_REPO", str(apps))
    assert _schema_errors(instance) == []
    _edit(apps / "hello-web" / "app.yaml", "        port: 80\n", "")
    assert any("hello-web v0.3.0: hello-web/app.yaml: spec.network.web[0]" in error for error in _schema_errors(instance))


def test_two_apps_making_one_web_name_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: no two apps make the same web name: app notes' entry admin and an app notes-admin both
    make notes-admin.apps.<dns suffix>."""
    _product, instance = _tree(tmp_path)
    apps = tmp_path / "apps"
    shutil.copytree(FIXTURE_APPS, apps)
    shutil.copytree(apps / "hello-web", apps / "notes-admin")
    _edit(apps / "notes-admin" / "app.yaml", "name: hello-web", "name: notes-admin")
    _edit(apps / "notes" / "app.yaml", "        port: 80\n", "        port: 80\n      - name: admin\n        service: notes\n        port: 80\n")
    monkeypatch.setenv("APPS_REPO", str(apps))
    values = instance / "envs" / "env-00" / _apps.APPS_VALUES
    values.write_text(values.read_text(encoding="utf-8") + "notes-admin:\n  version: v0.3.0\n", encoding="utf-8")
    errors = _schema_errors(instance)
    assert any("notes-admin v0.3.0: web name notes-admin.apps.<dns suffix> is also app notes's" in error for error in errors), errors


def test_an_app_with_backups_on_needs_its_credential(tmp_path: Path) -> None:
    """Rule: once the env is bootstrapped, an app whose backups are on and that keeps a backup: true
    volume has its credential sealed; with backups off it needs none."""
    _product, instance = _tree(tmp_path)
    values = instance / "envs" / "env-00" / _apps.APPS_VALUES
    assert _schema_errors(instance, bootstrapped=True) == []
    (instance / "envs" / "env-00" / "secrets" / "app-notes" / "backups-restic-app-notes.sops.yaml").unlink()
    assert any("notes: backups are on" in error for error in _schema_errors(instance, bootstrapped=True))
    assert _schema_errors(instance) == []
    _edit(values, "notes:\n  version: v0.3.0\n", "notes:\n  version: v0.3.0\n  backups: false\n")
    assert _schema_errors(instance, bootstrapped=True) == []
