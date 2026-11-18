"""A hosted app's own backup credential: registration, render, and the isolation guards.

Rules (core/feature/app-hosting/CONTRACT.md): core's credential never reaches an app-
namespace; an app credential reaches only the apps server, restic-check and restic-offsite; the
apps server runs --private-repos; an app credential is sealed only in its own namespace's folder.
"""

from __future__ import annotations

import copy as copy_module
import importlib
import shutil
from pathlib import Path

import pytest

from jarvis.checks.backup_isolation import _render_violations, scan_backup_isolation
from jarvis.schema.env import ValidationError, validate_one_env
from support import ExampleCopy, bootstrap_example_copy, flux_built, register_copy_app

APP = "app-notes"


def _app_copy(tmp_path: Path) -> ExampleCopy:
    copy = bootstrap_example_copy(tmp_path)
    register_copy_app(copy, APP)
    return copy


def _platform_configs(copy: ExampleCopy) -> list[dict]:
    return flux_built(copy.root, copy.name)


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
    # Sealed: Flux's build masks a SOPS-encrypted Secret's values.
    assert set((secret.get("data") or secret.get("stringData") or {}).values()) == {"**SOPS**"}
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
    """With no app credential sealed, no app credential renders or is mounted anywhere."""
    docs = _platform_configs(example_copy)
    assert not any(doc["metadata"]["name"].startswith("backups-restic-app-") for doc in docs)
    assert not any(volume["name"].startswith("app-") for volume in _pod(_find(docs, "Deployment", "restic-apps"))["volumes"])
    for name in ("restic-check", "restic-offsite"):
        pod = _pod(_find(docs, "CronJob", name))
        assert not any(volume["name"].startswith("app-") for volume in pod["volumes"])


def _drop_app_exclude(docs: list[dict]) -> None:
    spec = _find(docs, "GeneratingPolicy", "pvc-backup")["spec"]
    spec["matchConditions"] = [item for item in spec["matchConditions"] if item["name"] != "core-namespace"]


def _app_rule_reads_core(docs: list[dict]) -> None:
    variables = _find(docs, "GeneratingPolicy", "pvc-backup-app")["spec"]["variables"]
    credentials = next(item for item in variables if item["name"] == "credentials")
    credentials["expression"] = '[resource.get("v1", "secrets", "platform-backups", "backups-restic")]'


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
        (_drop_app_exclude, "policy pvc-backup generates a Secret in an app- namespace"),
        (_app_rule_reads_core, "policy pvc-backup-app generates a Secret in an app- namespace"),
        (_app_secret_in_core_server, "core's rest-server mounts Secret backups-restic-app-notes"),
        (_core_secret_in_apps_server, "the apps server mounts Secret backups-restic;"),
        (_app_password_in_apps_server, "the apps server mounts backups-restic-app-notes keys None; only htpasswd"),
        (_app_secret_in_core_env, "core's rest-server mounts Secret backups-restic-app-notes"),
        (_no_private_repos, "the apps server runs without --private-repos"),
        (_no_apps_server, "Secret backups-restic-app-notes is an app credential, but the apps server"),
    ],
)
def test_isolation_check_fails_each_seeded_leak(app_copy: ExampleCopy, seed, reason: str) -> None:
    """Each way an app credential could reach core, or core's an app, fails the backup-isolation check (jarvis validate)."""
    docs = copy_module.deepcopy(_platform_configs(app_copy))
    assert _render_violations("fixture", docs) == []
    seed(docs)
    assert any(reason in item.reason for item in _render_violations("fixture", docs))


def _schema_errors(copy: ExampleCopy) -> str:
    with pytest.raises(ValidationError) as caught:
        validate_one_env(copy.root, copy.name)
    return "\n".join(caught.value.messages)


def test_env_schema_rejects_a_credential_outside_its_folder(tmp_path: Path) -> None:
    """An app credential sealed outside its own folder fails env-schema, and registers nothing.
    A hand-edited registration is the render check's (test_render.py)."""
    copy = _app_copy(tmp_path)
    secrets = copy.env_dir / "secrets"
    sealed = secrets / APP / f"backups-restic-{APP}.sops.yaml"
    moved = secrets / f"backups-restic-{APP}.sops.yaml"
    shutil.move(sealed, moved)
    assert "an app credential is sealed only in its own folder" in _schema_errors(copy)
    shutil.move(moved, sealed)
    validate_one_env(copy.root, copy.name)


def test_bootstrap_app_mints_once_and_refuses_a_non_app_name(app_copy: ExampleCopy, monkeypatch) -> None:
    """A second run changes nothing; a name outside app-, or an app the env does not list, is
    refused before any write."""
    mod = importlib.import_module("jarvis.secrets.mint")
    monkeypatch.setattr(mod, "REPO_ROOT", app_copy.root)
    monkeypatch.setattr(mod, "_repo_root", lambda: app_copy.root)
    sealed = app_copy.env_dir / "secrets" / APP / f"backups-restic-{APP}.sops.yaml"
    before = sealed.read_bytes()
    assert mod.bootstrap_app(app_copy.name, APP, sops_bin=shutil.which("sops")) == []
    assert sealed.read_bytes() == before
    for bad in ("notes", "platform-backups", "app-", "app-Notes"):
        with pytest.raises(SystemExit, match="hosted app namespace"):
            mod.bootstrap_app(app_copy.name, bad, sops_bin=shutil.which("sops"))
    with pytest.raises(SystemExit, match="does not list photos"):
        mod.bootstrap_app(app_copy.name, "app-photos", sops_bin=shutil.which("sops"))
    assert not (app_copy.env_dir / "secrets" / "notes").exists()
    assert not (app_copy.env_dir / "secrets" / "app-photos").exists()
