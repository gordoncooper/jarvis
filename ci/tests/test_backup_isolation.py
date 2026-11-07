"""A hosted app's own backup credential: registration, render, and the isolation guards.

Rules (platform/backups/CONTRACT.md, App credentials): core's credential never reaches an app-
namespace; an app credential reaches only the apps server, restic-check and restic-offsite; the
apps server runs --private-repos; an app credential is sealed only in its own namespace's folder.
"""

from __future__ import annotations

import copy as copy_module
import shutil
from pathlib import Path

import pytest
import yaml

from _backup_isolation import _render_violations, scan_backup_isolation
from schema.env import ValidationError, validate_one_env
from support import REPO_ROOT, ExampleCopy, _kustomize_build, bootstrap_example_copy, register_copy_app

APP = "app-notes"


def _app_copy(tmp_path: Path) -> ExampleCopy:
    copy = bootstrap_example_copy(tmp_path)
    register_copy_app(copy, APP)
    return copy


def _platform_configs(copy: ExampleCopy) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(_kustomize_build(f"envs/{copy.name}/flux/platform-configs", root=copy.root))
        if isinstance(doc, dict)
    ]


def _find(docs: list[dict], kind: str, name: str) -> dict:
    return next(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _pod(doc: dict) -> dict:
    spec = doc["spec"]
    return (spec["jobTemplate"]["spec"] if doc["kind"] == "CronJob" else spec)["template"]["spec"]


@pytest.fixture(scope="module")
def app_copy(tmp_path_factory: pytest.TempPathFactory) -> ExampleCopy:
    return _app_copy(tmp_path_factory.mktemp("app-copy"))


def test_registered_app_renders_its_credential_only_where_needed(app_copy: ExampleCopy) -> None:
    """The registration adds the app's sealed Secret and the apps server, mounts the htpasswd
    key in the apps server and the password key in restic-check and restic-offsite, leaves core's
    rest-server alone, and passes env-schema and the isolation check."""
    docs = _platform_configs(app_copy)
    secret = _find(docs, "Secret", f"backups-restic-{APP}")
    assert secret["metadata"]["namespace"] == "platform-backups"
    assert "sops" in secret
    expected = {
        "restic-apps": "htpasswd",
        "restic-check": "RESTIC_PASSWORD",
        "restic-offsite": "RESTIC_PASSWORD",
    }
    for name, key in expected.items():
        doc = next(item for item in docs if item["metadata"]["name"] == name and item["kind"] in {"Deployment", "CronJob"})
        pod = _pod(doc)
        assert {"name": APP, "secret": {"secretName": f"backups-restic-{APP}", "items": [{"key": key, "path": key}]}} in pod["volumes"]
        mount = {"name": APP, "mountPath": f"/credentials/{APP}", "readOnly": True}
        assert mount in pod["containers"][0]["volumeMounts"]
    core = _find(docs, "Deployment", "restic")
    assert all(volume.get("secret", {}).get("secretName", "backups-restic") == "backups-restic" for volume in _pod(core)["volumes"])
    validate_one_env(app_copy.root, app_copy.name)
    assert scan_backup_isolation(app_copy.root) == []


def test_core_only_env_renders_no_app_workload(example_copy: ExampleCopy) -> None:
    """With no app registered and the apps server off, nothing for apps renders."""
    docs = _platform_configs(example_copy)
    assert not any(doc["metadata"]["name"].startswith(("restic-apps", "backups-restic-app-")) for doc in docs)
    for name in ("restic-check", "restic-offsite"):
        pod = _pod(_find(docs, "CronJob", name))
        assert not any(volume["name"].startswith("app-") for volume in pod["volumes"])


def _drop_app_exclude(docs: list[dict]) -> None:
    rule = next(rule for rule in _find(docs, "ClusterPolicy", "pvc-backup")["spec"]["rules"] if rule["name"] == "restic-secret")
    rule["exclude"]["any"] = [item for item in rule["exclude"]["any"] if "namespaces" not in item["resources"]]


def _app_rule_reads_core(docs: list[dict]) -> None:
    rule = next(rule for rule in _find(docs, "ClusterPolicy", "pvc-backup")["spec"]["rules"] if rule["name"] == "restic-secret-app")
    rule["context"][0]["apiCall"]["urlPath"] = "/api/v1/namespaces/platform-backups/secrets/backups-restic"


def _app_secret_in_core_server(docs: list[dict]) -> None:
    _pod(_find(docs, "Deployment", "restic"))["volumes"].append(
        {"name": APP, "secret": {"secretName": f"backups-restic-{APP}", "items": [{"key": "htpasswd", "path": "htpasswd"}]}}
    )


def _core_secret_in_apps_server(docs: list[dict]) -> None:
    _pod(_find(docs, "Deployment", "restic-apps"))["volumes"].append({"name": "core", "secret": {"secretName": "backups-restic"}})


def _app_password_in_apps_server(docs: list[dict]) -> None:
    volume = next(item for item in _pod(_find(docs, "Deployment", "restic-apps"))["volumes"] if item["name"] == APP)
    volume["secret"].pop("items")


def _app_secret_in_core_env(docs: list[dict]) -> None:
    _pod(_find(docs, "Deployment", "restic"))["containers"][0].setdefault("envFrom", []).append(
        {"secretRef": {"name": f"backups-restic-{APP}"}}
    )


def _no_private_repos(docs: list[dict]) -> None:
    container = _pod(_find(docs, "Deployment", "restic-apps"))["containers"][0]
    container["command"] = [part.replace(" --private-repos", "") for part in container["command"]]


def _no_apps_server(docs: list[dict]) -> None:
    docs[:] = [doc for doc in docs if doc["metadata"]["name"] != "restic-apps"]


@pytest.mark.parametrize(
    ("seed", "reason"),
    [
        (_drop_app_exclude, "rule restic-secret generates a Secret in an app- namespace"),
        (_app_rule_reads_core, "rule restic-secret-app generates a Secret in an app- namespace"),
        (_app_secret_in_core_server, "core's rest-server mounts Secret backups-restic-app-notes"),
        (_core_secret_in_apps_server, "the apps server mounts Secret backups-restic;"),
        (_app_password_in_apps_server, "the apps server mounts backups-restic-app-notes keys None; only htpasswd"),
        (_app_secret_in_core_env, "core's rest-server mounts Secret backups-restic-app-notes"),
        (_no_private_repos, "the apps server runs without --private-repos"),
        (_no_apps_server, "Secret backups-restic-app-notes is an app credential, but platform/optional/apps-backup-server is not listed"),
    ],
)
def test_isolation_check_fails_each_seeded_leak(app_copy: ExampleCopy, seed, reason: str) -> None:
    """Each way an app credential could reach core, or core's an app, fails ci/checks/backup-isolation."""
    docs = copy_module.deepcopy(_platform_configs(app_copy))
    assert _render_violations("fixture", docs) == []
    seed(docs)
    assert any(reason in item.reason for item in _render_violations("fixture", docs))


def _schema_errors(copy: ExampleCopy) -> str:
    with pytest.raises(ValidationError) as caught:
        validate_one_env(copy.root, copy.name)
    return "\n".join(caught.value.messages)


def _set_components(copy: ExampleCopy, components: list[str], *, apps_server: bool = True) -> None:
    path = copy.env_dir / "flux" / "platform-configs" / "kustomization.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # The registrations change; the app-hosting switch the copy carries stays.
    switch = [item for item in doc.get("components") or [] if item.endswith("core/platform/optional/app-hosting")]
    doc["components"] = [*switch, *components]
    if not apps_server:
        doc["resources"] = [item for item in doc["resources"] if not item.endswith("platform/optional/apps-backup-server")]
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_env_schema_rejects_each_bad_registration(tmp_path: Path) -> None:
    """A registration outside app-, an edited Component, a missing sealed file, no apps server,
    and an app credential sealed outside its own folder each fail env-schema."""
    copy = _app_copy(tmp_path)
    secrets = copy.env_dir / "secrets"
    sealed = secrets / APP / f"backups-restic-{APP}.sops.yaml"
    component = copy.env_dir / "flux" / "platform-configs" / "backups" / APP / "kustomization.yaml"

    _set_components(copy, [f"backups/{APP}", "backups/notes"])
    assert "hand-copying registrations" in _schema_errors(copy)
    _set_components(copy, [f"backups/{APP}"])

    original = component.read_text(encoding="utf-8")
    component.write_text(original.replace("name: restic-apps", "name: restic"), encoding="utf-8")
    assert "differs from the registration make bootstrap-secrets APP=app-notes writes" in _schema_errors(copy)
    component.write_text(original, encoding="utf-8")

    _set_components(copy, [f"backups/{APP}"], apps_server=False)
    path = copy.env_dir / "flux" / "platform-configs" / "kustomization.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["components"] = [item for item in doc.get("components") or [] if "backup-selection" not in str(item)]
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    assert "registers app credentials but does not list platform/optional/apps-backup-server" in _schema_errors(copy)
    _set_components(copy, [f"backups/{APP}"])
    path = copy.env_dir / "flux" / "platform-configs" / "kustomization.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    wrapper = "../generated/backup-selection"
    if wrapper not in (doc.get("components") or []):
        doc.setdefault("components", []).append(wrapper)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    moved = secrets / f"backups-restic-{APP}.sops.yaml"
    shutil.move(sealed, moved)
    errors = _schema_errors(copy)
    assert f"registers {APP}, missing required file" in errors
    assert "an app credential is sealed only in its own folder" in errors
    shutil.move(moved, sealed)
    import sys

    lib = str(REPO_ROOT / "tools" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from render_backup_selection import render_env

    render_env(copy.env_dir, copy.root)
    validate_one_env(copy.root, copy.name)


def test_bootstrap_app_mints_once_and_refuses_a_non_app_name(app_copy: ExampleCopy) -> None:
    """A second run changes nothing; a name outside app- is refused before any write."""
    from support import load_tool

    mod = load_tool("bootstrap_secrets_app_rerun", REPO_ROOT / "tools" / "bootstrap-secrets")
    mod.REPO_ROOT = app_copy.root
    mod._repo_root = lambda: app_copy.root
    sealed = app_copy.env_dir / "secrets" / APP / f"backups-restic-{APP}.sops.yaml"
    before = sealed.read_bytes()
    assert mod.bootstrap_app(app_copy.name, APP, sops_bin=shutil.which("sops")) == []
    assert sealed.read_bytes() == before
    for bad in ("notes", "platform-backups", "app-", "app-Notes"):
        with pytest.raises(SystemExit, match="hosted app namespace"):
            mod.bootstrap_app(app_copy.name, bad, sops_bin=shutil.which("sops"))
    assert not (app_copy.env_dir / "secrets" / "notes").exists()
