"""flux-identity CI check: Flux objects run only as identities they were given."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from _flux_identity import scan_flux_identity
from schema.env import EXAMPLE_ENV
from support import REPO_ROOT

_ACCOUNTS = """
apiVersion: v1
kind: ServiceAccount
metadata: {name: flux-platform, namespace: flux-system}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: flux-tenant, namespace: flux-system}
"""


def _kustomization(name: str, path: str, account: str | None, **spec: object) -> dict:
    body: dict = {"path": path, "sourceRef": {"kind": "GitRepository", "name": "flux-system"}}
    if account is not None:
        body["serviceAccountName"] = account
    body.update(spec)
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": body,
    }


def _helmrelease(account: str | None, **spec: object) -> dict:
    body: dict = {
        "chart": {"spec": {"chart": "demo", "sourceRef": {"kind": "HelmRepository", "name": "demo"}}}
    }
    if account is not None:
        body["serviceAccountName"] = account
    body.update(spec)
    return {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": "demo", "namespace": "flux-system"},
        "spec": body,
    }


def _flux_instance(*, multitenant: bool) -> dict:
    return {
        "apiVersion": "fluxcd.controlplane.io/v1",
        "kind": "FluxInstance",
        "metadata": {"name": "flux", "namespace": "flux-system"},
        "spec": {"cluster": {"multitenant": multitenant}},
    }


def _repo(
    root: Path,
    *,
    platform_ks: dict | None = None,
    tenant_ks: dict | None = None,
    platform_docs: list[dict] | None = None,
    tenant_docs: list[dict] | None = None,
    root_docs: list[dict] | None = None,
) -> Path:
    """An env whose root applies the accounts, a lockdown FluxInstance, a platform layer and a tenant layer."""
    platform_ks = platform_ks or _kustomization("platform-controllers", "./platform", "flux-platform")
    tenant_ks = tenant_ks or _kustomization("services", "./services", "flux-tenant")
    platform_docs = [_helmrelease("flux-platform")] if platform_docs is None else platform_docs
    tenant_docs = [] if tenant_docs is None else tenant_docs
    root_docs = [_flux_instance(multitenant=True)] if root_docs is None else root_docs
    files = {
        "envs/env-test/flux/kustomization.yaml": "resources: [accounts.yaml, layers.yaml]\n",
        "envs/env-test/flux/accounts.yaml": _ACCOUNTS,
        "envs/env-test/flux/layers.yaml": yaml.safe_dump_all([platform_ks, tenant_ks, *root_docs]),
        "platform/kustomization.yaml": "resources: [objects.yaml]\n",
        "platform/objects.yaml": yaml.safe_dump_all(platform_docs),
        "services/kustomization.yaml": "resources: [objects.yaml]\n" if tenant_docs else "resources: []\n",
        "services/objects.yaml": yaml.safe_dump_all(tenant_docs),
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _messages(root: Path) -> list[str]:
    return [item.message for item in scan_flux_identity(root)]


def test_objects_with_declared_accounts_pass(tmp_path: Path) -> None:
    """Each object names an account the root declares; the platform account stays on the platform layer."""
    assert _messages(_repo(tmp_path)) == []


@pytest.mark.parametrize(
    "seed",
    [
        {"platform_ks": _kustomization("platform-controllers", "./platform", None)},
        {"platform_docs": [_helmrelease(None)]},
        {"tenant_docs": [_kustomization("nested", "./platform", None)]},
    ],
    ids=["kustomization", "helmrelease-on-path", "kustomization-on-path"],
)
def test_object_without_service_account_fails(tmp_path: Path, seed: dict) -> None:
    """Under lockdown an object without serviceAccountName would run as the namespace default."""
    assert any("spec.serviceAccountName is unset" in item for item in _messages(_repo(tmp_path, **seed)))


def test_undeclared_service_account_fails(tmp_path: Path) -> None:
    """An account the root does not declare has no binding the check can see."""
    seed = _kustomization("services", "./services", "flux-missing")
    messages = _messages(_repo(tmp_path, tenant_ks=seed))
    assert any("flux-system/flux-missing is not declared" in item for item in messages)


@pytest.mark.parametrize(
    "seed",
    [
        {"tenant_ks": _kustomization("apps", "./services", "flux-platform")},
        {"tenant_docs": [_helmrelease("flux-platform")]},
    ],
    ids=["tenant-kustomization", "helmrelease-on-tenant-path"],
)
def test_platform_account_outside_the_allow_list_fails(tmp_path: Path, seed: dict) -> None:
    """Only the allow-listed Kustomizations and the objects on their paths run as flux-platform."""
    assert any("run as flux-platform" in item for item in _messages(_repo(tmp_path, **seed)))


_ADDON_ACCOUNT = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "addon-x", "namespace": "flux-system"}}


def _addon_repo(root: Path, *, addon_ks: dict | None = None, app_docs: list[dict] | None = None) -> Path:
    """A services stage, as the platform account, that wires one add-on: its account and its Kustomization."""
    addon_ks = addon_ks or _kustomization("addon-x", "./services/x", "addon-x")
    wiring = [_ADDON_ACCOUNT, _role_binding("x", "Role", "addon-x", account="addon-x"), addon_ks]
    _repo(
        root,
        tenant_ks=_kustomization("services", "./services", "flux-platform"),
        tenant_docs=wiring,
    )
    (root / "services" / "x").mkdir()
    (root / "services" / "x" / "kustomization.yaml").write_text("resources: [app.yaml]\n", encoding="utf-8")
    app = app_docs if app_docs is not None else [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a", "namespace": "x"}}]
    (root / "services" / "x" / "app.yaml").write_text(yaml.safe_dump_all(app), encoding="utf-8")
    return root


def test_services_stage_wiring_an_addon_tenant_passes(tmp_path: Path) -> None:
    """The services stage declares the add-on's account; the add-on's Kustomization runs as it."""
    assert _messages(_addon_repo(tmp_path)) == []


def test_addon_kustomization_running_as_the_platform_account_fails(tmp_path: Path) -> None:
    """An object on the services stage's path is an add-on's, and never runs as flux-platform."""
    seed = _kustomization("addon-x", "./services/x", "flux-platform")
    assert any("run as flux-platform" in item for item in _messages(_addon_repo(tmp_path, addon_ks=seed)))


def test_addon_path_declaring_its_own_account_fails(tmp_path: Path) -> None:
    """An add-on's own path cannot apply the tenant account it runs as."""
    messages = _messages(_addon_repo(tmp_path, app_docs=[_ADDON_ACCOUNT]))
    assert any("declares ServiceAccount flux-system/addon-x" in item for item in messages)


