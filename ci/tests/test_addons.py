"""Add-ons plug in through one seam: the addons check, the env-schema add-on rules, and full env-check runs."""

from __future__ import annotations

import contextlib
import copy
import io
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from jarvis.checks.addons import scan_addons
from jarvis.schema.env import ValidationError, validate_env_tree
from support import (
    _write_env,
    bootstrap_example_copy,
    core_at,
    instance_apps_repo_for_checks,
    render_switches,
    write_flux_root,
)

_TENANT = {"kind": "ServiceAccount", "name": "addon-widget", "namespace": "flux-system"}


_KEEP = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}


def _wiring() -> list[dict]:
    docs = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "widget", "labels": {"addons.jarvis.io/name": "widget"}},
        },
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "addon-widget", "namespace": "flux-system"}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "addon-widget", "namespace": "widget"},
            "rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "create", "patch", "delete"]}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "addon-widget", "namespace": "widget"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "addon-widget"},
            "subjects": [dict(_TENANT)],
        },
        {
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
            "kind": "Kustomization",
            "metadata": {"name": "addon-widget", "namespace": "flux-system"},
            "spec": {
                "path": "./addons/widget/app",
                "serviceAccountName": "addon-widget",
                "targetNamespace": "widget",
                "sourceRef": {"kind": "GitRepository", "name": "flux-system"},
                # Inline values only; the services stage fills them.
                "postBuild": {"substitute": {"jarvis_env": "${jarvis_env}"}},
            },
        },
    ]
    for doc in docs:
        if doc["kind"] != "Kustomization":
            doc["metadata"]["annotations"] = dict(_KEEP)
    return docs


def _app() -> list[dict]:
    return [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "page", "namespace": "widget"}}]


def _stage(account: str = "flux-platform", components: tuple[str, ...] = ("widget",)) -> dict:
    spec: dict = {
        "path": "./addons",
        "serviceAccountName": account,
        "sourceRef": {"kind": "GitRepository", "name": "flux-system"},
        "postBuild": {"substituteFrom": [{"kind": "ConfigMap", "name": "cluster-vars"}]},
    }
    if components:
        spec["components"] = list(components)
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": "services", "namespace": "flux-system"},
        "spec": spec,
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(
    tmp_path: Path,
    *,
    wiring: list[dict] | None = None,
    app: list[dict] | None = None,
    stage: dict | None = None,
    core_files: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """A product with one add-on and a chat-bot slot, and an instance whose env enables it."""
    product, instance = tmp_path / "product", tmp_path / "instance"
    files = {
        "addons/kustomization.yaml": "resources: []\n",
        "addons/slots/chat-bot/CONTRACT.md": "# CONTRACT: chat-bot slot\n",
        "addons/widget/kustomization.yaml": (
            "apiVersion: kustomize.config.k8s.io/v1alpha1\nkind: Component\nresources: [wiring.yaml]\n"
        ),
        "addons/widget/wiring.yaml": yaml.safe_dump_all(_wiring() if wiring is None else wiring),
        "addons/widget/app/kustomization.yaml": "resources: [app.yaml]\n",
        "addons/widget/app/app.yaml": yaml.safe_dump_all(_app() if app is None else app),
        "tools/verify.py": "# Core refers to the chat-bot slot only.\n",
        **(core_files or {}),
    }
    for rel, text in files.items():
        _write(product / rel, text)
    flux = instance / "envs" / "env-test" / "flux"
    vars_doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "cluster-vars", "namespace": "flux-system"},
        "data": {"jarvis_env": "env-test"},
    }
    _write(flux / "kustomization.yaml", "resources: [stage.yaml]\n")
    _write(flux / "stage.yaml", yaml.safe_dump_all([stage or _stage(), vars_doc]))
    write_flux_root(flux.parent)
    return product, instance


def _messages(product: Path, instance: Path) -> list[str]:
    with core_at(product):
        return [f"{item.path}: {item.message}" for item in scan_addons(product, instance)]


def test_addon_meeting_the_contract_passes(tmp_path: Path) -> None:
    """An add-on with its namespace, tenant, Role, binding and one Kustomization in its home passes."""
    assert _messages(*_tree(tmp_path)) == []


def _replace(docs: list[dict], kind: str, **changes: object) -> list[dict]:
    out = copy.deepcopy(docs)
    target = next(doc for doc in out if doc["kind"] == kind)
    for dotted, value in changes.items():
        node = target
        *parents, last = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[last] = value
    return out


_CLUSTER_ROLE = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "widget"}}
_PRIORITY = {"apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass", "metadata": {"name": "widget"}, "value": 1}


