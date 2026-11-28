"""A hosted app's own backup credential: registration, render and sealing.

Rules (core/feature/app-hosting/CONTRACT.md): an app credential reaches only the apps server,
restic-check and restic-offsite, and is sealed only in its own namespace's folder. What a login
can open is test_rest_server_isolation.py's; which credential a volume gets is
test_backup_enrollment.py's.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jarvis.schema.env import ValidationError, validate_one_env
from jarvis.secrets import mint
from support import copy_sample, flux_built, render_switches, sample_root

APP = "app-notes"
CREDENTIAL = f"backups-restic-{APP}"
# The apps server comes with app hosting, restic-offsite with the off-host copy.
FEATURES = ("app-hosting", "offsite-backup-copy")


def _find(docs: list[dict], kind: str, name: str) -> dict:
    return next(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _pod(doc: dict) -> dict:
    spec = doc["spec"]
    return (spec["jobTemplate"]["spec"] if doc["kind"] == "CronJob" else spec)["template"]["spec"]


def _naming(docs: list[dict], text: str) -> set[tuple[str, str]]:
    """(kind, name) of each object that holds text anywhere."""
    return {(doc["kind"], doc["metadata"]["name"]) for doc in docs if text in json.dumps(doc)}


def test_registered_app_renders_its_credential_only_where_needed() -> None:
    """Rule: a registered credential renders sealed, the one secret volume of the apps server (its htpasswd key),
    restic-check and restic-offsite (its password), named nowhere else; core's server keeps its own; the env validates."""
    root = sample_root(*FEATURES)
    docs = flux_built(root, "env-00")
    assert _naming(docs, CREDENTIAL) == {("Secret", CREDENTIAL), ("Deployment", "restic-apps"),
                                         ("CronJob", "restic-check"), ("CronJob", "restic-offsite")}  # fmt: skip
    secret = _find(docs, "Secret", CREDENTIAL)
    assert secret["metadata"]["namespace"] == "platform-backups"
    # Sealed: Flux's build masks a SOPS-encrypted Secret's values.
    assert set((secret.get("data") or secret.get("stringData") or {}).values()) == {"**SOPS**"}
    for kind, name, key in (("Deployment", "restic-apps", "htpasswd"), ("CronJob", "restic-check", "RESTIC_PASSWORD"),
                            ("CronJob", "restic-offsite", "RESTIC_PASSWORD")):  # fmt: skip
        pod = _pod(_find(docs, kind, name))
        assert [volume for volume in pod["volumes"] if "secret" in volume] == [
            {"name": APP, "secret": {"secretName": CREDENTIAL, "items": [{"key": key, "path": key}]}}]
        assert {"name": APP, "mountPath": f"/credentials/{APP}", "readOnly": True} in pod["containers"][0]["volumeMounts"]
    core = _pod(_find(docs, "Deployment", "restic"))
    assert {volume["secret"]["secretName"] for volume in core["volumes"] if "secret" in volume} == {"backups-restic"}
    validate_one_env(root, "env-00")


@pytest.mark.parametrize(
    ("backups", "sealed"),
    [pytest.param(False, True, id="backups-off"), pytest.param(True, False, id="credential-not-sealed")],
)
def test_registration_needs_backups_on_and_a_sealed_credential(tmp_path: Path, backups: bool, sealed: bool) -> None:
    """Rule: an app's credential registers only while its backups are on and it is sealed in the app's folder; without
    either, no object names it, and the apps server, restic-check and restic-offsite still render."""
    env_dir = copy_sample(tmp_path, *FEATURES) / "envs" / "env-00"
    values = env_dir / "values" / "apps.yaml"
    if not backups:
        values.write_text(values.read_text(encoding="utf-8").replace("notes:\n", "notes:\n  backups: false\n"), encoding="utf-8")
    if not sealed:
        shutil.rmtree(env_dir / "secrets" / APP)
    render_switches(env_dir, tmp_path)
    docs = flux_built(tmp_path, "env-00")
    assert _naming(docs, CREDENTIAL) == set()
    assert {("Deployment", "restic-apps"), ("CronJob", "restic-check"), ("CronJob", "restic-offsite")} <= {
        (doc["kind"], doc["metadata"]["name"]) for doc in docs}


def test_env_schema_rejects_a_credential_outside_its_folder(tmp_path: Path) -> None:
    """Rule: an app credential sealed outside its own folder fails env-schema, which names the file and its folder; the
    app then has no credential where it must be. A hand-edited registration is the render check's (test_render.py)."""
    secrets = copy_sample(tmp_path, *FEATURES) / "envs" / "env-00" / "secrets"
    shutil.move(secrets / APP / f"{CREDENTIAL}.sops.yaml", secrets / f"{CREDENTIAL}.sops.yaml")
    with pytest.raises(ValidationError) as caught:
        validate_one_env(tmp_path, "env-00")
    assert caught.value.messages == [
        "envs/env-00/values/apps.yaml: notes: backups are on and it keeps a backup: true volume, so its credential is"
        " sealed: jarvis secrets --env env-00 (or set backups: false)",
        f"envs/env-00/secrets/{CREDENTIAL}.sops.yaml: an app credential is sealed only in its own folder, envs/env-00/secrets/{APP}/",
    ]


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the sample, as the instance jarvis secrets mints into."""
    root = copy_sample(tmp_path, *FEATURES)
    monkeypatch.setattr(mint, "REPO_ROOT", root)
    monkeypatch.setattr(mint, "_repo_root", lambda: root)
    return root


def _tree(root: Path) -> dict[Path, bytes | None]:
    """Every path under root, with each file's bytes."""
    return {path: path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def test_bootstrap_app_mints_an_app_credential_once(instance: Path) -> None:
    """Rule: bootstrap_app mints a missing app credential in its app's folder; a second run changes nothing."""
    sealed = instance / "envs" / "env-00" / "secrets" / APP / f"{CREDENTIAL}.sops.yaml"
    sealed.unlink()
    assert mint.bootstrap_app("env-00", APP, sops_bin=shutil.which("sops")) == [str(sealed.relative_to(instance))]
    before = _tree(instance)
    assert mint.bootstrap_app("env-00", APP, sops_bin=shutil.which("sops")) == []
    assert _tree(instance) == before


@pytest.mark.parametrize(
    ("namespace", "refusal"),
    [
        pytest.param("notes", "refusing notes: it must be a hosted app namespace, app-<name>", id="no-app-prefix"),
        pytest.param("platform-backups", "refusing platform-backups: it must be a hosted app namespace, app-<name>", id="core-namespace"),
        pytest.param("app-", "refusing app-: it must be a hosted app namespace, app-<name>", id="empty-name"),
        pytest.param("app-Notes", "refusing app-Notes: it must be a hosted app namespace, app-<name>", id="upper-case"),
        pytest.param("app-photos", "refusing app-photos: the env does not list photos in values/apps.yaml", id="unlisted-app"),
    ],
)  # fmt: skip
def test_bootstrap_app_refuses_before_any_write(instance: Path, namespace: str, refusal: str) -> None:
    """Rule: a name that is not a hosted app namespace, or an app the env does not list, is refused before any write."""
    before = _tree(instance)
    with pytest.raises(SystemExit) as caught:
        mint.bootstrap_app("env-00", namespace, sops_bin=shutil.which("sops"))
    assert (caught.value.code, _tree(instance)) == (refusal, before)
