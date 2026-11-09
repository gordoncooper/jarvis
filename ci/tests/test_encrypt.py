"""encrypt and bootstrap-secrets refuse a path that is not envs/<env>/secrets."""

from __future__ import annotations

import importlib
import os
import shlex
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import yaml

_PRUNE = "kustomize.toolkit.fluxcd.io/prune"


def _load_encrypt():
    return importlib.import_module("jarvis.secrets.encrypt")


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (required for this test)")
    return path


def _sops_rules(root: Path, public_key: str) -> None:
    (root / ".sops.yaml").write_text(
        textwrap.dedent(
            f"""
            creation_rules:
              - path_regex: envs/.*/secrets/.*\\.sops\\.yaml$
                encrypted_regex: ^(data|stringData)$
                age: {public_key}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _age_keypair(tmp_path: Path) -> tuple[str, Path]:
    age_keygen = _require_tool("age-keygen")
    keyfile = tmp_path / "age-key.txt"
    subprocess.check_call([age_keygen, "-o", str(keyfile)], stdout=subprocess.DEVNULL)
    keyfile.chmod(0o600)
    private = keyfile.read_text(encoding="utf-8")
    public = next(line for line in private.splitlines() if line.startswith("# public key:"))
    return public.split(":", 1)[1].strip(), keyfile


def _marker_sops(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "sops-ran"
    wrapper = tmp_path / "sops-wrap"
    wrapper.write_text(
        "#!/bin/sh\n" + f"touch {shlex.quote(str(marker))}\n" + "exit 99\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper, marker


def _secret(name: str) -> str:
    return (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  namespace: demo\n"
        "type: Opaque\n"
        "stringData:\n"
        "  token: PLAINTEXT-MARKER\n"
    )


def _bind_repo(monkeypatch: pytest.MonkeyPatch, mod, root: Path) -> None:
    monkeypatch.setattr(mod, "REPO_ROOT", root)
    monkeypatch.setattr(mod, "_repo_root", lambda: root)


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ("Not-valid", "sample-secret"),
        ("..", "sample-secret"),
        ("a/b", "sample-secret"),
        ("env-test", ".."),
        ("env-test", "a/b"),
        ("env-test", "Not-Valid"),
    ],
)
def test_seal_refuses_bad_segments_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, name: str
) -> None:
    mod = _load_encrypt()
    _bind_repo(monkeypatch, mod, tmp_path)
    for folder in ("Not-valid", "env-test"):
        (tmp_path / "envs" / folder).mkdir(parents=True)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        mod.seal(env, name, _secret("sample-secret"), sops_bin=str(wrapper))
    assert not marker.exists()


def test_seal_refuses_a_missing_env_folder_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_encrypt()
    _bind_repo(monkeypatch, mod, tmp_path)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        mod.seal("env-missing", "sample-secret", _secret("sample-secret"), sops_bin=str(wrapper))
    assert not marker.exists()
    assert not (tmp_path / "envs" / "env-missing").exists()


def test_seal_refuses_a_symlinked_secrets_dir_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_encrypt()
    _bind_repo(monkeypatch, mod, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    env_dir = tmp_path / "envs" / "env-test"
    env_dir.mkdir(parents=True)
    (env_dir / "secrets").symlink_to(outside)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        mod.seal("env-test", "sample-secret", _secret("sample-secret"), sops_bin=str(wrapper))
    assert not marker.exists()
    assert list(outside.iterdir()) == []


def test_seal_writes_a_valid_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_encrypt()
    public, _keyfile = _age_keypair(tmp_path)
    _sops_rules(tmp_path, public)
    _bind_repo(monkeypatch, mod, tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    out = mod.seal("env-test", "sample-secret", _secret("sample-secret"), sops_bin=_require_tool("sops"))
    text = out.read_text(encoding="utf-8")
    assert out.parent == tmp_path / "envs" / "env-test" / "secrets"
    assert "PLAINTEXT-MARKER" not in text
    assert "ENC[" in text
    loaded = yaml.safe_load(text)
    assert loaded["metadata"]["name"] == "sample-secret"
    assert loaded["stringData"]["token"].startswith("ENC[")


def test_seal_writes_an_addon_secret_in_its_namespace_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An add-on's Secret is sealed under envs/<env>/secrets/<namespace>/, in that namespace."""
    mod = _load_encrypt()
    public, _keyfile = _age_keypair(tmp_path)
    _sops_rules(tmp_path, public)
    _bind_repo(monkeypatch, mod, tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    out = mod.seal("env-test", "demo-token", _secret("demo-token"), sops_bin=_require_tool("sops"), namespace="demo")
    assert out.parent == tmp_path / "envs" / "env-test" / "secrets" / "demo"
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["metadata"]["namespace"] == "demo"
    assert loaded["stringData"]["token"].startswith("ENC[")


@pytest.mark.parametrize("namespace", ["other", "../x", "Not-Valid"])
def test_seal_refuses_a_bad_or_mismatched_namespace_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    mod = _load_encrypt()
    _bind_repo(monkeypatch, mod, tmp_path)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        mod.seal("env-test", "demo-token", _secret("demo-token"), sops_bin=str(wrapper), namespace=namespace)
    assert not marker.exists()


def _app_credential(namespace: str = "platform-backups") -> str:
    return _secret("backups-restic-app-demo").replace("namespace: demo", f"namespace: {namespace}")


def test_seal_writes_an_app_credential_where_the_schema_expects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app's credential sits in its app's folder, and its Secret stays in platform-backups."""
    mod = _load_encrypt()
    public, _keyfile = _age_keypair(tmp_path)
    _sops_rules(tmp_path, public)
    _bind_repo(monkeypatch, mod, tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    out = mod.seal_app_credential("env-test", "app-demo", _app_credential(), sops_bin=_require_tool("sops"))
    assert out == tmp_path / mod._app_backups.sealed_rel("env-test", "app-demo")
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["metadata"]["namespace"] == "platform-backups"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda mod, sops: mod.seal_app_credential("env-test", "demo", _app_credential(), sops_bin=sops), id="not-an-app"),
        pytest.param(
            lambda mod, sops: mod.seal_app_credential("env-test", "app-demo", _app_credential("app-demo"), sops_bin=sops),
            id="secret-in-the-app-namespace",
        ),
        pytest.param(
            lambda mod, sops: mod.seal(
                "env-test", "backups-restic-app-demo", _secret("backups-restic-app-demo").replace("  namespace: demo\n", ""), sops_bin=sops, namespace="app-demo"
            ),
            id="namespace-flag-names-an-app",
        ),
        pytest.param(
            lambda mod, sops: mod.seal(
                "env-test", "backups-restic-app-demo", _app_credential(), sops_bin=sops, namespace="platform-backups"
            ),
            id="credential-name-in-the-wrong-folder",
        ),
        pytest.param(
            lambda mod, sops: mod.seal("env-test", "backups-restic-app-demo", _app_credential(), sops_bin=sops),
            id="credential-name-at-the-top-level",
        ),
    ],
)
def test_seal_refuses_an_app_credential_that_would_land_wrong_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call
) -> None:
    mod = _load_encrypt()
    _bind_repo(monkeypatch, mod, tmp_path)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        call(mod, str(wrapper))
    assert not marker.exists()


@pytest.mark.parametrize(
    "extra",
    [["--app", "app-demo", "--name", "other"], ["--app", "app-demo", "--namespace", "demo"], []],
)
def test_seal_takes_either_name_or_app(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        _load_encrypt().main(["--env", "env-test", *extra])


def test_seal_refuses_to_overwrite_a_sealed_secret_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_encrypt()
    public, _keyfile = _age_keypair(tmp_path)
    _sops_rules(tmp_path, public)
    _bind_repo(monkeypatch, mod, tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    sops_bin = _require_tool("sops")
    out = mod.seal("env-test", "sample-secret", _secret("sample-secret"), sops_bin=sops_bin)
    sealed = out.read_text(encoding="utf-8")

    other = _secret("sample-secret").replace("PLAINTEXT-MARKER", "SECOND-VALUE")
    with pytest.raises(SystemExit, match="--force"):
        mod.seal("env-test", "sample-secret", other, sops_bin=sops_bin)
    assert out.read_text(encoding="utf-8") == sealed

    mod.seal("env-test", "sample-secret", other, sops_bin=sops_bin, force=True)
    assert out.read_text(encoding="utf-8") != sealed


@pytest.mark.parametrize(
    "plaintext",
    [
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: sample-secret\ndata:\n  a: b\n",
        "- kind: Secret\n",
        "just text\n",
    ],
    ids=["other-kind", "list", "scalar"],
)
def test_seal_refuses_input_that_is_not_a_secret_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plaintext: str
) -> None:
    mod = _load_encrypt()
    _bind_repo(monkeypatch, mod, tmp_path)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit, match="kind: Secret"):
        mod.seal("env-test", "sample-secret", plaintext, sops_bin=str(wrapper))
    assert not marker.exists()
    assert not (tmp_path / "envs" / "env-test" / "secrets").exists()


def test_seal_refuses_a_manifest_name_that_differs_from_name_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_encrypt()
    _bind_repo(monkeypatch, mod, tmp_path)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit, match="metadata.name"):
        mod.seal("env-test", "sample-secret", _secret("another-name"), sops_bin=str(wrapper))
    assert not marker.exists()


def test_sops_set_adds_prune_annotation_without_changing_data(tmp_path: Path) -> None:
    """Owner command for the CA annotation. Throwaway key and file only."""
    public, keyfile = _age_keypair(tmp_path)
    _sops_rules(tmp_path, public)
    secret = tmp_path / "envs" / "env-test" / "secrets" / "cert-manager-ca.sops.yaml"
    secret.parent.mkdir(parents=True)
    plain = textwrap.dedent(
        """
        apiVersion: v1
        kind: Secret
        metadata:
            name: cert-manager-ca
            namespace: platform-cert-manager
        type: kubernetes.io/tls
        stringData:
            tls.crt: CERT
            tls.key: KEY
        """
    ).lstrip()
    sops_bin = _require_tool("sops")
    ciphertext = subprocess.check_output(
        [sops_bin, "encrypt", "--filename-override=envs/env-test/secrets/cert-manager-ca.sops.yaml", "/dev/stdin"],
        cwd=tmp_path,
        input=plain,
        text=True,
    )
    secret.write_text(ciphertext, encoding="utf-8")
    before = yaml.safe_load(ciphertext)
    stamp = before["sops"]["lastmodified"]
    deadline = time.time() + 2
    while time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) == stamp and time.time() < deadline:
        time.sleep(0.05)
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    subprocess.check_call(
        [
            sops_bin,
            "set",
            "envs/env-test/secrets/cert-manager-ca.sops.yaml",
            '["metadata"]["annotations"]',
            '{"kustomize.toolkit.fluxcd.io/prune":"disabled"}',
        ],
        cwd=tmp_path,
        env=env,
    )
    after = yaml.safe_load(secret.read_text(encoding="utf-8"))
    decrypted = yaml.safe_load(
        subprocess.check_output([sops_bin, "-d", str(secret)], env=env, text=True)
    )
    assert decrypted["stringData"] == {"tls.crt": "CERT", "tls.key": "KEY"}
    assert decrypted["metadata"]["annotations"][_PRUNE] == "disabled"
    assert before["stringData"] == after["stringData"]
    assert before["sops"]["mac"] != after["sops"]["mac"]
    assert before["sops"]["lastmodified"] != after["sops"]["lastmodified"]
    assert before["sops"]["version"] == after["sops"]["version"]
    assert before["sops"]["age"] == after["sops"]["age"]