@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        ({"wiring": [*_wiring(), _CLUSTER_ROLE]}, "is not admitted"),
        ({"wiring": _replace(_wiring(), "RoleBinding", **{"metadata.namespace": "default"})}, "outside the add-on's namespaces"),
        ({"wiring": _replace(_wiring(), "RoleBinding", roleRef={"kind": "ClusterRole", "name": "edit"})}, "does not ship"),
        (
            {"wiring": _replace(_wiring(), "Role", rules=[{"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles"], "verbs": ["create"]}])},
            "grants API group rbac.authorization.k8s.io",
        ),
        ({"wiring": _replace(_wiring(), "Role", rules=[{"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get"]}])}, "grants namespaces"),
        ({"wiring": _replace(_wiring(), "Namespace", **{"metadata.name": "app-widget"})}, "reserved"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"spec.path": "./core/platform"})}, "outside the add-on's home"),
        (
            {"wiring": _replace(_wiring(), "Kustomization", **{"spec.decryption": {"provider": "sops"}})},
            "sealed Secrets are under",
        ),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"spec.dependsOn": [{"name": "addon-other"}]})}, "waits only on its own"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"spec.serviceAccountName": "flux-apps"})}, "not the add-on's tenant"),
        ({"app": [_PRIORITY]}, "cluster-scoped; the tenant cannot apply it"),
        ({"app": [*_app(), _CLUSTER_ROLE]}, "cluster-scoped"),
        (
            {"wiring": _replace(_wiring(), "Kustomization", **{"spec.targetNamespace": None}), "app": _replace(_app(), "ConfigMap", **{"metadata.namespace": "platform-backups"})},
            "outside the add-on's namespaces",
        ),
        ({"stage": _stage(account="flux-apps")}, "runs as flux-platform"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"metadata.labels": {"addons.jarvis.io/slot": "pager"}})}, "has no folder"),
        ({"core_files": {"core/platform/x/notes.yaml": "# waits for widget\n"}}, "core names add-on 'widget'"),
        ({"wiring": _replace(_wiring(), "ServiceAccount", **{"metadata.annotations": {}})}, "could remove it before its workloads"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"metadata.namespace": "widget"})}, "lives outside flux-system"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"metadata.name": "widget-app"})}, "is named addon-widget"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"spec.targetNamespace": "other"})}, "targetNamespace other"),
        ({"wiring": _replace(_wiring(), "Namespace", **{"metadata.labels": {}})}, "label addons.jarvis.io/name"),
        ({"wiring": _replace(_wiring(), "Namespace", **{"metadata.name": "kyverno"})}, "named widget or widget-<part>"),
        ({"wiring": [doc for doc in _wiring() if doc["kind"] != "Namespace"]}, "declares no Namespace"),
        ({"wiring": [*_wiring(), _replace(_wiring(), "ServiceAccount", **{"metadata.name": "addon-widget-2"})[1]]}, "declares 2 ServiceAccounts"),
        ({"wiring": _replace(_wiring(), "ServiceAccount", **{"metadata.name": "widget-tenant"})}, "the tenant account is named"),
        ({"wiring": _replace(_wiring(), "RoleBinding", subjects=[_TENANT, {"kind": "Group", "name": "x"}])}, "binds subjects other than"),
        ({"wiring": _replace(_wiring(), "Role", rules=[{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["*"]}])}, "grants every verb"),
        (
            {"wiring": _replace(_wiring(), "Role", rules=[{"apiGroups": ["kustomize.toolkit.fluxcd.io"], "resources": ["kustomizations"], "verbs": ["get"]}])},
            "grants API group kustomize.toolkit.fluxcd.io",
        ),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"spec.sourceRef": {"kind": "GitRepository", "name": "other"}})}, "reads the root source"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"spec.patches": []})}, "sets spec.patches"),
        ({"wiring": _replace(_wiring(), "Kustomization", **{"spec.path": "./addons/widget/../../platform"})}, "outside the add-on's home"),
        ({"app": [{"apiVersion": "x.io/v1", "kind": "ClusterSecretStore", "metadata": {"name": "s"}}]}, "cluster-scoped"),
        (
            {"core_files": {"addons/widget/kustomization.yaml": "kind: Component\nresources: [wiring.yaml]\npatches: []\n"}},
            "lists files only",
        ),
        ({"core_files": {"addons/widget/kustomization.yaml": "kind: Kustomization\nresources: [wiring.yaml]\n"}}, "must be a kustomize Component"),
        (
            {"core_files": {"addons/widget/kustomization.yaml": "kind: Component\nresources: [wiring.yaml, gone.yaml]\n"}},
            "gone.yaml, which is not a file",
        ),
        ({"core_files": {"addons/slots/widget/CONTRACT.md": "# slot\n"}}, "named like slot widget"),
        (
            {"wiring": _replace(_wiring(), "Kustomization", **{"spec.postBuild": {"substituteFrom": [{"kind": "ConfigMap", "name": "cluster-vars"}]}})},
            "takes no substitution source",
        ),
        (
            {"wiring": _replace(_wiring(), "Kustomization", **{"spec.postBuild": {"substituteFrom": [{"kind": "Secret", "name": "x", "optional": True}]}})},
            "takes no substitution source",
        ),
    ],
    ids=[
        "cluster-role-in-wiring",
        "binding-outside-namespaces",
        "binding-to-cluster-role",
        "role-grants-rbac",
        "role-grants-namespaces",
        "reserved-app-prefix",
        "path-outside-home",
        "decrypts-outside-secrets",
        "depends-on-another-addon",
        "runs-as-another-account",
        "cluster-scoped-on-tenant-path",
        "rbac-on-tenant-path",
        "object-in-another-namespace",
        "stage-not-platform",
        "unknown-slot",
        "core-names-addon",
        "tenant-account-prunable",
        "kustomization-outside-flux-system",
        "kustomization-misnamed",
        "target-namespace-elsewhere",
        "namespace-unlabelled",
        "namespace-not-its-own-name",
        "no-namespace",
        "two-accounts",
        "tenant-misnamed",
        "binding-extra-subject",
        "role-every-verb",
        "role-grants-flux",
        "other-source",
        "kustomization-patches",
        "path-traversal",
        "cluster-prefixed-kind",
        "component-not-files-only",
        "not-a-component",
        "component-lists-missing-file",
        "named-like-a-slot",
        "substitution-source-required",
        "substitution-source-optional",
    ],
)
def test_addon_outside_the_seam_fails(tmp_path: Path, seed: dict, expected: str) -> None:
    """Each rule of the add-on contract fails on its seeded breach."""
    messages = _messages(*_tree(tmp_path, **seed))
    assert any(expected in item for item in messages), messages


