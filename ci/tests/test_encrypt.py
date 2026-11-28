"""jarvis encrypt: where it seals each Secret, and what it refuses before sops runs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import require_cli

import support
from jarvis.apps import backups
from jarvis.secrets import encrypt

CREDENTIAL = "backups-restic-app-demo"
SEGMENTS = "refusing env, namespace or name: only [a-z0-9-] segments are allowed"
NAMESPACE = "metadata.namespace must match --namespace"
WITH_APP = f"refusing {CREDENTIAL}: seal an app's backup credential with --app"
NOT_A_SECRET = "stdin must be a Kubernetes Secret manifest (kind: Secret)"


def _doc(name: str | None, namespace: str | None = "demo", token: str = "PLAINTEXT-MARKER") -> dict:
    metadata = {"annotations": {"kustomize.toolkit.fluxcd.io/prune": "disabled"}}
    metadata |= {key: value for key, value in (("name", name), ("namespace", namespace)) if value is not None}
    return {"apiVersion": "v1", "kind": "Secret", "metadata": metadata, "type": "Opaque", "stringData": {"token": token}}


def _secret(name: str | None, namespace: str | None = "demo", token: str = "PLAINTEXT-MARKER") -> str:
    return yaml.safe_dump(_doc(name, namespace, token), sort_keys=False)


def _paths(root: Path) -> set[str]:
    """Every file and folder under root (a symlink is listed, not followed)."""
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def _opened(root: Path, rel: str) -> dict:
    """The sealed file at rel, decrypted with the instance's throwaway key."""
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(root / ".age" / "keys.txt")}
    return yaml.safe_load(subprocess.run(["sops", "decrypt", rel], cwd=root, env=env, capture_output=True, text=True,
                                         check=True).stdout)  # fmt: skip


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The instance at tmp_path, env-test sealed to a throwaway key, with SOPS_AGE_KEY_FILE unset: a seal needs no key."""
    require_cli("sops")
    support.sealing_root(tmp_path, monkeypatch)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    return tmp_path.resolve()


@pytest.mark.parametrize(
    ("argv", "rel", "given", "sealed"),
    [
        pytest.param(["--name", "sample-secret"], "envs/env-test/secrets/sample-secret.sops.yaml",
                     ("sample-secret", "demo"), ("sample-secret", "demo"), id="env-secret"),
        pytest.param(["--namespace", "demo", "--name", "demo-token"], "envs/env-test/secrets/demo/demo-token.sops.yaml",
                     ("demo-token", "demo"), ("demo-token", "demo"), id="addon-secret"),
        pytest.param(["--namespace", "demo", "--name", "demo-token"], "envs/env-test/secrets/demo/demo-token.sops.yaml",
                     (None, None), ("demo-token", "demo"), id="metadata-filled-in"),
        pytest.param(["--app", "app-demo"], backups.sealed_rel("env-test", "app-demo"),
                     (CREDENTIAL, "platform-backups"), (CREDENTIAL, "platform-backups"), id="app-credential"),
    ],
)  # fmt: skip
def test_seal_writes_each_secret_where_the_env_reads_it(
    root: Path, capsys: pytest.CaptureFixture[str], argv: list[str], rel: str, given: tuple, sealed: tuple
) -> None:
    """Rule: jarvis encrypt writes one file, the env's ciphertext of the whole Secret, named and placed where render
    and the schema read it: an add-on's in its namespace's folder, an app credential in its app's, in platform-backups."""
    plain = root / "plain.yaml"
    plain.write_text(_secret(*given), encoding="utf-8")

    assert encrypt.main(["--env", "env-test", *argv, str(plain)]) == 0

    assert capsys.readouterr().out == f"{rel}\n"
    assert [path for path in _paths(root) if path.startswith("envs/") and (root / path).is_file()] == [rel]
    text = (root / rel).read_text(encoding="utf-8")
    assert "PLAINTEXT-MARKER" not in text and yaml.safe_load(text)["stringData"]["token"].startswith("ENC[")
    assert _opened(root, rel) == _doc(*sealed)