def test_decryption_outside_flux_system_fails(tmp_path: Path) -> None:
    """Only flux-system holds the env key; a Kustomization elsewhere that decrypts fails."""
    seed = _kustomization("addon-x", "./services/x", "addon-x", decryption={"provider": "sops"})
    seed["metadata"]["namespace"] = "x"
    messages = _messages(_addon_repo(tmp_path, addon_ks=seed))
    assert any("sets spec.decryption outside flux-system" in item for item in messages)


def _other(kind: str) -> dict:
    return {"kind": kind, "name": "x", "namespace": "other"}


def _tenant(**spec: object) -> dict:
    return {"tenant_ks": _kustomization("services", "./services", "flux-tenant", **spec)}


def _platform_release(**spec: object) -> dict:
    return {"platform_docs": [_helmrelease("flux-platform", **spec)]}


@pytest.mark.parametrize(
    "seed",
    [
        _tenant(sourceRef=_other("GitRepository")),
        _tenant(dependsOn=[{"name": "x", "namespace": "other"}]),
        _tenant(healthChecks=[dict(_other("Service"), apiVersion="v1")]),
        _platform_release(chart={"spec": {"chart": "demo", "sourceRef": _other("HelmRepository")}}),
        _platform_release(chart=None, chartRef=_other("OCIRepository")),
    ],
    ids=["sourceRef", "dependsOn", "healthChecks", "chart-sourceRef", "chartRef"],
)
def test_cross_namespace_reference_fails(tmp_path: Path, seed: dict) -> None:
    """Every ref stays in the object's namespace; health checks too, which Flux itself would allow."""
    assert any("points to namespace other" in item for item in _messages(_repo(tmp_path, **seed)))


_PLATFORM_SUBJECT = {"kind": "ServiceAccount", "name": "flux-platform", "namespace": "flux-system"}
_PLATFORM_ACCOUNT = {
    "apiVersion": "v1",
    "kind": "ServiceAccount",
    "metadata": {"name": "flux-platform", "namespace": "flux-system"},
}


def _binding(subject: dict) -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": "seeded"},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "cluster-admin",
        },
        "subjects": [subject],
    }


@pytest.mark.parametrize(
    "seeded",
    [
        _PLATFORM_ACCOUNT,
        _binding(_PLATFORM_SUBJECT),
        _binding({"kind": "User", "name": "system:serviceaccount:flux-system:flux-platform"}),
        _binding({"kind": "Group", "name": "system:serviceaccounts:flux-system"}),
        _binding({"kind": "Group", "name": "system:authenticated"}),
    ],
    ids=["service-account", "binding", "user-subject", "group-subject", "authenticated-group"],
)
def test_kustomization_creating_its_own_identity_fails(tmp_path: Path, seeded: dict) -> None:
    """A Kustomization cannot apply the account it runs as, or that account's binding."""
    docs = [_helmrelease("flux-platform"), seeded]
    messages = _messages(_repo(tmp_path, platform_docs=docs))
    assert any("the account it runs as" in item for item in messages)


