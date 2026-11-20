"""A hosted app's own backup credential: registration, render and sealing.

Rules (core/feature/app-hosting/CONTRACT.md): an app credential reaches only the apps server,
restic-check and restic-offsite, and is sealed only in its own namespace's folder. What a login
can open is test_rest_server_isolation.py's; which credential a volume gets is
test_backup_enrollment.py's.
"""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path

import pytest

from jarvis.schema.env import ValidationError, validate_one_env
from support import ExampleCopy, bootstrap_example_copy, flux_built, register_copy_app, render_switches

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
    rest-server alone, and passes env-schema."""
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


def test_core_only_env_renders_no_app_workload(tmp_path: Path) -> None:
    """With no app credential sealed (notes' backups off), no app credential renders or is mounted anywhere."""
    copy = bootstrap_example_copy(tmp_path)
    shutil.rmtree(copy.env_dir / "secrets" / APP)
    values = copy.env_dir / "values" / "apps.yaml"
    values.write_text(values.read_text(encoding="utf-8").replace("notes:\n", "notes:\n  backups: false\n"), encoding="utf-8")
    render_switches(copy.env_dir, copy.root)
    docs = _platform_configs(copy)
    assert not any(doc["metadata"]["name"].startswith("backups-restic-app-") for doc in docs)
    assert not any(volume["name"].startswith("app-") for volume in _pod(_find(docs, "Deployment", "restic-apps"))["volumes"])
    for name in ("restic-check", "restic-offsite"):
        pod = _pod(_find(docs, "CronJob", name))
        assert not any(volume["name"].startswith("app-") for volume in pod["volumes"])


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