@pytest.mark.parametrize(
    ("seal", "env", "name", "plaintext", "namespace", "message"),
    [
        pytest.param(encrypt.seal, "Not-valid", "sample-secret", _secret("sample-secret"), None, SEGMENTS,
                     id="env-not-a-name"),
        pytest.param(encrypt.seal, "..", "sample-secret", _secret("sample-secret"), None, SEGMENTS, id="env-dotdot"),
        pytest.param(encrypt.seal, "a/b", "sample-secret", _secret("sample-secret"), None, SEGMENTS, id="env-slash"),
        pytest.param(encrypt.seal, "env-test", "..", _secret("sample-secret"), None, SEGMENTS, id="name-dotdot"),
        pytest.param(encrypt.seal, "env-test", "a/b", _secret("sample-secret"), None, SEGMENTS, id="name-slash"),
        pytest.param(encrypt.seal, "env-test", "Not-Valid", _secret("sample-secret"), None, SEGMENTS,
                     id="name-not-a-name"),
        pytest.param(encrypt.seal, "env-missing", "sample-secret", _secret("sample-secret"), None,
                     "env folder does not exist", id="no-env-folder"),
        pytest.param(encrypt.seal, "env-linked", "sample-secret", _secret("sample-secret"), None,
                     "refusing a secret path outside envs/<env>/secrets", id="symlinked-secrets"),
        pytest.param(encrypt.seal, "env-test", "demo-token", _secret("demo-token"), "other", NAMESPACE,
                     id="namespace-differs"),
        pytest.param(encrypt.seal, "env-test", "demo-token", _secret("demo-token"), "../x", SEGMENTS,
                     id="namespace-dotdot"),
        pytest.param(encrypt.seal, "env-test", "demo-token", _secret("demo-token"), "Not-Valid", SEGMENTS,
                     id="namespace-not-a-name"),
        pytest.param(encrypt.seal_app_credential, "env-test", "demo", _secret(CREDENTIAL, "platform-backups"), None,
                     "refusing --app: it must be a hosted app namespace, app-<name>", id="app-not-an-app"),
        pytest.param(encrypt.seal_app_credential, "env-test", "app-demo", _secret(CREDENTIAL, "app-demo"), None,
                     NAMESPACE, id="credential-in-the-app-namespace"),
        pytest.param(encrypt.seal, "env-test", CREDENTIAL, _secret(CREDENTIAL, None), "app-demo", WITH_APP,
                     id="namespace-flag-names-an-app"),
        pytest.param(encrypt.seal, "env-test", "demo-token", _secret("demo-token", None), "app-demo",
                     "refusing demo-token: seal an app's backup credential with --app", id="app-namespace-flag"),
        pytest.param(encrypt.seal, "env-test", CREDENTIAL, _secret(CREDENTIAL, "platform-backups"), "platform-backups",
                     WITH_APP, id="credential-in-the-wrong-folder"),
        pytest.param(encrypt.seal, "env-test", CREDENTIAL, _secret(CREDENTIAL, "platform-backups"), None, WITH_APP,
                     id="credential-at-the-top-level"),
        pytest.param(encrypt.seal, "env-test", "sample-secret",
                     "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: sample-secret\ndata:\n  a: b\n", None,
                     NOT_A_SECRET, id="other-kind"),
        pytest.param(encrypt.seal, "env-test", "sample-secret", "- kind: Secret\n", None, NOT_A_SECRET, id="list"),
        pytest.param(encrypt.seal, "env-test", "sample-secret", "just text\n", None, NOT_A_SECRET, id="scalar"),
        pytest.param(encrypt.seal, "env-test", "sample-secret", _secret("another-name"), None,
                     "metadata.name must match --name", id="name-differs"),
    ],
)  # fmt: skip
def test_seal_refuses_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seal, env: str, name: str, plaintext: str, namespace: str | None,
    message: str,
) -> None:  # fmt: skip
    """Rule: a bad env, name or namespace, a path out of envs/<env>/secrets, an app credential placed anywhere but its
    app's folder, or a manifest that is not that Secret is refused with its message before sops runs or a file is made."""
    monkeypatch.setenv("INSTANCE", str(tmp_path))
    for folder in ("Not-valid", "env-test", "env-linked"):
        (tmp_path / "envs" / folder).mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "envs" / "env-linked" / "secrets").symlink_to(tmp_path / "outside")
    bin_dir, marker = support.marker_sops(tmp_path)
    before = _paths(tmp_path)
    kwargs = {} if namespace is None else {"namespace": namespace}

    with pytest.raises(SystemExit) as refused:
        seal(env, name, plaintext, sops_bin=str(bin_dir / "sops"), **kwargs)

    assert refused.value.code == message
    assert not marker.exists() and _paths(tmp_path) == before


def test_seal_runs_sops_last_and_a_failed_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: a call past every refusal runs sops (the refusals' marker would show), and a failed encrypt writes no
    file."""
    monkeypatch.setenv("INSTANCE", str(tmp_path))
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    bin_dir, _marker = support.marker_sops(tmp_path)
    before = _paths(tmp_path)

    with pytest.raises(subprocess.CalledProcessError) as failed:
        encrypt.seal("env-test", "sample-secret", _secret("sample-secret"), sops_bin=str(bin_dir / "sops"))

    assert failed.value.returncode == 99
    assert _paths(tmp_path) == before | {"sops-ran"}


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        pytest.param(["--app", "app-demo", "--name", "other"], "--app takes no --name or --namespace", id="app-and-name"),
        pytest.param(["--app", "app-demo", "--namespace", "demo"], "--app takes no --name or --namespace",
                     id="app-and-namespace"),
        pytest.param([], "--name or --app is required", id="neither"),
    ],
)  # fmt: skip
def test_seal_takes_either_name_or_app(capsys: pytest.CaptureFixture[str], argv: list[str], error: str) -> None:
    """Rule: jarvis encrypt takes --name or --app, and --app takes no --name or --namespace."""
    with pytest.raises(SystemExit) as refused:
        encrypt.main(["--env", "env-test", *argv])

    assert refused.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == f"jarvis encrypt: error: {error}"


@pytest.mark.parametrize(
    ("flags", "rel", "name", "namespace"),
    [
        pytest.param(["--name", "sample-secret"], "envs/env-test/secrets/sample-secret.sops.yaml", "sample-secret",
                     "demo", id="env-secret"),
        pytest.param(["--app", "app-demo"], backups.sealed_rel("env-test", "app-demo"), CREDENTIAL, "platform-backups",
                     id="app-credential"),
    ],
)  # fmt: skip
def test_seal_refuses_to_overwrite_a_sealed_secret_without_force(
    root: Path, flags: list[str], rel: str, name: str, namespace: str
) -> None:
    """Rule: jarvis encrypt replaces a sealed file, an app's credential too, only with --force."""
    plain = root / "plain.yaml"
    argv = ["--env", "env-test", *flags, str(plain)]
    plain.write_text(_secret(name, namespace), encoding="utf-8")
    assert encrypt.main(argv) == 0
    sealed = (root / rel).read_bytes()
    plain.write_text(_secret(name, namespace, token="SECOND-VALUE"), encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        encrypt.main(argv)
    assert refused.value.code == f"{root / rel} exists; pass --force to replace"
    assert (root / rel).read_bytes() == sealed

    assert encrypt.main([*argv, "--force"]) == 0
    assert _opened(root, rel) == _doc(name, namespace, token="SECOND-VALUE")