def test_addon_named_apps_fails(tmp_path: Path) -> None:
    """Rule: no add-on is named apps, so none claims the hosted apps' *.apps.<dns suffix>."""
    component = "apiVersion: kustomize.config.k8s.io/v1alpha1\nkind: Component\nresources: []\n"
    messages = _messages(*_tree(tmp_path, core_files={"addons/apps/kustomization.yaml": component}))
    assert any(item.startswith("addons/apps: the add-on name apps is reserved") for item in messages), messages


def test_addon_secrets_from_its_namespace_folder_pass(tmp_path: Path) -> None:
    """A decrypting Kustomization that reads envs/<env>/secrets/<its namespace> passes."""
    secrets = _replace(
        _wiring(),
        "Kustomization",
        **{"spec.path": "./envs/${jarvis_env}/secrets/widget", "spec.decryption": {"provider": "sops", "secretRef": {"name": "sops-age"}}},
    )
    product, instance = _tree(tmp_path, wiring=secrets)
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "widget-token", "namespace": "widget"}}
    _write(instance / "envs" / "env-test" / "secrets" / "widget" / "widget-token.sops.yaml", yaml.safe_dump(secret))
    assert _messages(product, instance) == []


def _schema_errors(root: Path, core: Path | None = None) -> list[str]:
    """env-schema's messages for root, against core (default the test core)."""
    with core_at(core) if core else contextlib.nullcontext():
        try:
            validate_env_tree(root, pre_host=True)
        except ValidationError as exc:
            return exc.messages
    return []


def _schema_env(tmp_path: Path, components: list[str], *, slots: dict[str, str] | None = None) -> Path:
    """An env listing components, over a core with add-ons widget and gadget (each may fill a slot)."""
    product, root = tmp_path / "product", tmp_path / "repo"
    for name in ("widget", "gadget"):
        labels = {"addons.jarvis.io/slot": slots[name]} if slots and name in slots else {}
        ks = {"kind": "Kustomization", "metadata": {"name": f"addon-{name}", "labels": labels}, "spec": {}}
        _write(product / "addons" / name / "kustomization.yaml", "kind: Component\nresources: [flux.yaml]\n")
        _write(product / "addons" / name / "flux.yaml", yaml.safe_dump(ks))
    root.mkdir()
    env = _write_env(root)
    _write(env / "flux" / "kustomizations" / "services.yaml", yaml.safe_dump({"spec": {"components": components}}))
    return root


def test_env_enabling_known_addons_passes_the_addon_rules(tmp_path: Path) -> None:
    errors = _schema_errors(_schema_env(tmp_path, ["widget", "gadget"], slots={"widget": "chat-bot"}), tmp_path / "product")
    assert not [item for item in errors if "add-on" in item], errors