def test_binding_for_another_account_passes(tmp_path: Path) -> None:
    """A binding on the path for an account the Kustomization does not run as is not self-creation."""
    other = dict(_PLATFORM_SUBJECT, name="some-workload")
    assert _messages(_repo(tmp_path, platform_docs=[_helmrelease("flux-platform"), _binding(other)])) == []


def test_flux_instance_without_lockdown_fails(tmp_path: Path) -> None:
    """The identities only bind when the FluxInstance turns multi-tenancy lockdown on."""
    messages = _messages(_repo(tmp_path, root_docs=[_flux_instance(multitenant=False)]))
    assert any("spec.cluster.multitenant is not true" in item for item in messages)


def test_cluster_wide_binding_for_a_tenant_fails(tmp_path: Path) -> None:
    """Tenant accounts get no cluster-wide rights, whatever their name."""
    tenant = dict(_PLATFORM_SUBJECT, name="flux-tenant")
    messages = _messages(_repo(tmp_path, root_docs=[_flux_instance(multitenant=True), _binding(tenant)]))
    assert any("binds flux-system/flux-tenant cluster-wide" in item for item in messages)


def _role_binding(namespace: str, role_kind: str, role: str, *, account: str = "flux-tenant") -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "seeded", "namespace": namespace},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": role_kind, "name": role},
        "subjects": [dict(_PLATFORM_SUBJECT, name=account)],
    }


def _env_root(binding: dict) -> dict:
    return {"root_docs": [_flux_instance(multitenant=True), binding]}


@pytest.mark.parametrize(
    ("binding", "expected"),
    [
        (
            _role_binding("flux-system", "ClusterRole", "cluster-admin"),
            "in its own namespace, where the Flux objects live",
        ),
        (_role_binding("flux-system", "Role", "narrow"), "in its own namespace"),
        (_role_binding("default", "ClusterRole", "cluster-admin"), "to cluster-admin"),
        (_role_binding("default", "ClusterRole", "admin"), "to admin"),
        (_role_binding("default", "ClusterRole", "edit"), "to edit"),
    ],
    ids=["flux-system-cluster-admin", "flux-system-narrow-role", "default-cluster-admin", "admin", "edit"],
)
def test_broad_or_flux_system_binding_for_a_tenant_fails(
    tmp_path: Path, binding: dict, expected: str
) -> None:
    """A tenant gets no rights in flux-system, and no cluster-admin, admin or edit anywhere."""
    messages = _messages(_repo(tmp_path, **_env_root(binding)))
    assert any("binds flux-system/flux-tenant" in item and expected in item for item in messages)


def test_narrow_binding_for_a_tenant_elsewhere_passes(tmp_path: Path) -> None:
    """A tenant may hold a narrow Role in the namespace it deploys to."""
    binding = _role_binding("default", "Role", "placeholder-writer")
    assert _messages(_repo(tmp_path, **_env_root(binding))) == []


def _example_tree(tmp_path: Path) -> Path:
    """The example env's flux tree and a writable platform copy; services is linked."""
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "platform", root / "platform", symlinks=True)
    (root / "services").symlink_to(REPO_ROOT / "services")
    shutil.copytree(REPO_ROOT / "envs" / EXAMPLE_ENV / "flux", root / "envs" / EXAMPLE_ENV / "flux")
    return root


def _seed(root: Path, directory: str, doc: dict) -> None:
    (root / directory / "seed.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    listing = root / directory / "kustomization.yaml"
    listing.write_text(listing.read_text(encoding="utf-8") + "  - seed.yaml\n", encoding="utf-8")


@pytest.mark.parametrize("directory", ["platform", "platform/configs"])
@pytest.mark.parametrize(
    ("seeded", "expected"),
    [
        (_helmrelease(None), "spec.serviceAccountName is unset"),
        (_PLATFORM_ACCOUNT, "declares ServiceAccount flux-system/flux-platform"),
        (_binding(_PLATFORM_SUBJECT), "declares ClusterRoleBinding seeded"),
    ],
    ids=["helmrelease-without-account", "platform-account", "platform-binding"],
)
def test_example_tree_seeded_under_a_platform_path_fails(
    tmp_path: Path, directory: str, seeded: dict, expected: str
) -> None:
    """The example flux tree passes, and fails once a platform Kustomization's path gains the seed.

    platform/configs is the core of the example env's platform-configs path,
    which is built without the bootstrap secrets the example does not have.
    """
    root = _example_tree(tmp_path)
    assert _messages(root) == []
    _seed(root, directory, seeded)
    assert any(expected in item for item in _messages(root))