@pytest.mark.parametrize(
    ("components", "slots", "expected"),
    [
        (["nothing"], None, "unknown add-on 'nothing'"),
        (["widget", "widget"], None, "listed twice"),
        (["widget", "gadget"], {"widget": "chat-bot", "gadget": "chat-bot"}, "all fill slot chat-bot"),
    ],
    ids=["unknown", "twice", "two-per-slot"],
)
def test_env_addon_list_breaking_the_rules_fails(
    tmp_path: Path, components: list[str], slots: dict | None, expected: str
) -> None:
    """env-schema rejects an unknown add-on, a duplicate, and two add-ons for one slot."""
    errors = _schema_errors(_schema_env(tmp_path, components, slots=slots), tmp_path / "product")
    assert any(expected in item for item in errors), errors


def test_render_refuses_two_addons_for_one_slot(tmp_path: Path) -> None:
    """Rule: the render checks slots before it writes, so a conflict never reaches the env's wiring."""
    root = _schema_env(tmp_path, [], slots={"widget": "chat-bot", "gadget": "chat-bot"})
    env = root / "envs" / "env-test"
    _write(env / "values" / "switches.yaml", yaml.safe_dump({"addons": ["widget", "gadget"]}))
    before = (env / "flux" / "kustomizations" / "services.yaml").read_text(encoding="utf-8")
    with core_at(tmp_path / "product"), pytest.raises(SystemExit, match="all fill slot chat-bot"):
        render_switches(env, root)
    assert (env / "flux" / "kustomizations" / "services.yaml").read_text(encoding="utf-8") == before


def _run_env_checks(root: Path) -> subprocess.CompletedProcess[str]:
    """Every env-level check (jarvis validate), against the copy as an instance and the test core
    (its fixture add-on). gitleaks reads its git history."""
    from jarvis.env import validate

    git = ["git", "-c", "user.email=fixture@example.com", "-c", "user.name=fixture"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], cwd=root, check=True)
    instance = root.resolve()
    out = io.StringIO()
    previous = os.environ.get("APPS_REPO")
    os.environ["APPS_REPO"] = str(instance_apps_repo_for_checks(instance))
    try:
        with contextlib.redirect_stdout(out):
            code = validate.run(instance)
    finally:
        if previous is None:
            os.environ.pop("APPS_REPO", None)
        else:
            os.environ["APPS_REPO"] = previous
    return subprocess.CompletedProcess([], code, out.getvalue(), "")


def test_core_only_env_passes_every_env_check(tmp_path: Path) -> None:
    """An env with no add-on is a full env: every env-level check passes on it."""
    copy_env = bootstrap_example_copy(tmp_path, core_only=True)
    proc = _run_env_checks(copy_env.root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS addons" in proc.stdout and "PASS secret-scan" in proc.stdout, proc.stdout


def test_env_with_the_sample_addon_and_its_sealed_secret_passes_every_env_check(tmp_path: Path) -> None:
    """The example's add-on list with each decrypted folder sealed to a throwaway key passes; without the seal it fails."""
    copy_env = bootstrap_example_copy(tmp_path)
    sealed = sorted((copy_env.env_dir / "secrets").glob("*/*.sops.yaml"))
    assert sealed
    proc = _run_env_checks(copy_env.root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS addons" in proc.stdout and "PASS secret-scan" in proc.stdout, proc.stdout
    for path in sealed:
        path.unlink()
    assert any("holds no sealed file" in item for item in _schema_errors(copy_env.root))


def test_a_sealed_file_nothing_reads_fails(tmp_path: Path) -> None:
    """Rule: every sealed file under envs/<env>/secrets/ has a reader. A left-over top-level
    secret and a folder no enabled add-on decrypts each fail env-schema; the bootstrap secrets,
    the off-host target and the enabled add-on's folder pass."""
    copy_env = bootstrap_example_copy(tmp_path)
    secrets = copy_env.env_dir / "secrets"
    assert _schema_errors(copy_env.root) == []
    template = next(secrets.glob("*.sops.yaml")).read_text(encoding="utf-8")
    (secrets / "grafana-admin.sops.yaml").write_text(template, encoding="utf-8")
    (secrets / "hermes").mkdir()
    (secrets / "hermes" / "token.sops.yaml").write_text(template, encoding="utf-8")
    orphans = [item for item in _schema_errors(copy_env.root) if "is sealed but nothing reads it" in item]
    assert len(orphans) == 2, orphans
    assert any("secrets/grafana-admin.sops.yaml" in item for item in orphans)
    assert any("secrets/hermes/token.sops.yaml" in item for item in orphans)
